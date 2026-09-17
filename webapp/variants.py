"""Registry of question-paper "variants" (layout profiles). Adding a future
variant is adding a dict entry that points at its own run_*_json function;
nothing else in the web layer needs to change.
"""

from src.pipeline import run_pipeline_json
from src.variant_gk import run_pipeline_json_gk

VARIANTS = {
    "cds_english_v1": {
        "label": "CDS Exam - English",
        "run": run_pipeline_json,
    },
    "cds_gk_v1": {
        "label": "CDS Exam - General Knowledge (bilingual, English extracted)",
        "run": run_pipeline_json_gk,
    },
}
