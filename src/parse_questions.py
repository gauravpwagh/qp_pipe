"""Turn a linear, reading-order stream of Lines (spanning all content pages)
into structured question records.
"""

import re
from dataclasses import dataclass, field

from .patterns import (
    DIRECTIONS_RE,
    OPTION_LETTER_FIX,
    OPTION_RE,
    PASSAGE_RE,
    MATCH_LIST_END_RE,
    MATCH_LIST_START_RE,
    QUESTION_START_LOOSE_RE,
    QUESTION_START_RE,
    SPOT_ERROR_INTRO_END_RE,
    SPOT_ERROR_INTRO_RE,
    STATEMENTS_END_RE,
    STATEMENTS_INTRO_RE,
)
from .reading_order import Line
from .underline import mark_underlines_in_text

FOOTER_Y_FRAC = 0.90  # lines starting below this fraction of page height are header/footer noise
PURE_NUMBER_RE = re.compile(r"^\d{1,4}$")

# A stem often embeds its own numbered/lettered sub-statements (plain "1. ...",
# para-jumble "S1: ..." fixed sentences, "P : ..." code-labelled sentences) and
# always ends with one of a small fixed set of trailer phrases. Each of these
# starts its own printed line in the source - when we display the stem we want
# to preserve that, not flatten everything into one run-on paragraph.
_NEW_LINE_TRIGGERS = [
    re.compile(r"^\d{1,2}\s*[\.\-_]\s*\S"),  # "1. ...", "2_ ...", "3- ..."
    re.compile(r"^[IVX]{1,4}\s*[\.\-_,:]\s*\S"),  # "I. ...", "II. ...", "III. ..." (roman-numeral sub-statements)
    re.compile(r"^S\d\s*[:.]\s*\S", re.IGNORECASE),  # "S1: ...", "S6. ..."
    re.compile(r"^[PQRS]\s*:\s*\S"),  # "P : ...", "Q : ..." (para-jumble labels)
    re.compile(
        r"^(select the answer|which of the (?:above\s+)?statements?|how many of the above|which of the above)",
        re.IGNORECASE,
    ),
]


def _starts_new_line(text: str) -> bool:
    return any(p.match(text) for p in _NEW_LINE_TRIGGERS)

# How many consecutive question numbers a single resync (see
# parse_document's RESYNC comment) is allowed to jump over in one go -
# generous enough for any realistic run of OCR misses, conservative
# enough that a stray number elsewhere in running text (extremely
# unlikely to land more than this far past the true expected number)
# can't get mistaken for a legitimate new question.
RESYNC_WINDOW = 10

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
    start_y: float = 0.0
    start_kind: str = ""  # "LEFT" / "RIGHT" / "FULL" - the column the question's first line was in
    regions: list[dict] = field(default_factory=list)  # [{"page","kind","x0","y0","x1","y1"}, ...] -
    # usually one, but a question's content can overflow from the bottom of
    # one column to the top of the next on the same page, giving two.


class Fragment:
    __slots__ = ("text", "y0", "y1", "conf", "underlined_words", "x0", "x1", "page", "kind")

    def __init__(
        self,
        text: str,
        y0: float,
        conf: float,
        underlined_words: frozenset = frozenset(),
        x0: float = 0.0,
        x1: float = 0.0,
        y1: float = 0.0,
        page: int = 0,
        kind: str = "",
    ):
        self.text = text
        self.y0 = y0
        self.y1 = y1 or y0
        self.conf = conf
        self.underlined_words = underlined_words
        self.x0 = x0
        self.x1 = x1
        self.page = page
        self.kind = kind


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
        self.start_y = 0.0
        self.start_kind = ""
        self.suppress_options = False
        self.suppress_new_question = False

    def add(self, field_name: str, text: str, line: Line):
        self.active = field_name
        self.fields[field_name].append(
            Fragment(text, line.y0, line.conf, line.underlined_words, line.x0, line.x1, line.y1, line.page, line.kind)
        )

    def append_active(self, text: str, line: Line):
        self.fields[self.active].append(
            Fragment(text, line.y0, line.conf, line.underlined_words, line.x0, line.x1, line.y1, line.page, line.kind)
        )

    def compute_regions(self) -> list[dict]:
        """Union the bounding rects of every fragment that went into this
        question, grouped by (page, kind) - almost always one region, but a
        question whose content overflows from the bottom of one column to
        the top of the next (same page) produces two, and the caller (image
        cropping) can render both instead of silently cutting one off."""
        groups: dict[tuple[int, str], list[Fragment]] = {}
        for frags in self.fields.values():
            for f in frags:
                groups.setdefault((f.page, f.kind), []).append(f)
        regions = []
        for (page, kind), frags in groups.items():
            regions.append(
                {
                    "page": page,
                    "kind": kind,
                    "x0": min(f.x0 for f in frags),
                    "y0": min(f.y0 for f in frags),
                    "x1": max(f.x1 for f in frags),
                    "y1": max(f.y1 for f in frags),
                }
            )
        return regions

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

    @staticmethod
    def _join_field_with_breaks(frags: list[Fragment]) -> str:
        """Like _join_field, but a fragment whose original line starts a new
        numbered/lettered sub-statement or a trailer phrase (see
        _NEW_LINE_TRIGGERS) begins a new displayed line instead of being
        run into the previous one - options stay single-paragraph (they
        don't have this structure), only the stem uses this."""
        parts: list[str] = []
        for i, f in enumerate(frags):
            marked = mark_underlines_in_text(f.text, f.underlined_words)
            if i == 0:
                parts.append(marked)
            elif _starts_new_line(f.text):
                parts.append("<br><br>" + marked)
            else:
                parts.append(" " + marked)
        return "".join(parts).strip()

    def to_question(self) -> Question:
        q = Question(
            q_number=self.q_number,
            page=self.page,
            section_directions=self.directions,
            passage_label=self.passage_label,
            passage_text=self.passage_text,
            question_stem=self._join_field_with_breaks(self.fields["stem"]),
            option_a=self._join_field(self.fields["a"]),
            option_b=self._join_field(self.fields["b"]),
            option_c=self._join_field(self.fields["c"]),
            option_d=self._join_field(self.fields["d"]),
            ocr_confidence=self.mean_conf(),
            raw_lines=self.raw_lines,
            start_y=self.start_y,
            start_kind=self.start_kind,
            regions=self.compute_regions(),
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

    def start_new_question(page_num: int, q_number: int, stem_prefix: str, line: Line, forced_reason: str = ""):
        nonlocal builder, expected_num
        finalize_current()
        builder = Builder(q_number, page_num, current_directions, current_passage_label, current_passage_text)
        builder.start_y = line.y0
        builder.start_kind = line.kind
        if stem_prefix:
            builder.add("stem", stem_prefix.strip(), line)
        if forced_reason:
            builder.forced_review_reason = forced_reason
        if SPOT_ERROR_INTRO_RE.search(stem_prefix) and not SPOT_ERROR_INTRO_END_RE.search(stem_prefix):
            builder.suppress_options = True
        if MATCH_LIST_START_RE.search(stem_prefix) and not MATCH_LIST_END_RE.search(stem_prefix):
            builder.suppress_new_question = True
        if STATEMENTS_INTRO_RE.search(stem_prefix) and not STATEMENTS_END_RE.search(stem_prefix):
            builder.suppress_new_question = True
        expected_num = q_number + 1

    for page_num in sorted(pages.keys()):
        lines, page_height = pages[page_num]
        for line in lines:
            if _is_footer(line, page_height):
                continue
            text = line.text.strip()
            if not text:
                continue

            # Loose fallback catches a question number OCR'd with its
            # trailing punctuation dropped entirely (e.g. "54 Which of the
            # following" instead of "54. Which..."); safe here because
            # every use below also checks the detected number against
            # expected_num/RESYNC_WINDOW, so a coincidental match against
            # ordinary prose still can't masquerade as a real question
            # unless its number also happens to fall in the plausible range.
            q_match = QUESTION_START_RE.match(text) or QUESTION_START_LOOSE_RE.match(text)
            # While the active question is inside its own List I/List II
            # table (see MATCH_LIST_START_RE/MATCH_LIST_END_RE), a
            # number-looking token found there is table noise, not a real
            # question boundary - suppress both the exact-match and resync
            # checks below until the table's "Code" header has been seen.
            if q_match and builder is not None and builder.suppress_new_question:
                q_match = None
            if q_match and int(q_match.group(1)) == expected_num:
                stem_prefix = (pending_stem + " " + q_match.group(2)).strip()
                pending_stem = ""
                start_new_question(page_num, expected_num, stem_prefix, line)
                builder.raw_lines.append(text)
                collecting = None
                continue

            # RESYNC: this line's own stated number is HIGHER than
            # expected, but still plausibly the next real question (within
            # RESYNC_WINDOW) - one or more questions before it were never
            # recognized (their own number/punctuation got dropped or
            # merged into a neighbor's text; reading_order.py's own
            # column-gutter handling is best-effort, not perfect). Without
            # this, the correctly-read number would just fall through to
            # "continuation line" below and get silently absorbed into
            # whatever question is currently active - and every question
            # from that point on would inherit the same drift forever,
            # since expected_num would never again match a real printed
            # number. Resyncing onto what this line actually says instead
            # contains the damage to just the missed run, not the rest of
            # the document.
            if q_match and expected_num < int(q_match.group(1)) <= expected_num + RESYNC_WINDOW:
                detected_num = int(q_match.group(1))
                stem_prefix = (pending_stem + " " + q_match.group(2)).strip()
                pending_stem = ""
                skipped = f"{expected_num}-{detected_num - 1}" if detected_num - 1 > expected_num else str(expected_num)
                start_new_question(
                    page_num,
                    detected_num,
                    stem_prefix,
                    line,
                    forced_reason=f"question(s) {skipped} not detected by OCR; resynced to {detected_num}",
                )
                builder.raw_lines.append(text)
                collecting = None
                continue

            # Real "Directions :" / "PASSAGE" headings are always standalone
            # full-width lines in this exam format. Gate on line.kind=="FULL"
            # too, not just the regex - otherwise a normal sentence that
            # happens to word-wrap with "directions" starting a new printed
            # line (e.g. "...shall extend to the giving of / directions to
            # the States...") gets misread as a new section header, since
            # each printed line is checked independently. Same risk applies
            # to "passage" as an ordinary word.
            dir_match = DIRECTIONS_RE.match(text) if line.kind == "FULL" else None
            if dir_match:
                finalize_current()
                current_directions = dir_match.group(1)
                collecting = "directions"
                continue

            pas_match = PASSAGE_RE.match(text) if line.kind == "FULL" else None
            if pas_match and len(text) < 40:
                finalize_current()
                current_passage_label = text
                current_passage_text = ""
                collecting = "passage"
                continue

            opt_match = OPTION_RE.match(text) if (builder is not None and not builder.suppress_options) else None
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
                    start_new_question(
                        page_num,
                        expected_num,
                        recovered_stem,
                        line,
                        forced_reason="question number not detected by OCR; number inferred from sequence",
                    )
                builder.add(letter, opt_match.group(2), line)
                builder.raw_lines.append(text)
                continue

            # continuation line
            if builder is not None:
                builder.raw_lines.append(text)
                builder.append_active(text, line)
                if builder.suppress_options and SPOT_ERROR_INTRO_END_RE.search(text):
                    builder.suppress_options = False
                if not builder.suppress_new_question and (MATCH_LIST_START_RE.search(text) or STATEMENTS_INTRO_RE.search(text)):
                    builder.suppress_new_question = True
                elif builder.suppress_new_question and (MATCH_LIST_END_RE.search(text) or STATEMENTS_END_RE.search(text)):
                    builder.suppress_new_question = False
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

    # sequence QA - checks 1..max(seen), not a fixed count, since different
    # papers have different total question counts (this used to be a
    # hardcoded 120, silently wrong for anything else).
    seen = {q.q_number for q in questions}
    missing = [n for n in range(1, max(seen) + 1) if n not in seen] if seen else []
    if missing:
        warnings.append(f"Missing question numbers: {missing}")
    dupes = [n for n in seen if sum(1 for q in questions if q.q_number == n) > 1]
    if dupes:
        warnings.append(f"Duplicate question numbers: {dupes}")

    return questions, warnings
