"""Find every master keyword in a piece of OCR text. Produces raw hits; resolver.py turns them into a verdict."""
import re
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

from . import config
from .master_data import MasterData
from .normalize import canon_phrase, compact, fold, norm, repair_item_code

METHOD_RANK = {"exact": 0, "ocr_repaired": 1, "ocr_folded": 2, "near_1_edit": 3}
TICK = r"[✓✔☑xXvV]"


@dataclass
class Hit:
    kind: str                  # code | lot | base_code | new_variant | phrase | word | production_no | generic
    keyword: str               # master keyword (or repaired code for new_variant)
    keyword_type: str
    item_codes: list
    matched_text: str          # the OCR text that matched
    method: str                # exact | ocr_repaired | ocr_folded | near_1_edit | fuzzy | word
    score: float = 100.0
    keyword_ids: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


KEY_WORDS = set(config.KNOWN_COUNTRIES) | {"ORGANIC", "MILK", "WHITE", "DARK", "DI", "KRAFT", "COSTCO"}


def key_tokens(canon: str) -> set:
    """Words that tell products apart and therefore must match exactly, never fuzzily."""
    return {t for t in canon.split() if t in KEY_WORDS or t.isdigit()}


def clean_text(raw: str) -> str:
    """Strip HTML tags and '[section_header]'-style markers some OCR/markdown outputs carry."""
    t = re.sub(r"<[^>]+>", " ", raw or "")
    t = re.sub(r"^\s*\[[a-z_]+\]\s*", "", t, flags=re.M)
    return t


class KeywordMatcher:
    def __init__(self, master: MasterData | None = None, classes: set | None = None):
        self.md = master or MasterData.load()
        self.classes = {c.upper() for c in classes} if classes else None   # e.g. {"RM"} to ignore PM/FG

    # ---------------------------------------------------------------- public
    def find(self, raw_text: str) -> dict:
        text = clean_text(raw_text)
        lines = [l for l in text.splitlines() if l.strip()]
        hits = []
        hits += self._codes_and_lots(lines)
        hits += self._phrases(text)
        hits += self._exact_words(text)
        hits = [h for h in hits if self._class_ok(h)]
        return {
            "hits": hits,
            "observed": {
                "origins": self._origins(text),
                "cuts": self._cuts(text),
                "organic": self._organic(text),
            },
        }

    # ---------------------------------------------------------------- codes & lots
    def _candidates(self, lines):
        """Identifier-shaped strings: 1-3 adjacent tokens joined (handles 'CO1547. AD', 'LC 25 295')."""
        seen = set()
        for line in lines:
            toks = norm(line).split()
            for n in (1, 2, 3):
                for i in range(len(toks) - n + 1):
                    span = toks[i:i + n]
                    if n > 1 and not all(re.search(r"[A-Z0-9]", t) for t in span):
                        continue
                    c = compact("".join(span))
                    if len(c) < 5 or len(c) > 16 or not re.search(r"\d", c) or (c, n) in seen:
                        continue
                    seen.add((c, n))
                    yield c, " ".join(span), n

    def _codes_and_lots(self, lines):
        md, best = self.md, {}

        def keep(hit):
            k = (hit.kind, hit.keyword)
            cur = best.get(k)
            if cur is None or METHOD_RANK.get(hit.method, 9) < METHOD_RANK.get(cur.method, 9):
                best[k] = hit

        for c, raw, n in self._candidates(lines):
            if c in md.production_nos:
                r = md.production_nos[c]
                keep(Hit("production_no", r["keyword"], "production_no", r["item_code_list"], raw, "exact",
                         keyword_ids=[r["keyword_id"]]))
                continue

            found = False
            for r in md.code_exact.get(c, []):
                keep(self._kw_hit(r, raw, "exact")); found = True

            code, repaired = repair_item_code(c)
            if code and not found:
                if code in md.products:
                    rows = md.code_exact.get(compact(code), [])
                    for r in rows:
                        keep(self._kw_hit(r, raw, "ocr_repaired" if repaired else "exact")); found = True
                else:
                    base = code.split(".")[0]
                    if base in md.item_bases:
                        kind = "new_variant" if "." in code else "base_code"
                        keep(Hit(kind, code, "item_code_base", md.item_bases[base], raw,
                                 "ocr_repaired" if repaired else "exact"))
                        found = True
            if found:
                continue

            f = fold(c)
            for r in md.code_folded.get(f, []):
                keep(self._kw_hit(r, raw, "ocr_folded")); found = True
            if found or len(f) < config.CODE_MIN_LEN_FOR_NEAR or n > 2:
                continue

            near = []
            for L in (len(f) - 1, len(f), len(f) + 1):
                for cand in md.code_by_len.get(L, []):
                    if Levenshtein.distance(f, cand, score_cutoff=config.CODE_MAX_EDITS) <= config.CODE_MAX_EDITS:
                        near.extend(md.code_folded[cand])
            for r in near:
                h = self._kw_hit(r, raw, "near_1_edit")
                h.score = round(fuzz.ratio(f, fold(r["keyword_compact"])), 1)
                keep(h)
        return list(best.values())

    def _kw_hit(self, r, raw, method):
        kind = "code" if r["keyword_type"].startswith("item_code") else "lot"
        return Hit(kind, r["keyword"], r["keyword_type"], r["item_code_list"], raw, method,
                   keyword_ids=[r["keyword_id"]])

    # ---------------------------------------------------------------- phrases
    def _phrases(self, text):
        toks = canon_phrase(text).split()
        windows = {}
        hits = []
        for p in self.md.phrases:
            sizes = {p.tokens, p.tokens + 1} | ({p.tokens - 1} if p.tokens >= 3 else set())
            keys = key_tokens(p.canon)
            best = None
            for w in sizes:
                if w not in windows:
                    windows[w] = [" ".join(toks[i:i + w]) for i in range(max(len(toks) - w + 1, 0))]
                for win, score, _ in process.extract(p.canon, windows[w], scorer=fuzz.token_sort_ratio,
                                                     score_cutoff=p.threshold, limit=5):
                    # distinguishing words must appear exactly: 8 OZ must not hit 18 OZ, PERU must not hit PRODUCT
                    if keys and not keys <= set(win.split()):
                        continue
                    if best is None or score > best[1]:
                        best = (win, score)
            if best:
                # report the longest original spelling; ties broken alphabetically so output is stable across runs
                hits.append(Hit("phrase", sorted(p.keywords, key=lambda k: (len(k), k))[-1], "|".join(sorted(p.keyword_types)),
                                sorted(p.item_codes), best[0], "fuzzy" if best[1] < 100 else "exact",
                                round(best[1], 1), p.keyword_ids))
        return hits

    # ---------------------------------------------------------------- exact words
    def _exact_words(self, text):
        t = norm(text)
        hits = []
        for r in self.md.exact_words + self.md.generic_terms:
            if r["keyword_type"] == "attribute":
                continue                                     # Organic is read from the tick-box logic
            pat = re.escape(r["pattern_norm"]).replace(r"\ ", r"\s?")
            m = re.search(rf"(?<![A-Z0-9]){pat}(?![A-Z0-9])", t)
            if m:
                kind = "generic" if r["keyword_type"] == "generic_term" else "word"
                hits.append(Hit(kind, r["keyword"], r["keyword_type"], r["item_code_list"], m.group(0), "word",
                                keyword_ids=[r["keyword_id"]]))
        return hits

    # ---------------------------------------------------------------- observed attributes
    def _origins(self, text):
        t = norm(text)
        out = []
        for alias, c in config.COUNTRY_ALIASES.items():
            if alias != "US" and re.search(rf"(?<![A-Z]){re.escape(alias)}(?![A-Z])", t) and c not in out:
                out.append(c)
        for c in config.KNOWN_COUNTRIES:
            if re.search(rf"(?<![A-Z]){c}(?![A-Z])", t) and c not in out:
                out.append(c)
        return out

    def _cuts(self, text):
        toks = set(canon_phrase(text).split())
        return [v for k, v in config.CUT_WORDS.items() if k in toks]

    def _organic(self, text):
        """yes / no (tick-box says No) / mentioned (word present, no tick-box) / absent."""
        t = re.sub(r"\s+", " ", text)
        box = re.search(rf"ORGANIC\W*YES\W*\(\s*({TICK})?\s*\)\W*NO\W*\(\s*({TICK})?\s*\)", t, re.I)
        if box:
            if box.group(1) and not box.group(2):
                return "yes"
            if box.group(2) and not box.group(1):
                return "no"
            return "unclear"
        return "mentioned" if re.search(r"\bORGANIC\b", t, re.I) else "absent"

    def _class_ok(self, h):
        if not self.classes or not h.item_codes:
            return True
        return any(self.md.products.get(c, {}).get("item_class") in self.classes for c in h.item_codes)
