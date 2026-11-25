"""MVP-01D: bounded, explainable natural-language response decisions.

These decisions are routing hints for the already registered read-only
capabilities, not model-issued tool commands or permission grants.
Unrecognized, ambiguous, hypothetical and quoted requests abstain and
take the existing conversational model path. No additional inference.
"""

from dataclasses import dataclass
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .entity_reference import entity_reference

# Explicit location names are intentionally bounded. Do not guess arbitrary
# city names or infer a timezone from the user's location or profile.
_LOCAL_ZONES = {
    "utc": "UTC",
    "gmt": "Etc/GMT",
    "berlin": "Europe/Berlin",
    "paris": "Europe/Paris",
    "london": "Europe/London",
    "tokyo": "Asia/Tokyo",
    "new york": "America/New_York",
    "los angeles": "America/Los_Angeles",
}

# Full-match is important: "What time is it tomorrow?" and "What if..."
# are questions for the conversational model, not direct clock invocations.
_TIME_QUERY = re.compile(
    r"^(?:what(?:'s| is) (?:the )?(?:current )?time"
    r"|what time is it|(?:tell|give) me (?:the )?(?:current )?time"
    r"|current time|time)"
    r"(?:\s+(?:right )?now)?"
    r"(?:\s+(?:in|for)\s+(?P<place>[A-Za-z_/-]+(?:\s+[A-Za-z_/-]+){0,3}?))?"
    r"(?:\s+(?:right )?now)?"
    r"\s*[?.!]?"
    r"(?:\s+please check(?: instead of guessing)?[.!]?)?$",
    re.I,
)
# A direct question for a personal fact is a recall need, not an instruction
# to create a memory. Keep it bounded and exclude advice, hypothetical,
# quoted and mixed requests. Topic terms are data, never query operators.
_PERSONAL_FACT_QUERY = re.compile(
    r"^(?:by the way,\s+)?(?:what|which)\s+"
    r"(?P<topic>[a-z][a-z-]{1,30}(?:\s+[a-z][a-z-]{1,30}){0,2})"
    r"\s+do i\s+(?P<verb>drive|own|use|wear|prefer|like)\s*\?$",
    re.I,
)


# These exact subject/relationship pairs have explicit source semantics.
# An arbitrary noun in "What command do I use?" is not a recall request.
_PERSONAL_FACT_RELATIONS = {"car": "drive", "phone": "use", "camera": "own"}


def personal_fact_topic(text: str) -> str | None:
    """Recognize only a supported standalone personal-fact recall question."""
    if not isinstance(text, str) or not 1 <= len(text) <= 160:
        return None
    match = _PERSONAL_FACT_QUERY.fullmatch(text.strip())
    if match is None:
        return None
    topic, verb = match["topic"].casefold(), match["verb"].casefold()
    return topic if _PERSONAL_FACT_RELATIONS.get(topic) == verb else None


def is_direct_clock_question(text: str) -> bool:
    """Recognize a direct clock query even if its timezone is unresolved."""
    return (isinstance(text, str) and 1 <= len(text) <= 200
            and _TIME_QUERY.fullmatch(text.strip().replace("’", "'")) is not None)


# A delimited, nonempty input payload is necessary. Without one, interpreting
# the entire command as the analysis subject produces misleading statistics.
_ANALYSIS_QUERY = re.compile(
    r"^(?:please\s+)?(?:analy[sz]e\s+(?:this\s+)?text"
    r"|count\s+(?:the\s+)?words\s+in"
    r"|word\s+count\s+(?:for|of))\s*:\s*(?P<payload>.+)$",
    re.I | re.S,
)


# A deliberately small allowlist of complete direct questions. Mixed requests,
# hypotheticals, quotations and less explicit wording stay model-driven.
_EMOJI_HISTORY_QUERY = re.compile(r"have i (?:(?:ever )?specified whether you should use emojis|(?:(?:ever )?set|told you about) an? emoji preference|(?:(?:ever )?told you) whether you should use emojis)")
_PREFERENCE_RECALL = (
    (re.compile(r"do you remember my preference(?:s)? about planning and (?:response|reply) length"), ("PLANNING","RESPONSE_LENGTH")),
    (re.compile(r"do you remember my (?:response|reply) length preference(?:s)?"), ("RESPONSE_LENGTH",)),
    (re.compile(r"do you remember my planning preference(?:s)?"), ("PLANNING",)),
    (re.compile(r"what (?:response|reply) length did i (?:(?:most recently|last) )?ask you to use"), ("RESPONSE_LENGTH",)),
    (re.compile(r"what (?:is|was) my (?:(?:current|saved|latest) )?(?:response|reply) length preference"), ("RESPONSE_LENGTH",)),
    (re.compile(r"what (?:is|was) my (?:(?:current|saved|latest) )?planning preference"), ("PLANNING",)),
    (_EMOJI_HISTORY_QUERY, ("EMOJI_USE",)),
    (re.compile(r"what (?:is|was) my (?:(?:current|saved|latest) )?emoji preference"), ("EMOJI_USE",)),
)


def _normalized_recall_query(text: str) -> str | None:
    if not isinstance(text, str) or not 1<=len(text)<=200:
        return None
    value=re.sub(r"\s+"," ",text.strip().replace("’","'")).casefold()
    if value.startswith(('"', "'", "“", "‘")):
        return None
    return value.removesuffix("?").strip()


def preference_recall_topics(text: str) -> tuple[str, ...] | None:
    """Return recognized topics only for a standalone direct recollection."""
    normalized=_normalized_recall_query(text)
    if normalized is None:
        return None
    for pattern,topics in _PREFERENCE_RECALL:
        if pattern.fullmatch(normalized):
            return topics
    return None


def is_historical_emoji_preference_query(text: str) -> bool:
    """Whether the exact routed question asks if a preference *ever* existed."""
    normalized=_normalized_recall_query(text)
    return normalized is not None and _EMOJI_HISTORY_QUERY.fullmatch(normalized) is not None


@dataclass(frozen=True)
class ResponseDecision:
    route: Literal["MODEL", "CLOCK", "TEXT_ANALYSIS", "PREFERENCE_RECALL", "PERSONAL_FACT_RECALL", "ENTITY_RECALL"]
    timezone_name: str | None = None
    preference_topics: tuple[str, ...] = ()
    method_revision: str = "mvp01d.rules.1"


def explicit_analysis_payload(text: str) -> str | None:
    """Extract verbatim user-supplied analysis data from a bounded command."""
    if not isinstance(text, str):
        return None
    match = _ANALYSIS_QUERY.fullmatch(text.strip())
    if match is None:
        return None
    payload = match.group("payload").strip()
    return payload if payload and len(payload) <= 12000 else None


def _timezone_from_query(place: str) -> str | None:
    value = place.casefold().strip()
    if value in _LOCAL_ZONES:
        return _LOCAL_ZONES[value]
    # A direct, valid IANA key is precise and requires no geocoding.
    if "/" not in place or len(place) > 80:
        return None
    try:
        ZoneInfo(place)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    return place


def decide_response(text: str, *, default_timezone: str = "UTC") -> ResponseDecision:
    """Route only unambiguous requests to allowlisted read-only capabilities.

    All other inputs stay on the primary conversational inference path.
    A decision alone does not execute a tool or establish authorization.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= 12000:
        return ResponseDecision("MODEL")
    cleaned = text.strip().replace("’", "'")
    # Never execute instructions framed as hypotheticals or direct quotes.
    if cleaned.startswith(('"', "'", "“", "‘")):
        return ResponseDecision("MODEL")
    if explicit_analysis_payload(cleaned) is not None:
        return ResponseDecision("TEXT_ANALYSIS")
    topics=preference_recall_topics(cleaned)
    if topics is not None:
        return ResponseDecision("PREFERENCE_RECALL",preference_topics=topics)
    if personal_fact_topic(cleaned) is not None:
        return ResponseDecision("PERSONAL_FACT_RECALL",method_revision="mvp04c3.rules.1")
    # Entity parsing owns its normalization and bounds. Dispatch must use
    # the same input as the persisted-turn revalidation and commit guard.
    if entity_reference(text) is not None:
        return ResponseDecision("ENTITY_RECALL",method_revision="mvp05c1.rules.1")
    match = _TIME_QUERY.fullmatch(cleaned) if is_direct_clock_question(cleaned) else None
    if match:
        place = match.group("place")
        timezone_name = (
            _timezone_from_query(place) if place is not None
            else default_timezone
        )
        if timezone_name is not None:
            return ResponseDecision("CLOCK",timezone_name=timezone_name)
    return ResponseDecision("MODEL")
