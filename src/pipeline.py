"""CLI orchestrator: PDF -> questions.csv + match_the_list.csv + qa_report.txt
(also: PDF -> paper.json + per-question image crops, for the web review UI)
"""

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from .bbox import compute_bboxes
from .instructions import extract_instructions
from .match_list import reconstruct as reconstruct_match_list
from .paired_table import detect as detect_paired_table, reconstruct as reconstruct_paired_table
from .ocr import Box, ocr_image
from .parse_questions import parse_document, Question
from .reading_order import order_page
from .render import render_pdf
from .underline import detect_underlined_words

ROUGH_WORK_RE = re.compile(r"space\s+for\s+rough\s+work", re.IGNORECASE)
INSTRUCTIONS_RE = re.compile(r"\binstructions\b", re.IGNORECASE)
DO_NOT_OPEN_RE = re.compile(r"do\s+not\s+open\s+this\s+test\s+booklet", re.IGNORECASE)

QUESTION_CSV_FIELDS = [
    "q_number", "page", "question_type", "section_directions",
    "passage_label", "passage_text", "question_stem",
    "option_a", "option_b", "option_c", "option_d",
    "has_underline", "ocr_confidence", "needs_review", "review_reason",
]


def classify_page(all_text: str) -> str:
    if ROUGH_WORK_RE.search(all_text):
        return "rough_work"
    if INSTRUCTIONS_RE.search(all_text) and DO_NOT_OPEN_RE.search(all_text):
        return "instructions"
    return "content"


def run_pipeline(pdf_path: str, out_dir: str, image_dir: str | None = None, verbose: bool = True) -> None:
    out_dir_p = Path(out_dir)
    out_dir_p.mkdir(parents=True, exist_ok=True)

    if image_dir:
        image_paths = sorted(Path(image_dir).glob("page_*.png"))
    else:
        img_dir = out_dir_p / "page_images"
        image_paths = render_pdf(pdf_path, img_dir)

    content_pages: dict[int, tuple[list, float]] = {}
    page_kinds = {}

    for path in image_paths:
        page_num = int(path.stem.split("_")[1])
        pil_img = Image.open(path).convert("L")
        # EasyOCR's own text detector handles this scan quality (slight skew,
        # photocopy speckling) better on the raw grayscale than after our
        # deskew/denoise preprocessing, which was empirically found to
        # fragment and misorder its text detections badly. Keep preprocess.py
        # available (e.g. for underline-strip detection) but skip it here.
        gray_proc = np.array(pil_img)

        boxes = ocr_image(gray_proc)
        all_text = " ".join(b.text for b in boxes)
        kind = classify_page(all_text)
        page_kinds[page_num] = kind

        if verbose:
            print(f"page {page_num:3d}: {kind:12s} ({len(boxes)} boxes)", file=sys.stderr)

        if kind != "content":
            continue

        w = gray_proc.shape[1]
        lines = order_page(boxes, w, page=page_num)
        for line in lines:
            line.underlined_words = detect_underlined_words(gray_proc, line)
        content_pages[page_num] = (lines, gray_proc.shape[0])

    questions, warnings = parse_document(content_pages)

    # has_underline propagation: a question has_underline if any of its raw
    # collected lines had an <u> marker. parse_document strips markers out of
    # plain concatenation logic implicitly kept in text (since it operates on
    # line.text which includes <u> tags) -- surface it explicitly per question.
    for q in questions:
        combined = q.question_stem + q.option_a + q.option_b + q.option_c + q.option_d
        q.has_underline = "<u>" in combined

    write_questions_csv(questions, out_dir_p / "questions.csv")
    write_match_list_csv(questions, out_dir_p / "match_the_list.csv")
    write_qa_report(questions, warnings, page_kinds, out_dir_p / "qa_report.txt")

    if verbose:
        print(f"\nWrote {len(questions)} questions to {out_dir_p / 'questions.csv'}", file=sys.stderr)
        for w_ in warnings:
            print(f"WARNING: {w_}", file=sys.stderr)


def run_pipeline_json(
    pdf_path: str,
    paper_dir: str,
    variant_id: str,
    source_filename: str | None = None,
    job_name: str | None = None,
    image_dir: str | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    should_ocr_page: Callable[[int, int], bool] | None = None,
    classify_page_fn: Callable[[int, str], str] | None = None,
) -> dict:
    """Like run_pipeline, but produces the web review UI's paper.json plus
    a cropped image per question, instead of CSVs. Reuses the same
    OCR/reading-order/parsing pipeline; the only new work here is keeping
    the raw per-page boxes around for bbox computation and match-the-list
    table reconstruction, and serializing everything to JSON.

    `should_ocr_page(page_num, total_pages)`, if given, lets a variant skip
    OCR entirely on pages it already knows aren't wanted (e.g. a bilingual
    booklet's non-English pages, which our English-only OCR reader can't
    usefully read anyway) - skipped pages are recorded as "skipped" and
    never enter parsing. `classify_page_fn(page_num, all_text)`, if given,
    overrides the default instructions/rough_work/content classification
    for pages that ARE ocr'd (e.g. to also key off page position).
    """
    paper_dir_p = Path(paper_dir)
    img_dir = paper_dir_p / "page_images"
    crops_dir = paper_dir_p / "images"
    crops_dir.mkdir(parents=True, exist_ok=True)

    if image_dir:
        image_paths = sorted(Path(image_dir).glob("page_*.png"))
    else:
        image_paths = render_pdf(pdf_path, img_dir)
    total_pages = len(image_paths)

    content_pages: dict[int, tuple[list, float]] = {}
    page_boxes: dict[int, list[Box]] = {}
    page_dims: dict[int, tuple[float, float]] = {}
    page_kinds = {}

    for path in image_paths:
        page_num = int(path.stem.split("_")[1])

        if should_ocr_page and not should_ocr_page(page_num, total_pages):
            page_kinds[page_num] = "skipped"
            if progress_cb:
                progress_cb(page_num, total_pages)
            continue

        pil_img = Image.open(path).convert("L")
        gray = np.array(pil_img)

        boxes = ocr_image(gray)
        all_text = " ".join(b.text for b in boxes)
        kind = classify_page_fn(page_num, all_text) if classify_page_fn else classify_page(all_text)
        page_kinds[page_num] = kind
        page_dims[page_num] = (gray.shape[1], gray.shape[0])

        if kind == "content":
            page_boxes[page_num] = boxes
            w = gray.shape[1]
            lines = order_page(boxes, w, page=page_num)
            for line in lines:
                line.underlined_words = detect_underlined_words(gray, line)
            content_pages[page_num] = (lines, gray.shape[0])

        if progress_cb:
            progress_cb(page_num, total_pages)

    questions, warnings = parse_document(content_pages)
    for q in questions:
        combined = q.question_stem + q.option_a + q.option_b + q.option_c + q.option_d
        q.has_underline = "<u>" in combined

    bboxes = compute_bboxes(questions, page_boxes, page_dims)

    page_images_by_num = {int(p.stem.split("_")[1]): p for p in image_paths}
    question_dicts = []
    for q in sorted(questions, key=lambda x: x.q_number):
        bb_list = bboxes.get(q.q_number) or []
        image_rel = _crop_question_image(q, bb_list, page_images_by_num, crops_dir)

        table = None
        if bb_list:
            region_boxes = [
                b
                for bb in bb_list
                for b in page_boxes.get(bb.page, [])
                if bb.x0 <= (b.x0 + b.x1) / 2 <= bb.x1 and bb.y0 <= (b.y0 + b.y1) / 2 <= bb.y1
            ]
            if q.question_type == "match_the_list":
                table, problems = reconstruct_match_list(region_boxes, q.q_number)
                if problems:
                    q.needs_review = True
                    q.review_reason = "; ".join(filter(None, [q.review_reason] + problems))
            elif detect_paired_table(region_boxes):
                # A "Read the following pairs :" style table embedded in an
                # otherwise-standard question's stem (see src/paired_table.py)
                # - unlike match_the_list this doesn't replace the normal
                # stem/options, just adds a cleanly-rendered table alongside
                # them.
                q.question_type = "paired_table"
                table, problems = reconstruct_paired_table(region_boxes, q.q_number)
                if problems:
                    q.needs_review = True
                    q.review_reason = "; ".join(filter(None, [q.review_reason] + problems))

        question_dicts.append(
            {
                "q_number": q.q_number,
                "page": q.page,
                "question_type": q.question_type,
                "image": image_rel,
                "section_directions_html": q.section_directions,
                "passage_label": q.passage_label,
                "passage_text_html": q.passage_text,
                "question_stem_html": q.question_stem,
                "options": {"a": q.option_a, "b": q.option_b, "c": q.option_c, "d": q.option_d},
                "table": table,
                "has_underline": q.has_underline,
                "ocr_confidence": round(q.ocr_confidence, 3),
                "needs_review": q.needs_review,
                "review_reason": q.review_reason,
                "user_answer": None,
                "explanation_html": "",
                "tags": [],
                "topics": [],
            }
        )

    # Collapses runs of consecutive questions that share the exact same
    # Directions text (see instructions.py) into one entry each, instead of
    # storing the same paragraph once per question under it.
    instructions = extract_instructions(question_dicts)

    needs_review_count = sum(1 for q in questions if q.needs_review)
    paper_id = paper_dir_p.name
    resolved_source_filename = source_filename or Path(pdf_path).name
    paper = {
        "paper_id": paper_id,
        "variant": variant_id,
        "job_name": job_name or resolved_source_filename,
        "source_filename": resolved_source_filename,
        # if the caller copied the upload into paper_dir/source.pdf (the web
        # app does this so a job never depends on the original upload path
        # sticking around), record it so the UI can offer it back.
        "source_pdf": "source.pdf" if (paper_dir_p / "source.pdf").exists() else None,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "total_questions": len(questions),
        "qa": {
            "sequence_ok": not warnings,
            "warnings": warnings,
            "needs_review_count": needs_review_count,
        },
        "instructions": instructions,
        "questions": question_dicts,
    }

    with open(paper_dir_p / "paper.json", "w", encoding="utf-8") as f:
        json.dump(paper, f, indent=2, ensure_ascii=False)

    return paper


def crop_and_stack_regions(regions: list[tuple[Path, tuple[float, float, float, float]]]) -> Image.Image | None:
    """Crop each (page_image_path, (x0,y0,x1,y1)) region and stack them
    vertically (with a thin separator) into one image. Shared by the
    pipeline's own auto-bbox cropping and the Review UI's manual "Edit
    image" tool (webapp/app.py's recrop route) - usually one region, but a
    question whose content overflows from the bottom of one column to the
    top of the next (or that a user manually marks as multiple areas) gets
    more than one, rather than silently showing only the first."""
    crops = []
    for path, (x0, y0, x1, y1) in regions:
        try:
            with Image.open(path) as page_img:
                crops.append(page_img.crop((x0, y0, x1, y1)).copy())
        except Exception:
            continue
    if not crops:
        return None

    if len(crops) == 1:
        return crops[0]

    gap = 10
    width = max(c.width for c in crops)
    height = sum(c.height for c in crops) + gap * (len(crops) - 1)
    combined = Image.new("L", (width, height), color=255)
    y = 0
    for c in crops:
        combined.paste(c, (0, y))
        y += c.height + gap
    return combined


def _crop_question_image(q: Question, bb_list: list, page_images_by_num: dict, crops_dir: Path) -> str | None:
    regions = [(page_images_by_num[bb.page], (bb.x0, bb.y0, bb.x1, bb.y1)) for bb in bb_list if bb.page in page_images_by_num]
    combined = crop_and_stack_regions(regions)
    if combined is None:
        return None

    rel_name = f"q_{q.q_number:04d}.png"
    combined.save(crops_dir / rel_name)
    return f"images/{rel_name}"


def write_questions_csv(questions: list[Question], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUESTION_CSV_FIELDS)
        writer.writeheader()
        for q in sorted(questions, key=lambda x: x.q_number):
            row = {k: getattr(q, k) for k in QUESTION_CSV_FIELDS}
            writer.writerow(row)


def write_match_list_csv(questions: list[Question], path: Path) -> None:
    rows = [q for q in questions if q.question_type == "match_the_list"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["q_number", "page", "raw_text"])
        for q in rows:
            writer.writerow([q.q_number, q.page, " | ".join(q.raw_lines)])


def write_qa_report(questions: list[Question], warnings: list[str], page_kinds: dict, path: Path) -> None:
    lines = []
    lines.append(f"Total questions parsed: {len(questions)}")
    lines.append(f"Page classification: {json.dumps(page_kinds)}")
    lines.append("")
    if warnings:
        lines.append("SEQUENCE WARNINGS:")
        lines.extend(f"  - {w}" for w in warnings)
    else:
        lines.append("Sequence check: OK (1..120 all present, no duplicates)")
    lines.append("")

    needs_review = [q for q in questions if q.needs_review]
    lines.append(f"Questions flagged needs_review: {len(needs_review)}")
    for q in sorted(needs_review, key=lambda x: x.q_number):
        lines.append(f"  Q{q.q_number} (page {q.page}, type={q.question_type}): {q.review_reason}")
    lines.append("")

    low_conf = [q for q in questions if q.ocr_confidence and q.ocr_confidence < 0.5]
    lines.append(f"Low OCR-confidence questions (<0.5): {len(low_conf)}")
    for q in sorted(low_conf, key=lambda x: x.q_number):
        lines.append(f"  Q{q.q_number} (page {q.page}): confidence={q.ocr_confidence:.2f}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_path")
    ap.add_argument("out_dir")
    ap.add_argument("--image-dir", default=None, help="Reuse pre-rendered page_NNN.png images instead of re-rendering the PDF")
    args = ap.parse_args()
    run_pipeline(args.pdf_path, args.out_dir, image_dir=args.image_dir)
