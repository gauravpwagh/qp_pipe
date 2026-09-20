# qp-pipeline

Converts a scanned CDS-exam-style question paper PDF (single-column and
two-column layout, no text layer) into structured questions, then lets you
review them in a browser: see each question's source-image crop, pick an
answer, and write a rich-text (HTML) explanation with tags.

Built and validated against three 2026 exam booklets:

- `QP-CDSE-II-26-ENGLISH-140926.pdf` - CDS English, 120 questions / 32 pages.
- `QP-CDSE-II-26-GENERAL-KNOWLEDGE-140926.pdf` - CDS General Knowledge,
  bilingual (every item printed in Hindi then English), 120 questions /
  56 pages. Our OCR reader is English-only, so this variant skips OCR on
  the Hindi pages entirely (see `src/variant_gk.py`) rather than just
  discarding garbled output - correctly *and* roughly halving processing
  time.
- `QP-NDANA-II-26-GENERAL-ABILITY-TEST-140926.pdf` - NDA & NA General
  Ability Test, 150 questions / 52 pages: an all-English "Part A"
  (Q1-50, pages 2-7) followed by a bilingual "Part B" (Q51-150, pages 8-47, alternating
  Hindi/English pages like the GK booklet). See `src/variant_ndana_gat.py`
  and "NDA/NA GAT: Part A/Part B page numbers" below - unlike the GK
  booklet, the bilingual split doesn't start on page 2, so the boundary
  is entered per job rather than hardcoded.

## Variants

A "variant" is a layout profile registered in `webapp/variants.py`, each
pointing at its own `run_*_json` function. `cds_english_v1` (plain
single-language), `cds_gk_v1` (bilingual, English-only extraction), and
`ndana_gat_v1` (English-only "Part A" then bilingual "Part B") all reuse
the same core pipeline (`src/pipeline.py`'s `run_pipeline_json`) - a new
variant is usually just a different page classifier / OCR-skip rule
passed into it, not new parsing logic. See `src/variant_gk.py` for the
bilingual pattern to copy from, or `src/variant_ndana_gat.py` for a
variant that also needs a per-job input from the user (see below).

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

A variant can ask for extra input beyond the PDF/job name (registered as
"fields" in `webapp/variants.py`) - the Process tab renders these as
plain inputs once that variant is selected.

#### NDA/NA GAT: Part A/Part B page numbers

Selecting `ndana_gat_v1` adds two required number fields: **Last page of
Part A** and **Last page of Part B**. Unlike the CDS GK booklet (bilingual
from page 2 onward, a fixed rule), this booklet's Hindi/English split only
starts partway through, at a page number that can shift between years'
printings - so rather than hardcoding it (and silently mis-processing a
differently-paginated booklet), you look it up once per PDF and enter it:
open the PDF, find the last all-English page before the questions start
repeating in Hindi (that's Part A's last page), then the last page before
rough-work/back-cover pages begin (Part B's last page). Pages 1..Part A
are always OCR'd (all-English); Part A+1..Part B alternate Hindi (skipped)
/English (OCR'd), Hindi first; everything after Part B is always OCR'd
too (rough-work and the English back-cover, same as any other variant).

In Review: pick a question number to see its source-image crop (left),
question text with clickable answer options or, for "match the list"
questions, a reconstructed List I/List II + Code-answer table (middle), and
topics, tags, and a rich-text explanation editor (bold/italic/underline/
color/font) (right) - Topics/Tags sit above Explanation, which grows to
fit its own content; the pane scrolls as a whole once that's taller than
the viewport. The stem, directions, passage text, and each option are
directly editable in place (click in and type - useful for fixing the
occasional OCR mistake); select some text in any of them and a small
floating **B**/**U** toolbar pops up to bold/underline it, same as you'd
expect from any text editor. Answers, explanations, edited text, topics,
and tags all autosave as you go. Topics and tags you type are remembered
per variant (`output/_vocab/<variant_id>.json`) and suggested back via
autocomplete on any question of any paper of that same variant - so a
topic scheme you build up on one paper carries over to the next.

A flagged `⚠ needs review: ...` warning has a small **×** next to it once
you've checked the question and confirmed it's actually fine - clicking it
clears the flag (and its "⚠" in the Question dropdown) for good; it
doesn't re-appear on its own unless a later **Reprocess** re-detects the
same issue. The paper picker's "N flagged" count stays in sync as flags
get dismissed (or re-raised).

If the pipeline's auto-detected crop is wrong - cuts off part of the
question, includes a neighbor's, or splits across a column in a way that
didn't stack right - click **Edit** in the Image pane's header. It opens
the question's full source page in a modal; drag to mark the question's
real area, draw more than one region for content split across a column
break (they stack vertically into one image, in the order drawn, exactly
like the pipeline's own auto-crop does), then **Save crop**. Reopening the
editor later starts from your last manual selection so it's easy to
nudge rather than redraw from scratch.

Once a crop is right, **Reprocess** (next to Edit) re-runs OCR + text
extraction on just that image and overwrites the question's stem/options
(or table, for match-the-list) or the instruction's text - useful after
fixing a bad crop, since the original extraction ran against the old one
and is now stale. It overwrites immediately (confirmed first, since any
manual corrections to that text are lost) rather than re-classifying the
question's type - a standard question stays standard, a match-the-list
stays match-the-list, just with fresher content.

Match-the-list's List I/List II and Code-answer table (see Known
limitations below) are directly editable in place too, same click-and-type
pattern as everywhere else, for fixing the OCR gaps that table type is
most prone to.

The Image and Question panes each have a -/+ button in their
header to collapse them to a narrow strip, freeing up width for the
explanation pane - useful when you're mostly writing and don't need the
source image or question text on screen. The panes' widths are already
weighted toward Explanation by default (it's the one most read/written).

### Shared instructions ("Directions :")

A block of consecutive questions sharing one "Directions :" paragraph
(common in these booklets - e.g. Q1-10 all say "select the option that
best describes...") is stored once, not once per question: it gets its
own entry (`I1`, `I2`, ...) in the paper's nav sequence, right before the
first question it applies to - `..., Q7, I1, Q8, Q9, ...`. Selecting one
shows it the same way a question is - image left, editable text middle -
minus the answer/topics/tags/explanation parts, which don't apply to it.
It starts with no image (the pipeline doesn't separately track a
Directions header's own region); use the same **Edit** button/full-page
editor described above to mark where it is on the page. A paper processed
before this feature existed is migrated automatically the first time it's
opened - its previously-duplicated per-question text is split out into
instructions transparently, no reprocessing needed.

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
`instructions`, and `questions`.

Each entry in `instructions` (see src/instructions.py; empty list if no
question shares a Directions block with another):

| field | meaning |
|---|---|
| `instruction_id` | `"I1"`, `"I2"`, ... - referenced by each question's own `instruction_id` |
| `text_html` | the Directions text (editable in the Review UI) |
| `applies_to` | ascending list of `q_number`s this instruction covers |
| `image`, `image_regions` | `null` until manually set via the Review UI's "Edit image" tool - same shape as a question's (below) |

Each question in the `questions` array:

| field | meaning |
|---|---|
| `q_number`, `page` | 1..N (editable per question from the Review UI - must stay unique; renumbering re-sorts the list and updates each instruction's covered range); source PDF page |
| `question_type` | `standard`, `para_jumble`, `sentence_relation`, `comprehension`, `match_the_list`, `paired_table`, `grid_table` - the pipeline's guess; changeable per question from the Review UI's type dropdown (switching to a table type builds its table from the crop, switching away drops it) |
| `image` | path (relative to the paper's folder) to the cropped question image |
| `image_regions` | `null` until the Review UI's "Edit image" tool is used on this question, then `{page, boxes: [[x0,y0,x1,y1], ...]}` in source-page pixel coordinates - the last manually-drawn crop, reloaded to pre-fill the editor next time; optionally also `marks: [{id, type: "formula"\|"chemistry"\|"diagram", box, latex?, file?}]` - see "Formulas, chemistry and diagrams" below |
| `instruction_id` | `null`, or the `instructions` entry this question shares a Directions block with |
| `passage_label`, `passage_text_html`, `question_stem_html` | OCR'd text; underlined words wrapped `<u>word</u>` |
| `options` | `{a, b, c, d}` option text (empty/unused for `match_the_list` with a Code grid; filled as usual for one without) |
| `table` | for `match_the_list`: `{list1, list2, code_table}` (see `src/match_list.py`; `code_table` is `null` when the question has no Code grid and instead ordinary text answers, and either list may be labelled I-IV, A-D or 1-4); for `paired_table`: `{headers: [left, right], rows: [{label, col1, col2}, ...]}` (see `src/paired_table.py`); for `grid_table`: `{header: bool, rows: [[{html, rs, cs}, ...], ...]}` (see `src/grid_table.py`; each row lists only the cells that start in it, `rs`/`cs` are row/column spans); else `null` |
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

### Formulas, chemistry and diagrams

Entirely manual, per question, in the Review tab's **Edit** (image) modal - the
one-go pipeline never creates them. After drawing the question's area, switch
the modal's "Draw" mode to **Formula**, **Chemistry** or **Diagram** and mark
those areas inside it (`src/marks.py`):

- **Formula** - read into LaTeX with pix2tex the moment it is drawn; the LaTeX
  is editable with a live KaTeX preview.
- **Chemistry** - typed in mhchem syntax (`\ce{H2SO4 + 2NaOH -> Na2SO4 + 2H2O}`;
  a bare `H2SO4` is wrapped for you). pix2tex isn't trained on reactions, so
  it's only offered as a draft via a button.
- **Diagram** - cropped from the full-resolution page into
  `images/diagrams/`, kept as a picture.

Saving rebuilds the question's stem and options from the crop image with the
marked areas masked out of OCR and each mark placed inline at its position (a
formula can sit mid-sentence, or on the option line it belongs to); a confirm
warns that manual text edits are replaced. Reprocess honours saved marks.
In `question_stem_html` / `options` a formula or chemistry mark is stored as
`<span class="math-token" data-type="formula|chemistry" data-latex="...">` and a
diagram as `<img class="diagram-token" src="images/diagrams/...">` (a path
relative to the job folder); the page renders the spans with KaTeX + mhchem.
Removing every mark and saving rebuilds plain OCR text and deletes the
diagram files.

**Editing in the review panes.** Click a formula or chemistry token in the
stem or an option to open a small editor (LaTeX box, live preview, Formula /
Chemistry switch, Apply / Cancel / Delete); "Insert at cursor" adds a new one
where the cursor is. Backspace/Delete on a token removes it as one unit.
Changes save like typed text, and an edit to a token that came from a mark also
updates the stored mark (`marks_update` on the question PATCH), so the
Edit-image modal shows the corrected LaTeX.

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
- **Paired-items questions** (`src/paired_table.py`; NDA/NA GAT, e.g. "Read
  the following pairs: I. ... | ... II. ... | ..." then "Identify the
  pair(s) wherein...") get a two-column table reconstructed alongside the
  normal stem and (a)-(d) options - unlike match-the-list this doesn't
  replace them, since the table is only part of the question, not the
  whole of it. Detected structurally (a genuine column gap under Roman-
  numeral row labels, not a keyword), so a plain vertical list of
  statements ("Consider the following: I. ... II. ...") is correctly left
  alone as a standard question.
- **Ruled ("all borders drawn") tables** (`src/grid_table.py`) are built on
  request: choose **Grid table** in the question's type dropdown (or Reprocess a
  question already of that type). The ruling lines are found with OpenCV, giving
  the rows and columns (a missing separator between two cells means a merged
  cell, kept as a row/column span); the table is OCR'd once and each word goes to
  the cell holding its centre, with words that cross a border re-read segment by
  segment. The text above the table is the stem, the text below it (before the
  options) is `stem_after_table_html`, and the options are the usual (a)-(d).
  Cells are editable in the review pane. Never done automatically by the one-go
  pipeline - it needs the ruling lines to be reasonably intact (a badly broken or
  strongly tilted scan can miss lines or merge cells wrongly; those cases are
  flagged for review), and a table split across a column or page break is not
  handled.
- **Spotting-errors-style questions** (a sentence divided into labelled
  segments, "No error" as a possible answer) weren't part of the original
  layout survey and aren't specially parsed - they'll come through with
  `needs_review=True` due to missing options.
- Scoped to this booklet's **English-variant, text-only MCQ** layout.
  Other subjects (Maths/GS) or booklet series may have different layouts
  (diagrams, equations, different spacing) not handled here.
