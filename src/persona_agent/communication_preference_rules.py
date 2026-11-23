"""Strict lexical cues for durable communication preferences.

Pure parsing shared by Relationship admission and read-only prompt rendering.
This is neither inferred user preference nor a new persistence layer.
"""
from dataclasses import dataclass
import re
from typing import Literal


SentenceLimitException = Literal["DETAILED_INSTRUCTIONS", "MORE_DETAIL"]


@dataclass(frozen=True)
class SentenceLimitInstruction:
    """A supported 1–2 sentence instruction and its optional exception."""

    exception: SentenceLimitException | None


_ONE_TO_TWO_SENTENCES = re.compile(
    r"^(?:please\s+)?(?:keep|limit)\s+(?:your\s+)?"
    r"(?:replies|responses|answers)\s+to\s+(?:one|1)\s+or\s+(?:two|2)\s+"
    r"sentences(?:\s+unless\s+i\s+ask\s+for\s+"
    r"(?P<exception>detailed\s+instructions|more\s+detail|details))?$",
    re.I,
)


def parse_explicit_sentence_limit(text: str) -> SentenceLimitInstruction | None:
    """Use one grammar for persistence and for recovering the exact exception."""
    value = text.strip().replace("’", "'").rstrip("!.").strip()
    if len(value) > 200:
        return None
    matched = _ONE_TO_TWO_SENTENCES.fullmatch(value)
    if matched is None:
        return None
    exception = matched.group("exception")
    if exception is None:
        return SentenceLimitInstruction(exception=None)
    kind: SentenceLimitException = (
        "DETAILED_INSTRUCTIONS"
        if re.fullmatch(r"detailed\s+instructions", exception, re.I)
        else "MORE_DETAIL"
    )
    return SentenceLimitInstruction(exception=kind)


def is_explicit_sentence_limit(text: str) -> bool:
    """Recognize only a complete first-party imperative, not a vague hint."""
    return parse_explicit_sentence_limit(text) is not None
