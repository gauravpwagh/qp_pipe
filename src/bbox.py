"""Per-question bounding boxes, computed from each question's start position
plus the raw OCR boxes on its page - used to crop a source-image thumbnail
per question, and to hand match_list.py just the boxes for one question.
"""

from dataclasses import dataclass

from .ocr import Box
from .parse_questions import FOOTER_Y_FRAC, Question
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
) -> dict[int, BBox]:
    """Returns {q_number: BBox}. Each question's vertical extent runs from its
    own start_y down to the next question's start_y *in the same column on the
    same page* (or the page's footer margin if it's the last one there)."""
    groups: dict[tuple[int, str], list[Question]] = {}
    for q in questions:
        groups.setdefault((q.page, q.start_kind), []).append(q)

    end_y_by_qnum: dict[int, float] = {}
    for (page, _kind), qs in groups.items():
        qs.sort(key=lambda q: q.start_y)
        _, h = page_dims.get(page, (0, 0))
        bottom = FOOTER_Y_FRAC * h
        for i, q in enumerate(qs):
            end_y_by_qnum[q.q_number] = qs[i + 1].start_y if i + 1 < len(qs) else bottom

    bboxes: dict[int, BBox] = {}
    for q in questions:
        w, h = page_dims.get(q.page, (0, 0))
        col_x0, col_x1 = _column_x_range(q.start_kind, w)
        y0 = max(0.0, q.start_y - PAD)
        y1 = min(h, end_y_by_qnum.get(q.q_number, h) + PAD)

        boxes = page_boxes.get(q.page, [])
        relevant = [
            b
            for b in boxes
            if col_x0 - PAD <= (b.x0 + b.x1) / 2 <= col_x1 + PAD and y0 <= (b.y0 + b.y1) / 2 <= y1
        ]
        if relevant:
            bx0 = max(0.0, min(b.x0 for b in relevant) - PAD)
            bx1 = min(w, max(b.x1 for b in relevant) + PAD)
            by0 = max(0.0, min(b.y0 for b in relevant) - PAD)
            by1 = min(h, max(b.y1 for b in relevant) + PAD)
            bboxes[q.q_number] = BBox(q.page, bx0, by0, bx1, by1)
        else:
            bboxes[q.q_number] = BBox(q.page, col_x0, y0, col_x1, y1)
    return bboxes
