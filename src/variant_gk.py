"""CDS General Knowledge variant: a bilingual booklet where every content
item and rough-work page is printed twice - once in Hindi, once in
English - alternating by page. Confirmed by direct inspection of
QP-CDSE-II-26-GENERAL-KNOWLEDGE-140926.pdf (56 pages): page 1 is Hindi
instructions, pages 2/4/6/... are Hindi content, pages 3/5/7/... are the
matching English content (Q1-120), the rough-work pages after that also
alternate Hindi/English, and the final page is an English instructions
back-cover.

Our OCR reader is English-only, so Hindi pages are not just unwanted but
unreadable - we skip OCR on them entirely rather than paying for a garbled
pass, which also roughly halves total processing time for this variant.
"""

from .pipeline import DO_NOT_OPEN_RE, INSTRUCTIONS_RE, ROUGH_WORK_RE, run_pipeline_json


def _should_ocr_page(page_num: int, total_pages: int) -> bool:
    # Odd pages carry the English text (content and rough-work alike);
    # always check page 1 and the last page too, since the front/back
    # instructions covers are the one place this booklet breaks strict
    # odd/even alternation (front is Hindi on page 1, an odd page; back is
    # English and may land on either parity depending on page count).
    return page_num % 2 == 1 or page_num == 1 or page_num == total_pages


def _classify_page(page_num: int, all_text: str) -> str:
    if ROUGH_WORK_RE.search(all_text):
        return "rough_work"
    if INSTRUCTIONS_RE.search(all_text) and DO_NOT_OPEN_RE.search(all_text):
        return "instructions"
    if page_num == 1:
        # Hindi instructions page - OCR (English reader, on Hindi text) is
        # gibberish and won't match the English instructions phrases above.
        return "instructions"
    return "content"


def run_pipeline_json_gk(
    pdf_path: str,
    paper_dir: str,
    variant_id: str,
    source_filename: str | None = None,
    image_dir: str | None = None,
    progress_cb=None,
) -> dict:
    return run_pipeline_json(
        pdf_path,
        paper_dir,
        variant_id,
        source_filename=source_filename,
        image_dir=image_dir,
        progress_cb=progress_cb,
        should_ocr_page=_should_ocr_page,
        classify_page_fn=_classify_page,
    )
