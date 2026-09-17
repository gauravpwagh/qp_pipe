"""EasyOCR wrapper: page image -> list of raw text boxes."""

from dataclasses import dataclass
from functools import lru_cache

import easyocr
import numpy as np


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float


@lru_cache(maxsize=1)
def get_reader() -> easyocr.Reader:
    return easyocr.Reader(["en"], gpu=False)


def ocr_image(image: np.ndarray) -> list[Box]:
    """Run EasyOCR on a full page (grayscale or BGR) image, return raw word/phrase boxes."""
    reader = get_reader()
    raw = reader.readtext(image, detail=1, paragraph=False)
    boxes = []
    for bbox, text, conf in raw:
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        boxes.append(Box(min(xs), min(ys), max(xs), max(ys), text, conf))
    return boxes
