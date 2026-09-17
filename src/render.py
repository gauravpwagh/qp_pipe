"""Render PDF pages to PNG images at OCR-quality resolution."""

from pathlib import Path

import pypdfium2 as pdfium

DEFAULT_SCALE = 3.0  # ~216 DPI at 72 base DPI; good balance of quality vs OCR speed


def render_pdf(pdf_path: str | Path, out_dir: str | Path, scale: float = DEFAULT_SCALE) -> list[Path]:
    """Render every page of a PDF to `out_dir/page_NNN.png`. Returns sorted list of paths."""
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        paths = []
        for i, page in enumerate(pdf):
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            out_path = out_dir / f"page_{i + 1:03d}.png"
            pil_image.save(out_path)
            paths.append(out_path)
        return paths
    finally:
        pdf.close()


if __name__ == "__main__":
    import sys

    render_pdf(sys.argv[1], sys.argv[2])
