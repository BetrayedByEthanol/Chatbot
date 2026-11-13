"""P4-06 real PostgreSQL communication tests. No external push provider."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.notification_contracts import (
    NotificationPolicyCommand,ScheduleNotificationCommand,NotificationActionCommand,
)
from persona_agent.notification_state import (
    update_notification_policy,schedule_notification,dispatch_due_notifications,
    list_notification_inbox,respond_to_notification,
    list_pending_notification_replies,_next_daily,
)
from persona_agent.runtime import (
    PermissionDenied,StaleSelection,VersionConflict,IdempotencyConflict,
)
from persona_agent.lifecycle import switch_persona
from test_phase0_lifecycle import seed,user_actor,invoke,switch_cmd


def call(dsn,fn,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def check(dsn,query,parameters=()):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute(query,parameters).fetchall()


def policy(user,inst,*,version=0,enabled=True,tz="UTC",start=None,end=None,key=None):
    return NotificationPolicyCommand(
        instance_id=inst,expected_generation=1,expected_version=version,
        idempotency_key=key or uuid4().hex,enabled=enabled,
        timezone=tz,quiet_start_min=start,quiet_end_min=end,
    )


def schedule(user,inst,*,due=None,kind="REMINDER",recur="NONE",tz="UTC",key=None):
    return ScheduleNotificationCommand(
        intent_id=uuid4(),instance_id=inst,expected_generation=1,
        idempotency_key=key or uuid4().hex,kind=kind,
        body="Ask me how my planned goal is progressing.",
        due_at=due or datetime.now(timezone.utc)-timedelta(minutes=1),
        timezone=tz,recurrence=recur,
    )


def enabled(dsn,user,inst,**kwargs):
    return call(dsn,update_notification_policy,user_actor(user),
                policy(user,inst,**kwargs))


def _ready(dsn):
    user,first,second=seed(dsn)
    enabled(dsn,user,first)
    return user,first,second


def test_explicit_opt_in_required_and_stale_persona_rejected(dsn):
    user,first,second=seed(dsn)
    cmd=schedule(user,first)
    with pytest.raises(PermissionDenied,match="opt-in"):
        call(dsn,schedule_notification,user_actor(user),cmd)
    with pytest.raises(PermissionDenied):
        call(dsn,update_notification_policy,user_actor(uuid4()),
             policy(user,first))
    enabled(dsn,user,first)
    call(dsn,switch_persona,user_actor(user),switch_cmd(first,second))
    with pytest.raises(StaleSelection):
        call(dsn,schedule_notification,user_actor(user),cmd)
    assert check(dsn,"SELECT count(*) FROM notification_intents WHERE user_id=%s",(user,))==[(0,)]


def test_due_inbox_from_inactive_persona_no_cognition_and_user_reply_queued(dsn):
    user,first,second=_ready(dsn)
    cmd=schedule(user,first,kind="FOLLOW_UP")
    result=call(dsn,schedule_notification,user_actor(user),cmd)
    call(dsn,switch_persona,user_actor(user),switch_cmd(first,second))
    assert call(dsn,dispatch_due_notifications)>=1
    inbox=call(dsn,list_notification_inbox,user_actor(user))
    mine=[item for item in inbox if item.intent_id==cmd.intent_id]
    assert len(mine)==1 and mine[0].instance_id==first
    assert mine[0].kind=="FOLLOW_UP"
    occurrence=mine[0].occurrence_id
    reply=call(dsn,respond_to_notification,user_actor(user),
               NotificationActionCommand(
                   occurrence_id=occurrence,idempotency_key=uuid4().hex,
                   action="REPLY",reply_text="I finished the task today!",
               ))
    assert reply.occurrence_id==occurrence
    assert check(dsn,
        """SELECT s.instance_id,s.generation,s.mode FROM persona_selection s
           WHERE s.user_id=%s""",(user,),
    )==[(second,2,"FOREGROUND")]
    assert check(dsn,
        "SELECT state FROM notification_inbound_replies WHERE occurrence_id=%s",
        (occurrence,),
    )==[("PENDING_PERSONA",)]
    with pytest.raises(StaleSelection):
        call(dsn,list_pending_notification_replies,user_actor(user),first)
    # Reactivation, rather than the notification callback, grants access.
    call(dsn,switch_persona,user_actor(user),switch_cmd(second,first,generation=2))
    queued=call(dsn,list_pending_notification_replies,user_actor(user),first)
    assert len(queued)==1 and queued[0]["reply_text"]=="I finished the task today!"
    assert check(dsn,
        "SELECT state FROM notification_inbound_replies WHERE occurrence_id=%s",
        (occurrence,),
    )==[("PENDING_PERSONA",)]
    # The notification itself cannot create a new Goal or a conversation turn.
    assert check(dsn,"SELECT count(*) FROM goal_records WHERE user_id=%s",(user,))==[(0,)]
    assert check(dsn,"SELECT count(*) FROM conversation_turns WHERE user_id=%s",(user,))==[(0,)]
    assert check(dsn,
        """SELECT count(*) FROM events e JOIN outbox b USING(event_id)
           WHERE e.event_type='notification.available.v1'
             AND e.payload->>'intent_id'=%s""",(str(cmd.intent_id),),
    )==[(1,)]


def test_delivery_and_action_receipts_are_exactly_once(dsn):
    user,first,_=seed(dsn)
    pref=policy(user,first,key="same-policy")
    result=call(dsn,update_notification_policy,user_actor(user),pref)
    assert call(dsn,update_notification_policy,user_actor(user),pref).replayed
    with pytest.raises(IdempotencyConflict):
        call(dsn,update_notification_policy,user_actor(user),
             pref.model_copy(update={"enabled":False}))
    cmd=schedule(user,first,key="same-intent")
    scheduled=call(dsn,schedule_notification,user_actor(user),cmd)
    assert call(dsn,schedule_notification,user_actor(user),cmd).replayed
    with pytest.raises(IdempotencyConflict):
        call(dsn,schedule_notification,user_actor(user),
             cmd.model_copy(update={"body":"New content"}))
    assert call(dsn,dispatch_due_notifications)>=1
    call(dsn,dispatch_due_notifications)
    occ=next(i for i in call(dsn,list_notification_inbox,user_actor(user))
             if i.intent_id==cmd.intent_id)
    action=NotificationActionCommand(
        occurrence_id=occ.occurrence_id,idempotency_key="same-action",action="ACK",
    )
    res=call(dsn,respond_to_notification,user_actor(user),action)
    assert call(dsn,respond_to_notification,user_actor(user),action).replayed
    with pytest.raises(IdempotencyConflict):
        call(dsn,respond_to_notification,user_actor(user),
             action.model_copy(update={"action":"STOP"}))
    assert check(dsn,
        """SELECT count(*) FROM notification_occurrences
           WHERE intent_id=%s""",(cmd.intent_id,),
    )==[(1,)]
    for event_id in (result.event_id,scheduled.event_id,res.event_id):
        assert check(dsn,"SELECT count(*) FROM outbox WHERE event_id=%s",(event_id,))==[(1,)]


def test_quiet_hours_defers_and_new_policy_releases_without_duplicates(dsn):
    user,first,_=seed(dsn)
    now=datetime.now(timezone.utc)
    local=now.astimezone(ZoneInfo("Europe/Berlin"))
    minute=local.hour*60+local.minute
    enabled(dsn,user,first,tz="Europe/Berlin",
            start=(minute-20)%1440,end=(minute+20)%1440)
    cmd=schedule(user,first,due=now-timedelta(minutes=1),tz="Europe/Berlin")
    call(dsn,schedule_notification,user_actor(user),cmd)
    assert call(dsn,dispatch_due_notifications)==0
    assert check(dsn,
        "SELECT count(*) FROM notification_occurrences WHERE intent_id=%s",
        (cmd.intent_id,),
    )==[(0,)]
    update=policy(user,first,version=1,tz="Europe/Berlin")
    call(dsn,update_notification_policy,user_actor(user),update)
    assert call(dsn,dispatch_due_notifications)==1
    assert call(dsn,dispatch_due_notifications)==0


def test_daily_recurs_on_local_wall_clock_and_missed_times_coalesce(dsn):
    user,first,_=_ready(dsn)
    overdue=datetime.now(timezone.utc)-timedelta(days=4)
    cmd=schedule(user,first,due=overdue,recur="DAILY",tz="Europe/Berlin")
    call(dsn,schedule_notification,user_actor(user),cmd)
    assert call(dsn,dispatch_due_notifications)==1
    assert check(dsn,
        "SELECT count(*) FROM notification_occurrences WHERE intent_id=%s",
        (cmd.intent_id,),
    )==[(0,)]
    nxt=check(dsn,"SELECT next_due_at FROM notification_intents WHERE intent_id=%s",
              (cmd.intent_id,))[0][0]
    assert nxt>datetime.now(timezone.utc)
    assert nxt.astimezone(ZoneInfo("Europe/Berlin")).date()==(
        datetime.now(ZoneInfo("Europe/Berlin")).date()+timedelta(days=1)
    )
    assert check(dsn,
        """SELECT count(*) FROM events WHERE event_type='notification.missed.v1'
           AND payload->>'intent_id'=%s""",(str(cmd.intent_id),),
    )==[(1,)]
    assert call(dsn,dispatch_due_notifications)==0


def test_one_shot_missed_due_expires_without_spam(dsn):
    user,first,_=_ready(dsn)
    cmd=schedule(user,first,due=datetime.now(timezone.utc)-timedelta(days=3))
    call(dsn,schedule_notification,user_actor(user),cmd)
    assert call(dsn,dispatch_due_notifications)==1
    assert call(dsn,dispatch_due_notifications)==0
    assert check(dsn,"SELECT active FROM notification_intents WHERE intent_id=%s",
                 (cmd.intent_id,))==[(False,)]
    assert check(dsn,
        "SELECT count(*) FROM notification_occurrences WHERE intent_id=%s",
        (cmd.intent_id,),
    )==[(0,)]


def test_snooze_stop_and_response_cross_user_isolation(dsn):
    user,first,_=_ready(dsn)
    cmd=schedule(user,first,due=datetime.now(timezone.utc)-timedelta(minutes=1),
                 recur="DAILY")
    call(dsn,schedule_notification,user_actor(user),cmd)
    assert call(dsn,dispatch_due_notifications)==1
    occ=next(i for i in call(dsn,list_notification_inbox,user_actor(user))
             if i.intent_id==cmd.intent_id)
    with pytest.raises(PermissionDenied):
        call(dsn,respond_to_notification,user_actor(uuid4()),
             NotificationActionCommand(occurrence_id=occ.occurrence_id,
                                       idempotency_key=uuid4().hex,action="STOP"))
    snooze=NotificationActionCommand(occurrence_id=occ.occurrence_id,
                                     idempotency_key=uuid4().hex,action="SNOOZE",
                                     snooze_minutes=5)
    call(dsn,respond_to_notification,user_actor(user),snooze)
    assert all(i.occurrence_id!=occ.occurrence_id
               for i in call(dsn,list_notification_inbox,user_actor(user)))
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE notification_occurrences
               SET snoozed_until=clock_timestamp()-interval '1 second'
               WHERE occurrence_id=%s""",(occ.occurrence_id,),
        )
    assert call(dsn,dispatch_due_notifications)==1
    assert any(i.occurrence_id==occ.occurrence_id
               for i in call(dsn,list_notification_inbox,user_actor(user)))
    stop=NotificationActionCommand(occurrence_id=occ.occurrence_id,
                                   idempotency_key=uuid4().hex,action="STOP")
    call(dsn,respond_to_notification,user_actor(user),stop)
    assert check(dsn,
        "SELECT active FROM notification_intents WHERE intent_id=%s",(cmd.intent_id,),
    )==[(False,)]
    assert check(dsn,
        "SELECT count(*) FROM notification_occurrences WHERE intent_id=%s",
        (cmd.intent_id,),
    )==[(1,)]


def test_duplicate_platform_ticks_cannot_duplicate_occurrence(dsn):
    user,first,_=_ready(dsn)
    cmd=schedule(user,first)
    call(dsn,schedule_notification,user_actor(user),cmd)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:call(dsn,dispatch_due_notifications),range(3)))
    assert sum(results)==1
    assert check(dsn,
        "SELECT count(*) FROM notification_occurrences WHERE intent_id=%s",
        (cmd.intent_id,),
    )==[(1,)]


def test_local_dst_recurring_schedule_gap_and_fold():
    zone=ZoneInfo("Europe/Berlin")
    before_spring=datetime(2026,3,28,12,tzinfo=zone).astimezone(timezone.utc)
    next_spring=_next_daily(before_spring,"Europe/Berlin",2,30)
    local_spring=next_spring.astimezone(zone)
    assert local_spring.date().isoformat()=="2026-03-29"
    assert local_spring.hour==3  # nonexistent 02:30 is moved after DST gap
    before_fall=datetime(2026,10,24,12,tzinfo=zone).astimezone(timezone.utc)
    fall=_next_daily(before_fall,"Europe/Berlin",2,30)
    assert fall.isoformat()=="2026-10-25T00:30:00+00:00"  # first fold only
