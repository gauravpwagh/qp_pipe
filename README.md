# qp-pipeline

Converts a scanned CDS-exam-style question paper PDF (single-column and
two-column layout, no text layer) into a structured CSV of questions and
options.

Built and validated against `QP-CDSE-II-26-ENGLISH-140926.pdf` (CDS
Examination (II), 2026, English test booklet, 120 questions across 32
scanned pages).

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

EasyOCR downloads its recognition model weights (~100MB+) automatically on
first run.

## Usage

```bash
python -m src.pipeline path\to\question_paper.pdf output\
```

This renders every page, OCRs it, reconstructs reading order, and writes:

- `output/questions.csv` - one row per question (see schema below)
- `output/match_the_list.csv` - raw text for "match List I to List II"
  questions, which don't fit the flat 4-option schema (see Known
  Limitations)
- `output/qa_report.txt` - sequence check (all N questions present, no
  duplicates), and a list of questions flagged `needs_review`

Pass `--image-dir <dir>` to reuse already-rendered `page_NNN.png` images
instead of re-rendering the PDF (useful when iterating).

## `questions.csv` schema

| column | meaning |
|---|---|
| `q_number` | 1..N |
| `page` | source PDF page number |
| `question_type` | `standard`, `para_jumble`, `sentence_relation`, `comprehension`, `match_the_list` (best-effort classification) |
| `section_directions` | the "Directions :" text in force for this question |
| `passage_label` / `passage_text` | for reading-comprehension questions |
| `question_stem`, `option_a..d` | the question text; underlined words (where detected) are wrapped `<u>word</u>` |
| `has_underline` | whether any underline was detected in this question |
| `ocr_confidence` | mean EasyOCR confidence across the question's text |
| `needs_review` / `review_reason` | flagged when something looks off (see below) |

## How it works

1. `render.py` - PDF pages -> PNG images (`pypdfium2`).
2. `ocr.py` - EasyOCR on the raw page image (word/phrase-level boxes with
   bounding coordinates and confidence).
3. `reading_order.py` - clusters raw OCR boxes into lines, classifies each
   line as `LEFT` / `RIGHT` column or `FULL` width (Directions headers and
   passage paragraphs), and re-emits two-column blocks column-major (all of
   the left column top-to-bottom, then all of the right column) - this
   reproduces the booklet's true ascending question order.
4. `underline.py` - for each OCR'd word, checks the pixel strip just below
   it for an underline stroke.
5. `parse_questions.py` - walks the linear line stream with a small state
   machine to find question/option/Directions/Passage boundaries via
   regex, with a recovery path for when OCR drops a question-number token
   or option-marker glyph outright (see Known Limitations).
6. `pipeline.py` - orchestrates the above and writes the CSVs + QA report.

## Known limitations

- **OCR is not perfect.** This is a photocopy-quality scan. A question gets
  `needs_review=True` when: an option is missing, OCR dropped a question
  number entirely (recovered by inferring it from the sequence - flagged
  regardless so a human can verify), or it's a `match_the_list` question.
  Always check `qa_report.txt` and cross-reference flagged rows against the
  source PDF before trusting them.
- **Underline detection is best-effort** on a noisy scan; it will miss some
  underlines and may occasionally mark a false one. This matters for
  question types where the answer depends on which word is underlined
  (e.g. "similar sounding words" items) - verify those manually.
- **Match-the-list questions** (List I / List II / a Code answer-table) have
  a genuinely nested 3-column layout within what looks like one half of the
  page. The generic 2-column reading-order logic can't cleanly separate
  List I terms, List II meanings, and the Code table for these, so they are
  detected and routed to `match_the_list.csv` with their raw (possibly
  jumbled) OCR text instead of being forced into the flat schema - expect
  to reconstruct these by hand against the source PDF.
- **Spotting-errors-style questions** (a sentence divided into labelled
  segments, "No error" as a possible answer) weren't part of the original
  layout survey and aren't specially parsed - they'll come through with
  `needs_review=True` due to missing options.
- Scoped to this booklet's **English-variant, text-only MCQ** layout.
  Other subjects (Maths/GS) or booklet series may have different layouts
  (diagrams, equations, different spacing) not handled here.
