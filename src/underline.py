"""Best-effort underline detection: for a text line, check whether a
horizontal dark stroke runs just below the text baseline, and if so, which
word(s) along that line it sits under.
"""

import numpy as np

from .ocr import Box
from .reading_order import Line


def line_has_underline(gray: np.ndarray, line: Line, strip_frac: float = 0.18) -> list[Box]:
    """Return the subset of `line.boxes` whose x-range overlaps a detected
    underline stroke just below the line's text. Empty list if none found.
    """
    h_img, w_img = gray.shape
    y1 = int(min(line.y1, h_img - 1))
    line_h = max(line.y1 - line.y0, 8)
    strip_top = int(min(y1 + 1, h_img - 1))
    strip_bot = int(min(y1 + max(3, line_h * strip_frac), h_img))
    if strip_bot <= strip_top:
        return []

    strip = gray[strip_top:strip_bot, :]
    # Dark pixels (text/underline ink) are low intensity on a light background.
    dark = strip < 128
    col_has_ink = dark.any(axis=0)

    underlined_boxes = []
    for box in line.boxes:
        x0, x1 = int(box.x0), int(box.x1)
        if x1 <= x0:
            continue
        span = col_has_ink[x0:x1]
        if span.size == 0:
            continue
        coverage = span.sum() / span.size
        if coverage > 0.6:  # most of the word's width has ink directly beneath it
            underlined_boxes.append(box)
    return underlined_boxes


def detect_underlined_words(gray: np.ndarray, line: Line) -> frozenset:
    """Return the set of plain-text words in `line` detected as underlined.

    Deliberately does NOT touch line.text - structural parsing (question
    numbers, option markers) must always match against clean, unmarked OCR
    text. Underline markup is applied later, only to already-assembled
    output strings (see mark_underlines_in_text), so a false-positive
    underline can never break the parser.
    """
    underlined = line_has_underline(gray, line)
    return frozenset(b.text for b in underlined)


def mark_underlines_in_text(text: str, underlined_words: frozenset) -> str:
    """Wrap any word in `text` that matches an underlined word with <u>...</u>."""
    if not underlined_words:
        return text
    words = text.split(" ")
    return " ".join(f"<u>{w}</u>" if w in underlined_words else w for w in words)
