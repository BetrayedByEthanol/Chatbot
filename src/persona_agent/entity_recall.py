"""Canonical private entity recall with delivered adjacency and provenance.

Render only from current eligible Memory-owned identities. A name collision
is not a ranking problem. No model-generated name or pronoun resolution
may create an identity or grant authority.
"""
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from .entity_reference import (
    EntityReference, RELATIONS, entity_reference, entity_clarification_relation,
)

ROUTE_ID="conversation.entity_recall.v1"
POLICY_REVISION="mvp05c2.1"


@dataclass(frozen=True)
class EntityAnswer:
    text: str
    status: Literal["KNOWN","UNKNOWN","AMBIGUOUS"]
    entity_id: UUID | None=None
    version: int | None=None
    source_turn_id: UUID | None=None
    clarification_candidates: tuple[tuple[str,int,str],...]=()


def render_entity_hits(hits) -> EntityAnswer:
    if not hits:
        return EntityAnswer("I don't have a reliable saved identification for that.","UNKNOWN")
    if len(hits)>1:
        if len(hits)==2:
            choices=[f"your {RELATIONS[hit.relation][1]} {hit.name}" for hit in hits]
            if choices[0].casefold()!=choices[1].casefold():
                return EntityAnswer(
                    f"Do you mean {choices[0]} or {choices[1]}?","AMBIGUOUS",
                    clarification_candidates=tuple(
                        (str(hit.entity_id),hit.version,hit.relation) for hit in hits
                    ),
                )
        return EntityAnswer(
            "More than one saved entity matches. Could you give a distinguishing detail or alias?",
            "AMBIGUOUS",
        )
    hit=hits[0]
    return EntityAnswer(
        f"Your saved report identifies {hit.name} as your {RELATIONS[hit.relation][1]}.",
        "KNOWN",hit.entity_id,hit.version,hit.relation_source_turn_id,
    )


def _adjacent_grounding(cur,*,user_id,instance_id,conversation_id,sequence,
                        include_history=False):
    """The previous response must have been delivered before this input.

    Bind only within one conversation and selection generation. The trusted
    response event records a host-resolved identity, not a model guess.
    History context can anchor a known identity, but cannot supply choices
    for the canonical identity-clarification path.
    """
    cur.execute(
        """SELECT e.payload,t.input_text
           FROM conversation_turns anchor
           JOIN conversation_turns t ON t.conversation_id=anchor.conversation_id
             AND t.user_id=anchor.user_id AND t.instance_id=anchor.instance_id
             AND t.sequence=anchor.sequence-1
           JOIN conversation_responses r ON r.turn_id=t.turn_id
             AND r.conversation_id=t.conversation_id
             AND r.user_id=t.user_id AND r.instance_id=t.instance_id
           JOIN events e ON e.user_id=t.user_id AND e.instance_id=t.instance_id
             AND (e.event_type='conversation.response.entity_grounded.v1'
                  OR (%s AND e.event_type='conversation.response.entity_history_context.v1'
                      AND e.payload->>'status'='KNOWN'))
             AND e.trust_level='TRUSTED_PLATFORM'
             AND e.payload->>'response_id'=r.response_id::text
           WHERE anchor.user_id=%s AND anchor.instance_id=%s AND anchor.conversation_id=%s
             AND anchor.sequence=%s AND r.delivery_status='DELIVERED'
             AND r.selection_generation=anchor.selection_generation
             AND r.delivered_at<=anchor.created_at
             AND NOT EXISTS (
                 SELECT 1 FROM conversation_assistant_followups f
                 WHERE f.user_id=anchor.user_id AND f.instance_id=anchor.instance_id
                   AND f.conversation_id=anchor.conversation_id
                   AND f.delivered_at>r.delivered_at AND f.delivered_at<=anchor.created_at
             )
           LIMIT 2""",
        (include_history,user_id,instance_id,conversation_id,sequence),
    )
    rows=cur.fetchall()
    return rows[0] if len(rows)==1 else None


def _adjacent_entity(cur,**scope):
    row=_adjacent_grounding(cur,include_history=True,**scope)
    if row is None or row["payload"].get("status")!="KNOWN":
        return None
    try:
        return UUID(row["payload"].get("entity_id"))
    except (ValueError,TypeError,AttributeError):
        return None


def adjacent_entity_clarification(cur,**scope):
    row=_adjacent_grounding(cur,**scope)
    if row is None or row["payload"].get("status")!="AMBIGUOUS":
        return None
    # Only the original named lookup (not an arbitrary ambiguous report)
    # with exactly two displayed, persisted choices can be continued.
    reference=entity_reference(row["input_text"])
    if reference is None or reference.adjacent:
        return None
    candidates=row["payload"].get("clarification_candidates")
    if not isinstance(candidates,list) or len(candidates)!=2:
        return None
    try:
        choices=tuple((str(UUID(cid)),int(version),relation)
                      for cid,version,relation in candidates)
    except (ValueError,TypeError,AttributeError):
        return None
    if len({cid for cid,_,_ in choices})!=2 or any(
        v<1 or relation not in RELATIONS for _,v,relation in choices
    ):
        return None
    return reference,choices


def _lock_current_entity_view(cur,user_id,instance_id):
    from .conversation_contracts import StaleConversation
    cur.execute(
        """SELECT revision FROM historical_recall_revisions
           WHERE user_id=%s AND instance_id=%s FOR SHARE""",
        (user_id,instance_id),
    )
    if cur.fetchone() is None:
        raise StaleConversation("entity recall scope is unavailable")


def resolve_entity_clarification(cur,*,relation,**scope):
    """Never apply a clarification as an entity mutation or other authority."""
    from .entity_memory import _read_entities
    pending=adjacent_entity_clarification(cur,**scope)
    if pending is None:
        return None
    reference,choices=pending
    _lock_current_entity_view(cur,scope["user_id"],scope["instance_id"])
    hits=_read_entities(
        cur,user_id=scope["user_id"],instance_id=scope["instance_id"],reference=reference,
    )
    observed=tuple((str(hit.entity_id),hit.version,hit.relation) for hit in hits)
    if observed!=choices:
        return EntityAnswer(
            "The saved matches changed. Please repeat the name or give a distinguishing alias.",
            "AMBIGUOUS",
        )
    selected=tuple(hit for hit in hits if hit.relation==relation)
    if len(selected)!=1:
        return EntityAnswer(
            "That does not identify one of the saved matches. Please use a distinguishing alias.",
            "AMBIGUOUS",
        )
    return render_entity_hits(selected)


def resolve_entity_answer(cur,*,user_id: UUID,instance_id: UUID,
                          conversation_id: UUID,sequence: int,
                          reference: EntityReference) -> EntityAnswer:
    """Current exact lookup inside the existing short response commit.

    The existing 05B revision lock serializes visibility against deferred
    entity writes, including newly inserted same-name matches. No entity
    row lock is taken afterwards. A read can precede a concurrent write
    or see its committed revision; it cannot see a half-written identity.
    """
    from .entity_memory import _read_entities
    _lock_current_entity_view(cur,user_id,instance_id)
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


def respond_with_entity_recall(connection,actor,turn_id: UUID,*,report=False):
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
    from .entity_reference import entity_report
    value=context.messages[-1].content
    if report:
        if entity_report(value) is None:
            raise ValueError("entity report requires a supported explicit report")
        from .entity_conversation import REPORT_ROUTE_ID
        return commit_text_response(
            connection,actor,context,ModelReply(model_id=REPORT_ROUTE_ID,text="Entity report."),
            entity_report=True,
        )
    if entity_reference(value) is None and entity_clarification_relation(value) is None:
        raise ValueError("entity recall requires a supported explicit reference")
    return commit_text_response(
        connection,actor,context,ModelReply(model_id=ROUTE_ID,text="Entity recall."),
        entity_recall=True,
    )
