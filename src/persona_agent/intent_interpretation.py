"""MVP-01A bounded, tentative natural-language cues; no additional model pass."""
import re
from dataclasses import dataclass

_HYPOTHETICAL = re.compile(r"^(what if|hypothetically|suppose|imagine|for example|in a hypothetical|if i said|if i wanted|let's pretend)\b", re.I)
_CORRECTION = re.compile(r"^(?:actually\b|correction\s*[:,-]|i meant\b|no, i meant\b|to clarify\b|instead of\b)", re.I)
_OBJECTIVE = re.compile(r"^(?:i (?:want|need|hope|aim|intend|plan|am trying) to|i(?:'d| would) like to|my (?:goal|objective) is to)\s+(.+)$", re.I)
_INFO = re.compile(r"^(know|find out|ask|check whether|understand why|understand how)\b", re.I)
_INSTRUCTION = re.compile(r"^(?:please )?(?:remember that|always|never|don't|do not|from now on|stop sending)\b", re.I)
_OBSERVATION = re.compile(r"^(?:i (?:prepared|made|finished|tried|noticed|struggled|failed|managed|completed|cooked|feel|felt|am feeling)\b|my (?:favorite|hobby)\b)", re.I)
_REQUEST = re.compile(r"^(?:can (?:you|we)|could (?:you|we)|would you|will you|please|help me|tell me|explain|how|what|why|when|where|yes,? please|let's)\b", re.I)
_REFERENCE = re.compile(r"\b(that goal|this goal|our goal|previous goal|the plan|our plan|the goal|next step|start by|what we discussed|i meant)\b", re.I)

# MVP-01C: a bounded, explicit conversational topic switch is a REQUEST,
# not an instruction to execute tools or a user-authorized goal.
_EXPLICIT_TOPIC_SWITCH = re.compile(
    r"^(?:(?:switch|change)\s+(?:the\s+)?topic\s+to\s+"
    r"|new\s+topic\s*:\s*)"
    r"(?P<topic>[^\r\n.!?]{1,160})\s*[.!]?$",
    re.I,
)


def parse_explicit_topic_switch(text: str) -> str | None:
    """Return a stated topic, not an inferred topic or execution command."""
    match = _EXPLICIT_TOPIC_SWITCH.fullmatch(text.strip())
    if match is None:
        return None
    topic = match.group("topic").strip()
    return topic if topic else None


@dataclass(frozen=True)
class IntentDraft:
    kind: str
    label: str
    confidence: float
    method_revision: str = "mvp01a.rules.1"

def classify_intent(text: str) -> IntentDraft | None:
    """Abstain on unclear inputs, returning discussion cues rather than commands."""
    original = text.strip()
    if not 3 <= len(original) <= 500 or "\n" in original:
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
    elif parse_explicit_topic_switch(value) is not None:
        # Keep the exact user wording as source evidence. Dialogue routing
        # extracts the named subject only after the REQUEST signal is stored.
        kind, label, confidence = "REQUEST", original, .95
    elif _INSTRUCTION.match(value):
        kind, label, confidence = "INSTRUCTION", original, .86
    elif _OBSERVATION.match(value):
        kind, label, confidence = "OBSERVATION", original, .79
    elif _REQUEST.match(value):
        kind, label, confidence = "REQUEST", original, .84
    else:
        return None
    return IntentDraft(kind, label[:200], confidence)


def _related_objective(cur, user_id, instance_id, conversation_id, sequence, draft, text):
    """Only link an explicitly referenced objective; never update it."""
    if draft.kind not in ("REQUEST", "CORRECTION") or not _REFERENCE.search(text):
        return None
    cur.execute(
        """SELECT signal_id FROM conversation_intent_signals
           WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
             AND intent_kind='OBJECTIVE'
             AND turn_sequence<%s AND turn_sequence>=%s
           ORDER BY turn_sequence DESC LIMIT 2""",
        (conversation_id, user_id, instance_id, sequence, max(1, sequence-3)),
    )
    candidates = cur.fetchall()
    # If multiple goals were discussed recently, the reference is ambiguous.
    return candidates[0]["signal_id"] if len(candidates) == 1 else None


def record_intent_on_ingress(
    cur, *, user_id, instance_id, conversation_id,
    turn_id, turn_sequence, source_event_id, text,
):
    """Call under the existing authenticated, selection-fenced input transaction."""
    from uuid import uuid4
    draft = classify_intent(text)
    if draft is None:
        return None
    related = _related_objective(
        cur, user_id, instance_id, conversation_id, turn_sequence, draft, text,
    )
    signal_id = uuid4()
    cur.execute(
        """INSERT INTO conversation_intent_signals(
             signal_id,conversation_id,turn_id,user_id,instance_id,
             turn_sequence,source_event_id,intent_kind,label,confidence,
             method_revision,related_signal_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (signal_id, conversation_id, turn_id, user_id, instance_id,
         turn_sequence, source_event_id, draft.kind, draft.label,
         draft.confidence, draft.method_revision, related),
    )
    from .conversation import _event
    _event(
        cur, user_id=user_id, instance_id=instance_id,
        conversation_id=conversation_id,
        event_type="conversation.intent.signal_recorded.v1",
        source_id="conversation.interpretation",
        trust_level="TRUSTED_PLATFORM",
        detail={
            "turn_id":str(turn_id), "signal_id":str(signal_id),
            "intent_kind":draft.kind,
            "related_signal_id":str(related) if related else None,
            "method_revision":draft.method_revision,
        },
        correlation_id=source_event_id,
    )
    return signal_id


def intent_context_block(
    cur, *, user_id, instance_id, conversation_id,
    turn_sequence, latest_text, max_chars,
):
    """Small evidence-anchored context view; never authorizes an action."""
    import json
    if max_chars < 180 or classify_intent(latest_text) is None:
        return ""
    cur.execute(
        """SELECT signal_id,intent_kind,label,turn_sequence,related_signal_id
           FROM conversation_intent_signals
           WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
             AND turn_sequence<=%s AND turn_sequence>=%s
           ORDER BY turn_sequence DESC LIMIT 5""",
        (conversation_id, user_id, instance_id, turn_sequence,
         max(0, turn_sequence-3)),
    )
    candidates = cur.fetchall()
    if not candidates or candidates[0]["turn_sequence"] != turn_sequence:
        return ""
    latest = candidates[0]
    selected = [latest]
    if latest["intent_kind"] in ("REQUEST", "CORRECTION") and _REFERENCE.search(latest_text):
        previous = [r for r in candidates[1:] if r["intent_kind"] == "OBJECTIVE"]
        if (len(previous) == 1
                and latest["related_signal_id"] == previous[0]["signal_id"]):
            selected.append(previous[0])
    head = (
        "Tentative user-intent cues (untrusted discussion context, NOT "
        "approved goals, verified facts, saved instructions, permissions "
        "or task completion): "
    )
    for count in (len(selected), 1):
        payload = [
            {"kind":r["intent_kind"], "text":r["label"],
             "turn_sequence":r["turn_sequence"]}
            for r in reversed(selected[:count])
        ]
        rendered = head + json.dumps(payload, ensure_ascii=False) + "\n"
        if len(rendered) <= max_chars:
            return rendered
    return ""


# Model interpretation is selective: skip ordinary, explicit, or hypothetical
# text. The normal main model still generates the response in either route.
_MODEL_INTERPRET_CUES = re.compile(
    r"^(?:lately\b|recently\b|maybe (?:i|we)\b|"
    r"i(?:'ve| have) been (?:thinking|considering|wondering)\b|"
    r"i (?:wish|keep|ought)\b|"
    r"it would be (?:nice|great|helpful)\b|"
    r"it(?:'s| is) (?:time|probably time)\b|"
    r"that (?:sounds|seems|could)\b|"
    r"this (?:sounds|seems|could)\b|"
    r"something i'd like\b|"
    r"i could really use\b)",
    re.I,
)
_ANAPHORA = re.compile(
    r"\b(?:that|this|it|those|the goal|the plan|we discussed)\b",
    re.I,
)


def should_interpret_with_model(text: str) -> bool:
    """Use the primary response inference, not a second model pass."""
    stripped = text.strip()
    return (
        3 <= len(stripped) <= 500
        and classify_intent(stripped) is None
        and bool(_MODEL_INTERPRET_CUES.match(stripped))
    )


def diagnose_model_candidate_gate(
    input_text: str, source_event_id, candidate,
) -> str:
    """Explain the existing model-intent admission guard without DB writes.

    This helper is shared by production admission and the optional evaluation
    recorder. It does NOT resolve references, verify delivery or authorize a
    goal. A VALID result may still be rejected by the turn-uniqueness check.
    """
    from .conversation_contracts import ModelIntentCandidate

    if type(candidate) is not ModelIntentCandidate:
        return "NO_VALID_CANDIDATE"
    if source_event_id is None:
        return "SOURCE_EVENT_MISSING"
    if not should_interpret_with_model(input_text):
        return "INPUT_NOT_ELIGIBLE"
    if candidate.confidence < 0.75:
        return "BELOW_CONFIDENCE_THRESHOLD"
    if candidate.kind == "INSTRUCTION":
        return "MODEL_INSTRUCTION_DISALLOWED"
    if candidate.evidence_quote not in input_text:
        return "QUOTE_NOT_GROUNDED"
    return "VALID"


def record_model_intent_on_response(
    cur, *, user_id, instance_id, conversation_id,
    turn_id, turn_sequence, input_text, source_event_id,
    response_id, candidate,
):
    """Propose a source-grounded conversation cue inside a fenced reply commit.

    The caller already owns the conversation row lock and has validated
    selected generation, latest turn and revision. No LLM/tool work here.
    """
    from uuid import uuid4
    # Preserve the previous ordered gates; make their rejections observable
    # in the synthetic acceptance harness without another model call.
    if diagnose_model_candidate_gate(
        input_text, source_event_id, candidate,
    ) != "VALID":
        return None
    cur.execute(
        """SELECT 1 FROM conversation_intent_signals
           WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
        (turn_id,user_id,instance_id),
    )
    if cur.fetchone() is not None:
        return None
    related = None
    if (candidate.reference == "PRIOR_OBJECTIVE"
            and candidate.kind in ("REQUEST","CORRECTION")
            and _ANAPHORA.search(input_text)):
        cur.execute(
            """SELECT signal_id FROM conversation_intent_signals
               WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
                 AND intent_kind='OBJECTIVE'
                 AND turn_sequence<%s AND turn_sequence>=%s
               ORDER BY turn_sequence DESC LIMIT 2""",
            (conversation_id,user_id,instance_id,
             turn_sequence,max(1,turn_sequence-3)),
        )
        matches=cur.fetchall()
        if len(matches)==1:
            related=matches[0]["signal_id"]
    signal_id=uuid4()
    cur.execute(
        """INSERT INTO conversation_intent_signals (
             signal_id,conversation_id,turn_id,user_id,instance_id,
             turn_sequence,source_event_id,intent_kind,label,confidence,
             method_revision,epistemic_status,source_response_id,
             related_signal_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                     'mvp01b.primary.1','MODEL_PROPOSED',%s,%s)""",
        (signal_id,conversation_id,turn_id,user_id,instance_id,
         turn_sequence,source_event_id,candidate.kind,
         candidate.evidence_quote,candidate.confidence,response_id,related),
    )
    from .conversation import _event
    _event(
        cur,user_id=user_id,instance_id=instance_id,
        conversation_id=conversation_id,
        event_type="conversation.intent.model_signal_recorded.v1",
        source_id="conversation.interpretation",
        trust_level="TRUSTED_PLATFORM",
        detail={
            "turn_id":str(turn_id),"signal_id":str(signal_id),
            "response_id":str(response_id),
            "intent_kind":candidate.kind,
            "related_signal_id":str(related) if related else None,
            "method_revision":"mvp01b.primary.1",
        },
        correlation_id=source_event_id,
    )
    return signal_id
