"""HTML table (as returned by ``ai_parse_document``) -> rows, CSV, Markdown. Standard library only."""

from __future__ import annotations

import csv
import io
import re
from html.parser import HTMLParser


class _TableParser(HTMLParser):
    """Collects <tr>/<td|th> cells of the first table in a fragment; nested tables are flattened as text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.header_rows: set[int] = set()
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_is_header = False
        self._depth = 0  # nested table depth

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            return
        if self._depth > 1:  # inside a nested table: treat as text only
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._cell_is_header = tag == "th"
            if self._row is None:
                self._row = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth = max(0, self._depth - 1)
            return
        if self._depth > 1:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = re.sub(r"[ \t]+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            if self._cell_is_header:
                self.header_rows.add(len(self.rows))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_rows(fragment: str) -> list[list[str]]:
    """Returns a rectangular list of rows (short rows are right-padded with '')."""
    p = _TableParser()
    p.feed(fragment or "")
    p.close()
    if p._row:  # unterminated last row
        p.rows.append(p._row)
    width = max((len(r) for r in p.rows), default=0)
    return [r + [""] * (width - len(r)) for r in p.rows]


def drop_empty_rows(rows: list[list[str]]) -> list[list[str]]:
    """``ai_parse_document`` keeps blank form rows; drop rows where every cell is empty (keeps the header)."""
    return [r for i, r in enumerate(rows) if i == 0 or any(c.strip() for c in r)]


def rows_to_csv(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def rows_to_markdown(rows: list[list[str]]) -> str:
    """GitHub-flavoured Markdown table; the first row is used as the header."""
    if not rows:
        return ""

    def cell(v: str) -> str:
        return v.replace("|", "\\|").replace("\n", " ").strip()

    width = len(rows[0])
    head = rows[0] if any(c.strip() for c in rows[0]) else [f"col{i + 1}" for i in range(width)]
    lines = ["| " + " | ".join(cell(c) or " " for c in head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(cell(c) or " " for c in r) + " |" for r in rows[1:]]
    return "\n".join(lines)
