"""P4-02 real PostgreSQL: frozen input, sequential handover and fail-closed gate."""

import json
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")

from persona_agent.background_coordinator import (
    StaleBackgroundPreparation, advance_background_preparation,
    run_preparation_scan,
)
from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply, TextInputCommand
from persona_agent.lifecycle import (
    BackgroundReason, EnterBackgroundCommand, PrimaryActivityCommand,
    SwitchPersonaCommand, TrustedPlatformActor,
    enter_background, record_primary_activity, switch_persona,
)
from persona_agent.operation_contracts import Status
from persona_agent.operation_scheduler import claim_operation, configure_resource_pool
from persona_agent.cognitive_contracts import COGNITIVE_CAPABILITY,COGNITIVE_OPERATION
from persona_agent.cognitive_engine import run_cognition, schedule_cognition
from persona_agent.summary_contracts import SUMMARY_CAPABILITY,SUMMARY_OPERATION
from persona_agent.summary_memory import run_summary, schedule_summary
from persona_agent.runtime import PermissionDenied


def call(dsn, fn, *args, **kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def seed(dsn):
    user, first, other, definition1, definition2 = (uuid4() for _ in range(5))
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)", (user,))
            conn.execute(
                """INSERT INTO persona_definitions(id,stable_key)
                   VALUES (%s,%s),(%s,%s)""",
                (definition1,f"p402-{definition1}",definition2,f"p402-{definition2}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'First'),(%s,%s,%s,'Other')""",
                (first,user,definition1,other,user,definition2),
            )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",
                (user,first),
            )
    call(dsn,configure_resource_pool,"cognition.llm",
         capacity_units=3,p0_reserved_units=1)
    return user,first,other


def user_actor(user):
    return TrustedActor(user_id=user,source_id="p402-user")


def coordinator(user):
    return TrustedPlatformActor(user_id=user,source_id="background.preparation")


class FastResponse:
    def generate(self, messages):
        return ModelReply(model_id="p4-fast",text="Understood.")


class SummaryModel:
    def generate(self, messages):
        return ModelReply(model_id="p4-summary",text=json.dumps({
            "summary":"The user discussed their progress.", "episodes": [],
        }))


class CognitiveModel:
    def generate(self, messages):
        return ModelReply(model_id="p4-reflection",text=json.dumps({
            "proposals": [],
        }))


def add_closed_turn(dsn, user, instance, conversation, text):
    result=call(dsn,ingest_text,user_actor(user),TextInputCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,idempotency_key=str(uuid4()),text=text,
    ))
    response=call(dsn,respond_to_text,user_actor(user),result.turn_id,
                  FastResponse())
    call(dsn,acknowledge_text_delivery,user_actor(user),response.response_id)
    return result


def enter(dsn,user,instance):
    return call(dsn,enter_background,user_actor(user),EnterBackgroundCommand(
        instance_id=instance,expected_generation=1,expected_presence_revision=0,
        reason=BackgroundReason.EXPLICIT_DEPARTURE,idempotency_key=str(uuid4()),
    ))


def tick(dsn,user,instance,epoch=1):
    return call(dsn,advance_background_preparation,coordinator(user),instance,epoch)


def claim(dsn,kind):
    capability=(SUMMARY_CAPABILITY,) if kind==SUMMARY_OPERATION else (COGNITIVE_CAPABILITY,)
    lease=call(dsn,claim_operation,worker_id=f"p4-worker-{uuid4()}",
               capabilities=capability,operation_types=(kind,),lease_seconds=120)
    assert lease is not None
    return lease


def check(dsn,sql,params):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return conn.execute(sql,params).fetchall()


def test_frozen_conversations_and_full_tail_staged_sequentially(dsn):
    user,instance,_=seed(dsn)
    first,second=uuid4(),uuid4()
    add_closed_turn(dsn,user,instance,first,"I was gardening yesterday.")
    add_closed_turn(dsn,user,instance,second,"I prepared a healthy meal.")
    add_closed_turn(dsn,user,instance,first,"I want to practice again.")
    entered=enter(dsn,user,instance)
    assert entered.background_epoch==1
    snapshot=check(dsn,
        """SELECT conversation_id,target_sequence
           FROM background_preparation_conversations WHERE user_id=%s
           ORDER BY conversation_id""",(user,))
    assert dict(snapshot)=={first:2,second:1}

    seen_summaries=set()
    seen_reflections=set()
    for _ in range(12):
        progress=tick(dsn,user,instance)
        if progress.stage=="CRITICAL_PENDING":
            break
        assert progress.state=="ENQUEUED", progress
        if progress.stage=="SUMMARIZE":
            assert progress.conversation_id not in seen_summaries
            seen_summaries.add(progress.conversation_id)
            lease=claim(dsn,SUMMARY_OPERATION)
            assert lease.operation_id==progress.operation_id
            call(dsn,run_summary,lease,SummaryModel())
        else:
            assert progress.stage=="EVALUATE"
            assert progress.conversation_id not in seen_reflections
            seen_reflections.add(progress.conversation_id)
            lease=claim(dsn,COGNITIVE_OPERATION)
            assert lease.operation_id==progress.operation_id
            call(dsn,run_cognition,lease,CognitiveModel())
    else:
        pytest.fail("background handover never reached critical owner gate")
    assert seen_summaries=={first,second}
    assert seen_reflections=={first,second}
    assert progress.state=="AWAITING_OWNER_COMMITS"
    assert tick(dsn,user,instance).state=="AWAITING_OWNER_COMMITS"

    marks=check(dsn,
        """SELECT conversation_id,summary_watermark,cognitive_watermark
           FROM conversations WHERE user_id=%s AND instance_id=%s""",
        (user,instance),
    )
    assert {x[0]:(x[1],x[2]) for x in marks}=={first:(2,2),second:(1,1)}
    assert check(dsn,
        """SELECT preparation_stage FROM background_preparation_epochs
           WHERE user_id=%s AND instance_id=%s AND background_epoch=1""",
        (user,instance),
    )==[("CRITICAL_PENDING",)]
    # Completion of the P2 model operations is not acceptance of critical
    # owner-domain changes or of the Phase 4 ALIGN-01 review.
    assert check(dsn,
        """SELECT 1 FROM background_ready_epochs WHERE user_id=%s
             AND instance_id=%s""",(user,instance),
    )==[]
    changes=check(dsn,
        """SELECT count(*) FROM events e
           WHERE e.user_id=%s AND e.instance_id=%s
             AND e.event_type='background.preparation.stage_changed.v1'""",
        (user,instance),
    )
    assert changes==[(2,)]


def test_pending_turn_blocks_summarization_without_skipping_it(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    call(dsn,ingest_text,user_actor(user),TextInputCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,idempotency_key=str(uuid4()),
        text="I need to update my evening routine.",
    ))
    enter(dsn,user,instance)
    progress=tick(dsn,user,instance)
    assert (progress.stage,progress.state)==("SUMMARIZE","WAITING_RESPONSE")
    assert check(dsn,
        """SELECT preparation_stage FROM background_preparation_epochs
           WHERE user_id=%s AND instance_id=%s""",(user,instance),
    )==[("SUMMARIZE",)]
    assert check(dsn,
        """SELECT 1 FROM background_ready_epochs WHERE user_id=%s""",(user,),
    )==[]


def test_no_history_reaches_critical_owner_gate_but_not_ready(dsn):
    user,instance,_=seed(dsn)
    enter(dsn,user,instance)
    progress=tick(dsn,user,instance)
    assert (progress.stage,progress.state)==(
        "CRITICAL_PENDING","AWAITING_OWNER_COMMITS"
    )
    # The scan is global and may also discover other test-owned epochs; our
    # completed epoch is excluded without implying a global empty queue.
    assert check(dsn,
        """SELECT preparation_stage FROM background_preparation_epochs
           WHERE user_id=%s AND instance_id=%s""",(user,instance),
    )==[("CRITICAL_PENDING",)]


def test_return_and_persona_switch_invalidate_preparation(dsn):
    user,instance,other=seed(dsn)
    conversation=uuid4()
    add_closed_turn(dsn,user,instance,conversation,"Today's workout felt better.")
    enter(dsn,user,instance)
    assert tick(dsn,user,instance).state=="ENQUEUED"
    call(dsn,record_primary_activity,user_actor(user),PrimaryActivityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleBackgroundPreparation):
        tick(dsn,user,instance)
    assert check(dsn,
        """SELECT status FROM background_preparation_epochs
           WHERE user_id=%s""",(user,),
    )==[("SUPERSEDED",)]
    call(dsn,switch_persona,user_actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleBackgroundPreparation):
        tick(dsn,user,instance)


def test_terminal_summary_failure_blocks_progress(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    add_closed_turn(dsn,user,instance,conversation,"Garden progress today.")
    enter(dsn,user,instance)
    first=tick(dsn,user,instance)
    assert first.state=="ENQUEUED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        # Simulate the terminal P0 state after its bounded attempt budget.
        # The coordinator does not own the operation and cannot retry it.
        conn.execute(
            """UPDATE operations SET status='FAILED',version=version+1
               WHERE operation_id=%s AND status='QUEUED'""",
            (first.operation_id,),
        )
    blocked=tick(dsn,user,instance)
    assert blocked.stage=="BLOCKED"
    assert blocked.state=="REQUIRES_OPERATOR"
    assert tick(dsn,user,instance).state=="BLOCKED"
    assert check(dsn,"SELECT count(*) FROM background_ready_epochs WHERE user_id=%s",
                 (user,))==[(0,)]


def test_untrusted_platform_and_cross_user_denied(dsn):
    user,instance,_=seed(dsn)
    stranger,_,_=seed(dsn)
    enter(dsn,user,instance)
    with pytest.raises(PermissionDenied):
        call(dsn,advance_background_preparation,TrustedPlatformActor(
            user_id=user,source_id="random.component",
        ),instance,1)
    with pytest.raises(StaleBackgroundPreparation):
        tick(dsn,stranger,instance)
    assert tick(dsn,user,instance).stage=="CRITICAL_PENDING"


def test_full_tail_operations_require_coordinator_and_background_mode(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    add_closed_turn(dsn,user,instance,conversation,"A finished response.")
    with pytest.raises(PermissionDenied):
        call(dsn,schedule_summary,user_actor(user),conversation,instance,1,
             retain_recent=0)
    with pytest.raises(PermissionDenied):
        call(dsn,schedule_summary,TrustedPlatformActor(
            user_id=user,source_id="memory.auto_summary.v1",
        ),conversation,instance,1,retain_recent=0)
    with pytest.raises(PermissionDenied):
        call(dsn,schedule_cognition,TrustedPlatformActor(
            user_id=user,source_id="cognition.auto_reflection.v1",
        ),conversation,instance,1,retain_recent=0,min_closed_turns=1)
    # Even a trusted coordinator cannot trigger full-tail inference in
    # Foreground (whether or not an entry had previously been prepared).
    from persona_agent.runtime import StaleSelection
    with pytest.raises(StaleSelection):
        call(dsn,schedule_summary,coordinator(user),conversation,instance,1,
             retain_recent=0)
    with pytest.raises(StaleSelection):
        call(dsn,schedule_cognition,coordinator(user),conversation,instance,1,
             retain_recent=0,min_closed_turns=1)
