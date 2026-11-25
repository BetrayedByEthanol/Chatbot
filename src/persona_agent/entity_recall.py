"""Canonical private entity recall with delivered adjacency and provenance.

Render only from current eligible Memory-owned identities. A name collision
is not a ranking problem. No model-generated name or pronoun resolution
may create an identity or grant authority.
"""
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from .entity_reference import EntityReference, RELATIONS, entity_reference

ROUTE_ID="conversation.entity_recall.v1"
POLICY_REVISION="mvp05c1.1"


@dataclass(frozen=True)
class EntityAnswer:
    text: str
    status: Literal["KNOWN","UNKNOWN","AMBIGUOUS"]
    entity_id: UUID | None=None
    version: int | None=None
    source_turn_id: UUID | None=None


def render_entity_hits(hits) -> EntityAnswer:
    if not hits:
        return EntityAnswer("I don't have a reliable saved identification for that.","UNKNOWN")
    if len(hits)>1:
        if len(hits)==2:
            choices=[f"your {RELATIONS[hit.relation][1]} {hit.name}" for hit in hits]
            if choices[0].casefold()!=choices[1].casefold():
                return EntityAnswer(f"Do you mean {choices[0]} or {choices[1]}?","AMBIGUOUS")
        return EntityAnswer(
            "More than one saved entity matches. Could you give a distinguishing detail or alias?",
            "AMBIGUOUS",
        )
    hit=hits[0]
    return EntityAnswer(
        f"Your saved report identifies {hit.name} as your {RELATIONS[hit.relation][1]}.",
        "KNOWN",hit.entity_id,hit.version,hit.relation_source_turn_id,
    )


def _adjacent_entity(cur,*,user_id,instance_id,conversation_id,sequence):
    # Only the immediately preceding delivered response may bind a pronoun.
    # An ambiguous/unknown answer, undelivered output, tool response, or
    # intervening topic cannot introduce a persistent entity referent.
    cur.execute(
        """SELECT e.payload->>'entity_id' AS entity_id
           FROM conversation_turns t
           JOIN conversation_responses r ON r.turn_id=t.turn_id
             AND r.conversation_id=t.conversation_id
             AND r.user_id=t.user_id AND r.instance_id=t.instance_id
           JOIN events e ON e.user_id=t.user_id AND e.instance_id=t.instance_id
             AND e.event_type='conversation.response.entity_grounded.v1'
             AND e.trust_level='TRUSTED_PLATFORM'
             AND e.payload->>'response_id'=r.response_id::text
           WHERE t.user_id=%s AND t.instance_id=%s AND t.conversation_id=%s
             AND t.sequence=%s AND r.delivery_status='DELIVERED'
             AND e.payload->>'status'='KNOWN'
           LIMIT 2""",
        (user_id,instance_id,conversation_id,sequence-1),
    )
    rows=cur.fetchall()
    if len(rows)!=1:
        return None
    try:
        return UUID(rows[0]["entity_id"])
    except (ValueError,TypeError,AttributeError):
        return None


def resolve_entity_answer(cur,*,user_id: UUID,instance_id: UUID,
                          conversation_id: UUID,sequence: int,
                          reference: EntityReference) -> EntityAnswer:
    """Current exact lookup inside the existing short response commit.

    The existing 05B revision lock serializes visibility against deferred
    entity writes, including newly inserted same-name matches. No entity
    row lock is taken afterwards. A read can precede a concurrent write
    or see its committed revision; it cannot see a half-written identity.
    """
    from .conversation_contracts import StaleConversation
    from .entity_memory import _read_entities
    cur.execute(
        """SELECT revision FROM historical_recall_revisions
           WHERE user_id=%s AND instance_id=%s FOR SHARE""",
        (user_id,instance_id),
    )
    if cur.fetchone() is None:
        raise StaleConversation("entity recall scope is unavailable")
    entity_id=None
    if reference.adjacent:
        entity_id=_adjacent_entity(
            cur,user_id=user_id,instance_id=instance_id,
            conversation_id=conversation_id,sequence=sequence,
        )
        if entity_id is None:
            return EntityAnswer("Which person or object do you mean?","AMBIGUOUS")
    return render_entity_hits(_read_entities(
        cur,user_id=user_id,instance_id=instance_id,reference=reference,entity_id=entity_id,
    ))


def respond_with_entity_recall(connection,actor,turn_id: UUID):
    from .conversation import (
        commit_text_response,lookup_response,prepare_direct_tool_context,respond_to_text,
    )
    from .conversation_contracts import ModelReply
    from .operation_contracts import require_idle
    require_idle(connection)
    if lookup_response(connection,actor,turn_id) is not None:
        class NoRegeneration:
            def generate(self,messages):
                raise AssertionError("persisted entity reply must not regenerate")
        return respond_to_text(connection,actor,turn_id,NoRegeneration())
    context=prepare_direct_tool_context(connection,actor,turn_id)
    if entity_reference(context.messages[-1].content) is None:
        raise ValueError("entity recall requires a supported explicit reference")
    return commit_text_response(
        connection,actor,context,ModelReply(model_id=ROUTE_ID,text="Entity recall."),
        entity_recall=True,
    )
