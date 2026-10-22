"""packet.traceability: one row per receipt, merged from the three documents with a fixed priority
(printed sources before handwritten ones; see my_docs section 6, traceability table)."""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from .common import Ctx

COUNTRIES = {"peru": "Peru", "chile": "Chile", "usa": "USA", "us": "USA", "united states": "USA", "mexico": "Mexico",
             "canada": "Canada", "china": "China", "egypt": "Egypt", "morocco": "Morocco", "poland": "Poland",
             "spain": "Spain", "turkey": "Turkey", "argentina": "Argentina", "serbia": "Serbia", "greece": "Greece"}
NON_PRODUCT = re.compile(r"tracker|recorder|sensitech|\btemp\b|-temp\b", re.I)
FACILITY_CODES = {"cp": "Crown Point", "co": "Crown Point", "c": "Crown Point"}


def _country(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = re.sub(r"[^a-z ]", "", value.lower()).strip()
    if key in COUNTRIES:
        return COUNTRIES[key]
    for k, v in COUNTRIES.items():
        if len(k) > 3 and re.search(r"\b" + k + r"\b", key):
            return v
    return None


def _product_lines(bol: Optional[dict]) -> list[dict]:
    if not bol:
        return []
    return [l for l in bol.get("lines", []) if not NON_PRODUCT.search(" ".join(str(l.get(k) or "") for k in ("item_code", "item_description", "lot_description")))
            and (l.get("unit_weight_lbs") is None or l["unit_weight_lbs"] >= 1)]


def _mode(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return Counter(vals).most_common(1)[0][0] if vals else None


def traceability(imir: Optional[dict], po: Optional[dict], bol: Optional[dict], extras: dict, match: Optional[dict],
                 ctx: Ctx) -> dict:
    T = "traceability."
    t: dict = {}
    po_line = next((l for l in (po or {}).get("lines", []) if (l.get("unit_of_measure") or "").upper() in ("LB", "LBS")), None)
    product = _product_lines(bol)

    def pick(path: str, *cands):
        for value, note in cands:
            if value not in (None, "", []):
                t[path] = value
                if note:
                    ctx.flag(T + path, note)
                return
        t[path] = None

    pick("po_number", ((po or {}).get("po_number"), None), ((imir or {}).get("order_number"), "from IMIR"),
         (((bol or {}).get("po_sales_number") or "") if len(re.sub(r"\D", "", (bol or {}).get("po_sales_number") or "")) >= 11 else None, "from BOL"))
    item_imir = (imir or {}).get("item_number")
    repaired = _repair_item(item_imir, match)
    pick("item_number", ((po_line or {}).get("our_item_number"), None), (repaired, "IMIR item repaired via master data" if repaired != item_imir else "from IMIR"))
    pick("product_description", ((po_line or {}).get("item_description"), None), ((imir or {}).get("product_description"), "from IMIR"))
    pick("supplier_name", (((po or {}).get("vendor") or {}).get("name"), None), ((imir or {}).get("vendor"), "from IMIR"))

    origin = _country((imir or {}).get("country_of_origin"))
    note = None
    if not origin:
        origin = _country(((po_line or {}).get("item_description") or "").split(" ")[-1])
        note = "IMIR value unreadable; from PO description"
    if not origin:
        for l in product:
            mm = re.search(r"/\s*([A-Z]{3,})\s*$", l.get("lot_description") or "")
            if mm and _country(mm.group(1)):
                origin, note = _country(mm.group(1)), "IMIR value unreadable; from BOL lot description"
                break
    if not origin and match:
        origins = [_country(o) for o in (match.get("observed") or {}).get("origins", [])]
        origins = [o for o in origins if o]
        if len(origins) == 1:
            origin, note = origins[0], "IMIR value unreadable; from master-data matcher"
    if not origin:
        origin, note = (imir or {}).get("country_of_origin"), "raw IMIR value, not a known country"
    pick("country_of_origin", (origin, note))

    imir_lots = [l for l in (imir or {}).get("lots", []) if l.get("manufacturing_lot")]
    mfg = list(dict.fromkeys(l["manufacturing_lot"] for l in imir_lots))
    bol_customer = list(dict.fromkeys(l["customer_lot"] for l in product if l.get("customer_lot")))
    bol_lots = list(dict.fromkeys(l["lot"] for l in product if l.get("lot")))
    coldstore = bool(bol and bol.get("title") == "SHORT FORM BILL OF LADING")
    pick("supplier_lot", (" / ".join(mfg) or None, None), (" / ".join(bol_customer) or None, "from BOL customer lots"),
         (" / ".join(bol_lots) if coldstore else None, "from cold-store BOL lots"),
         (" / ".join(extras.get("lots") or []) or None, "from PO handwriting"))
    vdf = list(dict.fromkeys(l["vdf_lot_number"] for l in (imir or {}).get("lots", []) if l.get("vdf_lot_number")))
    pick("vdf_lot_number", (" / ".join(vdf) or None, None), ((po or {}).get("handwritten_lot"), "from PO handwritten lot"))

    non_product = [l for l in (bol or {}).get("lines", []) if l not in product]
    qty_sum = sum(l["actual_qty"] for l in product if l.get("actual_qty")) if product else None
    qty_total = (bol or {}).get("total_qty")
    qty_bol = None
    if qty_total:
        qty_bol = qty_total - sum(l.get("actual_qty") or 0 for l in non_product)   # printed total minus the temp tracker line
        if qty_sum and abs(qty_sum - qty_bol) > 0.5:
            ctx.flag(T + "quantity_cases", f"BOL lines sum to {qty_sum:g}, printed total gives {qty_bol:g}; OCR may have dropped digits in a line")
    qty_imir = sum(l["boxes_received"] for l in imir_lots if l.get("boxes_received")) if imir_lots else None
    pick("quantity_cases", (qty_bol or None, None), (qty_sum or None, "sum of BOL lines (no printed total)"), (qty_imir or None, "from IMIR boxes"))
    pick("case_weight_lbs", (_mode([l.get("unit_weight_lbs") for l in product]), None),
         (_mode([l.get("weight_per_box_lbs") for l in imir_lots]), "from IMIR"),
         (_lbs_per_box(extras.get("notes_text", "")), "from PO handwriting"))
    net_sum = sum(l["net_weight_lbs"] for l in product if l.get("net_weight_lbs")) if any(l.get("net_weight_lbs") for l in product) else None
    net_total = (bol or {}).get("total_net_weight_lbs")
    net_bol = None
    if net_total:
        net_bol = round(net_total - sum(l.get("net_weight_lbs") or 0 for l in non_product), 2)
        if net_sum and abs(net_sum - net_bol) > 0.5:
            ctx.flag(T + "net_weight_lbs", f"BOL lines sum to {net_sum:g}, printed total gives {net_bol:g}")
    pick("net_weight_lbs", (net_bol, None), (net_sum, "sum of BOL lines (no printed total)"),
         (sum(l["total_weight_lbs"] for l in imir_lots if l.get("total_weight_lbs")) or None, "from IMIR"),
         ((po_line or {}).get("quantity"), "PO ordered quantity"))
    pick("gross_weight_lbs", ((bol or {}).get("total_gross_weight_lbs"), None))
    comment_pallets = re.search(r"(?<![\d\-/])(\d{1,2})\s*pallets?", (bol or {}).get("comment") or "", re.I)
    pick("pallets", (extras.get("pallets"), None), (int(comment_pallets.group(1)) if comment_pallets else None, "from BOL comment"),
         (len(product) if coldstore and len(product) > 1 and all(l.get("lot") for l in product) else None, "cold-store lot rows counted"))
    pick("ship_date", ((bol or {}).get("ship_date"), None))
    arrived = (bol or {}).get("truck_arrived") or {}
    departed = (bol or {}).get("truck_departed") or {}
    pick("arrival_date", (arrived.get("stamp_date"), None), ((imir or {}).get("inspection_date"), "no arrival stamp; IMIR date"))
    if t.get("arrival_date") and t.get("ship_date") and t["arrival_date"] < t["ship_date"]:
        ctx.flag(T + "arrival_date", f"before ship date {t['ship_date']}; stamp handwriting may be misread")
    pick("arrival_time", (arrived.get("stamp_time"), None))
    pick("departure_time", (departed.get("stamp_time"), None))
    bbds = sorted(l["best_before_date"] for l in product if l.get("best_before_date"))
    pick("best_before_date", (bbds[0] if bbds else None, "earliest of several" if len(set(bbds)) > 1 else None))
    pick("pack_or_production_date", ((po or {}).get("handwritten_date"), None))
    pick("shipped_from", (((bol or {}).get("ship_from") or {}).get("name"), None))
    ship_to = ((bol or {}).get("ship_to") or {}).get("name")
    fac = (imir or {}).get("receiving_facility")
    fac_full = FACILITY_CODES.get(re.sub(r"[^a-z]", "", (fac or "").lower()), fac)
    pick("received_at", (ship_to, None), (fac_full, "from IMIR facility"))
    pick("carrier", ((bol or {}).get("carrier"), None), ((imir or {}).get("carrier_name"), "from IMIR (handwritten)"))
    pick("seal_number", ((bol or {}).get("seal_number"), None), ((imir or {}).get("trailer_seal_number"), "from IMIR (handwritten)"))
    pick("product_temperature_f", ((imir or {}).get("product_temperature_f"), None))
    disp = (imir or {}).get("disposition")
    t["accepted"] = True if disp in ("accepted", "accepted_with_corrective_action") else (False if disp == "supervisor_qc_contacted" else None)
    return t


def _lbs_per_box(notes: str) -> Optional[float]:
    mm = re.search(r"(\d{2})\s*(?:lbs?|165|lb3)\s*(?:/|per|a)\s*(?:box|case)", notes, re.I)
    return float(mm.group(1)) if mm else None


def _repair_item(item: Optional[str], match: Optional[dict]) -> Optional[str]:
    if not item:
        return None
    compact = re.sub(r"[^A-Z0-9]", "", item.upper())
    compact = re.sub(r"^([A-Z])O", r"\g<1>0", compact)
    candidates = set()
    if match:
        for c in match.get("candidates") or []:
            if c.get("item_code"):
                candidates.add(c["item_code"])
        if match.get("item_code"):
            candidates.add(match["item_code"])
        for e in (match.get("evidence") or {}).get("codes_and_lots", []):
            candidates.update(e.get("item_codes") or [])
    for cand in sorted(candidates):
        cc = re.sub(r"[^A-Z0-9]", "", cand.upper())
        if cc == compact or cc.endswith(compact) or compact.endswith(cc[-7:]):
            return cand
    m = re.match(r"^([A-Z])?(\d{5})([A-Z]{2})$", compact)
    if m:
        return f"{m.group(1) or 'C'}{m.group(2)}.{m.group(3)}"
    return item
