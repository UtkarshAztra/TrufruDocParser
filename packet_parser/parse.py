"""Rule-based packet extraction: ``<stem>.ai_parse.json`` (raw ``ai_parse_document`` output) -> final JSON.

No LLM is involved. Pages are classified by their printed titles, each document kind has its own
extractor, the three documents are merged into one traceability row, and the result is validated against
``schema.ReceivingPacket`` and enriched with three deterministic check layers:

  cross_checks     values that should agree across IMIR / PO / BOL (``checks.py``)
  numeric_confirm  every number against the printed text and the math rules R1-R15 (``numeric_confirm.py``)
  master_match     fuzzy-matcher verdict against the master CSVs + checks of the extraction (``master_check.py``)

Ported from ``TruFru-exp/experiments/v1/regex_parser/run.py``; the two optional steps come from the
``fuzzy_matcher`` and ``interdependent_numeric_data`` experiments. Both read the same ``.parsed.md``-style text
that ``elements.parsed_text`` renders from the loaded ``ai_parse.json``, so no extra input file is needed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import bol_coldstore, bol_freezpak, bol_generic, classify, imir, vdf_po
from .checks import cross_checks
from .common import Ctx
from .elements import Doc, load, parsed_text
from .merge import traceability
from .schema import ParsedPacket, ReceivingPacket

ENGINE = "regex_parser"
AI_PARSE_SUFFIX = ".ai_parse.json"
FINAL_SUFFIX = ".final.json"


# ------------------------------------------------------------------ one packet
def parse_doc(doc: Doc, po_hint: Optional[str] = None, match: Optional[dict] = None) -> tuple[dict, Ctx, dict]:
    """(packet dict, source/flag context, page-kind info) for one loaded ``ai_parse_document`` result."""
    ctx = Ctx()
    kinds = [classify.classify(p) for p in doc.pages]
    info = {"page_kinds": kinds, "templates_detected": sorted(set(kinds)), "fallback_reason": None}

    imir_pages = [p for p, k in zip(doc.pages, kinds) if k == classify.IMIR]
    po_pages = [p for p, k in zip(doc.pages, kinds) if k in (classify.VDF_PO, classify.VDF_PO_CONT)]
    bol_idx = [i for i, k in enumerate(kinds) if k in classify.BOL_KINDS]

    inspection = imir.extract(imir_pages[0], ctx) if imir_pages else None
    purchase, extras = vdf_po.extract(po_pages, ctx, po_hint)

    bill = None
    if bol_idx:
        i = bol_idx[0]
        page, kind = doc.pages[i], kinds[i]
        nxt = None
        if i + 1 < len(doc.pages) and kinds[i + 1] in (classify.STAMP_ONLY, classify.OTHER, classify.BOL_FREEZPAK):
            nxt = doc.pages[i + 1]
        if kind == classify.BOL_FREEZPAK:
            bill = bol_freezpak.extract(page, ctx, nxt)
        elif kind == classify.BOL_COLDSTORE:
            cold_pages = [doc.pages[j] for j in bol_idx if kinds[j] == classify.BOL_COLDSTORE]
            bill = bol_coldstore.extract_pages(cold_pages, ctx)
        else:
            bill = bol_generic.extract(page, ctx)
            info["fallback_reason"] = "unknown BOL layout"
    else:
        info["fallback_reason"] = "no BOL page classified" if purchase else "no PO page classified"
    if not imir_pages:
        ctx.flag("inspection_report", "no IMIR page in packet")
    if not purchase:
        ctx.flag("purchase_order", "no PO page in packet")

    _scrub(inspection, purchase, bill, ctx.redact)
    packet = {
        "documents_present": classify.documents_present(kinds),
        "inspection_report": inspection,
        "purchase_order": purchase,
        "bill_of_lading": bill,
        "traceability": traceability(inspection, purchase, bill, extras, match, ctx),
        "low_confidence_fields": [],
    }
    packet["low_confidence_fields"] = ctx.low_confidence_fields()
    return packet, ctx, info


def _scrub(inspection: Optional[dict], purchase: Optional[dict], bill: Optional[dict], redact: set) -> None:
    """Remove initials seen on the stamps from every free-text field (org rule: never output initials)."""
    if not redact:
        return
    pat = re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(s) for s in sorted(redact, key=len, reverse=True)) + r")\.?(?![A-Za-z])")

    def clean_text(s: Optional[str]) -> Optional[str]:
        if not s:
            return s
        out = re.sub(r"\s{2,}", " ", pat.sub(" ", s)).strip(" ,;-")
        return out or None

    if purchase:
        purchase["handwritten_notes"] = [n for n in (clean_text(n) for n in purchase.get("handwritten_notes") or []) if n]
    if inspection:
        inspection["corrective_action"] = clean_text(inspection.get("corrective_action"))
    if bill:
        for key in ("truck_arrived", "truck_departed"):
            if bill.get(key):
                bill[key]["note"] = clean_text(bill[key].get("note"))


def validate(packet: dict) -> tuple[dict, Optional[str]]:
    """Normalise through the pydantic schema (dates -> ISO strings, enums -> values). Keeps the raw dict on failure."""
    try:
        pk = ReceivingPacket.model_validate(packet)
        return pk.model_dump(mode="json"), None
    except Exception as exc:  # noqa: BLE001 - the error text goes into the output
        return packet, f"{type(exc).__name__}: {str(exc)[:400]}"


def _checks(packet: dict) -> list[dict]:
    try:
        return [c.model_dump() for c in cross_checks(ReceivingPacket.model_validate(packet))]
    except Exception as exc:  # noqa: BLE001
        return [{"name": "cross_checks", "status": "unknown", "detail": f"not computed: {type(exc).__name__}: {exc}"}]


# ------------------------------------------------------------------ optional check layers (flags only)
def _numeric_confirm(packet: dict, text: str) -> dict:
    """``numeric_confirm`` block: every number vs the printed text and the math rules. Never raises."""
    try:
        from .numeric_confirm import numeric_confirm
        from .text_reader import read_printed_text
        return numeric_confirm(packet, read_printed_text(text, source="ai_parse.json"))
    except Exception as exc:  # noqa: BLE001 - the error text goes into the output
        return {"verdict": f"not computed: {type(exc).__name__}: {exc}", "counts": {}, "rules": [], "numbers": [],
                "needs_review": True}


def _match_text(text: str, master_dir: Optional[Path]) -> tuple[Optional[dict], Optional[object], Optional[str]]:
    """(matcher verdict, matcher, error). The matcher is loaded once per process (``fuzzy_matcher.get_matcher``)."""
    try:
        from .fuzzy_matcher import get_matcher, match_text
        matcher = get_matcher(master_dir)
        return match_text(text, matcher), matcher, None
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"


def _master_match(packet: dict, match: Optional[dict], matcher: Optional[object], error: Optional[str]) -> dict:
    """``master_match`` block: matcher verdict + checks of the extraction against it. Never raises."""
    if error or match is None or matcher is None:
        return {"matcher_status": None, "matcher_item": None, "matcher_candidates": [], "matcher_flags": [],
                "checks": [], "flags": [f"not computed: {error or 'no matcher verdict'}"], "needs_review": True}
    try:
        from .master_check import master_match
        return master_match(packet, match, matcher.md)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return {"matcher_status": match.get("status"), "matcher_item": match.get("item_code"),
                "matcher_candidates": [c.get("item_code") for c in match.get("candidates") or []],
                "matcher_flags": match.get("flags") or [], "checks": [],
                "flags": [f"not computed: {type(exc).__name__}: {exc}"], "needs_review": True}


def po_hint_from_name(name: str) -> Optional[str]:
    m = re.search(r"(\d{11})", name)
    return m.group(1) if m else None


def stem_of(ai_parse_path: Path) -> str:
    return Path(ai_parse_path).name.removesuffix(AI_PARSE_SUFFIX)


def build_final(ai_parse_path: Path, stem: Optional[str] = None, match: Optional[dict] = None, *,
                master: bool = True, numeric: bool = True, master_dir: Optional[Path] = None) -> dict:
    """Final JSON (as a dict) for one ``<stem>.ai_parse.json``.

    ``master``: run the fuzzy matcher on the document text and add the ``master_match`` block (the verdict also
    feeds the traceability merge: item-number repair and origin fallback). Pass a precomputed ``match`` to skip the
    matcher. ``numeric``: add the ``numeric_confirm`` block. ``master_dir`` overrides the master CSV folder.
    A skipped step is written as ``None``; a step that fails is written with a ``not computed`` flag.
    """
    ai_parse_path = Path(ai_parse_path)
    stem = stem or stem_of(ai_parse_path)
    po_hint = po_hint_from_name(stem)
    doc = load(ai_parse_path)
    text = parsed_text(doc) if (master or numeric) else ""

    matcher, match_error = None, None
    if master:
        if match is None:
            match, matcher, match_error = _match_text(text, master_dir)
        else:
            try:
                from .fuzzy_matcher import get_matcher
                matcher = get_matcher(master_dir)
            except Exception as exc:  # noqa: BLE001
                match_error = f"{type(exc).__name__}: {exc}"

    packet, ctx, info = parse_doc(doc, po_hint, match)
    packet, schema_error = validate(packet)
    trace = packet.get("traceability") or {}
    master_dir_used = str(matcher.md.master_dir) if matcher is not None else None  # type: ignore[attr-defined]
    result = {
        "source_file": stem,
        "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parser": {
            "engine": ENGINE,
            "ai_parse_document_version": doc.version,
            "input_path": str(ai_parse_path),
            **info,
            "schema_error": schema_error,
            "master_dir": master_dir_used,
        },
        "po_number_normalized": re.sub(r"\D", "", trace.get("po_number") or "") or po_hint,
        "lot_identifier_type": "supplier_lot",
        "packet": packet,
        "sources": ctx.sources,
        "cross_checks": _checks(packet),
        "numeric_confirm": _numeric_confirm(packet, text) if numeric else None,
        "master_match": _master_match(packet, match, matcher, match_error) if master else None,
    }
    if schema_error is None:
        result = ParsedPacket.model_validate(result).model_dump(mode="json")
    return result


def summary_row(result: dict) -> dict:
    """One line per packet for ``summary.json`` / the console."""
    packet = result.get("packet") or {}
    parser = result.get("parser") or {}
    numeric = result.get("numeric_confirm") or {}
    master = result.get("master_match") or {}
    return {
        "doc": result.get("source_file"),
        "page_kinds": parser.get("page_kinds"),
        "fallback_reason": parser.get("fallback_reason"),
        "schema_error": parser.get("schema_error"),
        "low_confidence": len(packet.get("low_confidence_fields") or []),
        "checks_failed": sorted(c["name"] for c in result.get("cross_checks") or [] if c.get("status") == "fail"),
        "numeric_conflicts": (numeric.get("counts") or {}).get("CONFLICT", 0),
        "numeric_verdict": numeric.get("verdict"),
        "master_item": master.get("matcher_item"),
        "master_flags": master.get("flags") or [],
        "needs_review": bool(numeric.get("needs_review") or master.get("needs_review")),
    }


# ------------------------------------------------------------------ files
def find_parses(target: Path) -> list[Path]:
    """Every ``*.ai_parse.json`` under ``target`` (a file, a document folder, or a run folder)."""
    target = Path(target)
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob(f"*{AI_PARSE_SUFFIX}") if p.is_file())


def write_final(ai_parse_path: Path, out_dir: Optional[Path] = None, match: Optional[dict] = None, *,
                master: bool = True, numeric: bool = True, master_dir: Optional[Path] = None) -> tuple[Path, dict]:
    """Write ``<stem>.final.json`` next to the input (default) or into ``out_dir``. Returns (path, result).

    Keyword options are those of :func:`build_final`.
    """
    ai_parse_path = Path(ai_parse_path)
    stem = stem_of(ai_parse_path)
    result = build_final(ai_parse_path, stem, match, master=master, numeric=numeric, master_dir=master_dir)
    folder = Path(out_dir) if out_dir else ai_parse_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stem}{FINAL_SUFFIX}"
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return target, result
