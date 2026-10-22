"""Master-data check: compare the parser's extraction with what the fuzzy matcher finds in the same parsed text.

Ported from ``TruFru-exp/experiments/v1/Databricks_parser/master_check.py``. The matcher
(``packet_parser.fuzzy_matcher``) reads the document text on its own and looks it up in
``docs/official-docs/master_*.csv``. This module then checks the extracted item number, origin, organic
tick-box, lots and cut words against that verdict and the master data. It only adds flags
(``needs_review``); it never changes or rejects the extraction.

  match = match_text(parsed_text, matcher)        # once per document
  block = master_match(packet_dict, match, matcher.md)
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .fuzzy_matcher import config as mconfig
from .fuzzy_matcher.master_data import MasterData
from .fuzzy_matcher.normalize import canon_phrase, compact, fold, repair_item_code


def matcher_item(match: dict) -> Optional[str]:
    """The item the matcher settled on: the chosen item, else a new-variant code such as C01547.AD."""
    if match.get("item_code"):
        return match["item_code"]
    variants = {v["keyword"] for v in match.get("evidence", {}).get("new_variants", [])}
    return next(iter(variants)) if len(variants) == 1 else None


# ------------------------------------------------------------------ helpers
def _get(d: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _item(v: Optional[str]) -> Optional[str]:
    """'C01547. AD' / 'CO1547AD' -> 'C01547.AD' (the matcher's OCR repair); other codes just compacted."""
    c = compact(v or "")
    if not c:
        return None
    code, _ = repair_item_code(c)
    return code or c


def _country(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    u = re.sub(r"\s+", " ", v.upper()).strip()
    u = mconfig.COUNTRY_ALIASES.get(u, u)
    for c in mconfig.KNOWN_COUNTRIES:
        if re.search(rf"(?<![A-Z]){c}(?![A-Z])", u):
            return c
    return u


def _lot(v: Optional[str]) -> Optional[str]:
    """Same rule as checks.norm_lot: first token before / , ; with spaces removed."""
    if not v:
        return None
    return compact(re.split(r"[/,;]", v)[0]) or None


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


# ------------------------------------------------------------------ the checks
def _item_check(packet: dict, item: Optional[str], products: dict) -> dict:
    raw = {"imir": _get(packet, "inspection_report.item_number"),
           "traceability": _get(packet, "traceability.item_number")}
    for i, line in enumerate(_get(packet, "purchase_order.lines") or []):
        if line.get("our_item_number"):
            raw[f"po_line{i + 1}"] = line["our_item_number"]
    got = {k: _item(v) for k, v in raw.items() if _item(v)}
    shown = ", ".join(f"{k}={v!r}" for k, v in got.items()) or "none"
    if not got:
        return _check("master_item_code", "unknown", f"parser gave no item number; matcher={item or 'none'}")
    if not item:
        return _check("master_item_code", "unknown", f"matcher found no single item; parser {shown}")
    # PO freight lines etc. carry other codes; only compare values from the same family as the matcher's item
    family = {k: v for k, v in got.items() if v.split(".")[0] == item.split(".")[0]} or got
    wrong = {k: v for k, v in family.items() if v != item}
    # an item missing from master data is the matcher's own flag (unknown_item_code); here only agreement counts
    known = "in master data" if item in products else "NOT in master data"
    if wrong:
        return _check("master_item_code", "fail",
                      f"matcher={item} ({known}); parser differs: " + ", ".join(f"{k}={raw[k]!r}" for k in wrong))
    return _check("master_item_code", "pass", f"matcher={item} ({known}); parser {shown}")


def _origin_check(packet: dict, match: dict, item: Optional[str], products: dict) -> dict:
    raw = {"imir": _get(packet, "inspection_report.country_of_origin"),
           "traceability": _get(packet, "traceability.country_of_origin")}
    got = {k: _country(v) for k, v in raw.items() if _country(v)}
    seen = match["observed"]["origins"]
    if not got:
        return _check("master_origin", "unknown", f"parser gave no origin; page mentions {', '.join(seen) or 'none'}")
    problems = []
    not_on_page = {k: v for k, v in got.items() if seen and v not in seen}
    if not_on_page:
        problems.append("parser origin not found in the text: " + ", ".join(f"{k}={v}" for k, v in not_on_page.items()))
    master_origin = (products.get(item, {}).get("origin") or "").upper() if item else ""
    if master_origin and any(v != master_origin for v in got.values()):
        problems.append(f"{item} is {master_origin} in master_products")
    shown = ", ".join(f"{k}={v}" for k, v in got.items())
    detail = f"parser {shown}; page mentions {', '.join(seen) or 'none'}" + (f" | {'; '.join(problems)}" if problems else "")
    return _check("master_origin", "fail" if problems else "pass", detail)


def _organic_check(packet: dict, match: dict, item: Optional[str], products: dict) -> dict:
    got = _get(packet, "inspection_report.organic")
    seen = match["observed"]["organic"]          # yes | no | unclear | mentioned | absent
    master = products.get(item, {}).get("organic") if item else None
    detail = f"parser tick-box={got or 'none'}; matcher tick-box={seen}" + (f"; {item} organic={master}" if master else "")
    if got not in ("yes", "no"):
        return _check("master_organic", "unknown", detail)
    if seen in ("yes", "no") and got != seen:
        return _check("master_organic", "fail", detail + " | parser and matcher read the tick-box differently")
    if master in ("Y", "N") and (got == "yes") != (master == "Y"):
        return _check("master_organic", "fail", detail + " | tick-box contradicts the item in master_products")
    return _check("master_organic", "pass" if seen in ("yes", "no") else "unknown", detail)


def _lot_check(packet: dict, match: dict, item: Optional[str], lots: dict) -> dict:
    raw = {"traceability.supplier_lot": _get(packet, "traceability.supplier_lot"),
           "traceability.vdf_lot_number": _get(packet, "traceability.vdf_lot_number"),
           "po.handwritten_lot": _get(packet, "purchase_order.handwritten_lot")}
    for i, l in enumerate(_get(packet, "inspection_report.lots") or []):
        raw[f"imir.lot{i + 1}.manufacturing_lot"] = l.get("manufacturing_lot")
        raw[f"imir.lot{i + 1}.vdf_lot_number"] = l.get("vdf_lot_number")
    for i, l in enumerate(_get(packet, "bill_of_lading.lines") or []):
        raw[f"bol.line{i + 1}.lot"] = l.get("lot")
    got = {k: _lot(v) for k, v in raw.items() if _lot(v)}
    by_compact = {compact(r["lot_compact"] or r["lot"]): r for r in lots.values()}
    by_fold = {fold(c): r for c, r in by_compact.items()}

    known, unknown, wrong_item = [], [], []
    for value in sorted(set(got.values())):
        row = by_compact.get(value) or by_fold.get(fold(value))
        if not row:
            unknown.append(value)
        elif item and row["item_code"] != item:
            wrong_item.append(f"{value} -> {row['item_code']}")
        else:
            known.append(f"{value} -> {row['item_code']}")
    got_lots = {fold(v) for v in got.values()}
    missed = [l["lot"] for l in match.get("lots_found", []) if fold(l["lot"]) not in got_lots]

    parts = []
    if known:
        parts.append(f"known: {', '.join(known)}")
    if wrong_item:
        parts.append(f"under another item in master_lots: {', '.join(wrong_item)}")
    if unknown:
        parts.append(f"not in master_lots: {', '.join(unknown)}")
    if missed:
        parts.append(f"matcher found in text but parser did not extract: {', '.join(missed)}")
    if not got and not missed:
        return _check("master_lots", "unknown", "parser gave no lots and the matcher found none")
    # master_lots only holds lots already consumed in production, so a new receiving lot is expected to be missing:
    # that alone is not flagged. A known lot under another item (fail) or a known lot the parser skipped (warn) is.
    status = "fail" if wrong_item else "warn" if missed else "pass" if known else "unknown"
    return _check("master_lots", status, "; ".join(parts))


def _cut_check(packet: dict, item: Optional[str], products: dict) -> dict:
    texts = [_get(packet, "inspection_report.product_description"), _get(packet, "traceability.product_description")]
    texts += [l.get("item_description") for l in _get(packet, "purchase_order.lines") or []]
    texts += [x for l in _get(packet, "bill_of_lading.lines") or [] for x in (l.get("item_description"), l.get("lot_description"))]
    words = set(canon_phrase(" ".join(t for t in texts if t)).split())
    cuts = sorted({v for k, v in mconfig.CUT_WORDS.items() if k in words})
    master = products.get(item, {}).get("cut") if item else None
    detail = f"parser descriptions say {', '.join(cuts) or 'no cut'}" + (f"; {item} is {master}" if master else "")
    if not cuts or not master:
        return _check("master_cut", "unknown", detail + ("" if master else f"; {item or 'item'} has no cut in master data"))
    other = [c for c in cuts if c != master]
    return _check("master_cut", "fail" if other else "pass", detail)


# ------------------------------------------------------------------ public
def master_checks(packet: dict, match: dict, md: MasterData) -> list[dict]:
    item = matcher_item(match)
    return [
        _item_check(packet, item, md.products),
        _origin_check(packet, match, item, md.products),
        _organic_check(packet, match, item, md.products),
        _lot_check(packet, match, item, md.lots),
        _cut_check(packet, item, md.products),
    ]


def master_match(packet: dict, match: dict, md: MasterData) -> dict:
    """Block stored as ``ParsedPacket.master_match``: matcher verdict + checks of the extraction against it. Flags only."""
    checks = master_checks(packet, match, md)
    flags = [f"matcher:{f['code']}" for f in match["flags"]]
    flags += [f"{c['name']}:{c['status']}" for c in checks if c["status"] in ("fail", "warn")]
    return {
        "matcher_status": match["status"],
        "matcher_item": matcher_item(match),
        "matcher_candidates": [c["item_code"] for c in match["candidates"]],
        "matcher_flags": match["flags"],
        "observed": match["observed"],
        "lots_found": match["lots_found"],
        "checks": checks,
        "flags": flags,
        "needs_review": bool(match["needs_review"] or any(c["status"] in ("fail", "warn") for c in checks)),
    }
