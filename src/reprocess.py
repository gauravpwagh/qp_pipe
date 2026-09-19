"""Re-extract a single question's (or instruction's) text straight from its
current crop image - used by the Review UI's "Reprocess" button, typically
right after "Edit image" fixes a bad crop, so stale OCR text left over from
the original pipeline run doesn't linger.

Unlike src/parse_questions.py's Builder (a state machine over a whole
multi-page reading-order stream: question boundaries, Directions/Passage
headers, column layout), this assumes the image IS already exactly one
question or instruction - much simpler, no boundary detection needed, just
"split stem from options (if any) and mark underlines".
"""

import numpy as np
from PIL import Image

from .match_list import reconstruct as reconstruct_match_list
from .ocr import ocr_image
from .paired_table import detect as detect_paired_table, reconstruct as reconstruct_paired_table
from .parse_questions import DIRECTIONS_RE, OPTION_LETTER_FIX, OPTION_RE, QUESTION_START_RE, _starts_new_line
from .reading_order import order_page
from .underline import detect_underlined_words, mark_underlines_in_text


def _ordered_lines(image: Image.Image):
    gray = np.array(image.convert("L"))
    boxes = ocr_image(gray)
    lines = order_page(boxes, gray.shape[1])
    for line in lines:
        line.underlined_words = detect_underlined_words(gray, line)
    return lines, boxes


def _join_with_breaks(entries: list[tuple[str, frozenset]]) -> str:
    """entries: [(text, underlined_words), ...]. Mirrors
    parse_questions.Builder._join_field_with_breaks: a line matching one of
    _NEW_LINE_TRIGGERS (a numbered sub-statement, a trailer phrase like
    "Select the answer...") starts a new displayed line instead of running
    into the previous one."""
    parts = []
    for i, (text, underlined) in enumerate(entries):
        marked = mark_underlines_in_text(text, underlined)
        if i == 0:
            parts.append(marked)
        elif _starts_new_line(text):
            parts.append("<br><br>" + marked)
        else:
            parts.append(" " + marked)
    return "".join(parts).strip()


def reprocess_standard(image: Image.Image, q_number: int | None = None) -> dict:
    """Returns {question_stem_html, options: {a,b,c,d}}."""
    lines, _ = _ordered_lines(image)

    stem_entries: list[tuple[str, frozenset]] = []
    option_entries: dict[str, list[tuple[str, frozenset]]] = {"a": [], "b": [], "c": [], "d": []}
    current_option: str | None = None

    for line in lines:
        text = line.text.strip()
        if not text:
            continue

        if not stem_entries and current_option is None and q_number is not None:
            m = QUESTION_START_RE.match(text)
            if m and int(m.group(1)) == q_number:
                text = m.group(2).strip()

        opt_match = OPTION_RE.match(text)
        if opt_match:
            letter = OPTION_LETTER_FIX.get(opt_match.group(1).lower(), opt_match.group(1).lower())
            current_option = letter
            option_entries[letter].append((opt_match.group(2).strip(), line.underlined_words))
            continue

        if current_option:
            option_entries[current_option].append((text, line.underlined_words))
        else:
            stem_entries.append((text, line.underlined_words))

    return {
        "question_stem_html": _join_with_breaks(stem_entries),
        "options": {letter: _join_with_breaks(entries) for letter, entries in option_entries.items()},
    }


def reprocess_match_list(image: Image.Image, q_number: int | None = None) -> dict:
    """Returns {table, problems}. reconstruct() only ever compares box
    positions relative to each other within the given set, never against
    absolute page dimensions - so it works unchanged on a standalone crop's
    own OCR boxes, no adaptation needed."""
    gray = np.array(image.convert("L"))
    boxes = ocr_image(gray)
    table, problems = reconstruct_match_list(boxes, q_number)
    return {"table": table, "problems": problems}


def build_table(image: Image.Image, question_type: str, q_number: int | None = None) -> tuple[dict, list[str]]:
    """Builds the table for a question the user has just switched to a
    table-bearing type - unlike reprocess_question, leaves the stem/options
    alone. Returns (table, problems)."""
    if question_type == "match_the_list":
        result = reprocess_match_list(image, q_number)
        return result["table"], result["problems"]
    if question_type == "paired_table":
        boxes = ocr_image(np.array(image.convert("L")))
        # The user chose this type, so build on a best-effort basis even when
        # the pipeline's own (deliberately strict) auto-detection wouldn't
        # have picked it - just say so.
        table, problems = reconstruct_paired_table(boxes, q_number)
        if not detect_paired_table(boxes):
            problems = ["paired-table layout wasn't clearly detected in this image - verify the table"] + problems
        if not table["rows"]:
            table = {"headers": ["", ""], "rows": []}
            problems.append("no rows found - fill the table in by hand")
        return table, problems
    raise ValueError(f"{question_type!r} has no table")


def reprocess_instruction(image: Image.Image) -> dict:
    """Returns {text_html}."""
    lines, _ = _ordered_lines(image)
    entries = []
    stripped_prefix = False
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if not stripped_prefix:
            # The crop typically still includes the literal "Directions :"
            # heading text visible in the source - strip it once, same as
            # the original full-page parser does, rather than leaving it
            # baked into the extracted body text.
            m = DIRECTIONS_RE.match(text)
            if m:
                text = m.group(1).strip()
            stripped_prefix = True
            if not text:
                continue
        entries.append((text, line.underlined_words))
    return {"text_html": _join_with_breaks(entries)}


def reprocess_question(image: Image.Image, question_type: str, q_number: int) -> dict:
    """Dispatches on the question's existing type - reprocess refreshes
    that type's content, it never re-classifies the type itself (a fluke
    OCR read on a tight crop could otherwise flip a standard question into
    something else, a confusing side effect)."""
    if question_type == "match_the_list":
        result = reprocess_match_list(image, q_number)
        problems = result["problems"]
        return {
            "table": result["table"],
            "needs_review": bool(problems),
            "review_reason": "; ".join(problems),
        }

    result = reprocess_standard(image, q_number)
    missing = [letter for letter, text in result["options"].items() if not text]
    return {
        "question_stem_html": result["question_stem_html"],
        "options": result["options"],
        "needs_review": bool(missing),
        "review_reason": f"reprocess: missing option(s) {', '.join(missing)}" if missing else "",
    }
