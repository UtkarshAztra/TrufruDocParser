"""Turn the raw ``ai_parse_document`` JSON into things people and other code can use.

Raw shape (version 2.0)::

    {"document": {"elements": [{"id", "type", "content", "description", "confidence",
                                "bbox": [{"coord": [x0, y0, x1, y1], "page_id": 0}]}, ...],
                  "pages": [{"id": 0, "image_uri": null}, ...]},
     "metadata": {"id", "version", "file_metadata"},
     "error_status": null}

Element types seen on the TruFru packets: text, section_header, title, table (content = HTML),
figure (content empty, description = caption of the image), signature, caption, footnote,
page_header, page_footer, page_number.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .tables import drop_empty_rows, html_table_to_rows, rows_to_markdown

PAGE_FURNITURE = {"page_header", "page_footer", "page_number"}


@dataclass
class Element:
    id: Optional[int]
    type: str
    page: int  # 0-based
    content: str
    description: str
    confidence: Optional[float]
    bbox: Optional[list[float]]

    @property
    def text(self) -> str:
        """What to show for this element: its content, else its description (figures, signatures)."""
        return (self.content or self.description or "").strip()

    @property
    def is_table(self) -> bool:
        return self.type == "table" and bool(self.content)

    def table_rows(self, keep_empty_rows: bool = False) -> list[list[str]]:
        rows = html_table_to_rows(self.content) if self.is_table else []
        return rows if keep_empty_rows else drop_empty_rows(rows)


@dataclass
class ParsedDocument:
    source_name: str
    elements: list[Element] = field(default_factory=list)
    page_count: int = 0
    version: Optional[str] = None
    error_status: Any = None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, parsed: dict, source_name: str = "") -> "ParsedDocument":
        doc = parsed.get("document") or {}
        els: list[Element] = []
        for el in doc.get("elements") or []:
            bbox = el.get("bbox") or []
            first = bbox[0] if bbox and isinstance(bbox[0], dict) else {}
            page = el.get("page_id", first.get("page_id", 0))
            els.append(
                Element(
                    id=el.get("id"),
                    type=el.get("type") or "text",
                    page=int(page or 0),
                    content=el.get("content") or "",
                    description=el.get("description") or "",
                    confidence=el.get("confidence"),
                    bbox=first.get("coord"),
                )
            )
        pages = doc.get("pages") or []
        page_count = max([len(pages)] + [e.page + 1 for e in els]) if (pages or els) else 0
        return cls(
            source_name=source_name,
            elements=els,
            page_count=page_count,
            version=(parsed.get("metadata") or {}).get("version"),
            error_status=parsed.get("error_status"),
            metadata=parsed.get("metadata") or {},
        )

    # ------------------------------------------------------------ accessors
    def pages(self) -> Iterator[tuple[int, list[Element]]]:
        """Yields (page_number_1_based, elements) in reading order, for every page that has content."""
        by_page: dict[int, list[Element]] = {}
        for e in self.elements:
            by_page.setdefault(e.page, []).append(e)
        for p in sorted(by_page):
            yield p + 1, by_page[p]

    def tables(self) -> list[dict]:
        """Every table as rows, with its page and position. Empty form rows are dropped."""
        out = []
        for i, e in enumerate(x for x in self.elements if x.is_table):
            out.append(
                {
                    "index": i,
                    "page": e.page + 1,
                    "element_id": e.id,
                    "confidence": e.confidence,
                    "bbox": e.bbox,
                    "rows": e.table_rows(),
                    "html": e.content,
                }
            )
        return out

    def low_confidence(self, threshold: float = 0.5) -> list[Element]:
        return [e for e in self.elements if e.confidence is not None and e.confidence < threshold and e.text]

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.elements:
            counts[e.type] = counts.get(e.type, 0) + 1
        return {
            "source": self.source_name,
            "pages": self.page_count,
            "elements": len(self.elements),
            "by_type": dict(sorted(counts.items())),
            "tables": sum(1 for e in self.elements if e.is_table),
            "low_confidence_elements": len(self.low_confidence()),
            "ai_parse_document_version": self.version,
            "error_status": self.error_status,
        }

    def to_json(self) -> dict:
        """Compact, flat form (one record per element) for downstream code."""
        return {
            "source": self.source_name,
            "version": self.version,
            "pages": self.page_count,
            "error_status": self.error_status,
            "elements": [
                {
                    "id": e.id,
                    "type": e.type,
                    "page": e.page + 1,
                    "confidence": e.confidence,
                    "bbox": e.bbox,
                    "text": e.text,
                    **({"rows": e.table_rows()} if e.is_table else {}),
                }
                for e in self.elements
            ],
        }


# ---------------------------------------------------------------- renderers
def parsed_to_text(parsed: dict | ParsedDocument, source_name: str = "") -> str:
    """Page-grouped ``[type] content`` lines; tables stay as the HTML the service returned.

    Same format the v1 experiment wrote to ``.parsed.md``, so the fuzzy matcher and numeric
    checks that consume that text keep working.
    """
    doc = parsed if isinstance(parsed, ParsedDocument) else ParsedDocument.from_raw(parsed, source_name)
    blocks = []
    for page, els in doc.pages():
        lines = [f"[{e.type}] {e.text}" for e in els if e.text]
        if lines:
            blocks.append(f"===== PAGE {page} =====\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def parsed_to_markdown(parsed: dict | ParsedDocument, source_name: str = "", furniture: bool = False) -> str:
    """Readable Markdown: headings, paragraphs, Markdown tables, figure captions in italics."""
    doc = parsed if isinstance(parsed, ParsedDocument) else ParsedDocument.from_raw(parsed, source_name)
    out = [f"# {doc.source_name or 'Parsed document'}", ""]
    for page, els in doc.pages():
        out += [f"## Page {page}", ""]
        for e in els:
            if e.type in PAGE_FURNITURE and not furniture:
                continue
            if e.is_table:
                out += [rows_to_markdown(e.table_rows()), ""]
            elif not e.text:
                continue
            elif e.type == "title":
                out += [f"### {e.text}", ""]
            elif e.type == "section_header":
                out += [f"#### {e.text}", ""]
            elif e.type in ("figure", "signature"):
                out += [f"*[{e.type}] {e.text}*", ""]
            elif e.type in ("caption", "footnote"):
                out += [f"> {e.text}", ""]
            else:
                out += [e.text, ""]
    return "\n".join(out).rstrip() + "\n"


def parsed_to_html(parsed: dict | ParsedDocument, title: str = "") -> str:
    """Browser view: one section per page, tables rendered as returned, confidence shown per element."""
    doc = parsed if isinstance(parsed, ParsedDocument) else ParsedDocument.from_raw(parsed, title)
    title = title or doc.source_name
    sections = []
    for page, els in doc.pages():
        items = []
        for e in els:
            body = e.content if e.is_table else html.escape(e.content)
            if e.description:
                body += f'<div class="desc">{html.escape(e.description)}</div>'
            if not body.strip():
                continue
            conf = f'<span class="conf">{e.confidence:.2f}</span>' if e.confidence is not None else ""
            low = " low" if (e.confidence is not None and e.confidence < 0.5) else ""
            items.append(f'<div class="el {html.escape(e.type)}{low}"><span class="tag">{html.escape(e.type)}</span>{conf}{body}</div>')
        sections.append(f"<section><h2>Page {page}</h2>{''.join(items)}</section>")
    warn = f'<p class="warn">error_status: {html.escape(json.dumps(doc.error_status))}</p>' if doc.error_status else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
 body{{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1100px;color:#222;background:#fff}}
 section{{border-top:2px solid #444;margin-top:28px}}
 .el{{margin:8px 0;padding:6px 8px;border-left:3px solid #ccc;white-space:pre-wrap}}
 .el.table{{border-left-color:#2b7;white-space:normal}} .el.figure,.el.signature{{border-left-color:#c80}}
 .el.section_header,.el.title{{font-weight:600}} .el.low{{background:#fff4e5}}
 .el.page_header,.el.page_footer,.el.page_number{{color:#888;font-size:12px}}
 .tag{{display:inline-block;font:11px monospace;color:#666;margin-right:8px}}
 .conf{{display:inline-block;font:11px monospace;color:#999;margin-right:8px}}
 .desc{{font-style:italic;color:#666}} .warn{{color:#b00}}
 table{{border-collapse:collapse;margin-top:4px}} td,th{{border:1px solid #bbb;padding:3px 6px;vertical-align:top}}
</style></head><body><h1>{html.escape(title)}</h1>
<p>Raw <code>ai_parse_document</code> output (v{html.escape(str(doc.version or '?'))}), {doc.page_count} page(s),
{len(doc.elements)} element(s). Orange rows have confidence &lt; 0.50.</p>{warn}
{''.join(sections)}
</body></html>"""


# ------------------------------------------------------------------- writers
def doc_dir(out_dir: Path, stem: str, per_doc: bool = True) -> Path:
    """Where one document's files go: <out>/<stem>/ (default) or <out>/ (flat)."""
    return out_dir / stem if per_doc else out_dir


def find_cached_parse(folder: Path, stem: str) -> Optional[Path]:
    """Locate <stem>.ai_parse.json in a per-doc folder or flat layout."""
    for cand in (folder / stem / f"{stem}.ai_parse.json", folder / f"{stem}.ai_parse.json"):
        if cand.is_file():
            return cand
    return None


def write_outputs(
    parsed: dict,
    local: Path,
    out_dir: Path,
    stem: str,
    csv_tables: bool = False,
    volume_path: Optional[str] = None,
    json_only: bool = False,
    per_doc: bool = True,
) -> ParsedDocument:
    """Write every local artifact for one PDF and return the parsed document.

    Default layout is one folder per document (``per_doc=True``):

    <out>/<stem>/<stem>.ai_parse.json   raw service output (re-usable with --from-parse)
    <out>/<stem>/<stem>.elements.json   flat element list + table rows + summary
    <out>/<stem>/<stem>.parsed.md       [type] lines per page (tables as HTML) - matcher-compatible
    <out>/<stem>/<stem>.md              readable Markdown with Markdown tables
    <out>/<stem>/<stem>.parsed.html     browser view
    <out>/<stem>/tables/                table_p<page>_<n>.csv (with --csv)
    <out>/<stem>/images/                page_<n>.png (with --images, written by the CLI)

    With ``per_doc=False`` everything is written flat into <out>/ with the <stem>. prefix.
    """
    out_dir = doc_dir(out_dir, stem, per_doc)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = ParsedDocument.from_raw(parsed, local.name)
    (out_dir / f"{stem}.ai_parse.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
    record = doc.to_json() | {"summary": doc.summary(), "source_path": str(local), "volume_path": volume_path}
    (out_dir / f"{stem}.elements.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    if not json_only:
        (out_dir / f"{stem}.parsed.md").write_text(parsed_to_text(doc), encoding="utf-8")
        (out_dir / f"{stem}.md").write_text(parsed_to_markdown(doc), encoding="utf-8")
        (out_dir / f"{stem}.parsed.html").write_text(parsed_to_html(doc, local.name), encoding="utf-8")
    if csv_tables and not json_only:
        from .tables import rows_to_csv

        tdir = out_dir / ("tables" if per_doc else f"{stem}.tables")
        tdir.mkdir(exist_ok=True)
        per_page: dict[int, int] = {}
        for t in doc.tables():
            n = per_page[t["page"]] = per_page.get(t["page"], 0) + 1
            (tdir / f"table_p{t['page']}_{n}.csv").write_text(rows_to_csv(t["rows"]), encoding="utf-8")
    return doc
