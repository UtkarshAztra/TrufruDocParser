"""Page-kind detection from printed titles and labels (see my_docs section 4)."""

from __future__ import annotations

import re

from .elements import Page

IMIR, VDF_PO, VDF_PO_CONT, BOL_FREEZPAK, BOL_COLDSTORE, BOL_OTHER = (
    "IMIR", "VDF_PO", "VDF_PO_cont", "BOL_FreezPak", "BOL_ColdStore", "BOL_other")
MICRO, COA, STAMP_ONLY, OTHER = "MicroReport", "COA", "StampOnly", "other"

BOL_KINDS = (BOL_FREEZPAK, BOL_COLDSTORE, BOL_OTHER)


def classify(page: Page) -> str:
    t = page.text
    if re.search(r"Incoming\s+material\s+inspection\s+Report", t, re.I) or "4-FRC-COR-031" in t:
        return IMIR
    if re.search(r"Purchase Order Number", t, re.I):
        return VDF_PO if re.search(r"Our Item\s*#", t, re.I) else VDF_PO_CONT
    if re.search(r"OF LADING", t) and (re.search(r"\bShip From\b", t) or re.search(r"Transaction Number|PO/Sales #", t)):
        return BOL_FREEZPAK
    if re.search(r"SHORT FORM BILL OF LADING", t) or re.search(r"From the Account of", t, re.I):
        return BOL_COLDSTORE
    if re.search(r"BILL OF LADING", t, re.I) or re.search(r"\bOF LADING\b", t):
        return BOL_OTHER
    if re.search(r"MICROBIOLOGICAL REPORT", t, re.I):
        return MICRO
    if re.search(r"CERTIFICATE OF ANALYSIS", t, re.I):
        return COA
    if re.search(r"TRUCK ARRIVED", t):
        return STAMP_ONLY
    return OTHER


def documents_present(kinds: list[str]) -> list[str]:
    out: list[str] = []
    mapping = [
        (IMIR, "inspection_report"), (VDF_PO, "purchase_order"), (VDF_PO_CONT, "purchase_order"),
        (BOL_FREEZPAK, "bill_of_lading"), (BOL_COLDSTORE, "bill_of_lading"), (BOL_OTHER, "bill_of_lading"),
        (MICRO, "coa"), (COA, "coa"),
    ]
    for kind, name in mapping:
        if kind in kinds and name not in out:
            out.append(name)
    if not out:
        out.append("other")
    return out
