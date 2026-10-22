"""Americold cold-store SHORT FORM BILL OF LADING -> packet.bill_of_lading (my_docs section 6).

One BOL is printed over two or three pages (page 1 with the product summary, page 2 with more lot rows and the
TOTALS line, and often a duplicate of page 2). All cold-store pages of a packet are parsed together: lot rows
are concatenated in page order, exact duplicate pages are skipped, and the summary / totals come from whichever
page carries them. Lot rows appear in four OCR shapes, so each row is read from its joined text.
"""

from __future__ import annotations

import re
from typing import Optional

from .bol_freezpak import empty_party, stamp
from .common import Ctx
from .elements import Page, Table, clean, strip_tags
from .normalize import num, numbers, to_iso

LOT_ROW = re.compile(r"Lots?\s*:", re.I)
MONEY = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})(?![\d.])")


def _block(page: Page, label: str, stop: str, max_len: int = 220) -> list[str]:
    m = page.find(label)
    if not m:
        return []
    window = page.text[m.end(): m.end() + max_len]
    window = re.split(stop, window)[0]
    return [clean(l) for l in window.split("\n") if clean(l)]


def _party_from_lines(lines: list[str]) -> dict:
    out = empty_party()
    if not lines:
        return out
    out["name"] = lines[0]
    addr = [l for l in lines[1:] if re.search(r"\d", l) or re.search(r",\s*[A-Z]{2}\b", l)]   # drops a contact person line
    out["address"] = " ".join(addr) or None
    return out


def _units_table(page: Page) -> Optional[Table]:
    """The lot table; its header row may sit a row or two down when the OCR glued an address line on top."""
    for t in page.tables:
        for i, row in enumerate(t.rows[:3]):
            head = [strip_tags(c).lower().replace("\n", " ") for c in row]
            if any("product code" in h for h in head) and any("net weight" in h or "units" in h for h in head):
                return t if i == 0 else Table(elem=t.elem, rows=t.rows[i:])
    return None


def _lot_row(cells: list[str]) -> Optional[dict]:
    """'60.00 | Lots: 77802 / PRD: | / 251222A | | | 1800.00 | 1920.00' in any of its shapes -> one lot line."""
    joined = " | ".join(strip_tags(c).replace("\n", " ") for c in cells)
    m = LOT_ROW.search(joined)
    if not m:
        return None
    before, after = joined[: m.start()], joined[m.end():]
    qty_m = MONEY.findall(before)
    qty = float(qty_m[-1].replace(",", "")) if qty_m else None
    if qty is None:
        lead = re.match(r"\s*\|?\s*(\d{1,4}\.\d{2})", after)
        qty = float(lead.group(1)) if lead else None
        if lead:
            after = after[lead.end():]
    lot_m = re.search(r"\b(\d{5,7})\b", after)
    lot = lot_m.group(1) if lot_m else None
    rest = after[lot_m.end():] if lot_m else after
    cust_m = re.search(r"/\s*\|?\s*([A-Z0-9]{5,9}[✓]?)", rest) or re.search(r"\b(2\d{5}[A-Z]\d?)\b", rest)
    cust = cust_m.group(1).strip("✓") if cust_m else None
    weights = [float(x.replace(",", "")) for x in MONEY.findall(rest)]
    weights = [w for w in weights if w >= 100]
    net = weights[0] if weights else None
    gross = weights[1] if len(weights) > 1 else None
    return {"actual_qty": qty, "item_code": None, "item_description": None, "lot": lot, "lot_description": None,
            "customer_lot": cust, "upc": None, "unit_weight_lbs": None, "best_before_date": None,
            "net_weight_lbs": net, "_gross": gross}


def _parse_table(t: Table) -> tuple[list[dict], list[dict], dict]:
    """(product summaries in order, lot rows tagged with the index of the summary printed above them, totals)."""
    head = [strip_tags(c).lower().replace("\n", " ") for c in t.header]
    summaries: list[dict] = []
    rows: list[dict] = []
    totals = {"total_qty": None, "total_net_weight_lbs": None, "total_gross_weight_lbs": None}
    for row in t.rows[1:]:
        cells = [strip_tags(c) for c in row]
        joined = " | ".join(c.replace("\n", " ") for c in cells)
        is_summary = re.search(r"\d+\s*/\s*\d+\s*LB", joined, re.I) and re.search(r"[A-Z]{3}", joined) and not re.search(r"TOTALS?\b", joined)
        if LOT_ROW.search(joined) and not is_summary:
            lr = _lot_row(cells)
            if lr and (lr["lot"] or lr["actual_qty"]):
                lr["_summary"] = len(summaries) - 1
                rows.append(lr)
            continue
        if re.search(r"TOTALS?\b", joined):
            nums = [float(x.replace(",", "")) for x in MONEY.findall(joined)]
            if nums:
                totals["total_qty"] = nums[0]
                big = [n for n in nums[1:] if n >= 1000 and n > nums[0]]   # '1320.00 | 1320.00 | TOTALS | 39600.00 | 42240.00 | 1405.53'
                totals["total_net_weight_lbs"] = big[0] if big else None
                totals["total_gross_weight_lbs"] = big[1] if len(big) > 1 else None
            continue
        if is_summary:
            idx = {k: next((i for i, h in enumerate(head) if k in h), None) for k in ("product code", "description", "pack size", "net weight", "gross weight")}
            pack = cells[idx["pack size"]] if idx["pack size"] is not None and idx["pack size"] < len(cells) else ""
            mm = re.search(r"/\s*([\d.]+)\s*LB", pack, re.I) or re.search(r"/\s*([\d.]+)\s*LB", joined, re.I)
            nums = [float(x.replace(",", "")) for x in MONEY.findall(re.sub(r"\d+\s*/\s*\d+\s*LB", " ", joined))]
            has_qty = bool(re.match(r"\s*[\d,]+\.\d{2}", cells[0])) if cells else False
            qty = nums[0] if has_qty and nums else None
            big = [n for n in (nums[1:] if has_qty else nums) if n >= 1000 and (qty is None or n > qty)]

            def cell(key: str) -> Optional[str]:
                i = idx[key]
                v = clean(cells[i].split("\n")[0]) if i is not None and i < len(cells) else None
                return re.sub(r"\s*/?\s*(GTIN|UCN)\s*:?.*$", "", v).strip() or None if v else None

            summaries.append({"actual_qty": qty, "item_code": cell("product code"), "item_description": cell("description"),
                              "unit_weight_lbs": float(mm.group(1)) if mm else None,
                              "net": big[0] if big else None, "gross": big[1] if len(big) > 1 else None})
    return summaries, rows, totals


def extract_pages(pages: list[Page], ctx: Ctx) -> dict:
    """All cold-store pages of one packet -> one bill_of_lading."""
    P = "bill_of_lading."
    page = pages[0]
    r: dict = {}
    r["title"] = "SHORT FORM BILL OF LADING"
    # 'From the Account of' is the supplier whose goods are stored; the shipper (3PL) is the cold store itself
    account = _party_from_lines(_block(page, r"From the Account of\s*:?", r"\[figure\]|Order N|Consigned|Customer PO"))
    r["customer"] = account
    warehouse = empty_party()
    if page.find(r"americold"):
        warehouse["name"] = "Americold"
        city = page.find(r"(?m)^([A-Z][A-Z ]+,\s*[A-Z]{2}\s*\d{2,5})\s*$")
        warehouse["address"] = clean(city.group(1)) if city else None
        ctx.src(P + "ship_from.name", page, None, page.find(r"americold").start())
    r["ship_from"] = warehouse if warehouse["name"] else account
    to_lines = _block(page, r"Consigned To\s*:?", r"Customer PO|Page:|B/L|\[figure\]")
    city = page.find(r"(?m)^(CROWN POINT,?\s+IN\s+46307)")
    if city and not any("46307" in l for l in to_lines):
        to_lines.append(city.group(1))
    r["ship_to"] = _party_from_lines(to_lines)
    r["principal_to"] = empty_party()

    def grab(pat: str, path: str) -> Optional[str]:
        for pg in pages:
            m = pg.find(pat)
            if m:
                ctx.src(P + path, pg, None, m.start())
                return clean(m.group(1))
        return None

    r["temperature_setpoint_f"] = None
    r["booking_number"] = None
    r["transaction_number"] = grab(r"Order N[^\n]*\n\s*(\d{4,})", "transaction_number")
    r["trailer_number"] = grab(r"Trailer\s*#\s*:?\s*([A-Z0-9\-]+)", "trailer_number")
    sd = grab(r"Ship Date\s*:?\s*([\d/\-.]+)", "ship_date")
    r["ship_date"], _ = to_iso(sd) if sd else (None, None)
    r["seal_number"] = grab(r"Seals?\s*:?\s*#?\s*([A-Z0-9]{4,})", "seal_number")
    r["po_sales_number"] = grab(r"Customer PO\s*#?\s*:?\s*([\d\-]{8,})", "po_sales_number")
    r["carrier"] = grab(r"Carrier\s*:\s*([^\n|]+)", "carrier")
    r["prepaid_or_collect"] = grab(r"Terms of Sales?\s*:?\s*([A-Z]+)", "prepaid_or_collect")
    r["container_number"] = None
    r["temp_recorder_number"] = None
    r["bol_number"] = grab(r"B/L\s*#\s*:?\s*(\d+)", "bol_number")
    r["barcode_labels"] = []
    r["comment"] = None

    summaries: list[dict] = []          # one per product on the BOL, in print order (a BOL can carry two products)
    lines: list[dict] = []
    totals = {"total_qty": None, "total_net_weight_lbs": None, "total_gross_weight_lbs": None}
    seen_pages: list[tuple] = []
    seen_labels: set = set()
    for pg in pages:
        t = _units_table(pg)
        if t is None:
            continue
        page_summaries, rows, tot = _parse_table(t)
        label = pg.find(r"Page\s*:?\s*(\d)\b")
        label_key = (label.group(1) if label else None, (pg.find(r"B/L\s*#\s*:?\s*(\d+)") or [None, None])[1])
        key = tuple((x["lot"], x["actual_qty"]) for x in rows)
        duplicate = (key in seen_pages and rows) or (label_key[0] is not None and label_key in seen_labels)
        if not duplicate:
            seen_pages.append(key)
            seen_labels.add(label_key)
            base = len(summaries)
            summaries += page_summaries
            for x in rows:
                x["_summary"] = base + x["_summary"] if x["_summary"] >= 0 else len(summaries) - len(page_summaries) - 1
            lines += rows
            if rows:
                ctx.src(P + "lines", pg, t.elem)
        for k, v in tot.items():
            totals[k] = totals[k] if totals[k] is not None else v
    for l in lines:
        s = summaries[l["_summary"]] if 0 <= l["_summary"] < len(summaries) else (summaries[0] if summaries else None)
        if s:
            l["item_code"], l["item_description"], l["unit_weight_lbs"] = s["item_code"], s["item_description"], s["unit_weight_lbs"]
        l.pop("_summary", None)
        l.pop("_gross", None)
    if summaries:
        def total(key: str) -> Optional[float]:
            vals = [s[key] for s in summaries if s[key] is not None]
            return round(sum(vals), 2) if vals else None
        totals["total_qty"] = totals["total_qty"] if totals["total_qty"] is not None else total("actual_qty")
        totals["total_net_weight_lbs"] = totals["total_net_weight_lbs"] if totals["total_net_weight_lbs"] is not None else total("net")
        totals["total_gross_weight_lbs"] = totals["total_gross_weight_lbs"] if totals["total_gross_weight_lbs"] is not None else total("gross")
        if not lines:
            for s in summaries:
                lines.append({"actual_qty": s["actual_qty"], "item_code": s["item_code"], "item_description": s["item_description"],
                              "lot": None, "lot_description": None, "customer_lot": None, "upc": None,
                              "unit_weight_lbs": s["unit_weight_lbs"], "best_before_date": None, "net_weight_lbs": s["net"]})
    r["lines"] = lines
    r.update(totals)
    # stamps: the first page that carries a date or time wins
    for key, label in (("truck_arrived", r"TRUCK ARRIVED"), ("truck_departed", r"TRUCK DEPARTED")):
        r[key] = stamp(page, ctx, label, P + key)
        if not (r[key].get("stamp_date") or r[key].get("stamp_time")):
            for pg in pages[1:]:
                other = stamp(pg, Ctx(), label, P + key)
                if other.get("stamp_date") or other.get("stamp_time"):
                    r[key] = other
                    break
    r["pallets_exchanged_in"] = None
    r["pallets_exchanged_out"] = None
    mm = page.find(r"Printed\s*:?\s*([\d/]+\s+[\d:]+)")
    r["printed_timestamp"] = mm.group(1) if mm else None
    return r


def extract(page: Page, ctx: Ctx) -> dict:
    return extract_pages([page], ctx)
