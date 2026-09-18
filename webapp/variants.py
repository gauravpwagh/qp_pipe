"""Registry of question-paper "variants" (layout profiles). Adding a future
variant is adding a dict entry that points at its own run_*_json function;
nothing else in the web layer needs to change.

A variant can optionally declare "fields": extra inputs the Process tab
should collect from the user beyond the PDF/job name (e.g. a booklet-
specific page boundary that isn't safe to hardcode - see
src/variant_ndana_gat.py). Each field is {name, label, type: "number",
required}; webapp/app.py's /api/process reads them from the form by name,
converts to int, and forwards them as extra keyword arguments to the
variant's own run_*_json function - which must accept a same-named
parameter.
"""

from src.pipeline import run_pipeline_json
from src.variant_gk import run_pipeline_json_gk
from src.variant_ndana_gat import run_pipeline_json_ndana_gat

VARIANTS = {
    "cds_english_v1": {
        "label": "CDS Exam - English",
        "run": run_pipeline_json,
        "fields": [],
    },
    "cds_gk_v1": {
        "label": "CDS Exam - General Knowledge (bilingual, English extracted)",
        "run": run_pipeline_json_gk,
        "fields": [],
    },
    "ndana_gat_v1": {
        "label": "NDA & NA - General Ability Test (Part A English, Part B bilingual)",
        "run": run_pipeline_json_ndana_gat,
        "fields": [
            {
                "name": "part_a_last_page",
                "label": "Last page of Part A (all-English)",
                "type": "number",
                "required": True,
            },
            {
                "name": "part_b_last_page",
                "label": "Last page of Part B (Hindi/English alternating)",
                "type": "number",
                "required": True,
            },
        ],
    },
}
