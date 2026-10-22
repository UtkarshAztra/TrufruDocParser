"""OCR keyword fuzzy matcher: which master-data item a document is about, how sure, and what needs a human look.

Ported from ``TruFru-exp/experiments/v1/fuzzy_matcher``. Reads the three master CSVs in ``docs/official-docs``
(``master_keywords.csv``, ``master_products.csv``, ``master_lots.csv``) and matches a document's OCR text against
them with ``rapidfuzz``. No LLM is involved; the verdict only ever adds flags.

    from packet_parser.fuzzy_matcher import get_matcher, match_text
    verdict = match_text(parsed_text, get_matcher())          # CSVs are loaded once per process
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from . import config
from .master_data import MasterData
from .matcher import Hit, KeywordMatcher
from .resolver import resolve


@lru_cache(maxsize=4)
def _cached(master_dir: str, classes: Optional[frozenset]) -> KeywordMatcher:
    return KeywordMatcher(MasterData.load(Path(master_dir)), classes=set(classes) if classes else None)


def get_matcher(master_dir: Optional[Path | str] = None, classes: Optional[Iterable[str]] = None) -> KeywordMatcher:
    """A matcher over the master CSVs in ``master_dir`` (default: ``docs/official-docs``), loaded once per process.

    ``classes`` restricts hits to item classes such as ``{"RM"}``. Raises ``FileNotFoundError`` when a CSV is missing.
    """
    folder = Path(master_dir or config.MASTER_DIR).resolve()
    return _cached(str(folder), frozenset(c.upper() for c in classes) if classes else None)


def match_text(text: str, matcher: Optional[KeywordMatcher] = None) -> dict:
    """Match one document's OCR text and return the verdict dict (see ``resolver.resolve``)."""
    m = matcher or get_matcher()
    return resolve(m.find(text), m.md)


__all__ = ["MasterData", "KeywordMatcher", "Hit", "resolve", "get_matcher", "match_text", "config"]
