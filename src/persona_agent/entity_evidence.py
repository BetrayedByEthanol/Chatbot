"""Memory-owned explicit identity binding and relationship commands.

Only authenticated typed commands link existing evidence or write a directed
edge. Never infer a link from a lexical hit or from model-generated prose.
"""
import re
from uuid import UUID,uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor,_selection
from .entity_contracts import EntityConflict
from .entity_evidence_contracts import (
    LinkEntityEvidenceCommand,SetEntityRelationshipCommand,
    EntityEvidenceResult,EntityRelationshipResult,RELATION_LABELS,RELATION_KINDS,
)
from .entity_memory import _source,lock_entity_writes
from .entity_reference import RELATIONS,EntityReference,normalize_name
from .memory import _receipt,_finish,_event
from .memory_read_policy import eligible_record_sql,eligible_episode_source_sql
from .operation_contracts import require_idle
from .runtime import PermissionDenied,VersionConflict


def _write_receipt(cur,actor,command,kind,result_type):
    cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id))
    if cur.fetchone() is None:
        raise PermissionDenied("entity evidence outside authenticated scope")
    cur.execute("SELECT 1 FROM persona_selection WHERE user_id=%s FOR SHARE",(actor.user_id,))
    cur.fetchone()
    lock_entity_writes(cur,command.instance_id)
    receipt,prior=_receipt(cur,actor,command.instance_id,kind,command,result_type=result_type)
    if prior is None:
        _selection(cur,actor,command.instance_id,command.expected_generation)
    return receipt,prior


def _endpoint(cur,actor,instance_id,entity_id,version):
    cur.execute(
        """SELECT *, (record_status='ACTIVE' AND privacy_class='NORMAL'
               AND (expires_at IS NULL OR expires_at>clock_timestamp())) AS eligible
           FROM memory_entities WHERE user_id=%s AND instance_id=%s AND entity_id=%s
           FOR SHARE""",(actor.user_id,instance_id,entity_id),
    )
    row=cur.fetchone()
    if row is None:
        raise PermissionDenied("entity endpoint outside authenticated scope")
    if row["version"]!=version:
        raise VersionConflict("entity endpoint version changed")
    if not row["eligible"]:
        raise EntityConflict("entity endpoint is not eligible")
    return row


def _qualified_labels(cur,entity):
    """Only labels that uniquely resolve the explicitly selected identity.

    Require a relation qualifier, even for a currently unique name. This
    avoids linking an unqualified historical mention merely by spelling.
    The owner command explicitly chooses the ID, and the source supports
    the qualifier; this is not evidence of external real-world identity.
    """
    from .entity_memory import _read_entities
    labels=[]
    for label in [entity["name"],*entity["aliases"]]:
        hits=_read_entities(
            cur,user_id=entity["user_id"],instance_id=entity["instance_id"],
            reference=EntityReference(name_key=normalize_name(label),relation=entity["relation"]),
        )
        if len(hits)==1 and hits[0].entity_id==entity["entity_id"]:
            labels.append("my "+RELATIONS[entity["relation"]][1]+" "+label)
    return labels


def _normalized(text):
    return re.sub(r"[ \t]+"," ",text.strip()).replace("’","'").casefold()


def _has_label(text,labels):
    value=_normalized(text)
    return any(re.search(
        r"(?<![a-z'-])"+re.escape(_normalized(label))+r"(?![a-z'-])",value,
    ) for label in labels)


def _safe_source(source):
    from .historical_user_turns import _SENSITIVE
    return not _SENSITIVE.search(source)


def link_entity_evidence(connection: Connection,actor: TrustedActor,
                         command: LinkEntityEvidenceCommand) -> EntityEvidenceResult:
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not LinkEntityEvidenceCommand:
        raise TypeError("validated entity evidence command required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            receipt,prior=_write_receipt(cur,actor,command,"memory.entity.evidence.link.v1",EntityEvidenceResult)
            if prior is not None:
                return prior
            entity=_endpoint(cur,actor,command.instance_id,command.entity_id,command.expected_entity_version)
            source=_source(cur,actor,command)
            if command.source_type=="USER_MEMORY":
                cur.execute(
                    f"""SELECT source_turn_id,source_conversation_id AS conversation_id,source_quote,
                               version, {eligible_record_sql("memory_records")} AS eligible
                        FROM memory_records
                        WHERE user_id=%s AND instance_id=%s AND memory_id=%s FOR SHARE""",
                    (actor.user_id,command.instance_id,command.source_id),
                )
            else:
                cur.execute(
                    f"""SELECT source_turn_id,conversation_id,source_quote,NULL::bigint AS version,
                               (privacy_class='NORMAL' AND {eligible_episode_source_sql()}) AS eligible
                        FROM memory_episodes
                        WHERE user_id=%s AND instance_id=%s AND episode_id=%s FOR SHARE""",
                    (actor.user_id,command.instance_id,command.source_id),
                )
            record=cur.fetchone()
            if record is None:
                raise PermissionDenied("linked evidence outside authenticated scope")
            if record["version"]!=command.expected_memory_version:
                raise VersionConflict("linked Memory revision changed")
            if (not record["eligible"] or record["source_turn_id"]!=command.evidence_turn_id
                    or record["conversation_id"]!=command.conversation_id
                    or record["source_quote"].casefold() not in source["input_text"].casefold()
                    or not _safe_source(source["input_text"])
                    or not _has_label(record["source_quote"],_qualified_labels(cur,entity))):
                raise EntityConflict("source does not support an eligible qualified entity binding")
            memory_id=command.source_id if command.source_type=="USER_MEMORY" else None
            episode_id=command.source_id if command.source_type=="EPISODE" else None
            cur.execute(
                """SELECT link_id,event_id FROM memory_entity_evidence_links
                   WHERE user_id=%s AND instance_id=%s AND entity_id=%s
                     AND memory_id IS NOT DISTINCT FROM %s::uuid
                     AND memory_version IS NOT DISTINCT FROM %s::bigint
                     AND episode_id IS NOT DISTINCT FROM %s::uuid""",
                (actor.user_id,command.instance_id,command.entity_id,memory_id,
                 command.expected_memory_version,episode_id),
            )
            previous=cur.fetchone()
            if previous is not None:
                return _finish(cur,receipt,EntityEvidenceResult(**previous))
            link_id=uuid4()
            event_id=_event(cur,actor,command.instance_id,link_id,1,
                            "memory.entity.evidence.linked.v1",command.evidence_turn_id,
                            receipt,aggregate_key="entity_evidence_id")
            cur.execute(
                """INSERT INTO memory_entity_evidence_links(
                       link_id,user_id,instance_id,entity_id,memory_id,memory_version,
                       episode_id,source_conversation_id,source_turn_id,source_event_id,event_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (link_id,actor.user_id,command.instance_id,command.entity_id,memory_id,
                 command.expected_memory_version,episode_id,command.conversation_id,
                 command.evidence_turn_id,source["source_input_event_id"],event_id),
            )
            return _finish(cur,receipt,EntityEvidenceResult(link_id=link_id,event_id=event_id))


def relationship_source_matches(text,subjects,objects,action,relation):
    """Complete directed relation assertion or explicit pair retraction."""
    if not _safe_source(text):
        return False
    value=_normalized(text).removesuffix(".")
    for subject in subjects:
        for object_name in objects:
            a,b=_normalized(subject),_normalized(object_name)
            if a==b:
                continue
            if action=="RETRACT":
                expected=f"retract saved relationship between {a} and {b}"
                if value==expected:
                    return True
            else:
                expected=f"{a} {RELATION_LABELS[relation]} {b}"
                if action=="CORRECT":
                    if value in ("actually "+expected,"actually, "+expected):
                        return True
                elif value==expected:
                    return True
    return False


def set_entity_relationship(connection: Connection,actor: TrustedActor,
                            command: SetEntityRelationshipCommand) -> EntityRelationshipResult:
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not SetEntityRelationshipCommand:
        raise TypeError("validated entity relationship command required")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            receipt,prior=_write_receipt(
                cur,actor,command,"memory.entity.relationship.set.v1",EntityRelationshipResult,
            )
            if prior is not None:
                return prior
            subject=_endpoint(cur,actor,command.instance_id,command.subject_entity_id,
                              command.expected_subject_version)
            obj=_endpoint(cur,actor,command.instance_id,command.object_entity_id,
                          command.expected_object_version)
            source=_source(cur,actor,command)
            cur.execute(
                """SELECT * FROM memory_entity_relationships
                   WHERE user_id=%s AND instance_id=%s
                     AND subject_entity_id=%s AND object_entity_id=%s FOR UPDATE""",
                (actor.user_id,command.instance_id,command.subject_entity_id,command.object_entity_id),
            )
            old=cur.fetchone()
            if (old["version"] if old else 0)!=command.expected_version:
                raise VersionConflict("relationship version changed")
            if old is not None and (
                old["record_status"]!="ACTIVE" or source["created_at"]<=old["source_turn_at"]
                or command.relation==old["relation"]
            ):
                raise EntityConflict("relationship requires newer distinct evidence")
            relation=command.relation if command.relation is not None else old["relation"]
            if RELATION_KINDS[relation]!=(subject["entity_kind"],obj["entity_kind"]):
                raise EntityConflict("incompatible relationship endpoints")
            if not relationship_source_matches(
                source["input_text"],_qualified_labels(cur,subject),_qualified_labels(cur,obj),
                command.action,relation,
            ):
                raise EntityConflict("source does not identify the requested directed relationship")
            relationship_id=old["relationship_id"] if old else uuid4()
            version=command.expected_version+1
            state="RETRACTED" if command.action=="RETRACT" else "ACTIVE"
            cur.execute(
                """INSERT INTO memory_entity_relationships(
                       relationship_id,user_id,instance_id,subject_entity_id,object_entity_id,
                       relation,version,record_status,source_conversation_id,source_turn_id,source_turn_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(relationship_id) DO UPDATE SET relation=EXCLUDED.relation,
                       version=EXCLUDED.version,record_status=EXCLUDED.record_status,
                       source_conversation_id=EXCLUDED.source_conversation_id,
                       source_turn_id=EXCLUDED.source_turn_id,source_turn_at=EXCLUDED.source_turn_at""",
                (relationship_id,actor.user_id,command.instance_id,command.subject_entity_id,
                 command.object_entity_id,relation,version,state,command.conversation_id,
                 command.evidence_turn_id,source["created_at"]),
            )
            event_id=_event(cur,actor,command.instance_id,relationship_id,version,
                            "memory.entity.relationship."+command.action.lower()+".v1",
                            command.evidence_turn_id,receipt,aggregate_key="entity_relationship_id")
            cur.execute(
                """INSERT INTO memory_entity_relationship_revisions(
                       relationship_id,version,user_id,instance_id,action,relation,record_status,
                       source_conversation_id,source_turn_id,source_turn_at,source_event_id,event_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(source_turn_id) DO NOTHING RETURNING relationship_id""",
                (relationship_id,version,actor.user_id,command.instance_id,command.action,
                 relation,state,command.conversation_id,command.evidence_turn_id,source["created_at"],
                 source["source_input_event_id"],event_id),
            )
            if cur.fetchone() is None:
                raise EntityConflict("relationship source was already used")
            return _finish(cur,receipt,EntityRelationshipResult(
                relationship_id=relationship_id,version=version,event_id=event_id,
            ))
