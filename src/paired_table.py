"""Reconstruct a "paired items" table embedded inside an otherwise normal
question's stem - two labelled columns (e.g. "Active Voice" / "Passive
Voice"), each row introduced by a Roman numeral (I., II., ...), followed
by the question's own normal (a)-(d) options below it (e.g. "(a) I only").

Confirmed by direct inspection of a General Ability Test booklet (Q43,
"Read the following pairs :"). This is a different, simpler shape than
match_the_list.py's List I / List II / Code layout - no separate
answer-code grid, and it doesn't replace the question's stem/options,
just sits inside the stem.
"""

import re

from .ocr import Box
from .reading_order import _cluster_rows

ROMAN_ROW_RE = re.compile(r"^([IVX]{1,4})\s*[\.\-_,:]\s*(.*)$")
# a real second column needs a gap well past normal word-spacing - same
# spirit as reading_order.X_GAP_SPLIT, which splits a row-band into
# separate lines/columns at exactly this kind of gap.
MIN_COLUMN_GAP = 40


def _row_text(row: list[Box]) -> str:
    return " ".join(b.text for b in sorted(row, key=lambda b: b.x0)).strip()


def _row_gap_split(row: list[Box]) -> tuple[str, str] | None:
    """Split one row's boxes into (left_text, right_text) at its own
    largest x-gap, if that gap looks like a real column break rather than
    ordinary word-spacing. None if the row doesn't have one."""
    if len(row) < 2:
        return None
    row_sorted = sorted(row, key=lambda b: b.x0)
    gap_idx = max(range(len(row_sorted) - 1), key=lambda i: row_sorted[i + 1].x0 - row_sorted[i].x1)
    gap = row_sorted[gap_idx + 1].x0 - row_sorted[gap_idx].x1
    if gap < MIN_COLUMN_GAP:
        return None
    left = " ".join(b.text for b in row_sorted[: gap_idx + 1]).strip()
    right = " ".join(b.text for b in row_sorted[gap_idx + 1 :]).strip()
    return left, right


def detect(boxes: list[Box]) -> bool:
    """True if this question's boxes show a genuine 2-column grid under
    Roman-numeral row labels - not just a plain vertical list of numbered
    statements (e.g. "Consider the following statements: I. ... II. ..."),
    which has no second column and shouldn't be mistaken for this."""
    roman_rows = 0
    two_col_rows = 0
    for row in _cluster_rows(boxes):
        text = _row_text(row)
        if not ROMAN_ROW_RE.match(text):
            continue
        roman_rows += 1
        if _row_gap_split(row):
            two_col_rows += 1
    return roman_rows >= 2 and two_col_rows >= 2


def reconstruct(boxes: list[Box], q_number: int | None = None) -> tuple[dict, list[str]]:
    """Returns ({"headers": [left, right], "rows": [{"label", "col1", "col2"}, ...]}, problems)."""
    if q_number is not None:
        marker_re = re.compile(rf"^0*{q_number}\s*[\.\-_,:]?\s*$")
        boxes = [b for b in boxes if not marker_re.match(b.text.strip())]

    rows = sorted(_cluster_rows(boxes), key=lambda row: min(b.y0 for b in row))
    problems: list[str] = []
    headers: tuple[str, str] | None = None
    data_rows: list[dict] = []

    for row in rows:
        text = _row_text(row)
        if not text:
            continue
        m = ROMAN_ROW_RE.match(text)
        if not m:
            # The header row is the first non-Roman row that itself shows a
            # real column split - anything before that (e.g. "Read the
            # following pairs :") is ordinary stem text, not the table.
            if headers is None:
                split = _row_gap_split(row)
                if split:
                    headers = split
            continue

        label = m.group(1)
        split = _row_gap_split(row)
        if split is None:
            problems.append(f"row {label}: could not split into two columns")
            data_rows.append({"label": label, "col1": m.group(2).strip(), "col2": ""})
            continue
        left, right = split
        left_m = ROMAN_ROW_RE.match(left)
        data_rows.append({"label": label, "col1": (left_m.group(2).strip() if left_m else left), "col2": right})

    if not data_rows:
        problems.append("no table rows detected")
    if headers is None:
        problems.append("column headers not detected")

    return {"headers": list(headers) if headers else ["", ""], "rows": data_rows}, problems
