"""Command line: parse PDFs with Databricks ``ai_parse_document`` only. No LLM.

    python -m pdf_parser <pdf-or-folder> [--out DIR] [--recursive] [--csv] [--images]
                                         [--batch-size N] [--cleanup] [--profile P]
                                         [--volume /Volumes/...] [--warehouse ID] [--parse-version 2.0]
                                         [--from-parse DIR] [--print-sql]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import PROJECT_ROOT, Settings
from .databricks_io import DEFAULT_BATCH_SIZE, parse_sql, safe_name, upload_and_parse
from .render import doc_dir, find_cached_parse, write_outputs

DEFAULT_OUT = PROJECT_ROOT / "output" / "pdf_parser"


def collect_pdfs(target: Path, recursive: bool = False) -> list[Path]:
    if target.is_dir():
        it = target.rglob("*.pdf") if recursive else target.glob("*.pdf")
        return sorted(p for p in it if p.is_file())
    if target.is_file() and target.suffix.lower() == ".pdf":
        return [target]
    return []


def unique_stems(pdfs: list[Path]) -> dict[Path, str]:
    """Local output stem per PDF; duplicates after sanitising get _2, _3, ..."""
    stems: dict[Path, str] = {}
    taken: set[str] = set()
    for pdf in pdfs:
        base = Path(safe_name(pdf.name)).stem
        stem, n = base, 1
        while stem in taken:
            n += 1
            stem = f"{base}_{n}"
        taken.add(stem)
        stems[pdf] = stem
    return stems


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pdf_parser",
        description="Parse PDFs with Databricks ai_parse_document (layout-aware text + tables). No LLM is called.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", type=Path, help="a PDF file or a folder of PDFs")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    ap.add_argument("--recursive", action="store_true", help="also parse PDFs in sub-folders")
    ap.add_argument("--json-only", action="store_true",
                    help="write only <stem>.ai_parse.json and <stem>.elements.json (no .md/.html/.csv)")
    ap.add_argument("--flat", action="store_true",
                    help="write <stem>.* files directly into --out instead of one folder per document")
    ap.add_argument("--csv", action="store_true", help="also write every table as CSV under <doc>/tables/")
    ap.add_argument("--images", action="store_true", help="also render page PNGs locally with PyMuPDF under <doc>/images/")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="PDFs per upload folder / SQL statement")
    ap.add_argument("--cleanup", action="store_true", help="delete the uploaded PDFs from the Volume after parsing")
    ap.add_argument("--profile", default=None, help="Databricks CLI profile (instead of the .env PAT)")
    ap.add_argument("--volume", default=None, help="UC Volume root (default: TRUFRU_PARSER_VOLUME from .env)")
    ap.add_argument("--warehouse", default=None, help="SQL warehouse id (default: DATABRICKS_WAREHOUSE_ID from .env)")
    ap.add_argument("--parse-version", default=None, help="ai_parse_document version option (default: 2.0)")
    ap.add_argument("--image-output-path", default=None,
                    help="Volume folder for ai_parse_document's own page images (imageOutputPath option)")
    ap.add_argument("--from-parse", type=Path, default=None,
                    help="output folder of an earlier run (<stem>/<stem>.ai_parse.json or flat): re-render offline, no Databricks calls")
    ap.add_argument("--print-sql", action="store_true", help="print the ai_parse_document SQL for the volume folder and exit")
    ap.add_argument("--quiet", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    args = build_parser().parse_args(argv)
    say = (lambda *_: None) if args.quiet else print

    pdfs = collect_pdfs(args.input.resolve(), args.recursive)
    if not pdfs:
        sys.exit(f"No PDFs found at {args.input}")
    stems = unique_stems(pdfs)
    args.out.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- source of parses
    if args.from_parse:
        say(f"[offline] re-rendering {len(pdfs)} PDF(s) from {args.from_parse}")
        mapping, parsed_by_path = {}, {}
        for pdf in pdfs:
            cached = find_cached_parse(args.from_parse, stems[pdf])
            if cached is None:
                sys.exit(f"No cached parse for {pdf.name} in {args.from_parse} - run without --from-parse first")
            mapping[f"cached:{cached}"] = pdf
            parsed_by_path[f"cached:{cached}"] = json.loads(cached.read_text(encoding="utf-8"))
        run_dir = None
    else:
        settings = Settings.from_env(args.volume, args.warehouse, args.parse_version, args.profile)
        if args.print_sql:
            print(parse_sql(f"{settings.volume_dir}/<run_id>/batch_001", settings.parse_version, args.image_output_path).strip())
            return 0
        say(f"env: {settings.env_file} | volume: {settings.volume_dir} | warehouse: {settings.warehouse_id} "
            f"| ai_parse_document v{settings.parse_version} | {len(pdfs)} PDF(s)")
        from .config import get_client

        w = get_client(settings.profile)
        t0 = time.time()
        mapping, parsed_by_path, run_dir = upload_and_parse(
            w, pdfs, settings.volume_dir, settings.warehouse_id, settings.parse_version,
            batch_size=args.batch_size, image_output_path=args.image_output_path,
            cleanup=args.cleanup, quiet=args.quiet,
        )
        say(f"Databricks steps took {time.time() - t0:.0f}s")

    # ---------------------------------------------------------------- local outputs
    manifest = []
    failures = 0
    for vol_path, local in mapping.items():
        parsed = parsed_by_path.get(vol_path)
        stem = stems[local]
        if parsed is None:
            failures += 1
            say(f"  MISSING   {local.name}: no row returned by ai_parse_document")
            manifest.append({"file": str(local), "stem": stem, "status": "missing", "volume_path": vol_path})
            continue
        per_doc = not args.flat
        target = doc_dir(args.out, stem, per_doc)
        doc = write_outputs(parsed, local, args.out, stem, csv_tables=args.csv,
                            volume_path=None if vol_path.startswith("cached:") else vol_path,
                            json_only=args.json_only, per_doc=per_doc)
        if args.images:
            from .images import render_pages

            render_pages(local, target / "images" if per_doc else args.out / f"{stem}.images")
        s = doc.summary()
        status = "warning" if doc.error_status else "ok"
        if doc.error_status:
            say(f"  WARNING   {local.name}: error_status={doc.error_status}")
        say(f"  {status:8}  {local.name}: {s['pages']} page(s), {s['elements']} elements, {s['tables']} table(s), "
            f"{s['low_confidence_elements']} low-confidence -> {target if per_doc else str(args.out / stem) + '.*'}")
        manifest.append({"file": str(local), "stem": stem, "status": status, "output_dir": str(target),
                         "volume_path": vol_path, **s})

    (args.out / "manifest.json").write_text(
        json.dumps({"run_dir": run_dir, "out": str(args.out), "documents": manifest}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    say(f"manifest -> {args.out / 'manifest.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
