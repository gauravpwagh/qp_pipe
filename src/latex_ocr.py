"""Image -> LaTeX conversion for the web app's LaTeX Scratchpad tab, via
pix2tex (LaTeX-OCR) running locally - no external API, same offline
philosophy as the EasyOCR pipeline. The model is loaded lazily on first
use (it downloads its weights on its own first run) and cached
process-wide, since construction takes a few seconds.
"""

import threading

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from pix2tex.cli import LatexOCR

                _model = LatexOCR()
    return _model


def image_to_latex(pil_image) -> str:
    model = _get_model()
    return model(pil_image)
