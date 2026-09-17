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


def _split_row_into_lines(row: list[Box]) -> list[list[Box]]:
    """Within one row-band, split into separate lines when there's a big x-gap
    (i.e. distinct columns sharing the same vertical position)."""
    row = sorted(row, key=lambda b: b.x0)
    lines: list[list[Box]] = [[row[0]]]
    for b in row[1:]:
        prev = lines[-1][-1]
        gap = b.x0 - prev.x1
        if gap > X_GAP_SPLIT:
            lines.append([b])
        else:
            lines[-1].append(b)
    return lines


def build_lines(boxes: list[Box], page: int = 0) -> list[Line]:
    if not boxes:
        return []
    lines = []
    for row in _cluster_rows(boxes):
        for seg in _split_row_into_lines(row):
            seg = sorted(seg, key=lambda b: b.x0)
            x0 = min(b.x0 for b in seg)
            x1 = max(b.x1 for b in seg)
            y0 = min(b.y0 for b in seg)
            y1 = max(b.y1 for b in seg)
            text = " ".join(b.text for b in seg)
            conf = sum(b.conf for b in seg) / len(seg)
            lines.append(Line(x0, y0, x1, y1, text, conf, boxes=seg, page=page))
    return sorted(lines, key=lambda l: (l.y0, l.x0))


def classify_lines(lines: list[Line], page_width: float) -> None:
    """Fill in .kind on each line in place."""
    for line in lines:
        width_frac = (line.x1 - line.x0) / page_width
        if width_frac > FULL_WIDTH_FRAC:
            line.kind = "FULL"
        elif line.x1 <= LEFT_EDGE_FRAC * page_width:
            line.kind = "LEFT"
        elif line.x0 >= RIGHT_EDGE_FRAC * page_width:
            line.kind = "RIGHT"
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
