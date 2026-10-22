"""Van Drunen Farms Purchase Order ("Receive PO") -> packet.purchase_order (my_docs section 6).

The printed part of this form is regular; the handwriting lands wherever the OCR put it (empty table columns,
loose text elements, one long text line). Line items are therefore found by token shape anywhere on the page
(item code ... quantity + unit of measure), and everything that is left after removing the printed parts is
the handwriting.
"""

from __future__ import annotations

import re
from typing import Optional

from .common import Ctx, after_label
from .elements import Page, clean, strip_tags
from .normalize import numbers, to_iso

ITEM_CODE = re.compile(r"\b([A-Z]\s?[O0]\d{4}\s?\.\s?[A-Z]{2})\b")
QTY_UOM = re.compile(r"(?P<qty>\d{1,3}(?:,\d{3})*\.\d{2})\s*(?:\|\s*)?(?P<uom>LBS?|EA|CS|KG)\b", re.I)
FREIGHT_TERMS = re.compile(r"\b(Prepaid and Add|Delivered|3rd Party|Third Party|Prepaid|Collect)\b", re.I)
LOT_LABEL = re.compile(r"\bLot\s*(?:#|code or pallet|code|number|-)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9 ./]{3,30})", re.I)
LOT_SHAPES = [r"\bC\s?1\d{5}\b", r"\bLC\s?2\d\s?\d{3}\b", r"\b26\d{4}[A-Z]?\d?\b", r"\b261\d{6}\b", r"\b2\d{8}\b"]
PRINTED_NOISE = [
    r"Do Not Pull Samples", r"Freight-Inbound", r"Receive PO", r"^Next$", r"Page \d+ of \d+", r"Van D\s?r[a-z]*n[a-z]* Farms",
    r"Purchase Order Number", r"Facility\s*:", r"Vendor\s*:", r"Phone\s*:", r"Fax\s*:", r"Receiving", r"Instructions",
    r"Our Item\s*#", r"Legacy Item", r"Vendor Item", r"Item Description", r"Quantity", r"Unit [Oo]f Measure",
    r"Vendor/Custom Lot Number", r"^Number$", r"^Ordered$", r"Ship Via", r"^Freight$", r"^\[figure\]", r"^D$", r"^[A-Za-z]$",
    r"^\d{2}/\d{2}/\d{4}$", r"^TRU\s?FRU$", r"^Vendor$", r"^Phone$", r"^Fax$",
]
NOISE_RE = re.compile("|".join(PRINTED_NOISE), re.I)
PALLETS_RE = re.compile(r"(?:total\s+pallets?\s*[:\-]?\s*(\d{1,2})\b|(?<![\d\-/@.])(\d{1,2})\s*(?:total\s+)?(?:pallets?|pellets?|plts?)\b(?!\s*(?:@|per|/)))", re.I)


def select_page(pages: list[Page], po_hint: Optional[str]) -> Optional[Page]:
    """The PO page whose number matches the file name / IMIR, else the first one with a line table."""
    if not pages:
        return None
    if po_hint:
        for p in pages:
            m = re.search(r"Purchase Order Number\s*:?\s*(\d{4}-\d{2}-\d{5})", p.text, re.I)
            if m and re.sub(r"\D", "", m.group(1)) == po_hint and QTY_UOM.search(p.text):
                return p
    for p in pages:
        if QTY_UOM.search(p.text):
            return p
    return pages[0]


def _code(s: str) -> str:
    s = re.sub(r"\s", "", s).upper()
    return s[0] + "0" + s[2:] if s[1] == "O" else s


def _vendor(page: Page, ctx: Ctx) -> dict:
    out = {"name": None, "address": None, "phone": None, "fax": None, "code": None}
    m = page.find(r"Vendor\s*:")
    if m:
        window = page.text[m.end(): m.end() + 300]
        window = re.split(r"Phone\s*:|Receiving|Instructions|Ordered|Our Item", window, flags=re.I)[0]
        lines = [clean(l) for l in window.split("\n")]
        lines = [l for l in lines if l]
        if lines and re.match(r"^\d", lines[0]):
            # OCR put the vendor name before the 'Vendor:' label: take the last non-empty line above it
            above = [clean(l) for l in page.text[max(0, m.start() - 120): m.start()].split("\n")]
            above = [l for l in above if l and re.search(r"[A-Za-z]{3}", l) and not re.search(r"Purchase Order|Facility|Page \d|Farms", l)]
            if above:
                lines = [above[-1]] + lines
        if lines:
            out["name"] = lines[0]
            out["address"] = " ".join(lines[1:]) or None
            ctx.src("purchase_order.vendor.name", page, None, m.end() + 1)
    mp = page.find(r"Phone\s*:\s*([\d\-() ]{7,})")
    if mp:
        out["phone"] = clean(mp.group(1))
        ctx.src("purchase_order.vendor.phone", page, None, mp.start())
    mf = page.find(r"Fax\s*:\s*([\d\-() ]{7,})")
    if mf:
        out["fax"] = clean(mf.group(1))
    return out


def _header_table(page: Page, ctx: Ctx) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(order_date, ship_via, freight_terms) from the 'Ordered | Ship Via | Freight' grid, whatever shape it took."""
    order_date = ship_via = freight = None
    for t in page.tables:
        head = [strip_tags(c).replace("\n", " ").strip() for c in t.header]
        if not any(re.fullmatch(r"Ship Via", h, re.I) for h in head):
            continue
        body = t.rows[1] if len(t.rows) > 1 else []
        # date can be a header cell (next to Ordered) or a body cell under Ordered
        for i, h in enumerate(head):
            if re.fullmatch(r"Ordered", h, re.I):
                cand = (head[i + 1] if i + 1 < len(head) else "") + " " + (body[i] if i < len(body) else "")
                iso, _ = to_iso(cand)
                order_date = iso
            if re.fullmatch(r"Ship Via", h, re.I) and i < len(body):
                ship_via = clean(body[i])
            if re.fullmatch(r"Freight", h, re.I) and i < len(body):
                freight = clean(body[i])
        ctx.src("purchase_order.order_date", page, t.elem)
        break
    if order_date is None:
        m = page.find(r"Ordered[^\n]{0,80}?(\d{2}/\d{2}/\d{4})") or page.find(r"(\d{2}/\d{2}/\d{4})")
        if m:
            order_date, _ = to_iso(m.group(1))
            ctx.src("purchase_order.order_date", page, None, m.start(1))
    if freight is None:
        m = FREIGHT_TERMS.search(page.text)
        if m:
            freight = m.group(1)
    return order_date, ship_via, freight


def _lines(page: Page, ctx: Ctx) -> tuple[list[dict], list[tuple[int, int]]]:
    """Line items by shape; also returns the text spans of the printed tokens (codes, description, quantity)
    so that only those are removed before the handwriting is collected."""
    text = page.text
    lines: list[dict] = []
    spans: list[tuple[int, int]] = []
    seen: set = set()
    last_end = 0
    for m in QTY_UOM.finditer(text):
        base = max(last_end, m.start() - 220)
        back = text[base: m.start()]
        code_matches = list(ITEM_CODE.finditer(back))
        codes = [_code(c.group(1)) for c in code_matches]
        freight = re.search(r"Freight-Inbound", back, re.I)
        if freight and not codes:
            desc, our, vendor = "Freight-Inbound", None, None
            spans.append((base + freight.start(), base + freight.end()))
        elif codes:
            our, vendor = codes[0], (codes[1] if len(codes) > 1 else None)
            for c in code_matches:
                spans.append((base + c.start(), base + c.end()))
            desc_start = code_matches[-1].end()
            desc_raw = back[desc_start:]
            desc = None
            # the description is the last printed, alphabetic piece between the code and the quantity;
            # handwritten fragments (lot codes, pallet notes) in between are left for the notes
            pos = desc_start
            for piece in re.split(r"(\s\|\s|\n)", desc_raw):
                if piece and not re.fullmatch(r"\s\|\s|\n", piece):
                    p = strip_tags(re.sub(r"Do Not Pull Samples", " ", piece, flags=re.I)).strip()
                    if re.search(r"[A-Za-z]{3}", p) and not re.search(r"\d\s?[/@]|pallet|cases|\bLC\b|\bLot\b", p, re.I):
                        desc = clean(p)
                        spans.append((base + pos, base + pos + len(piece)))
                pos += len(piece)
            if desc and (len(desc) < 4 or not re.search(r"[A-Za-z]{3}", desc)):
                desc = None
        else:
            continue
        qty = float(m.group("qty").replace(",", ""))
        uom = m.group("uom").upper()
        spans.append((m.start(), m.end()))
        key = (our, desc, qty, uom)
        last_end = m.end()
        if key in seen:
            continue
        seen.add(key)
        lines.append({"our_item_number": our, "legacy_item_number": None, "vendor_item_number": vendor,
                      "item_description": desc, "quantity": qty, "unit_of_measure": uom, "vendor_lot_number": None})
        ctx.src(f"purchase_order.lines[{len(lines) - 1}]", page, None, m.start())
    return lines, spans


def _handwriting(page: Page, line_spans: list[tuple[int, int]]) -> list[str]:
    text = page.text
    chars = list(text)
    for s, e in line_spans:
        for i in range(s, e):
            chars[i] = " "
    # everything before the line-item header is printed (logo, PO number, vendor block)
    start = 0
    for pat in (r"Vendor/Custom Lot Number|Unit [Oo]f Measure|Our Item\s*#", r"Instructions\s*:?", r"Fax\s*:"):
        mm = re.search(pat, text)
        if mm:
            start = max(start, mm.end())
            break
    body = "".join(chars)[start:]
    notes: list[str] = []
    for piece in re.split(r"\n|\s\|\s", body):
        if piece.lstrip().startswith("[figure]"):
            continue
        v = clean(piece)
        if not v or NOISE_RE.search(v) and (len(v) < 32 or re.search(r"Purchase Order Number|Facility|Van D", v, re.I)):
            continue
        v = re.sub(r"Do Not Pull Samples", "", v, flags=re.I).strip(" :-")
        if not v or len(v) < 2 or v in notes or ITEM_CODE.fullmatch(v.replace(" ", "")):
            continue
        if re.fullmatch(r"(Delivered|Prepaid and Add|3rd Party|U|Prepaid|Collect)", v, re.I):
            continue
        notes.append(v)
    return notes


def extract(pages: list[Page], ctx: Ctx, po_hint: Optional[str]) -> tuple[Optional[dict], dict]:
    """(purchase_order dict or None, extras for the merge: {'pallets', 'lots', 'notes_text'})."""
    page = select_page(pages, po_hint)
    extras: dict = {"pallets": None, "lots": [], "notes_text": "", "page": page}
    if page is None:
        return None, extras
    r: dict = {}
    m = page.find(r"Purchase Order Number\s*:?\s*(\d{4}-\d{2}-\d{5})") or page.find(r"(\d{4}-\d{2}-\d{5})")
    r["po_number"] = m.group(1) if m else None
    if m:
        ctx.src("purchase_order.po_number", page, None, m.start(1))
    r["facility"] = None
    for pg in [page] + [p for p in pages if p is not page]:   # the label survives the OCR on some PO pages only
        fac, pos = after_label(pg, r"Facility\s*:", [r"Purchase Order Number", r"Vendor", r"Page \d"], 60, True)
        if fac and not re.search(r"\d{4}-\d{2}", fac):
            r["facility"] = fac
            ctx.src("purchase_order.facility", pg, None, pos)
            break
    r["order_date"], r["ship_via"], r["freight_terms"] = _header_table(page, ctx)
    r["vendor"] = _vendor(page, ctx)

    instr = None
    m = page.find(r"Receiving")
    if m:
        window = page.text[max(0, m.start() - 40): m.end() + 60]
        window = re.sub(r"Receiving|Instructions\s*:?|\[figure\][^\n]*", " ", window, flags=re.I)
        window = re.sub(r"Fax\s*:.*|Phone\s*:[^\n]*|Vendor\s*:.*", " ", window)
        cand = [clean(x) for x in re.split(r"\n|\s\|\s", window)]
        cand = [c for c in cand if c and len(c) <= 30 and not re.search(r"\d{3}", c) and not re.search(r"Ordered|Ship Via|Freight|Page", c, re.I)]
        instr = cand[0] if cand else None
        if instr:
            ctx.src("purchase_order.receiving_instructions", page, None, m.start())
    r["receiving_instructions"] = instr

    r["lines"], spans = _lines(page, ctx)
    notes = _handwriting(page, spans)
    notes_text = "\n".join(notes)
    lot = None
    mm = LOT_LABEL.search(notes_text)
    if mm:
        lot = re.split(r"\s{2,}|\s/\s|\n", mm.group(1).strip())[0].strip(" ./")
    if not lot:
        for shape in LOT_SHAPES:
            mm = re.search(shape, notes_text)
            if mm:
                lot = mm.group().strip()
                break
    r["handwritten_lot"] = lot or None
    hd = None
    mm = re.search(r"(?:Production Date|Post date|Date)\s*[-:]?\s*([^\n]{0,20})", notes_text, re.I)
    if mm:
        hd, flag = to_iso(mm.group(1))
        if flag:
            ctx.flag("purchase_order.handwritten_date", flag)
    if hd is None:
        for line in notes:
            iso, flag = to_iso(line)
            if iso and not re.search(r"\d{2}/\d{2}/\d{4}", line):   # printed dates are mm/dd/yyyy
                hd = iso
                if flag:
                    ctx.flag("purchase_order.handwritten_date", flag)
                break
    r["handwritten_date"] = hd
    r["handwritten_notes"] = notes
    if notes:
        ctx.src("purchase_order.handwritten_notes", page, None, None)
        ctx.flag("purchase_order.handwritten_notes", "handwriting")
    if lot:
        ctx.flag("purchase_order.handwritten_lot", "handwriting")

    joined_digits = re.sub(r"\b(\d)\s(\d)(?=\s*(?:pallets?|pellets?))", r"\1\2", notes_text)   # '2 2 pellets' -> '22'
    pm = PALLETS_RE.search(joined_digits)
    extras["pallets"] = int(pm.group(1) or pm.group(2)) if pm else None
    found: list[str] = []
    for shape in LOT_SHAPES:
        found += [x.strip() for x in re.findall(shape, notes_text)]
    extras["lots"] = list(dict.fromkeys(found))
    extras["notes_text"] = notes_text
    return r, extras
