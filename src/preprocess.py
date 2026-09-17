"""Image preprocessing: grayscale, deskew, denoise, binarize a page image."""

import cv2
import numpy as np


def load_gray(path_or_array) -> np.ndarray:
    if isinstance(path_or_array, np.ndarray):
        img = path_or_array
        if img.ndim == 3:
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img
    img = cv2.imread(str(path_or_array), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Could not read image: {path_or_array}")
    return img


def estimate_skew_angle(gray: np.ndarray) -> float:
    """Estimate skew angle (degrees) via minAreaRect over thresholded ink pixels."""
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thresh > 0))
    if coords.shape[0] < 100:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    # cv2.minAreaRect returns angle in (-90, 0]; normalize to a small rotation
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    # Only trust small corrective angles; large values usually mean a bad rect fit
    if abs(angle) > 15:
        return 0.0
    return angle


def deskew(gray: np.ndarray, angle: float | None = None) -> np.ndarray:
    if angle is None:
        angle = estimate_skew_angle(gray)
    if abs(angle) < 0.1:
        return gray
    h, w = gray.shape
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def denoise(gray: np.ndarray) -> np.ndarray:
    return cv2.fastNlMeansDenoising(gray, h=10, templateWindowSize=7, searchWindowSize=21)


def binarize(gray: np.ndarray) -> np.ndarray:
    """Adaptive threshold to handle uneven photocopy lighting/speckling."""
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
    )


def preprocess_page(path_or_array) -> dict:
    """Returns dict with 'gray' (deskewed, denoised grayscale) and 'binary' images.

    EasyOCR generally performs better on denoised grayscale than on hard binary,
    so 'gray' is the primary image fed to OCR; 'binary' is used for layout
    (column/gutter) detection where a clean ink mask matters more than OCR fidelity.
    """
    gray = load_gray(path_or_array)
    angle = estimate_skew_angle(gray)
    gray = deskew(gray, angle)
    gray = denoise(gray)
    binary = binarize(gray)
    return {"gray": gray, "binary": binary, "skew_angle": angle}
