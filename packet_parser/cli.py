"""Command line: ``<stem>.ai_parse.json`` -> ``<stem>.final.json``. No Databricks calls, no LLM.

    python -m packet_parser docs/my_docs/data            # every *.ai_parse.json under the folder
    python -m packet_parser docs/my_docs/data/many/IQF_STRAWBERRY_PO_20260089932
    python -m packet_parser some/<stem>.ai_parse.json --out output/final
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .parse import FINAL_SUFFIX, find_parses, stem_of, summary_row, write_final


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m packet_parser",
        description="Rule-based extraction of TruFru receiving packets from ai_parse_document JSON (no LLM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", type=Path, help="a <stem>.ai_parse.json file, a document folder, or a run folder")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"write <stem>{FINAL_SUFFIX} files here (default: next to each .ai_parse.json)")
    ap.add_argument("--doc", action="append", help="only packets whose stem is in this list (repeatable)")
    ap.add_argument("--summary", type=Path, default=None,
                    help="where to write summary.json (default: <out>/summary.json, or <input>/summary.json for a folder)")
    ap.add_argument("--quiet", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    args = build_parser().parse_args(argv)
    say = (lambda *_: None) if args.quiet else print

    parses = find_parses(args.input)
    if args.doc:
        wanted = set(args.doc)
        parses = [p for p in parses if stem_of(p) in wanted]
    if not parses:
        sys.exit(f"No *.ai_parse.json found at {args.input}")

    rows = []
    failures = 0
    for p in parses:
        stem = stem_of(p)
        try:
            target, result = write_final(p, args.out)
            row = summary_row(result) | {"output": str(target)}
            kinds = " ".join(k[:4] for k in row["page_kinds"] or [])
            note = ""
            if row["fallback_reason"]:
                note += f"  FALLBACK: {row['fallback_reason']}"
            if row["schema_error"]:
                note += f"  SCHEMA: {row['schema_error'][:60]}"
            if row["checks_failed"]:
                note += f"  FAILED CHECKS: {', '.join(row['checks_failed'])}"
            say(f"  ok    {stem:<48} {kinds}{note}")
        except Exception:  # noqa: BLE001 - one bad packet must not stop the batch
            failures += 1
            row = {"doc": stem, "error": traceback.format_exc()}
            say(f"  FAIL  {stem}\n{row['error']}")
        rows.append(row)

    summary_path = args.summary
    if summary_path is None:
        base = args.out if args.out else (args.input if args.input.is_dir() else args.input.parent)
        summary_path = base / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "input": str(args.input), "docs": rows}
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    n_fb = sum(1 for r in rows if r.get("fallback_reason"))
    say(f"\n{len(rows)} packet(s), {failures} error(s), {n_fb} with fallback -> summary {summary_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
