"""Per-question bounding boxes, computed from each question's start position
plus the raw OCR boxes on its page - used to crop a source-image thumbnail
per question, and to hand match_list.py just the boxes for one question.
"""

from dataclasses import dataclass

from .ocr import Box
from .parse_questions import Question
from .reading_order import LEFT_EDGE_FRAC, RIGHT_EDGE_FRAC

PAD = 6  # px of breathing room around the tightest box union


@dataclass
class BBox:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


def _column_x_range(kind: str, width: float) -> tuple[float, float]:
    if kind == "LEFT":
        return 0.0, LEFT_EDGE_FRAC * width
    if kind == "RIGHT":
        return RIGHT_EDGE_FRAC * width, width
    return 0.0, width  # "FULL" or unknown - full page width


def compute_bboxes(
    questions: list[Question],
    page_boxes: dict[int, list[Box]],
    page_dims: dict[int, tuple[float, float]],
) -> dict[int, list[BBox]]:
    """Returns {q_number: [BBox, ...]} - built directly from the bounding
    rects of the OCR lines that actually became this question's text
    (Question.regions, computed in parse_questions.py), grouped by (page,
    kind). Normally one region; a question whose content overflows from the
    bottom of one column to the top of the next on the same page (seen in
    practice on a dense 2-column layout) yields two - callers should render
    /search across all of them rather than just the first, or a chunk of
    that question's own text and image would silently go missing."""
    bboxes: dict[int, list[BBox]] = {}
    for q in questions:
        regions = []
        for r in q.regions:
            w, h = page_dims.get(r["page"], (0, 0))
            x0 = max(0.0, r["x0"] - PAD)
            y0 = max(0.0, r["y0"] - PAD)
            x1 = min(w, r["x1"] + PAD) if w else r["x1"] + PAD
            y1 = min(h, r["y1"] + PAD) if h else r["y1"] + PAD
            regions.append(BBox(r["page"], x0, y0, x1, y1))
        if not regions:
            # defensive fallback - shouldn't happen (every question has at
            # least a stem fragment) but don't leave a question imageless.
            w, h = page_dims.get(q.page, (0, 0))
            col_x0, col_x1 = _column_x_range(q.start_kind, w)
            regions = [BBox(q.page, col_x0, max(0.0, q.start_y - PAD), col_x1, h)]
        # Deliberately NOT sorted by y0: q.regions is already in true reading
        # order (fragments were grouped in the order first encountered while
        # walking the parsed text stream), and a bottom-of-left-column ->
        # top-of-right-column overflow has a LATER region with a SMALLER y0
        # than the first - sorting by y0 would silently reverse it.
        bboxes[q.q_number] = regions
    return bboxes
