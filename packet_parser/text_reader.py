"""Read the printed numbers out of a document's parsed text (the ``.parsed.md`` format). No LLM involved.

Ported from ``TruFru-exp/experiments/v1/interdependent_numeric_data/text_reader.py``. This gives an
independent source to confirm the extracted numbers against, the same way the fuzzy matcher gives an
independent source for item codes and lots. Only printed values come through reliably (BOL tables, the PO
quantity line); handwriting on the IMIR is usually empty in the text.

Three BOL table layouts are handled by mapping columns from their header names:
  FreezPak  : Actual Qty | Item | Lot | Customer Lot | Unit Weight / BBD | Net Weight      (+ TOTALS row)
  Cold store: # of Units | Product Code | Description | Pack Size | Net Weight | Gross Weight | Cubic Ft
  Other     : QTY ORDERED | QTY SHIPPED | PRODUCT CODE | LOT NUMBER | ... | WEIGHT
Each cell keeps *all* numbers found in it as candidates, so a messy cell (e.g. '1740 58') can still
confirm a value without forcing a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


def numbers(text: Optional[str]) -> list[float]:
    return [float(m.group().replace(",", "")) for m in NUM_RE.finditer(text or "")]


def compact(text: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


# ------------------------------------------------------------------ HTML tables
class _TableParser(HTMLParser):
    """Collects every <table> as a list of rows; a colspan cell keeps its text in the first column only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._rows: Optional[list[list[str]]] = None
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None
        self._span = 1

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            try:
                self._span = max(1, int(dict(attrs).get("colspan") or 1))
            except ValueError:
                self._span = 1
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._row.extend([""] * (self._span - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._rows is not None:
            if self._row:
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _col_kind(header: str) -> Optional[str]:
    h = header.lower().replace("\n", " ")
    if "gross" in h:
        return "gross"
    if "net weight" in h:
        return "net"
    if "unit weight" in h:
        return "unit"
    if "actual qty" in h or "# of units" in h or "qty shipped" in h:
        return "qty"
    if h.strip().startswith("weight"):
        return "weight_any"  # a bare WEIGHT column could be net or gross
    return None


# ------------------------------------------------------------------ result types
@dataclass
class PrintedRow:
    qty: list[float]
    unit: list[float]
    net: list[float]
    gross: list[float]
    weight_any: list[float]
    text: str
    is_total: bool

    @property
    def compact(self) -> str:
        return compact(self.text)


@dataclass
class PrintedNumbers:
    source: str
    rows: list[PrintedRow] = field(default_factory=list)       # BOL data rows (not totals)
    totals: list[PrintedRow] = field(default_factory=list)     # BOL TOTALS rows
    po_lb: list[float] = field(default_factory=list)           # printed 'NN,NNN.NN LB' on the PO
    pallets: list[float] = field(default_factory=list)         # 'PALLETS OUT: 22', 'total pallets 22', ...
    cases_per_pallet: list[float] = field(default_factory=list)
    lbs_per_pallet: list[float] = field(default_factory=list)
    all_numbers: set[float] = field(default_factory=set)

    def seen(self, value: float) -> bool:
        return any(abs(value - n) <= 0.005 for n in self.all_numbers)

    def to_dict(self) -> dict:
        row = lambda r: {"text": r.text, "qty": r.qty, "unit": r.unit, "net": r.net, "gross": r.gross, "weight_any": r.weight_any}
        return {"source": self.source, "bol_rows": [row(r) for r in self.rows], "bol_totals": [row(r) for r in self.totals],
                "po_lb": self.po_lb, "pallets": self.pallets, "cases_per_pallet": self.cases_per_pallet,
                "lbs_per_pallet": self.lbs_per_pallet}


def _bol_rows(table: list[list[str]]) -> tuple[list[PrintedRow], list[PrintedRow]]:
    header_i = next((i for i, r in enumerate(table) if sum(1 for c in r if _col_kind(c)) >= 2), None)
    if header_i is None:
        return [], []
    kinds = [_col_kind(c) for c in table[header_i]]
    if "qty" not in kinds or not ({"net", "weight_any"} & set(kinds)):
        return [], []

    rows, totals = [], []
    for r in table[header_i + 1:]:
        cells = r + [""] * (len(kinds) - len(r))
        got: dict[str, list[float]] = {"qty": [], "unit": [], "net": [], "gross": [], "weight_any": []}
        for i, kind in enumerate(kinds):
            if not kind or i >= len(cells):
                continue
            cell = cells[i]
            if kind == "qty" and not cell.strip() and i + 1 < len(cells):
                # cold-store per-lot rows print the units one column to the right ('60.00 Lots:')
                lead = re.match(r"\s*(\d[\d,]*\.\d+|\d[\d,]*)(?=\s|$)", cells[i + 1] or "")
                cell = lead.group(1) if lead else ""
            text = cell.split("\n")[0] if kind == "unit" else cell  # 'Unit Weight<br>BBD': skip the date line
            got[kind] += numbers(text)
        text = " | ".join(c for c in cells if c)
        is_total = "TOTAL" in text.upper()
        if is_total and not got["gross"]:
            # FreezPak TOTALS rows label the numbers in the row itself: 'Net Weight:<br>Gross Weight:' or just 'Gross Weight:'
            upper = text.upper()
            if "GROSS WEIGHT" in upper and "NET WEIGHT" not in upper:
                got["net"], got["gross"] = [], got["net"]
            elif len(got["net"]) == 2:
                got["net"], got["gross"] = [got["net"][0]], [got["net"][1]]
        if not got["qty"] and not (is_total and (got["net"] or got["gross"])):
            continue  # data rows need a quantity; drops label rows such as 'GTIN: | UCN: | Sup.Code: 7387V'
        (totals if is_total else rows).append(PrintedRow(text=text, is_total=is_total, **got))
    return rows, totals


# ------------------------------------------------------------------ main entry
def read_printed_text(raw: str, source: str = "") -> PrintedNumbers:
    """Printed numbers in a document's parsed text (``[type] content`` lines, tables as HTML)."""
    out = PrintedNumbers(source=source)

    parser = _TableParser()
    parser.feed(raw)
    for table in parser.tables:
        rows, totals = _bol_rows(table)
        out.rows += rows
        out.totals += totals

    flat = re.sub(r"<br\s*/?>", " ", raw)
    flat = re.sub(r"<[^>]+>", " ", flat)
    flat = re.sub(r"[ \t]+", " ", flat)
    out.all_numbers = set(numbers(flat))

    out.po_lb = [float(x.replace(",", "")) for x in re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})\s*LB\b", flat)]

    pallet_patterns = [
        r"PALLETS\s*OUT\s*:?\s*(\d+)",
        r"PALLETS IN\s*/\s*PALLETS OUT\s*\d+\s*/\s*(\d+)",
        r"total\s+pallets\s*:?\s*(\d+)",
        r"(?<![@\d] )\b(\d{2})\s+pallets\b",
    ]
    for pat in pallet_patterns:
        out.pallets += [float(x) for x in re.findall(pat, flat, flags=re.I)]
    out.cases_per_pallet = [float(x) for x in re.findall(
        r"(\d+)\s*(?:boxes|cases|cs)\s*(?:/|per|@)\s*(?:full\s+)?pallet", flat, flags=re.I)]
    out.lbs_per_pallet = [float(x.replace(",", "")) for x in re.findall(
        r"(\d[\d,]*)\s*lbs?\s*(?:/|per|@)?\s*(?:full\s+)?pallet(?!-)", flat, flags=re.I)]
    out.lbs_per_pallet += [float(x.replace(",", "")) for x in re.findall(
        r"per\s+pallet\s*[-:]\s*(\d[\d,]*)", flat, flags=re.I) if float(x.replace(",", "")) >= 200]
    return out


def read_printed_numbers(path: Path) -> PrintedNumbers:
    """Same as :func:`read_printed_text`, reading a ``.parsed.md`` file written by ``pdf_parser``."""
    path = Path(path)
    return read_printed_text(path.read_text(encoding="utf-8", errors="replace"), source=str(path))
