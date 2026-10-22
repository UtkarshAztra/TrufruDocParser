"""Text normalisation shared by the master index and the OCR text, so both sides are compared the same way."""
import re

_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", " ": " "})

# Letters OCR commonly reads in place of digits (and the reverse). Used only on identifier-shaped strings.
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "Z": "2",
                           "S": "5", "B": "8", "G": "6"})
_TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"})

# Supplier abbreviations / plural forms -> one canonical word, applied to both master phrases and OCR text.
CANON_WORDS = {
    "STRAWBERRIES": "STRAWBERRY", "STRAWBERRYS": "STRAWBERRY", "STRW": "STRAWBERRY", "STRWB": "STRAWBERRY",
    "STRAW": "STRAWBERRY", "FRESA": "STRAWBERRY", "FRESAS": "STRAWBERRY",
    "HALVES": "HALVED", "HALVE": "HALVED", "HALF": "HALVED", "HLVD": "HALVED", "HALVD": "HALVED",
    "CHOC": "CHOCOLATE", "CHOCO": "CHOCOLATE", "CHOCOLATES": "CHOCOLATE",
    "SEMISWT": "SEMISWEET", "POUCHES": "POUCH", "BOXES": "BOX", "ZIPPERS": "ZIPPER",
}

ITEM_CODE_RE = re.compile(r"^([CPS])([0-9]{5})([A-Z]{2})?$")


def norm(s: str) -> str:
    """Uppercase, unify dashes, drop symbols except . + / % -, collapse spaces."""
    s = (s or "").translate(_DASHES).upper()
    s = re.sub(r"[^A-Z0-9.+/%# -]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compact(s: str) -> str:
    """Letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def fold(s: str) -> str:
    """OCR-confusion fold: map look-alike letters to digits. Apply to BOTH sides before comparing."""
    return compact(s).translate(_TO_DIGIT)


def canon_phrase(s: str) -> str:
    """Normalised text with semi-sweet joined and abbreviations/plurals mapped to canonical words."""
    s = norm(s)
    s = re.sub(r"\bSEMI[\s-]+SWEET\b", "SEMISWEET", s)
    s = re.sub(r"[.,/#+%-]", " ", s)
    return " ".join(CANON_WORDS.get(w, w) for w in s.split())


def repair_item_code(token: str):
    """Position-aware repair of an item-code-shaped token, e.g. 'CO1547. AD' -> 'C01547.AD'.

    Prefix must be C/P/S, then 5 digits, then an optional 2-letter suffix.
    Returns (code, was_repaired) or (None, False).
    """
    c = compact(token)
    if len(c) not in (6, 8) or c[0] not in "CPS":
        return None, False
    digits = c[1:6].translate(_TO_DIGIT)
    suffix = c[6:].translate(_TO_LETTER)
    fixed = c[0] + digits + suffix
    m = ITEM_CODE_RE.match(fixed)
    if not m:
        return None, False
    code = m.group(1) + m.group(2) + ("." + m.group(3) if m.group(3) else "")
    return code, fixed != c
