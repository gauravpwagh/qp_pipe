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
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from . import jobs
from .variants import VARIANTS
from src import latex_ocr, screenshot_stitch

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
        return json.load(f)


def _write_paper_atomic(paper_id: str, paper: dict) -> None:
    path = _paper_dir(paper_id) / "paper.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(paper, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
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
    # fields. question_stem_html/section_directions_html/passage_text_html/
    # options: lets the user correct OCR mistakes directly in the question
    # body.
    allowed_flat = {
        "user_answer",
        "explanation_html",
        "tags",
        "topics",
        "question_stem_html",
        "section_directions_html",
        "passage_text_html",
    }
    updates = {k: v for k, v in body.items() if k in allowed_flat}
    options_update = body.get("options")
    if not isinstance(options_update, dict):
        options_update = None
    if not updates and not options_update:
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
        _write_paper_atomic(paper_id, paper)
        if "tags" in updates or "topics" in updates:
            _remember_vocab(paper.get("variant", paper_id), updates.get("tags"), updates.get("topics"))

    return jsonify(question)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
