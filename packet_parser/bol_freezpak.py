"""FreezPak Straight Bill of Lading -> packet.bill_of_lading (my_docs section 6)."""

from __future__ import annotations

import re
from typing import Optional

from .common import Ctx
from .elements import Page, Table, clean, strip_tags
from .normalize import num, numbers, to_24h, to_iso

KV_KEYS = {
    "temperature": "temperature_setpoint_f", "booking number": "booking_number", "transaction number": "transaction_number",
    "trailer#": "trailer_number", "trailer #": "trailer_number", "ship date": "ship_date", "seal no.": "seal_number",
    "seal no": "seal_number", "po/sales #": "po_sales_number", "carrier": "carrier", "prepaid or collect": "prepaid_or_collect",
    "container #": "container_number", "temp recorder #": "temp_recorder_number", "bol #": "bol_number",
    "recv/ship dt": "ship_date",
}
PARTY_STOPS = r"Ship From|Customer|Ship To|Send Freight Bill|Principal To|Comment|TRUCK ARRIVED|TRUCK DEPARTED|THIS MEMORANDUM|Temperature\s*\||Actual Qty"


def empty_party() -> dict:
    return {"name": None, "address": None, "phone": None, "fax": None, "code": None}


def party(page: Page, ctx: Ctx, label: str, path: str) -> dict:
    out = empty_party()
    m = page.find(r"(?m)^" + label + r"\s*$") or page.find(label)
    if not m:
        return out
    window = page.text[m.end(): m.end() + 260]
    window = re.split(PARTY_STOPS + r"|\[figure\]|CHECK WITH|Pallets? Exchange", window)[0]
    lines = [clean(l) for l in window.split("\n")]
    lines = [l for l in lines if l]
    addr: list[str] = []
    for i, line in enumerate(lines):
        if i > 0 and not re.search(r"\d|,\s*[A-Z]{2}\b|\b[A-Z]{2}\s+\d{5}\b", line) and len(line) > 28:
            break   # free text after the block (handwritten remarks), not an address line
        if re.match(r"Phone", line, re.I):
            mm = re.search(r"Phone\s*:?\s*([\(\d][\d\-() ]{6,})", line)
            out["phone"] = clean(mm.group(1)) if mm else None
            continue
        if re.match(r"Fax", line, re.I):
            mm = re.search(r"Fax\s*:?\s*([\(\d][\d\-() ]{6,})", line)
            out["fax"] = clean(mm.group(1)) if mm else None
            continue
        code = re.search(r"\b([A-Z]{3}\d{3})\b|(web-?\s?\d{4,})", line)
        if code:
            out["code"] = (code.group(1) or code.group(2)).replace(" ", "")
            line = clean(line.replace(code.group(), "")) or ""
            if not line:
                continue
        if out["name"] is None:
            out["name"] = line
        else:
            addr.append(line)
    out["address"] = " ".join(addr) or None
    if out["name"]:
        ctx.src(path + ".name", page, None, m.end() + 1)
    return out


def kv_table(page: Page, ctx: Ctx) -> dict:
    out: dict = {}
    for t in page.tables:
        keys = [strip_tags(r[0]).strip().lower() for r in t.rows if r]
        if sum(1 for k in keys if k in KV_KEYS) < 4:
            continue
        for row in t.rows:
            if len(row) < 2:
                continue
            key = strip_tags(row[0]).strip().lower()
            field = KV_KEYS.get(key)
            if not field:
                continue
            raw = " ".join(strip_tags(c) for c in row[1:])          # 'key | : | value' or 'key | : value'
            raw = re.sub(r"^\s*:\s*", "", raw.strip()).strip()
            value = raw if field == "temperature_setpoint_f" else clean(raw)   # keep the sign on '-10'
            out[field] = value or None
            if value:
                ctx.src("bill_of_lading." + field, page, t.elem)
        break
    if not out:   # flat shape: 'Transaction Number: SE707967' lines
        for key, field in KV_KEYS.items():
            m = page.find(re.escape(key) + r"\s*:\s*([^\n|]{0,40})")
            if m and field not in out:
                out[field] = clean(m.group(1))
    return out


def _col_kinds(header: list[str]) -> list[Optional[str]]:
    kinds = []
    for h in header:
        h = strip_tags(h).lower().replace("\n", " ")
        if "qty" in h or h.strip() == "actual" or "units" in h:
            kinds.append("qty")
        elif "customer" in h:
            kinds.append("customer")
        elif "unit" in h:
            kinds.append("unit")
        elif "net" in h:
            kinds.append("net")
        elif "gross" in h:
            kinds.append("gross")
        elif "lot" in h:
            kinds.append("lot")
        elif "item" in h:
            kinds.append("item")
        else:
            kinds.append(None)
    return kinds


def lines_table(page: Page) -> Optional[Table]:
    for t in page.tables:
        kinds = _col_kinds(t.header)
        if "qty" in kinds and ("net" in kinds or "lot" in kinds) and "item" in kinds:
            return t
    return None


WAREHOUSE_LOT = re.compile(r"^[A-Z]\d{5,7}$")
CUSTOMER_LOT = re.compile(r"^(?:LC\s?\d{2}\s?\d{3}|C\s?1\d{5}|\d{6}[A-Z]?\d?|[A-Z]{1,3}\d{5,7})$")
ITEM_TOKEN = re.compile(r"^[A-Z0-9][A-Z0-9\-]{3,12}$")


def _empty_line() -> dict:
    return {"actual_qty": None, "item_code": None, "item_description": None, "lot": None, "lot_description": None,
            "customer_lot": None, "upc": None, "unit_weight_lbs": None, "best_before_date": None, "net_weight_lbs": None}


def _parts(cell: str) -> list[str]:
    out = []
    for p in strip_tags(cell).split("\n"):
        p = clean(p)
        if p:
            out.append(p)
    return out


def _split_item(parts: list[str]) -> tuple[Optional[str], Optional[str]]:
    """'FSTRA925US' / 'Halved Strawberries 30lb', or one line 'FSTR4925US Halved Strawberries 30lb'."""
    if not parts:
        return None, None
    first = parts[0]
    if " " in first and not ITEM_TOKEN.match(first):
        mm = re.match(r"^([A-Z0-9][A-Z0-9\-]{3,12})\s+(.+)$", first)
        if mm:
            return mm.group(1), clean(" ".join([mm.group(2)] + parts[1:]))
    return first, (clean(" ".join(parts[1:])) or None)


def _split_lot(parts: list[str]) -> tuple[Optional[str], Optional[str]]:
    """The lot is the line shaped like a warehouse lot (E046449), wherever the OCR put it in the cell."""
    if not parts:
        return None, None
    idx = next((i for i, p in enumerate(parts) if WAREHOUSE_LOT.match(p)), None)
    if idx is None:
        idx = next((i for i, p in enumerate(parts) if re.match(r"^[A-Z0-9]{5,9}$", p)), 0)
    lot = parts[idx]
    rest = [p for i, p in enumerate(parts) if i != idx]
    return lot, (clean(" ".join(rest)) or None)


def _line_by_kinds(cells: list[str], kinds: list[Optional[str]]) -> dict:
    line = _empty_line()
    for i, kind in enumerate(kinds):
        if kind is None or i >= len(cells):
            continue
        parts = _parts(cells[i])
        if kind == "qty":
            line["actual_qty"] = num(cells[i])
        elif kind == "item":
            line["item_code"], line["item_description"] = _split_item(parts)
        elif kind == "lot":
            if line["lot"] is None:
                line["lot"], line["lot_description"] = _split_lot(parts)
            else:   # separate 'Lot Description' column
                line["lot_description"] = clean(" ".join(parts)) or line["lot_description"]
        elif kind == "customer":
            line["customer_lot"] = parts[0] if parts else None
            line["upc"] = parts[1] if len(parts) > 1 else None
        elif kind == "unit":
            line["unit_weight_lbs"] = num(parts[0]) if parts and not re.search(r"\d/\d", parts[0]) else None
            line["best_before_date"], _ = to_iso(" ".join(parts))
        elif kind == "net":
            line["net_weight_lbs"] = num(cells[i])
    return line


def _line_by_shape(cells: list[str]) -> dict:
    """Used when a row has fewer cells than the header: assign cells by what they look like."""
    line = _empty_line()
    texts = [(i, _parts(c)) for i, c in enumerate(cells) if _parts(c)]
    nums = [(i, num(c)) for i, c in enumerate(cells) if re.fullmatch(r"\s*[\d,]+\.\d{2}\s*", strip_tags(c).split("\n")[0] or "")]
    if nums:
        line["actual_qty"] = nums[0][1]
        if len(nums) > 1:
            line["net_weight_lbs"] = nums[-1][1]
        if len(nums) > 2:
            line["unit_weight_lbs"] = nums[-2][1]
    used = {i for i, _ in nums}
    for i, parts in texts:
        if i in used:
            continue
        head = parts[0]
        if line["item_code"] is None and (ITEM_TOKEN.match(head) and not WAREHOUSE_LOT.match(head) or " " in head and re.match(r"^[A-Z0-9]{5,}\s", head)):
            line["item_code"], line["item_description"] = _split_item(parts)
        elif line["lot"] is None and any(WAREHOUSE_LOT.match(p) for p in parts):
            line["lot"], line["lot_description"] = _split_lot(parts)
        elif line["customer_lot"] is None and CUSTOMER_LOT.match(head.replace(" ", "")) and not WAREHOUSE_LOT.match(head):
            line["customer_lot"] = head
        elif re.search(r"\d{1,2}/\d{1,2}/\d{4}", " ".join(parts)):
            line["best_before_date"], _ = to_iso(" ".join(parts))
            if line["unit_weight_lbs"] is None and parts and not re.search(r"/", parts[0]):
                line["unit_weight_lbs"] = num(parts[0])
        elif line["lot_description"] is None and line["lot"] is not None:
            line["lot_description"] = clean(" ".join(parts))
        elif line["item_description"] is None and line["item_code"] is not None:
            line["item_description"] = clean(" ".join(parts))
    return line


def _merge_continuation(prev: dict, cells: list[str], kinds: list[Optional[str]]) -> None:
    """A row without a quantity continues the previous line (description / lot description / BBD lines)."""
    for i, kind in enumerate(kinds):
        if i >= len(cells):
            break
        parts = _parts(cells[i])
        if not parts:
            continue
        text = clean(" ".join(parts))
        if kind == "item":
            prev["item_description"] = clean(" ".join(filter(None, [prev["item_description"], text])))
        elif kind == "lot":
            if prev["lot"] is None and WAREHOUSE_LOT.match(parts[0]):
                prev["lot"] = parts[0]
                text = clean(" ".join(parts[1:])) or None
            if text:
                prev["lot_description"] = clean(" ".join(filter(None, [prev["lot_description"], text])))
        elif kind == "customer" and prev["customer_lot"] is None:
            prev["customer_lot"] = parts[0]
        elif kind == "unit":
            iso, _ = to_iso(text)
            if iso and prev["best_before_date"] is None:
                prev["best_before_date"] = iso
            elif prev["unit_weight_lbs"] is None and not iso:
                prev["unit_weight_lbs"] = num(text)
        elif kind == "net" and prev["net_weight_lbs"] is None:
            prev["net_weight_lbs"] = num(text)


def parse_lines(t: Table, ctx: Ctx) -> tuple[list[dict], dict]:
    kinds = _col_kinds(t.header)
    lines: list[dict] = []
    totals: dict = {"total_qty": None, "total_net_weight_lbs": None, "total_gross_weight_lbs": None}
    for row in t.rows[1:]:
        joined = " ".join(strip_tags(c) for c in row)
        if re.search(r"TOTAL|Net Weight\s*:|Gross Weight\s*:|<-{3,}", joined, re.I):
            _totals_row(row + [""] * (len(kinds) - len(row)), totals)
            continue
        qty_i = kinds.index("qty") if "qty" in kinds else 0
        has_qty = qty_i < len(row) and bool(re.search(r"\d", strip_tags(row[qty_i])))
        if not has_qty and lines and len(row) == len(kinds):
            _merge_continuation(lines[-1], row, kinds)
            continue
        line = _line_by_kinds(row + [""] * (len(kinds) - len(row)), kinds) if len(row) == len(kinds) else _line_by_shape(row)
        if line["actual_qty"] is None and line["item_code"] is None and line["lot"] is None:
            continue
        if line["actual_qty"] is None and lines:
            _merge_continuation(lines[-1], row, kinds[:len(row)])
            continue
        lines.append(line)
    if lines:
        ctx.src("bill_of_lading.lines", None, t.elem)
    return lines, totals


def _totals_row(cells: list[str], totals: dict) -> None:
    joined = " | ".join(strip_tags(c) for c in cells)
    first = numbers(strip_tags(cells[0]))
    if first and totals["total_qty"] is None and not re.search(r"Net|Gross", strip_tags(cells[0]), re.I):
        totals["total_qty"] = first[0]
    for label, key in (("Net Weight", "total_net_weight_lbs"), ("Gross Weight", "total_gross_weight_lbs")):
        for i, c in enumerate(cells):
            lines_c = strip_tags(c).split("\n")
            for li, lc in enumerate(lines_c):
                if re.search(label, lc, re.I):
                    # value: rest of the same cell line, else the matching line of the next cell
                    after = numbers(lc.split(":")[-1])
                    if not after and i + 1 < len(cells):
                        nxt = strip_tags(cells[i + 1]).split("\n")
                        after = numbers(nxt[li] if li < len(nxt) else " ".join(nxt))
                    if after and totals[key] is None:
                        totals[key] = after[0]


def stamp(page: Page, ctx: Ctx, label: str, path: str, extra_text: str = "") -> dict:
    """TRUCK ARRIVED / TRUCK DEPARTED stamp. Rows are read by their value shape (a date, a time, initials), because
    the printed row labels are often cropped by the OCR ('TE:' for DATE, 'E:' for TIME, 'VER INITIALS:')."""
    out = {"stamp_date": None, "stamp_time": None, "vdf_initialled": None, "driver_initialled": None, "note": None}
    text = page.text + "\n" + extra_text
    # the label occurrence that is actually followed by the stamp rows (a bare 'TRUCK ARRIVED TRUCK DEPARTED' line is not)
    m = None
    for cand in re.finditer(label, text):
        follow = text[cand.end(): cand.end() + 90]
        if re.search(r"(?:DA)?TE\s*:|TIME|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2}", follow):
            m = cand
            break
    if m is None:
        return out
    window = text[m.end(): m.end() + 420]
    stop = re.search(r"TRUCK DEPARTED|TRUCK ARRIVED|Principal To|CHECK WITH FORMAN|Pallets? Exchange", window[8:])
    if stop:
        window = window[: stop.start() + 8]
    rows: list[tuple[str, str]] = []
    for line in window.split("\n"):
        line = line.strip()
        if not line:
            continue
        bare = re.match(r"^\s*((?:VDF|DRIVER)?\s*INITIALS)\s+(.*)$", line, re.I)   # 'DRIVER INITIALS EA' (colon lost)
        if bare:
            rows.append((bare.group(1).upper(), bare.group(2)))
        elif ":" in line or "|" in line:
            key, _, value = line.partition(":") if ":" in line else line.partition("|")
            rows.append((key.strip().upper(), value))
        else:
            rows.append(("", line))
    date_done = time_done = False
    initials: list[tuple[str, str]] = []
    for key, value in rows:
        v = strip_tags(value).replace("|", " ").strip()
        if "INITIAL" in key or key in ("VDF", "DRIVER"):
            initials.append((key, v))
            continue
        if not date_done and (re.search(r"DATE|^(?:DA)?TE$", key) or not key) and to_iso(v)[0]:
            out["stamp_date"], flag = to_iso(v)
            date_done = True
            if flag:
                ctx.flag(path + ".stamp_date", flag)
            continue
        if not time_done and (re.search(r"TIME|^(?:TI)?M?E$", key) or not key) and TIME_TOKEN.search(v):
            out["stamp_time"], rest = to_24h(v)
            # initials written on the time line ('1:37pm SH') are never copied; they count as the VDF initials
            words = [w for w in (clean(rest) or "").split(" ") if w]
            initials_here = [w for w in words if re.fullmatch(r"[A-Z]{1,4}\.?", w)]
            if initials_here and len(initials_here) == len(words):
                rest = ""
                initials.append(("VDF INITIALS (on time line)", initials_here[0]))
            rest = clean(rest)
            out["note"] = rest if rest and len(rest) > 1 else None
            time_done = True
            continue
    for i, (key, v) in enumerate(initials):
        v = clean(re.sub(r"\bIN\b\s*:?|\bOUT\b\s*:?", " ", v))
        for tok in (v or "").split(" "):
            if re.fullmatch(r"[A-Za-z]{2,4}\.?", tok):
                ctx.redact.add(tok.rstrip("."))
        is_driver = "DRIVER" in key or ("VER" in key and "VDF" not in key)
        if is_driver:
            out["driver_initialled"] = bool(v)
        elif out["vdf_initialled"] is None or (v and not out["vdf_initialled"]):
            out["vdf_initialled"] = bool(v)
    if m.start() < len(page.text):
        ctx.src(path + ".stamp_date", page, None, m.start())
    ctx.flag(path, "handwritten stamp")
    return out


TIME_TOKEN = re.compile(r"(?<!\d)\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m?\b|(?<!\d)\d{1,2}:\d{2}(?!\d)", re.I)


def extract(page: Page, ctx: Ctx, next_page: Optional[Page] = None) -> dict:
    P = "bill_of_lading."
    r: dict = {}
    heads = [e.content for e in page.elements if e.type in ("page_header", "title") and re.search(r"LADING|STRAIGH", e.content)]
    title = clean(" ".join(heads)) if heads else None
    if title and re.search(r"OF LADING", title) and not re.search(r"BILL OF LADING", title):
        title = re.sub(r"STRAIGH\S*\s*", "", title)
        title = "STRAIGHT BILL " + title
    r["title"] = title
    r["ship_from"] = party(page, ctx, r"Ship From", P + "ship_from")
    if r["ship_from"]["name"] is None:
        r["ship_from"] = party(page, ctx, r"From", P + "ship_from")   # OCR dropped 'Ship'; party() tries '^From$' first
    r["customer"] = party(page, ctx, r"Customer(?! Lot)(?! PO)", P + "customer")
    r["ship_to"] = party(page, ctx, r"Ship To", P + "ship_to")
    r["principal_to"] = party(page, ctx, r"Principal To", P + "principal_to")
    if r["principal_to"]["name"] is None and next_page is not None:
        r["principal_to"] = party(next_page, ctx, r"Principal To", P + "principal_to")

    kv = kv_table(page, ctx)
    for f in ("temperature_setpoint_f", "booking_number", "transaction_number", "trailer_number", "ship_date", "seal_number",
              "po_sales_number", "carrier", "prepaid_or_collect", "container_number", "temp_recorder_number", "bol_number"):
        r[f] = kv.get(f)
    r["temperature_setpoint_f"] = num(kv.get("temperature_setpoint_f")) if kv.get("temperature_setpoint_f") else None
    if r["ship_date"]:
        r["ship_date"], _ = to_iso(r["ship_date"])
    r["barcode_labels"] = [e.content for e in page.elements if e.type == "caption" and re.fullmatch(r"[A-Z0-9]{4,12}", e.content)]
    m = page.find(r"(?m)^Comment\s*\n([^\n]+)")
    r["comment"] = clean(m.group(1)) if m else None
    if r["comment"]:
        ctx.src(P + "comment", page, None, m.start(1))
    if not r["temp_recorder_number"] and r["comment"]:
        mm = re.search(r"TEMP RECORDER\s*[=:#]?\s*([A-Z0-9]{5,})", r["comment"], re.I)
        r["temp_recorder_number"] = mm.group(1) if mm else None

    t = lines_table(page)
    lines, totals = (parse_lines(t, ctx) if t else ([], {"total_qty": None, "total_net_weight_lbs": None, "total_gross_weight_lbs": None}))
    # a second FreezPak page of the same BOL (same transaction number, or no header grid) continues the line table
    if next_page is not None and lines_table(next_page) is not None:
        same = (kv.get("transaction_number") and kv["transaction_number"] in next_page.text) or not kv_table(next_page, Ctx())
        if same:
            more, more_totals = parse_lines(lines_table(next_page), Ctx())
            seen = {(l["actual_qty"], l["lot"], l["net_weight_lbs"]) for l in lines}
            lines += [l for l in more if (l["actual_qty"], l["lot"], l["net_weight_lbs"]) not in seen]
            for k, v in more_totals.items():
                totals[k] = totals[k] if totals[k] is not None else v
    r["lines"] = lines
    search_text = page.text + ("\n" + next_page.text if next_page else "")
    for key, label in (("total_net_weight_lbs", "Net Weight"), ("total_gross_weight_lbs", "Gross Weight")):
        if totals[key] is None:
            mm = re.search(label + r"\s*:\s*\|?\s*([\d,]+\.\d+)", search_text)
            if mm:
                totals[key] = num(mm.group(1))
    if totals["total_qty"] is None:
        mm = re.search(r"([\d,]+\.\d{2})\s*\|\s*<-{3,}", search_text)
        if mm:
            totals["total_qty"] = num(mm.group(1))
    r.update(totals)
    extra = next_page.text if next_page else ""
    r["truck_arrived"] = stamp(page, ctx, r"TRUCK ARRIVED", P + "truck_arrived", extra)
    r["truck_departed"] = stamp(page, ctx, r"TRUCK DEPARTED", P + "truck_departed", extra)
    mm = re.search(r"Pallets?\s*Exchanges?[ \t]*\|?[ \t]*IN[ \t]*:?[ \t]*\|?[ \t]*([^\n|]*?)[ \t]*\|?[ \t]*OUT[ \t]*:?[ \t]*\|?[ \t]*([^\n|]*)", search_text, re.I)
    r["pallets_exchanged_in"] = clean(mm.group(1)) if mm else None
    r["pallets_exchanged_out"] = clean(mm.group(2)) if mm else None
    for k in ("pallets_exchanged_in", "pallets_exchanged_out"):
        if r[k] and (re.search(r"[A-Za-z]{4}", r[k]) or len(r[k]) > 8):
            r[k] = None   # captured a neighbouring label, not a value
    mm = re.search(r"CHECK WITH FORMAN BEFORE DEPARTING\s*(\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)", search_text, re.I)
    r["printed_timestamp"] = mm.group(1) if mm else None
    return r
