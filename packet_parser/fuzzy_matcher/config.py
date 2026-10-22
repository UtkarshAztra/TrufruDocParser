"""Paths, thresholds and evidence weights for the OCR keyword matcher.

Ported from ``TruFru-exp/experiments/v1/fuzzy_matcher/fuzzy_matcher/config.py``. The master CSVs live in the
project's ``docs/official-docs``; override the folder with the ``TRUFRU_MASTER_DIR`` environment variable, the
``--master-dir`` CLI flag, or by passing ``master_dir`` to :func:`packet_parser.fuzzy_matcher.get_matcher`.
"""
import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent                 # packet_parser/fuzzy_matcher
PROJECT_DIR = PKG_DIR.parents[1]                          # project root
MASTER_DIR = Path(os.environ.get("TRUFRU_MASTER_DIR") or PROJECT_DIR / "docs" / "official-docs")

MASTER_FILES = {
    "keywords": "master_keywords.csv",
    "products": "master_products.csv",
    "lots": "master_lots.csv",
}

# Codes / lots. The CSV's ratio thresholds (92/95) are stricter than they look: one substituted
# character in an 11-digit lot already scores 90.9. So codes and lots use an edit-distance rule instead.
CODE_MIN_LEN_FOR_NEAR = 8        # near matches (1 edit) only for identifiers this long or longer
CODE_MAX_EDITS = 1

# Phrases: token_sort_ratio on canonicalised text; per-keyword threshold comes from master_keywords.csv.
PHRASE_THRESHOLD_FLOOR = 85
SINGLE_WORD_PHRASE_THRESHOLD = 92  # one-word synonyms ("Strawberry") need near-exact spelling

# Countries we recognise in text even if they are not in the master data (flagged as unknown_origin).
KNOWN_COUNTRIES = ["USA", "PERU", "CHILE", "MEXICO", "CANADA", "MOROCCO", "EGYPT", "CHINA", "POLAND",
                   "SERBIA", "ARGENTINA", "GUATEMALA", "TURKEY", "SPAIN"]
COUNTRY_ALIASES = {"U.S.A.": "USA", "US": "USA", "UNITED STATES": "USA"}

# Cut / form words. Master data only has Halved strawberries; any other cut is reported.
CUT_WORDS = {"HALVED": "Halved", "SLICED": "Sliced", "DICED": "Diced", "WHOLE": "Whole"}

# Keyword types that count as "strong" (identify one item on their own when matched exactly).
STRONG_TYPES = {"item_code", "item_code_compact", "lot_input", "lot_fg", "lot_compact"}
PHRASE_TYPES = {"item_desc", "item_desc_core", "product_phrase", "product_synonym"}
EXACT_WORD_TYPES = {"origin", "attribute", "pack_size", "unit", "item_class"}
