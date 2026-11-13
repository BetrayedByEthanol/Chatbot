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
    for _ in range(8):
        progress=tick(dsn,user,instance)
        if progress.stage=="ALIGNMENT_PENDING":
            break
        assert progress.stage=="CRITICAL_PENDING",progress
    else:
        pytest.fail("critical source scan did not reach alignment handoff")
    assert progress.state=="AWAITING_ALIGNMENT"

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
    )==[("ALIGNMENT_PENDING",)]
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
    assert changes==[(3,)]


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


def test_incomplete_snapshot_never_means_no_conversation(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    add_closed_turn(dsn,user,instance,conversation,"Important instruction before departure.")
    enter(dsn,user,instance)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT captured_conversation_count
               FROM background_preparation_epochs
               WHERE user_id=%s AND instance_id=%s AND background_epoch=1""",
            (user,instance),
        ).fetchone()[0]==1
        # Corrupt the derived snapshot to model a partial migration or a
        # failed manual repair. The production entry transaction is atomic.
        conn.execute(
            """DELETE FROM background_preparation_conversations
               WHERE user_id=%s AND instance_id=%s AND background_epoch=1""",
            (user,instance),
        )
    result=tick(dsn,user,instance)
    assert (result.stage,result.state)==("BLOCKED","REQUIRES_OPERATOR")
    assert check(dsn,
        "SELECT 1 FROM background_ready_epochs WHERE user_id=%s",
        (user,),
    )==[]


def test_completed_job_watermark_race_does_not_block_epoch(dsn,monkeypatch):
    import persona_agent.background_coordinator as coordinator_module

    user,instance,_=seed(dsn)
    conversation=uuid4()
    add_closed_turn(dsn,user,instance,conversation,"The source turn was delivered.")
    enter(dsn,user,instance)

    def completed_after_initial_read(cursor,conversation_id,generation,start_sequence,
                                     *,summary):
        assert summary and conversation_id==conversation
        # Simulate an independent P0 summary worker committing the watermark
        # after preparation has read the old watermark, but before this SQL
        # statement sees its new COMPLETED status. The coordinator holds no
        # lock on this conversation row, by design.
        with psycopg.connect(dsn,autocommit=True) as other:
            other.execute(
                """UPDATE conversations SET summary_watermark=1
                   WHERE conversation_id=%s AND user_id=%s AND instance_id=%s""",
                (conversation,user,instance),
            )
        return uuid4(),"COMPLETED"

    monkeypatch.setattr(coordinator_module,"_job_status",completed_after_initial_read)
    progress=tick(dsn,user,instance)
    assert (progress.stage,progress.state)==("SUMMARIZE","RECHECK_WATERMARK")
    assert check(dsn,
        """SELECT preparation_stage FROM background_preparation_epochs
           WHERE user_id=%s AND instance_id=%s AND background_epoch=1""",
        (user,instance),
    )==[("SUMMARIZE",)]
    assert check(dsn,
        """SELECT 1 FROM background_ready_epochs WHERE user_id=%s""",
        (user,),
    )==[]


# P4-02b source-linked critical update integration tests ---------------------

from persona_agent.background_critical_updates import list_critical_updates
from persona_agent.goal_contracts import (
    CreateGoalCommand,SetGoalStatusCommand,goal_action_approval_phrase,
)
from persona_agent.goal_state import create_goal,set_goal_status
from persona_agent.memory_extraction import (
    register_extraction_consumer,run_extraction_tick,
)


def drive_to_critical(dsn,user,instance,limit=36,*,epoch=1):
    """Run actual P0 model-operation handover, not a fabricated watermark."""
    for _ in range(limit):
        progress=tick(dsn,user,instance,epoch=epoch)
        if progress.stage=="CRITICAL_PENDING":
            return progress
        assert progress.state=="ENQUEUED",progress
        if progress.stage=="SUMMARIZE":
            call(dsn,run_summary,claim(dsn,SUMMARY_OPERATION),SummaryModel())
        else:
            assert progress.stage=="EVALUATE"
            call(dsn,run_cognition,claim(dsn,COGNITIVE_OPERATION),CognitiveModel())
    pytest.fail("summary/cognitive handover never finished")


def drive_critical(dsn,user,instance,limit=16,*,epoch=1):
    for _ in range(limit):
        result=tick(dsn,user,instance,epoch=epoch)
        if result.state in ("WAITING_OWNER","AWAITING_ALIGNMENT","REQUIRES_OPERATOR"):
            return result
        assert result.stage=="CRITICAL_PENDING",result
    pytest.fail("critical consolidation did not reach a stable result")


def test_critical_scan_reaches_alignment_only_after_source_watermark(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    add_closed_turn(dsn,user,instance,conversation,"We discussed caring for plants.")
    add_closed_turn(dsn,user,instance,conversation,"I'm learning to make soup.")
    enter(dsn,user,instance)
    drive_to_critical(dsn,user,instance)
    done=drive_critical(dsn,user,instance)
    assert done.stage=="ALIGNMENT_PENDING"
    assert done.state=="AWAITING_ALIGNMENT"
    assert call(dsn,list_critical_updates,user_actor(user),instance,1)==()
    assert check(dsn,
        """SELECT critical_committed_watermark FROM conversations
           WHERE conversation_id=%s""",(conversation,),
    )==[(2,)]
    assert check(dsn,
        "SELECT count(*) FROM background_ready_epochs WHERE user_id=%s",(user,),
    )==[(0,)]


def test_action_restriction_is_durable_unresolved_and_not_silently_applied(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=add_closed_turn(dsn,user,instance,conversation,
                           "Do not run autonomous background tasks.")
    enter(dsn,user,instance)
    drive_to_critical(dsn,user,instance)
    waiting=drive_critical(dsn,user,instance)
    assert (waiting.stage,waiting.state)==("CRITICAL_PENDING","WAITING_OWNER")
    items=call(dsn,list_critical_updates,user_actor(user),instance,1)
    assert len(items)==1
    assert (items[0]["domain"],items[0]["status"],items[0]["source_turn_id"])==(
        "INSTRUCTION","PENDING",source.turn_id,
    )
    assert check(dsn,
        "SELECT critical_committed_watermark FROM conversations WHERE conversation_id=%s",
        (conversation,),
    )==[(0,)]
    assert check(dsn,
        "SELECT count(*) FROM background_ready_epochs WHERE user_id=%s",(user,),
    )==[(0,)]


def test_goal_status_requires_actual_owner_command_and_source_turn(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    goal_id=uuid4()
    initial=add_closed_turn(dsn,user,instance,conversation,
                            "I want to make healthy dinners.")
    proposed=CreateGoalCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conversation,evidence_turn_id=initial.turn_id,
        evidence_quote="I want to make healthy dinners.",goal_id=goal_id,
        title="Cook",desired_outcome="Prepare healthy dinners",
        success_criterion="Three dinners each week",horizon="SHORT",
    )
    approved=add_closed_turn(dsn,user,instance,conversation,
                             goal_action_approval_phrase(proposed))
    call(dsn,create_goal,user_actor(user),
         proposed.model_copy(update={"approval_turn_id":approved.turn_id}))
    instruction=add_closed_turn(dsn,user,instance,conversation,"Pause my goal.")
    status=SetGoalStatusCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conversation,evidence_turn_id=instruction.turn_id,
        evidence_quote="Pause my goal.",goal_id=goal_id,
        expected_goal_version=1,new_status="PAUSED",
    )
    approved_status=add_closed_turn(dsn,user,instance,conversation,
                                   goal_action_approval_phrase(status))
    accepted=call(dsn,set_goal_status,user_actor(user),
                  status.model_copy(update={"approval_turn_id":approved_status.turn_id}))
    enter(dsn,user,instance)
    drive_to_critical(dsn,user,instance)
    verified=drive_critical(dsn,user,instance)
    assert verified.stage=="ALIGNMENT_PENDING"
    items=call(dsn,list_critical_updates,user_actor(user),instance,1)
    assert len(items)==1
    assert (items[0]["domain"],items[0]["status"],items[0]["owner_ref"])==(
        "GOAL","VERIFIED",accepted.event_id,
    )
    assert check(dsn,"SELECT status FROM goal_records WHERE goal_id=%s",
                 (goal_id,))==[("PAUSED",)]


def test_memory_conflict_cannot_be_promoted_without_memory_owner(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        register_extraction_consumer(conn)
    first=add_closed_turn(dsn,user,instance,conversation,
                          "My favorite drink is tea.")
    # The existing Memory domain admits only a safe explicit claim; this
    # consumer does not change persona selection or infer a goal.
    call(dsn,run_extraction_tick,worker_id=f"p4-mem-{uuid4()}",
         max_deliveries=1000)
    second=add_closed_turn(dsn,user,instance,conversation,
                           "My favorite drink is coffee.")
    call(dsn,run_extraction_tick,worker_id=f"p4-mem-{uuid4()}",
         max_deliveries=1000)
    statuses=check(dsn,
        """SELECT source_turn_id,status FROM memory_extraction_candidates
           WHERE user_id=%s AND instance_id=%s""",(user,instance),
    )
    assert dict(statuses)=={first.turn_id:"ADMITTED",second.turn_id:"CONFLICT"}
    enter(dsn,user,instance)
    drive_to_critical(dsn,user,instance)
    pending=drive_critical(dsn,user,instance)
    assert (pending.stage,pending.state)==("CRITICAL_PENDING","WAITING_OWNER")
    items=call(dsn,list_critical_updates,user_actor(user),instance,1)
    assert {v["source_turn_id"]:v["status"] for v in items}=={
        first.turn_id:"VERIFIED",second.turn_id:"PENDING",
    }
    assert check(dsn,
        "SELECT count(*) FROM background_ready_epochs WHERE user_id=%s",(user,),
    )==[(0,)]


def test_task_priority_needs_owner_commit_with_separate_user_approval(dsn):
    from persona_agent.task_contracts import (
        CreateTaskCommand,SetTaskPriorityCommand,task_approval_phrase,
    )
    from persona_agent.task_state import create_task,set_task_priority

    user,instance,_=seed(dsn)
    conversation=uuid4()
    goal=uuid4()
    goal_source=add_closed_turn(dsn,user,instance,conversation,
                                "I want to eat healthy dinners.")
    goal_cmd=CreateGoalCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conversation,evidence_turn_id=goal_source.turn_id,
        evidence_quote="I want to eat healthy dinners.",goal_id=goal,
        title="Healthy dinners",desired_outcome="Eat healthy",
        success_criterion="Cook dinner three times a week",horizon="SHORT",
    )
    goal_approval=add_closed_turn(dsn,user,instance,conversation,
                                  goal_action_approval_phrase(goal_cmd))
    call(dsn,create_goal,user_actor(user),
         goal_cmd.model_copy(update={"approval_turn_id":goal_approval.turn_id}))

    task_id=uuid4()
    task_source=add_closed_turn(dsn,user,instance,conversation,
                                "Prepare vegetables this weekend.")
    task_cmd=CreateTaskCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conversation,evidence_turn_id=task_source.turn_id,
        evidence_quote="Prepare vegetables this weekend.",
        task_id=task_id,goal_id=goal,expected_goal_version=1,
        title="Vegetables",action_description="Prepare vegetables",
        completion_criterion="Vegetables prepared",
        evidence_requirement="User confirms",executor="USER",
    )
    task_approval=add_closed_turn(dsn,user,instance,conversation,
                                  task_approval_phrase(task_cmd))
    task_result=call(dsn,create_task,user_actor(user),
                     task_cmd.model_copy(update={"approval_turn_id":task_approval.turn_id}))
    suspend_source=add_closed_turn(dsn,user,instance,conversation,
                                   "Suspend my task.")
    priority_cmd=SetTaskPriorityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conversation,evidence_turn_id=suspend_source.turn_id,
        evidence_quote="Suspend my task.",
        task_id=task_id,expected_task_version=task_result.version,rule="SUSPEND",
    )
    priority_approval=add_closed_turn(dsn,user,instance,conversation,
                                      task_approval_phrase(priority_cmd))
    result=call(dsn,set_task_priority,user_actor(user),
                priority_cmd.model_copy(update={"approval_turn_id":priority_approval.turn_id}))
    enter(dsn,user,instance)
    drive_to_critical(dsn,user,instance)
    verified=drive_critical(dsn,user,instance)
    assert verified.stage=="ALIGNMENT_PENDING"
    issues=call(dsn,list_critical_updates,user_actor(user),instance,1)
    assert len(issues)==1
    assert (issues[0]["domain"],issues[0]["status"],issues[0]["owner_ref"])==(
        "TASK","VERIFIED",result.event_id,
    )
    assert check(dsn,"SELECT suspended FROM goal_tasks WHERE task_id=%s",
                 (task_id,))==[(True,)]
