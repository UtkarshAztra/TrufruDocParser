"""Databricks-only PDF parser (no LLM).

Uploads PDFs to a Unity Catalog Volume, runs ``ai_parse_document()`` on a SQL
warehouse and writes the layout-aware result (text, tables, figures, per page)
to local files. Nothing is sent to a language model.

    python -m pdf_parser "docs/official-docs/BOL IQF Strawberries"
    python -m pdf_parser some.pdf --images --csv
    python -m pdf_parser some.pdf --from-parse output/pdf_parser   # offline re-render
"""

from .config import Settings, get_client, load_env
from .databricks_io import ai_parse_documents, run_sql, safe_name, upload_pdfs
from .render import ParsedDocument, parsed_to_html, parsed_to_markdown, parsed_to_text

__all__ = [
    "Settings",
    "ParsedDocument",
    "ai_parse_documents",
    "get_client",
    "load_env",
    "parsed_to_html",
    "parsed_to_markdown",
    "parsed_to_text",
    "run_sql",
    "safe_name",
    "upload_pdfs",
]
