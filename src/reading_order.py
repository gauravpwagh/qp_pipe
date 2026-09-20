"""Turn raw OCR word/phrase boxes into a linear, reading-order text stream.

Approach: cluster raw boxes into text lines (row-band, then split by large
horizontal gaps so a same-height left-column line and right-column line
don't get merged into one). Classify each line as LEFT / RIGHT / FULL by
its horizontal extent relative to the page. Full-width lines (Directions
headers, passage paragraphs, section headings) stand on their own; runs of
LEFT/RIGHT lines are re-emitted column-major (all LEFT lines top-to-bottom,
then all RIGHT lines top-to-bottom) which reproduces the booklet's true
ascending question-number order, as verified by manual inspection of the
source PDF.
"""

from dataclasses import dataclass, field

from .ocr import Box
from .patterns import DIRECTIONS_RE, OPTION_RE, PASSAGE_RE, QUESTION_START_LOOSE_RE, QUESTION_START_RE

Y_OVERLAP_TOL = 10  # px: boxes within this y-center distance are considered the same row-band
X_GAP_SPLIT = 45  # px: a horizontal gap bigger than this splits a row-band into separate lines
FULL_WIDTH_FRAC = 0.55  # a line wider than this fraction of the page is "full width"
LEFT_EDGE_FRAC = 0.54  # a line ending before this fraction of page width is "left column"
RIGHT_EDGE_FRAC = 0.46  # a line starting after this fraction of page width is "right column"


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float
    boxes: list[Box] = field(default_factory=list)
    kind: str = ""  # "LEFT" / "RIGHT" / "FULL", filled in by classify_lines
    page: int = 0
    underlined_words: frozenset = field(default_factory=frozenset)  # plain-text words detected as underlined; never mutates .text


def _cluster_rows(boxes: list[Box]) -> list[list[Box]]:
    """Group boxes into row-bands by vertical center proximity."""
    boxes = sorted(boxes, key=lambda b: (b.y0 + b.y1) / 2)
    rows: list[list[Box]] = []
    for b in boxes:
        yc = (b.y0 + b.y1) / 2
        placed = False
        for row in rows:
            row_yc = sum((r.y0 + r.y1) / 2 for r in row) / len(row)
            if abs(yc - row_yc) <= Y_OVERLAP_TOL:
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])
    return rows


MIN_FORCE_SPLIT_GAP = 12  # px: floor below which even a "new unit" text match doesn't force a split


def _looks_like_new_unit(text: str) -> bool:
    """True if `text` looks like the start of a new structural unit - a
    question's own leading number, an (a)-(d) option marker, or a
    Directions/Passage heading. Used (alongside a minimum gap - see
    _split_row_into_lines) to force a line-split below the normal 45px
    threshold: a booklet's column gutter can be narrower than that
    (confirmed: as little as ~21px) - relying on gap size alone there let
    a right-column question's real start get silently absorbed into the
    previous (left-column) question's trailing text instead of ever
    being recognized as a new question, corrupting both.

    QUESTION_START_RE alone is too broad to trust on its own regardless of
    gap - it also matches an ordinary decimal number ("0.1 N", "3.5 km"),
    extremely common in this booklet's physics/chemistry content. The
    minimum-gap requirement is what keeps that safe: a mid-sentence
    decimal is preceded by normal word-spacing (a few px), while a
    genuine new question/option/heading starting at a narrow column
    gutter still has a real, if narrow, gap in front of it."""
    text = text.strip()
    return bool(QUESTION_START_RE.match(text) or OPTION_RE.match(text) or DIRECTIONS_RE.match(text) or PASSAGE_RE.match(text))


def _looks_like_bare_question_number(text: str, next_text: str) -> bool:
    """True if `text` is a bare 1-3 digit number (no trailing punctuation -
    OCR sometimes drops it entirely, e.g. "54" instead of "54.") AND the
    box immediately following it looks like the start of an ordinary
    capitalized word (QUESTION_START_LOOSE_RE's lookahead), not a unit
    abbreviation glued to a numeric value ("10 N", "50 kg"). Checked
    separately from _looks_like_new_unit because a bare number alone is
    far more ambiguous - it only earns a forced split when what follows it
    also looks like a fresh sentence starting."""
    if not text.isdigit() or not (1 <= len(text) <= 3):
        return False
    return bool(QUESTION_START_LOOSE_RE.match(f"{text} {next_text.strip()}"))


def _split_row_into_lines(row: list[Box]) -> list[list[Box]]:
    """Within one row-band, split into separate lines when there's a big
    x-gap (i.e. distinct columns sharing the same vertical position), OR
    when there's at least a modest gap (MIN_FORCE_SPLIT_GAP) AND the next
    box's text looks like the start of a new structural unit
    (_looks_like_new_unit, or _looks_like_bare_question_number with its
    own successor box) - catching column gutters narrower than the normal
    45px threshold without also firing on a decimal number sitting a
    couple pixels after the previous word in ordinary prose."""
    row = sorted(row, key=lambda b: b.x0)
    lines: list[list[Box]] = [[row[0]]]
    for i in range(1, len(row)):
        b = row[i]
        prev = lines[-1][-1]
        gap = b.x0 - prev.x1
        next_text = row[i + 1].text if i + 1 < len(row) else ""
        force = gap > MIN_FORCE_SPLIT_GAP and (
            _looks_like_new_unit(b.text) or _looks_like_bare_question_number(b.text, next_text)
        )
        if gap > X_GAP_SPLIT or force:
            lines.append([b])
        else:
            lines[-1].append(b)
    return lines


def build_lines(boxes: list[Box], page: int = 0) -> list[Line]:
    if not boxes:
        return []
    lines = []
    for row in _cluster_rows(boxes):
        # Shared by every segment split out of THIS row-band (rather than
        # each segment's own min-y0), so a y0-based sort/tiebreak elsewhere
        # (order_page's column buffers sort purely by y0, no x0 tiebreak)
        # can't invert two segments' intended left-to-right order over a
        # couple px of noise between their individual bounding boxes -
        # confirmed as a real bug once _split_row_into_lines started
        # splitting a question's own number from its immediately-following
        # option text (e.g. "38." from "(a) The teacher...") within one
        # row-band: their near-identical but not-quite-equal y0 values
        # could sort the option text before the number that starts it.
        row_y0 = min(b.y0 for b in row)
        for seg in _split_row_into_lines(row):
            seg = sorted(seg, key=lambda b: b.x0)
            x0 = min(b.x0 for b in seg)
            x1 = max(b.x1 for b in seg)
            y1 = max(b.y1 for b in seg)
            text = " ".join(b.text for b in seg)
            conf = sum(b.conf for b in seg) / len(seg)
            lines.append(Line(x0, row_y0, x1, y1, text, conf, boxes=seg, page=page))
    return sorted(lines, key=lambda l: (l.y0, l.x0))


def classify_lines(lines: list[Line], page_width: float) -> None:
    """Fill in .kind on each line in place."""
    for line in lines:
        width_frac = (line.x1 - line.x0) / page_width
        if width_frac > FULL_WIDTH_FRAC:
            line.kind = "FULL"
        elif line.x0 >= RIGHT_EDGE_FRAC * page_width:
            # Checked before the LEFT test: a narrow line that starts in the
            # right column (e.g. a lone question number like "4." at ~49% of
            # page width, seen on a booklet whose gutter sits left of centre)
            # also ends before LEFT_EDGE_FRAC, and testing LEFT first filed
            # it under the left column.
            line.kind = "RIGHT"
        elif line.x1 <= LEFT_EDGE_FRAC * page_width:
            line.kind = "LEFT"
        else:
            # centered but not wide enough to be "full" by area -> treat as
            # a standalone heading, safest to emit on its own
            line.kind = "FULL"


def order_page(boxes: list[Box], page_width: float, page: int = 0) -> list[Line]:
    """Full pipeline: boxes -> lines -> classified -> column-major reading order."""
    lines = build_lines(boxes, page=page)
    classify_lines(lines, page_width)
    lines.sort(key=lambda l: (l.y0, l.x0))

    ordered: list[Line] = []
    left_buf: list[Line] = []
    right_buf: list[Line] = []

    def flush_columns():
        left_buf.sort(key=lambda l: l.y0)
        right_buf.sort(key=lambda l: l.y0)
        ordered.extend(left_buf)
        ordered.extend(right_buf)
        left_buf.clear()
        right_buf.clear()

    for line in lines:
        if line.kind == "FULL":
            flush_columns()
            ordered.append(line)
        elif line.kind == "LEFT":
            left_buf.append(line)
        else:  # RIGHT
            right_buf.append(line)
    flush_columns()
    return ordered
