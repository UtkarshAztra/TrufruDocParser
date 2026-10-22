"""Rule-based packet extraction: ``<stem>.ai_parse.json`` (raw ``ai_parse_document`` output) -> final JSON.

No LLM is involved. Pages are classified by their printed titles, each document kind has its own
extractor, the three documents are merged into one traceability row, and the result is validated against
``schema.ReceivingPacket`` and enriched with deterministic cross-checks.

Ported from ``TruFru-exp/experiments/v1/regex_parser/run.py``. The optional steps of that experiment that
depend on sibling experiments (numeric confirmation against ``.parsed.md``, master-data match) are not
included here.
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
from .elements import Doc, load
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


def po_hint_from_name(name: str) -> Optional[str]:
    m = re.search(r"(\d{11})", name)
    return m.group(1) if m else None


def stem_of(ai_parse_path: Path) -> str:
    return Path(ai_parse_path).name.removesuffix(AI_PARSE_SUFFIX)


def build_final(ai_parse_path: Path, stem: Optional[str] = None, match: Optional[dict] = None) -> dict:
    """Final JSON (as a dict) for one ``<stem>.ai_parse.json``."""
    ai_parse_path = Path(ai_parse_path)
    stem = stem or stem_of(ai_parse_path)
    po_hint = po_hint_from_name(stem)
    doc = load(ai_parse_path)
    packet, ctx, info = parse_doc(doc, po_hint, match)
    packet, schema_error = validate(packet)
    trace = packet.get("traceability") or {}
    result = {
        "source_file": stem,
        "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parser": {
            "engine": ENGINE,
            "ai_parse_document_version": doc.version,
            "input_path": str(ai_parse_path),
            **info,
            "schema_error": schema_error,
        },
        "po_number_normalized": re.sub(r"\D", "", trace.get("po_number") or "") or po_hint,
        "lot_identifier_type": "supplier_lot",
        "packet": packet,
        "sources": ctx.sources,
        "cross_checks": _checks(packet),
    }
    if schema_error is None:
        result = ParsedPacket.model_validate(result).model_dump(mode="json")
    return result


def summary_row(result: dict) -> dict:
    """One line per packet for ``summary.json`` / the console."""
    packet = result.get("packet") or {}
    parser = result.get("parser") or {}
    return {
        "doc": result.get("source_file"),
        "page_kinds": parser.get("page_kinds"),
        "fallback_reason": parser.get("fallback_reason"),
        "schema_error": parser.get("schema_error"),
        "low_confidence": len(packet.get("low_confidence_fields") or []),
        "checks_failed": sorted(c["name"] for c in result.get("cross_checks") or [] if c.get("status") == "fail"),
    }


# ------------------------------------------------------------------ files
def find_parses(target: Path) -> list[Path]:
    """Every ``*.ai_parse.json`` under ``target`` (a file, a document folder, or a run folder)."""
    target = Path(target)
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob(f"*{AI_PARSE_SUFFIX}") if p.is_file())


def write_final(ai_parse_path: Path, out_dir: Optional[Path] = None, match: Optional[dict] = None) -> tuple[Path, dict]:
    """Write ``<stem>.final.json`` next to the input (default) or into ``out_dir``. Returns (path, result)."""
    ai_parse_path = Path(ai_parse_path)
    stem = stem_of(ai_parse_path)
    result = build_final(ai_parse_path, stem, match)
    folder = Path(out_dir) if out_dir else ai_parse_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stem}{FINAL_SUFFIX}"
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return target, result
