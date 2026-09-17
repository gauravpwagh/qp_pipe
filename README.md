# qp-pipeline

Converts a scanned CDS-exam-style question paper PDF (single-column and
two-column layout, no text layer) into structured questions, then lets you
review them in a browser: see each question's source-image crop, pick an
answer, and write a rich-text (HTML) explanation with tags.

Built and validated against two CDS Examination (II) 2026 booklets:

- `QP-CDSE-II-26-ENGLISH-140926.pdf` - English, 120 questions / 32 pages.
- `QP-CDSE-II-26-GENERAL-KNOWLEDGE-140926.pdf` - bilingual (every item
  printed in Hindi then English), 120 questions / 56 pages. Our OCR reader
  is English-only, so this variant skips OCR on the Hindi pages entirely
  (see `src/variant_gk.py`) rather than just discarding garbled output -
  correctly *and* roughly halving processing time.

## Variants

A "variant" is a layout profile registered in `webapp/variants.py`, each
pointing at its own `run_*_json` function. `cds_english_v1` (plain
single-language) and `cds_gk_v1` (bilingual, English-only extraction) both
reuse the same core pipeline (`src/pipeline.py`'s `run_pipeline_json`) -
a new variant is usually just a different page classifier / OCR-skip rule
passed into it, not new parsing logic. See `src/variant_gk.py` for the
bilingual pattern to copy from.

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

EasyOCR downloads its recognition model weights (~100MB+) automatically on
first run.

## Web app (recommended)

```bash
python -m flask --app webapp.app run --port 5050
```

Open `http://localhost:5050`. **Process** tab: upload a PDF, optionally give
it a job name (defaults to the PDF's filename - this becomes the label
you'll pick it out by later), pick a variant (see Variants above), click
Process. This runs as a background job (OCR takes ~5-6 minutes for the
English booklet, ~3 minutes for the GK booklet since half its pages are
skipped, both on CPU); the page polls and shows progress, then switches you
to **Review** with the new paper pre-selected. The Review tab's paper
picker also lists every previously-processed job by its name, newest first
- come back anytime and pick up where you left off, no need to re-upload
or re-process anything. Don't want to keep a job? **Delete job** next to
the picker removes it - its `paper.json`, images, and stored source PDF -
permanently, after a confirmation prompt.

In Review: pick a question number to see its source-image crop (left),
question text with clickable answer options or, for "match the list"
questions, a reconstructed List I/List II + Code-answer table (middle), and
topics, tags, and a rich-text explanation editor (bold/italic/underline/
color/font) (right) - the explanation gets whatever vertical space is left
after topics/tags, with its own internal scrollbar, so a long explanation
doesn't push the rest of the pane around. The stem, directions, passage
text, and each option are directly editable in place (click in and type -
useful for fixing the occasional OCR mistake). Answers, explanations,
edited text, topics, and tags all autosave as you go. Topics and tags you
type are remembered per variant (`output/_vocab/<variant_id>.json`) and
suggested back via autocomplete on any question of any paper of that same
variant - so a topic scheme you build up on one paper carries over to the
next.

The Image and Question panes each have a -/+ button in their
header to collapse them to a narrow strip, freeing up width for the
explanation pane - useful when you're mostly writing and don't need the
source image or question text on screen. The panes' widths are already
weighted toward Explanation by default (it's the one most read/written).

The explanation editor's toolbar has a **formula** button (type/paste raw
LaTeX, renders inline via KaTeX) and an **image** button, and also accepts
a directly pasted image (e.g. copied from the LaTeX Scratchpad tab below,
or anywhere else) - so an explanation can mix normal text, LaTeX math, and
images freely.

### LaTeX Scratchpad tab

A formula copied off a website or a PDF usually comes through as either a
picture or, worse, its rendered text flattened into one line - useless for
getting a real stacked fraction back. This tab converts a **formula
image** to LaTeX so you can drop the result straight into an explanation:
click the left pane and paste (Ctrl+V) a copied formula image, or drag-drop
/ choose an image file. pix2tex expects one isolated expression, not a
whole screenshot with surrounding headings/bullets/answer text (that
confuses it into hallucinating unrelated LaTeX) - so once an image loads,
**drag on it to crop down to just the formula** before converting; the
selection defaults to the whole image if you skip this, and **Reset
selection** clears a crop back to the full image. Then **Convert to
LaTeX**. It runs
[pix2tex](https://github.com/lukas-blecher/LaTeX-OCR) locally (no external
API - same offline approach as the EasyOCR pipeline; its model weights,
~115MB, download automatically on first use and are cached after that).
Check the rendered preview and the raw LaTeX source on the right, then
**Copy LaTeX** and paste it into an explanation's formula field. Like any
OCR, it's reliable on clean typeset formulas and can misread messy or
low-resolution ones - always check the rendered preview before trusting
it.

Don't want to crop at all? **Auto-detect & Stitch** (section 3, same
pasted image) splits the whole screenshot into lines via OCR, guesses
which ones are a formula (has "=" and a lot of math/digit characters) vs.
plain prose, converts only the formula-looking ones via pix2tex, and
stitches everything back into one HTML block - text and rendered
formulas both. This is a heuristic, not a trained layout-detection model,
so check the line list it shows you: click **Mark as formula** /
**Mark as text** to fix a misclassified line (forcing a line to formula
runs the conversion on demand). It works well when a formula sits on one
line (e.g. `UR = 50-45/50 x 100 = 10%`); a stacked fraction spanning two
visual rows gets split into separate text lines instead, since each
fragment alone doesn't look like a formula - use the crop tool (section
1/2) for those instead. The stitched HTML textarea is editable and
re-renders live, same as the single-formula result - **Copy HTML** when
it looks right, and paste it into an explanation.

Each processed job's data lives at `output/<paper_id>/`:
`paper.json` (schema below), the per-question cropped `images/`, the full
rendered `page_images/`, and a `source.pdf` copy of exactly what was
uploaded - the job never depends on the original upload path again, so
it's safe to delete/move the PDF you uploaded from afterward.

## CLI (CSV output)

For scripting/inspection without the web UI:

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

## `paper.json` schema

Each question in the `questions` array:

Top-level fields: `paper_id`, `variant`, `job_name` (the label shown in the
Review picker - user-chosen at upload time, falls back to the filename),
`source_filename` (the original uploaded filename), `source_pdf` (relative
path to the stored copy, normally `"source.pdf"`), `processed_at`,
`total_questions`, `qa` (`sequence_ok`/`warnings`/`needs_review_count`),
and `questions`. Each question in that array:

| field | meaning |
|---|---|
| `q_number`, `page` | 1..N; source PDF page |
| `question_type` | `standard`, `para_jumble`, `sentence_relation`, `comprehension`, `match_the_list` |
| `image` | path (relative to the paper's folder) to the cropped question image |
| `section_directions_html`, `passage_label`, `passage_text_html`, `question_stem_html` | OCR'd text; underlined words wrapped `<u>word</u>` |
| `options` | `{a, b, c, d}` option text (empty/unused for `match_the_list`) |
| `table` | for `match_the_list`: `{list1, list2, code_table}` (see `src/match_list.py`), else `null` |
| `ocr_confidence`, `needs_review`, `review_reason` | as in the CSV schema below |
| `user_answer`, `explanation_html`, `tags` | filled in by the review UI - `null`/`""`/`[]` until then |

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
   or option-marker glyph outright (see Known Limitations). Directions/
   Passage headers are only matched on `FULL`-width lines - a normal
   sentence can word-wrap so an ordinary printed line starts with the word
   "directions" (e.g. "...shall extend to the giving of / directions to
   the States...", seen in the GK booklet), and without that guard such a
   line reads as a false section-header and corrupts every question after
   it.
6. `bbox.py` - computes each question's pixel bounding region(s), directly
   from the bounding rects of the OCR lines that actually became that
   question's text (`Question.regions`, grouped by page+column in
   `parse_questions.py`) - used to crop its review-UI image and to scope
   `match_list.py`'s box search. Normally one region; on a dense layout a
   question's content can overflow from the bottom of one column to the
   top of the next on the same page, giving two - both get cropped and
   stacked into one image (`pipeline.py::_crop_question_image`) rather than
   silently showing only the first.
7. `match_list.py` - for `match_the_list` questions, re-parses the raw OCR
   boxes in that question's bbox into a structured List I / List II / Code
   answer-table, with position-based recovery when a label glyph or answer
   digit drops out (see Known Limitations).
8. `pipeline.py` - orchestrates the above; `run_pipeline` writes the CSVs +
   QA report (CLI), `run_pipeline_json` writes `paper.json` + image crops
   (web app, see `webapp/`).

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
- **Match-the-list questions** (List I / List II / a Code answer-table) get
  a dedicated reconstruction pass (`match_list.py`) into a proper table
  (see the Review UI or `table` in `paper.json`) rather than the flat
  option schema. The small answer-grid digits are the least reliable OCR
  spot found in this booklet - a digit or an option-letter glyph
  occasionally drops out entirely. Where exactly one grid value is missing
  and the other three are distinct digits 1-4, it's inferred (every row
  observed is a permutation of 1-4) and flagged `needs_review` anyway;
  where more is missing it's left blank (`null`/`?`) - check these against
  the question's own image crop, shown right alongside the table. The CLI's
  `match_the_list.csv` still carries the raw OCR text as a fallback.
- **Spotting-errors-style questions** (a sentence divided into labelled
  segments, "No error" as a possible answer) weren't part of the original
  layout survey and aren't specially parsed - they'll come through with
  `needs_review=True` due to missing options.
- Scoped to this booklet's **English-variant, text-only MCQ** layout.
  Other subjects (Maths/GS) or booklet series may have different layouts
  (diagrams, equations, different spacing) not handled here.
