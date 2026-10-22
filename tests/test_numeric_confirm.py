"""Tests for the numeric confirmer (ported from the v1 interdependent_numeric_data experiment) and the
printed-number reader. Pure Python, no master data or Databricks needed."""
import copy

from packet_parser.numeric_confirm import confirm, numeric_confirm, to_block
from packet_parser.text_reader import read_printed_text

# A FreezPak-style packet: 1310 cases x 30 lb = 39,300 lb net, consistent across IMIR / PO / BOL.
PACKET = {
    "inspection_report": {
        "lots": [{"manufacturing_lot": "LC25207", "vdf_lot_number": "C142265",
                  "boxes_received": 1310, "weight_per_box_lbs": 30, "total_weight_lbs": 39300}],
        "product_temperature_class": "frozen",
        "product_temperature_f": -10,
        "damaged_boxes_count": None,
    },
    "purchase_order": {
        "lines": [{"our_item_number": "C01547.AB", "quantity": 39300.0, "unit_of_measure": "LB"},
                  {"our_item_number": "FREIGHT", "quantity": 1, "unit_of_measure": "EA"}],
        "handwritten_notes": ["70 boxes / full pallet", "19 pallets"],
        "handwritten_lot": "C142265",
    },
    "bill_of_lading": {
        "lines": [{"actual_qty": 1310, "item_code": "10610", "item_description": "IQF STRAWBERRY HALVED",
                   "lot": "LC25207", "unit_weight_lbs": 30, "net_weight_lbs": 39300},
                  {"actual_qty": 1, "item_code": "TT", "item_description": "Temp Tracker Sensitech",
                   "lot": None, "unit_weight_lbs": 0.5, "net_weight_lbs": 0.5}],
        "total_qty": 1311, "total_net_weight_lbs": 39300.5, "total_gross_weight_lbs": 41100,
    },
    "traceability": {"quantity_cases": 1310, "case_weight_lbs": 30, "net_weight_lbs": 39300,
                     "gross_weight_lbs": 41100, "pallets": 19, "product_temperature_f": -10},
}

PARSED_TEXT = """===== PAGE 3 =====
[title] STRAIGHT BILL OF LADING
[text] PO# 2026-00-89938   Comment: 19 pallets
[table] <table><tr><th>Actual Qty</th><th>Item</th><th>Lot</th><th>Customer Lot</th><th>Unit Weight<br>BBD</th><th>Net Weight</th></tr>
<tr><td>1,310</td><td>10610 IQF STRAWBERRY HALVED</td><td>LC25207</td><td>C142265</td><td>30.00<br>03/01/2027</td><td>39,300.00</td></tr>
<tr><td>1</td><td>TT Temp Tracker</td><td></td><td></td><td>0.50</td><td>0.50</td></tr>
<tr><td colspan="4">TOTALS</td><td>Net Weight:<br>Gross Weight:</td><td>39,300.50<br>41,100.00</td></tr></table>
===== PAGE 2 =====
[text] C01547.AB IQF Strawberries Halves Peru   39,300.00 LB
"""


def statuses(block):
    return {n["field"]: n["status"] for n in block["numbers"]}


def rules(block):
    return {r["id"]: r["status"] for r in block["rules"]}


# ---------------------------------------------------------------- text reader
def test_text_reader_reads_freezpak_table_and_po_line():
    pr = read_printed_text(PARSED_TEXT)
    assert len(pr.rows) == 2 and len(pr.totals) == 1
    assert pr.rows[0].qty == [1310.0] and pr.rows[0].unit == [30.0] and pr.rows[0].net == [39300.0]
    assert pr.totals[0].net == [39300.5] and pr.totals[0].gross == [41100.0]
    assert pr.po_lb == [39300.0]
    assert 19.0 in pr.pallets
    assert pr.seen(39300.5) and not pr.seen(12345.0)


# ---------------------------------------------------------------- math only
def test_consistent_packet_passes_all_strict_rules():
    block = numeric_confirm(PACKET)
    assert block["verdict"] == "ALL NUMBERS CONSISTENT" and not block["needs_review"]
    rs = rules(block)
    assert rs["R1"] == "PASS" and rs["R2.1"] == "PASS" and rs["R3.1"] == "PASS"
    assert rs["R4"] == "PASS" and rs["R5"] == "PASS" and rs["R7.bol"] == "PASS" and rs["R8.imir"] == "PASS"
    assert rs["R10"] == "PASS" and rs["R12"] == "PASS" and rs["R13"] == "PASS" and rs["R14"] == "PASS"
    st = statuses(block)
    assert st["trace.quantity_cases"] == "CONFIRMED_MATH" and st["trace.net_weight_lbs"] == "CONFIRMED_MATH"
    assert "CONFLICT" not in st.values()


def test_wrong_case_count_is_blamed_with_expected_value():
    bad = copy.deepcopy(PACKET)
    bad["traceability"]["quantity_cases"] = 1301          # OCR transposition
    block = numeric_confirm(bad)
    assert block["needs_review"] and block["counts"]["CONFLICT"] == 1
    n = next(x for x in block["numbers"] if x["field"] == "trace.quantity_cases")
    assert n["status"] == "CONFLICT" and n["should_be"] == 1310
    assert "R1" in n["rules_disagree"] and "R7.bol" in n["rules_disagree"]
    # the partner numbers in the failed rules are not blamed
    assert statuses(block)["trace.case_weight_lbs"] != "CONFLICT"
    assert statuses(block)["trace.net_weight_lbs"] != "CONFLICT"


def test_short_shipment_only_warns():
    short = copy.deepcopy(PACKET)
    short["purchase_order"]["lines"][0]["quantity"] = 40000.0     # ordered more than received
    block = numeric_confirm(short)
    assert rules(block)["R10"] == "WARN" and not block["needs_review"]
    assert statuses(block)["po.line[1].quantity"] == "WARN"


def test_missing_numbers_skip_rules_and_report_missing():
    sparse = {"traceability": {"quantity_cases": 100, "case_weight_lbs": None, "net_weight_lbs": 3000}}
    block = numeric_confirm(sparse)
    assert rules(block)["R1"] == "SKIP"
    assert statuses(block)["trace.case_weight_lbs"] == "MISSING"
    assert statuses(block)["trace.quantity_cases"] == "UNVERIFIED"


def test_temperature_out_of_spec_is_report_only():
    warm = copy.deepcopy(PACKET)
    warm["inspection_report"]["product_temperature_f"] = 25
    warm["traceability"]["product_temperature_f"] = 25
    block = numeric_confirm(warm)
    assert rules(block)["R14"] == "WARN" and not block["needs_review"]


# ---------------------------------------------------------------- with printed text
def test_printed_text_confirms_bol_numbers():
    block = to_block(confirm(PACKET, read_printed_text(PARSED_TEXT)))
    st = statuses(block)
    assert st["bol.line[1].actual_qty"] == "CONFIRMED_TEXT"
    assert st["bol.line[1].net_weight_lbs"] == "CONFIRMED_TEXT"
    assert st["bol.total_gross_weight_lbs"] == "CONFIRMED_TEXT"
    assert st["po.line[1].quantity"] == "CONFIRMED_TEXT"
    n = next(x for x in block["numbers"] if x["field"] == "bol.total_net_weight_lbs")
    assert n["text_match"] is True


def test_printed_text_catches_a_misread_total():
    bad = copy.deepcopy(PACKET)
    bad["bill_of_lading"]["total_gross_weight_lbs"] = 44100     # text says 41,100
    bad["traceability"]["gross_weight_lbs"] = 44100
    block = to_block(confirm(bad, read_printed_text(PARSED_TEXT)))
    n = next(x for x in block["numbers"] if x["field"] == "bol.total_gross_weight_lbs")
    assert n["status"] == "CONFLICT" and n["text_match"] is False
    assert block["needs_review"]
