"""Unknown BOL layouts (Arctic Cold, Lineage, legacy FreezPak short form): a key:value scraper that gets the
identifiers and the numeric columns, and marks the packet for review. LLM fallback candidates."""

from __future__ import annotations

import re

from .bol_freezpak import empty_party, stamp
from .common import Ctx
from .elements import Page, clean, strip_tags
from .normalize import num, numbers, to_iso


def extract(page: Page, ctx: Ctx) -> dict:
    P = "bill_of_lading."
    t = page.text
    r: dict = {}
    m = page.find(r"[^\n]*BILL OF LADING[^\n]*")
    r["title"] = clean(m.group()) if m else None
    r["ship_from"] = empty_party()
    r["customer"] = empty_party()
    r["ship_to"] = empty_party()
    r["principal_to"] = empty_party()
    for label, key in ((r"SHIP(?:PER| FROM)\s*:?", "ship_from"), (r"SHIP TO\s*:?", "ship_to")):
        mm = page.find(r"(?m)^(?:\[section_header\]\s*)?" + label + r"\s*\n?([^\n]+)\n([^\n]*)\n?([^\n]*)")
        if mm:
            r[key]["name"] = clean(mm.group(1))
            r[key]["address"] = clean(" ".join(g for g in mm.groups()[1:] if g)) or None

    def grab(pat: str, path: str):
        mm = page.find(pat)
        if mm:
            ctx.src(P + path, page, None, mm.start())
            return clean(mm.group(1))
        return None

    r["temperature_setpoint_f"] = None
    r["booking_number"] = None
    r["transaction_number"] = grab(r"(?:Trans|Order)\s*(?:#|No\.?|Number)\s*:?\s*([A-Z0-9\-]{4,})", "transaction_number")
    r["trailer_number"] = grab(r"Trailer\s*#?\s*:?\s*([A-Z0-9\-]{2,})", "trailer_number")
    sd = grab(r"(?:Ship|Date)\s*(?:Date)?\s*:?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})", "ship_date")
    r["ship_date"], _ = to_iso(sd) if sd else (None, None)
    r["seal_number"] = grab(r"Seals?\s*(?:#|No\.?)?\s*:?\s*([A-Z0-9]{4,})", "seal_number")
    r["po_sales_number"] = grab(r"(?:PO|Sales)\s*#?\s*:?\s*(\d{4}-\d{2}-\d{4,5})", "po_sales_number")
    r["carrier"] = grab(r"Carrier(?: Name)?\s*:\s*([^\n|]+)", "carrier")
    r["prepaid_or_collect"] = grab(r"(PREPAID|COLLECT)\b", "prepaid_or_collect")
    r["container_number"] = None
    r["temp_recorder_number"] = None
    r["bol_number"] = grab(r"(?:BILL OF LADING NO|B/L\s*#|Bill of Lading Number)\s*:?\s*([A-Z0-9]{4,})", "bol_number")
    r["barcode_labels"] = [e.content for e in page.elements if e.type == "caption" and re.fullmatch(r"[A-Z0-9]{4,12}", e.content)]
    r["comment"] = None
    lines = []
    totals = {"total_qty": None, "total_net_weight_lbs": None, "total_gross_weight_lbs": None}
    for tb in page.tables:
        head = [strip_tags(c).lower() for c in tb.header]
        qi = next((i for i, h in enumerate(head) if "qty shipped" in h or "qty" in h or "units" in h), None)
        wi = next((i for i, h in enumerate(head) if "net" in h or "weight" in h), None)
        gi = next((i for i, h in enumerate(head) if "gross" in h), None)
        li = next((i for i, h in enumerate(head) if "lot" in h), None)
        ci = next((i for i, h in enumerate(head) if "product code" in h or "item" in h), None)
        if qi is None or wi is None:
            continue
        for row in tb.rows[1:]:
            cells = [strip_tags(c) for c in row] + [""] * (len(head) - len(row))
            q = numbers(cells[qi])
            if not q:
                continue
            if re.search(r"total", " ".join(cells), re.I):
                totals["total_qty"] = q[0]
                totals["total_net_weight_lbs"] = num(cells[wi])
                totals["total_gross_weight_lbs"] = num(cells[gi]) if gi is not None else None
                continue
            lines.append({"actual_qty": q[0], "item_code": clean(cells[ci]) if ci is not None else None,
                          "item_description": None, "lot": clean(cells[li]) if li is not None else None,
                          "lot_description": None, "customer_lot": None, "upc": None, "unit_weight_lbs": None,
                          "best_before_date": None, "net_weight_lbs": num(cells[wi])})
        if lines:
            ctx.src(P + "lines", page, tb.elem)
            break
    r["lines"] = lines
    r.update(totals)
    r["truck_arrived"] = stamp(page, ctx, r"TRUCK ARRIVED", P + "truck_arrived")
    r["truck_departed"] = stamp(page, ctx, r"TRUCK DEPARTED", P + "truck_departed")
    r["pallets_exchanged_in"] = None
    r["pallets_exchanged_out"] = None
    mm = page.find(r"Printed\s*:?\s*([\d/]+\s+[\d:]+)")
    r["printed_timestamp"] = mm.group(1) if mm else None
    ctx.flag("bill_of_lading", "unknown BOL layout: generic scraper, needs review / LLM fallback")
    return r
