"""NDA & NA Examination - General Ability Test: a booklet with an
all-English "Part A" followed by a bilingual "Part B" where every item is
printed twice - once in Hindi, once in English - alternating by page (Hindi
first, then its English match). Confirmed by direct inspection of
QP-NDANA-II-26-GENERAL-ABILITY-TEST-140926.pdf (52 pages): page 1 is Hindi
instructions, pages 2-7 are Part A (Q1-50, English only, two-column, every
page processed), pages 8-47 are Part B (Q51-150; 8/10/12/... Hindi,
9/11/13/... English), pages 48-51 are rough-work, and page 52 is an English
instructions back-cover. (These page numbers are this printing's; the
caller supplies them per job - see below.)

Unlike cds_gk_v1 (bilingual from page 2 all the way through, a fixed
rule), the Part A/Part B boundary here is a booklet-specific page number
that isn't necessarily the same from one year's printing to the next -
so the caller supplies it (see webapp/variants.py's "fields" and
webapp/app.py's /api/process, which surfaces it as Process-tab inputs for
this variant) rather than it being hardcoded.
"""

from .pipeline import DO_NOT_OPEN_RE, INSTRUCTIONS_RE, ROUGH_WORK_RE, run_pipeline_json


def _should_ocr_page(page_num: int, total_pages: int, part_a_last_page: int, part_b_last_page: int) -> bool:
    # Part A, and everything after Part B (rough-work, back-cover
    # instructions) - always OCR. classify_page_fn below needs the text to
    # check for rough-work/instructions keywords, same reasoning as
    # cds_gk_v1's front/back covers.
    if page_num <= part_a_last_page or page_num > part_b_last_page:
        return True
    # Part B: alternating Hindi/English, Hindi first - only the English
    # (even offset from the Part A/B boundary) pages are worth OCR'ing.
    offset = page_num - part_a_last_page
    return offset % 2 == 0


def _classify_page(page_num: int, all_text: str) -> str:
    if ROUGH_WORK_RE.search(all_text):
        return "rough_work"
    if INSTRUCTIONS_RE.search(all_text) and DO_NOT_OPEN_RE.search(all_text):
        return "instructions"
    if page_num == 1:
        # Hindi instructions page - an English OCR reader on Hindi text is
        # gibberish and won't match the English instructions phrases above.
        return "instructions"
    return "content"


def run_pipeline_json_ndana_gat(
    pdf_path: str,
    paper_dir: str,
    variant_id: str,
    part_a_last_page: int,
    part_b_last_page: int,
    source_filename: str | None = None,
    job_name: str | None = None,
    image_dir: str | None = None,
    progress_cb=None,
) -> dict:
    def should_ocr_page(page_num: int, total_pages: int) -> bool:
        return _should_ocr_page(page_num, total_pages, part_a_last_page, part_b_last_page)

    return run_pipeline_json(
        pdf_path,
        paper_dir,
        variant_id,
        source_filename=source_filename,
        job_name=job_name,
        image_dir=image_dir,
        progress_cb=progress_cb,
        should_ocr_page=should_ocr_page,
        classify_page_fn=_classify_page,
    )
