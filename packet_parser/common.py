"""Shared helpers: source tracking per field and the label -> value rule used on every form."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .elements import Element, Page, clean

LOW_CONFIDENCE = 0.7


@dataclass
class Ctx:
    """Where each output field came from (element id, page, OCR confidence) and why it is doubtful."""

    sources: dict[str, dict] = field(default_factory=dict)
    flags: list[tuple[str, str]] = field(default_factory=list)
    redact: set = field(default_factory=set)   # initials seen on stamps; scrubbed from every free-text field, never output

    def src(self, path: str, page: Optional[Page], elem: Optional[Element], pos: Optional[int] = None) -> None:
        if elem is None and page is not None and pos is not None:
            elem = page.elem_at(pos)
        if elem is None:
            return
        self.sources[path] = {"element": elem.id, "page": elem.page + 1, "type": elem.type, "confidence": elem.confidence}
        if elem.confidence is not None and elem.confidence < LOW_CONFIDENCE:
            self.flag(path, f"ocr confidence {elem.confidence:.2f}")

    def flag(self, path: str, reason: str) -> None:
        if (path, reason) not in self.flags:
            self.flags.append((path, reason))

    def low_confidence_fields(self) -> list[str]:
        seen: dict[str, list[str]] = {}
        for path, reason in self.flags:
            seen.setdefault(path, []).append(reason)
        return [f"{p} ({'; '.join(r)})" for p, r in seen.items()]


def _stop_regex(stops: list[str]) -> Optional[re.Pattern]:
    return re.compile("|".join(f"(?:{s})" for s in stops), re.I) if stops else None


def after_label(page: Page, label: str, stops: list[str], max_len: int = 160,
                same_line_only: bool = False) -> tuple[Optional[str], Optional[int]]:
    """Value that follows the first `label` match: the rest of that line, or the next non-empty line / cell.

    Works for all three OCR shapes of a form field: `[header] Label:` + `[text] value`, table cells
    `Label: | value`, and inline `Label: value`. The value is cut at the first other known label (`stops`).
    Returns (value, text position of the value) or (None, None).
    """
    m = re.search(label, page.text, re.I)
    if not m:
        return None, None
    window = page.text[m.end(): m.end() + max_len]
    stop = _stop_regex(stops)
    if stop:
        s = stop.search(window)
        if s:
            window = window[: s.start()]
    pieces = re.split(r"\n|\s\|\s", window)
    offset = m.end()
    for piece in pieces:
        value = clean(piece)
        if value:
            return value, offset + window.find(piece)
        if same_line_only:
            break
        offset += len(piece) + 1
    return None, None


def value_after(page: Page, label: str, stops: list[str], ctx: Ctx, path: str, max_len: int = 160) -> Optional[str]:
    value, pos = after_label(page, label, stops, max_len)
    if value is not None:
        ctx.src(path, page, None, pos)
    return value


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")
