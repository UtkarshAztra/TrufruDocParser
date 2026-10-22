"""Load an `ai_parse_document` JSON (v2.0) into pages of elements with a flat, searchable text per page.

Every character of `Page.text` maps back to the element it came from (`Page.elem_at(pos)`), so a value found
by regex carries its element id and OCR confidence. Tables are kept twice: as rows of cells (`Page.tables`)
and flattened into the text as `cell | cell | ...` lines. `<s>...</s>` (struck-out) markers inside cells are
preserved because a crossed-out tick must not count as a tick.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class Element:
    id: int
    type: str
    content: str
    description: str
    page: int
    confidence: Optional[float]


@dataclass
class Table:
    elem: Element
    rows: list[list[str]]

    @property
    def header(self) -> list[str]:
        return self.rows[0] if self.rows else []

    @property
    def header_text(self) -> str:
        return " | ".join(c.replace("\n", " ") for c in self.header)


@dataclass
class Page:
    index: int
    elements: list[Element] = field(default_factory=list)
    text: str = ""
    spans: list[tuple[int, int, Element]] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)

    def elem_at(self, pos: int) -> Optional[Element]:
        for start, end, el in self.spans:
            if start <= pos < end:
                return el
        return None

    def find(self, pattern: str, flags: int = re.I) -> Optional[re.Match]:
        return re.search(pattern, self.text, flags)

    def finditer(self, pattern: str, flags: int = re.I) -> Iterator[re.Match]:
        return re.finditer(pattern, self.text, flags)

    def next_text_after(self, elem: Element, types: tuple[str, ...] = ("text",)) -> Optional[Element]:
        """The element that follows `elem` on this page, if it is of one of `types`."""
        ids = [e.id for e in self.elements]
        try:
            i = ids.index(elem.id)
        except ValueError:
            return None
        for e in self.elements[i + 1:]:
            if e.type in types:
                return e
            if e.type in ("section_header", "table", "title"):
                return None
        return None


@dataclass
class Doc:
    path: Path
    pages: list[Page]
    version: Optional[str]
    elements: dict[int, Element] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


# ------------------------------------------------------------------ HTML tables
class _TableParser(HTMLParser):
    """Rows of cells; colspan pads with empty cells; <br> -> newline; <s>..</s> kept as markers."""

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
        elif self._cell is not None:
            if tag == "br":
                self._cell.append("\n")
            elif tag == "s":
                self._cell.append("<s>")
            elif tag == "input" and "checked" in dict(attrs):
                self._cell.append("☑")

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
        elif tag == "s" and self._cell is not None:
            self._cell.append("</s>")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_tables(html: str) -> list[list[list[str]]]:
    p = _TableParser()
    p.feed(html)
    return p.tables


# ------------------------------------------------------------------ loading
def _render(el: Element, tables: list[list[list[str]]]) -> str:
    if el.type == "table":
        return "\n".join(" | ".join(c.replace("\n", " ") for c in row) for t in tables for row in t)
    if el.type == "figure":
        body = el.content or el.description or ""
        return f"[figure] {body}" if body else ""
    return el.content or el.description or ""


def load(path: Path) -> Doc:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    document = raw.get("document") or {}
    version = (raw.get("metadata") or {}).get("version")
    pages: dict[int, Page] = {}
    doc = Doc(path=Path(path), pages=[], version=version)
    for e in document.get("elements") or []:
        page_id = e.get("page_id")
        if page_id is None:
            page_id = (e.get("bbox") or [{}])[0].get("page_id", 0)
        el = Element(id=int(e.get("id", len(doc.elements))), type=e.get("type") or "text",
                     content=(e.get("content") or "").strip(), description=(e.get("description") or "").strip(),
                     page=int(page_id), confidence=e.get("confidence"))
        doc.elements[el.id] = el
        page = pages.setdefault(el.page, Page(index=el.page))
        page.elements.append(el)
        tables = parse_tables(el.content) if el.type == "table" else []
        for t in tables:
            page.tables.append(Table(elem=el, rows=t))
        rendered = _render(el, tables)
        if rendered.strip():
            start = len(page.text)
            page.text += rendered + "\n"
            page.spans.append((start, len(page.text), el))
    doc.pages = [pages[k] for k in sorted(pages)]
    return doc


def parsed_text(doc: Doc) -> str:
    """The document as ``pdf_parser`` writes ``<stem>.parsed.md``: ``===== PAGE n =====`` + one ``[type] content``
    line per element, tables kept as the HTML the service returned.

    Same bytes as ``pdf_parser.render.parsed_to_text`` on the same ``ai_parse.json``, so the fuzzy matcher and the
    printed-number reader see exactly the text the v1 experiments were tuned on.
    """
    blocks = []
    for page in doc.pages:
        lines = [f"[{e.type}] {e.content or e.description}" for e in page.elements if (e.content or e.description)]
        if lines:
            blocks.append(f"===== PAGE {page.index + 1} =====\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def clean(s: Optional[str]) -> Optional[str]:
    """Collapse whitespace, drop HTML tags and pipe separators; None if nothing is left."""
    if s is None:
        return None
    s = strip_tags(s).replace("|", " ")
    s = re.sub(r"\s+", " ", s).strip(" :-_")
    return s or None
