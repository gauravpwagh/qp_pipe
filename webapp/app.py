"""Flask app: upload a PDF, run the pipeline as a background job, review
results (question image + text + answer choice + rich-text explanation/tags)
in the browser.
"""

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from . import jobs
from .variants import VARIANTS

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "papers"
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")

_write_lock = threading.Lock()


def _slugify(name: str) -> str:
    stem = Path(name).stem
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return slug or "paper"


def _paper_dir(paper_id: str) -> Path:
    return DATA_DIR / paper_id


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
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    paper_id = f"{variant_id}-{_slugify(source_filename)}-{timestamp}"
    paper_dir = _paper_dir(paper_id)
    paper_dir.mkdir(parents=True, exist_ok=True)

    upload_path = UPLOAD_DIR / f"{paper_id}.pdf"
    file.save(upload_path)

    job_id = jobs.start_job(str(upload_path), variant_id, paper_dir, source_filename)
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
    for child in sorted(DATA_DIR.iterdir(), reverse=True):
        paper = _read_paper(child.name)
        if paper is None:
            continue
        summaries.append(
            {
                "paper_id": paper["paper_id"],
                "label": paper.get("source_filename", paper["paper_id"]),
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


@app.route("/api/papers/<paper_id>/images/<path:filename>")
def api_paper_image(paper_id, filename):
    return send_from_directory(_paper_dir(paper_id) / "images", filename)


@app.route("/api/papers/<paper_id>/questions/<int:q_number>", methods=["PATCH"])
def api_update_question(paper_id, q_number):
    body = request.get_json(force=True, silent=True) or {}
    allowed = {"user_answer", "explanation_html", "tags"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({"error": "no recognized fields in body"}), 400

    with _write_lock:
        paper = _read_paper(paper_id)
        if paper is None:
            return jsonify({"error": "paper not found"}), 404
        question = next((q for q in paper["questions"] if q["q_number"] == q_number), None)
        if question is None:
            return jsonify({"error": "question not found"}), 404
        question.update(updates)
        _write_paper_atomic(paper_id, paper)

    return jsonify(question)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
