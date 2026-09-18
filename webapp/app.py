"""Flask app: upload a PDF, run the pipeline as a background job, review
results (question image + text + answer choice + rich-text explanation/tags)
in the browser.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from . import jobs
from .variants import VARIANTS
from src import latex_ocr, screenshot_stitch
from src.instructions import extract_instructions
from src.pipeline import crop_and_stack_regions
from src.reprocess import reprocess_instruction, reprocess_question

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Per-variant vocabulary of every tag/topic ever entered, so the UI can
# suggest previously-used ones while typing a new one. Lives in its own
# subfolder (name starts with "_", so it never collides with a paper_id,
# which always starts with a variant id like "cds_english_v1-...").
VOCAB_DIR = OUTPUT_DIR / "_vocab"
VOCAB_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")

_write_lock = threading.Lock()


def _slugify(name: str) -> str:
    stem = Path(name).stem
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return slug or "paper"


def _paper_dir(paper_id: str) -> Path:
    return OUTPUT_DIR / paper_id


def _read_paper(paper_id: str) -> dict | None:
    path = _paper_dir(paper_id) / "paper.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        paper = json.load(f)
    if "instructions" not in paper:
        # One-time lazy migration for a paper.json written before shared
        # Directions text was split out (see src/instructions.py) - avoids
        # needing to re-OCR every already-processed job to get it.
        paper["instructions"] = extract_instructions(paper["questions"])
        _write_paper_atomic(paper_id, paper)
    return paper


def _replace_with_retry(tmp_path: str, path: Path) -> None:
    """os.replace() onto a destination that's open for reading elsewhere
    at that exact moment - a GET request loading paper.json/an image, an
    antivirus/search-indexer/sync-client scan, this same process's own
    _read_paper() a moment earlier - can raise PermissionError outright on
    Windows (unlike POSIX rename, which doesn't care who has the target
    open). Confirmed by reproducing the race directly. The reader is
    normally done within milliseconds, so retrying briefly resolves it
    rather than surfacing a failure the user has to notice and retry by
    hand (this is what made both "Save crop" and dismissing a needs_review
    flag sometimes need a couple of tries - paper.json's write had no
    retry at all, and images/ only relied on the swap itself, not a retry
    loop)."""
    last_err = None
    for _ in range(20):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.1)
    raise last_err


def _write_paper_atomic(paper_id: str, paper: dict) -> None:
    # Recomputed from the questions themselves on every save, rather than
    # incrementally tracked (+1/-1) at each call site that can change
    # needs_review - a delta only where the dismiss-flag route remembered
    # to apply one already drifted out of sync with Reprocess, which flips
    # needs_review directly via question.update() with no such bookkeeping.
    # A single recompute here is the ground truth and can't drift.
    if "qa" in paper and "questions" in paper:
        paper["qa"]["needs_review_count"] = sum(1 for q in paper["questions"] if q.get("needs_review"))

    path = _paper_dir(paper_id) / "paper.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(paper, f, indent=2, ensure_ascii=False)
        _replace_with_retry(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _save_image_atomic(path: Path, image) -> None:
    """Same reasoning as _write_paper_atomic - this filename is fixed
    (q_0001.png, I1.png, ...) and can be open for reading elsewhere at the
    same moment (the review page's own <img>, a Reprocess call's
    Image.open())."""
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.close(fd)
        image.save(tmp_path, format="PNG")
        _replace_with_retry(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _vocab_path(variant_id: str) -> Path:
    return VOCAB_DIR / f"{_slugify(variant_id)}.json"


def _read_vocab(variant_id: str) -> dict:
    path = _vocab_path(variant_id)
    if not path.exists():
        return {"tags": [], "topics": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {"tags": data.get("tags", []), "topics": data.get("topics", [])}


def _write_vocab_atomic(variant_id: str, vocab: dict) -> None:
    path = _vocab_path(variant_id)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(vocab, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _remember_vocab(variant_id: str, tags: list | None, topics: list | None) -> None:
    """Merge any new tags/topics into this variant's suggestion vocabulary
    (dedup, case-insensitively, keeping first-seen casing) - called after a
    question's tags/topics are saved."""
    if not tags and not topics:
        return
    vocab = _read_vocab(variant_id)
    changed = False
    for key, incoming in (("tags", tags), ("topics", topics)):
        if not incoming:
            continue
        existing_lower = {v.lower() for v in vocab[key]}
        for v in incoming:
            v = (v or "").strip()
            if v and v.lower() not in existing_lower:
                vocab[key].append(v)
                existing_lower.add(v.lower())
                changed = True
    if changed:
        vocab["tags"].sort(key=str.lower)
        vocab["topics"].sort(key=str.lower)
        _write_vocab_atomic(variant_id, vocab)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/variants")
def api_variants():
    return jsonify([{"id": k, "label": v["label"]} for k, v in VARIANTS.items()])


@app.route("/api/process", methods=["POST"])
def api_process():
    variant_id = request.form.get("variant_id")
    if variant_id not in VARIANTS:
        return jsonify({"error": f"unknown variant_id {variant_id!r}"}), 400
    file = request.files.get("pdf")
    if file is None or not file.filename:
        return jsonify({"error": "no pdf file uploaded"}), 400

    source_filename = secure_filename(file.filename)
    job_name = (request.form.get("job_name") or "").strip() or Path(source_filename).stem
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    paper_id = f"{variant_id}-{_slugify(job_name)}-{timestamp}"
    paper_dir = _paper_dir(paper_id)
    paper_dir.mkdir(parents=True, exist_ok=True)

    # Keep a copy of the uploaded PDF inside the job's own folder, so this
    # job never depends on the original upload (which the browser only sent
    # once) or a shared uploads dir still existing later.
    source_pdf_path = paper_dir / "source.pdf"
    file.save(source_pdf_path)

    job_id = jobs.start_job(str(source_pdf_path), variant_id, paper_dir, source_filename, job_name=job_name)
    return jsonify({"job_id": job_id, "paper_id": paper_id})


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    job = jobs.get_job(job_id)
    if job is None:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job)


@app.route("/api/papers")
def api_papers():
    summaries = []
    for child in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        paper = _read_paper(child.name)
        if paper is None:
            continue
        summaries.append(
            {
                "paper_id": paper["paper_id"],
                "label": paper.get("job_name") or paper.get("source_filename", paper["paper_id"]),
                "job_name": paper.get("job_name"),
                "source_filename": paper.get("source_filename"),
                "processed_at": paper.get("processed_at"),
                "total_questions": paper.get("total_questions"),
                "needs_review_count": paper.get("qa", {}).get("needs_review_count"),
            }
        )
    return jsonify(summaries)


@app.route("/api/papers/<paper_id>")
def api_paper_detail(paper_id):
    paper = _read_paper(paper_id)
    if paper is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(paper)


@app.route("/api/papers/<paper_id>", methods=["DELETE"])
def api_delete_paper(paper_id):
    paper_dir = _paper_dir(paper_id)
    # Guard against a paper_id that escapes OUTPUT_DIR (e.g. "..") before
    # ever touching the filesystem with a recursive delete.
    if paper_dir.resolve().parent != OUTPUT_DIR.resolve():
        return jsonify({"error": "invalid paper_id"}), 400
    if not paper_dir.is_dir():
        return jsonify({"error": "not found"}), 404
    with _write_lock:
        shutil.rmtree(paper_dir)
    return jsonify({"ok": True})


@app.route("/api/papers/<paper_id>/images/<path:filename>")
def api_paper_image(paper_id, filename):
    return send_from_directory(_paper_dir(paper_id) / "images", filename)


@app.route("/api/papers/<paper_id>/page_images/<path:filename>")
def api_paper_page_image(paper_id, filename):
    """Full source page image - used by the Review UI's "Edit image" tool,
    which lets the user redraw the question's crop region(s) from scratch
    rather than trusting only the pipeline's auto-detected bounding box."""
    return send_from_directory(_paper_dir(paper_id) / "page_images", filename)


def _crop_regions_or_error(paper_id: str, body: dict):
    """Shared by the question and instruction recrop routes: validate the
    {page, boxes} body, load that page's stored image, and produce the
    stacked crop. Returns (combined_image, None) on success or
    (None, (response, status)) on failure."""
    page = body.get("page")
    boxes = body.get("boxes")
    if not isinstance(page, int) or not isinstance(boxes, list) or not boxes:
        return None, (jsonify({"error": "expected {page: int, boxes: [[x0,y0,x1,y1], ...]}"}), 400)
    for b in boxes:
        if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)):
            return None, (jsonify({"error": "each box must be [x0,y0,x1,y1]"}), 400)

    page_image_path = _paper_dir(paper_id) / "page_images" / f"page_{page:03d}.png"
    if not page_image_path.exists():
        return None, (jsonify({"error": f"no stored page image for page {page}"}), 404)

    combined = crop_and_stack_regions([(page_image_path, tuple(float(v) for v in b)) for b in boxes])
    if combined is None:
        return None, (jsonify({"error": "could not crop the given region(s)"}), 400)
    return combined, None


@app.route("/api/papers/<paper_id>/questions/<int:q_number>/recrop", methods=["POST"])
def api_recrop_question(paper_id, q_number):
    body = request.get_json(force=True, silent=True) or {}
    combined, err = _crop_regions_or_error(paper_id, body)
    if err:
        return err

    with _write_lock:
        paper = _read_paper(paper_id)
        if paper is None:
            return jsonify({"error": "paper not found"}), 404
        question = next((q for q in paper["questions"] if q["q_number"] == q_number), None)
        if question is None:
            return jsonify({"error": "question not found"}), 404

        images_dir = _paper_dir(paper_id) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        rel_name = f"q_{q_number:04d}.png"
        try:
            _save_image_atomic(images_dir / rel_name, combined)
        except PermissionError as e:
            return jsonify({"error": f"could not save the image (file busy, try again): {e}"}), 409

        question["image"] = f"images/{rel_name}"
        # Remembered so re-opening the editor later starts from the last
        # manual selection instead of blank.
        question["image_regions"] = {"page": body["page"], "boxes": body["boxes"]}
        _write_paper_atomic(paper_id, paper)

    return jsonify(question)


@app.route("/api/papers/<paper_id>/questions/<int:q_number>/reprocess", methods=["POST"])
def api_reprocess_question(paper_id, q_number):
    """Re-runs OCR + extraction on the question's CURRENT image (see
    src/reprocess.py) and overwrites its stem/options (or table, for
    match_the_list) - typically used right after "Edit image" fixes a bad
    crop, so stale text from the original pipeline run doesn't linger.
    Overwrites immediately, no preview - any manual corrections to the
    question's text are lost, which the Review UI warns about before
    calling this."""
    with _write_lock:
        paper = _read_paper(paper_id)
        if paper is None:
            return jsonify({"error": "paper not found"}), 404
        question = next((q for q in paper["questions"] if q["q_number"] == q_number), None)
        if question is None:
            return jsonify({"error": "question not found"}), 404
        if not question.get("image"):
            return jsonify({"error": "this question has no image to reprocess"}), 400

        image_path = _paper_dir(paper_id) / question["image"]
        if not image_path.exists():
            return jsonify({"error": "the question's image file is missing"}), 404

        from PIL import Image

        try:
            with Image.open(image_path) as image:
                result = reprocess_question(image, question.get("question_type", "standard"), q_number)
        except Exception as e:
            return jsonify({"error": f"reprocess failed: {e}"}), 500

        question.update(result)
        _write_paper_atomic(paper_id, paper)

    return jsonify(question)


@app.route("/api/papers/<paper_id>/instructions/<instruction_id>", methods=["PATCH"])
def api_update_instruction(paper_id, instruction_id):
    body = request.get_json(force=True, silent=True) or {}
    if "text_html" not in body:
        return jsonify({"error": "expected {text_html}"}), 400

    with _write_lock:
        paper = _read_paper(paper_id)
        if paper is None:
            return jsonify({"error": "paper not found"}), 404
        inst = next((i for i in paper.get("instructions", []) if i["instruction_id"] == instruction_id), None)
        if inst is None:
            return jsonify({"error": "instruction not found"}), 404
        inst["text_html"] = body["text_html"]
        _write_paper_atomic(paper_id, paper)

    return jsonify(inst)


@app.route("/api/papers/<paper_id>/instructions/<instruction_id>/recrop", methods=["POST"])
def api_recrop_instruction(paper_id, instruction_id):
    body = request.get_json(force=True, silent=True) or {}
    combined, err = _crop_regions_or_error(paper_id, body)
    if err:
        return err

    with _write_lock:
        paper = _read_paper(paper_id)
        if paper is None:
            return jsonify({"error": "paper not found"}), 404
        inst = next((i for i in paper.get("instructions", []) if i["instruction_id"] == instruction_id), None)
        if inst is None:
            return jsonify({"error": "instruction not found"}), 404

        images_dir = _paper_dir(paper_id) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        rel_name = f"{instruction_id}.png"
        try:
            _save_image_atomic(images_dir / rel_name, combined)
        except PermissionError as e:
            return jsonify({"error": f"could not save the image (file busy, try again): {e}"}), 409

        inst["image"] = f"images/{rel_name}"
        inst["image_regions"] = {"page": body["page"], "boxes": body["boxes"]}
        _write_paper_atomic(paper_id, paper)

    return jsonify(inst)


@app.route("/api/papers/<paper_id>/instructions/<instruction_id>/reprocess", methods=["POST"])
def api_reprocess_instruction(paper_id, instruction_id):
    with _write_lock:
        paper = _read_paper(paper_id)
        if paper is None:
            return jsonify({"error": "paper not found"}), 404
        inst = next((i for i in paper.get("instructions", []) if i["instruction_id"] == instruction_id), None)
        if inst is None:
            return jsonify({"error": "instruction not found"}), 404
        if not inst.get("image"):
            return jsonify({"error": "this instruction has no image to reprocess"}), 400

        image_path = _paper_dir(paper_id) / inst["image"]
        if not image_path.exists():
            return jsonify({"error": "the instruction's image file is missing"}), 404

        from PIL import Image

        try:
            with Image.open(image_path) as image:
                result = reprocess_instruction(image)
        except Exception as e:
            return jsonify({"error": f"reprocess failed: {e}"}), 500

        inst.update(result)
        _write_paper_atomic(paper_id, paper)

    return jsonify(inst)


@app.route("/api/papers/<paper_id>/source.pdf")
def api_paper_source_pdf(paper_id):
    path = _paper_dir(paper_id) / "source.pdf"
    if not path.exists():
        return jsonify({"error": "no stored source PDF for this paper"}), 404
    return send_from_directory(_paper_dir(paper_id), "source.pdf")


@app.route("/api/latex/convert", methods=["POST"])
def api_latex_convert():
    """LaTeX Scratchpad tab: image -> LaTeX via pix2tex, running locally.
    The model download/load on first call can take a while; later calls
    reuse the cached model."""
    file = request.files.get("image")
    if file is None or not file.filename:
        return jsonify({"error": "no image uploaded"}), 400
    from PIL import Image
    import io

    try:
        image = Image.open(io.BytesIO(file.read())).convert("RGB")
    except Exception:
        return jsonify({"error": "could not read the uploaded image"}), 400

    try:
        latex = latex_ocr.image_to_latex(image)
    except Exception as e:
        return jsonify({"error": f"conversion failed: {e}"}), 500

    return jsonify({"latex": latex})


@app.route("/api/latex/analyze_screenshot", methods=["POST"])
def api_latex_analyze_screenshot():
    """LaTeX Scratchpad's "Auto-detect & Stitch" mode: split a whole pasted
    screenshot into text/formula lines (heuristic, not a trained
    layout-detection model - see README) and return each one top-to-bottom
    so the web app can stitch and let the user correct misclassified
    lines."""
    file = request.files.get("image")
    if file is None or not file.filename:
        return jsonify({"error": "no image uploaded"}), 400
    from PIL import Image
    import io

    try:
        image = Image.open(io.BytesIO(file.read())).convert("RGB")
    except Exception:
        return jsonify({"error": "could not read the uploaded image"}), 400

    try:
        lines = screenshot_stitch.analyze_screenshot(image)
        return jsonify({"lines": lines})
    except Exception as e:
        # Covers a jsonify() serialization failure too, not just analysis
        # itself - either way the caller should get JSON back, not Flask's
        # default HTML error page.
        return jsonify({"error": f"analysis failed: {e}"}), 500


@app.route("/api/variants/<variant_id>/vocab")
def api_variant_vocab(variant_id):
    """Every tag/topic ever entered for this variant, for autocomplete
    suggestions while typing a new one on any paper of this variant."""
    return jsonify(_read_vocab(variant_id))


@app.route("/api/papers/<paper_id>/questions/<int:q_number>", methods=["PATCH"])
def api_update_question(paper_id, q_number):
    body = request.get_json(force=True, silent=True) or {}
    # user_answer/explanation_html/tags/topics: the review workflow's own
    # fields. question_stem_html/passage_text_html/options: lets the user
    # correct OCR mistakes directly in the question body. A question's
    # shared Directions text lives on its instruction entry instead (see
    # src/instructions.py) - edited via the /instructions/<id> route.
    # needs_review/review_reason: lets the user dismiss a flag once they've
    # manually verified the question is actually fine.
    allowed_flat = {
        "user_answer",
        "explanation_html",
        "tags",
        "topics",
        "question_stem_html",
        "passage_text_html",
        "needs_review",
        "review_reason",
    }
    updates = {k: v for k, v in body.items() if k in allowed_flat}
    options_update = body.get("options")
    if not isinstance(options_update, dict):
        options_update = None
    # match_the_list's table is edited as one object client-side (it
    # already has the full current table in memory), so a full replace is
    # simplest - no per-cell merge logic needed like options above.
    table_update = body.get("table")
    if not isinstance(table_update, dict):
        table_update = None
    if not updates and not options_update and not table_update:
        return jsonify({"error": "no recognized fields in body"}), 400

    with _write_lock:
        paper = _read_paper(paper_id)
        if paper is None:
            return jsonify({"error": "paper not found"}), 404
        question = next((q for q in paper["questions"] if q["q_number"] == q_number), None)
        if question is None:
            return jsonify({"error": "question not found"}), 404
        question.update(updates)
        if options_update:
            question.setdefault("options", {}).update(
                {k: v for k, v in options_update.items() if k in ("a", "b", "c", "d")}
            )
        if table_update:
            question["table"] = table_update
        _write_paper_atomic(paper_id, paper)
        if "tags" in updates or "topics" in updates:
            _remember_vocab(paper.get("variant", paper_id), updates.get("tags"), updates.get("topics"))

    return jsonify(question)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
