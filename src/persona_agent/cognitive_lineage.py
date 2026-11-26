"""Cognition-owned interpretation eligibility, using Memory-owned source policy.

No new evidence or execution authority. The fixed r alias denotes a scoped
reflection; the whole model-visible pass is its conservative dependency.
"""
from .memory_read_policy import eligible_summary_turn_sql
from .meta_episodic import episode_dependencies_current_sql


def reflection_lineage_current_sql() -> str:
    """A missing/legacy job, missing input, or stale episode fails closed."""
    return f"""EXISTS (
        SELECT 1 FROM cognitive_jobs j
        WHERE j.operation_id=r.operation_id AND j.user_id=r.user_id
          AND j.instance_id=r.instance_id AND j.conversation_id=r.conversation_id
          AND j.committed_at IS NOT NULL
          AND j.source_turn_ids IS NOT NULL
          AND j.source_episode_ids IS NOT NULL
          AND j.source_episode_hashes IS NOT NULL
          AND j.source_digest IS NOT NULL
          AND r.evidence_turn_id=ANY(j.source_turn_ids)
          AND (SELECT count(*) FROM conversation_turns t
               WHERE t.user_id=j.user_id AND t.instance_id=j.instance_id
                 AND t.conversation_id=j.conversation_id
                 AND t.turn_id=ANY(j.source_turn_ids)
                 AND t.sequence BETWEEN j.start_sequence AND j.end_sequence
                 AND {eligible_summary_turn_sql()})=cardinality(j.source_turn_ids)
          AND {episode_dependencies_current_sql()}
    )"""
