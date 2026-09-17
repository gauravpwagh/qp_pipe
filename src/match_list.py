"""Reconstruct the List I / List II / Code-answer-table structure for
"match the following" questions, operating on the raw OCR boxes confined to
one question's bounding box (see bbox.py).

Layout (confirmed by direct visual inspection of a sample page): each such
question is a single column (our usual LEFT or RIGHT half). Within that
column, top to bottom: a "List I (Word/Term)" / "List II (Meaning)" header
row, four List-I items (A-D) each paired on the same rows with four List-II
items (1-4) in a second sub-column, then a "Code :" label, then a small
grid: a header row "A B C D" followed by four rows "(a) n n n n" etc.
"""

import re

from .ocr import Box
from .parse_questions import OPTION_LETTER_FIX, OPTION_RE
from .reading_order import _cluster_rows

LIST1_ITEM_RE = re.compile(r"^([A-D])\s*[\.\-_,:]?\s*(.*)$")
LIST2_ITEM_RE = re.compile(r"^([1-4])\s*[\.\-_,:]?\s*(.*)$")
CODE_LABEL_RE = re.compile(r"^code\b", re.IGNORECASE)
HEADER_WORDS = {"list", "word", "term", "meaning", "code"}
NUMBER_RE = re.compile(r"\d+")


def _row_text(row: list[Box]) -> str:
    return " ".join(b.text for b in sorted(row, key=lambda b: b.x0)).strip()


def _looks_like_header(text: str) -> bool:
    stripped = re.sub(r"[^a-zA-Z]", "", text).lower()
    return any(w in stripped for w in HEADER_WORDS) and len(stripped) < 20


def _split_columns(boxes: list[Box]) -> tuple[list[Box], list[Box]]:
    """Split a box set into two x-clusters at the single largest x0 gap."""
    if len(boxes) < 2:
        return list(boxes), []
    xs = sorted(set(b.x0 for b in boxes))
    if len(xs) < 2:
        return list(boxes), []
    gap_idx = max(range(len(xs) - 1), key=lambda i: xs[i + 1] - xs[i])
    threshold = (xs[gap_idx] + xs[gap_idx + 1]) / 2
    left = [b for b in boxes if b.x0 <= threshold]
    right = [b for b in boxes if b.x0 > threshold]
    return left, right


def _extract_items(boxes: list[Box], item_re: re.Pattern, labels_order: list[str]) -> tuple[list[dict], list[str]]:
    """Returns (items, notes). OCR occasionally drops the tiny single-glyph
    label box for an item entirely (seen in practice: "Tabernacle" detected
    with no accompanying "A" box; separately, an undetected "4." merged that
    item's text onto the end of item "3"'s). Recovery: build segments as
    normal (a label match starts a new one; anything else extends the
    current one, or starts a label-less leading segment if none is open
    yet); a label-less segment usually IS a genuine dropped-label item, but
    one may also be lurking merged inside a labelled segment - if we're
    still short of `labels_order`'s full count after that, split off the
    trailing chunk after the largest internal row-gap in whichever segment
    has one (a real inter-item gap is reliably bigger than an item's own
    line-wrap gaps), repeating only as many times as still needed. Finally,
    walk segments in order assigning each label-less one the next label
    `labels_order` says should be there."""
    rows = sorted(_cluster_rows(boxes), key=lambda row: min(b.y0 for b in row))
    segments: list[dict] = []  # {"label": str|None, "rows": [(text, y0), ...]}
    current: dict | None = None
    for row in rows:
        text = _row_text(row)
        if not text or _looks_like_header(text):
            continue
        y0 = min(b.y0 for b in row)
        m = item_re.match(text)
        if m:
            if current:
                segments.append(current)
            label = m.group(1).upper() if item_re is LIST1_ITEM_RE else m.group(1)
            current = {"label": label, "rows": [(m.group(2).strip(), y0)]}
        elif current:
            current["rows"].append((text, y0))
        else:
            current = {"label": None, "rows": [(text, y0)]}
    if current:
        segments.append(current)

    notes: list[str] = []
    found_labels = {s["label"] for s in segments if s["label"] is not None}
    missing_count = len(labels_order) - len(found_labels)
    unlabeled_count = sum(1 for s in segments if s["label"] is None)
    needed_splits = max(0, missing_count - unlabeled_count)

    for _ in range(needed_splits):
        best = None  # (gap, seg_index, split_after_row_index)
        for idx, seg in enumerate(segments):
            if len(seg["rows"]) < 2:
                continue
            for i in range(len(seg["rows"]) - 1):
                gap = seg["rows"][i + 1][1] - seg["rows"][i][1]
                if best is None or gap > best[0]:
                    best = (gap, idx, i)
        if best is None:
            break
        _, idx, i = best
        seg = segments[idx]
        tail_rows = seg["rows"][i + 1 :]
        seg["rows"] = seg["rows"][: i + 1]
        segments.insert(idx + 1, {"label": None, "rows": tail_rows})

    items: list[dict] = []
    label_ptr = 0
    inferrable_budget = missing_count  # only ever infer as many labels as are
    # actually missing - otherwise a genuinely unlabeled leading segment
    # (e.g. an intro sentence like "Match List I with List II..." sitting
    # above the real column headers, when all 4 real items already OCR'd
    # fine) would get wrongly consumed as a fake extra item.
    for seg in segments:
        text = " ".join(t for t, _ in seg["rows"]).strip()
        if not text:
            continue
        if seg["label"] is not None:
            items.append({"label": seg["label"], "text": text})
            if seg["label"] in labels_order:
                label_ptr = labels_order.index(seg["label"]) + 1
        elif label_ptr < len(labels_order) and inferrable_budget > 0:
            items.append({"label": labels_order[label_ptr], "text": text})
            notes.append(f"item {labels_order[label_ptr]!r} label not detected by OCR - inferred from position, verify text")
            label_ptr += 1
            inferrable_budget -= 1
        else:
            notes.append(f"discarded unlabeled text (not a missing item): {text!r}")

    return items, notes


def _extract_code_table(boxes: list[Box]) -> tuple[dict, list[str]]:
    """Extract the 4x4 answer grid. Digit detection in this small grid is the
    least reliable OCR spot we've seen (empirically, "1" specifically is
    often missed even at high render resolution) - so rather than just
    reading digits left-to-right per row (which silently misaligns columns
    the moment one is missing), we track each digit's actual x-position and
    assign it to the nearest of 4 column positions inferred from ALL rows
    combined. If exactly one column ends up missing in a row and the other
    three values are 3 distinct digits from {1,2,3,4}, the 4th is inferred:
    every option row observed across this booklet's match-the-list
    questions is a permutation of 1-4, so the missing value is uniquely
    determined."""
    rows = sorted(_cluster_rows(boxes), key=lambda row: min(b.y0 for b in row))
    notes: list[str] = []
    candidate_rows: list[list] = []  # each: the row's boxes, header/empty rows excluded
    for row in rows:
        text = _row_text(row)
        if not text:
            continue
        alpha = re.sub(r"[^A-Za-z]", "", text).upper()
        has_digit = bool(NUMBER_RE.search(text))
        # the "A B C D" header row never has digits and is made up only of
        # those 4 letters - but OCR sometimes drops one of them, so match on
        # "alpha-only, subset of ABCD" rather than requiring all 4 present.
        # A real data row always carries digit values, so this can't
        # misfire on one (which was the previous bug: "(a) 3 2" has alpha
        # residue "A", a subset of ABCD, but also has digits).
        if _looks_like_header(text) or (not has_digit and alpha and set(alpha) <= set("ABCD")):
            continue
        candidate_rows.append(row)

    # The exam always lists the 4 answer-grid rows in fixed (a),(b),(c),(d)
    # order - so rather than trust the tiny option-letter glyph's own OCR
    # (observed, on this exact page, to occasionally garble "(b)"/"(c)" into
    # single stray characters with bizarre bounding boxes and get dropped
    # entirely), assign letters by row position. A row whose OWN marker did
    # OCR cleanly is used to sanity-check/override that position if it
    # disagrees, but position is the primary signal.
    expected_letters = ["a", "b", "c", "d"]
    if len(candidate_rows) != 4:
        notes.append(f"expected 4 answer-grid rows, found {len(candidate_rows)}")

    raw_rows: list[dict] = []  # {"option": str, "digits": [(x0, text), ...]}
    for i, row in enumerate(candidate_rows[:4]):
        letter = expected_letters[i]
        sorted_row = sorted(row, key=lambda b: b.x0)
        first = sorted_row[0]
        opt_match = OPTION_RE.match(first.text.strip())
        rest_boxes = sorted_row
        if opt_match:
            found_letter = OPTION_LETTER_FIX.get(opt_match.group(1).lower(), opt_match.group(1).lower())
            if found_letter != letter:
                notes.append(f"answer-grid row {i + 1}: marker read as '({found_letter})' but expected '({letter})' by position - using position")
            rest_boxes = sorted_row[1:]

        digits: list[tuple[float, str]] = []
        for b in rest_boxes:
            nums = NUMBER_RE.findall(b.text)
            if len(nums) == 1:
                digits.append((b.x0, nums[0]))
            elif len(nums) > 1:
                step = (b.x1 - b.x0) / len(nums)
                digits.extend((b.x0 + i2 * step, n) for i2, n in enumerate(nums))
        raw_rows.append({"option": letter, "digits": digits})

    all_x = sorted(x for r in raw_rows for x, _ in r["digits"])
    thresholds: list[float] = []
    if len(all_x) >= 4:
        gap_indices = sorted(range(len(all_x) - 1), key=lambda i: all_x[i + 1] - all_x[i], reverse=True)[:3]
        thresholds = sorted((all_x[i] + all_x[i + 1]) / 2 for i in gap_indices)

    def column_of(x0: float) -> int:
        col = 0
        for t in thresholds:
            if x0 > t:
                col += 1
        return col

    data_rows = []
    for r in raw_rows:
        cols: dict[int, str] = {}
        if thresholds:
            for x0, text in r["digits"]:
                c = column_of(x0)
                if 0 <= c < 4 and c not in cols:
                    cols[c] = text
        else:
            for i, (_, text) in enumerate(sorted(r["digits"])[:4]):
                cols[i] = text

        missing = [c for c in range(4) if c not in cols]
        if len(missing) == 1:
            present = [cols[c] for c in range(4) if c in cols]
            if len(set(present)) == 3 and set(present) <= {"1", "2", "3", "4"}:
                inferred = next(iter({"1", "2", "3", "4"} - set(present)))
                cols[missing[0]] = inferred
                notes.append(
                    f"Code table row ({r['option']}): column {'ABCD'[missing[0]]} not detected by OCR - "
                    f"inferred as {inferred} (every row here is a permutation of 1-4), verify against source"
                )

        values = [cols.get(c) for c in range(4)]
        data_rows.append({"option": r["option"], "values": values})
        if any(v is None for v in values):
            notes.append(f"Code table row ({r['option']}): missing value(s) - {values}")

    return {"headers": ["A", "B", "C", "D"], "rows": data_rows}, notes


def reconstruct(boxes: list[Box], q_number: int | None = None) -> tuple[dict, list[str]]:
    """Returns (table_dict, problems). `problems` is a list of human-readable
    issues (wrong item/value counts etc.) for needs_review flagging.

    `q_number`, if given, lets us drop the question's own leading number
    marker box (e.g. "105."), which sits right at this block's top edge and
    can get swept in by the bbox padding - dropping it here (by exact value,
    not "any bare digit") avoids it masquerading as a stray List-I item
    without also eating a genuine single-digit List-II label like "2_" that
    OCR happened to detect as its own box.
    """
    problems: list[str] = []
    if q_number is not None:
        marker_re = re.compile(rf"^0*{q_number}\s*[\.\-_,:]?\s*$")
        boxes = [b for b in boxes if not marker_re.match(b.text.strip())]
    code_idx = next((i for i, b in enumerate(boxes) if CODE_LABEL_RE.match(b.text.strip())), None)
    if code_idx is None:
        return {}, ["'Code' label not found - could not locate the answer grid"]

    code_y0 = boxes[code_idx].y0
    list_boxes = [b for b in boxes if b.y0 < code_y0]
    code_boxes = [b for b in boxes if b.y0 >= code_y0]

    left_col, right_col = _split_columns(list_boxes)
    list1, list1_notes = _extract_items(left_col, LIST1_ITEM_RE, ["A", "B", "C", "D"])
    list2, list2_notes = _extract_items(right_col, LIST2_ITEM_RE, ["1", "2", "3", "4"])
    code_table, code_notes = _extract_code_table(code_boxes)

    problems.extend(f"List I: {n}" for n in list1_notes)
    problems.extend(f"List II: {n}" for n in list2_notes)
    if len(list1) != 4:
        problems.append(f"List I: expected 4 items, found {len(list1)}")
    if len(list2) != 4:
        problems.append(f"List II: expected 4 items, found {len(list2)}")
    if len(code_table["rows"]) != 4:
        problems.append(f"Code table: expected 4 rows, found {len(code_table['rows'])}")
    problems.extend(code_notes)

    return {"list1": list1, "list2": list2, "code_table": code_table}, problems
