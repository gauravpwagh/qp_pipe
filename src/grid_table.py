"""Reconstruct a ruled ("all borders drawn") table inside a question crop.

The ruling lines are found with OpenCV - long horizontal and vertical strokes
survive a morphological opening that erases text - and their positions give
the row/column boundaries. Where a separator between two neighbouring cells
is missing, those cells are one merged cell (rowspan / colspan). The table is
then OCR'd once and each word assigned to the cell holding its centre - words
read in context come out far better than words read in isolated, cropped cells
- and only the few words whose box crosses a drawn border (OCR joins
neighbouring cells' words across a narrow border) are re-read one cell-segment
at a time. This is what fixes a bordered table that used to come out as a
jumbled stem: reading it as running lines interleaves the wrapped cells.

Result shape (stored as question["table"] for question_type "grid_table"):
    {"header": bool, "rows": [[{"html": str, "rs": int, "cs": int}, ...], ...]}
where each row lists only the cells that START in it, left to right, like the
rows of an HTML table.
"""

import html
import re

import cv2
import numpy as np

from .ocr import Box, ocr_image
from .reading_order import build_lines

LINE_MERGE_TOL = 8  # px: line-mask pieces closer than this are one ruling line
SEPARATOR_PRESENT_FRAC = 0.5  # a cell border counts as drawn if this much of it has ink
CELL_INSET = 4  # px inside the ruling lines, when re-reading a cell on its own
MIN_CELL_INK = 0.004  # a cell with less ink than this is treated as empty (no OCR)


def _cluster(items: list[dict], tol: float) -> list[dict]:
    """Group line-mask pieces at (nearly) the same coordinate. Each item:
    {"pos", "start", "end"}; returns [{"pos", "start", "end", "length"}]."""
    items = sorted(items, key=lambda it: it["pos"])
    groups: list[list[dict]] = []
    for it in items:
        if groups and abs(it["pos"] - groups[-1][-1]["pos"]) <= tol:
            groups[-1].append(it)
        else:
            groups.append([it])
    clusters = []
    for g in groups:
        total = sum(it["end"] - it["start"] for it in g)
        clusters.append(
            {
                "pos": sum(it["pos"] * (it["end"] - it["start"]) for it in g) / total,
                "start": min(it["start"] for it in g),
                "end": max(it["end"] for it in g),
                "length": total,
            }
        )
    return clusters


def _pieces(mask: np.ndarray, horizontal: bool, min_len: int, max_thick: int) -> list[dict]:
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    pieces = []
    for i in range(1, count):
        x, y, w, h, _area = stats[i]
        length, thick = (w, h) if horizontal else (h, w)
        if length < min_len or thick > max_thick:
            continue
        cx, cy = centroids[i]
        if horizontal:
            pieces.append({"pos": cy, "start": x, "end": x + w})
        else:
            pieces.append({"pos": cx, "start": y, "end": y + h})
    return pieces


def estimate_skew(gray: np.ndarray, max_angle: float = 4.0) -> float:
    """The rotation (degrees, cv2 convention) that best straightens the crop:
    the angle at which the ink's row-by-row totals are spikiest, i.e. text
    baselines and ruling lines all line up. A photocopy that is even a
    degree or two off otherwise splits one ruling line into a staircase."""
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    ink = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    h, w = ink.shape
    centre = (w / 2, h / 2)

    def score(angle: float) -> float:
        matrix = cv2.getRotationMatrix2D(centre, angle, 1.0)
        rotated = cv2.warpAffine(ink, matrix, (w, h), flags=cv2.INTER_NEAREST)
        return float(np.sum(np.diff(rotated.sum(axis=1, dtype=np.float64)) ** 2))

    coarse = max(np.arange(-max_angle, max_angle + 0.01, 0.5), key=score)
    return float(max(np.arange(coarse - 0.5, coarse + 0.51, 0.1), key=score))


def deskew(gray: np.ndarray) -> np.ndarray:
    angle = estimate_skew(gray)
    if abs(angle) < 0.2:
        return gray
    h, w = gray.shape
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(
        gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderValue=int(np.percentile(gray, 90))
    )


def find_grid(gray: np.ndarray) -> dict | None:
    """Locate the ruling lines. Returns {"xs", "ys", "hmask", "vmask", "bbox"}
    (xs/ys: sorted line positions in pixels) or None if there's no grid."""
    h, w = gray.shape
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    ink = cv2.dilate(ink, np.ones((3, 3), np.uint8))  # thicken, so a slightly tilted line still opens

    min_h = max(40, int(w * 0.08))
    min_v = max(40, int(h * 0.04))
    hmask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (min_h, 1)))
    vmask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_v)))

    hclusters = _cluster(_pieces(hmask, True, min_h, 30), LINE_MERGE_TOL)
    vclusters = _cluster(_pieces(vmask, False, min_v, 30), LINE_MERGE_TOL)
    if not hclusters or not vclusters:
        return None
    # drop short strays (an underline, a letter stroke): a real ruling line
    # covers at least half of what the longest one does
    hclusters = [c for c in hclusters if c["length"] >= 0.5 * max(x["length"] for x in hclusters)]
    vclusters = [c for c in vclusters if c["length"] >= 0.5 * max(x["length"] for x in vclusters)]
    # ...and horizontals must sit within the verticals' span, and vice versa
    v_lo, v_hi = min(c["start"] for c in vclusters), max(c["end"] for c in vclusters)
    h_lo, h_hi = min(c["start"] for c in hclusters), max(c["end"] for c in hclusters)
    tol = LINE_MERGE_TOL * 2
    hclusters = [c for c in hclusters if v_lo - tol <= c["pos"] <= v_hi + tol]
    vclusters = [c for c in vclusters if h_lo - tol <= c["pos"] <= h_hi + tol]

    ys = sorted(int(round(c["pos"])) for c in hclusters)
    xs = sorted(int(round(c["pos"])) for c in vclusters)
    if len(ys) < 2 or len(xs) < 2:
        return None
    return {"xs": xs, "ys": ys, "hmask": hmask, "vmask": vmask, "bbox": (xs[0], ys[0], xs[-1], ys[-1])}


def _band_has_ink(mask: np.ndarray, y0: int, y1: int, x0: int, x1: int, along_axis: int) -> bool:
    """Whether the strip has ink along at least SEPARATOR_PRESENT_FRAC of its
    length. `along_axis`: 0 = the border runs down the rows (a vertical one)."""
    y0, x0 = max(0, y0), max(0, x0)
    region = mask[y0:y1, x0:x1]
    if region.size == 0:
        return True  # too small to tell - assume drawn
    if along_axis == 0:
        return (region.max(axis=1) > 0).mean() >= SEPARATOR_PRESENT_FRAC
    return (region.max(axis=0) > 0).mean() >= SEPARATOR_PRESENT_FRAC


def _cell_regions(grid: dict) -> tuple[list[dict], list[str]]:
    """Merge grid cells whose separating border isn't drawn into spanning
    regions. Returns ([{"r0","c0","rs","cs"}], problems)."""
    xs, ys, hmask, vmask = grid["xs"], grid["ys"], grid["hmask"], grid["vmask"]
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    parent = {(i, j): (i, j) for i in range(n_rows) for j in range(n_cols)}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for i in range(n_rows):
        for j in range(1, n_cols):
            if not _band_has_ink(vmask, ys[i] + 4, ys[i + 1] - 3, xs[j] - 6, xs[j] + 7, 0):
                union((i, j - 1), (i, j))
    for i in range(1, n_rows):
        for j in range(n_cols):
            if not _band_has_ink(hmask, ys[i] - 6, ys[i] + 7, xs[j] + 4, xs[j + 1] - 3, 1):
                union((i - 1, j), (i, j))

    groups: dict[tuple, list] = {}
    for cell in parent:
        groups.setdefault(find(cell), []).append(cell)

    regions, problems = [], []
    for cells in groups.values():
        rows = [c[0] for c in cells]
        cols = [c[1] for c in cells]
        r0, c0 = min(rows), min(cols)
        rs, cs = max(rows) - r0 + 1, max(cols) - c0 + 1
        if rs * cs == len(cells):
            regions.append({"r0": r0, "c0": c0, "rs": rs, "cs": cs})
        else:
            # not a rectangle - a broken/noisy line; keep the cells separate
            problems.append(f"cells around row {r0 + 1}, column {c0 + 1} looked merged in an irregular shape - check them")
            regions.extend({"r0": r, "c0": c, "rs": 1, "cs": 1} for r, c in cells)
    return sorted(regions, key=lambda r: (r["r0"], r["c0"])), problems


def _bucket(lines: list[int], value: float) -> int | None:
    for k in range(len(lines) - 1):
        if lines[k] <= value < lines[k + 1]:
            return k
    return None


def _clean(text: str) -> str:
    """Drop the stray "|" / "[" / "]" a ruling line leaves at a word's edge."""
    return re.sub(r"^[|\[\]!\s]+|[|\[\]!\s]+$", "", text)


def _read_rect(gray: np.ndarray, rect: tuple, scale: int = 3) -> str:
    """OCR one rectangle on its own, enlarged - used for the few words that
    cross a ruling line and for cells the whole-table read missed."""
    x0, y0, x1, y1 = (int(v) for v in rect)
    crop = gray[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)]
    if crop.size == 0 or min(crop.shape) < 4:
        return ""
    crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(crop, 12, 12, 12, 12, cv2.BORDER_REPLICATE)
    lines = build_lines(ocr_image(padded))
    return " ".join(line.text.strip() for line in lines if line.text.strip())


def read_grid(gray: np.ndarray, grid: dict) -> tuple[dict, list[str]]:
    """Fill the grid with text. The whole table is read once (words read in
    context come out far better than words read in isolated, cropped cells)
    and each word is assigned to the cell holding its centre; a word whose
    box crosses a drawn border (OCR joins neighbouring cells' words across a
    narrow border) is re-read one cell-segment at a time. Returns
    (table, problems)."""
    xs, ys = grid["xs"], grid["ys"]
    regions, problems = _cell_regions(grid)
    n_cols = len(xs) - 1

    owner: dict[tuple[int, int], int] = {}
    for idx, reg in enumerate(regions):
        for r in range(reg["r0"], reg["r0"] + reg["rs"]):
            for c in range(reg["c0"], reg["c0"] + reg["cs"]):
                owner[(r, c)] = idx

    def owner_at(x: float, y: float) -> int | None:
        r, c = _bucket(ys, y), _bucket(xs, x)
        return owner.get((r, c)) if r is not None and c is not None else None

    ox0, oy0 = max(0, xs[0] - 4), max(0, ys[0] - 4)
    table_img = gray[oy0 : ys[-1] + 4, ox0 : xs[-1] + 4]
    assigned: dict[int, list[Box]] = {idx: [] for idx in range(len(regions))}
    for raw in ocr_image(table_img):
        text = _clean(raw.text)
        if not text:
            continue
        b = Box(raw.x0 + ox0, raw.y0 + oy0, raw.x1 + ox0, raw.y1 + oy0, text, raw.conf)
        cy = (b.y0 + b.y1) / 2
        left, right = owner_at(b.x0 + 3, cy), owner_at(b.x1 - 3, cy)
        if left == right or left is None or right is None:
            idx = owner_at((b.x0 + b.x1) / 2, cy)
            if idx is not None:
                assigned[idx].append(b)
            continue
        row = _bucket(ys, cy)
        if row is None:
            continue
        # consecutive columns owned by the same region are one segment
        segments: list[list] = []
        for c in range(n_cols):
            seg0, seg1 = max(b.x0, xs[c]), min(b.x1, xs[c + 1])
            if seg1 - seg0 < 8 or (row, c) not in owner:
                continue
            if segments and segments[-1][0] == owner[(row, c)] and segments[-1][2] >= xs[c] - 1:
                segments[-1][2] = seg1
            else:
                segments.append([owner[(row, c)], seg0, seg1])
        for idx, seg0, seg1 in segments:
            piece = _clean(_read_rect(gray, (seg0, b.y0 - 2, seg1, b.y1 + 2)))
            if piece:
                assigned[idx].append(Box(seg0, b.y0, seg1, b.y1, piece, b.conf))

    background = int(np.percentile(gray, 90))
    unread = 0
    for idx, reg in enumerate(regions):
        if assigned[idx]:
            continue
        rect = (
            xs[reg["c0"]] + CELL_INSET,
            ys[reg["r0"]] + CELL_INSET,
            xs[reg["c0"] + reg["cs"]] - CELL_INSET,
            ys[reg["r0"] + reg["rs"]] - CELL_INSET,
        )
        cell = gray[rect[1] : rect[3], rect[0] : rect[2]]
        if cell.size == 0 or (cell < background - 60).mean() < MIN_CELL_INK:
            continue  # genuinely empty
        text = _clean(_read_rect(gray, rect))
        if text:
            assigned[idx].append(Box(rect[0], rect[1], rect[2], rect[3], text, 0.0))
        else:
            unread += 1

    rows: list[list[dict]] = [[] for _ in range(len(ys) - 1)]
    for idx, reg in enumerate(regions):
        lines = build_lines(assigned[idx])
        text = " ".join(line.text.strip() for line in lines if line.text.strip())
        rows[reg["r0"]].append({"html": html.escape(text, quote=False), "rs": reg["rs"], "cs": reg["cs"]})
    if unread:
        problems.append(f"{unread} cell(s) had content OCR couldn't read - fill them in by hand")
    return {"header": True, "rows": rows}, problems
