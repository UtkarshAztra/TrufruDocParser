"""Wiring tests: the parsed-text rendering matches pdf_parser's ``.parsed.md`` and ``build_final`` carries the
``numeric_confirm`` / ``master_match`` blocks. Uses a tiny synthetic ``ai_parse.json``."""
import json

import pytest

from packet_parser import build_final
from packet_parser.elements import load, parsed_text
from packet_parser.fuzzy_matcher import config

HAS_MASTER = all((config.MASTER_DIR / n).is_file() for n in config.MASTER_FILES.values())

RAW = {
    "metadata": {"version": "2.0"},
    "document": {
        "pages": [{"id": 0}, {"id": 1}],
        "elements": [
            {"id": 0, "type": "title", "page_id": 0, "content": "STRAIGHT BILL OF LADING", "confidence": 0.99},
            {"id": 1, "type": "text", "page_id": 0, "content": "Item C01544.AA Milk Chocolate USA  lot 225108-01"},
            {"id": 2, "type": "table", "page_id": 0,
             "content": "<table><tr><th>Actual Qty</th><th>Net Weight</th></tr><tr><td>10</td><td>300.00</td></tr></table>"},
            {"id": 3, "type": "figure", "page_id": 1, "content": "", "description": "a signature"},
            {"id": 4, "type": "text", "page_id": 1, "content": "   "},
        ],
    },
}


@pytest.fixture
def ai_parse(tmp_path):
    p = tmp_path / "SYNTHETIC_PO_20260000001.ai_parse.json"
    p.write_text(json.dumps(RAW), encoding="utf-8")
    return p


def test_parsed_text_matches_pdf_parser_format(ai_parse):
    from pdf_parser.render import parsed_to_text
    text = parsed_text(load(ai_parse))
    assert text == parsed_to_text(RAW)
    assert text.startswith("===== PAGE 1 =====\n[title] STRAIGHT BILL OF LADING\n[text] Item C01544.AA")
    assert "===== PAGE 2 =====\n[figure] a signature" in text
    assert "[text]    " not in text            # blank elements are dropped


def test_build_final_has_both_blocks_and_can_skip_them(ai_parse):
    result = build_final(ai_parse, master=False, numeric=False)
    assert result["numeric_confirm"] is None and result["master_match"] is None
    assert result["parser"]["master_dir"] is None

    result = build_final(ai_parse, master=False, numeric=True)
    nc = result["numeric_confirm"]
    assert nc["verdict"] == "ALL NUMBERS CONSISTENT" and nc["needs_review"] is False
    assert isinstance(nc["rules"], list) and isinstance(nc["numbers"], list)
    assert result["master_match"] is None


@pytest.mark.skipif(not HAS_MASTER, reason=f"master CSVs not found in {config.MASTER_DIR}")
def test_build_final_master_match_uses_the_document_text(ai_parse):
    result = build_final(ai_parse)
    mm = result["master_match"]
    assert mm["matcher_status"] == "CONFIRMED" and mm["matcher_item"] == "C01544.AA"
    assert "225108-01" in [l["lot"] for l in mm["lots_found"]]
    assert {c["name"] for c in mm["checks"]} == {"master_item_code", "master_origin", "master_organic", "master_lots", "master_cut"}
    assert result["parser"]["master_dir"] == str(config.MASTER_DIR)
    # key order in the final JSON: cross_checks, numeric_confirm, master_match
    keys = list(result)
    assert keys.index("cross_checks") < keys.index("numeric_confirm") < keys.index("master_match")


def test_missing_master_dir_is_reported_not_raised(ai_parse, tmp_path):
    result = build_final(ai_parse, master_dir=tmp_path / "nowhere")
    mm = result["master_match"]
    assert mm["matcher_status"] is None and mm["needs_review"] is True
    assert mm["flags"] and mm["flags"][0].startswith("not computed: FileNotFoundError")
    assert result["numeric_confirm"] is not None     # the other layer still runs
