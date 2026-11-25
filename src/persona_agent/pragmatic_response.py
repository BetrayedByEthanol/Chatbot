"""MVP-04C: bounded current-turn conversational cues and reply safeguards.

These are Conversation-owned presentation decisions, not emotions, persistent
preferences, tasks or authority. No inference, database queries, external
verification or interpretation of arbitrary prose is performed here.
"""
from dataclasses import dataclass
import re

from .response_decisions import is_direct_clock_question


# Standalone, first-party expressions of current conversational needs.
# A compound request, quoted speech, hypothetical or unrelated observation
# must not be turned into an instruction or suppress a required answer.
_LOW_DECISION = re.compile(
    r"(?:(?:even )?(?:deciding|choosing)(?: what to do| anything)? "
    r"(?:feels?|seems?) (?:like )?(?:too much work|exhausting|overwhelming)"
    r"(?: today| right now| at the moment)?"
    r"|i (?:don't|do not) want to (?:make (?:any )?decisions|decide anything)"
    r"(?: today| right now| at the moment)?"
    r"|i can't face (?:another decision|making (?:any )?decisions)"
    r"(?: today| right now)?)",
    re.I,
)
_CHAT_ONLY = re.compile(
    r"(?:please )?(?:(?:don't|do not) make (?:me )?(?:a )?big plans?"
    r"(?: for me)?[;,.]\s*)?i just want to chat",
    re.I,
)
_NO_QUESTIONS = re.compile(
    r"(?:please )?(?:don't|do not) ask me (?:any |more )?questions"
    r"(?: today| right now| for now)?",
    re.I,
)
_NO_GOAL = re.compile(
    r"(?:i'm still exploring (?:the|this) idea[;,.]\s*)?"
    r"(?:please )?(?:don't|do not) (?:create|save|set) (?:that|this|a|the) goal"
    r"(?: yet| for now)?"
    r"|i(?:'m| am) not (?:agreeing|ready) to (?:save|create|set)"
    r" (?:that|this|a|the)(?: as a)? goal(?: yet| for now)?",
    re.I,
)
_UNDECIDED = re.compile(
    r"(?:i (?:haven't|have not) decided(?: yet)?"
    r"|(?:i (?:might|may|want to|would like to)|maybe|perhaps)"
    r" [^?.;,\r\n]{1,135}(?:[.;,]\s*|,?\s+but\s+)"
    r"(?:i (?:haven't|have not) decided(?: yet)?|"
    r"(?:that|this|it) (?:isn't|is not) a commitment))",
    re.I,
)
# Formatting alone is not evidence of a decision or agenda. A list can
# contain ordinary conversational information. Require an explicit directive,
# user-facing menu introduction, or proposed agenda before replacing a reply.
_CHOICE_MENU = re.compile(
    r"(?m)^\s*(?:here (?:are|is) (?:two|three|some|a few|several|\d+) "
    r"(?:options|choices) for you|your (?:options|choices) are)\s*:",
    re.I,
)
_AGENDA_INTRO = re.compile(
    r"(?m)^\s*(?:here(?:'s| is) (?:a|the|your) "
    r"(?:plan|schedule|agenda|routine)(?: for [^:\r\n]+)?"
    r"|your (?:plan|schedule|agenda|routine)(?: for [^:\r\n]+)?"
    r"|here are the steps you should follow)\s*:",
    re.I,
)
_GOAL_CHOICE = re.compile(
    r"(?:^|[.!?;\n]\s*)(?:please\s+)?(?:choose|pick|select)"
    r"\s+(?:a|the|your|one) goal\b", re.I,
)
_CHOICE_PROMPT = re.compile(
    r"(?:^|[.!?;\n]\s*)(?:please\s+)?(?:choose|pick|decide|select)\b",
    re.I,
)
_PLAN_PROMPT = re.compile(
    r"\b(?:let's|let us|we can|we should|you should)\s+"
    r"(?:make|create|set up|build|draft|start)\s+(?:a |the |your )?"
    r"(?:plan|schedule|agenda|routine)\b", re.I,
)
_GOAL_COMMITMENT = re.compile(
    r"\b(?:i(?:'ll| will| have|'ve)|we(?:'ll| will| have|'ve))\s+"
    r"(?:create(?:d)?|sav(?:e|ed)|set)\s+(?:that|this|a|the|your|our)"
    r"(?: as a)? goal\b|\b(?:(?P<agreement>we(?:'ve| have) agreed)|your approved goal)\b",
    re.I,
)
_AGREEMENT_CONDITION = re.compile(
    r"\b(?:until|once|if|when|after|before|unless|whether|"
    r"provided(?: that)?|as soon as)\s+\Z", re.I,
)


# An agreement alone says nothing about what the user committed to.
# Require a positive goal/plan object or a concrete action commitment.
# Agreements to defer, not act, or continue discussing are not evidence.
_AFFIRMATIVE_AGREEMENT = re.compile(
    r"\s+(?:on|upon)\s+(?:a|an|the|our|your|this|that)\s+"
    r"(?:goal|objective|target|plan|schedule)\b"
    r"|\s+to\s+(?:create|save|set|adopt|approve|pursue|commit to|follow)"
    r"\s+(?:a|an|the|our|your|this|that)\s+(?:goal|objective|target|plan|schedule)\b"
    r"|\s+to\s+(?:work|walk|exercise|study|train|run|practice)"
    r"\s+(?:today|tonight|tomorrow|daily|every day|each week)\b",
    re.I,
)


def _has_asserted_goal_commitment(text: str) -> bool:
    """Distinguish positive asserted commitments from deferral or conditions.

    An agreement must have a recognized affirmative object and must not
    be introduced by a conditional or uncertainty marker. Check each match
    independently, so an unrelated deferral cannot hide a later creation
    claim. Ambiguous agreement wording is left to the model; this rule
    neither exempts tool promises nor authorizes goal creation.
    """
    for match in _GOAL_COMMITMENT.finditer(text):
        if match.group("agreement") is not None:
            prefix = text[max(0, match.start() - 80):match.start()]
            if (_AGREEMENT_CONDITION.search(prefix)
                    or _AFFIRMATIVE_AGREEMENT.match(text, match.end()) is None):
                continue
        return True
    return False


_CLOCK_VALUE = re.compile(
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[ap]\.?m\.?)?\b"
    r"|\b(?:1[0-2]|[1-9])\s*[ap]\.?m\.?\b", re.I,
)
# A labeled signed offset is a timezone descriptor, not a clock reading.
# Mask only that token, never an adjacent independent time or tool claim.
_CLOCK_OFFSET = re.compile(
    r"\b(?:UTC|GMT)[ \t]*[+\-\u2212][ \t]*(?:0?\d|1[0-4]):[0-5]\d\b"
    r"(?![ \t]*[ap]\.?m\.?\b)", re.I,
)
_CLOCK_CHECK = re.compile(
    r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+"
    r"(?:checked|looked up|verified|queried)\b", re.I,
)


def pragmatic_cue(text: str) -> str | None:
    """High-precision current-turn cue, independent of any stored mood."""
    if not isinstance(text, str) or not 1 <= len(text) <= 220:
        return None
    value = text.strip().replace("’", "'").rstrip(".!")
    for kind, pattern in (
        ("LOW_DECISION", _LOW_DECISION), ("NO_QUESTIONS", _NO_QUESTIONS),
        ("CHAT_ONLY", _CHAT_ONLY), ("NO_GOAL", _NO_GOAL),
        ("UNDECIDED", _UNDECIDED),
    ):
        if pattern.fullmatch(value):
            return kind
    return None


def pragmatic_context(text: str, *, max_chars: int) -> str:
    """Only the applicable, bounded guidance for the authenticated input."""
    cue = pragmatic_cue(text)
    guidance = {
        "LOW_DECISION": "The user reports that deciding feels effortful. "
                        "Respond naturally without another choice, agenda or question.",
        "NO_QUESTIONS": "The user explicitly asks for no questions in this turn. "
                        "Acknowledge without asking one.",
        "CHAT_ONLY": "The user wants casual chat, not a big plan. "
                     "Do not introduce an unsolicited agenda or list of tasks.",
        "NO_GOAL": "The user explicitly declines saving a goal. "
                   "Do not imply approval, create a commitment or push a plan.",
        "UNDECIDED": "The user has not decided or committed. "
                     "Treat this as tentative discussion, not an agreed plan.",
    }.get(cue)
    if guidance is None:
        return ""
    block = "Current conversational cue (not a diagnosis or durable preference): " + guidance + "\n"
    return block if len(block) <= max_chars else ""


@dataclass(frozen=True)
class PragmaticEnforcement:
    text: str
    reason: str | None = None
    policy_revision: str = "mvp04c.4"


def enforce_pragmatic_reply(
    user_text: str, reply: str, *, model_generated: bool,
) -> PragmaticEnforcement:
    """Selective safeguard; preserve compliant replies verbatim.

    Only a direct, standalone current-turn cue may change output. Clock
    checks apply only to the model path, which has no fresh clock result.
    Tool and direct preference replies are not treated as model output.
    This is not a semantic fact verifier or a universal sentence limiter.
    """
    if not model_generated:
        return PragmaticEnforcement(reply)
    cue = pragmatic_cue(user_text)
    normalized = reply.replace("’", "'")
    choice = bool(_CHOICE_MENU.search(normalized) or _CHOICE_PROMPT.search(normalized))
    agenda = bool(_AGENDA_INTRO.search(normalized) or _PLAN_PROMPT.search(normalized))
    if cue in ("LOW_DECISION", "NO_QUESTIONS") and (
        "?" in reply or (cue == "LOW_DECISION" and (
            choice or agenda
        ))
    ):
        text = ("We can keep chatting without making another decision."
                if cue == "LOW_DECISION" else "Understood. No questions for now.")
        return PragmaticEnforcement(text, "current_boundary:" + cue.lower())
    if cue == "CHAT_ONLY" and (choice or agenda):
        return PragmaticEnforcement(
            "We can just chat without making a plan.", "current_boundary:chat_only",
        )
    if cue in ("NO_GOAL", "UNDECIDED") and (
        _has_asserted_goal_commitment(normalized)
        or (cue == "NO_GOAL" and (agenda or _GOAL_CHOICE.search(normalized)))
    ):
        text = ("We can keep it as an idea without saving a goal."
                if cue == "NO_GOAL" else "Nothing has been decided yet.")
        return PragmaticEnforcement(text, "unconfirmed_commitment:" + cue.lower())
    if is_direct_clock_question(user_text) and (
        _CLOCK_VALUE.search(_CLOCK_OFFSET.sub(" ", normalized))
        or _CLOCK_CHECK.search(normalized)
    ):
        return PragmaticEnforcement(
            "I don't have a verified current clock reading for this request.",
            "unverified_clock_claim",
        )
    return PragmaticEnforcement(reply)
