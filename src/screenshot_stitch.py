"""Best-effort auto-processing for the LaTeX Scratchpad's "whole screenshot"
mode: split a pasted image into text lines (via EasyOCR's own line/word
boxes, clustered the same way the exam-page pipeline does), guess which
lines are a math formula vs. plain prose with a heuristic (no trained
layout-detection model here - see README limitations), OCR the text lines
normally and run pix2tex only on the formula-classified ones, and hand
back a top-to-bottom list the web app stitches into one HTML block.

This is deliberately a heuristic, not a solved layout-analysis problem -
callers should expect to eyeball/correct the classification, same as any
other OCR output in this project.
"""

import numpy as np
from PIL import Image

from .latex_ocr import image_to_latex
from .ocr import ocr_image
from .reading_order import build_lines

_MATH_CHARS = set("=+-*/^_\\%<>≤≥∑∫√±×÷")
_CROP_PAD = 4


def _looks_like_formula(text: str, x0: float, x1: float, page_width: float) -> bool:
    text = text.strip()
    if not text or "=" not in text:
        return False
    math_count = sum(1 for c in text if c in _MATH_CHARS)
    digit_count = sum(1 for c in text if c.isdigit())
    symbol_density = (math_count + digit_count) / max(1, len(text))
    alpha_words = [w for w in text.split() if w.isalpha() and len(w) > 2]
    left_margin = x0
    right_margin = page_width - x1
    centered = left_margin > 0.05 * page_width and abs(left_margin - right_margin) < 0.18 * page_width
    return symbol_density > 0.25 and len(alpha_words) <= 2 and (centered or symbol_density > 0.4)


def analyze_screenshot(image: Image.Image) -> list[dict]:
    """Returns per-line dicts, top-to-bottom:
    {kind: "text"|"formula", text, latex (formula only), bbox: [x0,y0,x1,y1]}
    """
    rgb = image.convert("RGB")
    width, height = rgb.size
    boxes = ocr_image(np.array(rgb))
    lines = build_lines(boxes)
    lines.sort(key=lambda l: l.y0)

    results = []
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        # Plain float(), not the numpy scalar types EasyOCR's box coordinates
        # can come back as - those aren't JSON-serializable and would blow up
        # the jsonify() call in the route with an opaque 500.
        bbox = [
            float(max(0, line.x0 - _CROP_PAD)),
            float(max(0, line.y0 - _CROP_PAD)),
            float(min(width, line.x1 + _CROP_PAD)),
            float(min(height, line.y1 + _CROP_PAD)),
        ]
        entry = {"kind": "text", "text": text, "bbox": bbox}
        if _looks_like_formula(text, line.x0, line.x1, width):
            crop = rgb.crop(tuple(int(v) for v in bbox))
            try:
                entry["latex"] = image_to_latex(crop)
                entry["kind"] = "formula"
            except Exception as e:
                entry["error"] = str(e)  # fall back to the plain OCR text
        results.append(entry)
    return results
