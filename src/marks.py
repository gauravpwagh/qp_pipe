"""Manual formula / chemistry / diagram marks on a question's crop.

The user marks areas inside a question's cropped region(s) in the Review UI's
Edit-image modal. Formula and chemistry marks become inline LaTeX tokens in
the question's stem/options HTML (rendered client-side with KaTeX + mhchem);
diagram marks become inline <img> tags pointing at a PNG cropped from the
full-resolution page image.

A crop is a vertical stack of regions, each on its own page (a question or
instruction can run onto another page); image_regions records them:

    {"regions": [{"page": 3, "box": [x0, y0, x1, y1]}, ...],   # stacking order
     "marks":   [{"id", "type", "page", "box", "latex"?, "file"?}, ...]}

Marks are in *page* pixel coordinates like the regions, so re-opening the modal
restores them; a mark is translated to crop coordinates by locating the region
(same page) that contains it. An older job stores {"page", "boxes", "marks"}
(one page for everything) - normalize_image_regions reads both shapes.
"""

import html
import re

MARK_TYPES = ("formula", "chemistry", "diagram")
MARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
STACK_GAP = 10  # must match crop_and_stack_regions' separator


def normalize_image_regions(image_regions) -> dict | None:
    """Either stored shape -> {"regions": [{page, box}, ...], "marks": [...]},
    or None if there's nothing usable (never edited, and an older job that the
    pipeline didn't record regions for)."""
    if not isinstance(image_regions, dict):
        return None
    if isinstance(image_regions.get("regions"), list):
        regions = [
            {"page": r["page"], "box": r["box"]}
            for r in image_regions["regions"]
            if isinstance(r, dict) and isinstance(r.get("page"), int) and isinstance(r.get("box"), list)
        ]
        marks = [m for m in (image_regions.get("marks") or []) if isinstance(m, dict)]
    elif isinstance(image_regions.get("boxes"), list) and isinstance(image_regions.get("page"), int):
        page = image_regions["page"]
        regions = [{"page": page, "box": b} for b in image_regions["boxes"]]
        marks = [{**m, "page": m.get("page", page)} for m in (image_regions.get("marks") or []) if isinstance(m, dict)]
    else:
        return None
    return {"regions": regions, "marks": marks}


def _rounded(box):
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    return x0, y0, x1, y1


def crop_layout(regions: list) -> list[dict]:
    """Where each page-space region lands inside the stacked crop image, using
    the same rounding and gap as crop_and_stack_regions."""
    layout = []
    y_offset = 0
    for region in regions:
        x0, y0, x1, y1 = _rounded(region["box"])
        layout.append({"page": region["page"], "x0": x0, "y0": y0, "x1": x1, "y1": y1, "y_offset": y_offset})
        y_offset += (y1 - y0) + STACK_GAP
    return layout


def to_crop_box(mark: dict, regions: list) -> list[float] | None:
    """A mark's page-space box in the stacked crop's coordinates, or None if
    its centre isn't inside a region on the mark's own page."""
    mx0, my0, mx1, my1 = mark["box"]
    cx, cy = (mx0 + mx1) / 2, (my0 + my1) / 2
    for r in crop_layout(regions):
        if r["page"] == mark.get("page") and r["x0"] <= cx <= r["x1"] and r["y0"] <= cy <= r["y1"]:
            dx, dy = -r["x0"], r["y_offset"] - r["y0"]
            width, height = r["x1"] - r["x0"], r["y1"] - r["y0"] + r["y_offset"]
            return [
                max(0.0, mx0 + dx),
                max(0.0, my0 + dy),
                min(float(width), mx1 + dx),
                min(float(height), my1 + dy),
            ]
    return None


def validate_marks(marks, regions: list) -> tuple[list[dict], str | None]:
    """Returns (clean_marks, error). Each mark: {id, type, page, box, latex?}."""
    if not isinstance(marks, list):
        return [], "marks must be a list"
    only_page = regions[0]["page"] if regions and all(r["page"] == regions[0]["page"] for r in regions) else None
    clean = []
    seen = set()
    for m in marks:
        if not isinstance(m, dict):
            return [], "each mark must be an object"
        mid, mtype, box = m.get("id"), m.get("type"), m.get("box")
        page = m.get("page", only_page)
        if not isinstance(mid, str) or not MARK_ID_RE.match(mid) or mid in seen:
            return [], "each mark needs a unique id (letters, digits, - or _)"
        if mtype not in MARK_TYPES:
            return [], f"mark type must be one of {', '.join(MARK_TYPES)}"
        if not isinstance(page, int) or isinstance(page, bool):
            return [], "each mark needs the page it was drawn on"
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            return [], "each mark box must be [x0,y0,x1,y1]"
        box = [float(v) for v in box]
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            return [], "a mark is too small"
        if to_crop_box({"page": page, "box": box}, regions) is None:
            return [], "each mark must lie inside one of the question's crop regions, on its own page"
        entry = {"id": mid, "type": mtype, "page": page, "box": box}
        if mtype in ("formula", "chemistry"):
            latex = m.get("latex")
            if not isinstance(latex, str) or not latex.strip():
                return [], f"the {mtype} mark needs its LaTeX filled in"
            if len(latex) > 4000:
                return [], "LaTeX is too long"
            entry["latex"] = latex.strip()
        seen.add(mid)
        clean.append(entry)
    return clean, None


def token_html(mark: dict) -> str:
    """The inline HTML stored in the stem/option for one mark."""
    if mark["type"] == "diagram":
        return (
            f'<img class="diagram-token" src="{html.escape(mark["file"], quote=True)}" '
            f'data-mark="{mark["id"]}" alt="diagram">'
        )
    return (
        f'<span class="math-token" data-type="{mark["type"]}" data-mark="{mark["id"]}" '
        f'data-latex="{html.escape(mark["latex"], quote=True)}" contenteditable="false"></span>'
    )
