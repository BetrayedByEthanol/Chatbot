"""Persistent identity and user-to-entity relations within the Memory owner.

An authenticated explicit command chooses whether to register a distinct
identity or revise an existing one. Identical labels are never merged.
Whole-source evidence is required, but remains a user report, not external
verification. No conversation model may call these mutation primitives.
"""
import re
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor, _selection
from .entity_contracts import (
    AmendEntityCommand, EntityConflict, EntityHit, EntityWriteResult,
    RegisterEntityCommand,
)
from .entity_reference import EntityReference, RELATIONS, normalize_name
from .memory import _receipt, _finish, _event
from .operation_contracts import require_idle
from .runtime import PermissionDenied, VersionConflict


def _source(cur,actor,command):
    cur.execute(
        """SELECT t.input_text,t.created_at,t.source_input_event_id
           FROM conversation_turns t
           JOIN events e ON e.event_id=t.source_input_event_id
             AND e.user_id=t.user_id AND e.instance_id=t.instance_id
           WHERE t.turn_id=%s AND t.conversation_id=%s
             AND t.user_id=%s AND t.instance_id=%s
             AND e.event_type='conversation.user_text.received.v1'
             AND e.trust_level='AUTHENTICATED_USER'""",
        (command.evidence_turn_id,command.conversation_id,actor.user_id,command.instance_id),
    )
    source=cur.fetchone()
    if source is None:
        raise PermissionDenied("entity evidence outside authenticated input scope")
    return source


def _relation_source(source: str,name: str,relation: str,*,correction: bool) -> bool:
    # Complete assertion only; a quoted/negated/hypothetical substring is
    # not evidence. The chosen entity ID comes from the explicit command.
    prefix=r"actually,?\s+" if correction else r"(?:remember that\s+)?"
    label=RELATIONS[relation][1]
    return re.fullmatch(
        prefix+re.escape(name)+r" is my "+re.escape(label)+r"\.?",
        source.strip(),re.I,
    ) is not None


def _record_revision(cur,actor,command,row,source,receipt,action):
    event_id=_event(
        cur,actor,command.instance_id,row["entity_id"],row["version"],
        "memory.entity."+action.lower()+".v1",command.evidence_turn_id,receipt,
        aggregate_key="entity_id",
    )
    cur.execute(
        """INSERT INTO memory_entity_revisions(
               entity_id,version,user_id,instance_id,action,name,aliases,relation,
               record_status,source_conversation_id,source_turn_id,source_event_id,
               source_turn_at,event_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(source_turn_id) DO NOTHING RETURNING entity_id""",
        (row["entity_id"],row["version"],actor.user_id,command.instance_id,action,
         row["name"],row["aliases"],row["relation"],row["record_status"],
         command.conversation_id,command.evidence_turn_id,source["source_input_event_id"],
         source["created_at"],event_id),
    )
    if cur.fetchone() is None:
        # The entire command, including its event and any inserted identity,
        # rolls back. Reusing one report cannot create a second identity.
        raise EntityConflict("entity source was already used for a revision")
    return _finish(cur,receipt,EntityWriteResult(
        entity_id=row["entity_id"],version=row["version"],event_id=event_id,
    ))


def register_entity(connection: Connection,actor: TrustedActor,
                    command: RegisterEntityCommand) -> EntityWriteResult:
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not RegisterEntityCommand:
        raise TypeError("validated entity registration required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            # Explicit scope check before receipt creation/replay, as Memory.
            cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                        (actor.user_id,command.instance_id))
            if cur.fetchone() is None:
                raise PermissionDenied("entity instance outside user scope")
            receipt,prior=_receipt(cur,actor,command.instance_id,"memory.entity.register.v1",
                                   command,result_type=EntityWriteResult)
            if prior is not None:
                return prior
            _selection(cur,actor,command.instance_id,command.expected_generation)
            source=_source(cur,actor,command)
            if not _relation_source(source["input_text"],command.name,command.relation,correction=False):
                raise EntityConflict("source does not explicitly identify the requested relation")
            entity_id=uuid4()
            cur.execute(
                """INSERT INTO memory_entities(
                       entity_id,user_id,instance_id,entity_kind,name,name_keys,relation,
                       source_turn_at,relation_source_conversation_id,relation_source_turn_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (entity_id,actor.user_id,command.instance_id,RELATIONS[command.relation][0],
                 command.name,[normalize_name(command.name)],command.relation,
                 source["created_at"],command.conversation_id,command.evidence_turn_id),
            )
            return _record_revision(cur,actor,command,cur.fetchone(),source,receipt,"REGISTER")


def amend_entity(connection: Connection,actor: TrustedActor,
                 command: AmendEntityCommand) -> EntityWriteResult:
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not AmendEntityCommand:
        raise TypeError("validated entity amendment required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                        (actor.user_id,command.instance_id))
            if cur.fetchone() is None:
                raise PermissionDenied("entity instance outside user scope")
            receipt,prior=_receipt(cur,actor,command.instance_id,"memory.entity.amend.v1",
                                   command,result_type=EntityWriteResult)
            if prior is not None:
                return prior
            _selection(cur,actor,command.instance_id,command.expected_generation)
            cur.execute(
                """SELECT * FROM memory_entities
                   WHERE entity_id=%s AND user_id=%s AND instance_id=%s FOR UPDATE""",
                (command.entity_id,actor.user_id,command.instance_id),
            )
            row=cur.fetchone()
            if row is None:
                raise PermissionDenied("entity outside authenticated scope")
            if row["version"]!=command.expected_version:
                raise VersionConflict("entity version is stale")
            if row["record_status"]!="ACTIVE":
                raise EntityConflict("entity is retracted")
            source=_source(cur,actor,command)
            if source["created_at"]<=row["source_turn_at"]:
                raise VersionConflict("entity change requires newer evidence")
            text=source["input_text"].strip()
            names=[row["name"],*row["aliases"]]
            relation=row["relation"]
            aliases=list(row["aliases"])
            state=row["record_status"]
            relation_conversation=row["relation_source_conversation_id"]
            relation_turn=row["relation_source_turn_id"]
            if command.action=="CORRECT_RELATION":
                if (command.relation==relation
                    or RELATIONS[command.relation][0]!=row["entity_kind"]
                    or not any(_relation_source(text,name,command.relation,correction=True)
                               for name in names)):
                    raise EntityConflict("unsupported or unevidenced relation correction")
                relation=command.relation
                relation_conversation=command.conversation_id
                relation_turn=command.evidence_turn_id
            elif command.action=="ADD_ALIAS":
                if (len(aliases)>=8 or normalize_name(command.alias) in row["name_keys"]
                    or not any(re.fullmatch(
                        r"i also call "+re.escape(name)+r" "+re.escape(command.alias)+r"\.?",
                        text,re.I,
                    ) for name in names)):
                    raise EntityConflict("unsupported, duplicate or unevidenced alias")
                aliases.append(command.alias)
            else:
                if not any(re.fullmatch(
                    r"forget my saved entity "+re.escape(name)+r"\.?",text,re.I,
                ) for name in names):
                    raise EntityConflict("explicit entity retraction evidence required")
                state="RETRACTED"
            cur.execute(
                """UPDATE memory_entities SET aliases=%s,name_keys=%s,relation=%s,
                       record_status=%s,version=version+1,source_turn_at=%s,
                       relation_source_conversation_id=%s,relation_source_turn_id=%s
                   WHERE entity_id=%s AND user_id=%s AND instance_id=%s RETURNING *""",
                (aliases,[normalize_name(name) for name in [row["name"],*aliases]],
                 relation,state,source["created_at"],relation_conversation,relation_turn,
                 row["entity_id"],actor.user_id,command.instance_id),
            )
            return _record_revision(cur,actor,command,cur.fetchone(),source,receipt,command.action)


def _read_entities(cur,*,user_id: UUID,instance_id: UUID,reference: EntityReference,
                   entity_id: UUID | None=None) -> tuple[EntityHit,...]:
    """Scoped, bounded exact-name/alias or known-ID lookup; no recency tie-break."""
    if reference.adjacent and entity_id is None:
        return ()
    if entity_id is None and reference.name_key is None:
        return ()
    cur.execute(
        """SELECT entity_id,entity_kind,name,relation,version,
                  relation_source_turn_id,source_turn_at
           FROM memory_entities
           WHERE user_id=%s AND instance_id=%s
             AND record_status='ACTIVE' AND privacy_class='NORMAL'
             AND (expires_at IS NULL OR expires_at>clock_timestamp())
             AND (%s::uuid IS NULL OR entity_id=%s)
             AND (%s::text IS NULL OR name_keys @> ARRAY[%s]::text[])
             AND (%s='ANY' OR (%s='PERSON' AND entity_kind='PERSON')
                          OR (%s='NONPERSON' AND entity_kind IN ('PLACE','OBJECT')))
             AND (%s::text IS NULL OR relation=%s)
           ORDER BY entity_id LIMIT 3""",
        (user_id,instance_id,entity_id,entity_id,reference.name_key,reference.name_key,
         reference.kind,reference.kind,reference.kind,reference.relation,reference.relation),
    )
    return tuple(EntityHit(**row) for row in cur.fetchall())


def find_entities(connection: Connection,actor: TrustedActor,instance_id: UUID,
                  name: str,*,relation=None) -> tuple[EntityHit,...]:
    """Explicit scoped candidate lookup; multiple hits mean ambiguous, not a rank."""
    require_idle(connection)
    _require_actor(actor)
    if relation is not None and relation not in RELATIONS:
        raise ValueError("unsupported entity relation")
    reference=EntityReference(name_key=normalize_name(name),relation=relation)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                        (actor.user_id,instance_id))
            if cur.fetchone() is None:
                raise PermissionDenied("entity lookup outside user scope")
            return _read_entities(cur,user_id=actor.user_id,instance_id=instance_id,reference=reference)
