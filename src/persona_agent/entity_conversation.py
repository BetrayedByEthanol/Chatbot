"""Memory-owned admission of a bounded authenticated conversational report.

Called only during the selected, current-turn fenced response commit.
The original report, owner command receipt, entity revision, events and
reply commit together; a failed or superseded reply cannot claim a write.
No model output is accepted as entity admission evidence.
"""
from .entity_contracts import RegisterEntityCommand,AmendEntityCommand
from .entity_memory import (
    _register_entity_in_transaction,_amend_entity_in_transaction,_read_entities,
)
from .entity_reference import EntityReference,RELATIONS,normalize_name,entity_report
from .entity_recall import EntityAnswer,render_entity_hits

REPORT_ROUTE_ID="conversation.entity_report.v1"


def apply_entity_report(cur,actor,context,scoped):
    """Caller holds the entity owner guard, before any conversation row lock."""
    report=entity_report(scoped["input_text"])
    if report is None:
        raise ValueError("entity admission requires an explicit persisted report")
    user_id,instance_id=actor.user_id,context.instance_id

    def read(entity_id):
        return render_entity_hits(_read_entities(
            cur,user_id=user_id,instance_id=instance_id,
            reference=EntityReference(),entity_id=entity_id,
        ))

    # A host may already have executed the typed command for this source.
    # Never reuse that source to create another identity or revision.
    cur.execute(
        """SELECT entity_id FROM memory_entity_revisions
           WHERE user_id=%s AND instance_id=%s AND source_turn_id=%s""",
        (user_id,instance_id,context.turn_id),
    )
    consumed=cur.fetchone()
    if consumed is not None:
        return read(consumed["entity_id"])
    args=dict(
        instance_id=instance_id,expected_generation=context.selection_generation,
        conversation_id=context.conversation_id,evidence_turn_id=context.turn_id,
        idempotency_key=f"natural-entity:{context.turn_id}",
    )
    # Explicit distinction is not an alias match, and cannot merge entities.
    if report.action=="DISTINCT":
        saved=_register_entity_in_transaction(cur,actor,RegisterEntityCommand(
            **args,name=report.name,relation=report.relation,
        ))
        return read(saved.entity_id)

    # Include ineligible names in admission's absence check, but NEVER
    # expose their names, relation or existence in a response. This avoids
    # resurrecting a retracted/private identity as a new saved identity.
    cur.execute(
        """SELECT entity_id,entity_kind,relation,version,source_turn_at,
                  (record_status='ACTIVE' AND privacy_class='NORMAL'
                   AND (expires_at IS NULL OR expires_at>clock_timestamp())) AS eligible
           FROM memory_entities WHERE user_id=%s AND instance_id=%s
             AND name_keys @> ARRAY[%s]::text[]
           ORDER BY entity_id LIMIT 2""",
        (user_id,instance_id,normalize_name(report.name)),
    )
    matches=cur.fetchall()
    if not matches and report.action=="REPORT":
        saved=_register_entity_in_transaction(cur,actor,RegisterEntityCommand(
            **args,name=report.name,relation=report.relation,
        ))
        return read(saved.entity_id)
    if len(matches)!=1 or not matches[0]["eligible"]:
        return EntityAnswer(
            "Please identify the entity more specifically before I change any saved relationship.",
            "AMBIGUOUS",
        )
    row=matches[0]
    if row["relation"]==report.relation:
        # This is recognition, not another admission or proof that two
        # equal labels identify the same real-world entity.
        return read(row["entity_id"])
    if (report.action!="CORRECT"
            or row["entity_kind"]!=RELATIONS[report.relation][0]):
        return EntityAnswer(
            "Are you correcting a saved relationship, or describing a different entity? "
            "Please identify it explicitly.",
            "AMBIGUOUS",
        )
    cur.execute(
        """SELECT created_at FROM conversation_turns
           WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
        (context.turn_id,user_id,instance_id),
    )
    source=cur.fetchone()
    if source["created_at"]<=row["source_turn_at"]:
        return EntityAnswer(
            "I can't apply that correction to the current saved relationship. Please restate it.",
            "AMBIGUOUS",
        )
    saved=_amend_entity_in_transaction(cur,actor,AmendEntityCommand(
        **args,entity_id=row["entity_id"],expected_version=row["version"],
        action="CORRECT_RELATION",relation=report.relation,
    ))
    return read(saved.entity_id)
