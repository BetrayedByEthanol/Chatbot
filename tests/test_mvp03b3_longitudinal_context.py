"""MVP-03B.3: longitudinal v2.5.1 Context Builder quality through PostgreSQL.

Ground truth is the owner-approved Goal/Procedure/Dialogue fixture. The
observed prediction is tagged context supplied to the model, not its reply.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.conversation import (
    acknowledge_text_delivery,prepare_text_context,respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply
from persona_agent.goal_contracts import SetGoalStatusCommand
from persona_agent.goal_state import set_goal_status
from persona_agent.procedure_run_state import (
    correct_procedure_step,read_procedure_run,start_procedure_run,
)
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.acceptance_host import seed_local_demo

import test_phase3_goals as goals
from test_phase3_procedures import actor,call,turn,seed
from test_phase3_runs import (
    published,start_command,record,run_status,correction,
)
from test_mvp03b2_procedure_dialogue_context import (
    link_focus,_prior_pending,
)
from context_quality_metrics import (
    ContextCase,ContextSelections,extract_context_selections,
    score_context_cases,
)


class FixedModel:
    """Persist authentic delivered replies without evaluating model prose."""
    def generate(self,messages):
        return ModelReply(
            model_id="mvp03b3.context-observer",
            text="I can help continue this discussion.",
        )


MODEL=FixedModel()


def deliver(dsn,user,turn_id):
    reply=call(dsn,respond_to_text,actor(user),turn_id,MODEL)
    call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    return reply


def observe(dsn,user,instance,conversation,text,*,budget=12000,finish=True):
    source=turn(dsn,user,instance,conversation,text)
    started=perf_counter()
    ctx=goals.call(
        dsn,prepare_text_context,actor(user),source.turn_id,
        max_context_chars=budget,
    )
    elapsed=(perf_counter()-started)*1000
    assert ctx.messages[0].role=="system"
    extracted=extract_context_selections(ctx.messages[0].content)
    result=(ctx,extracted,elapsed)
    if finish:
        deliver(dsn,user,source.turn_id)
    return result


def case(dsn,user,instance,conv,name,text,*,goals_expected=(),
         runs_expected=(),pending_expected=(),budget=12000):
    context,selected,elapsed=observe(
        dsn,user,instance,conv,text,budget=budget,
    )
    assert context.messages[-1].role=="user"
    return ContextCase(
        name=name,
        expected=ContextSelections(
            goals=frozenset(map(str,goals_expected)),
            runs=frozenset(map(str,runs_expected)),
            pending=frozenset(pending_expected),
        ),
        selected=selected,elapsed_ms=elapsed,budget_chars=budget,
        total_chars=sum(len(x.content) for x in context.messages),
        latest_preserved=context.messages[-1].content==text,
    )


def setup_mixed_persona(dsn):
    user,_,instance,old,template=published(dsn)
    source=turn(dsn,user,instance,old,"I want to improve my eating habits.")
    goal,_,_=goals.root(dsn,user,instance,old,source)
    run_command=start_command(dsn,user,instance,old,template)
    call(dsn,start_procedure_run,actor(user),run_command)
    thread,_=link_focus(dsn,user,instance,old,run_command.run_id)
    return user,instance,old,goal,run_command.run_id,thread


def test_three_session_mixed_context_precision_and_omission(dsn):
    user,instance,old,gid,run_id,_=setup_mixed_persona(dsn)

    # Model-visible delivered-turn history, not direct SQL state injection.
    # Sequence exceeds the 12-turn immediate history window.
    for index in range(18):
        conversation_turn=turn(
            dsn,user,instance,old,
            f"Unrelated weekend logistics item {index}: packing a backpack.",
        )
        deliver(dsn,user,conversation_turn.turn_id)
    middle=uuid4()
    distraction=case(
        dsn,user,instance,middle,"interruption",
        "Continue with weekend travel plans.",
    )
    returning=uuid4()
    cases=[
        distraction,
        case(dsn,user,instance,returning,"named-goal",
             "How is my eating goal progressing?",goals_expected=[gid]),
        case(dsn,user,instance,returning,"named-procedure",
             "Can we resume the Nginx TLS setup?",runs_expected=[run_id]),
        case(dsn,user,instance,returning,"unrelated-topic",
             "Tell me about weekend travel plans."),
        case(dsn,user,instance,returning,"generic-approved-goal",
             "What is my approved goal?",goals_expected=[gid]),
        case(dsn,user,instance,returning,"ambiguous-resume",
             "Where were we?"),
        case(dsn,user,instance,returning,"generic-explicit-procedure",
             "Resume my procedure.",runs_expected=[run_id]),
        case(dsn,user,instance,returning,"unrelated-same-keyword",
             "Continue with the Nginx release notes."),
    ]
    result=score_context_cases(cases)
    assert result["cases"]==8
    assert result["positive_cases"]==4
    assert result["negative_cases"]==4
    assert result["precision"]==1.0, result
    assert result["recall"]==1.0, result
    assert result["clean_negative_rate"]==1.0, result
    assert result["injections"]==result["omissions"]==0, result
    assert result["all_within_budget"] and result["all_latest_preserved"]
    assert all(item["elapsed_ms"]>=0 for item in result["case_details"])

    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE goal_id=%s",
            (gid,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM procedure_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s "
            "AND instance_id=%s",(user,instance),
        ).fetchone()[0]==0


def test_progress_correction_and_inactive_state_across_conversations(dsn):
    user,instance,old,gid,run_id,_=setup_mixed_persona(dsn)
    submitted=record(
        dsn,user,instance,old,run_id,2,"generate",1,"USER_REPORTED_DONE",
    )
    resumed=uuid4()
    context,selected,_=observe(
        dsn,user,instance,resumed,"Can we resume the Nginx TLS setup?",
    )
    assert str(run_id) in selected.runs
    assert '"status":"USER_REPORTED_DONE"' in context.messages[0].content
    assert "not verified real-world" in context.messages[0].content

    # Correction has its own owner-reviewed source, not a model inference.
    command=correction(
        dsn,user,instance,old,run_id,submitted.version,
        "generate",2,submitted.step_event_id,
    )
    call(dsn,correct_procedure_step,actor(user),command)
    corrected=uuid4()
    context,selected,_=observe(
        dsn,user,instance,corrected,"Can we resume the Nginx TLS setup?",
    )
    assert str(run_id) in selected.runs
    assert '"status":"NEEDS_REVIEW"' in context.messages[0].content
    assert call(dsn,read_procedure_run,actor(user),instance,run_id).status==(
        "NEEDS_REVIEW"
    )

    # Explicit user-sourced Goal pause excludes it from ACTIVE context.
    src=goals.turn(dsn,user,instance,old,"Pause my eating goal for now.")
    command=SetGoalStatusCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=old,
        evidence_turn_id=src.turn_id,evidence_quote="Pause my eating goal",
        goal_id=gid,expected_goal_version=1,new_status="PAUSED",
    )
    goals.call(
        dsn,set_goal_status,goals.actor(user),
        goals.authorize(dsn,user,instance,command),
    )
    no_goal=case(
        dsn,user,instance,uuid4(),"paused-goal",
        "How is my eating goal progressing?",
    )
    assert no_goal.selected.goals==frozenset()

    run_status(dsn,user,instance,old,run_id,4,"CANCEL")
    no_run=case(
        dsn,user,instance,uuid4(),"cancelled-run",
        "Can we resume the Nginx TLS setup?",
    )
    assert no_run.selected.runs==frozenset()
    result=score_context_cases([no_goal,no_run])
    assert result["clean_negative_rate"]==1.0
    assert result["injections"]==0


def test_pending_question_survives_unrelated_session_and_new_link(dsn):
    user,instance,old,run_id,_=_prior_pending(dsn)
    other=uuid4()
    case(dsn,user,instance,other,"interruption","Plan weekend travel.")
    returned=uuid4()
    link_focus(dsn,user,instance,returned,run_id,version=2)
    message=case(
        dsn,user,instance,returned,"pending-and-step",
        "What should I do next?",
        runs_expected=[run_id],
        pending_expected=["Which certificate hostname should we use?"],
    )
    idle=case(
        dsn,user,instance,returned,"unrelated-while-linked",
        "Continue with weekend travel plans.",
    )
    report=score_context_cases([message,idle])
    assert report["precision"]==report["recall"]==1.0, report
    assert report["clean_negative_rate"]==1.0
    assert report["injections"]==report["omissions"]==0


def test_other_user_and_tight_budget_do_not_leak_or_drop_latest(dsn):
    user,instance,old,gid,run_id,_=setup_mixed_persona(dsn)
    outsider,_,other_instance=seed(dsn)
    foreign=case(
        dsn,outsider,other_instance,uuid4(),"foreign-user",
        "Can we resume the Nginx TLS setup?",
    )
    assert foreign.selected==ContextSelections()
    short=case(
        dsn,user,instance,old,"short-context",
        "How is my eating goal progressing?",budget=1024,
    )
    assert short.latest_preserved and short.total_chars<=1024
    # Budget-related omission is not counted as retrieval failure:
    # context selection is optional under this much smaller budget.
    assert short.selected.runs==frozenset()
    assert score_context_cases([foreign])["clean_negative_rate"]==1.0


def test_three_session_source_linked_memory_context_is_still_available(dsn):
    """Historical recall stays separate from approved Goal/Procedure truth."""
    class InspectModel:
        def __init__(self):
            self.messages=[]
        def generate(self,messages):
            self.messages.append(tuple(messages))
            return ModelReply(
                model_id="mvp03b3.historical-observer",
                text="I can review the previous discussion with you.",
            )
    model=InspectModel()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("historical")
    source=host.send(
        "I've been thinking about walking more regularly.",
        idempotency_key="walk-original",
    )
    host.begin_session("interruption")
    host.send(
        "Can we discuss weekend photography instead?",
        idempotency_key="photo-interruption",
    )
    host.begin_session("return")
    host.restart_host()
    host.send(
        "Do you remember what we discussed about walking?",
        idempotency_key="walk-recall",
    )
    system=model.messages[-1][0].content
    assert source.turn_id in system
    assert "I've been thinking about walking more regularly." in system
    assert model.messages[-1][-1].content==(
        "Do you remember what we discussed about walking?"
    )
    assert "Prior-session user statements" in system
    assert "Approved/delegated Goal state" not in system
    assert "Tracked procedure (pinned)" not in system
