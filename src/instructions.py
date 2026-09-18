"""Deduplicates repeated "Directions :" text across consecutive questions
that share it - parse_questions.py's Builder copies the exact same
`current_directions` string onto every question in a section, so a run of
N questions under one heading stored the identical paragraph N times.
This splits those runs out into a separate top-level `instructions` list
(each entry has its own id, "I1", "I2", ...) and leaves each question with
just an `instruction_id` reference - the Review UI shows/edits it once, in
its place in the reading-order sequence (e.g. Q7, I1, Q8, Q9), instead of
once per question.
"""


def extract_instructions(questions: list[dict]) -> list[dict]:
    """Mutates `questions` in place: replaces each question's
    `section_directions_html` key with `instruction_id` (str or None).
    Returns the new `instructions` list, each
    `{instruction_id, text_html, image, image_regions, applies_to}`."""
    instructions: list[dict] = []
    by_id: dict[str, dict] = {}
    prev_text = None
    current_id = None
    counter = 0

    for q in questions:
        text = q.pop("section_directions_html", "") or ""
        if text:
            if text == prev_text:
                q["instruction_id"] = current_id
                by_id[current_id]["applies_to"].append(q["q_number"])
            else:
                counter += 1
                current_id = f"I{counter}"
                inst = {
                    "instruction_id": current_id,
                    "text_html": text,
                    "image": None,
                    "image_regions": None,
                    "applies_to": [q["q_number"]],
                }
                instructions.append(inst)
                by_id[current_id] = inst
                q["instruction_id"] = current_id
        else:
            q["instruction_id"] = None
            current_id = None
        prev_text = text

    return instructions
