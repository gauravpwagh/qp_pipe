"""Turn a linear, reading-order stream of Lines (spanning all content pages)
into structured question records.
"""

import re
from dataclasses import dataclass, field

from .reading_order import Line
from .underline import mark_underlines_in_text

OPTION_RE = re.compile(r"^\(?\s*([abcd6])\s*\)\s*(.*)$", re.IGNORECASE)
# OCR frequently misreads "(b)" as "(6)" in this font; normalize on match.
OPTION_LETTER_FIX = {"6": "b"}

QUESTION_START_RE = re.compile(r"^(\d{1,3})\s*[\.\-_,:]\s*(.*)$")
DIRECTIONS_RE = re.compile(r"^directions?\s*[:.]?\s*(.*)$", re.IGNORECASE)
PASSAGE_RE = re.compile(r"^passage\b\s*(.*)$", re.IGNORECASE)

FOOTER_Y_FRAC = 0.90  # lines starting below this fraction of page height are header/footer noise
PURE_NUMBER_RE = re.compile(r"^\d{1,4}$")

TOTAL_QUESTIONS = 120
FIELD_NAMES = ("stem", "a", "b", "c", "d")


@dataclass
class Question:
    q_number: int
    page: int
    section_directions: str = ""
    passage_label: str = ""
    passage_text: str = ""
    question_type: str = "standard"
    question_stem: str = ""
    option_a: str = ""
    option_b: str = ""
    option_c: str = ""
    option_d: str = ""
    has_underline: bool = False
    ocr_confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    raw_lines: list[str] = field(default_factory=list)


class Fragment:
    __slots__ = ("text", "y0", "conf", "underlined_words")

    def __init__(self, text: str, y0: float, conf: float, underlined_words: frozenset = frozenset()):
        self.text = text
        self.y0 = y0
        self.conf = conf
        self.underlined_words = underlined_words


class Builder:
    """Accumulates a question's stem/options as (text, y0) fragments so that,
    if OCR drops a question-number token and the next question's stem gets
    appended onto our last field by mistake, we can recover the split point
    from the one large vertical gap (new questions are visually spaced apart
    more than continuation lines or options are)."""

    def __init__(self, q_number: int, page: int, directions: str, passage_label: str, passage_text: str):
        self.q_number = q_number
        self.page = page
        self.directions = directions
        self.passage_label = passage_label
        self.passage_text = passage_text
        self.fields: dict[str, list[Fragment]] = {k: [] for k in FIELD_NAMES}
        self.active = "stem"
        self.raw_lines: list[str] = []
        self.forced_review_reason = ""

    def add(self, field_name: str, text: str, y0: float, conf: float, underlined_words: frozenset = frozenset()):
        self.active = field_name
        self.fields[field_name].append(Fragment(text, y0, conf, underlined_words))

    def append_active(self, text: str, y0: float, conf: float, underlined_words: frozenset = frozenset()):
        self.fields[self.active].append(Fragment(text, y0, conf, underlined_words))

    def is_field_set(self, field_name: str) -> bool:
        return len(self.fields[field_name]) > 0

    def split_active_on_largest_gap(self) -> str:
        """Pop trailing fragments off the active field, splitting at its largest
        internal vertical gap, and return them joined as text (for reassignment
        to a newly-discovered next question). Mutates self.fields[self.active]
        in place to keep only the fragments before the split."""
        frags = self.fields[self.active]
        if len(frags) < 2:
            # nothing to split on - hand back nothing, leave the single fragment
            # (if any) where it is; caller will just get an empty pending stem
            return ""
        gaps = [(frags[i + 1].y0 - frags[i].y0, i) for i in range(len(frags) - 1)]
        biggest_gap, split_after = max(gaps)
        kept, moved = frags[: split_after + 1], frags[split_after + 1 :]
        self.fields[self.active] = kept
        return " ".join(f.text for f in moved)

    def mean_conf(self) -> float:
        all_frags = [f for frags in self.fields.values() for f in frags]
        return sum(f.conf for f in all_frags) / len(all_frags) if all_frags else 0.0

    @staticmethod
    def _join_field(frags: list[Fragment]) -> str:
        return " ".join(mark_underlines_in_text(f.text, f.underlined_words) for f in frags).strip()

    def to_question(self) -> Question:
        q = Question(
            q_number=self.q_number,
            page=self.page,
            section_directions=self.directions,
            passage_label=self.passage_label,
            passage_text=self.passage_text,
            question_stem=self._join_field(self.fields["stem"]),
            option_a=self._join_field(self.fields["a"]),
            option_b=self._join_field(self.fields["b"]),
            option_c=self._join_field(self.fields["c"]),
            option_d=self._join_field(self.fields["d"]),
            ocr_confidence=self.mean_conf(),
            raw_lines=self.raw_lines,
        )
        if self.forced_review_reason:
            q.needs_review = True
            q.review_reason = self.forced_review_reason
        if not all([q.option_a, q.option_b, q.option_c, q.option_d]):
            q.needs_review = True
            q.review_reason = (q.review_reason + "; missing option(s)").strip("; ")
        return q


def _is_footer(line: Line, page_height: float) -> bool:
    if line.y0 < FOOTER_Y_FRAC * page_height:
        return False
    text = line.text.strip()
    if PURE_NUMBER_RE.match(text):
        return True
    if len(text) <= 20 and "-" in text and text.upper() == text:
        return True  # booklet code footer, e.g. "BFVS-F-GNE" / "A - BFVS-F-GNE"
    return False


def parse_document(pages: dict[int, tuple[list[Line], float]]) -> tuple[list[Question], list[str]]:
    """pages: {page_number: (ordered_lines, page_height)} for content pages only,
    in the order they should be read (i.e. already sorted by page number).

    Returns (questions, warnings).
    """
    warnings: list[str] = []
    questions: list[Question] = []

    current_directions = ""
    current_passage_label = ""
    current_passage_text = ""

    builder: Builder | None = None
    collecting: str | None = None  # "directions" | "passage" | None
    pending_stem = ""  # recovered text belonging to a question whose number OCR dropped

    expected_num = 1

    def finalize_current():
        nonlocal builder
        if builder is not None:
            questions.append(builder.to_question())
            builder = None

    def start_new_question(page_num: int, stem_prefix: str, y0: float, conf: float, inferred: bool = False, underlined_words: frozenset = frozenset()):
        nonlocal builder, expected_num
        finalize_current()
        builder = Builder(expected_num, page_num, current_directions, current_passage_label, current_passage_text)
        if stem_prefix:
            builder.add("stem", stem_prefix.strip(), y0=y0, conf=conf, underlined_words=underlined_words)
        if inferred:
            builder.forced_review_reason = "question number not detected by OCR; number inferred from sequence"
        expected_num += 1

    for page_num in sorted(pages.keys()):
        lines, page_height = pages[page_num]
        for line in lines:
            if _is_footer(line, page_height):
                continue
            text = line.text.strip()
            if not text:
                continue

            q_match = QUESTION_START_RE.match(text)
            if q_match and int(q_match.group(1)) == expected_num:
                stem_prefix = (pending_stem + " " + q_match.group(2)).strip()
                pending_stem = ""
                start_new_question(page_num, stem_prefix, line.y0, line.conf, underlined_words=line.underlined_words)
                builder.raw_lines.append(text)
                collecting = None
                continue

            dir_match = DIRECTIONS_RE.match(text)
            if dir_match:
                finalize_current()
                current_directions = dir_match.group(1)
                collecting = "directions"
                continue

            pas_match = PASSAGE_RE.match(text)
            if pas_match and len(text) < 40:
                finalize_current()
                current_passage_label = text
                current_passage_text = ""
                collecting = "passage"
                continue

            opt_match = OPTION_RE.match(text) if builder is not None else None
            if opt_match:
                letter = OPTION_LETTER_FIX.get(opt_match.group(1).lower(), opt_match.group(1).lower())
                if builder.is_field_set(letter):
                    # This exact option letter was already set once for the current
                    # question - a single question can't have two "(a)"s, so OCR must
                    # have dropped the next question's number token entirely. Recover
                    # the split point from the largest vertical gap in whatever field
                    # is currently active (almost always the trailing option), and use
                    # everything after that gap as the new question's stem.
                    recovered_stem = builder.split_active_on_largest_gap()
                    start_new_question(page_num, recovered_stem, line.y0, line.conf, inferred=True)
                builder.add(letter, opt_match.group(2), line.y0, line.conf, line.underlined_words)
                builder.raw_lines.append(text)
                continue

            # continuation line
            if builder is not None:
                builder.raw_lines.append(text)
                builder.append_active(text, line.y0, line.conf, line.underlined_words)
            elif collecting == "directions":
                current_directions = (current_directions + " " + text).strip()
            elif collecting == "passage":
                current_passage_text = (current_passage_text + " " + text).strip()
            # else: stray text before first Directions block - ignore

    finalize_current()

    # sub-type detection + match-the-list flagging (best-effort, on the assembled text)
    for q in questions:
        blob = (q.question_stem + " " + q.option_a + " " + q.option_b + " " + q.option_c + " " + q.option_d).lower()
        if "list i" in blob and "list ii" in blob:
            q.question_type = "match_the_list"
            q.needs_review = True
            q.review_reason = (q.review_reason + "; match-the-list layout, raw text may be jumbled - verify against source PDF").strip("; ")
        elif re.search(r"\bs1\s*[:.]", blob) and re.search(r"\bs6\s*[:.]", blob):
            q.question_type = "para_jumble"
        elif re.search(r"\bpassage\b", blob) or q.passage_text:
            q.question_type = "comprehension"
        elif re.search(r"the second sentence", blob):
            q.question_type = "sentence_relation"

    # sequence QA
    seen = {q.q_number for q in questions}
    missing = [n for n in range(1, TOTAL_QUESTIONS + 1) if n not in seen]
    if missing:
        warnings.append(f"Missing question numbers: {missing}")
    dupes = [n for n in seen if sum(1 for q in questions if q.q_number == n) > 1]
    if dupes:
        warnings.append(f"Duplicate question numbers: {dupes}")

    return questions, warnings
