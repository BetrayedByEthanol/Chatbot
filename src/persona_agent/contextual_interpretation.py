"""MVP-04C.2: narrow contextual input interpretation.

No state owner, tool execution, model inference, or new persistent authority.
The dialogue caller supplies scoped, delivery-linked pending acts. A user
statement is evidence of what they reported, not proof of an external fact
or permission to amend an independently owned record.
"""
from __future__ import annotations

import re
from typing import Literal, Mapping, Sequence


ShortReply = Literal["POLAR", "UNCERTAIN", "REFERENCE"]
_POLAR = frozenset(("yes", "yeah", "yep", "no", "nope"))
_UNCERTAIN = frozenset(("maybe", "perhaps", "not sure", "i'm not sure",
                       "i don't know", "i dunno", "either", "whatever"))
_REFERENCE = frozenset(("that", "this", "it", "that one", "this one",
                       "the first one", "the second one", "the other one",
                       "same", "the same"))
# Deliberately narrow: an inverted auxiliary and an explicit question mark.
# Do not auto-interpret alternatives, negative questions or compound prompts.
_POLAR_QUESTION = re.compile(
    r"^(?:do|does|did|is|are|was|were|have|has|had|can|could|would|will|should)"
    r"\s+[^?!.;\r\n]{1,170}\?$", re.I,
)
_POLAR_AMBIGUITY = re.compile(
    r"\b(?:or|not|never|and|but|what|which|who|whom|whose|where|when|why|how)\b"
    r"|n't\b|[,/:]", re.I,
)


def short_reply_kind(text: str) -> ShortReply | None:
    if not isinstance(text, str) or not 1 <= len(text) <= 80:
        return None
    value = text.strip().replace("’", "'").casefold().rstrip(".!")
    if value in _POLAR:
        return "POLAR"
    if value in _UNCERTAIN:
        return "UNCERTAIN"
    if value in _REFERENCE:
        return "REFERENCE"
    return None


def short_reply_answers_question(text: str, question: str) -> bool:
    """Only a literal polarity reply to a supported polar question.

    This means the conversational question has an answer, not that its
    proposition is true or any decision, operation or outcome is approved.
    Full free-text interpretation remains the existing model/owner path.
    """
    question = question.strip().replace("’", "'")
    return (short_reply_kind(text) == "POLAR"
            and _POLAR_QUESTION.fullmatch(question) is not None
            and _POLAR_AMBIGUITY.search(question) is None)


def pending_reply_context(
    text: str, pending: Sequence[Mapping], *, turn_sequence: int, max_chars: int,
) -> str:
    """Use the existing scoped pending snapshot, not a guessed referent.

    Pending records here are not a complete inventory of other threads.
    An old question can be useful history but is not an adjacent prompt.
    Do not manufacture a clarification when the immediate exchange is clear.
    """
    kind = short_reply_kind(text)
    if kind is None or not pending:
        return ""
    adjacent = (
        len(pending) == 1 and pending[0]["delivery_status"] == "DELIVERED"
        and pending[0]["source_sequence"] == turn_sequence - 1
    )
    if not adjacent:
        guidance = (
            "A short reply does not resolve older or competing pending acts. "
            "Use the immediately preceding delivered exchange; clarify only "
            "if the referent remains unclear. Never infer consent."
        )
    elif kind == "UNCERTAIN":
        guidance = (
            "The short reply expresses uncertainty, not an answer or consent. "
            "Keep the pending discussion open; acknowledge uncertainty without pressure."
        )
    elif (kind == "POLAR" and pending[0]["kind"] == "QUESTION"
          and pending[0]["origin_kind"] == "AUTO_DELIVERED_QUESTION"
          and short_reply_answers_question(text, pending[0]["label"])):
        guidance = (
            "The short reply addresses the adjacent polar question. "
            "It is conversational evidence, not authorization or verified completion."
        )
    else:
        guidance = (
            "The short reply does not unambiguously resolve the adjacent pending act. "
            "If its meaning matters, ask one short clarification about the choice "
            "or referent rather than assuming an answer or approval."
        )
    block = "Pending-reply interpretation (advisory): " + guidance + "\n"
    return block if len(block) <= max_chars else ""


# Complete, explicit arithmetic corrections only. This verifies the
# calculation, not the provenance of the operands or any game rule.
_ARITHMETIC = re.compile(
    r"^(?:(?:actually|correction|no)\s*[:,]?\s*)?"
    r"(?P<left>-?\d{1,6})\s*(?P<op>[+\-×*])\s*(?P<right>-?\d{1,6})"
    r"\s*(?:=|equals|is)\s*(?P<claimed>-?\d{1,13})[.!]?$", re.I,
)


def arithmetic_evidence(text: str) -> str:
    match = _ARITHMETIC.fullmatch(text.strip())
    if match is None:
        return ""
    a, b, claimed = (int(match[k]) for k in ("left", "right", "claimed"))
    op = match["op"]
    result = a+b if op == "+" else a-b if op == "-" else a*b
    verdict = "matches" if result == claimed else "does not match"
    return (
        f"Checked arithmetic only: {a} {op} {b} = {result}; "
        f"the reported result {claimed} {verdict}. This does not establish "
        "operand provenance, rule applicability or permission to change a record.\n"
    )


def correction_authority_context(text: str, *, max_chars: int) -> str:
    """Current-turn source/authority distinction using existing intent cues.

    No claim to have a game, weather, or other unregistered capability.
    The normal main model may consider evidence; only the owner can commit
    its state through a separately authorized, versioned command.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= 500:
        return ""
    from .intent_interpretation import classify_intent
    draft = classify_intent(text)
    arithmetic = arithmetic_evidence(text)
    if draft is None and not arithmetic:
        return ""
    kind = draft.kind if draft else "CORRECTION"
    # Limit this context contribution to pertinent evidence/authority turns.
    # Ordinary greetings, questions, objectives and generic observations
    # acquire no additional context block, model inference or retrieval.
    if not arithmetic and kind not in ("CORRECTION", "HYPOTHETICAL", "INSTRUCTION"):
        if kind != "OBSERVATION" or not is_direct_observation(text):
            return ""
    guidance = {
        "CORRECTION": "A correction is a claim to assess, not automatic truth. "
                      "Check relevant source evidence or calculation.",
        "INSTRUCTION": "An instruction expresses the user's requested action, "
                       "not authority to alter every referenced state.",
        "OBSERVATION": "A direct observation can outweigh an older external report. "
                       "Compare time, location and source reliability; acknowledge the discrepancy.",
        "HYPOTHETICAL": "A hypothetical is not an actual instruction, observation "
                        "or approval.",
    }.get(kind, "")
    if not guidance and not arithmetic:
        return ""
    boundary = (
        "Correction authority (advisory): " + guidance + " "
        "Authenticated speech is evidence of what was said, not independent "
        "verification. Credibility and mutation authority are separate. "
        "Preserve accepted records unless the owning domain validates an "
        "authorized correction; never claim a saved change without its result.\n"
    )
    block = boundary + arithmetic
    # Never retain the result while dropping its provenance caveat.
    return block if len(block) <= max_chars else ""


_DIRECT_OBSERVATION = re.compile(
    r"^(?:i (?:can see|saw|observed|measured|checked|looked outside and saw)"
    r"\s+[^?\r\n]{2,220}|"
    r"it(?:'s| is) (?:raining|snowing|hailing)"
    r"(?: (?:here|outside|right now|at my location)){1,2})[.!]?$",
    re.I,
)
_RECORD_CHANGE = re.compile(
    r"^(?:please )?(?:change|replace|overwrite|set|amend)\s+"
    r"(?:the|my|our|that|this)\s+(?:recorded|accepted|saved)\s+"
    r"[^?\r\n]{2,180}[.!]?$", re.I,
)


def is_direct_observation(text: str) -> bool:
    return _DIRECT_OBSERVATION.fullmatch(text.strip().replace("’", "'")) is not None


def is_record_change_request(text: str) -> bool:
    return _RECORD_CHANGE.fullmatch(text.strip().replace("’", "'")) is not None
