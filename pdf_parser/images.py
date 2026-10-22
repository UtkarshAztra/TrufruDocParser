"""Optional local page rendering with PyMuPDF (for eyeballing the parse next to the scan). No Databricks involved."""

from __future__ import annotations

from pathlib import Path

RENDER_DPI = 150


def render_pages(pdf: Path, folder: Path, dpi: int = RENDER_DPI) -> list[Path]:
    """Write page_<n>.png for every page of ``pdf`` into ``folder``; returns the paths."""
    import pymupdf  # imported lazily so the parser works without it

    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    with pymupdf.open(pdf) as doc:
        for n, page in enumerate(doc, 1):
            target = folder / f"page_{n}.png"
            page.get_pixmap(dpi=dpi).save(target)
            paths.append(target)
    return paths
