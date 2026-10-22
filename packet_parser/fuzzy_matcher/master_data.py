"""Load the three master CSVs and build lookup indexes used by the matcher."""
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .normalize import canon_phrase, compact, fold, norm


def _read(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _codes(s: str):
    return [c for c in (s or "").split("|") if c]


@dataclass
class Phrase:
    canon: str                 # canonical phrase used for matching
    tokens: int
    item_codes: set
    keyword_ids: list
    keyword_types: set
    keywords: set              # original spellings, for reporting
    threshold: int


@dataclass
class MasterData:
    products: dict                                         # item_code -> product row
    lots: dict                                             # lot -> lot row
    code_exact: dict = field(default_factory=dict)         # compact -> [keyword rows] (codes, lots)
    code_folded: dict = field(default_factory=lambda: defaultdict(list))  # folded -> [keyword rows]
    code_by_len: dict = field(default_factory=lambda: defaultdict(list))  # len(folded) -> [folded]
    phrases: list = field(default_factory=list)            # [Phrase]
    exact_words: list = field(default_factory=list)        # keyword rows matched with word boundaries
    generic_terms: list = field(default_factory=list)
    production_nos: dict = field(default_factory=dict)     # compact -> keyword row
    item_bases: dict = field(default_factory=dict)         # base code -> [item codes]
    origins: set = field(default_factory=set)
    master_dir: Path = field(default_factory=lambda: config.MASTER_DIR)

    @classmethod
    def load(cls, master_dir: Path = config.MASTER_DIR) -> "MasterData":
        master_dir = Path(master_dir)
        missing = [n for n in config.MASTER_FILES.values() if not (master_dir / n).is_file()]
        if missing:
            raise FileNotFoundError(f"master data not found in {master_dir}: {', '.join(missing)}")
        kw = _read(master_dir / config.MASTER_FILES["keywords"])
        products = {r["item_code"]: r for r in _read(master_dir / config.MASTER_FILES["products"])}
        lots = {r["lot"]: r for r in _read(master_dir / config.MASTER_FILES["lots"])}
        md = cls(products=products, lots=lots, master_dir=master_dir)

        for p in products.values():
            md.item_bases.setdefault(p["item_code_base"], []).append(p["item_code"])
            if p.get("origin"):
                md.origins.add(p["origin"].upper())

        phrase_groups = {}
        for r in kw:
            r["item_code_list"] = _codes(r["item_codes"])
            t = r["keyword_type"]
            if t in config.STRONG_TYPES:
                md.code_exact.setdefault(r["keyword_compact"], []).append(r)
                f = fold(r["keyword_compact"])
                if r not in md.code_folded[f]:
                    md.code_folded[f].append(r)
                    if f not in md.code_by_len[len(f)]:
                        md.code_by_len[len(f)].append(f)
            elif t == "item_code_base":
                continue                                    # handled via item_bases
            elif t == "production_no":
                md.production_nos[r["keyword_compact"]] = r
            elif t in config.PHRASE_TYPES:
                c = canon_phrase(r["keyword"])
                g = phrase_groups.get(c)
                if g is None:
                    thr = max(int(r["suggested_threshold"]), config.PHRASE_THRESHOLD_FLOOR)
                    if len(c.split()) == 1:
                        thr = max(thr, config.SINGLE_WORD_PHRASE_THRESHOLD)
                    g = phrase_groups[c] = Phrase(c, len(c.split()), set(), [], set(), set(), thr)
                g.item_codes.update(r["item_code_list"])
                g.keyword_ids.append(r["keyword_id"])
                g.keyword_types.add(t)
                g.keywords.add(r["keyword"])
            elif t in config.EXACT_WORD_TYPES:
                r["pattern_norm"] = norm(r["keyword"])
                md.exact_words.append(r)
            elif t == "generic_term":
                r["pattern_norm"] = norm(r["keyword"])
                md.generic_terms.append(r)
        md.phrases = list(phrase_groups.values())
        return md
