"""In-memory background job tracker for PDF processing. Fine for a
single-user local tool - no queue/worker infrastructure needed.
"""

import threading
import traceback
import uuid
from pathlib import Path

from .variants import VARIANTS

JOBS: dict[str, dict] = {}
_lock = threading.Lock()


def start_job(
    pdf_path: str,
    variant_id: str,
    paper_dir: Path,
    source_filename: str,
    job_name: str | None = None,
    variant_options: dict | None = None,
) -> str:
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "pending",
        "progress": "starting...",
        "paper_id": paper_dir.name,
        "error": None,
    }

    def run():
        JOBS[job_id]["status"] = "running"

        def progress_cb(page_num: int, total_pages: int):
            JOBS[job_id]["progress"] = f"page {page_num}/{total_pages}"

        try:
            variant = VARIANTS[variant_id]
            variant["run"](
                pdf_path,
                str(paper_dir),
                variant_id=variant_id,
                source_filename=source_filename,
                job_name=job_name,
                progress_cb=progress_cb,
                **(variant_options or {}),
            )
            with _lock:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["progress"] = "done"
        except Exception as exc:
            traceback.print_exc()
            with _lock:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = str(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return job_id


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)
