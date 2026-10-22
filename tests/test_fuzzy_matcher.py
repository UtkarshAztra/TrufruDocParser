"""Behaviour tests for the OCR keyword matcher (ported from the v1 fuzzy_matcher experiment).

Run from the project root: python -m pytest -q
Needs the master CSVs in docs/official-docs (or $TRUFRU_MASTER_DIR).
"""
from pathlib import Path

import pytest

from packet_parser.fuzzy_matcher import KeywordMatcher, config, get_matcher, match_text
from packet_parser.fuzzy_matcher.normalize import canon_phrase, fold, repair_item_code

SAMPLES = Path(__file__).resolve().parent / "samples"

pytestmark = pytest.mark.skipif(
    not all((config.MASTER_DIR / n).is_file() for n in config.MASTER_FILES.values()),
    reason=f"master CSVs not found in {config.MASTER_DIR}")


@pytest.fixture(scope="module")
def m():
    return get_matcher()


def run(m, text):
    return match_text(text, m)


def flags(res):
    return {f["code"] for f in res["flags"]}


# ---------------------------------------------------------------- normalisation
def test_repair_item_code_fixes_letter_o_and_spaces():
    assert repair_item_code("CO1547. AD") == ("C01547.AD", True)
    assert repair_item_code("C01547.AB") == ("C01547.AB", False)
    assert repair_item_code("P00413") == ("P00413", False)
    assert repair_item_code("X12345") == (None, False)


def test_canon_phrase_maps_supplier_wording():
    assert canon_phrase("Strawberries, Halves IQF") == "STRAWBERRY HALVED IQF"
    assert canon_phrase("Dark Semi-Sweet Choc") == "DARK SEMISWEET CHOCOLATE"
    assert canon_phrase("STRW A 30# cn Sliced") == "STRAWBERRY A 30 CN SLICED"


def test_fold_is_symmetric_for_lookalikes():
    assert fold("25O919B") == fold("250919B")
    assert fold("LC 25 207") == fold("LC25207")


# ---------------------------------------------------------------- loading
def test_get_matcher_is_cached_per_master_dir(m):
    assert get_matcher() is m
    assert get_matcher(config.MASTER_DIR) is m


def test_missing_master_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_matcher(tmp_path)


# ---------------------------------------------------------------- codes and lots
def test_exact_item_code_confirms(m):
    r = run(m, "Item C01544.AA Milk Chocolate USA")
    assert r["status"] == "CONFIRMED" and r["item_code"] == "C01544.AA"


def test_lot_with_spaces_and_dash_confirms(m):
    r = run(m, "Milk Chocolate lot 225108 01")
    assert r["item_code"] == "C01544.AA"
    assert r["lots_found"][0]["lot"] == "225108-01"


def test_ocr_letter_o_in_lot_is_folded(m):
    r = run(m, "IQF Strawberry Halved USA lot 25O919B")
    assert r["status"] == "CONFIRMED" and r["item_code"] == "C01547.AC"
    assert r["lots_found"][0]["method"] == "ocr_folded"


def test_one_digit_off_lot_is_near_match_and_flagged(m):
    r = run(m, "packaging lot 20250069631")            # real lot is 20250069630
    assert "near_match" in flags(r) and r["needs_review"]


def test_po_number_does_not_match_a_lot(m):
    r = run(m, "Bill of Lading PO# 20260089938 Net Weight 40200")
    assert r["status"] == "NO_MATCH"


def test_unknown_suffix_is_new_variant(m):
    r = run(m, "Item#: CO1547. AD Strawberry Halved")
    assert r["status"] == "NEW_VARIANT"
    assert {c["item_code"] for c in r["candidates"]} == {"C01547.AB", "C01547.AC"}
    assert "unknown_item_code" in flags(r)


# ---------------------------------------------------------------- descriptions + two-clue rule
def test_generic_strawberry_plus_peru_is_likely_peru(m):
    r = run(m, "IQF Strawberries Halves, product of Peru")
    assert r["status"] == "LIKELY" and r["item_code"] == "C01547.AB"


def test_generic_strawberry_plus_usa_without_organic_is_conventional(m):
    r = run(m, "Strawberries, Halves IQF  Product of USA")
    assert r["item_code"] == "C01547.AC"


def test_organic_tickbox_yes_picks_organic(m):
    r = run(m, "IQF Strawberry Halved USA  Organic: Yes: (✓) No: ( )")
    assert r["item_code"] == "C01565.AA"


def test_organic_tickbox_no_is_not_treated_as_organic(m):
    r = run(m, "IQF Strawberry Halved USA  Organic: Yes: ( ) No: (✓)")
    assert r["item_code"] == "C01547.AC" and r["observed"]["organic"] == "no"


def test_generic_strawberry_alone_is_ambiguous(m):
    r = run(m, "IQF Strawberry Halved")
    assert r["status"] == "AMBIGUOUS" and len(r["candidates"]) == 3


def test_18oz_pouch_does_not_match_8oz(m):
    r = run(m, "Tru Fru Milk Chocolate Strawberry Printed 18 OZ Pouch")
    assert r["item_code"] == "P00409"


def test_8oz_pouch_does_not_match_18oz(m):
    r = run(m, "Tru Fru Milk Chocolate Strawberry Printed 8 OZ Pouch")
    assert r["item_code"] == "P00407"


def test_parsed_md_markers_and_html_are_ignored(m):
    text = "===== PAGE 1 =====\n[section_header] Item#:\n[text] C01544.AA\n[table] <table><tr><td>Milk Chocolate</td></tr></table>"
    r = run(m, text)
    assert r["status"] == "CONFIRMED" and r["item_code"] == "C01544.AA"


# ---------------------------------------------------------------- conflicts
def test_lot_vs_origin_conflict(m):
    r = run(m, "IQF Strawberry Halved  Origin: Peru  Lot: 250919B")   # 250919B is a USA lot
    assert r["status"] == "CONFLICT" and "origin_mismatch" in flags(r)


def test_two_codes_for_different_items_conflict(m):
    r = run(m, "C01544.AA and C01545.AA")
    assert r["status"] == "CONFLICT" and "codes_disagree" in flags(r)


def test_class_filter_ignores_packaging(m):
    rm_only = KeywordMatcher(m.md, classes={"RM"})
    r = run(rm_only, "lot 20250069630 Milk Chocolate USA")
    assert r["item_code"] == "C01544.AA"


def test_phrase_keyword_tie_break_is_stable(m):
    a = run(m, "IQF Strawberries Halves Peru")["evidence"]["phrases"]
    b = run(m, "IQF Strawberries Halves Peru")["evidence"]["phrases"]
    assert [h["keyword"] for h in a] == [h["keyword"] for h in b]


# ---------------------------------------------------------------- sample files end to end
@pytest.mark.parametrize("name,status,item", [
    ("01_coa_peru_clean.txt", "CONFIRMED", "C01547.AB"),
    ("02_bol_usa_ocr_noise.txt", "CONFIRMED", "C01547.AC"),
    ("03_bol_chile_new_variant.txt", "NEW_VARIANT", None),
    ("04_conflict_lot_vs_origin.txt", "CONFLICT", "C01547.AC"),
    ("05_organic_tickbox_yes.txt", "LIKELY", "C01565.AA"),
    ("06_packaging_18oz_pouch.txt", "LIKELY", "P00409"),
    ("07_unrelated_document.txt", "NO_MATCH", None),
])
def test_samples(m, name, status, item):
    r = run(m, (SAMPLES / name).read_text(encoding="utf-8"))
    assert (r["status"], r["item_code"]) == (status, item), r["flags"]
