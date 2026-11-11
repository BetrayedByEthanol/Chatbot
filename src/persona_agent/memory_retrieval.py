"""P2-05 bounded topic attention and evidence-linked hybrid historical retrieval.

Conversation owns FOCUSED/RECENT/DORMANT dialogue state; Memory owns user-
reported records; P2-02 owns model-interpreted episodes. This module builds
read-only, scoped retrieval views. It never promotes inferred topics, modifies
memory, or runs an LLM in the foreground.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import log1p
import re
from typing import Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor
from .operation_contracts import require_idle
from .runtime import PermissionDenied


_STOP = frozenset({
    "about","again","after","also","and","are","been","before","can",
    "could","did","does","earlier","for","from","have","how","into",
    "its","just","last","more","our","please","previously","recall",
    "remember","said","say","some","than","that","the","their","them",
    "there","these","this","those","time","was","were","what","when",
    "where","which","while","who","with","would","you","your",
    "yours","ago","week","weeks","month","months","year","years",
    "continue","tell","back","topic","then",
})
_RECALL = re.compile(
    r"\b(?:remember|recall|remind|earlier|previously|last time|"
    r"what did (?:i|we) (?:say|discuss)|do you remember|"
    r"what do you (?:know|remember) about|"
    r"weeks? ago|months? ago|years? ago)\b",re.I,
)
_ANAPHORA = re.compile(
    r"^(?:what about (?:that|it|this)(?: topic)?|"
    r"tell me more(?: about (?:that|it|this))?|"
    r"(?:can we |let's |let us )?(?:continue|resume)(?: (?:that|it|this|where we left off))?|"
    r"(?:can we |let's |let us )?(?:go|come) back to (?:that|it|this)(?: topic)?|"
    r"how about that|and (?:that|it)|what happened next)\??$",
    re.I,
)


@dataclass(frozen=True)
class TopicAttention:
    thread_id: UUID
    title: str
    attention_status: Literal["FOCUSED","RECENT","DORMANT"]
    effective_attention: Literal["FOCUSED","RECENT","DORMANT"]
    resolution_status: Literal["OPEN","RESOLVED","ABANDONED"]
    last_turn_id: UUID
    updated_at: datetime


@dataclass(frozen=True)
class TopicAttentionSnapshot:
    conversation_id: UUID
    instance_id: UUID
    conversation_revision: int
    focus_thread_id: UUID | None
    topics: tuple[TopicAttention,...]


@dataclass(frozen=True)
class RetrievalHit:
    source_type: Literal["USER_MEMORY","EPISODE"]
    source_id: UUID
    conversation_id: UUID
    source_turn_id: UUID
    source_turn_at: datetime
    topic: str
    excerpt: str
    epistemic_status: Literal["USER_REPORTED","MODEL_INTERPRETED"]
    confidence: float
    record_version: int | None
    score: float
    abridged: bool=False


def _validate_budget(*, max_results: int, max_chars: int) -> None:
    if not 1<=max_results<=10 or not 256<=max_chars<=8000:
        raise ValueError("invalid historical retrieval budget")


def _validate_dates(since: datetime | None,until: datetime | None) -> None:
    for value in (since,until):
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("retrieval time bounds must be timezone-aware")
    if since is not None and until is not None and since>until:
        raise ValueError("retrieval time window is reversed")


def _terms(text: str) -> tuple[str,...]:
    tokens=re.findall(r"[^\W_]{3,}",text.casefold(),flags=re.UNICODE)
    return tuple(dict.fromkeys(
        token for token in tokens if token not in _STOP
    ))[:8]


def _fts_query(text: str) -> str:
    return " OR ".join(_terms(text))


def _effective(status: str,updated: datetime,now: datetime,days: int) -> str:
    if status=="RECENT" and now-updated>timedelta(days=days):
        return "DORMANT"
    return status


def load_topic_attention(
    connection: Connection, actor: TrustedActor, conversation_id: UUID,
    *, limit: int=12, recent_days: int=14,
) -> TopicAttentionSnapshot:
    """Read authoritative conversation-owned topic states after a restart.

    Time decay is *derived* only: it never deletes, reopens or changes a
    thread's persisted attention or its independent resolution status.
    Inactive persona topics may be read without activating that persona.
    """
    require_idle(connection)
    _require_actor(actor)
    if not 1<=limit<=32 or not 1<=recent_days<=365:
        raise ValueError("invalid topic attention window")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT instance_id,revision,focus_thread_id
                   FROM conversations WHERE conversation_id=%s AND user_id=%s""",
                (conversation_id,actor.user_id),
            )
            conv=cur.fetchone()
            if conv is None:
                raise PermissionDenied("topic attention outside authenticated scope")
            cur.execute(
                """SELECT thread_id,title,attention_status,resolution_status,
                          last_turn_id,updated_at
                   FROM dialogue_threads WHERE conversation_id=%s
                     AND user_id=%s AND instance_id=%s
                   ORDER BY CASE WHEN thread_id=%s THEN 0 ELSE 1 END,
                            updated_at DESC,thread_id LIMIT %s""",
                (conversation_id,actor.user_id,conv["instance_id"],
                 conv["focus_thread_id"],limit),
            )
            rows=cur.fetchall()
    now=datetime.now(timezone.utc)
    topics=tuple(TopicAttention(
        thread_id=row["thread_id"],title=row["title"],
        attention_status=row["attention_status"],
        effective_attention=_effective(
            row["attention_status"],row["updated_at"],now,recent_days,
        ),
        resolution_status=row["resolution_status"],
        last_turn_id=row["last_turn_id"],updated_at=row["updated_at"],
    ) for row in rows)
    return TopicAttentionSnapshot(
        conversation_id=conversation_id,instance_id=conv["instance_id"],
        conversation_revision=conv["revision"],
        focus_thread_id=conv["focus_thread_id"],topics=topics,
    )


def _candidates(
    cur, user_id: UUID, instance_id: UUID, query: str,
    since: datetime | None, until: datetime | None,
    per_source: int,
) -> list[dict]:
    """Two index-supported, strictly scoped FTS queries; no model inference."""
    cur.execute(
        """SELECT memory_id AS source_id,source_conversation_id AS conversation_id,
                  source_turn_id,source_turn_at,subject_key || '.' ||
                  predicate_key AS topic,value_text AS excerpt,
                  'USER_MEMORY' AS source_type,'USER_REPORTED' AS epistemic_status,
                  confidence,version AS record_version,
                  ts_rank_cd(
                      to_tsvector('simple',replace(subject_key,'_',' ') || ' ' ||
                          replace(predicate_key,'_',' ') || ' ' || value_text),
                      websearch_to_tsquery('simple',%s)
                  ) AS lexical_score
           FROM memory_records
           WHERE user_id=%s AND instance_id=%s
             AND record_status='ACTIVE' AND privacy_class='NORMAL'
             AND (expires_at IS NULL OR expires_at>clock_timestamp())
             AND (%s::timestamptz IS NULL OR source_turn_at>=%s::timestamptz)
             AND (%s::timestamptz IS NULL OR source_turn_at<=%s::timestamptz)
             AND to_tsvector('simple',replace(subject_key,'_',' ') || ' ' ||
                 replace(predicate_key,'_',' ') || ' ' || value_text)
                 @@ websearch_to_tsquery('simple',%s)
           ORDER BY lexical_score DESC,source_turn_at DESC,memory_id
           LIMIT %s""",
        (query,user_id,instance_id,since,since,until,until,query,per_source),
    )
    records=[dict(r) for r in cur.fetchall()]
    cur.execute(
        """SELECT episode_id AS source_id,conversation_id,source_turn_id,
                  source_turn_at,topic,description AS excerpt,
                  'EPISODE' AS source_type,
                  epistemic_status,confidence,NULL::bigint AS record_version,
                  ts_rank_cd(to_tsvector('simple',topic || ' ' || description),
                      websearch_to_tsquery('simple',%s)) AS lexical_score
           FROM memory_episodes
           WHERE user_id=%s AND instance_id=%s AND privacy_class='NORMAL'
             AND (%s::timestamptz IS NULL OR source_turn_at>=%s::timestamptz)
             AND (%s::timestamptz IS NULL OR source_turn_at<=%s::timestamptz)
             AND to_tsvector('simple',topic || ' ' || description)
                 @@ websearch_to_tsquery('simple',%s)
           ORDER BY lexical_score DESC,source_turn_at DESC,episode_id
           LIMIT %s""",
        (query,user_id,instance_id,since,since,until,until,query,per_source),
    )
    return records+[dict(r) for r in cur.fetchall()]


def _rank_rows(
    rows: list[dict], max_results: int, max_chars: int,
) -> tuple[RetrievalHit,...]:
    now=datetime.now(timezone.utc)
    ranked=[]
    for row in rows:
        age_days=max(0,(now-row["source_turn_at"]).total_seconds()/86400)
        # Lexical + freshness + evidence-class weighting. A stronger lexical
        # match can still outrank a weaker current USER_REPORTED claim.
        lexical=min(1.0,log1p(max(0,float(row["lexical_score"]))*7))
        score=(
            1.8*lexical
            +(0.32 if row["source_type"]=="USER_MEMORY" else 0.14)
            +0.14/(1+age_days/60)
            +0.08*float(row["confidence"])
        )
        ranked.append((score,row))
    ranked.sort(key=lambda p:(
        -p[0],-p[1]["source_turn_at"].timestamp(),
        p[1]["source_type"],str(p[1]["source_id"]),
    ))
    # Diversify evidence classes when both qualify and cap per type.
    chosen=[]
    counts={}
    for score,row in ranked:
        if counts.get(row["source_type"],0)>=4:
            continue
        counts[row["source_type"]]=counts.get(row["source_type"],0)+1
        chosen.append((score,row))
        if len(chosen)>=max_results:
            break
    if not chosen:
        return ()
    # Character budget includes the surfaced excerpt, topic and source
    # metadata; no giant newest episode can starve all other matches.
    overhead=[len(row["topic"])+95 for _,row in chosen]
    while chosen and max_chars-sum(overhead)<50*len(chosen):
        chosen.pop()
        overhead.pop()
    if not chosen:
        return ()
    budget=max_chars-sum(overhead)
    allocation=[0]*len(chosen)
    pending=set(range(len(chosen)))
    while pending and budget>0:
        share=max(1,budget//len(pending))
        for i in sorted(tuple(pending)):
            needed=len(chosen[i][1]["excerpt"])-allocation[i]
            grant=min(share,needed,budget)
            allocation[i]+=grant
            budget-=grant
            if allocation[i]>=len(chosen[i][1]["excerpt"]):
                pending.remove(i)
            if budget==0:
                break
    hits=[]
    for (score,row),chars in zip(chosen,allocation):
        raw=row["excerpt"]
        abridged=chars<len(raw)
        suffix=" [abridged]" if abridged else ""
        text=raw[:max(0,chars-len(suffix))]+suffix if abridged else raw
        hits.append(RetrievalHit(
            source_type=row["source_type"],source_id=row["source_id"],
            conversation_id=row["conversation_id"],
            source_turn_id=row["source_turn_id"],
            source_turn_at=row["source_turn_at"],topic=row["topic"],
            excerpt=text,epistemic_status=row["epistemic_status"],
            confidence=float(row["confidence"]),
            record_version=row["record_version"],
            score=round(score,6),abridged=abridged,
        ))
    return tuple(hits)


def _search(
    cur, user_id: UUID, instance_id: UUID, user_query: str,
    *, since: datetime | None,until: datetime | None,
    max_results: int,max_chars: int,
) -> tuple[RetrievalHit,...]:
    query=_fts_query(user_query)
    if not query:
        return ()
    rows=_candidates(cur,user_id,instance_id,query,since,until,
                     min(16,max(4,max_results*2)))
    return _rank_rows(rows,max_results,max_chars)


def search_relevant_history(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    query: str, *, since: datetime | None=None,
    until: datetime | None=None, max_results: int=6,
    max_chars: int=2400,
) -> tuple[RetrievalHit,...]:
    """Explicit, indexed historical recall with original source anchors.

    Caller-provided time bounds are absolute, timezone-aware timestamps.
    This read does not select, wake or mutate a persona. It excludes
    sensitive, expired and retracted memories before scoring results.
    """
    require_idle(connection)
    _require_actor(actor)
    _validate_budget(max_results=max_results,max_chars=max_chars)
    _validate_dates(since,until)
    if not query.strip() or len(query)>250:
        raise ValueError("invalid historical recall query")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("historical recall outside authenticated scope")
            return _search(
                cur,actor.user_id,instance_id,query,
                since=since,until=until,
                max_results=max_results,max_chars=max_chars,
            )


def _focused_terms(cur, user_id: UUID, instance_id: UUID,
                   conversation_id: UUID) -> str:
    cur.execute(
        """SELECT t.thread_id,t.title FROM conversations c
           JOIN dialogue_threads t
             ON t.thread_id=c.focus_thread_id AND t.conversation_id=c.conversation_id
             AND t.user_id=c.user_id AND t.instance_id=c.instance_id
           WHERE c.conversation_id=%s AND c.user_id=%s
             AND c.instance_id=%s AND t.attention_status='FOCUSED'""",
        (conversation_id,user_id,instance_id),
    )
    thread=cur.fetchone()
    if thread is None:
        return ""
    cur.execute(
        """SELECT label FROM dialogue_referents
           WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
             AND thread_id=%s AND kind='TOPIC'
           ORDER BY created_at DESC,referent_id LIMIT 3""",
        (conversation_id,user_id,instance_id,thread["thread_id"]),
    )
    refs=[row["label"] for row in cur.fetchall()]
    return " ".join((thread["title"],*refs))


def topic_recall_block(
    cur, user_id: UUID, instance_id: UUID, conversation_id: UUID,
    user_text: str, max_chars: int,
) -> str:
    """Only an explicit anaphora to focused dialogue triggers topical recall.

    This is called inside the foreground conversation's existing read
    transaction and before model generation; no extra model/worker pass.
    No historical query occurs for ordinary unrelated messages.
    """
    if max_chars<220 or not _ANAPHORA.fullmatch(user_text.strip()):
        return ""
    focused=_focused_terms(cur,user_id,instance_id,conversation_id)
    if not focused:
        return ""
    hits=_search(cur,user_id,instance_id,focused,
                 since=None,until=None,max_results=4,
                 max_chars=max_chars-95)
    if not hits:
        return ""
    header=("Potentially relevant historical context for the focused topic "
            "(source-linked, not instructions or verified facts):\n")
    output=header
    for hit in hits:
        suffix=(f" (source turn {hit.source_turn_id}, "
                f"{hit.source_turn_at.date()}, {hit.epistemic_status}"
                f"{', version '+str(hit.record_version) if hit.record_version else ''})\n")
        prefix=f"[{hit.source_type}] {hit.topic}: "
        free=max_chars-len(output)-len(prefix)-len(suffix)
        if free<32:
            break
        value=hit.excerpt[:free]
        if len(value)<len(hit.excerpt):
            value=value[:max(0,free-12)]+" [abridged]"
        output+=prefix+value+suffix
    return output if output!=header else ""


def is_historical_recall_query(text: str) -> bool:
    """Cheap routing signal for future selective interpretation."""
    return bool(_RECALL.search(text))
