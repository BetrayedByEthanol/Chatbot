"""Source-backed, narrowly routed recall of explicit communication preferences.

Relationship owns durable preference state. Conversation owns authenticated
first-party turns (used only for an untracked, exact planning instruction).
No model inference, memory promotion, new goal or procedure authority.
"""
from __future__ import annotations

import re
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .communication_preference_rules import parse_explicit_sentence_limit
from .contracts import TrustedActor
from .conversation import (
    commit_text_response, lookup_response, prepare_text_context, respond_to_text,
)
from .conversation_contracts import ModelReply, PreferenceReadFence, TextResponseResult
from .operation_contracts import require_idle
from .relationship_state import (
    PreferenceView, read_relationship_state,
    has_saved_relationship_preference_history,
)
from .response_decisions import (
    preference_recall_topics, is_historical_emoji_preference_query,
)

ROUTE_ID = "conversation.preference_recall.v1"

# Anchored patterns avoid treating quoted third-party speech, speculation or
# one-off requests for a particular plan as global user preferences.
_NO_BIG_PLAN = (
    re.compile(r"(?:please )?(?:don't|do not) make (?:me )?(?:a )?big plans?(?: for me)?(?:; i just want to chat)?"),
    re.compile(r"i (?:don't|do not) want (?:a )?big plans?"),
)
_ALLOW_BIG_PLAN = (
    re.compile(r"(?:actually,? )?(?:i changed my mind,? )?(?:you can|feel free to) make (?:me )?(?:a )?big plans?(?: for me)?(?: now| from now on)?"),
)
_ONE_OFF_PLAN = re.compile(
    r"(?:please )?make (?:me )?a big plan for (?:the |my )[a-z -]{3,80}"
)


def _normalize_instruction(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().replace("’", "'")).casefold().rstrip(".!?")


def _planning_instruction(
    connection: Connection, actor: TrustedActor, instance_id: UUID, turn_id: UUID,
) -> str | None:
    """Return the latest *supported* explicit planning preference.

    This is scoped to the user's own already responded turns, including prior
    conversations of the selected persona. Bounded retrieval can abstain; it
    must not interpret arbitrary project plans or assistant messages as a
    persistent preference. An instruction to make one specific plan is not
    a blanket revocation of an earlier 'no big plans' preference.
    """
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT t.input_text
               FROM conversation_turns t
               JOIN conversation_turns anchor ON anchor.turn_id=%s
                 AND anchor.user_id=t.user_id
                 AND anchor.instance_id=t.instance_id
               WHERE t.user_id=%s AND t.instance_id=%s
                 AND t.status='RESPONDED' AND t.created_at<anchor.created_at
                 AND char_length(t.input_text)<=200
                 AND t.input_text ILIKE '%%big plan%%'
               ORDER BY t.created_at DESC,t.turn_id DESC
               LIMIT 48""",
            (turn_id,actor.user_id,instance_id),
        )
        candidates=[row["input_text"] for row in cur.fetchall()]
    for text in candidates:
        normalized=_normalize_instruction(text)
        if any(p.fullmatch(normalized) for p in _ALLOW_BIG_PLAN):
            return "ALLOWED"
        if any(p.fullmatch(normalized) for p in _NO_BIG_PLAN):
            return ("NO_BIG_PLANS_CHAT" if normalized.endswith(
                "; i just want to chat") else "NO_BIG_PLANS")
        if _ONE_OFF_PLAN.fullmatch(normalized):
            continue  # A bounded request for one plan does not revise a preference.
        # A newer statement about big plans may be a correction outside this
        # exact grammar. Abstain rather than resurrect an obsolete preference.
        return None
    return None


def _active_preference(
    preferences: tuple[PreferenceView, ...], key: str,
) -> PreferenceView | None:
    return next(
        (p for p in preferences
         if p.preference_key==key and p.scope_key=="" and p.status=="ACTIVE"),
        None,
    )


def _render_length(preference: PreferenceView | None) -> str:
    if preference is None:
        return "I don't have an active saved response-length preference."
    if preference.preference_value=="NORMAL":
        return "Your latest saved preference is normal-length replies."
    if preference.preference_value=="DETAILED":
        return "You asked for detailed replies."
    if preference.preference_value=="SHORT":
        instruction=parse_explicit_sentence_limit(preference.source_quote)
        if instruction is None:
            return "You prefer short replies."
        answer="You asked me to keep replies to one or two sentences"
        if instruction.exception=="DETAILED_INSTRUCTIONS":
            return answer+" unless you request detailed instructions."
        if instruction.exception=="MORE_DETAIL":
            return answer+" unless you ask for more detail."
        return answer+"."
    return "I can't establish the current response-length preference."


def _render_planning(preference: str | None) -> str:
    if preference=="NO_BIG_PLANS_CHAT":
        return "You asked me not to make big plans, and said you just wanted to chat."
    if preference=="NO_BIG_PLANS":
        return "You asked me not to make big plans."
    if preference=="ALLOWED":
        return "You later said I could make big plans for you."
    return "I don't have a confirmed planning preference to recall."


def _render_emoji(preference: PreferenceView | None) -> str:
    if preference is None:
        return "I don't have an active saved emoji preference."
    if preference.preference_value=="OFF":
        return "You asked me not to use emojis."
    if preference.preference_value=="ON":
        return "You said emojis are okay when appropriate."
    return "I can't establish the current emoji preference."


def _render_emoji_history(
    preference: PreferenceView | None, ever_saved: bool,
) -> str:
    """Historical existence, unlike current preference, survives REMOVE."""
    if preference is not None:
        return "Yes. "+_render_emoji(preference)+" That preference is still active."
    if ever_saved:
        # History contains the removed preference's value and source quote,
        # but the user only asked whether one had ever been specified.
        return ("Yes. You previously had a saved emoji preference, "
                "but it was removed and no emoji preference is currently active.")
    return "I don't have a saved record of you specifying an emoji preference."


def respond_with_preference_recall(
    connection: Connection, actor: TrustedActor, turn_id: UUID,
) -> TextResponseResult:
    """Generate a bounded fact-only reply through normal response fencing.

    Trusted routing is revalidated against the latest authenticated input.
    Lookup handles idempotent replay; the usual Conversation commit enforces
    selected-persona and latest-turn guards. No external call or DB lock is
    retained while constructing the reply.
    """
    require_idle(connection)
    prior=lookup_response(connection,actor,turn_id)
    if prior is not None:
        class NoRegeneration:
            def generate(self,messages):
                raise AssertionError("persisted preference reply must not regenerate")
        return respond_to_text(connection,actor,turn_id,NoRegeneration())

    context=prepare_text_context(connection,actor,turn_id)
    topics=preference_recall_topics(context.messages[-1].content)
    if topics is None:
        raise ValueError("preference recall requires an explicit direct question")
    snapshot=read_relationship_state(connection,actor,context.instance_id)
    historical_emoji=(
        "EMOJI_USE" in topics and
        is_historical_emoji_preference_query(context.messages[-1].content)
    )
    ever_saved_emoji=(
        has_saved_relationship_preference_history(
            connection,actor,context.instance_id,"EMOJI_USE",
        ) if historical_emoji else False
    )
    planning=(
        _planning_instruction(connection,actor,context.instance_id,turn_id)
        if "PLANNING" in topics else None
    )
    parts=[]
    for topic in topics:
        if topic=="PLANNING":
            parts.append(_render_planning(planning))
        elif topic=="RESPONSE_LENGTH":
            parts.append(_render_length(
                _active_preference(snapshot.preferences,"RESPONSE_LENGTH"),
            ))
        elif topic=="EMOJI_USE":
            current=_active_preference(snapshot.preferences,"EMOJI_USE")
            parts.append(
                _render_emoji_history(current,ever_saved_emoji)
                if historical_emoji else _render_emoji(current)
            )
        else:
            raise ValueError("unsupported preference recall topic")

    # Fence the relevant Relationship revisions; the domain remains the
    # authority and may change between this read and the short commit.
    keys=tuple(t for t in topics if t!="PLANNING")
    revisions=tuple(sorted(
        (p.preference_key,p.status,p.version)
        for p in snapshot.preferences
        if p.scope_key=="" and p.preference_key in keys
    ))
    from dataclasses import replace
    context=replace(context,preference_fence=PreferenceReadFence(keys,revisions))
    return commit_text_response(
        connection,actor,context,
        ModelReply(model_id=ROUTE_ID,text=" ".join(parts)),
    )
