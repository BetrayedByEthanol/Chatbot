"""Shared Memory-owned eligibility for conversational recall.

Only fixed internal aliases are accepted. These read-time guards do not
delete, amend, or repair historical evidence.
"""
from typing import Literal
from uuid import UUID

RecordAlias = Literal["memory_records", "m", "linked", "target"]


def _alias(alias: RecordAlias) -> str:
    if alias not in ("memory_records", "m", "linked", "target"):
        raise ValueError("invalid internal Memory alias")
    return alias


def visible_record_sql(alias: RecordAlias) -> str:
    a = _alias(alias)
    return f"""(
        {a}.record_status='ACTIVE' AND {a}.privacy_class='NORMAL'
        AND {a}.epistemic_status='USER_REPORTED'
        AND ({a}.expires_at IS NULL OR {a}.expires_at>clock_timestamp())
    )"""


def disputed_record_sql(alias: RecordAlias) -> str:
    """Compare unresolved candidates to the current revision, not target_version."""
    a = _alias(alias)
    return f"""EXISTS (
        SELECT 1 FROM memory_extraction_candidates unresolved
        JOIN conversation_turns newer_turn
          ON newer_turn.turn_id=unresolved.source_turn_id
         AND newer_turn.conversation_id=unresolved.conversation_id
         AND newer_turn.user_id=unresolved.user_id
         AND newer_turn.instance_id=unresolved.instance_id
        WHERE unresolved.target_memory_id={a}.memory_id
          AND unresolved.user_id={a}.user_id
          AND unresolved.instance_id={a}.instance_id
          AND unresolved.status='CONFLICT'
          AND newer_turn.created_at>{a}.source_turn_at
          AND unresolved.value_text<>{a}.value_text
    )"""


def eligible_record_sql(alias: RecordAlias) -> str:
    return f"({visible_record_sql(alias)} AND NOT {disputed_record_sql(alias)})"


def eligible_episode_source_sql() -> str:
    """Conservative exact-turn lineage, not semantic claim matching.

    A multi-topic source may lose an otherwise useful episode when one
    linked record is superseded or ineligible. The original remains stored.
    """
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM memory_revisions mr
            JOIN memory_records linked ON linked.memory_id=mr.memory_id
              AND linked.user_id=mr.user_id AND linked.instance_id=mr.instance_id
            WHERE mr.user_id=memory_episodes.user_id
              AND mr.instance_id=memory_episodes.instance_id
              AND mr.source_conversation_id=memory_episodes.conversation_id
              AND mr.source_turn_id=memory_episodes.source_turn_id
              AND (mr.version<>linked.version OR NOT {eligible_record_sql("linked")})
        )
        AND NOT EXISTS (
            SELECT 1 FROM memory_extraction_candidates c
            LEFT JOIN memory_records target ON target.memory_id=c.target_memory_id
              AND target.user_id=c.user_id AND target.instance_id=c.instance_id
            WHERE c.user_id=memory_episodes.user_id
              AND c.instance_id=memory_episodes.instance_id
              AND c.conversation_id=memory_episodes.conversation_id
              AND c.source_turn_id=memory_episodes.source_turn_id
              AND (target.memory_id IS NULL OR NOT {eligible_record_sql("target")}
                   OR c.value_text<>target.value_text)
        )
    )"""


DISPUTE_NOTICE = (
    "A newer user-reported correction to a matching preference is "
    "unresolved. Do not assert the old value; ask for clarification.\n"
)


def has_matching_dispute(cur, user_id: UUID, instance_id: UUID, query: str) -> bool:
    """Only visible, topical owner slots may produce a dispute notice."""
    cur.execute(
        f"""SELECT 1 FROM memory_records
           WHERE user_id=%s AND instance_id=%s
             AND {visible_record_sql("memory_records")}
             AND {disputed_record_sql("memory_records")}
             AND to_tsvector('simple',replace(subject_key,'_',' ')||' '||
                 replace(predicate_key,'_',' ')||' '||value_text)
                 @@ websearch_to_tsquery('simple',%s)
           LIMIT 1""",
        (user_id,instance_id,query),
    )
    return cur.fetchone() is not None
