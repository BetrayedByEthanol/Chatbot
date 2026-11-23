"""Strict lexical cues for durable communication preferences.

Pure parsing shared by Relationship admission and read-only prompt rendering.
This is neither inferred user preference nor a new persistence layer.
"""
import re

_ONE_TO_TWO_SENTENCES = re.compile(
    r"^(?:please\s+)?(?:keep|limit)\s+(?:your\s+)?"
    r"(?:replies|responses|answers)\s+to\s+(?:one|1)\s+or\s+(?:two|2)\s+"
    r"sentences(?:\s+unless\s+i\s+ask\s+for\s+"
    r"(?:detailed\s+instructions|more\s+detail|details))?$",
    re.I,
)


def is_explicit_sentence_limit(text: str) -> bool:
    """Recognize only a complete first-party imperative, not a vague hint."""
    value = text.strip().replace("’", "'").rstrip("!.").strip()
    return len(value) <= 200 and bool(_ONE_TO_TWO_SENTENCES.fullmatch(value))
