"""Source-linked report projection, not a Memory write/admission API."""
from uuid import UUID

from .memory_explicit_grammar import parse_explicit_claim


def record_explicit_memory_report(
    cur, *, user_id: UUID, instance_id: UUID, conversation_id: UUID,
    turn_id: UUID, source_event_id: UUID, text: str,
) -> None:
    """Called in authenticated ingress, after recording its source event.

    Safe reports retain their existing projection. A recognized unsupported
    personal definition records a value-free read barrier only. No inference,
    candidate, Memory revision, approval or effect authority is granted.
    The asynchronous owner still consumes the original input event.
    """
    claim=parse_explicit_claim(text)
    if claim is None:
        from .personal_meaning_grammar import unresolved_meaning_topic
        topic=unresolved_meaning_topic(text)
        if topic is not None:
            cur.execute(
                """INSERT INTO memory_meaning_change_notices(
                       source_turn_id,user_id,instance_id,conversation_id,source_event_id,predicate_key
                   ) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(source_turn_id) DO NOTHING""",
                (turn_id,user_id,instance_id,conversation_id,source_event_id,
                 "meaning_"+topic.replace(" ","_")),
            )
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
