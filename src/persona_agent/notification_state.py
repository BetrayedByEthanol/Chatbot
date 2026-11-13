"""P4-06: deterministic global in-app notification service, no persona cognition.

A real transport is NOT assumed: AVAILABLE means committed to the supported
authenticated in-app inbox, not pushed/read. No platform callback changes
persona selection, goal state, or a Procedure.
"""
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .operation_contracts import require_idle
from .runtime import (
    PermissionDenied,StaleSelection,VersionConflict,IdempotencyConflict,_fingerprint,
)
from .notification_contracts import (
    NotificationPolicyCommand,NotificationPolicyResult,
    ScheduleNotificationCommand,NotificationResult,
    NotificationActionCommand,NotificationActionResult,InboxNotification,
)


def _actor(actor):
    if type(actor) is not TrustedActor:
        raise PermissionDenied("notification action requires authenticated primary user")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError,ValueError) as exc:
        raise ValueError("unknown IANA timezone") from exc


def _owned_instance(cur,user_id,instance_id):
    cur.execute(
        "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
        (user_id,instance_id),
    )
    if cur.fetchone() is None:
        raise PermissionDenied("notification persona outside authenticated user scope")


def _selection(cur,user_id,instance_id,generation):
    cur.execute(
        """SELECT instance_id,generation,mode FROM persona_selection
           WHERE user_id=%s FOR SHARE""",(user_id,),
    )
    sel=cur.fetchone()
    if (sel is None or sel["instance_id"]!=instance_id
        or sel["generation"]!=generation or sel["mode"]!="FOREGROUND"):
        raise StaleSelection("notification creation requires selected Foreground persona")


def _receipt(cur,actor,instance_id,command,kind):
    """Existing P0 receipt contract: exact payload replay, no duplicate event."""
    receipt_id=uuid4()
    fingerprint=_fingerprint(command)
    cur.execute(
        """INSERT INTO command_receipts(
             receipt_id,user_id,instance_id,command_type,
             idempotency_key,request_fingerprint
           ) VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
           DO NOTHING RETURNING receipt_id""",
        (receipt_id,actor.user_id,instance_id,kind,
         command.idempotency_key,fingerprint),
    )
    if cur.fetchone() is not None:
        return receipt_id,None
    cur.execute(
        """SELECT request_fingerprint,result_json FROM command_receipts
           WHERE user_id=%s AND instance_id=%s AND command_type=%s
             AND idempotency_key=%s""",
        (actor.user_id,instance_id,kind,command.idempotency_key),
    )
    previous=cur.fetchone()
    if previous is None or previous["request_fingerprint"]!=fingerprint:
        raise IdempotencyConflict("notification idempotency key reused with different input")
    if previous["result_json"] is None:
        raise VersionConflict("notification receipt incomplete")
    return None,previous["result_json"]


def _remember(cur,receipt_id,event_id,result):
    cur.execute(
        """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
           WHERE receipt_id=%s""",
        (event_id,result.model_dump_json(),receipt_id),
    )


def _event(cur, user_id,instance_id,event_type,payload,*,actor_source,
           trust_level,receipt_id=None) -> UUID:
    stream=uuid5(NAMESPACE_URL,f"persona-agent/notification/{user_id}/{instance_id}")
    cur.execute(
        """INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
           VALUES(%s,%s,%s,'notification') ON CONFLICT(stream_id) DO NOTHING""",
        (stream,user_id,instance_id),
    )
    cur.execute(
        """UPDATE event_streams SET last_sequence=last_sequence+1
           WHERE stream_id=%s AND user_id=%s AND instance_id=%s
           RETURNING last_sequence""",(stream,user_id,instance_id),
    )
    seq=cur.fetchone()
    if seq is None:
        raise PermissionDenied("notification event stream outside user scope")
    event_id=uuid4()
    cur.execute(
        """INSERT INTO events(
             event_id,event_type,schema_version,user_id,instance_id,
             stream_id,stream_sequence,source_id,trust_level,correlation_id,payload
           ) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (event_id,event_type,user_id,instance_id,stream,seq["last_sequence"],
         actor_source,trust_level,receipt_id,json.dumps(payload)),
    )
    cur.execute("INSERT INTO outbox(outbox_id,event_id) VALUES(%s,%s)",
                (uuid4(),event_id))
    return event_id


def update_notification_policy(
    connection: Connection, actor: TrustedActor, command: NotificationPolicyCommand,
) -> NotificationPolicyResult:
    """Global user opt-in, timezone and quiet hours (optimistic revision)."""
    require_idle(connection)
    _actor(actor)
    _zone(command.timezone)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _owned_instance(cur,actor.user_id,command.instance_id)
            rid,previous=_receipt(cur,actor,command.instance_id,command,
                                  "notification.policy.update.v1")
            if previous is not None:
                return NotificationPolicyResult.model_validate(previous).model_copy(
                    update={"replayed":True},
                )
            _selection(cur,actor.user_id,command.instance_id,
                       command.expected_generation)
            cur.execute(
                "SELECT version FROM notification_policies WHERE user_id=%s FOR UPDATE",
                (actor.user_id,),
            )
            prior=cur.fetchone()
            current=prior["version"] if prior else 0
            if current!=command.expected_version:
                raise VersionConflict("notification policy revision changed")
            version=current+1
            cur.execute(
                """INSERT INTO notification_policies(
                     user_id,enabled,timezone,quiet_start_min,quiet_end_min,version)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id) DO UPDATE
                   SET enabled=EXCLUDED.enabled,timezone=EXCLUDED.timezone,
                       quiet_start_min=EXCLUDED.quiet_start_min,
                       quiet_end_min=EXCLUDED.quiet_end_min,version=EXCLUDED.version,
                       updated_at=clock_timestamp()
                   WHERE notification_policies.version=%s
                   RETURNING version""",
                (actor.user_id,command.enabled,command.timezone,
                 command.quiet_start_min,command.quiet_end_min,version,current),
            )
            if cur.fetchone() is None:
                # Concurrent first-time policy creation must not silently
                # replace another user's newer preference revision.
                raise VersionConflict("notification policy revision changed concurrently")
            event=_event(cur,actor.user_id,command.instance_id,
                         "notification.policy.changed.v1",{
                "enabled":command.enabled,"version":version,
                "timezone":command.timezone,
            },actor_source=actor.source_id,trust_level="AUTHENTICATED_USER",
                         receipt_id=rid)
            result=NotificationPolicyResult(version=version,event_id=event)
            _remember(cur,rid,event,result)
            return result


def schedule_notification(
    connection: Connection,actor: TrustedActor,command: ScheduleNotificationCommand,
) -> NotificationResult:
    """User-authorized reminder or follow-up, no Goal/Cognition side effect."""
    require_idle(connection)
    _actor(actor)
    zone=_zone(command.timezone)
    local=command.due_at.astimezone(zone)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _owned_instance(cur,actor.user_id,command.instance_id)
            rid,previous=_receipt(cur,actor,command.instance_id,command,
                                  "notification.schedule.v1")
            if previous is not None:
                return NotificationResult.model_validate(previous).model_copy(
                    update={"replayed":True},
                )
            _selection(cur,actor.user_id,command.instance_id,
                       command.expected_generation)
            cur.execute(
                "SELECT enabled FROM notification_policies WHERE user_id=%s FOR SHARE",
                (actor.user_id,),
            )
            policy=cur.fetchone()
            if policy is None or not policy["enabled"]:
                raise PermissionDenied("in-app notification channel requires user opt-in")
            if command.source_conversation_id is not None:
                cur.execute(
                    """SELECT 1 FROM conversation_turns
                       WHERE conversation_id=%s AND turn_id=%s
                         AND user_id=%s AND instance_id=%s""",
                    (command.source_conversation_id,command.source_turn_id,
                     actor.user_id,command.instance_id),
                )
                if cur.fetchone() is None:
                    raise PermissionDenied("notification source turn not owned by persona")
            cur.execute(
                """SELECT 1 FROM notification_intents WHERE intent_id=%s""",
                (command.intent_id,),
            )
            if cur.fetchone() is not None:
                raise VersionConflict("notification identifier already allocated")
            event=_event(cur,actor.user_id,command.instance_id,
                         "notification.scheduled.v1",{
                "intent_id":str(command.intent_id),"kind":command.kind,
                "due_at":command.due_at.isoformat(),
                "recurrence":command.recurrence,
                "source_turn_id":(str(command.source_turn_id)
                                  if command.source_turn_id else None),
            },actor_source=actor.source_id,trust_level="AUTHENTICATED_USER",
                         receipt_id=rid)
            cur.execute(
                """INSERT INTO notification_intents(
                     intent_id,user_id,instance_id,kind,body,timezone,
                     local_hour,local_minute,recurrence,next_due_at,
                     selection_generation,source_conversation_id,source_turn_id,
                     idempotency_key,request_fingerprint,created_event_id)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (command.intent_id,actor.user_id,command.instance_id,
                 command.kind,command.body.strip(),command.timezone,
                 local.hour,local.minute,command.recurrence,
                 command.due_at.astimezone(timezone.utc),command.expected_generation,
                 command.source_conversation_id,command.source_turn_id,
                 command.idempotency_key,_fingerprint(command),event),
            )
            result=NotificationResult(intent_id=command.intent_id,event_id=event)
            _remember(cur,rid,event,result)
            return result


def _is_quiet(policy,utc_now: datetime) -> bool:
    if policy["quiet_start_min"] is None:
        return False
    local=utc_now.astimezone(_zone(policy["timezone"]))
    minute=local.hour*60+local.minute
    start,end=policy["quiet_start_min"],policy["quiet_end_min"]
    span=(end-start)%1440
    return span>0 and (minute-start)%1440<span


def _next_daily(now: datetime,zone_name: str,hour: int,minute: int) -> datetime:
    """Next local wall-time; roll DST gaps forward, use first fold in autumn."""
    zone=_zone(zone_name)
    day=now.astimezone(zone).date()+timedelta(days=1)
    wall=datetime(day.year,day.month,day.day,hour,minute,tzinfo=zone,fold=0)
    utc=wall.astimezone(timezone.utc)
    # ZoneInfo's nonexistent local time round-trips to its next valid time.
    roundtrip=utc.astimezone(zone)
    if roundtrip.date()!=day or (roundtrip.hour,roundtrip.minute)<(hour,minute):
        # Some timezones shift backwards across nonexistent wall time.
        # Prefer first later valid local instant; never loop indefinitely.
        utc=(wall+timedelta(hours=2)).astimezone(timezone.utc)
    return utc


# The in-app inbox is the only supported channel. No network or provider call
# occurs; this transaction makes an occurrence AVAILABLE atomically with its
# provenance event and next recurrence time.
def dispatch_due_notifications(connection: Connection, *, limit: int=32) -> int:
    """Globally ordered in-app due scan across new and snoozed occurrences.

    One bounded SQL discovery joins both work types and sorts by due time.
    An endless stream of new due intents cannot starve an older snooze.
    """
    require_idle(connection)
    if not 1<=limit<=128:
        raise ValueError("notification batch limit must be 1..128")
    committed=0
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT kind,item_id FROM (
                  SELECT 'INTENT' AS kind,i.intent_id AS item_id,
                         i.next_due_at AS due_at
                  FROM notification_intents i
                  JOIN notification_policies p ON p.user_id=i.user_id
                  WHERE i.active AND p.enabled AND i.next_due_at<=clock_timestamp()
                  AND (
                      p.quiet_start_min IS NULL OR
                      p.quiet_start_min=p.quiet_end_min OR
                      MOD(
                        EXTRACT(HOUR FROM timezone(p.timezone,clock_timestamp()))::int*60
                        + EXTRACT(MINUTE FROM timezone(p.timezone,clock_timestamp()))::int
                        - p.quiet_start_min + 1440, 1440
                      ) >= MOD((p.quiet_end_min-p.quiet_start_min+1440),1440)
                    )

                  UNION ALL
                  SELECT 'SNOOZE' AS kind,o.occurrence_id AS item_id,
                         o.snoozed_until AS due_at
                  FROM notification_occurrences o
                  JOIN notification_policies p ON p.user_id=o.user_id
                  WHERE o.status='SNOOZED' AND p.enabled
                    AND o.snoozed_until<=clock_timestamp()
                  AND (
                      p.quiet_start_min IS NULL OR
                      p.quiet_start_min=p.quiet_end_min OR
                      MOD(
                        EXTRACT(HOUR FROM timezone(p.timezone,clock_timestamp()))::int*60
                        + EXTRACT(MINUTE FROM timezone(p.timezone,clock_timestamp()))::int
                        - p.quiet_start_min + 1440, 1440
                      ) >= MOD((p.quiet_end_min-p.quiet_start_min+1440),1440)
                    )

               ) due
               ORDER BY due_at,item_id LIMIT %s""",(limit*4,),
        )
        candidates=[(row["kind"],row["item_id"]) for row in cur.fetchall()]
    for kind,item_id in candidates:
        if committed>=limit:
            break
        if kind=="INTENT":
            ident=item_id
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """SELECT * FROM notification_intents
                           WHERE intent_id=%s AND active AND next_due_at<=clock_timestamp()
                           FOR UPDATE SKIP LOCKED""",(ident,),
                    )
                    intent=cur.fetchone()
                    if intent is None:
                        continue
                    cur.execute(
                        """SELECT * FROM notification_policies
                           WHERE user_id=%s FOR SHARE""",(intent["user_id"],),
                    )
                    policy=cur.fetchone()
                    if policy is None or not policy["enabled"]:
                        continue
                    cur.execute("SELECT clock_timestamp() AS now")
                    now=cur.fetchone()["now"]
                    if _is_quiet(policy,now):
                        continue
                    overdue=now-intent["next_due_at"]>timedelta(hours=24)
                    if not overdue:
                        occurrence=uuid4()
                        cur.execute(
                            """INSERT INTO notification_occurrences(
                                 occurrence_id,intent_id,user_id,instance_id,scheduled_for)
                               VALUES(%s,%s,%s,%s,%s)""",
                            (occurrence,ident,intent["user_id"],
                             intent["instance_id"],intent["next_due_at"]),
                        )
                        _event(cur,intent["user_id"],intent["instance_id"],
                               "notification.available.v1",{
                            "intent_id":str(ident),"occurrence_id":str(occurrence),
                            "scheduled_for":intent["next_due_at"].isoformat(),
                            "channel":"IN_APP",
                        },actor_source="notification.platform.in_app",
                               trust_level="TRUSTED_PLATFORM")
                        committed+=1
                    else:
                        _event(cur,intent["user_id"],intent["instance_id"],
                               "notification.missed.v1",{
                            "intent_id":str(ident),
                            "scheduled_for":intent["next_due_at"].isoformat(),
                        },actor_source="notification.platform.in_app",
                               trust_level="TRUSTED_PLATFORM")
                        committed+=1
                    if intent["recurrence"]=="DAILY":
                        next_due=_next_daily(now,intent["timezone"],
                                             intent["local_hour"],intent["local_minute"])
                        cur.execute(
                            """UPDATE notification_intents SET next_due_at=%s,
                                 updated_at=clock_timestamp()
                               WHERE intent_id=%s""",(next_due,ident),
                        )
                    else:
                        cur.execute(
                            """UPDATE notification_intents SET active=false,
                                 updated_at=clock_timestamp()
                               WHERE intent_id=%s""",(ident,),
                        )
        else:
            oid=item_id
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """SELECT * FROM notification_occurrences
                           WHERE occurrence_id=%s AND status='SNOOZED'
                             AND snoozed_until<=clock_timestamp() FOR UPDATE SKIP LOCKED""",
                        (oid,),
                    )
                    row=cur.fetchone()
                    if row is None:
                        continue
                    cur.execute("SELECT * FROM notification_policies WHERE user_id=%s FOR SHARE",
                                (row["user_id"],))
                    policy=cur.fetchone()
                    if policy is None or not policy["enabled"]:
                        continue
                    cur.execute("SELECT clock_timestamp() AS now")
                    if _is_quiet(policy,cur.fetchone()["now"]):
                        continue
                    cur.execute(
                        """UPDATE notification_occurrences SET status='AVAILABLE',
                             snoozed_until=NULL,available_at=clock_timestamp()
                           WHERE occurrence_id=%s""",(oid,),
                    )
                    _event(cur,row["user_id"],row["instance_id"],
                           "notification.snooze_elapsed.v1",{
                           "occurrence_id":str(oid)},
                           actor_source="notification.platform.in_app",
                           trust_level="TRUSTED_PLATFORM")
                    committed+=1
    return committed


def list_notification_inbox(
    connection: Connection,actor: TrustedActor, *,limit: int=32,
) -> tuple[InboxNotification,...]:
    """All originating personas including INACTIVE; no selection mutation."""
    require_idle(connection)
    _actor(actor)
    if not 1<=limit<=128:
        raise ValueError("invalid inbox limit")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT o.occurrence_id,o.intent_id,o.instance_id,o.scheduled_for,
                      o.available_at,i.kind,i.body
               FROM notification_occurrences o
               JOIN notification_intents i ON i.intent_id=o.intent_id
               WHERE o.user_id=%s AND o.status='AVAILABLE'
               ORDER BY o.available_at DESC,o.occurrence_id LIMIT %s""",
            (actor.user_id,limit),
        )
        return tuple(InboxNotification.model_validate(r) for r in cur.fetchall())


def respond_to_notification(
    connection: Connection,actor: TrustedActor,command: NotificationActionCommand,
) -> NotificationActionResult:
    """Exactly scoped ACK/STOP/SNOOZE/REPLY; free text is queued, never reasoned."""
    require_idle(connection)
    _actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            # Consistent lock order for all responses (including STOP):
            # intent first, occurrence second. STOP may cancel other
            # occurrences; locking an occurrence first permits a 2-STOP
            # deadlock when each caller holds a different occurrence lock.
            cur.execute(
                """SELECT i.intent_id FROM notification_intents i
                   JOIN notification_occurrences o ON o.intent_id=i.intent_id
                   WHERE o.occurrence_id=%s AND o.user_id=%s
                     AND i.user_id=%s
                   FOR UPDATE OF i""",
                (command.occurrence_id,actor.user_id,actor.user_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("notification response outside authenticated user")
            cur.execute(
                """SELECT o.*,i.active FROM notification_occurrences o
                   JOIN notification_intents i ON i.intent_id=o.intent_id
                   WHERE o.occurrence_id=%s AND o.user_id=%s FOR UPDATE OF o""",
                (command.occurrence_id,actor.user_id),
            )
            row=cur.fetchone()
            if row is None:
                raise PermissionDenied("notification response outside authenticated user")
            rid,previous=_receipt(cur,actor,row["instance_id"],command,
                                  "notification.respond.v1")
            if previous is not None:
                return NotificationActionResult.model_validate(previous).model_copy(
                    update={"replayed":True},
                )
            if row["status"]!="AVAILABLE":
                raise VersionConflict("notification already acknowledged or snoozed")
            if command.action=="SNOOZE":
                cur.execute(
                    """UPDATE notification_occurrences SET status='SNOOZED',
                       snoozed_until=clock_timestamp()+(%s*interval '1 minute')
                       WHERE occurrence_id=%s""",
                    (command.snooze_minutes,command.occurrence_id),
                )
            else:
                cur.execute(
                    """UPDATE notification_occurrences SET status='ACKNOWLEDGED',
                       acknowledged_at=clock_timestamp()
                       WHERE occurrence_id=%s""",(command.occurrence_id,),
                )
            if command.action=="STOP":
                cur.execute(
                    """UPDATE notification_intents SET active=false,
                       updated_at=clock_timestamp() WHERE intent_id=%s""",
                    (row["intent_id"],),
                )
                # STOP terminates all other pending occurrences too,
                # including a prior snooze of the same recurring intent.
                # Otherwise the platform could notify the user *after* STOP.
                cur.execute(
                    """UPDATE notification_occurrences
                       SET status='CANCELLED',snoozed_until=NULL
                       WHERE intent_id=%s AND occurrence_id<>%s
                         AND status IN ('AVAILABLE','SNOOZED')""",
                    (row["intent_id"],command.occurrence_id),
                )
            event=_event(cur,actor.user_id,row["instance_id"],
                         "notification.response.v1",{
                "intent_id":str(row["intent_id"]),
                "occurrence_id":str(command.occurrence_id),
                "action":command.action,
            },actor_source=actor.source_id,trust_level="AUTHENTICATED_USER",
                         receipt_id=rid)
            if command.action=="REPLY":
                cur.execute(
                    """INSERT INTO notification_inbound_replies(
                         reply_id,occurrence_id,user_id,instance_id,reply_text,event_id)
                       VALUES(%s,%s,%s,%s,%s,%s)""",
                    (uuid4(),command.occurrence_id,actor.user_id,
                     row["instance_id"],command.reply_text.strip(),event),
                )
            result=NotificationActionResult(occurrence_id=command.occurrence_id,
                                            event_id=event)
            _remember(cur,rid,event,result)
            return result


def list_pending_notification_replies(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    *, limit: int=32,
) -> tuple[dict,...]:
    """Reactivation read: no automatic interpretation/ack of queued replies."""
    require_idle(connection)
    _actor(actor)
    if not 1<=limit<=128:
        raise ValueError("invalid reply list limit")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT s.instance_id,s.mode FROM persona_selection s
               WHERE s.user_id=%s FOR SHARE""",(actor.user_id,),
        )
        selected=cur.fetchone()
        if selected is None or selected["instance_id"]!=instance_id or selected["mode"]!="FOREGROUND":
            raise StaleSelection("persona must be reactivated to read inbound replies")
        cur.execute(
            """SELECT reply_id,occurrence_id,reply_text,event_id,created_at
               FROM notification_inbound_replies
               WHERE user_id=%s AND instance_id=%s AND state='PENDING_PERSONA'
               ORDER BY created_at,reply_id LIMIT %s""",
            (actor.user_id,instance_id,limit),
        )
        return tuple(dict(r) for r in cur.fetchall())
