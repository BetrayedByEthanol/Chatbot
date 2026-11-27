"""P2-07 Relationship domain: interaction history and user-owned boundaries.

A turn is evidence that an interaction occurred, not evidence of attachment.
Rapport changes only for narrow explicit feedback; persistent communication
preferences require an authenticated command or an exact user instruction.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID,uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .affect_contracts import (
    RelationshipPreferenceResult,SetRelationshipPreference,
)
from .affect_events import write_domain_event
from .communication_preference_rules import is_explicit_sentence_limit
from .contracts import TrustedActor
from .conversation import _require_actor
from .operation_contracts import require_idle
from .runtime import (
    IdempotencyConflict,PermissionDenied,StaleSelection,VersionConflict,
    _fingerprint,
)

from .affect_lineage import current_sources,projection_current,source_current
from .memory_read_policy import capture_historical_read_fence,require_historical_read_fence

RELATIONSHIP_CONSUMER="relationship.user_interaction.v1"
_COMMAND="relationship.preference.set.v1"

_FEEDBACK={
    "thanks":"POSITIVE",
    "thank you":"POSITIVE",
    "that helped":"POSITIVE",
    "that was helpful":"POSITIVE",
    "that was not helpful":"NEGATIVE",
}
# Only unequivocal, exact user utterances may auto-persist preferences.
# Other user instructions remain authoritative conversation content until
# an authenticated Relationship command is issued by the orchestrator.
_AUTO_PREFERENCES={
    "please keep replies short":("RESPONSE_LENGTH","","SHORT"),
    "please keep responses short":("RESPONSE_LENGTH","","SHORT"),
    "please give detailed replies":("RESPONSE_LENGTH","","DETAILED"),
    "please use normal length replies":("RESPONSE_LENGTH","","NORMAL"),
    "please don't use emojis":("EMOJI_USE","","OFF"),
    "please do not use emojis":("EMOJI_USE","","OFF"),
    "please use emojis":("EMOJI_USE","","ON"),
    "please don't send proactive messages":("PROACTIVE_MESSAGES","","DISALLOW"),
    "please do not send proactive messages":("PROACTIVE_MESSAGES","","DISALLOW"),
    "please send proactive messages":("PROACTIVE_MESSAGES","","ALLOW"),
}


@dataclass(frozen=True)
class PreferenceView:
    preference_key:str
    scope_key:str
    preference_value:str
    status:str
    version:int
    source_turn_id:UUID
    source_quote:str


@dataclass(frozen=True)
class RelationshipSnapshot:
    user_id:UUID
    instance_id:UUID
    version:int
    interaction_count:int
    familiarity:float
    rapport:float
    last_interaction_at:datetime|None
    preferences:tuple[PreferenceView,...]


def _exact(text:str)->str:
    return text.strip().replace("’","'").casefold().rstrip("!.").strip()


def _ensure_state(cur,user_id:UUID,instance_id:UUID):
    cur.execute(
        """INSERT INTO relationship_state(user_id,instance_id)
           VALUES (%s,%s) ON CONFLICT(user_id,instance_id) DO NOTHING""",
        (user_id,instance_id),
    )
    cur.execute(
        """SELECT * FROM relationship_state WHERE user_id=%s
           AND instance_id=%s FOR UPDATE""",
        (user_id,instance_id),
    )
    state=cur.fetchone()
    if state is None:
        raise PermissionDenied("relationship state outside persona scope")
    return state


def _preference_update(
    cur,*,user_id:UUID,instance_id:UUID,conversation_id:UUID,turn_id:UUID,
    at:datetime,quote:str,source_id:str,key:str,scope:str,value:str,
    action:str,causation_id:UUID|None=None,
    correlation_id:UUID|None=None,expected_version:int|None=None,
)->RelationshipPreferenceResult|None:
    """Relationship-owned revision while caller holds Relationship state lock.

    Auto updates must never overwrite a newer explicit correction. Explicit
    commands enforce a version compare-and-swap, not last-writer-wins.
    """
    cur.execute(
        """SELECT version,source_turn_at FROM relationship_preferences
           WHERE user_id=%s AND instance_id=%s AND preference_key=%s
             AND scope_key=%s FOR UPDATE""",
        (user_id,instance_id,key,scope),
    )
    previous=cur.fetchone()
    old_version=previous["version"] if previous else 0
    if expected_version is not None and old_version!=expected_version:
        raise VersionConflict("relationship preference version changed")
    if previous is not None:
        if expected_version is None and at<=previous["source_turn_at"]:
            # At-least-once, delayed automatic ingestion has lower authority
            # than any already committed preference revision. An auto
            # candidate from the same source turn must not restore an
            # explicit removal/correction that happened before delivery.
            return None
        if expected_version is not None and at<previous["source_turn_at"]:
            raise VersionConflict("older evidence cannot supersede a newer user preference")
    if action=="REMOVE" and previous is None:
        raise VersionConflict("cannot remove a preference without an existing version")
    version=old_version+1
    status="ACTIVE" if action=="SET" else "REMOVED"
    cur.execute(
        """INSERT INTO relationship_preferences(
             user_id,instance_id,preference_key,scope_key,preference_value,
             status,version,source_conversation_id,source_turn_id,
             source_turn_at,source_quote,source_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (user_id,instance_id,preference_key,scope_key)
           DO UPDATE SET preference_value=EXCLUDED.preference_value,
             status=EXCLUDED.status,version=EXCLUDED.version,
             source_conversation_id=EXCLUDED.source_conversation_id,
             source_turn_id=EXCLUDED.source_turn_id,
             source_turn_at=EXCLUDED.source_turn_at,
             source_quote=EXCLUDED.source_quote,source_id=EXCLUDED.source_id,
             updated_at=clock_timestamp()""",
        (user_id,instance_id,key,scope,value,status,version,conversation_id,
         turn_id,at,quote,source_id),
    )
    event_id=write_domain_event(
        cur,user_id=user_id,instance_id=instance_id,domain="relationship",
        event_type="relationship.preference.changed.v1",
        source_id=source_id,
        trust_level="AUTHENTICATED_USER" if correlation_id else "TRUSTED_PLATFORM",
        causation_id=causation_id,correlation_id=correlation_id,
        payload={"key":key,"scope":scope,"version":version,"status":status,
                 "source_turn_id":str(turn_id)},
    )
    cur.execute(
        """INSERT INTO relationship_preference_revisions(
             user_id,instance_id,preference_key,scope_key,version,
             preference_value,status,source_conversation_id,source_turn_id,
             source_quote,event_id
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (user_id,instance_id,key,scope,version,value,status,
         conversation_id,turn_id,quote,event_id),
    )
    return RelationshipPreferenceResult(
        instance_id=instance_id,preference_key=key,scope_key=scope,
        preference_value=value,status=status,version=version,event_id=event_id,
    )


def effective_rapport(cur,user_id,instance_id,state,fence):
    """Replay originally applicable feedback, retaining factual interaction count.

    Event stream order reproduces the owner's original late-delivery rule.
    Revoked feedback does not resurrect older, originally unapplied feedback.
    """
    if state is None:
        return 0.0
    if projection_current(state,fence):
        return float(state["rapport"])
    cur.execute(
        """SELECT o.conversation_id,o.source_turn_id,o.source_turn_at,o.feedback
           FROM relationship_observations o JOIN events e
             ON e.event_id=o.event_id AND e.user_id=o.user_id AND e.instance_id=o.instance_id
           WHERE o.user_id=%s AND o.instance_id=%s AND o.feedback<>'NONE'
           ORDER BY e.stream_sequence""",
        (user_id,instance_id),
    )
    rows=cur.fetchall()
    eligible=current_sources(cur,user_id,instance_id,rows)
    rapport=0.0
    watermark=None
    for row in rows:
        at=row["source_turn_at"]
        if watermark is not None and at<=watermark:
            continue
        watermark=at
        if row["source_turn_id"] in eligible:
            rapport=round(max(-1,min(1,rapport+(
                .06 if row["feedback"]=="POSITIVE" else -.08))),3)
    return rapport


def observe_relationship_input(cur,event,source:dict) -> bool:
    """At-least-once authenticated interaction, no inference beyond feedback."""
    state=_ensure_state(cur,event.user_id,event.instance_id)
    cur.execute(
        """SELECT 1 FROM relationship_observations WHERE source_event_id=%s
           AND user_id=%s AND instance_id=%s""",
        (event.event_id,event.user_id,event.instance_id),
    )
    if cur.fetchone() is not None:
        return False
    # The evidence tables cap verbatim quotes at 200 characters. Do not
    # allow punctuation normalization to turn an oversized input into a
    # match whose full evidence would violate a CHECK constraint. The turn
    # still counts as an interaction with neutral feedback.
    fence=capture_historical_read_fence(cur,event.user_id,event.instance_id)
    eligible=source_current(cur,event.user_id,event.instance_id,
                            source["conversation_id"],source["turn_id"])
    matchable=eligible and len(source["input_text"])<=200
    normalized=_exact(source["input_text"]) if matchable else ""
    feedback=_FEEDBACK.get(normalized,"NONE")
    count=state["interaction_count"]+1
    previous=state["last_feedback_at"]
    apply_feedback=(feedback!="NONE"
                    and (previous is None or source["created_at"]>previous))
    rapport=max(-1,min(1,
        effective_rapport(cur,event.user_id,event.instance_id,state,fence)+(
            (0.06 if feedback=="POSITIVE" else -0.08)
            if apply_feedback else 0
        ),
    ))
    familiarity=min(1,count/40)
    cur.execute(
        """UPDATE relationship_state
           SET version=version+1,interaction_count=%s,
               familiarity=%s,rapport=%s,
               last_interaction_at=GREATEST(
                   coalesce(last_interaction_at,%s),%s
               ),
               last_feedback_at=CASE WHEN %s THEN %s ELSE last_feedback_at END,
               evidence_revision=%s,evidence_next_expiry=%s,
               updated_at=clock_timestamp()
           WHERE user_id=%s AND instance_id=%s""",
        (count,familiarity,rapport,source["created_at"],source["created_at"],
         apply_feedback,source["created_at"],fence.revision,fence.next_expiry,
         event.user_id,event.instance_id),
    )
    event_id=write_domain_event(
        cur,user_id=event.user_id,instance_id=event.instance_id,
        domain="relationship",event_type="relationship.interaction.recorded.v1",
        source_id=RELATIONSHIP_CONSUMER,trust_level="TRUSTED_PLATFORM",
        payload={"source_turn_id":str(source["turn_id"]),"feedback":feedback,
                 "interaction_count":count},
        causation_id=event.event_id,
    )
    cur.execute(
        """INSERT INTO relationship_observations(
             observation_id,user_id,instance_id,conversation_id,
             source_turn_id,source_event_id,event_id,source_turn_at,feedback,
             source_quote
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (uuid4(),event.user_id,event.instance_id,source["conversation_id"],
         source["turn_id"],event.event_id,event_id,source["created_at"],
         feedback,source["input_text"] if feedback!="NONE" else None),
    )
    matched=_AUTO_PREFERENCES.get(normalized)
    if matched is None and matchable and is_explicit_sentence_limit(source["input_text"]):
        matched=("RESPONSE_LENGTH","","SHORT")
    if matched is not None:
        key,scope,value=matched
        _preference_update(
            cur,user_id=event.user_id,instance_id=event.instance_id,
            conversation_id=source["conversation_id"],
            turn_id=source["turn_id"],at=source["created_at"],
            quote=source["input_text"],source_id=RELATIONSHIP_CONSUMER,
            key=key,scope=scope,value=value,action="SET",
            causation_id=event.event_id,
        )
    require_historical_read_fence(cur,event.user_id,event.instance_id,fence)
    return True


def set_relationship_preference(
    connection:Connection,actor:TrustedActor,command:SetRelationshipPreference,
)->RelationshipPreferenceResult:
    """User-owned, version-checked, evidence-linked preference or boundary."""
    require_idle(connection)
    _require_actor(actor)
    fingerprint=_fingerprint(command)
    receipt_id=uuid4()
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT 1 FROM persona_instances
                   WHERE user_id=%s AND id=%s""",
                (actor.user_id,command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("relationship preference outside user scope")
            cur.execute(
                """INSERT INTO command_receipts(
                     receipt_id,user_id,instance_id,command_type,idempotency_key,
                     request_fingerprint
                   ) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                   DO NOTHING RETURNING receipt_id""",
                (receipt_id,actor.user_id,command.instance_id,_COMMAND,
                 command.idempotency_key,fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s AND command_type=%s
                         AND idempotency_key=%s""",
                    (actor.user_id,command.instance_id,_COMMAND,
                     command.idempotency_key),
                )
                prior=cur.fetchone()
                if (prior is None or prior["request_fingerprint"]!=fingerprint
                    or prior["result_json"] is None):
                    raise IdempotencyConflict("relationship command key changed")
                return RelationshipPreferenceResult.model_validate(
                    prior["result_json"],
                ).model_copy(update={"replayed":True})
            cur.execute(
                """SELECT instance_id,generation,mode FROM persona_selection
                   WHERE user_id=%s FOR SHARE""",
                (actor.user_id,),
            )
            selected=cur.fetchone()
            if (selected is None or selected["instance_id"]!=command.instance_id
                or selected["generation"]!=command.expected_generation
                or selected["mode"]!="FOREGROUND"):
                raise StaleSelection("relationship command requires selected foreground persona")
            cur.execute(
                """SELECT input_text,created_at,source_id
                   FROM conversation_turns WHERE conversation_id=%s
                     AND turn_id=%s AND user_id=%s AND instance_id=%s""",
                (command.conversation_id,command.evidence_turn_id,
                 actor.user_id,command.instance_id),
            )
            evidence=cur.fetchone()
            if (evidence is None or command.evidence_quote.casefold()
                not in evidence["input_text"].casefold()):
                raise PermissionDenied("relationship preference must cite actual user text")
            _ensure_state(cur,actor.user_id,command.instance_id)
            fence=capture_historical_read_fence(cur,actor.user_id,command.instance_id)
            if not source_current(cur,actor.user_id,command.instance_id,
                                  command.conversation_id,command.evidence_turn_id):
                raise PermissionDenied("relationship preference source no longer eligible")
            result=_preference_update(
                cur,user_id=actor.user_id,instance_id=command.instance_id,
                conversation_id=command.conversation_id,
                turn_id=command.evidence_turn_id,at=evidence["created_at"],
                quote=command.evidence_quote,source_id=actor.source_id,
                key=command.preference_key,scope=command.scope_key,
                value=command.preference_value,action=command.action,
                correlation_id=receipt_id,
                expected_version=command.expected_version,
            )
            if result is None:
                raise VersionConflict("explicit preference unexpectedly discarded")
            cur.execute(
                """UPDATE command_receipts
                   SET event_id=%s,result_json=%s::jsonb WHERE receipt_id=%s""",
                (result.event_id,json.dumps(result.model_dump(mode="json")),receipt_id),
            )
            require_historical_read_fence(cur,actor.user_id,command.instance_id,fence)
            return result


def read_relationship_state(
    connection:Connection,actor:TrustedActor,instance_id:UUID,
)->RelationshipSnapshot:
    """Authenticated scoped state; no hidden inferred attachment."""
    require_idle(connection)
    _require_actor(actor)
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
            (actor.user_id,instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("relationship state outside actor scope")
        fence=capture_historical_read_fence(cur,actor.user_id,instance_id)
        cur.execute(
            """SELECT *
               FROM relationship_state
               WHERE user_id=%s AND instance_id=%s""",
            (actor.user_id,instance_id),
        )
        state=cur.fetchone()
        rapport=effective_rapport(cur,actor.user_id,instance_id,state,fence)
        cur.execute(
            """SELECT preference_key,scope_key,preference_value,status,version,
                      source_turn_id,source_quote
               FROM current_relationship_preferences
               WHERE user_id=%s AND instance_id=%s
               ORDER BY preference_key,scope_key""",
            (actor.user_id,instance_id),
        )
        preferences=tuple(PreferenceView(**dict(row)) for row in cur.fetchall())
    return RelationshipSnapshot(
        user_id=actor.user_id,instance_id=instance_id,
        version=state["version"] if state else 0,
        interaction_count=state["interaction_count"] if state else 0,
        familiarity=float(state["familiarity"]) if state else 0,
        rapport=rapport,
        last_interaction_at=state["last_interaction_at"] if state else None,
        preferences=preferences,
    )


def has_saved_relationship_preference_history(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    preference_key: str, *, scope_key: str = "",
) -> bool:
    """Whether Relationship ever recorded an ACTIVE revision for this key.

    An explicit REMOVE keeps immutable revision evidence but makes the
    preference inactive. A historical "ever" question must not be answered
    from the active projection alone. This read discloses no retired value or
    user quote; absence means no *saved record*, not proof no utterance occurred.
    """
    require_idle(connection)
    _require_actor(actor)
    if preference_key not in (
        "RESPONSE_LENGTH", "EMOJI_USE", "PROACTIVE_MESSAGES",
        "AVOID_PROACTIVE_TOPIC",
    ):
        raise ValueError("unsupported relationship preference key")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
            (actor.user_id, instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("relationship state outside actor scope")
        cur.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM relationship_preference_revisions p
                   WHERE user_id=%s AND instance_id=%s
                     AND preference_key=%s AND scope_key=%s
                     AND status='ACTIVE'
                     AND dialogue_turn_current(p.user_id,p.instance_id,
                                               p.source_conversation_id,p.source_turn_id)
               ) AS ever_saved""",
            (actor.user_id, instance_id, preference_key, scope_key),
        )
        return bool(cur.fetchone()["ever_saved"])
