"""Turn raw keyword hits into one verdict per document: which item it is, how sure, and what needs a human look.

Statuses (most severe first):
  CONFLICT     strong evidence (codes / lots) points at different items, or contradicts origin / organic
  NEW_VARIANT  a known product family with a code suffix the master data has never seen (e.g. C01547.AD)
  CONFIRMED    a code or lot identifies exactly one item and nothing contradicts it
  LIKELY       only descriptions + attributes identify one item (two-clue rule); no code / lot
  AMBIGUOUS    descriptions match, but more than one item remains
  NO_MATCH     nothing from the master data was found
"""
from .normalize import canon_phrase

STRONG_METHODS = {"exact", "ocr_repaired", "ocr_folded"}


def _flag(flags, code, detail):
    flags.append({"code": code, "detail": detail})


def resolve(found: dict, md) -> dict:
    hits, obs = found["hits"], found["observed"]
    flags = []

    strong = [h for h in hits if h.kind in ("code", "lot") and h.method in STRONG_METHODS]
    near = [h for h in hits if h.kind in ("code", "lot") and h.method == "near_1_edit"]
    variants = [h for h in hits if h.kind == "new_variant"]
    bases = [h for h in hits if h.kind == "base_code"]
    phrases = [h for h in hits if h.kind == "phrase"]

    # ---- 1. strong evidence: unambiguous codes / lots
    strong_items = {}
    for h in strong:
        if len(h.item_codes) == 1:
            strong_items.setdefault(h.item_codes[0], []).append(h.keyword)
        else:
            _flag(flags, "multi_item_lot", f"{h.keyword} is recorded under {', '.join(h.item_codes)} in master_lots")

    # ---- 2. description evidence: longer phrases matched well outweigh short generic ones
    #         ("Tru Fru Milk Chocolate Strawberry Printed 18 OZ Pouch" beats "Milk Chocolate")
    phrase_items = {}
    for h in phrases:
        weight = h.score / 100.0 * len(canon_phrase(h.keyword).split())
        for c in h.item_codes:
            phrase_items[c] = max(phrase_items.get(c, 0), weight)

    # ---- 3. attribute filters (two-clue rule), applied before picking the top description match
    def by_attributes(cands):
        out = set(cands)
        known_obs = [o for o in obs["origins"] if o in md.origins]
        if known_obs:
            narrowed = {c for c in out if md.products[c].get("origin", "").upper() in known_obs
                        or not md.products[c].get("origin")}
            out = narrowed or out
        if obs["organic"] in ("yes", "mentioned"):
            out = {c for c in out if md.products[c].get("organic") == "Y"} or out
        elif obs["organic"] == "no" or (known_obs and len(out) > 1):
            # tick-box says No, or origin given without the word Organic -> conventional item
            out = {c for c in out if md.products[c].get("organic") != "Y"} or out
        return out

    status, chosen, candidates = "NO_MATCH", None, []
    if len(strong_items) > 1:
        status, candidates = "CONFLICT", sorted(strong_items)
        _flag(flags, "codes_disagree", "; ".join(f"{c} <- {', '.join(sorted(set(v)))}" for c, v in strong_items.items()))
    elif len(strong_items) == 1:
        chosen = next(iter(strong_items))
        status, candidates = "CONFIRMED", [chosen]
    elif variants:
        status = "NEW_VARIANT"
        candidates = sorted({c for h in variants for c in h.item_codes})
        for h in variants:
            _flag(flags, "unknown_item_code",
                  f"'{h.matched_text}' -> {h.keyword}: family {h.keyword.split('.')[0]} is known "
                  f"({', '.join(h.item_codes)}) but this suffix is not in master_products")
    elif phrase_items:
        allowed = by_attributes(phrase_items)
        top = max(phrase_items[c] for c in allowed)
        narrowed = {c for c in allowed if phrase_items[c] >= top * 0.95}
        candidates = sorted(narrowed)
        if len(narrowed) == 1:
            status, chosen = "LIKELY", next(iter(narrowed))
        else:
            status = "AMBIGUOUS"
    elif bases:
        status = "AMBIGUOUS"
        candidates = sorted({c for h in bases for c in h.item_codes})

    # ---- 4. consistency checks on the chosen item
    if chosen:
        p = md.products[chosen]
        if phrases and chosen not in phrase_items:
            _flag(flags, "description_mismatch",
                  f"{chosen} found by code/lot, but the descriptions on the page match "
                  f"{', '.join(sorted(phrase_items))}")
        item_origin = p.get("origin", "").upper()
        other_origins = [o for o in obs["origins"] if o != item_origin]
        if item_origin and other_origins:
            _flag(flags, "origin_mismatch", f"{chosen} is {p['origin']} but the page also mentions {', '.join(other_origins)}")
        if p.get("organic") == "Y" and obs["organic"] == "no":
            _flag(flags, "organic_mismatch", f"{chosen} is organic but the tick-box says Organic: No")
        if p.get("organic") == "N" and obs["organic"] == "yes":
            _flag(flags, "organic_mismatch", f"{chosen} is not organic but the tick-box says Organic: Yes")
        if p.get("organic") == "N" and obs["organic"] == "mentioned" and p.get("item_class") == "RM":
            _flag(flags, "organic_mentioned", f"{chosen} is not organic but the page mentions Organic")
        if p.get("cut") and obs["cuts"] and any(c != p["cut"] for c in obs["cuts"]):
            _flag(flags, "cut_mismatch", f"{chosen} is {p['cut']} but the page also says {', '.join(obs['cuts'])}")
        if status == "CONFIRMED" and any(f["code"].endswith("_mismatch") for f in flags):
            status = "CONFLICT"
    else:
        unknown = [o for o in obs["origins"] if o not in md.origins]
        if unknown:
            _flag(flags, "unknown_origin", f"page mentions {', '.join(unknown)}; master data only has "
                                           f"{', '.join(sorted(md.origins))}")
        other_cuts = [c for c in obs["cuts"] if c != "Halved"]
        if other_cuts:
            _flag(flags, "unknown_cut", f"page mentions {', '.join(other_cuts)}; master data only has Halved")

    for h in near:
        _flag(flags, "near_match", f"'{h.matched_text}' is 1 character away from {h.keyword} "
                                   f"({', '.join(h.item_codes)}); check the OCR")
    if obs["organic"] == "unclear":
        _flag(flags, "organic_unclear", "Organic tick-box could not be read")

    lots_found = []
    for h in hits:
        if h.kind == "lot":
            row = md.lots.get(h.keyword, {})
            lots_found.append({"lot": h.keyword, "matched_text": h.matched_text, "method": h.method,
                               "lot_role": row.get("lot_role"), "item_code": row.get("item_code"),
                               "n_jobs": row.get("n_jobs"), "linked_fg_lots": row.get("linked_fg_lots")})

    return {
        "status": status,
        "item_code": chosen,
        "item_desc": md.products[chosen]["item_desc"] if chosen else None,
        "candidates": [{"item_code": c, "item_desc": md.products[c]["item_desc"]} for c in candidates],
        "needs_review": status != "CONFIRMED" or bool(flags),
        "flags": flags,
        "observed": obs,
        "lots_found": lots_found,
        "evidence": {
            "codes_and_lots": [h.to_dict() for h in strong + near],
            "new_variants": [h.to_dict() for h in variants],
            "base_codes": [h.to_dict() for h in bases],
            "phrases": [h.to_dict() for h in sorted(phrases, key=lambda h: -h.score)],
            "words": [h.to_dict() for h in hits if h.kind == "word"],
            "production_nos": [h.to_dict() for h in hits if h.kind == "production_no"],
            "generic_terms": sorted({h.keyword for h in hits if h.kind == "generic"}),
        },
    }
