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


def _linked_sources_visible_sql(alias: str, source_key: str) -> str:
    """Conservatively exclude exact sources linked to an ineligible identity."""
    return f"""NOT EXISTS (
        SELECT 1 FROM memory_entity_evidence_links el
        JOIN memory_entities en ON en.entity_id=el.entity_id
          AND en.user_id=el.user_id AND en.instance_id=el.instance_id
        WHERE el.user_id={alias}.user_id AND el.instance_id={alias}.instance_id
          AND el.source_turn_id={alias}.{source_key}
          AND (en.record_status<>'ACTIVE' OR en.privacy_class<>'NORMAL'
               OR (en.expires_at IS NOT NULL AND en.expires_at<=clock_timestamp()))
    )"""


def _unmanaged_relationship_source_sql(alias: str,source_key: str) -> str:
    return f"""NOT EXISTS (
        SELECT 1 FROM memory_entity_relationship_revisions er
        WHERE er.user_id={alias}.user_id AND er.instance_id={alias}.instance_id
          AND er.source_turn_id={alias}.{source_key}
    )"""


def visible_record_sql(alias: RecordAlias) -> str:
    a = _alias(alias)
    return f"""(
        {a}.record_status='ACTIVE' AND {a}.privacy_class='NORMAL'
        AND {a}.epistemic_status='USER_REPORTED'
        AND ({a}.expires_at IS NULL OR {a}.expires_at>clock_timestamp())
        AND {_linked_sources_visible_sql(a,"source_turn_id")}
        AND {_unmanaged_relationship_source_sql(a,"source_turn_id")}
    )"""


def unresolved_meaning_record_sql(alias: RecordAlias) -> str:
    a=_alias(alias)
    return f"""(
        {a}.memory_kind='PERSONAL_MEANING' AND {a}.subject_key='user'
        AND EXISTS (
            SELECT 1 FROM memory_meaning_change_notices notice
            JOIN conversation_turns n ON n.turn_id=notice.source_turn_id
              AND n.conversation_id=notice.conversation_id
              AND n.user_id=notice.user_id AND n.instance_id=notice.instance_id
            WHERE notice.user_id={a}.user_id AND notice.instance_id={a}.instance_id
              AND notice.predicate_key={a}.predicate_key
              AND n.created_at>={a}.source_turn_at
              -- An identical, owner-validated reconfirmation may resolve
              -- this notice without rewriting the original revision.
              AND NOT EXISTS (
                  SELECT 1 FROM memory_claim_support support
                  JOIN memory_extraction_candidates c
                    ON c.target_memory_id=support.memory_id
                   AND c.target_version=support.version
                   AND c.user_id=support.user_id AND c.instance_id=support.instance_id
                   AND c.conversation_id=support.conversation_id
                   AND c.source_turn_id=support.source_turn_id
                   AND c.source_quote=support.source_quote
                  JOIN conversation_turns confirmed
                    ON confirmed.turn_id=support.source_turn_id
                   AND confirmed.conversation_id=support.conversation_id
                   AND confirmed.user_id=support.user_id AND confirmed.instance_id=support.instance_id
                   AND confirmed.source_input_event_id=c.source_event_id
                  WHERE support.memory_id={a}.memory_id AND support.version={a}.version
                    AND support.user_id={a}.user_id AND support.instance_id={a}.instance_id
                    AND c.status='CORROBORATED' AND c.memory_kind={a}.memory_kind
                    AND c.subject_key={a}.subject_key AND c.predicate_key={a}.predicate_key
                    AND c.value_text={a}.value_text
                    AND confirmed.created_at>n.created_at
              )
        )
    )"""


def disputed_record_sql(alias: RecordAlias) -> str:
    """Compare unresolved candidates to the current revision, not target_version."""
    a = _alias(alias)
    return f"""(
      {unresolved_meaning_record_sql(a)}
      OR EXISTS (
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
      )
      OR EXISTS (
        SELECT 1 FROM explicit_memory_reports report
        JOIN conversation_turns newer
          ON newer.turn_id=report.source_turn_id
         AND newer.conversation_id=report.conversation_id
         AND newer.user_id=report.user_id AND newer.instance_id=report.instance_id
        WHERE report.user_id={a}.user_id AND report.instance_id={a}.instance_id
          AND report.memory_kind={a}.memory_kind
          AND report.subject_key={a}.subject_key AND report.predicate_key={a}.predicate_key
          AND newer.created_at>{a}.source_turn_at
          AND report.value_text<>{a}.value_text
      )
    )"""


def eligible_record_sql(alias: RecordAlias) -> str:
    return f"({visible_record_sql(alias)} AND NOT {disputed_record_sql(alias)})"


def eligible_report_source_sql(alias: Literal["t","memory_episodes"]) -> str:
    """Do not recover a disputed or superseded exact slot via its raw source.

    An unowned source without a newer conflicting report can still be
    quoted as historical user speech, not a saved fact.
    """
    if alias not in ("t","memory_episodes"):
        raise ValueError("invalid internal source alias")
    source_key="turn_id" if alias=="t" else "source_turn_id"
    return f"""(
      NOT EXISTS (
        SELECT 1 FROM memory_meaning_change_notices notice
        WHERE notice.user_id={alias}.user_id AND notice.instance_id={alias}.instance_id
          AND notice.source_turn_id={alias}.{source_key}
      )
      AND NOT EXISTS (
        SELECT 1 FROM explicit_memory_reports report
        LEFT JOIN memory_records linked
          ON linked.user_id=report.user_id AND linked.instance_id=report.instance_id
         AND linked.memory_kind=report.memory_kind
         AND linked.subject_key=report.subject_key AND linked.predicate_key=report.predicate_key
        JOIN conversation_turns origin
          ON origin.turn_id=report.source_turn_id
         AND origin.conversation_id=report.conversation_id
         AND origin.user_id=report.user_id AND origin.instance_id=report.instance_id
        WHERE report.user_id={alias}.user_id AND report.instance_id={alias}.instance_id
          AND report.source_turn_id={alias}.{source_key}
          AND (
              (linked.memory_id IS NOT NULL AND (
                  NOT {eligible_record_sql("linked")} OR linked.value_text<>report.value_text
              ))
              OR (report.memory_kind='PERSONAL_MEANING' AND EXISTS (
                  SELECT 1 FROM memory_meaning_change_notices notice
                  JOIN conversation_turns newer ON newer.turn_id=notice.source_turn_id
                    AND newer.conversation_id=notice.conversation_id
                    AND newer.user_id=notice.user_id AND newer.instance_id=notice.instance_id
                  WHERE notice.user_id=report.user_id AND notice.instance_id=report.instance_id
                    AND notice.predicate_key=report.predicate_key
                    AND newer.created_at>=origin.created_at
              ))
              OR EXISTS (
                  SELECT 1 FROM explicit_memory_reports later
                  JOIN conversation_turns newer
                    ON newer.turn_id=later.source_turn_id
                   AND newer.conversation_id=later.conversation_id
                   AND newer.user_id=later.user_id AND newer.instance_id=later.instance_id
                  WHERE later.user_id=report.user_id AND later.instance_id=report.instance_id
                    AND later.memory_kind=report.memory_kind
                    AND later.subject_key=report.subject_key AND later.predicate_key=report.predicate_key
                    AND newer.created_at>origin.created_at
                    AND later.value_text<>report.value_text
              )
          )
      )
    )"""


def unmanaged_entity_source_sql(alias: Literal["t","memory_episodes"]) -> str:
    """Registered entity evidence is recalled via its identity owner only.

    Conservatively omit its exact source turns from generic raw/episode
    recall; explicit entity lookup retains current relation and provenance.
    """
    if alias not in ("t","memory_episodes"):
        raise ValueError("invalid internal source alias")
    source_key="turn_id" if alias=="t" else "source_turn_id"
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM memory_entity_revisions er
            WHERE er.user_id={alias}.user_id AND er.instance_id={alias}.instance_id
              AND er.source_turn_id={alias}.{source_key}
        )
        AND {_unmanaged_relationship_source_sql(alias,source_key)}
        AND {_linked_sources_visible_sql(alias,source_key)}
    )"""


def eligible_summary_turn_sql() -> str:
    """Known Memory-owned invalidation of the exact turn aliased as t.

    This is not a general transcript privacy classifier. Ordinary unowned
    reports remain evidence of speech; exact superseded or ineligible
    Memory revisions and disputed explicit reports cannot be reused.
    """
    return f"""(
        {eligible_report_source_sql("t")}
        AND NOT EXISTS (
            SELECT 1 FROM memory_revisions mr
            JOIN memory_records linked ON linked.memory_id=mr.memory_id
              AND linked.user_id=mr.user_id AND linked.instance_id=mr.instance_id
            WHERE mr.user_id=t.user_id AND mr.instance_id=t.instance_id
              AND mr.source_conversation_id=t.conversation_id
              AND mr.source_turn_id=t.turn_id
              AND (mr.version<>linked.version OR NOT {eligible_record_sql("linked")})
        )
    )"""


def summary_range_current_sql(alias: Literal["conversation_summaries", "s"]) -> str:
    """Check all admitted summary inputs, not merely an episode citation.

    Legacy rows have no exact input set; conservatively check their whole
    original range and follow-up origins delivered before summary commit.
    Do not guess historical eligibility or promote derived verification.
    """
    if alias not in ("conversation_summaries","s"):
        raise ValueError("invalid internal summary alias")
    a=alias
    scope=f"""t.user_id={a}.user_id AND t.instance_id={a}.instance_id
              AND t.conversation_id={a}.conversation_id"""
    return f"""(
        CASE WHEN {a}.source_turn_ids IS NOT NULL THEN
            (SELECT count(*) FROM conversation_turns t
             WHERE {scope} AND t.turn_id=ANY({a}.source_turn_ids)
               AND {eligible_summary_turn_sql()})=cardinality({a}.source_turn_ids)
        ELSE
            (SELECT count(*) FROM conversation_turns t
             WHERE {scope} AND t.sequence BETWEEN {a}.start_sequence AND {a}.end_sequence)
             =({a}.end_sequence-{a}.start_sequence+1)
            AND NOT EXISTS (
                SELECT 1 FROM conversation_turns t
                WHERE {scope}
                  AND (t.sequence BETWEEN {a}.start_sequence AND {a}.end_sequence
                       OR EXISTS (
                           SELECT 1 FROM conversation_assistant_followups f
                           JOIN conversation_turns d ON d.turn_id=f.delivery_turn_id
                             AND d.conversation_id=f.conversation_id
                             AND d.user_id=f.user_id AND d.instance_id=f.instance_id
                           WHERE f.user_id=t.user_id AND f.instance_id=t.instance_id
                             AND f.conversation_id=t.conversation_id AND f.turn_id=t.turn_id
                             AND d.sequence BETWEEN {a}.start_sequence AND {a}.end_sequence
                             AND f.delivered_at<={a}.created_at
                       ))
                  AND NOT {eligible_summary_turn_sql()}
            )
        END
    )"""


def eligible_episode_source_sql() -> str:
    """Conservative exact-turn lineage, not semantic claim matching.

    A multi-topic source may lose an otherwise useful episode when one
    linked record is superseded or ineligible. The original remains stored.
    """
    return f"""(
        {eligible_report_source_sql("memory_episodes")}
        AND {unmanaged_entity_source_sql("memory_episodes")}
        AND EXISTS (
            SELECT 1 FROM conversation_summaries s
            WHERE s.summary_id=memory_episodes.summary_id
              AND s.conversation_id=memory_episodes.conversation_id
              AND s.user_id=memory_episodes.user_id
              AND s.instance_id=memory_episodes.instance_id
              AND {summary_range_current_sql("s")}
        )
        AND NOT EXISTS (
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


def capture_historical_read_fence(cur, user_id: UUID, instance_id: UUID):
    """Observe revision before reading optional historical context.

    Any tracked evidence change invalidates the response, even if another
    slot changed. Also retain the next normal active expiry, because time
    can change eligibility without a database write.
    """
    from .conversation_contracts import HistoricalReadFence, StaleConversation
    cur.execute(
        """SELECT revision,LEAST(
               (SELECT min(m.expires_at) FROM memory_records m
                WHERE m.user_id=r.user_id AND m.instance_id=r.instance_id
                  AND m.record_status='ACTIVE' AND m.privacy_class='NORMAL'
                  AND m.expires_at>clock_timestamp()),
               (SELECT min(e.expires_at) FROM memory_entities e
                WHERE e.user_id=r.user_id AND e.instance_id=r.instance_id
                  AND e.record_status='ACTIVE' AND e.privacy_class='NORMAL'
                  AND e.expires_at>clock_timestamp()),
               (SELECT min(k.expires_at) FROM memory_knowledge_documents k
                WHERE k.user_id=r.user_id AND k.instance_id=r.instance_id
                  AND k.record_status='ACTIVE' AND k.privacy_class='NORMAL'
                  AND k.expires_at>clock_timestamp())
           ) AS next_expiry
           FROM historical_recall_revisions r WHERE user_id=%s AND instance_id=%s""",
        (user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None:
        raise StaleConversation("historical recall revision is unavailable")
    return HistoricalReadFence(row["revision"],row["next_expiry"])


def require_historical_read_fence(cur, user_id: UUID, instance_id: UUID, fence) -> None:
    """Short commit lock orders this reply before later evidence commits.

    Deferred write triggers need this row exclusively before they commit.
    Do not acquire Memory record/candidate locks after this check.
    """
    from .conversation_contracts import StaleHistoricalContext
    cur.execute(
        """SELECT revision FROM historical_recall_revisions
           WHERE user_id=%s AND instance_id=%s FOR SHARE""",
        (user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None or row["revision"]!=fence.revision:
        raise StaleHistoricalContext("historical evidence changed during inference")
    if fence.next_expiry is not None:
        cur.execute("SELECT clock_timestamp() AS now")
        if cur.fetchone()["now"]>=fence.next_expiry:
            raise StaleHistoricalContext("historical evidence expired during inference")
