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
    r"(?:\s+(?:in|for)\s+(?P<place>[A-Za-z_/-]+(?:\s+[A-Za-z_/-]+){0,3}))?"
    r"\s*[?.!]?$",
    re.I,
)
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
_PREFERENCE_RECALL = (
    (re.compile(r"do you remember my preference(?:s)? about planning and (?:response|reply) length"), ("PLANNING","RESPONSE_LENGTH")),
    (re.compile(r"do you remember my (?:response|reply) length preference(?:s)?"), ("RESPONSE_LENGTH",)),
    (re.compile(r"do you remember my planning preference(?:s)?"), ("PLANNING",)),
    (re.compile(r"what (?:response|reply) length did i (?:(?:most recently|last) )?ask you to use"), ("RESPONSE_LENGTH",)),
    (re.compile(r"what (?:is|was) my (?:(?:current|saved|latest) )?(?:response|reply) length preference"), ("RESPONSE_LENGTH",)),
    (re.compile(r"what (?:is|was) my (?:(?:current|saved|latest) )?planning preference"), ("PLANNING",)),
    (re.compile(r"have i (?:(?:ever )?specified whether you should use emojis|(?:(?:ever )?set|told you about) an? emoji preference|(?:(?:ever )?told you) whether you should use emojis)"), ("EMOJI_USE",)),
    (re.compile(r"what (?:is|was) my (?:(?:current|saved|latest) )?emoji preference"), ("EMOJI_USE",)),
)


def preference_recall_topics(text: str) -> tuple[str, ...] | None:
    """Return recognized topics only for a standalone direct recollection."""
    if not isinstance(text,str) or not 1<=len(text)<=200:
        return None
    normalized=re.sub(r"\s+"," ",text.strip().replace("’","'")).casefold()
    if normalized.startswith(('"', "'", "“", "‘")):
        return None
    normalized=normalized.removesuffix("?").strip()
    for pattern,topics in _PREFERENCE_RECALL:
        if pattern.fullmatch(normalized):
            return topics
    return None


@dataclass(frozen=True)
class ResponseDecision:
    route: Literal["MODEL", "CLOCK", "TEXT_ANALYSIS", "PREFERENCE_RECALL"]
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
    match = _TIME_QUERY.fullmatch(cleaned)
    if match:
        place = match.group("place")
        timezone_name = (
            _timezone_from_query(place) if place is not None
            else default_timezone
        )
        if timezone_name is not None:
            return ResponseDecision("CLOCK",timezone_name=timezone_name)
    return ResponseDecision("MODEL")
