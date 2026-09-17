"""Registry of question-paper "variants" (layout profiles). One exists
today - the CDS English booklet layout built this session. Adding a future
variant is adding a dict entry that points at its own run_*_json function;
nothing else in the web layer needs to change.
"""

from src.pipeline import run_pipeline_json

VARIANTS = {
    "cds_english_v1": {
        "label": "CDS Exam - English",
        "run": run_pipeline_json,
    },
}
