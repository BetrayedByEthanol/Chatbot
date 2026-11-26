"""Memory-owned private supplied documents and bounded source-linked retrieval."""
from datetime import timedelta
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor,_selection
from .knowledge_contracts import (
    SaveKnowledgeCommand,WithdrawKnowledgeCommand,KnowledgeWriteResult,
    KnowledgePassage,knowledge_approval_phrase,content_digest,split_passages,
)
from .memory import _receipt,_finish,_event,_evidence
from .operation_contracts import require_idle
from .runtime import PermissionDenied,VersionConflict


def _scope(cur,actor,instance_id):
    cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,instance_id))
    if cur.fetchone() is None:
        raise PermissionDenied("knowledge instance outside authenticated scope")


def _approval(cur,actor,command):
    source=_evidence(cur,actor,command.instance_id,command.conversation_id,
                     command.evidence_turn_id,command.evidence_quote)
    if command.approval_turn_id is None or command.approval_turn_id==command.evidence_turn_id:
        raise PermissionDenied("knowledge change requires separate user approval")
    cur.execute(
        """SELECT input_text,created_at,selection_generation FROM conversation_turns
           WHERE conversation_id=%s AND turn_id=%s AND user_id=%s AND instance_id=%s""",
        (command.conversation_id,command.approval_turn_id,actor.user_id,command.instance_id),
    )
    approval=cur.fetchone()
    if (approval is None or approval["input_text"].strip()!=knowledge_approval_phrase(command)
            or approval["selection_generation"]!=command.expected_generation
            or not source["created_at"]<=approval["created_at"]<=source["created_at"]+timedelta(days=1)):
        raise PermissionDenied("knowledge approval is not bound to the exact action")


def _current(cur,actor,command):
    cur.execute(
        """SELECT current_version,record_status FROM memory_knowledge_documents
           WHERE document_id=%s AND user_id=%s AND instance_id=%s FOR UPDATE""",
        (command.document_id,actor.user_id,command.instance_id),
    )
    return cur.fetchone()


def _validate_refs(cur,actor,command):
    # Procedure Manager retains ownership. Only an existing current private
    # variant revision can be referenced; no template publication or run.
    for ref in sorted(command.procedure_refs,key=lambda item:item.variant_id.int):
        cur.execute(
            """SELECT p.current_revision FROM personalized_procedures p
               JOIN personalized_procedure_revisions v ON v.variant_id=p.variant_id
                 AND v.revision=p.current_revision AND v.user_id=p.user_id
                 AND v.instance_id=p.instance_id
               WHERE p.variant_id=%s AND p.user_id=%s AND p.instance_id=%s
               FOR SHARE OF p""",
            (ref.variant_id,actor.user_id,command.instance_id),
        )
        row=cur.fetchone()
        if row is None:
            raise PermissionDenied("procedure reference outside authenticated scope")
        if row["current_revision"]!=ref.revision:
            raise VersionConflict("procedure reference is no longer current")


def save_knowledge(connection: Connection,actor: TrustedActor,
                   command: SaveKnowledgeCommand) -> KnowledgeWriteResult:
    """Store exact supplied text after explicit approval; no network or model work."""
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not SaveKnowledgeCommand:
        raise TypeError("validated knowledge save command required")
    digest=content_digest(command.content)
    passages=split_passages(command.content)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _scope(cur,actor,command.instance_id)
            receipt,prior=_receipt(cur,actor,command.instance_id,"memory.knowledge.save.v1",
                                   command,result_type=KnowledgeWriteResult)
            if prior is not None:
                return prior
            _selection(cur,actor,command.instance_id,command.expected_generation)
            _approval(cur,actor,command)
            _validate_refs(cur,actor,command)
            if command.expires_at is not None:
                cur.execute("SELECT clock_timestamp() AS now")
                if command.expires_at<=cur.fetchone()["now"]:
                    raise ValueError("cannot admit already expired knowledge")
            version=command.expected_version+1
            if command.expected_version==0:
                cur.execute(
                    """INSERT INTO memory_knowledge_documents(
                           document_id,user_id,instance_id,current_version,privacy_class,expires_at
                       ) VALUES (%s,%s,%s,1,%s,%s)
                       ON CONFLICT(document_id) DO NOTHING RETURNING document_id""",
                    (command.document_id,actor.user_id,command.instance_id,
                     command.privacy_class,command.expires_at),
                )
                if cur.fetchone() is None:
                    raise VersionConflict("knowledge document identifier unavailable")
            else:
                current=_current(cur,actor,command)
                if current is None:
                    raise PermissionDenied("knowledge document outside authenticated scope")
                if (current["current_version"]!=command.expected_version
                        or current["record_status"]!="ACTIVE"):
                    raise VersionConflict("knowledge document revision is stale or withdrawn")
                cur.execute(
                    """UPDATE memory_knowledge_documents
                       SET current_version=%s,privacy_class=%s,expires_at=%s
                       WHERE document_id=%s AND user_id=%s AND instance_id=%s""",
                    (version,command.privacy_class,command.expires_at,
                     command.document_id,actor.user_id,command.instance_id),
                )
            event=_event(cur,actor,command.instance_id,command.document_id,version,
                         "memory.knowledge.saved.v1",command.approval_turn_id,receipt,
                         aggregate_key="document_id")
            cur.execute(
                """INSERT INTO memory_knowledge_versions(
                       document_id,version,user_id,instance_id,title,source_kind,source_locator,
                       source_revision,content,content_sha256,privacy_class,expires_at,
                       source_conversation_id,evidence_turn_id,approval_turn_id,event_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (command.document_id,version,actor.user_id,command.instance_id,
                 command.title,command.source_kind,command.source_locator,command.source_revision,
                 command.content,digest,command.privacy_class,command.expires_at,
                 command.conversation_id,command.evidence_turn_id,command.approval_turn_id,event),
            )
            cur.executemany(
                """INSERT INTO memory_knowledge_passages(
                       document_id,version,user_id,instance_id,position,start_offset,end_offset,passage_text
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(command.document_id,version,actor.user_id,command.instance_id,i,start,end,text)
                 for i,(start,end,text) in enumerate(passages,1)],
            )
            if command.procedure_refs:
                cur.executemany(
                    """INSERT INTO memory_knowledge_procedure_refs(
                           document_id,version,user_id,instance_id,variant_id,procedure_revision
                       ) VALUES (%s,%s,%s,%s,%s,%s)""",
                    [(command.document_id,version,actor.user_id,command.instance_id,
                      ref.variant_id,ref.revision) for ref in command.procedure_refs],
                )
            return _finish(cur,receipt,KnowledgeWriteResult(
                document_id=command.document_id,version=version,event_id=event,status="ACTIVE",
            ))


def withdraw_knowledge(connection: Connection,actor: TrustedActor,
                       command: WithdrawKnowledgeCommand) -> KnowledgeWriteResult:
    """Withdraw from recall without silently deleting provenance/history."""
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not WithdrawKnowledgeCommand:
        raise TypeError("validated knowledge withdrawal command required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _scope(cur,actor,command.instance_id)
            receipt,prior=_receipt(cur,actor,command.instance_id,"memory.knowledge.withdraw.v1",
                                   command,result_type=KnowledgeWriteResult)
            if prior is not None:
                return prior
            _selection(cur,actor,command.instance_id,command.expected_generation)
            _approval(cur,actor,command)
            current=_current(cur,actor,command)
            if current is None:
                raise PermissionDenied("knowledge document outside authenticated scope")
            if (current["record_status"]!="ACTIVE"
                    or current["current_version"]!=command.expected_version):
                raise VersionConflict("knowledge document revision is stale or withdrawn")
            cur.execute(
                """UPDATE memory_knowledge_documents SET record_status='WITHDRAWN'
                   WHERE document_id=%s AND user_id=%s AND instance_id=%s""",
                (command.document_id,actor.user_id,command.instance_id),
            )
            event=_event(cur,actor,command.instance_id,command.document_id,command.expected_version,
                         "memory.knowledge.withdrawn.v1",command.approval_turn_id,receipt,
                         aggregate_key="document_id")
            return _finish(cur,receipt,KnowledgeWriteResult(
                document_id=command.document_id,version=command.expected_version,
                event_id=event,status="WITHDRAWN",
            ))


def select_knowledge(cur,*,user_id,instance_id,query,max_results=3,max_chars=2400,
                     document_id=None,variant_id=None):
    """Single-statement current-snapshot read; no raw conversation fallback."""
    from .metamemory import safe_query_key
    feedback_key=safe_query_key("KNOWLEDGE",query)
    cur.execute(
        """SELECT d.document_id,v.version,v.title,v.source_kind,v.source_locator,
                  v.source_revision,v.content_sha256,p.position,p.start_offset,p.end_offset,
                  p.passage_text,v.recorded_at,v.evidence_turn_id,v.approval_turn_id,v.epistemic_status
           FROM memory_knowledge_documents d
           JOIN memory_knowledge_versions v ON v.document_id=d.document_id
             AND v.version=d.current_version AND v.user_id=d.user_id AND v.instance_id=d.instance_id
           JOIN memory_knowledge_passages p ON p.document_id=v.document_id AND p.version=v.version
             AND p.user_id=v.user_id AND p.instance_id=v.instance_id
           WHERE d.user_id=%s AND d.instance_id=%s AND d.record_status='ACTIVE'
             AND d.privacy_class='NORMAL' AND v.privacy_class='NORMAL'
             AND (d.expires_at IS NULL OR d.expires_at>clock_timestamp())
             AND (v.expires_at IS NULL OR v.expires_at>clock_timestamp())
             AND (%s::uuid IS NULL OR d.document_id=%s)
             AND (%s::uuid IS NULL OR EXISTS (
                 SELECT 1 FROM memory_knowledge_procedure_refs k
                 JOIN personalized_procedures proc ON proc.variant_id=k.variant_id
                    AND proc.user_id=k.user_id AND proc.instance_id=k.instance_id
                    AND proc.current_revision=k.procedure_revision
                 WHERE k.document_id=d.document_id AND k.version=v.version
                   AND k.user_id=d.user_id AND k.instance_id=d.instance_id AND k.variant_id=%s
             ))
             AND to_tsvector('simple',p.passage_text) @@ plainto_tsquery('simple',%s)
             AND p.passage_text=substring(v.content FROM p.start_offset+1 FOR p.end_offset-p.start_offset)
             AND v.content_sha256=encode(sha256(convert_to(v.content,'UTF8')),'hex')
           ORDER BY COALESCE((
                SELECT f.outcome='IRRELEVANT'
                FROM memory_retrieval_feedback f
                JOIN memory_recall_observations o ON o.response_id=f.response_id
                  AND o.user_id=f.user_id AND o.instance_id=f.instance_id
                WHERE f.user_id=d.user_id AND f.instance_id=d.instance_id
                  AND o.route='KNOWLEDGE' AND o.query_key=%s
                  AND f.passage_ordinal IS NOT NULL
                  AND f.outcome IN ('RELEVANT','IRRELEVANT')
                  AND (o.evidence_refs->(f.passage_ordinal-1)->>'id')=d.document_id::text
                  AND (o.evidence_refs->(f.passage_ordinal-1)->>'version')=v.version::text
                  AND (o.evidence_refs->(f.passage_ordinal-1)->>'position')=p.position::text
                  AND (o.evidence_refs->(f.passage_ordinal-1)->>'content_sha256')=v.content_sha256
                ORDER BY f.recorded_at DESC,f.feedback_id DESC LIMIT 1
           ),false) ASC,
                    ts_rank_cd(to_tsvector('simple',p.passage_text),plainto_tsquery('simple',%s)) DESC,
                    v.recorded_at DESC,d.document_id,p.position
           LIMIT %s""",
        (user_id,instance_id,document_id,document_id,variant_id,variant_id,query,feedback_key,query,max_results),
    )
    result=[]
    remaining=max_chars
    for row in cur.fetchall():
        cost=sum(len(row[key]) for key in ("title","source_locator","passage_text"))
        cost+=len(row["source_revision"] or "")+180
        if cost>remaining:
            continue
        result.append(KnowledgePassage(**row))
        remaining-=cost
    return tuple(result)


def search_knowledge(connection: Connection,actor: TrustedActor,instance_id: UUID,
                     query: str,*,max_results: int=3,max_chars: int=2400,
                     document_id: UUID | None=None,variant_id: UUID | None=None):
    """Authorized current-document retrieval, optionally linked to current procedure."""
    require_idle(connection)
    _require_actor(actor)
    if (not isinstance(query,str) or not 1<=len(query.strip())<=100
            or "\x00" in query or not 1<=max_results<=8 or not 256<=max_chars<=8000):
        raise ValueError("invalid knowledge query or budget")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _scope(cur,actor,instance_id)
            return select_knowledge(cur,user_id=actor.user_id,instance_id=instance_id,
                                    query=query,max_results=max_results,max_chars=max_chars,
                                    document_id=document_id,variant_id=variant_id)


UNKNOWN="I don't have an eligible saved document passage about that."


def render_knowledge(hits):
    """Verbatim source excerpts, not model claims, recommendations or commands."""
    if not hits:
        return UNKNOWN
    parts=["From your saved sources (supplied text, not independently verified):"]
    for ordinal,hit in enumerate(hits,1):
        revision=f", source revision {hit.source_revision}" if hit.source_revision else ""
        parts.append(
            f"Saved passage {ordinal}: {hit.title} — source: {hit.source_locator} "
            f"(saved version {hit.version}{revision}, character offsets "
            f"[{hit.start_offset}, {hit.end_offset})):\n"
            f"“{hit.passage_text}”"
        )
    return "\n\n".join(parts)


def respond_with_knowledge_recall(connection,actor,turn_id):
    from .conversation import commit_text_response,lookup_response,prepare_text_context,respond_to_text
    from .conversation_contracts import ModelReply,StaleHistoricalContext
    from .knowledge_contracts import knowledge_reference
    require_idle(connection)
    class NoRegeneration:
        def generate(self,messages):
            raise AssertionError("persisted knowledge response must not regenerate")
    for attempt in range(2):
        if lookup_response(connection,actor,turn_id) is not None:
            return respond_to_text(connection,actor,turn_id,NoRegeneration())
        context=prepare_text_context(connection,actor,turn_id)
        if knowledge_reference(context.messages[-1].content) is None:
            raise ValueError("knowledge recall requires a supported question")
        try:
            return commit_text_response(connection,actor,context,
                ModelReply(model_id="conversation.knowledge_recall.v1",text="Knowledge recall."),
                knowledge_recall=True)
        except StaleHistoricalContext:
            if attempt:
                raise
    raise AssertionError("unreachable knowledge recall retry")
