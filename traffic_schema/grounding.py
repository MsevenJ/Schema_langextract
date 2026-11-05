"""Strict source grounding with a narrowly scoped whitespace allowance."""

from __future__ import annotations

import re


# Deliberately excludes punctuation and ordinary visible characters.  NFKC or
# fuzzy matching must not be used here because they can change road names,
# numbers, directions, and time expressions.
_WEB_WHITESPACE_RE = re.compile(
    r"[ \t\r\n\f\v\u00a0\u1680\u180e\u2000-\u200b"
    r"\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)

GROUNDING_EXACT = "exact"
GROUNDING_WHITESPACE_NORMALIZED = "whitespace_normalized"
GROUNDING_MISMATCH = "mismatch"


def normalize_web_whitespace(text: str) -> str:
    """Collapse only whitespace characters commonly introduced by web pages."""
    return _WEB_WHITESPACE_RE.sub(" ", text).strip()


def compare_grounding(extraction_text: str, evidence_text: str) -> str:
    """Return exact, whitespace_normalized, or mismatch."""
    if extraction_text == evidence_text:
        return GROUNDING_EXACT
    if normalize_web_whitespace(extraction_text) == normalize_web_whitespace(
        evidence_text
    ):
        return GROUNDING_WHITESPACE_NORMALIZED
    return GROUNDING_MISMATCH
