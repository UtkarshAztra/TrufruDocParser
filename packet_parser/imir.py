"""VDF/FC Incoming Material Inspection Report (IMIR) -> packet.inspection_report (my_docs section 6)."""

from __future__ import annotations

import re
from typing import Optional

from .common import Ctx, after_label, value_after
from .elements import Page, clean, strip_tags
from .normalize import num, read_ticks, tick_value, to_iso

# printed labels of the form, used to cut a value at the next label
LABELS = [
    r"Receiving Facility", r"Product Description", r"Vendor\s*:", r"Item\s*#", r"Order Number", r"Country of Origin",
    r"Carrier Name", r"Confirm Order", r"Trailer Seal Number", r"Seal Present", r"Allergen", r"Organic\s*:",
    r"Kosher", r"Is there a Packing", r"Is product from", r"All Product Received", r"If Yes, Attach",
    r"Manufacturing Lot", r"Trailer Condition", r"Product Condition", r"Inspector'?s Signature",
    r"Verification Signature", r"Date\s*:", r"4-FRC-COR", r"Returned Product", r"If LTL",
]

TICKS = [  # (field, anchor regex)
    ("order_matches_bol", r"Confirm Order\s*#?\s*matches BOL"),
    ("returned_product", r"Returned Product"),
    ("ltl_truck_padlocked", r"If LTL,?\s*is Truck Padlocked"),
    ("seal_present_matches_bol", r"Seal Present and matches"),
    ("organic", r"Organic\s*:"),
    ("kosher", r"(?<!Symbol on Label: )(?<!Symbol on Label:)\bKosher\s*:"),
    ("kosher_symbol_on_label", r"Kosher Symbol on Label"),
    ("packing_list_or_coa_present", r"Is there a Packing List"),
    ("manufacturer_lot_matches_packing_list", r"Manufacturer'?s Lot\s*#?\s*Match"),
    ("approved_vendor", r"approved Vendor"),
    ("all_product_accounted_for", r"All Product Received is Accounted"),
    ("trailer_clean", r"Inside Trailer Clean"),
    ("trailer_free_of_damage", r"Trailer Free of Damage"),
    ("trailer_free_of_odors", r"Trailer Free of Bad"),
    ("trailer_free_of_pests", r"Trailer Free of Pest"),
    ("trailer_free_of_chemicals", r"Trailer free of chemicals"),
    ("properly_stacked_no_damage", r"Properly Stacked"),
    ("damage_noted_on_bol", r"damage Noted on BOL"),
    ("product_clearly_labeled", r"Is Product Clearly Labeled"),
    ("label_matches_po", r"Label Description match"),
    ("temperature_meets_criteria", r"temperature meet acceptance"),
    ("trailer_temp_recorder_present", r"Trailer Temperature Recorder Present"),
    ("pallet_stack_1_ok", r"Stack 1 Acceptable"),
    ("pallet_stack_2_ok", r"Stack 2 Acceptable"),
    ("pallet_stack_3_ok", r"Stack 3 Acceptable"),
]
# how far after the anchor the Yes/No/N/A boxes may sit
TICK_WINDOW = {
    "approved_vendor": 220, "manufacturer_lot_matches_packing_list": 90, "trailer_temp_recorder_present": 70,
}


def _tick(page: Page, ctx: Ctx, field: str, anchor: str) -> str:
    m = page.find(anchor)
    if not m:
        ctx.flag(f"inspection_report.{field}", "label not found")
        return "blank"
    window = page.text[m.end(): m.end() + TICK_WINDOW.get(field, 120)]
    states = read_ticks(window)
    value, flag = tick_value(states)
    ctx.src(f"inspection_report.{field}", page, None, m.start())
    if flag:
        ctx.flag(f"inspection_report.{field}", flag)
    return value


def _lots(page: Page, ctx: Ctx) -> list[dict]:
    lots: list[dict] = []
    for t in page.tables:
        if not re.search(r"Manufacturing Lot", t.header_text, re.I):
            continue
        for row in t.rows[1:]:
            cells = [clean(c) or "" for c in row] + [""] * (5 - len(row))
            if not any(cells):
                continue
            if re.match(r"total", cells[0] or cells[1], re.I) or not any(cells[:3]):
                continue   # a 'Total' row, or a row that only carries the grand total
            cells = [re.sub(r"^(\d{2,3})\s?(165|1bs|lbs|1b5)$", r"\1", c, flags=re.I) for c in cells]   # '30165' = '30 lbs'
            # a row shifted left by one (weight landed in the boxes column) is detected by a unit in cell 3
            if re.search(r"\b(lb|lbs|ea)\b", cells[2], re.I) and not re.search(r"\b(lb|lbs|ea)\b", cells[3], re.I):
                cells = [cells[0], cells[1], "", cells[2], cells[3]]
                ctx.flag(f"inspection_report.lots[{len(lots)}]", "columns shifted in OCR table")
            boxes = num(cells[2])
            if not cells[3] and cells[4] and (num(cells[4]) or 0) <= 100:
                cells[3], cells[4] = cells[4], ""        # '1340 | | 30': the per-box weight landed in the total column
            lots.append({
                "manufacturing_lot": cells[0] or None,
                "vdf_lot_number": cells[1] or None,
                "boxes_received": int(boxes) if boxes is not None and boxes == int(boxes) else None,
                "weight_per_box_lbs": num(cells[3]),
                "total_weight_lbs": num(cells[4]),
            })
            ctx.src(f"inspection_report.lots[{len(lots) - 1}]", page, t.elem)
        break
    return lots


def _temperature(page: Page, ctx: Ctx) -> tuple[str, Optional[float]]:
    m = page.find(r"Ambient/dry product")
    if not m:
        ctx.flag("inspection_report.product_temperature_class", "temperature row not found")
        return "unknown", None
    row = page.text[max(0, m.start() - 12): m.start() + 160]
    row = strip_tags(row.replace("\n", " "))
    klass = "unknown"
    ticked: list[str] = []
    for name, pat in (("ambient", r"\(\s*([^()]*?)\s*\)\s*Ambient"), ("frozen", r"\(\s*([^()]*?)\s*\)\s*Frozen"),
                      ("refrigerated", r"\(\s*([^()]*?)\s*\)\s*Refrigerated")):
        mm = re.search(pat, row, re.I)
        if mm and (any(ch in "✓√☑☒✔xX" for ch in mm.group(1)) or re.fullmatch(r"[A-Za-z]", mm.group(1).strip())):
            ticked.append(name)
    if len(ticked) == 1:
        klass = ticked[0]
    elif len(ticked) > 1:
        # OCR ticked several boxes: the real one is the box that carries a handwritten reading
        with_reading = [n for n in ticked if n != "ambient" and re.search(n + r"\s*[−\-~]?\s*\d", row, re.I)]
        klass = with_reading[0] if len(with_reading) == 1 else "unknown"
        ctx.flag("inspection_report.product_temperature_class", f"several boxes ticked in OCR: {ticked}")
    temp: Optional[float] = None
    target = {"frozen": r"Frozen", "refrigerated": r"Refrigerated"}.get(klass)
    if target:
        mm = re.search(target + r"\s*([−\-~]?\s*\d+(?:\.\d+)?)", row)
        if mm:
            raw = mm.group(1).replace("−", "-").replace("~", "-").replace(" ", "")
            try:
                temp = float(raw)
            except ValueError:
                temp = None
    ctx.src("inspection_report.product_temperature_class", page, None, m.start())
    ctx.src("inspection_report.product_temperature_f", page, None, m.start())
    if klass == "frozen" and temp is not None and temp > 10:
        ctx.flag("inspection_report.product_temperature_f", "above frozen limit; handwriting may be misread")
    return klass, temp


def _disposition(page: Page, ctx: Ctx) -> tuple[str, Optional[str]]:
    opts = [("supervisor_qc_contacted", r"Supervisor and QC Contacted"),
            ("accepted_with_corrective_action", r"Accepted\s*[–\-]\s*With the following"),
            ("accepted", r"Accepted\s*[–\-]\s*All material")]
    chosen = "unknown"
    for name, pat in opts:   # priority order: supervisor > corrective > accepted
        m = re.search(r"\(\s*(?:<mark>)?\s*([^()]{0,8}?)\s*(?:</mark>)?\s*\)\s*" + pat, page.text, re.I)
        if m and any(ch in "✓√☑☒✔xX" for ch in strip_tags(m.group(1))):
            chosen = name
            ctx.src("inspection_report.disposition", page, None, m.start())
            break
    corrective = None
    m = page.find(r"\(describe\)\s*:?")
    if m:
        window = page.text[m.end(): m.end() + 300]
        window = re.split(r"Supervisor and QC Contacted|Inspector'?s Signature", window, flags=re.I)[0]
        window = re.sub(r"\(\s*(?:<mark>)?[^()]{0,6}(?:</mark>)?\s*\)", " ", window)   # drop empty / ticked boxes
        corrective = clean(window)
        if corrective and (len(corrective) < 4 or not re.search(r"[A-Za-z]{3}", corrective)):
            corrective = None
    if chosen == "accepted" and corrective:
        chosen = "accepted_with_corrective_action"
    return chosen, corrective


def _signed(page: Page, ctx: Ctx, label: str, path: str) -> Optional[bool]:
    m = page.find(label)
    if not m:
        return None
    el = page.elem_at(m.start())
    nxt = page.next_text_after(el, ("text", "figure")) if el else None
    if nxt is None:
        # value may sit on the same line after the label (table shape)
        value, _ = after_label(page, label, [r"Verification Signature", r"Date\s*:", r"4-FRC-COR"], 60, True)
        return bool(value)
    body = nxt.content or nxt.description
    ctx.src(path, page, nxt)
    return bool(clean(body))


def _dates(page: Page, ctx: Ctx) -> tuple[Optional[str], Optional[str]]:
    sig = page.find(r"Inspector'?s Signature")
    start = sig.start() if sig else 0
    found: list[tuple[str, int]] = []
    for m in re.finditer(r"Date\s*:?\s*([^\n|]{0,24})", page.text[start:], re.I):
        raw = m.group(1).strip()
        if not raw:
            # value on the next line
            tail = page.text[start + m.end(): start + m.end() + 30]
            raw = tail.strip().split("\n")[0]
        found.append((raw, start + m.start()))
        if len(found) == 2:
            break
    out: list[Optional[str]] = [None, None]
    for i, (raw, pos) in enumerate(found):
        iso, flag = to_iso(raw)
        path = ["inspection_report.inspection_date", "inspection_report.verification_date"][i]
        out[i] = iso
        ctx.src(path, page, None, pos + 6)
        if flag:
            ctx.flag(path, flag)
    return out[0], out[1]


def _item_code(raw: Optional[str]) -> Optional[str]:
    """'Co1547, AD' -> 'C01547.AD'; '[0]547.AB' -> '0547.AB' (left for the master-data repair in traceability)."""
    if not raw:
        return None
    s = re.sub(r"[^A-Za-z0-9.]", "", raw).upper()
    s = re.sub(r"^([A-Z])O(?=\d{4})", r"\g<1>0", s)
    m = re.fullmatch(r"([A-Z]?\d{5})\.?([A-Z]{2})", s)
    return f"{m.group(1)}.{m.group(2)}" if m else (s or None)


def extract(page: Page, ctx: Ctx) -> dict:
    P = "inspection_report."
    r: dict = {}
    foot = page.find(r"(4-FRC-COR-031[^\n]*)")
    r["form_id"] = clean(foot.group(1)) if foot else None
    if foot:
        ctx.src(P + "form_id", page, None, foot.start())

    def val(path: str, label: str, max_len: int = 120) -> Optional[str]:
        return value_after(page, label, LABELS, ctx, P + path, max_len)

    r["receiving_facility"] = val("receiving_facility", r"Receiving Facility\s*:?")
    r["product_description"] = val("product_description", r"Product Description\s*:?")
    item = val("item_number", r"Item\s*#\s*:?")
    r["item_number"] = _item_code(item)
    r["country_of_origin"] = val("country_of_origin", r"Country of Origin\s*:?")
    r["vendor"] = val("vendor", r"Vendor\s*:?")
    order = val("order_number", r"Order Number\s*:?")
    m = re.search(r"\d{4}-\d{2}-\d{4,5}", order or "")
    r["order_number"] = m.group() if m else order
    r["carrier_name"] = val("carrier_name", r"Carrier Name\s*:?")
    seal = val("trailer_seal_number", r"Trailer Seal Number\s*:?")
    m = re.search(r"[A-Z]?\d{4,}", seal or "")
    r["trailer_seal_number"] = m.group() if m else seal

    for field, anchor in TICKS[:11]:
        r[field] = _tick(page, ctx, field, anchor)

    m = page.find(r"Allergen\s*/\s*Sensitivity[^:]*:")
    allergens = None
    if m:
        window = page.text[m.end(): m.end() + 80]
        window = re.split(r"Organic|Kosher|Is there", window, flags=re.I)[0]
        allergens = clean(window)
        if allergens and re.fullmatch(r"n/?a|none|-+", allergens, re.I):
            allergens = None
    r["allergens"] = allergens
    r["lots"] = _lots(page, ctx)

    for field, anchor in TICKS[11:]:
        r[field] = _tick(page, ctx, field, anchor)

    m = page.find(r"Crushed Boxes\s*:?\s*([^\n|]{0,20}?)\s*(?:Was damage|$)")
    damaged = None
    if m:
        raw = clean(m.group(1)) or ""
        n = num(raw)
        damaged = int(n) if n is not None and not re.search(r"n/?a", raw, re.I) else None
    r["damaged_boxes_count"] = damaged
    r["product_temperature_class"], r["product_temperature_f"] = _temperature(page, ctx)
    r["disposition"], r["corrective_action"] = _disposition(page, ctx)
    r["inspector_signed"] = _signed(page, ctx, r"Inspector'?s Signature\s*:?", P + "inspector_signed")
    r["verifier_signed"] = _signed(page, ctx, r"Verification Signature\s*:?", P + "verifier_signed")
    r["inspection_date"], r["verification_date"] = _dates(page, ctx)

    # reorder to the schema order
    order_keys = ["form_id", "receiving_facility", "product_description", "item_number", "country_of_origin", "vendor",
                  "order_number", "carrier_name", "trailer_seal_number", "order_matches_bol", "returned_product",
                  "ltl_truck_padlocked", "seal_present_matches_bol", "allergens", "organic", "kosher",
                  "kosher_symbol_on_label", "packing_list_or_coa_present", "manufacturer_lot_matches_packing_list",
                  "approved_vendor", "all_product_accounted_for", "lots", "trailer_clean", "trailer_free_of_damage",
                  "trailer_free_of_odors", "trailer_free_of_pests", "trailer_free_of_chemicals",
                  "properly_stacked_no_damage", "damaged_boxes_count", "damage_noted_on_bol", "product_clearly_labeled",
                  "label_matches_po", "product_temperature_class", "product_temperature_f", "temperature_meets_criteria",
                  "trailer_temp_recorder_present", "pallet_stack_1_ok", "pallet_stack_2_ok", "pallet_stack_3_ok",
                  "disposition", "corrective_action", "inspector_signed", "inspection_date", "verifier_signed",
                  "verification_date"]
    return {k: r.get(k) for k in order_keys}
