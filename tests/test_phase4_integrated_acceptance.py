"""P4-07: end-to-end Phase 4 lifecycle and communication contract drills.

All tests run against actual PostgreSQL, durable P0 claims, real domain
commands and Background readiness barriers. Inference is deterministically
stubbed here; the separate live Ollama CI exercise validates the provider.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.background_autonomy import dispatch_one_background_step
from persona_agent.lifecycle import switch_persona,TrustedPlatformActor
from persona_agent.notification_contracts import (
    NotificationPolicyCommand,ScheduleNotificationCommand,NotificationActionCommand,
)
from persona_agent.notification_state import (
    update_notification_policy,schedule_notification,dispatch_due_notifications,
    list_notification_inbox,respond_to_notification,list_pending_notification_replies,
)
from persona_agent.procedure_agent_checkpoint import load_validated_procedure_checkpoint
from persona_agent.procedure_agent_state import (
    renew_background_agent_step,run_one_agent_step,reconcile_agent_step,
)
from persona_agent.runtime import StaleSelection,PermissionDenied
from persona_agent.tool_contracts import PROCEDURE_AGENT_OPERATION
from test_phase4_resumption import _stopped,_renew_command,_enter_next_ready
from test_phase4_preemption import _admitted,_return
from test_phase4_autonomy import producer
from test_phase4_preparation import call,check,user_actor
from test_phase0_lifecycle import switch_cmd


def _opt_in(dsn,user,instance):
    return call(dsn,update_notification_policy,user_actor(user),
                NotificationPolicyCommand(
                    instance_id=instance,expected_generation=1,
                    expected_version=0,idempotency_key=uuid4().hex,
                    enabled=True,timezone="Europe/Berlin",
                    quiet_start_min=None,quiet_end_min=None,
                ))


def _follow_up(dsn,user,instance,run):
    conversation,source_turn=check(
        dsn,"SELECT source_conversation_id,source_turn_id "
            "FROM procedure_runs WHERE run_id=%s",(run,),
    )[0]
    command=ScheduleNotificationCommand(
        intent_id=uuid4(),instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,kind="FOLLOW_UP",
        body="Would you like to review your previous plan?",
        due_at=datetime.now(timezone.utc)-timedelta(minutes=1),
        timezone="Europe/Berlin",recurrence="NONE",
        source_conversation_id=conversation,source_turn_id=source_turn,
    )
    call(dsn,schedule_notification,user_actor(user),command)
    return command


def _reply(dsn,user,occurrence_id):
    command=NotificationActionCommand(
        occurrence_id=occurrence_id,idempotency_key=uuid4().hex,
        action="REPLY",reply_text="Yes, please review it later.",
    )
    reply=call(dsn,respond_to_notification,user_actor(user),command)
    assert call(dsn,respond_to_notification,user_actor(user),command).replayed
    return reply


def _drain_only_notification(dsn,user,intent):
    assert call(dsn,dispatch_due_notifications,limit=1)==1
    inbox=[r for r in call(dsn,list_notification_inbox,user_actor(user))
           if r.intent_id==intent.intent_id]
    assert len(inbox)==1
    assert call(dsn,dispatch_due_notifications,limit=1)==0
    return inbox[0]


def test_safe_stop_to_new_epoch_resume_plus_inactive_mode_communication(
    dsn,monkeypatch,
):
    """P4-01..06 full path: safe-stop, approval, new READY, P2, inbox, reply."""
    user,instance,task,run,source,checkpoint=_stopped(dsn,monkeypatch)
    assert call(dsn,load_validated_procedure_checkpoint,user_actor(user),
                operation_id=source).operation_id==source
    _opt_in(dsn,user,instance)
    intent=_follow_up(dsn,user,instance,run)
    renewed=_renew_command(dsn,user,instance,run,source,checkpoint)
    call(dsn,renew_background_agent_step,user_actor(user),renewed)
    assert check(dsn,
        "SELECT count(*) FROM operations WHERE operation_id=%s",
        (renewed.operation_id,),
    )==[(0,)]
    _enter_next_ready(dsn,user,instance)
    selected=check(dsn,
        "SELECT mode,background_epoch FROM persona_selection WHERE user_id=%s",(user,),
    )
    assert selected==[("BACKGROUND",3)]
    dispatched=call(dsn,dispatch_one_background_step,producer(user),instance,3)
    assert dispatched.operation_id==renewed.operation_id
    observed=[]
    lease=call(dsn,run_one_agent_step,
        worker_id=f"p407-resume-{uuid4().hex[:8]}",
        executor=lambda instruction,max_words:(
            observed.append((instruction,max_words)) or "phase4 integrated safe result"),
    )
    assert lease is not None and lease.operation_id==renewed.operation_id
    assert len(observed)==1 and observed[0][1]==5
    result=call(
        dsn,reconcile_agent_step,
        TrustedPlatformActor(user_id=user,source_id="background.autonomous.result"),
        renewed.operation_id,
    )
    assert result.resolution=="VERIFIED"
    notice=_drain_only_notification(dsn,user,intent)
    assert notice.instance_id==instance
    _reply(dsn,user,notice.occurrence_id)
    # In-app platform delivery + reply must NOT wake Background cognition.
    with pytest.raises(StaleSelection):
        call(dsn,list_pending_notification_replies,user_actor(user),instance)
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]
    assert check(dsn,
        "SELECT status,checkpoint_ref FROM operations WHERE operation_id=%s",(source,),
    )==[("SUSPENDED",checkpoint)]
    assert check(dsn,
        "SELECT status,background_epoch FROM operations WHERE operation_id=%s",
        (renewed.operation_id,),
    )==[("COMPLETED",3)]
    assert check(dsn,
        "SELECT count(*) FROM notification_inbound_replies WHERE user_id=%s "
        "AND instance_id=%s AND state='PENDING_PERSONA'",(user,instance),
    )==[(1,)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_resume_links "
        "WHERE operation_id=%s AND source_operation_id=%s",
        (renewed.operation_id,source),
    )==[(1,)]
    assert check(dsn,
        "SELECT count(*) FROM operation_resource_reservations "
        "WHERE operation_id=%s",(renewed.operation_id,),
    )==[(0,)]
    _return(dsn,user,instance)
    messages=call(dsn,list_pending_notification_replies,user_actor(user),instance)
    assert len(messages)==1
    assert check(dsn,
        "SELECT count(*) FROM conversation_turns WHERE turn_id=%s",
        (messages[0]["reply_id"],),
    )==[(0,)]
    for name in (
        "procedure.agent.background_resumption_authorized.v1",
        "notification.scheduled.v1","notification.available.v1",
        "notification.response.v1",
    ):
        assert check(dsn,
            "SELECT count(*) FROM events e JOIN outbox b USING(event_id) "
            "WHERE e.user_id=%s AND e.instance_id=%s AND e.event_type=%s",
            (user,instance,name),
        )==[(1,)]


def test_active_p2_tool_never_blocks_foreground_notifications_or_commits_late(
    dsn,monkeypatch,record_property,
):
    """User return / notification opt-in executes while a P2 tool is held."""
    user,instance,task,run,operation=_admitted(dsn,monkeypatch)
    entered=Event()
    release=Event()
    observed=[]
    def blocked_tool(instruction,max_words):
        entered.set()
        if not release.wait(timeout=20):
            raise RuntimeError("test tool release deadline exceeded")
        observed.append("finished after return")
        return "late model/tool completion must be discarded"

    with ThreadPoolExecutor(max_workers=1) as pool:
        job=pool.submit(call,dsn,run_one_agent_step,
                        worker_id=f"p407-live-{uuid4().hex[:8]}",
                        executor=blocked_tool)
        try:
            assert entered.wait(timeout=20),"P2 never reached the test tool"
            started=monotonic()
            returned=_return(dsn,user,instance)
            _opt_in(dsn,user,instance)
            intent=_follow_up(dsn,user,instance,run)
            notify=_drain_only_notification(dsn,user,intent)
            elapsed_ms=(monotonic()-started)*1000
            record_property("foreground_database_roundtrip_ms",
                            round(elapsed_ms,2))
            # A blocked Background tool must not hold any SQL lock that
            # requires the user to wait for the tool's completion.
            assert not release.is_set()
            assert returned.mode.value=="FOREGROUND"
            assert notify.instance_id==instance
            assert check(dsn,
                "SELECT count(*) FROM procedure_agent_operation_results "
                "WHERE operation_id=%s",(operation,),
            )==[(0,)]
        finally:
            release.set()
        lease=job.result(timeout=20)
    assert lease is not None and lease.operation_id==operation
    assert observed==["finished after return"]
    assert check(dsn,
        "SELECT status FROM operations WHERE operation_id=%s",(operation,),
    )==[("SUSPENDED",)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_operation_results "
        "WHERE operation_id=%s",(operation,),
    )==[(0,)]
    assert check(dsn,"SELECT status FROM goal_tasks WHERE task_id=%s",
                 (task,))==[("PLANNED",)]
    assert check(dsn,
        "SELECT count(*) FROM operation_resource_reservations "
        "WHERE operation_id=%s",(operation,),
    )==[(0,)]


def test_inactive_persona_reply_retains_scope_but_cannot_revive_old_checkpoint(
    dsn,monkeypatch,
):
    """Switch fences P2 authority; notification still targets original persona."""
    user,instance,task,run,source,checkpoint=_stopped(dsn,monkeypatch)
    _opt_in(dsn,user,instance)
    intent=_follow_up(dsn,user,instance,run)
    other_persona,other=uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES(%s,%s)",
                (other_persona,f"phase4-07-{other_persona}"),
            )
            conn.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                "VALUES(%s,%s,%s,'Another')",
                (other,user,other_persona),
            )
    call(dsn,switch_persona,user_actor(user),switch_cmd(instance,other))
    inbox=_drain_only_notification(dsn,user,intent)
    _reply(dsn,user,inbox.occurrence_id)
    assert check(dsn,
        "SELECT instance_id,generation,mode FROM persona_selection WHERE user_id=%s",
        (user,),
    )==[(other,2,"FOREGROUND")]
    assert check(dsn,
        "SELECT count(*) FROM goal_tasks WHERE task_id=%s AND user_id=%s",
        (task,user),
    )==[(1,)]
    with pytest.raises(StaleSelection):
        call(dsn,load_validated_procedure_checkpoint,user_actor(user),
             operation_id=source)
    with pytest.raises(StaleSelection):
        call(dsn,list_pending_notification_replies,user_actor(user),instance)
    with pytest.raises(PermissionDenied):
        call(dsn,list_notification_inbox,user_actor(uuid4()))
        # An unauthenticated caller cannot observe a different user's inbox.
    call(dsn,switch_persona,user_actor(user),
         switch_cmd(other,instance,generation=2))
    queued=call(dsn,list_pending_notification_replies,user_actor(user),instance)
    assert len(queued)==1 and queued[0]["reply_text"].startswith("Yes")
    assert check(dsn,
        "SELECT count(*) FROM events e JOIN outbox b USING(event_id) "
        "WHERE e.user_id=%s AND e.instance_id=%s "
        "AND e.event_type='notification.response.v1'",(user,instance),
    )==[(1,)]
    assert check(dsn,
        "SELECT count(*) FROM procedure_agent_resume_links "
        "WHERE source_operation_id=%s",(source,),
    )==[(0,)]


def test_failed_resumed_p2_exhaustion_does_not_cancel_independent_inbox(
    dsn,monkeypatch,
):
    """P0 bounded recovery remains independent of user-approved comms."""
    user,instance,task,run,source,checkpoint=_stopped(dsn,monkeypatch)
    _opt_in(dsn,user,instance)
    followup=_follow_up(dsn,user,instance,run)
    renewed=_renew_command(dsn,user,instance,run,source,checkpoint)
    call(dsn,renew_background_agent_step,user_actor(user),renewed)
    _enter_next_ready(dsn,user,instance)
    assert call(dsn,dispatch_one_background_step,
                producer(user),instance,3).operation_id==renewed.operation_id
    def failure(text,max_words):
        raise RuntimeError("bounded simulated read-only failure")
    for attempt,delay in ((1,2),(2,4),(3,None)):
        lease=call(dsn,run_one_agent_step,
                   worker_id=f"p407-error-{attempt}-{uuid4().hex[:6]}",
                   executor=failure)
        assert lease is not None and lease.operation_id==renewed.operation_id
        status,attempts=check(
            dsn,"SELECT status,attempt_count FROM operations "
                "WHERE operation_id=%s",(renewed.operation_id,),
        )[0]
        assert attempts==attempt
        if delay is None:
            assert status=="FAILED"
        else:
            assert status=="RETRY_WAIT"
            # Adjust only database test clock eligibility, not attempts,
            # authorization state, or P0 claim generation.
            with psycopg.connect(dsn,autocommit=True) as conn:
                conn.execute(
                    "UPDATE operations SET eligible_at=clock_timestamp()-"
                    "interval '1 second' WHERE operation_id=%s",
                    (renewed.operation_id,),
                )
    assert call(dsn,run_one_agent_step,
                worker_id="p407-do-not-repeat",executor=failure) is None
    inbox=_drain_only_notification(dsn,user,followup)
    _reply(dsn,user,inbox.occurrence_id)
    assert check(dsn,
        "SELECT status FROM goal_tasks WHERE task_id=%s",(task,),
    )==[("PLANNED",)]
    assert check(dsn,
        "SELECT count(*) FROM notification_inbound_replies WHERE user_id=%s "
        "AND state='PENDING_PERSONA'",(user,),
    )==[(1,)]
    assert check(dsn,
        "SELECT count(*) FROM operation_resource_reservations "
        "WHERE operation_id=%s",(renewed.operation_id,),
    )==[(0,)]
