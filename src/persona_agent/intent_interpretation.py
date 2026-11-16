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
# MVP-01F.2: high-precision conversational details, never Goal-domain writes.
# These patterns abstain on unqualified musings ("thinking about ...") and
# on hypothetical / negative input. They supply a source-grounded fallback
# only after the primary model pass failed to admit a candidate.
_IMPLICIT_ROUTINE = re.compile(
    r"^i(?:'ve| have) been thinking about "
    r"(?P<activity>[a-z][a-z -]{2,65}?) "
    r"(?P<cadence>more regularly|more often|every day|each week)\s*[.!]?$",
    re.I,
)
_FREQUENCY_NOTE = re.compile(
    r"^(?:maybe|perhaps)\s+(?:(?:once|twice)(?:\s+times?)?|"
    r"(?:three|four|five|six|seven|\d{1,2})\s+times?)"
    r"\s+per\s+week\s*[.!]?$", re.I,
)
_FREQUENCY_CORRECTION = re.compile(
    r"^actually[, ]+\s*(?:(?:once|twice)(?:\s+times?)?|"
    r"(?:three|four|five|six|seven|\d{1,2})\s+times?)"
    r"\s+per\s+week\s+would\s+be\s+better\s*[.!]?$",
    re.I,
)
_NAMED_GOAL_REFERENCE = re.compile(
    r"^(?:what about|let(?:'s| us) (?:discuss|talk about)|"
    r"(?:can|could) we (?:discuss|return to))\s+"
    r"(?:that|this|my|our|the)\s+"
    r"(?P<name>[a-z][a-z-]{2,28})\s+(?:goal|plan)"
    r"(?:\s+instead)?\s*[?.!]?$",re.I,
)


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
    elif _FREQUENCY_NOTE.fullmatch(value):
        kind, label, confidence = "OBSERVATION", original, .80
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


def conservative_implicit_objective(text: str) -> IntentDraft | None:
    """Source-grounded routine intent, only if the model failed to admit one.

    Avoid interpreting negation, questions, wishes about others, or vague
    "thinking about" reports as objective commitments. This produces a
    *tentative discussion topic*, never an approved Goal.
    """
    value=text.strip().replace("’","'")
    match=_IMPLICIT_ROUTINE.fullmatch(value)
    if len(value)>200 or "\n" in value or match is None:
        return None
    activity=match.group("activity").casefold()
    # A description of another person or a negated activity is not a
    # user-owned objective, even if it has the same sentence skeleton.
    if activity.startswith((
        "not ","never ","my ","your ","their ","someone ",
        "another ","other people ","you ",
    )):
        return None
    return IntentDraft(
        "OBJECTIVE", text.strip()[:200], 0.85,
        "mvp01f2.routine_fallback.1",
    )


def is_named_objective_reference(text: str) -> bool:
    """Recognize a bounded named goal/plan reference, not a new topic."""
    return _NAMED_GOAL_REFERENCE.fullmatch(
        text.strip().replace("’","'")
    ) is not None


def _unique_active_objective(
    cur, user_id, instance_id, conversation_id, sequence, *, window: int,
):
    """Find a single objective attached to the focused AUTO discussion."""
    cur.execute(
        """SELECT s.signal_id
           FROM conversation_intent_signals s
           JOIN dialogue_turn_links l
             ON l.turn_id=s.turn_id AND l.conversation_id=s.conversation_id
            AND l.user_id=s.user_id AND l.instance_id=s.instance_id
           JOIN conversations c
             ON c.conversation_id=s.conversation_id AND c.user_id=s.user_id
            AND c.instance_id=s.instance_id
           JOIN dialogue_threads d
             ON d.thread_id=l.thread_id AND d.conversation_id=l.conversation_id
            AND d.user_id=l.user_id AND d.instance_id=l.instance_id
           WHERE s.conversation_id=%s AND s.user_id=%s AND s.instance_id=%s
             AND s.intent_kind='OBJECTIVE' AND s.turn_sequence<%s
             AND s.turn_sequence>=%s
             AND l.thread_id=c.focus_thread_id
             AND d.origin_kind='AUTO_SIGNAL' AND d.resolution_status='OPEN'
           ORDER BY s.turn_sequence DESC LIMIT 2""",
        (conversation_id,user_id,instance_id,sequence,
         max(1,sequence-window)),
    )
    matches=cur.fetchall()
    return matches[0]["signal_id"] if len(matches)==1 else None


def _named_objective_reference(
    cur, user_id, instance_id, conversation_id, sequence, text,
):
    """Resolve only explicit named goal/plan references, never fuzzy guesses.

    A single exact source-word match is necessary; two possible objectives
    are ambiguous and cannot redirect dialogue. Match only linked AUTO
    discussion sources from this conversation, not other sessions or users.
    """
    match=_NAMED_GOAL_REFERENCE.fullmatch(text.strip().replace("’","'"))
    if match is None:
        return None
    name=match.group("name").casefold()
    cur.execute(
        """SELECT s.signal_id,s.label
           FROM conversation_intent_signals s
           JOIN dialogue_turn_links l
             ON l.turn_id=s.turn_id AND l.conversation_id=s.conversation_id
            AND l.user_id=s.user_id AND l.instance_id=s.instance_id
           JOIN dialogue_threads d
             ON d.thread_id=l.thread_id AND d.conversation_id=l.conversation_id
            AND d.user_id=l.user_id AND d.instance_id=l.instance_id
           WHERE s.conversation_id=%s AND s.user_id=%s AND s.instance_id=%s
             AND s.intent_kind='OBJECTIVE' AND s.turn_sequence<%s
             AND d.origin_kind='AUTO_SIGNAL' AND d.resolution_status='OPEN'
           ORDER BY s.turn_sequence DESC LIMIT 64""",
        (conversation_id,user_id,instance_id,sequence),
    )
    candidates=[
        row for row in cur.fetchall()
        if re.search(r"\b"+re.escape(name)+r"\b",row["label"].casefold())
    ]
    return candidates[0]["signal_id"] if len(candidates)==1 else None


def _related_objective(cur, user_id, instance_id, conversation_id, sequence, draft, text):
    """Evidence-linked discussion reference; never change or approve a goal."""
    if draft.kind not in ("REQUEST","CORRECTION","OBSERVATION"):
        return None
    if draft.kind=="REQUEST":
        named=_named_objective_reference(
            cur,user_id,instance_id,conversation_id,sequence,text,
        )
        if named is not None:
            return named
        if _NAMED_GOAL_REFERENCE.fullmatch(text.strip().replace("’","'")):
            # Named references must not fall back to a different unnamed goal.
            return None
    if draft.kind=="CORRECTION" and _FREQUENCY_CORRECTION.fullmatch(text.strip()):
        return _unique_active_objective(
            cur,user_id,instance_id,conversation_id,sequence,window=8,
        )
    if draft.kind=="OBSERVATION" and _FREQUENCY_NOTE.fullmatch(text.strip()):
        return _unique_active_objective(
            cur,user_id,instance_id,conversation_id,sequence,window=8,
        )
    if draft.kind not in ("REQUEST","CORRECTION") or not _REFERENCE.search(text):
        return None
    cur.execute(
        """SELECT signal_id FROM conversation_intent_signals
           WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
             AND intent_kind='OBJECTIVE'
             AND turn_sequence<%s AND turn_sequence>=%s
           ORDER BY turn_sequence DESC LIMIT 2""",
        (conversation_id, user_id, instance_id, sequence, max(1, sequence-3)),
    )
    candidates=cur.fetchall()
    return candidates[0]["signal_id"] if len(candidates)==1 else None


def record_intent_on_ingress(
    cur, *, user_id, instance_id, conversation_id,
    turn_id, turn_sequence, source_event_id, text, draft=None,
):
    """Call under the existing authenticated, selection-fenced transaction.

    A supplied draft is a bounded rule-based fallback evaluated against the
    original user input inside the response commit, not a model command.
    """
    from uuid import uuid4
    draft = draft if draft is not None else classify_intent(text)
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



def resolved_objective_context_block(
    cur, *, user_id, instance_id, conversation_id, turn_id,
    latest_text: str, max_chars: int,
) -> str:
    """Read-only, scope-fenced context for a *persisted* named reference.

    The current turn's related_signal_id was resolved by authenticated
    ingress; do not independently guess a goal or focus a thread here.
    This is read during generation, while the conversation's old focus is
    still authoritative until response delivery is acknowledged.
    """
    import json

    if max_chars < 220 or not is_named_objective_reference(latest_text):
        return ""

    cur.execute(
        """SELECT source.signal_id,source.label AS objective,
                  input.input_text AS source_input,
                  thread.thread_id,thread.title,thread.resolution_status,
                  thread.next_conversational_action,
                  source.turn_sequence AS objective_sequence,
                  current.turn_sequence AS current_sequence
           FROM conversation_intent_signals current
           JOIN conversation_intent_signals source
             ON source.signal_id=current.related_signal_id
            AND source.conversation_id=current.conversation_id
            AND source.user_id=current.user_id
            AND source.instance_id=current.instance_id
           JOIN conversation_turns input
             ON input.turn_id=source.turn_id
            AND input.conversation_id=source.conversation_id
            AND input.user_id=source.user_id
            AND input.instance_id=source.instance_id
           JOIN dialogue_turn_links link
             ON link.turn_id=source.turn_id
            AND link.conversation_id=source.conversation_id
            AND link.user_id=source.user_id
            AND link.instance_id=source.instance_id
           JOIN dialogue_threads thread
             ON thread.thread_id=link.thread_id
            AND thread.conversation_id=link.conversation_id
            AND thread.user_id=link.user_id
            AND thread.instance_id=link.instance_id
           WHERE current.turn_id=%s AND current.conversation_id=%s
             AND current.user_id=%s AND current.instance_id=%s
             AND current.intent_kind='REQUEST'
             AND source.intent_kind='OBJECTIVE'
             AND source.turn_sequence<current.turn_sequence
             AND thread.origin_kind='AUTO_SIGNAL'
             AND thread.resolution_status='OPEN'
           LIMIT 2""",
        (turn_id,conversation_id,user_id,instance_id),
    )
    matches=cur.fetchall()
    if len(matches)!=1:
        return ""
    resolved=matches[0]
    # Limited history of the *resolved thread*, not whatever was in focus
    # when this input arrived. Exclude the founding objective itself.
    cur.execute(
        """SELECT cue.intent_kind,cue.label,cue.turn_sequence
           FROM conversation_intent_signals cue
           JOIN dialogue_turn_links link
             ON link.turn_id=cue.turn_id
            AND link.conversation_id=cue.conversation_id
            AND link.user_id=cue.user_id
            AND link.instance_id=cue.instance_id
           WHERE cue.conversation_id=%s AND cue.user_id=%s
             AND cue.instance_id=%s AND link.thread_id=%s
             AND cue.turn_sequence>%s AND cue.turn_sequence<%s
           ORDER BY cue.turn_sequence DESC LIMIT 3""",
        (conversation_id,user_id,instance_id,resolved["thread_id"],
         resolved["objective_sequence"],resolved["current_sequence"]),
    )
    notes=list(reversed([{
        "kind":row["intent_kind"], "text":row["label"][:160],
        "turn":row["turn_sequence"],
    } for row in cur.fetchall()]))

    prefix=(
        "Resolved prior conversation reference (untrusted discussion "
        "evidence, NOT an approved goal, permission, or completed action; "
        "the persisted focus changes only after delivery): "
    )
    # Fit as much as possible while prioritizing the original verbatim user
    # observation. Late corrections are useful but must not crowd it out.
    data={
        "thread":resolved["title"][:160],
        "objective":resolved["source_input"][:200],
        "details":notes,
    }
    for _ in range(6):
        block=prefix+json.dumps(data,ensure_ascii=False)+"\n"
        if len(block)<=max_chars:
            return block
        if data["details"]:
            data["details"].pop(0)
        elif len(data["objective"])>100:
            data["objective"]=data["objective"][:100]
        elif len(data["thread"])>60:
            data["thread"]=data["thread"][:60]
        else:
            break
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
