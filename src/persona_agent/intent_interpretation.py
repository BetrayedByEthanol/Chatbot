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
    # A narrow, source-grounded correction to the current weekly cadence.
    # Match natural variants observed in real model-comparison transcripts,
    # not arbitrary statements starting with "actually".
    r"^actually\s*,?\s*(?:(?:once|twice)(?:\s+times?)?|"
    r"(?:three|four|five|six|seven|\d{1,2})\s+times?)"
    r"\s+(?:per|a)\s+week\s+"
    r"(?:would\s+be\s+better|is\s+(?:more\s+realistic|better))\s*[.!]?$",
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
    r"^(?:(?:let(?:'s| us)\s+)?(?:switch|change)\s+(?:the\s+)?topics?\s+to\s+"
    r"|(?:let(?:'s| us)\s+)?switch\s+to\s+"
    r"|new\s+topic\s*:\s*)"
    r"(?P<topic>[^\r\n.!?]{1,160})\s*[.!]?$",
    re.I,
)
# Only named, explicit return requests qualify. A colon-delimited question
# is conversational context, not part of the topic title.
_EXPLICIT_TOPIC_RETURN = re.compile(
    r"^(?:(?:anyway\s*,?\s*)?back\s+to\s+"
    r"|let(?:'s| us)\s+(?:go\s+back|return)\s+to\s+"
    r"|(?:can|could)\s+we\s+(?:go\s+back|return)\s+to\s+)"
    r"(?P<topic>[^\r\n:!?.]{1,100}?)"
    r"(?:\s*:\s*[^\r\n]{1,120})?\s*[.?!]?$",
    re.I,
)
# Pronouns/deictic phrases refer to the focused topic, not a new named
# discussion. Leave them to existing anaphoric conversation/memory handling.
_ANAPHORIC_TOPIC_NAME = re.compile(
    r"^(?:it|this|that|them|those|these|there|"
    r"(?:(?:the|that|this|our|my|your|previous|earlier|last|same)\s+)"
    r"(?:topic|discussion|subject|conversation|one|plan|goal|thing)|"
    r"(?:the|our)\s+(?:previous|last|same)\s+"
    r"(?:topic|discussion|subject|conversation)|"
    r"what\s+we\s+were\s+(?:discussing|talking\s+about)|"
    r"where\s+we\s+left\s+off)$",
    re.I,
)


def _named_topic_or_none(topic: str) -> str | None:
    value = topic.strip()
    if not value or _ANAPHORIC_TOPIC_NAME.fullmatch(value):
        return None
    return value


def is_anaphoric_topic_return(text: str) -> bool:
    """Return requests resolved by existing focused-dialogue anaphora logic."""
    match = _EXPLICIT_TOPIC_RETURN.fullmatch(text.strip().replace("’", "'"))
    return bool(
        match and _ANAPHORIC_TOPIC_NAME.fullmatch(match.group("topic").strip())
    )


_TOPIC_PROGRESS_RECALL = re.compile(
    r"^where\s+(?:(?:did|do)\s+we\s+leave\s+off"
    r"|(?:had|did)\s+i\s+(?:stop|stopped))\s+with\s+"
    r"(?P<topic>[^\r\n.!?]{1,100})\s*\?\s*$",
    re.I,
)


# A local pause followed by an explicit named subject is navigation,
# not cancellation of a persisted operation or authorization to resume one.
_PAUSE_AND_SWITCH = re.compile(
    r"^(?:please\s+)?(?:don't|do not)\s+"
    r"(?:troubleshoot|debug|plan|work on that|continue)\s+"
    r"(?:yet|now|for now|right now)[.;]\s*"
    r"let(?:'s| us)\s+(?:talk about|discuss)\s+"
    r"(?P<topic>[^:\r\n.!?]{1,120}?)(?:\s+instead)?\s*[.!]?$",
    re.I,
)


def parse_explicit_topic_switch(text: str) -> str | None:
    """A clear named topic switch, not a hypothetical or permission to act."""
    value = text.strip().replace("’", "'")
    match = _EXPLICIT_TOPIC_SWITCH.fullmatch(value) or _PAUSE_AND_SWITCH.fullmatch(value)
    if match is None:
        return None
    return _named_topic_or_none(match.group("topic"))


def parse_explicit_topic_return(text: str) -> str | None:
    """A named return; no claim that an earlier topic was found or resumed."""
    match = _EXPLICIT_TOPIC_RETURN.fullmatch(text.strip().replace("’", "'"))
    if match is None:
        return None
    return _named_topic_or_none(match.group("topic"))


def explicit_topic_history_request(text: str) -> str | None:
    """Named retrieval cues only; ordinary topic mentions never search history."""
    named = parse_explicit_topic_return(text)
    if named is not None:
        return named
    match = _TOPIC_PROGRESS_RECALL.fullmatch(text.strip().replace("’", "'"))
    if match is None:
        return None
    return _named_topic_or_none(match.group("topic"))


@dataclass(frozen=True)
class IntentDraft:
    kind: str
    label: str
    confidence: float
    method_revision: str = "mvp01a.rules.1"

def _current_pragmatic_cue(text: str) -> str | None:
    from .pragmatic_response import pragmatic_cue
    return pragmatic_cue(text)


def _record_change_request(text: str) -> bool:
    from .contextual_interpretation import is_record_change_request
    return is_record_change_request(text)


def _direct_observation(text: str) -> bool:
    from .contextual_interpretation import is_direct_observation
    return is_direct_observation(text)


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
    elif (cue := _current_pragmatic_cue(value)) is not None:
        # Current conversational constraints are source-linked discussion
        # signals, never durable preferences or commands to other owners.
        kind = ("INSTRUCTION" if cue in ("CHAT_ONLY", "NO_QUESTIONS", "NO_GOAL")
                else "OBSERVATION")
        return IntentDraft(kind, original[:200], .90, "mvp04c.pragmatic.1")
    elif match := _OBJECTIVE.match(value):
        subject = match.group(1).strip().rstrip(" .!?")
        if not subject or _INFO.match(subject):
            kind, label, confidence = "REQUEST", original, .80
        else:
            kind, label, confidence = "OBJECTIVE", subject, .88
    elif _FREQUENCY_NOTE.fullmatch(value):
        kind, label, confidence = "OBSERVATION", original, .80
    elif (parse_explicit_topic_switch(value) is not None
          or parse_explicit_topic_return(value) is not None):
        # Keep the exact user wording as source evidence. Dialogue routing
        # extracts the named subject only after the REQUEST signal is stored.
        kind, label, confidence = "REQUEST", original, .95
    elif _INSTRUCTION.match(value) or _record_change_request(value):
        kind, label, confidence = "INSTRUCTION", original, .86
    elif _OBSERVATION.match(value) or _direct_observation(value):
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
    if _current_pragmatic_cue(value) is not None:
        return None
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


def explicit_unresolved_named_topic(text: str) -> str | None:
    """Explicit discussion switch, not a generic request for past recall.

    The caller must verify that no matching topic is already present
    in this conversation. A found but ambiguous topic is NOT a new topic.
    """
    value=text.strip().replace("’","'")
    match=_NAMED_GOAL_REFERENCE.fullmatch(value)
    if match is None or not re.match(
        r"^let(?:'s| us) (?:discuss|talk about)\b",value,re.I,
    ):
        return None
    return match.group("name").casefold() + " plan"


def _unique_active_objective(
    cur, user_id, instance_id, conversation_id, sequence, *, window: int,
):
    """Find a single objective attached to the focused AUTO discussion."""
    cur.execute(
        """SELECT s.signal_id
           FROM current_dialogue_intents s
           JOIN dialogue_turn_links l
             ON l.turn_id=s.turn_id AND l.conversation_id=s.conversation_id
            AND l.user_id=s.user_id AND l.instance_id=s.instance_id
           JOIN conversations c
             ON c.conversation_id=s.conversation_id AND c.user_id=s.user_id
            AND c.instance_id=s.instance_id
           JOIN LATERAL (
               SELECT d.* FROM current_dialogue_threads d
               WHERE d.thread_id=l.thread_id AND d.conversation_id=l.conversation_id
                 AND d.user_id=l.user_id AND d.instance_id=l.instance_id
               OFFSET 0
           ) d ON true
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
           FROM current_dialogue_intents s
           JOIN dialogue_turn_links l
             ON l.turn_id=s.turn_id AND l.conversation_id=s.conversation_id
            AND l.user_id=s.user_id AND l.instance_id=s.instance_id
           JOIN LATERAL (
               SELECT d.* FROM current_dialogue_threads d
               WHERE d.thread_id=l.thread_id AND d.conversation_id=l.conversation_id
                 AND d.user_id=l.user_id AND d.instance_id=l.instance_id
               OFFSET 0
           ) d ON true
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
        if (parse_explicit_topic_switch(text) is not None
                or explicit_topic_history_request(text) is not None):
            # The explicit subject wins over generic words in its follow-up
            # (e.g. "Back to Nginx: what's the next step?"). Never attach it
            # to an unrelated recent OBJECTIVE via _REFERENCE.
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
        """SELECT signal_id FROM current_dialogue_intents
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
    turn_sequence, latest_text, max_chars,dependencies=None,
):
    """Small evidence-anchored context view; never authorizes an action."""
    import json
    if max_chars < 180 or classify_intent(latest_text) is None:
        return ""
    cur.execute(
        """SELECT signal_id,intent_kind,label,turn_sequence,related_signal_id,
                  source_ids,broad_dependency
           FROM current_dialogue_intents
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
    if latest["intent_kind"] in ("REQUEST", "CORRECTION") and (
        latest["intent_kind"] == "CORRECTION" or _REFERENCE.search(latest_text)
    ):
        previous = [
            r for r in candidates[1:]
            if r["intent_kind"] == "OBJECTIVE"
            and r["signal_id"] == latest["related_signal_id"]
        ]
        if len(previous) == 1:
            selected.append(previous[0])
    head = (
        "Tentative user-intent cues (untrusted discussion context, NOT "
        "approved goals, verified facts, saved instructions, permissions "
        "or task completion). Linked user corrections revise an earlier "
        "tentative position; do not present the old and corrected positions "
        "as equally current: "
    )
    for count in (len(selected), 1):
        payload = [
            {"kind":r["intent_kind"], "text":r["label"],
             "turn_sequence":r["turn_sequence"]}
            for r in reversed(selected[:count])
        ]
        rendered = head + json.dumps(payload, ensure_ascii=False) + "\n"
        if len(rendered) <= max_chars and (
            dependencies is None or dependencies.admit(*selected[:count])
        ):
            return rendered
    return ""




# Targeted historical objective-correction view. A model needs the corrected
# source statement, not just a semantically similar but superseded original.
# This is a read-only Conversation view, not a new Memory/Goal store.
_CADENCE_RECALL = re.compile(
    r"^(?:what (?:was|is) my|was my)\s+"
    r"(?:(?:latest|updated|original)\s+)?"
    r"(?P<topic>[a-z][a-z -]{1,60}?)\s+"
    r"(?:plan|target|routine|schedule)"
    r"(?:\s+[^?\r\n]{1,100})?\?\s*$",
    re.I,
)


def correction_precedence_context_block(
    cur, *, user_id, instance_id, before_at, latest_text: str,
    max_chars: int,
) -> str:
    """Expose an unambiguous original + latest linked cadence correction.

    Only explicit *named* plan/target recollection qualifies. Retrieval is
    scoped to this user and selected persona, and all cited source turns must
    predate the requesting turn and have been answered. Multiple matching
    objective roots or missing linked corrections cause abstention.

    A correction revises the user's tentative discussion position, NEVER an
    authorized Goal. This function never writes state, resolves intent, or
    licenses a tool action.
    """
    import json
    from .historical_user_turns import _SENSITIVE

    if max_chars < 450 or len(latest_text) > 220 or _SENSITIVE.search(latest_text):
        return ""
    match = _CADENCE_RECALL.fullmatch(latest_text.strip().replace("’", "'"))
    if match is None:
        return ""
    topic = match.group("topic").strip()
    # A single *named* activity is required; reject broad or empty topics.
    if not topic or len(topic.split()) > 3:
        return ""
    cur.execute(
        """SELECT source.signal_id,source.conversation_id,
                  source.turn_sequence,source.epistemic_status,
                  original.turn_id,original.input_text,original.created_at,
                  EXISTS(SELECT 1 FROM current_dialogue_intents v WHERE v.signal_id=source.signal_id
                         AND v.user_id=source.user_id AND v.instance_id=source.instance_id
                         AND v.conversation_id=source.conversation_id OFFSET 0)
                      AS source_current,
                  EXISTS (
                      SELECT 1 FROM memory_revisions revision
                      WHERE revision.user_id=original.user_id
                        AND revision.instance_id=original.instance_id
                        AND revision.source_turn_id=original.turn_id
                  ) AS has_memory_revision,
                  EXISTS (
                      SELECT 1 FROM memory_extraction_candidates candidate
                      WHERE candidate.user_id=original.user_id
                        AND candidate.instance_id=original.instance_id
                        AND candidate.source_turn_id=original.turn_id
                  ) AS has_memory_candidate
           FROM conversation_intent_signals source
           JOIN conversation_turns original
             ON original.turn_id=source.turn_id
            AND original.conversation_id=source.conversation_id
            AND original.user_id=source.user_id
            AND original.instance_id=source.instance_id
           WHERE source.user_id=%s AND source.instance_id=%s
             AND source.intent_kind='OBJECTIVE'
             AND original.status='RESPONDED'
             AND original.created_at<%s
             AND to_tsvector('english',source.label)
                 @@ plainto_tsquery('english',%s)
           ORDER BY original.created_at DESC,source.signal_id DESC LIMIT 2""",
        (user_id, instance_id, before_at, topic),
    )
    roots = cur.fetchall()
    # A turn represented in Memory's revision/candidate workflow must not
    # be independently resurfaced as current by Conversation retrieval.
    # Check all possible roots *before* selecting one.
    if (len(roots) != 1
            or not roots[0]["source_current"]
            or roots[0]["has_memory_revision"]
            or roots[0]["has_memory_candidate"]
            or _SENSITIVE.search(roots[0]["input_text"])):
        return ""
    root = roots[0]
    # Latest related CORRECTION controls this source chain. If that change
    # is not specifically a cadence correction, we cannot infer the current
    # cadence and must not resurrect an older frequency.
    cur.execute(
        """SELECT change.turn_id,change.input_text,change.created_at,
                  signal.turn_sequence,signal.epistemic_status,
                  EXISTS(SELECT 1 FROM current_dialogue_intents v WHERE v.signal_id=signal.signal_id
                         AND v.user_id=signal.user_id AND v.instance_id=signal.instance_id
                         AND v.conversation_id=signal.conversation_id OFFSET 0)
                      AS source_current,
                  EXISTS (
                      SELECT 1 FROM memory_revisions revision
                      WHERE revision.user_id=change.user_id
                        AND revision.instance_id=change.instance_id
                        AND revision.source_turn_id=change.turn_id
                  ) AS has_memory_revision,
                  EXISTS (
                      SELECT 1 FROM memory_extraction_candidates candidate
                      WHERE candidate.user_id=change.user_id
                        AND candidate.instance_id=change.instance_id
                        AND candidate.source_turn_id=change.turn_id
                  ) AS has_memory_candidate
           FROM conversation_intent_signals signal
           JOIN conversation_turns change
             ON change.turn_id=signal.turn_id
            AND change.conversation_id=signal.conversation_id
            AND change.user_id=signal.user_id
            AND change.instance_id=signal.instance_id
           WHERE signal.user_id=%s AND signal.instance_id=%s
             AND signal.conversation_id=%s
             AND signal.related_signal_id=%s
             AND signal.intent_kind='CORRECTION'
             AND signal.turn_sequence>%s
             AND change.status='RESPONDED'
             AND change.created_at<%s
           ORDER BY signal.turn_sequence DESC,signal.signal_id DESC LIMIT 1""",
        (user_id, instance_id, root["conversation_id"], root["signal_id"],
         root["turn_sequence"], before_at),
    )
    correction = cur.fetchone()
    if (correction is None
            or not correction["source_current"]
            or correction["has_memory_revision"]
            or correction["has_memory_candidate"]
            or _SENSITIVE.search(correction["input_text"])
            or _FREQUENCY_CORRECTION.fullmatch(
                correction["input_text"].strip().replace("’", "'")
            ) is None):
        return ""
    # An objective can leave the bounded linkage window while its discussion
    # continues. A newer unlinked frequency amendment then has no source ID,
    # so "latest linked" is NOT necessarily the latest user position.
    # Inspect the remainder of the *source conversation*, never a different
    # user's/persona's messages. Other-topic corrections that have no link
    # are indistinguishable from an amendment: abstain on cadence matches.
    # A newer linked correction excluded above because it was not RESPONDED
    # also prevents a confident current-position assertion.
    #
    # The bounded result limit is fail-closed: if it cannot establish the
    # absence of relevant later corrections, do not emit stale context.
    cur.execute(
        """SELECT later.input_text, later.status,
                  signal.related_signal_id
           FROM conversation_intent_signals signal
           JOIN conversation_turns later
             ON later.turn_id=signal.turn_id
            AND later.conversation_id=signal.conversation_id
            AND later.user_id=signal.user_id
            AND later.instance_id=signal.instance_id
           WHERE signal.user_id=%s AND signal.instance_id=%s
             AND signal.conversation_id=%s
             AND signal.intent_kind='CORRECTION'
             AND signal.turn_sequence>%s
             AND (signal.related_signal_id IS NULL
                  OR signal.related_signal_id=%s)
             AND later.created_at<%s
           ORDER BY signal.turn_sequence DESC,signal.signal_id DESC
           LIMIT 33""",
        (user_id, instance_id, root["conversation_id"],
         correction["turn_sequence"], root["signal_id"], before_at),
    )
    later = cur.fetchall()
    if len(later) > 32:
        return ""
    for change in later:
        if change["related_signal_id"] == root["signal_id"]:
            # The latest linked correction would have been selected if
            # responded; a later incomplete turn cannot be silently skipped.
            return ""
        if _FREQUENCY_CORRECTION.fullmatch(
            change["input_text"].strip().replace("’", "'")
        ):
            return ""

    heading = (
        "Source-linked conversation correction (USER_REPORTED discussion, "
        "not an approved Goal, verified fact, or instruction). The latest "
        "linked user correction revises the earlier tentative cadence; do "
        "not present both cadences as equally current. Other unobserved "
        "changes remain possible: "
    )
    data = {
        "topic": topic,
        "original": {
            "turn_id": str(root["turn_id"]),
            "date": root["created_at"].date().isoformat(),
            "text": root["input_text"][:200],
            "epistemic_status": root["epistemic_status"],
        },
        "latest_linked_correction": {
            "turn_id": str(correction["turn_id"]),
            "date": correction["created_at"].date().isoformat(),
            "text": correction["input_text"][:200],
            "epistemic_status": correction["epistemic_status"],
        },
        "approved_goal": "NOT_INFERRED_FROM_DISCUSSION",
    }
    result = heading + json.dumps(data, ensure_ascii=False) + "\n"
    # Never emit a dangling original without its correction due to budget.
    return result if len(result) <= max_chars else ""



def resolved_objective_context_block(
    cur, *, user_id, instance_id, conversation_id, turn_id,
    latest_text: str, max_chars: int,dependencies=None,
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
                  source.source_ids||thread.source_ids AS source_ids,
                  (source.broad_dependency OR thread.broad_dependency) AS broad_dependency,
                  source.turn_sequence AS objective_sequence,
                  current.turn_sequence AS current_sequence
           FROM conversation_intent_signals current
           JOIN LATERAL (
               SELECT source.* FROM current_dialogue_intents source
               WHERE source.signal_id=current.related_signal_id
                 AND source.conversation_id=current.conversation_id
                 AND source.user_id=current.user_id
                 AND source.instance_id=current.instance_id
               OFFSET 0
           ) source ON true
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
           JOIN LATERAL (
               SELECT thread.* FROM current_dialogue_threads thread
               WHERE thread.thread_id=link.thread_id
                 AND thread.conversation_id=link.conversation_id
                 AND thread.user_id=link.user_id
                 AND thread.instance_id=link.instance_id
               OFFSET 0
           ) thread ON true
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
        """SELECT cue.intent_kind,cue.label,cue.turn_sequence,
                  eligible.source_ids,eligible.broad_dependency
           FROM conversation_intent_signals cue
           LEFT JOIN LATERAL (
               SELECT eligible.* FROM current_dialogue_intents eligible
               WHERE eligible.signal_id=cue.signal_id
                 AND eligible.user_id=cue.user_id AND eligible.instance_id=cue.instance_id
                 AND eligible.conversation_id=cue.conversation_id
               OFFSET 0
           ) eligible ON true
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
    rows=cur.fetchall()
    corrections=[row for row in rows if row["intent_kind"]=="CORRECTION"]
    if any(row["source_ids"] is None for row in corrections):
        # Never silently fall back to an older position when a selected
        # correction is no longer eligible.
        return ""

    prefix=(
        "Resolved prior conversation reference (untrusted discussion "
        "evidence, NOT an approved goal, permission, or completed action; "
        "the persisted focus changes only after delivery): "
    )

    def details(selected):
        return [{
            "kind":row["intent_kind"], "text":row["label"][:160],
            "turn":row["turn_sequence"],
        } for row in sorted(selected,key=lambda row:row["turn_sequence"])]

    def render():
        return prefix+json.dumps(data,ensure_ascii=False)+"\n"

    # Corrections are required context, not optional discussion details.
    # First fit the mandatory block, then atomically admit its dependencies.
    # A rejected block must not consume any of the shared source budget.
    data={
        "thread":resolved["title"][:160],
        "thread_status":resolved["resolution_status"],
        "objective":resolved["source_input"][:200],
        "details":details(corrections),
    }
    # Conversation-owned next action is a hint, not an agreed commitment.
    if resolved["next_conversational_action"]:
        data["next_dialogue_hint"]=resolved["next_conversational_action"][:120]
    for _ in range(4):
        if len(render())<=max_chars:
            break
        if "next_dialogue_hint" in data:
            data.pop("next_dialogue_hint")
        elif len(data["objective"])>100:
            data["objective"]=data["objective"][:100]
        elif len(data["thread"])>60:
            data["thread"]=data["thread"][:60]
        else:
            return ""
    if len(render())>max_chars:
        return ""
    if dependencies is not None and not dependencies.admit(resolved,*corrections):
        return ""

    # Optional observations and requests may use only the remaining text
    # and source budgets. They cannot displace a required correction.
    selected=list(corrections)
    for row in rows:
        if row["intent_kind"]=="CORRECTION" or row["source_ids"] is None:
            continue
        data["details"]=details([*selected,row])
        if (len(render())<=max_chars
                and (dependencies is None or dependencies.admit(row))):
            selected.append(row)
        else:
            data["details"]=details(selected)
    return render()


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
            """SELECT signal_id FROM current_dialogue_intents
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
