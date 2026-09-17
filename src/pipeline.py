"""CLI orchestrator: PDF -> questions.csv + match_the_list.csv + qa_report.txt"""

import csv
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from .ocr import ocr_image
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
