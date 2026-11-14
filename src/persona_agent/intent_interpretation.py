"""MVP-01A bounded, tentative natural-language cues; no additional model pass."""
import re
from dataclasses import dataclass

_HYPOTHETICAL = re.compile(r"^(what if|hypothetically|suppose|imagine|for example|in a hypothetical|if i said|if i wanted|let's pretend)\\b", re.I)
_CORRECTION = re.compile(r"^(actually|correction:|i meant|no, i meant|to clarify|instead of)\\b", re.I)
_OBJECTIVE = re.compile(r"^(?:i (?:want|need|hope|aim|intend|plan|am trying) to|i(?:'d| would) like to|my (?:goal|objective) is to)\\s+(.+)$", re.I)
_INFO = re.compile(r"^(know|find out|ask|check whether|understand why|understand how)\\b", re.I)
_INSTRUCTION = re.compile(r"^(?:please )?(?:remember that|always|never|don't|do not|from now on|stop sending)\\b", re.I)
_OBSERVATION = re.compile(r"^(?:i (?:prepared|made|finished|tried|noticed|struggled|failed|managed|completed|cooked|feel|felt|am feeling)\\b|my (?:favorite|hobby)\\b)", re.I)
_REQUEST = re.compile(r"^(?:can (?:you|we)|could (?:you|we)|would you|will you|please|help me|tell me|explain|how|what|why|when|where|yes,? please|let's)\\b", re.I)
_REFERENCE = re.compile(r"\\b(that goal|this goal|our goal|previous goal|the plan|our plan|next step|start by|what we discussed|i meant)\\b", re.I)

@dataclass(frozen=True)
class IntentDraft:
    kind: str
    label: str
    confidence: float
    method_revision: str = "mvp01a.rules.1"

def classify_intent(text: str) -> IntentDraft | None:
    """Abstain on unclear inputs, returning discussion cues rather than commands."""
    original = text.strip()
    if not 3 <= len(original) <= 500 or "\\n" in original:
        return None
    value = original.replace("’", "'")
    if _HYPOTHETICAL.match(value):
        kind, label, confidence = "HYPOTHETICAL", original, .95
    elif _CORRECTION.match(value):
        kind, label, confidence = "CORRECTION", original, .92
    elif match := _OBJECTIVE.match(value):
        subject = match.group(1).strip().rstrip(" .!?")
        if not subject or _INFO.match(subject):
            kind, label, confidence = "REQUEST", original, .80
        else:
            kind, label, confidence = "OBJECTIVE", subject, .88
    elif _INSTRUCTION.match(value):
        kind, label, confidence = "INSTRUCTION", original, .86
    elif _OBSERVATION.match(value):
        kind, label, confidence = "OBSERVATION", original, .79
    elif _REQUEST.match(value):
        kind, label, confidence = "REQUEST", original, .84
    else:
        return None
    return IntentDraft(kind, label[:200], confidence)
