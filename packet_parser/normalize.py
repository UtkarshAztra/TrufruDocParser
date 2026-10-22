"""Dates, times, numbers and tick marks (see my_docs section 7). Nothing here guesses: a value that fails a
sanity check is returned unchanged together with a flag."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")
ISO_RE = re.compile(r"(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4}|\d{2})(?![\d/])")
TIME_RE = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m?\b\.?|(?<!\d)(\d{1,2}):(\d{2})(?!\d)", re.I)
YEAR_MIN, YEAR_MAX = 2024, 2027

TICK_CHARS = "✓√☑☒✔✗xX•"
EMPTY_CHARS = "☐-_. "
# "Yes: (✓)", "No: ( )", "N/A: (✓)", "Yes: ☑", "Yes: ( ☒)", "Yes: (<mark>✓</mark>)" ...
TICK_TOKEN = re.compile(
    r"\b(Yes|No|N/?A)\b\s*:?\s*(?:\(\s*(?:<mark>)?\s*(?P<box>(?:<s>)?[^()]{0,16}?(?:</s>)?)\s*(?:</mark>)?\s*\)|(?P<bare>[☑☐✓√☒✔]))",
    re.I)


def numbers(text: Optional[str]) -> list[float]:
    return [float(m.group().replace(",", "")) for m in NUM_RE.finditer(text or "")]


def num(text: Optional[str]) -> Optional[float]:
    found = numbers(text)
    return found[0] if found else None


def to_iso(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(ISO date or None, flag or None). Handles m/d/yy, mm/dd/yyyy, m-d-yy, m.d.yy, yyyy/mm/dd, mixed separators."""
    if not text:
        return None, None
    m = ISO_RE.search(text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        best = None
        for m in DATE_RE.finditer(text):
            mo, d, y = m.groups()
            yy = int(y) if len(y) == 4 else 2000 + int(y)
            try:
                date(yy, int(mo), int(d))
            except ValueError:
                best = best or ("invalid", m.group())
                continue
            best = ("ok", (yy, int(mo), int(d)))
            break
        if not best:
            return None, None
        if best[0] == "invalid":
            return None, f"not a calendar date: {best[1]!r}"
        y, mo, d = best[1]
    try:
        dt = date(y, mo, d)
    except ValueError:
        return None, f"not a calendar date: {text!r}"
    flag = None if YEAR_MIN <= dt.year <= YEAR_MAX else f"year {dt.year} outside {YEAR_MIN}-{YEAR_MAX}"
    return dt.isoformat(), flag


def to_24h(text: Optional[str]) -> tuple[Optional[str], str]:
    """(HH:MM or None, remainder of the text after the time token). '12pm' -> 12:00, '2:57pm' -> 14:57."""
    if not text:
        return None, ""
    m = TIME_RE.search(text)
    if not m:
        return None, text.strip()
    if m.group(4):
        h, mi, ap = int(m.group(4)), m.group(5), ""
    else:
        h, mi, ap = int(m.group(1)), m.group(2) or "00", m.group(3).lower()
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    if h > 23 or int(mi) > 59:
        return None, text.strip()
    return f"{h:02d}:{mi}", (text[:m.start()] + " " + text[m.end():]).strip(" ,;-")


def mark_state(mark: Optional[str]) -> str:
    """'tick' | 'struck' | 'empty' for the content of one box."""
    if mark is None:
        return "empty"
    if "<s>" in mark:
        return "struck"
    inner = re.sub(r"<[^>]+>", "", mark).strip()
    if inner == "":
        return "empty"
    if any(ch in TICK_CHARS for ch in inner) or "checked" in inner.lower():
        return "tick"
    if all(ch in EMPTY_CHARS for ch in inner):
        return "empty"
    if len(inner) == 1 and inner.isalpha():
        return "tick"          # a single stray letter inside a box ('U', 'V', 'J') is a tick the OCR mis-shaped
    return "unknown"


def read_ticks(window: str) -> dict[str, str]:
    """First Yes / No / N/A box states in `window`: {'yes': 'tick', 'no': 'empty', 'na': 'struck' ...}."""
    out: dict[str, str] = {}
    for m in TICK_TOKEN.finditer(window):
        key = {"yes": "yes", "no": "no"}.get(m.group(1).lower(), "na")
        if key in out:
            if key == "yes":        # a second "Yes" means the next question started
                break
            continue
        mark = m.group("box") if m.group("box") is not None else m.group("bare")
        out[key] = mark_state(mark)
        if len(out) == 3:
            break
    return out


def tick_value(states: dict[str, str]) -> tuple[str, Optional[str]]:
    """(yes|no|na|blank, flag). Exactly one ticked box wins; struck, several or none -> blank."""
    ticked = [k for k, v in states.items() if v == "tick"]
    struck = [k for k, v in states.items() if v == "struck"]
    unknown = [k for k, v in states.items() if v == "unknown"]
    if len(ticked) == 1:
        return ticked[0], None
    if len(ticked) > 1:
        return "blank", f"several boxes ticked: {ticked}"
    if struck:
        return "blank", f"only a struck-out tick ({struck[0]})"
    if unknown:
        return "blank", "unreadable mark in box"
    return "blank", None
