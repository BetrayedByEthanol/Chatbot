"""Source-linked report projection, not a Memory write/admission API."""
from uuid import UUID

from .memory_explicit_grammar import parse_explicit_claim


def record_explicit_memory_report(
    cur, *, user_id: UUID, instance_id: UUID, conversation_id: UUID,
    turn_id: UUID, source_event_id: UUID, text: str,
) -> None:
    """Called in authenticated ingress, after recording its source event.

    Only the existing safe grammar is projected. No new category, inference,
    operation, candidate, Memory revision, approval or effect authority.
    The asynchronous owner still consumes the original input event.
    """
    claim=parse_explicit_claim(text)
    if claim is None:
        return
    cur.execute(
        """INSERT INTO explicit_memory_reports(
               source_turn_id,user_id,instance_id,conversation_id,
               source_event_id,memory_kind,subject_key,predicate_key,value_text
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(source_turn_id) DO NOTHING""",
        (turn_id,user_id,instance_id,conversation_id,source_event_id,
         claim.memory_kind,claim.subject_key,claim.predicate_key,claim.value_text),
    )
