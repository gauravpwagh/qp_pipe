"""Shared regexes for recognizing structural markers in OCR'd text - a
question's own leading number, an (a)-(d) option marker, a Directions/
Passage section heading.

Kept in one place, at the bottom of the import graph, because both
parse_questions.py (the actual state-machine parser) and reading_order.py
need identical definitions: reading_order.py's line-building has to
recognize these same markers to avoid merging a new question's real start
into the previous question's trailing text whenever the column gutter is
narrower than normal word-spacing (see reading_order._split_row_into_lines)
- and reading_order.py is lower-level than parse_questions.py (which
already imports Line from it), so parse_questions.py can't be the source
of these without creating a circular import.
"""

import re

OPTION_RE = re.compile(r"^\(?\s*([abcd6])\s*\)\s*(.*)$", re.IGNORECASE)
# OCR frequently misreads "(b)" as "(6)" in this font; normalize on match.
OPTION_LETTER_FIX = {"6": "b"}

QUESTION_START_RE = re.compile(r"^(\d{1,3})\s*[\.\-_,:]\s*(.*)$")
# Fallback for a question number OCR'd with its trailing punctuation dropped
# entirely (e.g. "54 Which of the following" instead of "54. Which..."). The
# lookahead requires an uppercase letter followed by a lowercase one right
# after the number, so it matches an ordinary capitalized word ("Which",
# "Consider") but not a unit abbreviation glued to a numeric value ("10 N",
# "50 kg") - those either have no lowercase letter (single-letter symbols)
# or aren't capitalized at all. Only used where the detected number is also
# checked against the parser's expected sequence (see parse_questions.py),
# since the pattern alone can't rule out a plain sentence that happens to
# start with a number.
QUESTION_START_LOOSE_RE = re.compile(r"^(\d{1,3})\s+(?=[A-Z][a-z])(.*)$")
DIRECTIONS_RE = re.compile(r"^directions?\s*[:.]?\s*(.*)$", re.IGNORECASE)
PASSAGE_RE = re.compile(r"^passage\b\s*(.*)$", re.IGNORECASE)

# "Directions (for the next 05 items that follow)" / "Directions for the
# following 5 (five) items" / "Directions for the next 2 (two) items" - a
# fixed, distinctive phrase, safe to treat as a genuine heading regardless of column width or
# trailing punctuation (both of which OCR gets inconsistently across
# otherwise-identical repeats of this exact heading - seen in practice:
# only 1 of 6 real occurrences in one booklet had its trailing colon
# actually captured). The item count also drives auto-expiry - see
# parse_questions.py - since some booklets print this heading only for a
# batch of N questions and never repeat or explicitly close it before the
# next, differently-instructed section begins.
DIRECTIONS_ITEM_COUNT_RE = re.compile(
    r"for the (?:next|following)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:\(\s*\w+\s*\)\s+)?items?",
    re.IGNORECASE,
)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def item_count(match: "re.Match") -> int:
    """The N of a DIRECTIONS_ITEM_COUNT_RE match ("5", "five")."""
    raw = match.group(1).lower()
    return int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]

# "Spot the error" questions (a fixed, verbatim boilerplate in CDS/NDA
# papers) describe the sentence's own parts as "(a), (b) and (c)" inline
# within the stem, then say "mark your option (d)" - each of those
# parenthesized letters can land at the START of its own printed line
# purely from OCR line-wrap, making it indistinguishable from a real
# OPTION_RE match by pattern alone. Detected via the fixed phrasing so
# option-scanning can be suppressed until the boilerplate's known closing
# phrase, after which the two real options ("(a) ...", "(d) No error")
# start.
SPOT_ERROR_INTRO_RE = re.compile(r"sentence is given in \w+ parts", re.IGNORECASE)
SPOT_ERROR_INTRO_END_RE = re.compile(r"\baccordingly\b", re.IGNORECASE)

# A match-the-list question's "List I" / "List II" table is dense with
# small numbers (List II's own "1.", "2." item labels) and, occasionally,
# an outright OCR-hallucinated stray token that happens to look like a
# plausible next question number (seen in practice: a bogus "109." printed
# between the "List II" and "(Word/Term)" header lines, with nothing
# actually there in the source). Because match-the-list questions are
# usually announced with no stem text of their own (just the bare number,
# e.g. "107."), the resync window can't tell a genuine skipped question
# from this kind of table-interior noise by number proximity alone -
# so new-question detection is suppressed for the whole block, from the
# "List I" header to the "Code" answer-grid header that always follows it.
MATCH_LIST_START_RE = re.compile(r"\blist\s+i\b", re.IGNORECASE)
MATCH_LIST_END_RE = re.compile(r"\bcode\b", re.IGNORECASE)

# "Consider the following statements/pairs: 1. ... 2. ..." is the single
# most common GK/CDS question shape, and its own numbered sub-statements
# restart at 1 for every question - so a sub-statement's number is bound
# to coincide with the real expected_num sooner or later across a long
# document, and OCR frequently drops the trailing punctuation on these
# sub-statement lines just as it does on real question numbers (e.g. "2
# Indian Princely States..." instead of "2. Indian..."), making
# QUESTION_START_LOOSE_RE match a sub-statement as if it were the next
# real question. New-question detection is suppressed for the whole
# block, from the intro phrase to its standard closing trailer (the same
# phrases _NEW_LINE_TRIGGERS already recognizes for stem formatting).
# ("conclusions", "assumptions", ... too: the same numbered-list shape with a different noun)
STATEMENTS_INTRO_RE = re.compile(
    r"the following (?:statements?|pairs?|conclusions?|assumptions?|inferences?|arguments?|propositions?)",
    re.IGNORECASE,
)
STATEMENTS_END_RE = re.compile(
    r"select the answer|which of the (?:above\s+)?statements?|how many of the above|which of the above",
    re.IGNORECASE,
)
