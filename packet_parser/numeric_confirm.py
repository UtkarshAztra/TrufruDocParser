"""Confirm every number in a parsed receiving packet, not just the result of a calculation.

Ported from ``TruFru-exp/experiments/v1/interdependent_numeric_data/numeric_confirm.py``.

For `quantity_cases x case_weight_lbs = net_weight_lbs` all three numbers are checked: each one collects
*witnesses* from every rule it takes part in, and from the printed text (see text_reader.py).

  quantity_cases   <- R1 (cases x weight = net), R7 (= sum IMIR boxes, = sum BOL product qty), R12 (pallets)
  case_weight_lbs  <- R1, R11 (= IMIR weight/box, = BOL unit weight)
  net_weight_lbs   <- R1, R8 (= sum IMIR totals, = sum BOL product net), R10 (PO ordered lbs), R6 (gross >= net)

A field's status comes from its witnesses:
  CONFLICT         a strict rule or the printed text disagrees with it
  CONFIRMED_TEXT   the printed text agrees (and nothing strict disagrees)
  CONFIRMED_MATH   at least one strict rule agrees
  WARN             only a soft rule disagrees (ordered vs received, pallets)
  WEAK             only soft support (gross >= net, number seen somewhere in the text)
  UNVERIFIED       nothing to check it against (e.g. handwriting with no partner number)

When a strict rule fails, the member with the least support from *other* witnesses is named as the likely
wrong number, with the value the rule says it should have. Flags only: no value is ever changed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .text_reader import PrintedNumbers, compact

STRICT, SOFT, BOUND, NONE = "strict", "soft", "bound", "none"
NON_PRODUCT_RE = re.compile(r"tracker|recorder|sensitech|\btemp\b|-temp\b", re.I)
FROZEN_MAX_F, REFRIGERATED_MAX_F = 10.0, 41.0
STATUSES = ["CONFLICT", "WARN", "CONFIRMED_TEXT", "CONFIRMED_MATH", "WEAK", "UNVERIFIED", "MISSING"]


def fmt(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


# ------------------------------------------------------------------ fields
@dataclass
class Packet:
    values: dict[str, Optional[float]]            # field key -> number (None = missing)
    imir_lots: int = 0
    bol_lines: int = 0
    bol_product: list[int] = field(default_factory=list)      # BOL line indexes (0-based) that are product
    bol_non_product: list[int] = field(default_factory=list)  # e.g. temperature tracker
    bol_lots: dict[int, str] = field(default_factory=dict)
    po_lb_lines: list[int] = field(default_factory=list)
    temp_class: Optional[str] = None
    notes_text: str = ""
    imir_pallet_rows: list[int] = field(default_factory=list)  # IMIR lots written per pallet (set by build_rules)


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        found = re.findall(r"-?\d[\d,]*\.?\d*", str(v))
        return float(found[0].replace(",", "")) if found else None


def collect(packet: dict) -> Packet:
    """Flatten the numeric fields of a ReceivingPacket (or a bare {'traceability': ...} sample) into keys."""
    v: dict[str, Optional[float]] = {}
    t = packet.get("traceability") or {}
    for k in ("quantity_cases", "case_weight_lbs", "net_weight_lbs", "gross_weight_lbs", "pallets", "product_temperature_f"):
        v[f"trace.{k}"] = _num(t.get(k))

    imir = packet.get("inspection_report") or {}
    lots = imir.get("lots") or []
    for i, lot in enumerate(lots, 1):
        for k in ("boxes_received", "weight_per_box_lbs", "total_weight_lbs"):
            v[f"imir.lot[{i}].{k}"] = _num(lot.get(k))
    v["imir.product_temperature_f"] = _num(imir.get("product_temperature_f"))
    v["imir.damaged_boxes_count"] = _num(imir.get("damaged_boxes_count"))

    bol = packet.get("bill_of_lading") or {}
    lines = bol.get("lines") or []
    product, non_product, lot_codes = [], [], {}
    for i, line in enumerate(lines, 1):
        for k in ("actual_qty", "unit_weight_lbs", "net_weight_lbs"):
            v[f"bol.line[{i}].{k}"] = _num(line.get(k))
        desc = " ".join(str(line.get(k) or "") for k in ("item_code", "item_description", "lot_description"))
        unit = _num(line.get("unit_weight_lbs"))
        (non_product if NON_PRODUCT_RE.search(desc) or (unit is not None and unit < 1) else product).append(i - 1)
        if line.get("lot"):
            lot_codes[i - 1] = str(line["lot"])
    for k in ("total_qty", "total_net_weight_lbs", "total_gross_weight_lbs"):
        v[f"bol.{k}"] = _num(bol.get(k))

    po = packet.get("purchase_order") or {}
    po_lb = []
    for i, line in enumerate(po.get("lines") or [], 1):
        if (line.get("unit_of_measure") or "").strip().upper() in ("LB", "LBS") and _num(line.get("quantity")) is not None:
            v[f"po.line[{i}].quantity"] = _num(line.get("quantity"))
            po_lb.append(i - 1)
    notes = " ".join([str(x) for x in po.get("handwritten_notes") or []] + [str(po.get("handwritten_lot") or "")])

    return Packet(values=v, imir_lots=len(lots), bol_lines=len(lines), bol_product=product,
                  bol_non_product=non_product, bol_lots=lot_codes, po_lb_lines=po_lb,
                  temp_class=imir.get("product_temperature_class"), notes_text=notes)


# ------------------------------------------------------------------ rules
@dataclass
class Rule:
    id: str
    name: str
    kind: str                       # product | sum | ge | custom
    lhs: list[str]                  # product: [a, b]; sum/ge: keys added on the left
    rhs: list[str]                  # product: [c];    sum/ge: keys added on the right
    evidence: str = STRICT          # strict | soft | bound (pass = soft support, fail = conflict) | none (report only)
    tol: float = 0.5
    custom: Optional[Callable[[dict[str, float]], tuple[bool, dict[str, float], str]]] = None

    @property
    def keys(self) -> list[str]:
        return self.lhs + self.rhs


@dataclass
class RuleResult:
    rule: Rule
    status: str                     # PASS | FAIL | WARN | SKIP
    formula: str
    should_be: dict[str, float] = field(default_factory=dict)   # value each member would need for the rule to hold
    missing: list[str] = field(default_factory=list)
    fill: dict[str, float] = field(default_factory=dict)        # value a single missing member would have
    suspects: list[str] = field(default_factory=list)


def build_rules(p: Packet, printed: Optional[PrintedNumbers]) -> list[Rule]:
    R: list[Rule] = []
    T = "trace."

    # ---- A. arithmetic inside one document
    R.append(Rule("R1", "traceability: cases x case weight = net", "product",
                  [T + "quantity_cases", T + "case_weight_lbs"], [T + "net_weight_lbs"]))
    for i in range(1, p.imir_lots + 1):
        b = f"imir.lot[{i}]."
        R.append(Rule(f"R2.{i}", f"IMIR lot {i}: boxes x weight/box = total", "product",
                      [b + "boxes_received", b + "weight_per_box_lbs"], [b + "total_weight_lbs"]))
    for i in range(1, p.bol_lines + 1):
        b = f"bol.line[{i}]."
        R.append(Rule(f"R3.{i}", f"BOL line {i}: qty x unit weight = net", "product",
                      [b + "actual_qty", b + "unit_weight_lbs"], [b + "net_weight_lbs"]))
    all_lines = range(1, p.bol_lines + 1)
    if p.bol_lines:
        R.append(Rule("R4", "BOL: sum of line qty = total qty", "sum",
                      [f"bol.line[{i}].actual_qty" for i in all_lines], ["bol.total_qty"], tol=0.5))
        R.append(Rule("R5", "BOL: sum of line net = total net", "sum",
                      [f"bol.line[{i}].net_weight_lbs" for i in all_lines], ["bol.total_net_weight_lbs"], tol=1.0))
    R.append(Rule("R6.bol", "BOL: gross >= net", "ge", ["bol.total_gross_weight_lbs"], ["bol.total_net_weight_lbs"], evidence=BOUND))
    R.append(Rule("R6.trace", "traceability: gross >= net", "ge", [T + "gross_weight_lbs"], [T + "net_weight_lbs"], evidence=BOUND))

    # ---- B. the same number on different documents
    case_lots, pallet_lots = _split_imir_lots(p)
    imir_boxes = [f"imir.lot[{i}].boxes_received" for i in case_lots]
    imir_totals = [f"imir.lot[{i}].total_weight_lbs" for i in range(1, p.imir_lots + 1)]
    if pallet_lots:
        # IMIR rows written per pallet ('19 x 2100 lb'): the count column holds pallets, not boxes
        R.append(Rule("R12.imir", "pallets: traceability = sum IMIR pallet rows (soft)", "sum",
                      [T + "pallets"], [f"imir.lot[{i}].boxes_received" for i in pallet_lots], evidence=SOFT))
    bol_qty = [f"bol.line[{i + 1}].actual_qty" for i in p.bol_product]
    bol_net = [f"bol.line[{i + 1}].net_weight_lbs" for i in p.bol_product]
    if imir_boxes:
        R.append(Rule("R7.imir", "cases: traceability = sum IMIR boxes", "sum", [T + "quantity_cases"], imir_boxes))
    if bol_qty:
        R.append(Rule("R7.bol", "cases: traceability = sum BOL product qty", "sum", [T + "quantity_cases"], bol_qty))
    if imir_boxes and bol_qty:
        R.append(Rule("R7.imir_bol", "cases: sum IMIR boxes = sum BOL product qty", "sum", imir_boxes, bol_qty))
    if imir_totals:
        R.append(Rule("R8.imir", "net: traceability = sum IMIR totals", "sum", [T + "net_weight_lbs"], imir_totals, tol=1.0))
    if bol_net:
        R.append(Rule("R8.bol", "net: traceability = sum BOL product net", "sum", [T + "net_weight_lbs"], bol_net, tol=1.0))
    R.append(Rule("R9", "gross: traceability = BOL total gross", "sum", [T + "gross_weight_lbs"], ["bol.total_gross_weight_lbs"]))
    po_keys = [f"po.line[{i + 1}].quantity" for i in p.po_lb_lines]
    if po_keys:
        R.append(Rule("R10", "net received = PO ordered lbs (soft: short shipments are real)", "sum",
                      [T + "net_weight_lbs"], po_keys, evidence=SOFT, tol=1.0))
    for i in case_lots:
        R.append(Rule(f"R11.imir{i}", f"case weight: traceability = IMIR lot {i} weight/box", "sum",
                      [T + "case_weight_lbs"], [f"imir.lot[{i}].weight_per_box_lbs"]))
    for i in p.bol_product:
        R.append(Rule(f"R11.bol{i + 1}", f"case weight: traceability = BOL line {i + 1} unit weight", "sum",
                      [T + "case_weight_lbs"], [f"bol.line[{i + 1}].unit_weight_lbs"]))

    # ---- C. rough rules and spec checks
    per_pallet = _cases_per_pallet(p, printed)
    if per_pallet:
        def pallets_rule(vals: dict[str, float], n=per_pallet) -> tuple[bool, dict[str, float], str]:
            need = math.ceil(vals[T + "quantity_cases"] / n - 1e-9)
            ok = vals[T + "pallets"] == need
            return ok, {T + "pallets": need}, f"ceil({fmt(vals[T + 'quantity_cases'])} / {fmt(n)} per pallet) = {need}  vs  {fmt(vals[T + 'pallets'])}"
        R.append(Rule("R12", f"pallets = ceil(cases / {fmt(per_pallet)}) (soft)", "custom",
                      [T + "quantity_cases"], [T + "pallets"], evidence=SOFT, custom=pallets_rule))
    R.append(Rule("R13", "temperature: traceability = IMIR", "sum", [T + "product_temperature_f"], ["imir.product_temperature_f"]))
    limit = {"frozen": FROZEN_MAX_F, "refrigerated": REFRIGERATED_MAX_F}.get(str(p.temp_class or "").lower())
    if limit is not None:
        def temp_rule(vals: dict[str, float], lim=limit) -> tuple[bool, dict[str, float], str]:
            t = vals["imir.product_temperature_f"]
            return t <= lim, {}, f"{fmt(t)} F <= {fmt(lim)} F ({p.temp_class})"
        R.append(Rule("R14", "spec: product temperature within limit (not a read check)", "custom",
                      ["imir.product_temperature_f"], [], evidence=NONE, custom=temp_rule))
    if imir_boxes or bol_qty:
        R.append(Rule("R15", "damaged boxes <= cases received", "ge", [T + "quantity_cases"], ["imir.damaged_boxes_count"], evidence=NONE))
    return R


def _split_imir_lots(p: Packet) -> tuple[list[int], list[int]]:
    """IMIR lot rows (1-based) written per case vs per pallet.

    A row is a pallet row when its weight per 'box' is a whole multiple (>= 2) of the case weight known from
    the traceability row or the BOL, e.g. 2100 lb = 70 x 30 lb. Those rows are left out of the case-count and
    case-weight rules; their totals still count towards the net weight.
    """
    ref = p.values.get("trace.case_weight_lbs")
    if not ref:
        units = [p.values.get(f"bol.line[{i + 1}].unit_weight_lbs") for i in p.bol_product]
        ref = next((u for u in units if u), None)
    p.imir_pallet_rows = []
    cases, pallets = [], []
    for i in range(1, p.imir_lots + 1):
        w = p.values.get(f"imir.lot[{i}].weight_per_box_lbs")
        ratio = (w / ref) if (w and ref) else 0
        if ratio >= 2 and abs(ratio - round(ratio)) < 0.01:
            pallets.append(i)
        else:
            cases.append(i)
    p.imir_pallet_rows = pallets
    return cases, pallets


def _cases_per_pallet(p: Packet, printed: Optional[PrintedNumbers]) -> Optional[float]:
    """'70 boxes/full pallet' from the PO notes or the text; else lbs-per-pallet / case weight."""
    found = [float(x) for x in re.findall(r"(\d+)\s*(?:boxes|cases|cs)\s*(?:/|per|@)\s*(?:full\s+)?pallet", p.notes_text, re.I)]
    if printed:
        found += printed.cases_per_pallet
    if found:
        return found[0]
    lbs = [float(x.replace(",", "")) for x in re.findall(r"(\d[\d,]*)\s*lbs?\s*(?:/|per|@)?\s*(?:full\s+)?pallet(?!-)", p.notes_text, re.I)]
    if printed:
        lbs += printed.lbs_per_pallet
    w = p.values.get("trace.case_weight_lbs")
    if lbs and w:
        return round(lbs[0] / w)
    return None


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= max(tol, 1e-6 * max(abs(a), abs(b)))


def evaluate(rule: Rule, values: dict[str, Optional[float]]) -> RuleResult:
    vals = {k: values.get(k) for k in rule.keys}
    missing = [k for k, v in vals.items() if v is None]

    if rule.kind == "product":
        (a, b), (c,) = rule.lhs, rule.rhs
        if missing:
            fill = {}
            if len(missing) == 1:
                m = missing[0]
                if m == c:
                    fill[c] = vals[a] * vals[b]
                elif m == a and vals[b]:
                    fill[a] = vals[c] / vals[b]
                elif m == b and vals[a]:
                    fill[b] = vals[c] / vals[a]
            return RuleResult(rule, "SKIP", "missing " + ", ".join(missing), missing=missing, fill=fill)
        prod = vals[a] * vals[b]
        ok = _close(prod, vals[c], rule.tol)
        should = {c: prod}
        if vals[b]:
            should[a] = vals[c] / vals[b]
        if vals[a]:
            should[b] = vals[c] / vals[a]
        return RuleResult(rule, "PASS" if ok else ("WARN" if rule.evidence == SOFT else "FAIL"),
                          f"{fmt(vals[a])} x {fmt(vals[b])} = {fmt(prod)}  vs  {fmt(vals[c])}", should_be=should)

    if rule.kind in ("sum", "ge"):
        present_l = [k for k in rule.lhs if vals[k] is not None]
        present_r = [k for k in rule.rhs if vals[k] is not None]
        if missing:
            fill = {}
            if rule.kind == "sum" and len(missing) == 1:
                m = missing[0]
                ls = sum(vals[k] for k in present_l)
                rs = sum(vals[k] for k in present_r)
                fill[m] = (rs - ls) if m in rule.lhs else (ls - rs)
            return RuleResult(rule, "SKIP", "missing " + ", ".join(missing), missing=missing, fill=fill)
        ls = sum(vals[k] for k in rule.lhs)
        rs = sum(vals[k] for k in rule.rhs)
        show = lambda ks: " + ".join(fmt(vals[k]) for k in ks) + (f" = {fmt(sum(vals[k] for k in ks))}" if len(ks) > 1 else "")
        if rule.kind == "ge":
            ok = ls >= rs - rule.tol
            res = "PASS" if ok else ("WARN" if rule.evidence == SOFT else "FAIL")
            return RuleResult(rule, res, f"{show(rule.lhs)}  >=  {show(rule.rhs)}")
        ok = _close(ls, rs, rule.tol)
        should = {k: rs - (ls - vals[k]) for k in rule.lhs}
        should.update({k: ls - (rs - vals[k]) for k in rule.rhs})
        return RuleResult(rule, "PASS" if ok else ("WARN" if rule.evidence == SOFT else "FAIL"),
                          f"{show(rule.lhs)}  vs  {show(rule.rhs)}", should_be=should)

    # custom
    if missing:
        return RuleResult(rule, "SKIP", "missing " + ", ".join(missing), missing=missing)
    ok, should, formula = rule.custom(vals)  # type: ignore[misc]
    return RuleResult(rule, "PASS" if ok else ("WARN" if rule.evidence in (SOFT, NONE) else "FAIL"), formula, should_be=should)


# ------------------------------------------------------------------ witnesses and field status
@dataclass
class Witness:
    source: str           # rule id, 'text' or 'text-seen'
    strict: bool
    agrees: bool
    how: str
    blamed: bool = True   # False: the rule failed but another number in it is the likely culprit


@dataclass
class FieldReport:
    key: str
    value: Optional[float]
    status: str
    witnesses: list[Witness]
    should_be: Optional[float] = None   # suggested value when the field is the likely wrong one


def rule_witnesses(results: list[RuleResult]) -> dict[str, list[Witness]]:
    out: dict[str, list[Witness]] = {}
    for r in results:
        if r.status == "SKIP" or r.rule.evidence == NONE:
            continue
        ok = r.status == "PASS"
        if r.rule.evidence == STRICT:
            strict = True
        elif r.rule.evidence == SOFT:
            strict = False
        else:  # BOUND: passing proves little, failing proves a misread
            strict = not ok
        for k in r.rule.keys:
            out.setdefault(k, []).append(Witness(r.rule.id, strict, ok, r.formula))
    return out


def text_witnesses(p: Packet, printed: PrintedNumbers) -> dict[str, list[Witness]]:
    out: dict[str, list[Witness]] = {}

    def add(key: str, cands: list[float], where: str, strict: bool = True) -> None:
        v = p.values.get(key)
        if v is None or not cands:
            return
        ok = any(_close(v, c, 0.05) for c in cands)
        out.setdefault(key, []).append(Witness("text", strict, ok, f"printed {where}: {', '.join(fmt(c) for c in cands)}"))

    for row in printed.totals:
        add("bol.total_qty", row.qty, "BOL TOTALS qty")
        add("bol.total_net_weight_lbs", row.net, "BOL TOTALS net")
        add("bol.total_gross_weight_lbs", row.gross, "BOL TOTALS gross")

    for i in range(p.bol_lines):
        key = f"bol.line[{i + 1}]."
        lot = compact(p.bol_lots.get(i))
        row = None
        if len(lot) >= 4:
            row = next((r for r in printed.rows if lot in r.compact), None)
        if row is None:
            q, n = p.values.get(key + "actual_qty"), p.values.get(key + "net_weight_lbs")
            if q is not None and n is not None:
                row = next((r for r in printed.rows if any(_close(q, c, .05) for c in r.qty)
                            and any(_close(n, c, .05) for c in r.net + r.weight_any)), None)
        if row is None:
            continue
        where = f"BOL row '{row.text[:40]}'"
        add(key + "actual_qty", row.qty, where)
        add(key + "unit_weight_lbs", row.unit, where)
        add(key + "net_weight_lbs", row.net, where)
        if row.weight_any and not row.net:  # bare WEIGHT column: only count it when it agrees
            v = p.values.get(key + "net_weight_lbs")
            if v is not None and any(_close(v, c, .05) for c in row.weight_any):
                out.setdefault(key + "net_weight_lbs", []).append(Witness("text", True, True, f"printed {where} WEIGHT"))

    for i in p.po_lb_lines:
        add(f"po.line[{i + 1}].quantity", printed.po_lb, "PO 'NN,NNN.NN LB'")
    add("trace.pallets", printed.pallets, "pallet count in text", strict=False)

    # anything else: a non-trivial number that appears verbatim somewhere in the text is weak support
    for key, v in p.values.items():
        if v is None or abs(v) < 100 or any(w.source == "text" for w in out.get(key, [])):
            continue
        if printed.seen(v):
            out.setdefault(key, []).append(Witness("text-seen", False, True, f"{fmt(v)} appears in the parsed text"))
    return out


def field_status(ws: list[Witness]) -> str:
    if any(w.strict and not w.agrees and w.blamed for w in ws):
        return "CONFLICT"
    if any(w.source == "text" and w.strict and w.agrees for w in ws):
        return "CONFIRMED_TEXT"
    if any(w.strict and w.agrees for w in ws):
        return "CONFIRMED_MATH"
    if any(not w.agrees for w in ws):
        return "WARN"
    if ws:
        return "WEAK"
    return "UNVERIFIED"


def _support(ws: list[Witness], exclude: str) -> float:
    s = 0.0
    for w in ws:
        if w.source == exclude:
            continue
        weight = 1.0 if w.strict else 0.5
        s += weight if w.agrees else -weight
    return s


@dataclass
class Report:
    packet: Packet
    results: list[RuleResult]
    fields: list[FieldReport]
    printed: Optional[PrintedNumbers]

    def count(self, status: str) -> int:
        return sum(1 for f in self.fields if f.status == status)

    @property
    def failed_rules(self) -> list[RuleResult]:
        return [r for r in self.results if r.status == "FAIL"]


def confirm(packet: dict, printed: Optional[PrintedNumbers] = None) -> Report:
    p = collect(packet)
    results = [evaluate(r, p.values) for r in build_rules(p, printed)]
    ws = rule_witnesses(results)
    if printed:
        for k, extra in text_witnesses(p, printed).items():
            ws.setdefault(k, []).extend(extra)

    # who is most likely wrong in each failed strict rule: members with negative support from the *other*
    # witnesses, else the least supported one. The other members are cleared for this rule.
    should: dict[str, float] = {}
    for r in results:
        if r.status != "FAIL" or r.rule.evidence not in (STRICT, BOUND):
            continue
        members = [k for k in r.rule.keys if p.values.get(k) is not None]
        scores = {k: _support(ws.get(k, []), r.rule.id) for k in members}
        if not scores:
            continue
        low = min(scores.values())
        suspects = [k for k, sc in scores.items() if sc < 0] or [k for k, sc in scores.items() if sc == low]
        if len(members) > 1 and len(set(scores.values())) == 1:
            suspects = []  # every member equally supported: cannot tell which one is wrong
        r.suspects = suspects
        if suspects:
            for k in members:
                if k not in suspects:
                    for w in ws.get(k, []):
                        if w.source == r.rule.id:
                            w.blamed = False
        if len(suspects) == 1 and suspects[0] in r.should_be:
            should.setdefault(suspects[0], r.should_be[suspects[0]])

    fields = [FieldReport(k, v, field_status(ws.get(k, [])) if v is not None else "MISSING", ws.get(k, []),
                          should.get(k)) for k, v in p.values.items()]

    # drop fields that are absent from the document and not used by any rule (keeps the report short)
    used = {k for r in results for k in r.rule.keys}
    fields = [f for f in fields if f.value is not None or f.key in used]
    return Report(p, results, fields, printed)


# ------------------------------------------------------------------ JSON block
def to_block(rep: Report) -> dict:
    """The ``numeric_confirm`` block written to the final JSON (same shape as the v1 experiments)."""
    counts = Counter(f.status for f in rep.fields)
    numbers = []
    for f in rep.fields:
        text = [w for w in f.witnesses if w.source == "text" and w.strict]
        numbers.append({
            "field": f.key, "value": f.value, "status": f.status, "should_be": f.should_be,
            "text_match": None if not text else all(w.agrees for w in text),          # A. text match
            "rules_agree": sorted({w.source for w in f.witnesses if w.source.startswith("R") and w.agrees}),   # B. math
            "rules_disagree": sorted({w.source for w in f.witnesses if w.source.startswith("R") and not w.agrees}),
        })
    checked = sum(counts[s] for s in STATUSES if s != "MISSING")
    return {
        "verdict": "ALL NUMBERS CONSISTENT" if not counts["CONFLICT"] else f"{counts['CONFLICT']} NUMBER(S) IN CONFLICT",
        "counts": {s: counts[s] for s in STATUSES if counts[s]},
        "confirmed": counts["CONFIRMED_TEXT"] + counts["CONFIRMED_MATH"],
        "checked": checked,
        "rules": [{"id": r.rule.id, "name": r.rule.name, "status": r.status, "formula": r.formula,
                   "suspects": r.suspects, "should_be": {k: r.should_be[k] for k in r.suspects if k in r.should_be}}
                  for r in rep.results],
        "numbers": numbers,
        "needs_review": bool(counts["CONFLICT"]),
    }


def numeric_confirm(packet: dict, printed: Optional[PrintedNumbers] = None) -> dict:
    """``confirm`` + ``to_block`` in one call."""
    return to_block(confirm(packet, printed))
