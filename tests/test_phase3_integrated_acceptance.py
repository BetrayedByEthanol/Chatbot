"""P3-06 integrated acceptance across Phase 3 domain owners.

Real PostgreSQL, real command/event/receipt/outbox paths, actual P0 read-only
worker. No direct writes to authoritative Phase 3 state (other than fixtures
for persona provisioning). Tests deliberately cross domain boundaries instead
of replacing existing isolated P3-01 through P3-05 contract suites.
"""
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from persona_agent.dialogue_contracts import OpenThreadCommand
from persona_agent.dialogue_state import open_thread
from persona_agent.goal_contracts import (
    CreateGoalCommand, ProposeGoalStrategyCommand,
    SelectGoalStrategyCommand, goal_action_approval_phrase,
)
from persona_agent.goal_state import (
    create_goal, list_goals, propose_goal_strategy, select_goal_strategy,
)
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.operation_control import CancelOperationCommand, cancel_operation
from persona_agent.operation_scheduler import claim_operation
from persona_agent.procedure_run_contracts import (
    LinkProcedureThreadCommand, RecordProcedureStepCommand,
)
from persona_agent.procedure_run_state import (
    link_procedure_thread, list_procedure_runs, read_procedure_run,
    record_procedure_step, start_procedure_run,
)
from persona_agent.procedure_state import (
    create_personalized_approach, publish_shared_procedure,
    read_personalized_approach, read_shared_procedure,
)
from persona_agent.procedure_learning_state import (
    decide_procedure_adaptation, evaluate_procedure_run,
    propose_procedure_adaptation, read_procedure_adaptation,
    read_procedure_evaluations, summarize_personal_procedure_experience,
)
from persona_agent.procedure_agent_state import (
    reconcile_agent_step, run_one_agent_step, schedule_agent_step,
)
from persona_agent.runtime import PermissionDenied, StaleSelection, VersionConflict
from persona_agent.task_state import correct_task_outcome, list_tasks

from test_phase3_procedures import (
    actor, approve, definition, publication, seed, turn, variant_command,
)
from test_phase3_runs import (
    correction as step_correction, record, run_status, start_command,
)
from test_phase3_tasks import (
    confirm_done, correction_command as task_correction, edge, make_task,
    report_done,
)
from test_phase3_learning import (
    decide_command, evaluation_command, proposal_command,
)
from test_phase3_agent_operations import (
    agent_method, authorization, configured_pool, operation_row, platform,
)
from persona_agent.tool_contracts import PROCEDURE_AGENT_OPERATION, SLOW_TOOL


def call(dsn, fn, *args, **kwargs):
    """Reopen a DB connection per domain command; recovery is not process-local."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        return fn(connection, *args, **kwargs)


@pytest.fixture(autouse=True)
def isolate_registered_worker_queue(dsn):
    """Avoid relying on global P0 queue order from earlier acceptance cases."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        pending=connection.execute(
            """SELECT operation_id,user_id,version FROM operations
               WHERE operation_type=%s AND status IN ('QUEUED','RETRY_WAIT')""",
            (PROCEDURE_AGENT_OPERATION,),
        ).fetchall()
    for operation_id,user_id,version in pending:
        call(dsn,cancel_operation,actor(user_id),CancelOperationCommand(
            operation_id=operation_id,expected_version=version,
            idempotency_key=uuid4().hex,
        ))


def goal_with_strategy(dsn,user,instance,conversation):
    source=turn(
        dsn,user,instance,conversation,
        "I want a reproducible self-signed TLS setup for my local Nginx test service.",
    )
    goal_id,strategy_id=uuid4(),uuid4()
    create=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="I want a reproducible self-signed TLS setup",
        goal_id=goal_id,title="Reproducible local TLS",
        desired_outcome="A test Nginx endpoint configured with working local TLS",
        success_criterion="A local TLS client completes the test handshake",
        horizon="MEDIUM",
    )
    confirmed=turn(
        dsn,user,instance,conversation,goal_action_approval_phrase(create),
    )
    created=call(
        dsn,create_goal,actor(user),
        create.model_copy(update={"approval_turn_id":confirmed.turn_id}),
    )
    assert created.goal_version==1
    proposed=ProposeGoalStrategyCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="reproducible self-signed TLS",
        goal_id=goal_id,strategy_id=strategy_id,
        title="Guided local certificate",
        approach="Follow the reviewed parameterized local certificate method",
        suitability_note="A self-signed certificate is suitable only for local development",
    )
    call(dsn,propose_goal_strategy,actor(user),proposed)
    select=SelectGoalStrategyCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="reproducible self-signed TLS",
        goal_id=goal_id,strategy_id=strategy_id,expected_goal_version=1,
    )
    selected=turn(
        dsn,user,instance,conversation,goal_action_approval_phrase(select),
    )
    chosen=call(
        dsn,select_goal_strategy,actor(user),
        select.model_copy(update={"approval_turn_id":selected.turn_id}),
    )
    assert chosen.goal_version==2
    return goal_id,strategy_id


def published_private_method(dsn,user,persona,instance,conversation,goal,strategy):
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),publication(
        dsn,user,persona,instance,conversation,
        template,0,definition("PARAMETERIZED"),
    ))
    candidate=variant_command(
        dsn,user,instance,conversation,template,1,
        params={"hostname":"localhost","days":30},
        goal=goal,strategy=strategy,
    )
    created=call(dsn,create_personalized_approach,actor(user),candidate)
    assert created.revision==1
    return template,candidate.variant_id


def link_thread(dsn,user,instance,conversation,run,version):
    source=turn(dsn,user,instance,conversation,
                "Discuss the unfinished TLS procedure and resume its progress.")
    with psycopg.connect(dsn,autocommit=True) as connection:
        revision=connection.execute(
            "SELECT revision FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]
    thread_id=uuid4()
    call(dsn,open_thread,actor(user),OpenThreadCommand(
        instance_id=instance,conversation_id=conversation,
        expected_generation=1,expected_revision=revision,
        evidence_turn_id=source.turn_id,idempotency_key=uuid4().hex,
        thread_id=thread_id,title="Local TLS setup",
    ))
    link=LinkProcedureThreadCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="Discuss the unfinished TLS procedure",
        run_id=run,expected_run_version=version,thread_id=thread_id,
    )
    result=call(dsn,link_procedure_thread,actor(user),link)
    return thread_id,result.version


def assert_exact_event_outbox(dsn,user,instance,event_ids):
    """Every successful cross-domain command has exactly one durable event/outbox."""
    assert len(set(event_ids))==len(event_ids)
    with psycopg.connect(dsn,autocommit=True) as connection:
        events=connection.execute(
            """SELECT event_id,user_id,instance_id FROM events
               WHERE event_id=ANY(%s)""",(list(event_ids),),
        ).fetchall()
        assert len(events)==len(event_ids)
        assert all(uid==user and pid==instance for _,uid,pid in events)
        outbox=connection.execute(
            """SELECT event_id,count(*) FROM outbox
               WHERE event_id=ANY(%s) GROUP BY event_id""",
            (list(event_ids),),
        ).fetchall()
        assert len(outbox)==len(event_ids)
        assert all(count==1 for _,count in outbox)
        receipts=connection.execute(
            """SELECT event_id,count(*) FROM command_receipts
               WHERE event_id=ANY(%s) GROUP BY event_id""",
            (list(event_ids),),
        ).fetchall()
        assert len(receipts)==len(event_ids)
        assert all(count==1 for _,count in receipts)


def test_complete_user_flow_goal_strategy_task_run_sessions_learning(dsn):
    """P3-01 → 02 → 03 → 04 → 05 in one real scoped user history."""
    user,persona,instance=seed(dsn,grant=True)
    conversation,second_conversation=uuid4(),uuid4()
    goal,strategy=goal_with_strategy(dsn,user,instance,conversation)
    template,variant=published_private_method(
        dsn,user,persona,instance,conversation,goal,strategy,
    )

    initial_task,_,_=make_task(
        dsn,user,instance,conversation,goal,2,
        title="Generate and validate local TLS certificate",
    )
    dependent,_,_=make_task(
        dsn,user,instance,conversation,goal,3,
        title="Record local test endpoint details",
    )
    edge(dsn,user,instance,conversation,dependent,initial_task,4)
    assert not {t.task_id:t for t in call(
        dsn,list_tasks,actor(user),instance,goal,
    )}[dependent].actionable

    start=start_command(
        dsn,user,instance,conversation,template,
        variant=variant,revision=1,task=initial_task,
    )
    started=call(dsn,start_procedure_run,actor(user),start)
    assert started.version==1
    assert call(dsn,start_procedure_run,actor(user),start).replayed

    first_thread,version=link_thread(
        dsn,user,instance,conversation,start.run_id,started.version,
    )
    second_thread,version=link_thread(
        dsn,user,instance,second_conversation,start.run_id,version,
    )
    resumed=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert {(x.conversation_id,x.thread_id) for x in resumed.thread_links}=={
        (conversation,first_thread),(second_conversation,second_thread),
    }
    assert resumed.next_step_id=="generate" and resumed.version==version
    assert "30 days" in resumed.steps[0].step.instruction

    report_one=record(
        dsn,user,instance,second_conversation,start.run_id,
        version,"generate",1,"USER_REPORTED_DONE",
    )
    confirmed_one=record(
        dsn,user,instance,second_conversation,start.run_id,
        report_one.version,"generate",2,"USER_CONFIRMED",
    )
    report_two=record(
        dsn,user,instance,second_conversation,start.run_id,
        confirmed_one.version,"check",1,"USER_REPORTED_DONE",
    )
    confirmed_two=record(
        dsn,user,instance,second_conversation,start.run_id,
        report_two.version,"check",2,"USER_CONFIRMED",
    )
    finished=run_status(
        dsn,user,instance,second_conversation,start.run_id,
        confirmed_two.version,"FINISH",
    )
    assert finished.action=="RUN_USER_CONFIRMED"
    original=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert original.status=="USER_CONFIRMED" and original.next_step_id is None

    success=evaluation_command(
        dsn,user,instance,second_conversation,start.run_id,
        finished.version,outcome="USER_REPORTED_SUCCESS",
        step_events=(confirmed_one.step_event_id,confirmed_two.step_event_id),
        criteria=((1,"USER_REPORTED_MET"),),
    )
    assessment=call(dsn,evaluate_procedure_run,actor(user),success)
    assert assessment.action=="RUN_EVALUATED"
    assert call(dsn,evaluate_procedure_run,actor(user),success).replayed

    current={t.task_id:t for t in call(
        dsn,list_tasks,actor(user),instance,goal,
    )}
    assert current[initial_task].status=="PLANNED"
    assert not current[dependent].actionable  # method success != task confirmation

    reported=report_done(dsn,user,instance,second_conversation,initial_task,1)
    confirmed=confirm_done(
        dsn,user,instance,second_conversation,
        initial_task,2,reported.report_id,
    )
    assert {t.task_id:t for t in call(
        dsn,list_tasks,actor(user),instance,goal,
    )}[dependent].actionable

    corrected=call(
        dsn,correct_task_outcome,actor(user),
        task_correction(
            dsn,user,instance,second_conversation,initial_task,3,
            confirmation_id=confirmed.confirmation_id,
        ),
    )
    assert corrected.action=="OUTCOME_CORRECTED"
    while_corrected={t.task_id:t for t in call(
        dsn,list_tasks,actor(user),instance,goal,
    )}
    assert not while_corrected[dependent].actionable
    assert while_corrected[initial_task].status=="NEEDS_REVIEW"
    assert call(dsn,read_procedure_run,actor(user),instance,
                start.run_id).status=="USER_CONFIRMED"  # independent ownership

    updated=evaluation_command(
        dsn,user,instance,second_conversation,start.run_id,
        finished.version,outcome="USER_REPORTED_FAILURE",cause="ENVIRONMENT",
        prior=success.evaluation_id,criteria=((1,"USER_REPORTED_UNMET"),),
    )
    reassessed=call(dsn,evaluate_procedure_run,actor(user),updated)
    assert reassessed.revision==2
    history=call(dsn,read_procedure_evaluations,actor(user),instance,start.run_id)
    assert [x.evaluation_revision for x in history]==[2,1]
    assert history[0].supersedes_evaluation_id==success.evaluation_id
    assert not history[0].independently_verified

    proposal=proposal_command(
        dsn,user,instance,second_conversation,updated.evaluation_id,variant,1,
        params={"hostname":"localhost","days":60},
    )
    proposed=call(dsn,propose_procedure_adaptation,actor(user),proposal)
    assert proposed.action=="ADAPTATION_PROPOSED"
    assert call(dsn,read_procedure_adaptation,actor(user),instance,
                proposal.proposal_id).status=="PROPOSED"
    assert call(dsn,read_personalized_approach,actor(user),instance,variant).revision==1
    decision=decide_command(
        dsn,user,instance,second_conversation,proposal.proposal_id,1,"APPLY",
    )
    applied=call(dsn,decide_procedure_adaptation,actor(user),decision)
    assert applied.revision==2 and applied.action=="ADAPTATION_APPLIED"
    assert call(dsn,decide_procedure_adaptation,actor(user),decision).replayed

    old=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert old.variant_revision==1 and old.steps==original.steps
    assert call(dsn,read_personalized_approach,actor(user),instance,variant,1
                ).parameters["days"]==30
    assert call(dsn,read_personalized_approach,actor(user),instance,variant
                ).parameters["days"]==60
    next_start=start_command(
        dsn,user,instance,second_conversation,template,
        variant=variant,revision=2,
    )
    call(dsn,start_procedure_run,actor(user),next_start)
    newer=call(dsn,read_procedure_run,actor(user),instance,next_start.run_id)
    assert newer.variant_revision==2 and "60 days" in newer.steps[0].step.instruction

    personal=call(
        dsn,summarize_personal_procedure_experience,
        actor(user),instance,template,variant_id=variant,
    )
    assert personal.observed_run_count==2
    assert personal.reported_failure_count==1
    assert personal.reported_success_count==0
    assert personal.unevaluated_run_count==1
    assert not personal.independently_verified_effectiveness
    goals=call(dsn,list_goals,actor(user),instance)
    root=next(x for x in goals if x.goal_id==goal)
    assert root.selected_strategy_id==strategy and root.status=="ACTIVE"
    assert root.desired_outcome=="A test Nginx endpoint configured with working local TLS"
    assert call(dsn,read_shared_procedure,actor(user),instance,template
                ).version==1
    assert_exact_event_outbox(
        dsn,user,instance,
        [started.event_id,confirmed_one.event_id,finished.event_id,
         assessment.event_id,reassessed.event_id,proposed.event_id,
         applied.event_id,corrected.event_id],
    )


def test_dependency_correction_revokes_queued_agent_and_new_authorization_recovers(dsn):
    """Task correction invalidates a P0 grant; a fresh approval can later run."""
    user,persona,instance=seed(dsn,grant=True)
    conversation=uuid4()
    configured_pool(dsn)
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),publication(
        dsn,user,persona,instance,conversation,template,0,agent_method(),
    ))
    # The Goal and AGENT task use the real Task owner rather than a fake
    # procedure-run task marker.
    from test_phase3_tasks import make_goal
    goal=make_goal(dsn,user,instance,conversation)
    agent_task,_,_=make_task(
        dsn,user,instance,conversation,goal,1,
        executor="AGENT",capability=SLOW_TOOL,title="Analyze local text",
    )
    start=start_command(dsn,user,instance,conversation,template,task=agent_task)
    call(dsn,start_procedure_run,actor(user),start)

    prerequisite,_,_=make_task(
        dsn,user,instance,conversation,goal,2,title="Approve input sample",
    )
    edge(dsn,user,instance,conversation,agent_task,prerequisite,3)
    blocked_auth=authorization(
        dsn,user,instance,conversation,start.run_id,task_version=2,
    )
    with pytest.raises(VersionConflict,match="dependency"):
        call(dsn,schedule_agent_step,actor(user),blocked_auth)
    with psycopg.connect(dsn,autocommit=True) as connection:
        assert connection.execute(
            """SELECT count(*) FROM procedure_agent_operation_authorizations
               WHERE operation_id=%s""",(blocked_auth.operation_id,),
        ).fetchone()[0]==0

    first_report=report_done(dsn,user,instance,conversation,prerequisite,1)
    confirmed=confirm_done(
        dsn,user,instance,conversation,prerequisite,2,first_report.report_id,
    )
    enabled=authorization(
        dsn,user,instance,conversation,start.run_id,task_version=2,
    )
    accepted=call(dsn,schedule_agent_step,actor(user),enabled)
    assert accepted.status.value=="QUEUED"
    assert call(dsn,schedule_agent_step,actor(user),enabled).replayed

    invalid=call(
        dsn,correct_task_outcome,actor(user),
        task_correction(
            dsn,user,instance,conversation,prerequisite,3,
            confirmation_id=confirmed.confirmation_id,
        ),
    )
    assert invalid.action=="OUTCOME_CORRECTED"
    # Registered P0 worker cannot claim this queued work after a correction.
    assert call(
        dsn,claim_operation,worker_id="phase3-revoked",
        capabilities=(SLOW_TOOL,),
        operation_types=(PROCEDURE_AGENT_OPERATION,),
    ) is None
    queued=operation_row(dsn,enabled.operation_id)
    assert queued[0]=="QUEUED"
    cancelled=call(dsn,cancel_operation,actor(user),CancelOperationCommand(
        operation_id=enabled.operation_id,expected_version=queued[5],
        idempotency_key=uuid4().hex,
    ))
    assert cancelled.value=="CANCELLED"
    stale=call(dsn,reconcile_agent_step,platform(user),enabled.operation_id)
    assert stale.resolution=="INVALIDATED"
    assert call(dsn,read_procedure_run,actor(user),instance,
                start.run_id).steps[0].status=="NOT_STARTED"

    another_report=report_done(dsn,user,instance,conversation,prerequisite,4)
    restored=confirm_done(
        dsn,user,instance,conversation,prerequisite,5,another_report.report_id,
    )
    assert restored.confirmation_id!=confirmed.confirmation_id
    fresh=authorization(
        dsn,user,instance,conversation,start.run_id,task_version=2,
    )
    call(dsn,schedule_agent_step,actor(user),fresh)
    lease=call(dsn,run_one_agent_step,worker_id="phase3-registered-executor")
    assert lease is not None and lease.operation_id==fresh.operation_id
    verified=call(dsn,reconcile_agent_step,platform(user),fresh.operation_id)
    assert verified.resolution=="VERIFIED"
    result=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert result.steps[0].status=="VERIFIED" and result.version==2
    assert call(dsn,list_tasks,actor(user),instance,goal)[0].status in (
        "PLANNED","USER_CONFIRMED",
    )
    task_views={t.task_id:t for t in call(dsn,list_tasks,actor(user),instance,goal)}
    assert task_views[agent_task].status=="PLANNED"
    assert not task_views[agent_task].effective_completion
    finished=run_status(
        dsn,user,instance,conversation,start.run_id,result.version,"FINISH",
    )
    assert finished.action=="RUN_USER_CONFIRMED"
    reported_success=evaluation_command(
        dsn,user,instance,conversation,start.run_id,finished.version,
        outcome="USER_REPORTED_SUCCESS",step_events=(verified.step_event_id,),
        criteria=((1,"USER_REPORTED_MET"),),
    )
    assessed=call(dsn,evaluate_procedure_run,actor(user),reported_success)
    assert assessed.action=="RUN_EVALUATED"
    assert {t.task_id:t for t in call(
        dsn,list_tasks,actor(user),instance,goal,
    )}[agent_task].status=="PLANNED"
    with psycopg.connect(dsn,autocommit=True) as connection:
        evidence=connection.execute(
            """SELECT ev.trust_level FROM procedure_run_step_events st
               JOIN events ev ON ev.event_id=st.event_id
               WHERE st.step_event_id=%s""",
            (verified.step_event_id,),
        ).fetchone()
        assert evidence==("TRUSTED_PLATFORM",)
    assert_exact_event_outbox(
        dsn,user,instance,[verified.step_event_id] if False else
        [invalid.event_id,assessed.event_id],
    )


def test_stale_proposal_after_step_correction_never_mutates_private_revision(dsn):
    """Step correction invalidates an assessment's evidence and APPLY gate."""
    user,persona,instance=seed(dsn,grant=True)
    conversation=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),publication(
        dsn,user,persona,instance,conversation,template,0,
        definition("ADAPTIVE"),
    ))
    private=variant_command(
        dsn,user,instance,conversation,template,1,
        params={"hostname":"localhost","days":30},
    )
    call(dsn,create_personalized_approach,actor(user),private)
    start=start_command(
        dsn,user,instance,conversation,template,
        variant=private.variant_id,revision=1,
    )
    call(dsn,start_procedure_run,actor(user),start)
    reported=record(
        dsn,user,instance,conversation,start.run_id,1,"generate",1,"FAILED",
        detail="The configured hostname was not accepted by my local test client",
    )
    observed=evaluation_command(
        dsn,user,instance,conversation,start.run_id,reported.version,
        outcome="USER_REPORTED_FAILURE",cause="INCORRECT_INPUT",
        step_events=(reported.step_event_id,),
    )
    call(dsn,evaluate_procedure_run,actor(user),observed)
    candidate=proposal_command(
        dsn,user,instance,conversation,observed.evaluation_id,
        private.variant_id,1,params={"hostname":"localhost","days":60},
        overrides={"generate":"Use the permitted alternate TLS setup for {hostname}"},
    )
    call(dsn,propose_procedure_adaptation,actor(user),candidate)
    assert call(dsn,read_procedure_adaptation,actor(user),instance,
                candidate.proposal_id).status=="PROPOSED"

    corrected=call(
        dsn,record_procedure_step,actor(user),
        step_correction(
            dsn,user,instance,conversation,start.run_id,reported.version,
            "generate",2,reported.step_event_id,
        ),
    )
    assert corrected.action=="STEP_CORRECTED"
    stale=call(dsn,read_procedure_adaptation,actor(user),instance,
               candidate.proposal_id)
    assert stale.status=="STALE"
    decision=decide_command(
        dsn,user,instance,conversation,candidate.proposal_id,1,"APPLY",
    )
    with pytest.raises(VersionConflict,match="evidence changed"):
        call(dsn,decide_procedure_adaptation,actor(user),decision)
    assert call(dsn,read_personalized_approach,actor(user),instance,
                private.variant_id).revision==1
    rejected=decide_command(
        dsn,user,instance,conversation,candidate.proposal_id,1,"REJECT",
    )
    declined=call(dsn,decide_procedure_adaptation,actor(user),rejected)
    assert declined.action=="ADAPTATION_REJECTED"
    assert call(dsn,read_procedure_adaptation,actor(user),instance,
                candidate.proposal_id).status=="REJECTED"
    assert call(dsn,read_procedure_run,actor(user),instance,start.run_id
                ).steps[0].status=="NEEDS_REVIEW"


def test_reselection_recovers_durable_run_and_rejects_old_generation(dsn):
    """Persona changes cannot rewrite other selected persona's in-flight state."""
    user,persona,instance=seed(dsn,grant=True)
    conversation=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),publication(
        dsn,user,persona,instance,conversation,template,0,
        definition("STABLE"),
    ))
    start=start_command(dsn,user,instance,conversation,template)
    call(dsn,start_procedure_run,actor(user),start)
    step=record(
        dsn,user,instance,conversation,start.run_id,1,"generate",1,"ATTEMPTED",
    )
    original=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert original.version==step.version

    # Provision a second instance for the same user, then switch via lifecycle.
    alternate_persona,alternate_instance=uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as connection:
        with connection.transaction():
            connection.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES(%s,%s)",
                (alternate_persona,"phase3-06-"+alternate_persona.hex),
            )
            connection.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES(%s,%s,%s,'Second persona')""",
                (alternate_instance,user,alternate_persona),
            )
    switched=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=alternate_instance,
        expected_generation=1,idempotency_key=uuid4().hex,
    ))
    assert switched.generation==2
    with pytest.raises(StaleSelection):
        call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    with pytest.raises(StaleSelection):
        call(dsn,list_procedure_runs,actor(user),instance)

    returned=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=alternate_instance,target_instance_id=instance,
        expected_generation=2,idempotency_key=uuid4().hex,
    ))
    assert returned.generation==3
    recovered=call(dsn,read_procedure_run,actor(user),instance,start.run_id)
    assert recovered.version==original.version
    assert recovered.steps==original.steps and recovered.next_step_id=="generate"
    old_turn=turn(dsn,user,instance,conversation,"This was already a prior attempt.")
    stale=RecordProcedureStepCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conversation,
        evidence_turn_id=old_turn.turn_id,
        evidence_quote="This was already a prior attempt",
        run_id=start.run_id,expected_run_version=recovered.version,
        step_id="check",expected_step_version=1,outcome="ATTEMPTED",
    )
    with pytest.raises(StaleSelection):
        call(dsn,record_procedure_step,actor(user),stale)
    assert call(dsn,read_procedure_run,actor(user),instance,start.run_id
                ).steps==original.steps

    outsider,_,outside_instance=seed(dsn)
    with pytest.raises(PermissionDenied):
        call(dsn,read_procedure_run,actor(outsider),outside_instance,start.run_id)
