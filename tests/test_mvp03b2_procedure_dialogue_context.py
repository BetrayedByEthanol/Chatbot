"""MVP-03B.2 real PostgreSQL contextual ProcedureRun continuity tests."""
from uuid import uuid4
import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.conversation import (
    acknowledge_text_delivery,prepare_text_context,respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply
from persona_agent.dialogue_contracts import OpenThreadCommand,OfferDialogueActCommand
from persona_agent.dialogue_state import open_thread,offer_dialogue_act
from persona_agent.procedure_run_contracts import LinkProcedureThreadCommand
from persona_agent.procedure_run_state import (
    link_procedure_thread,read_procedure_run,start_procedure_run,
)
from persona_agent.procedure_dialogue_context import procedure_dialogue_context_block
from test_phase3_runs import published,start_command,record,run_status
from test_phase3_procedures import actor,call,seed,turn


def setup_run(dsn):
    user,persona,instance,conversation,template=published(dsn)
    start=start_command(dsn,user,instance,conversation,template)
    call(dsn,start_procedure_run,actor(user),start)
    return user,instance,conversation,start.run_id,template


def link_focus(dsn,user,instance,conversation,run_id,*,version=1):
    source=turn(dsn,user,instance,conversation,
                "Talk about my Nginx TLS setup progress.")
    with psycopg.connect(dsn,autocommit=True) as conn:
        rev=conn.execute(
            "SELECT revision FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]
    thread=uuid4()
    call(dsn,open_thread,actor(user),OpenThreadCommand(
        instance_id=instance,conversation_id=conversation,
        expected_generation=1,expected_revision=rev,
        evidence_turn_id=source.turn_id,idempotency_key=uuid4().hex,
        thread_id=thread,title="Nginx TLS setup",
    ))
    call(dsn,link_procedure_thread,actor(user),LinkProcedureThreadCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conversation,evidence_turn_id=source.turn_id,
        evidence_quote="Nginx TLS setup progress",
        run_id=run_id,expected_run_version=version,thread_id=thread,
    ))
    return thread,source


def context_for(dsn,user,instance,conversation,text,*,budget=12000):
    source=turn(dsn,user,instance,conversation,text)
    with psycopg.connect(dsn,autocommit=True) as conn:
        snapshot=prepare_text_context(
            conn,actor(user),source.turn_id,max_context_chars=budget,
        )
    return snapshot.messages[0].content,snapshot


def test_focused_link_recovers_unfinished_pinned_step(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    link_focus(dsn,user,instance,conv,run_id)
    prompt,context=context_for(dsn,user,instance,conv,"What should I do next?")
    assert "Tracked procedure (pinned)" in prompt
    assert str(run_id) in prompt
    assert '"step_id":"generate"' in prompt
    assert "Generate the certificate" in prompt
    assert "not an execution grant" in prompt
    assert context.messages[-1].content=="What should I do next?"


def test_unrelated_question_does_not_pull_focused_run(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    link_focus(dsn,user,instance,conv,run_id)
    for question in (
        "Tell me about weekend plans.",
        "What is the next Nginx version?",
        "Continue with weekend travel plans.",
    ):
        prompt,_=context_for(dsn,user,instance,conv,question)
        assert "Tracked procedure (pinned)" not in prompt


def test_user_report_not_independent_verification(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    link_focus(dsn,user,instance,conv,run_id)
    record(dsn,user,instance,conv,run_id,2,"generate",1,"USER_REPORTED_DONE")
    prompt,_=context_for(dsn,user,instance,conv,"What is my next step?")
    assert '"status":"USER_REPORTED_DONE"' in prompt
    assert '"step_id":"generate"' in prompt
    assert call(dsn,read_procedure_run,actor(user),instance,run_id).steps[0].status==(
        "USER_REPORTED_DONE"
    )


def test_paused_run_stays_paused_after_context_read(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    link_focus(dsn,user,instance,conv,run_id)
    run_status(dsn,user,instance,conv,run_id,2,"PAUSE")
    prompt,_=context_for(dsn,user,instance,conv,"Can we continue this procedure?")
    assert '"status":"PAUSED"' in prompt
    assert "must not be resumed implicitly" in prompt
    assert call(dsn,read_procedure_run,actor(user),instance,run_id).status=="PAUSED"


def test_fresh_conversation_can_recall_unique_named_run(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    prompt,context=context_for(
        dsn,user,instance,uuid4(),"Can we resume the Nginx TLS setup?",
    )
    assert "Tracked procedure (pinned)" in prompt
    assert str(run_id) in prompt
    assert context.messages[-1].content=="Can we resume the Nginx TLS setup?"


def test_same_topic_multiple_runs_require_disambiguation(dsn):
    user,instance,conv,first,template=setup_run(dsn)
    second=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),second)
    prompt,_=context_for(
        dsn,user,instance,uuid4(),"Can we resume the Nginx TLS setup?",
    )
    assert "Tracked procedure (pinned)" not in prompt
    assert str(first) not in prompt
    assert str(second.run_id) not in prompt


def test_generic_resume_requires_link_or_explicit_method_reference(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    prompt,_=context_for(dsn,user,instance,uuid4(),"Where were we?")
    assert "Tracked procedure (pinned)" not in prompt
    prompt,_=context_for(dsn,user,instance,uuid4(),"Resume my procedure.")
    assert "Tracked procedure (pinned)" in prompt
    assert str(run_id) in prompt


def test_other_user_and_cancelled_run_are_excluded(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    outsider,_,other=seed(dsn)
    prompt,_=context_for(
        dsn,outsider,other,uuid4(),"Resume the Nginx TLS setup.",
    )
    assert "Tracked procedure (pinned)" not in prompt
    run_status(dsn,user,instance,conv,run_id,1,"CANCEL")
    prompt,_=context_for(
        dsn,user,instance,uuid4(),"Resume the Nginx TLS setup.",
    )
    assert "Tracked procedure (pinned)" not in prompt


def test_prior_linked_delivered_question_survives_session_gap(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    thread,source=link_focus(dsn,user,instance,conv,run_id)
    class Fixed:
        def generate(self,messages):
            return ModelReply(
                model_id="procedure-continuity-test",
                text="Please tell me which certificate hostname to use.",
            )
    reply=call(dsn,respond_to_text,actor(user),source.turn_id,Fixed())
    call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        rev=conn.execute(
            "SELECT revision FROM conversations WHERE conversation_id=%s",
            (conv,),
        ).fetchone()[0]
    call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
        instance_id=instance,conversation_id=conv,
        expected_generation=1,expected_revision=rev,
        evidence_turn_id=source.turn_id,idempotency_key=uuid4().hex,
        thread_id=thread,act_id=uuid4(),source_response_id=reply.response_id,
        kind="QUESTION",label="Which certificate hostname should we use?",
    ))
    prompt,_=context_for(
        dsn,user,instance,uuid4(),"Can we resume the Nginx TLS setup?",
    )
    assert "Pending prior dialogue act (not approval)" in prompt
    assert "Which certificate hostname should we use?" in prompt


def test_prompt_budget_preserves_user_turn(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    link_focus(dsn,user,instance,conv,run_id)
    _,context=context_for(
        dsn,user,instance,conv,"What should I do next?",budget=1024,
    )
    assert context.messages[-1].content=="What should I do next?"
    assert sum(len(m.content) for m in context.messages)<=1024


def test_direct_view_is_read_only_and_bounded(dsn):
    user,instance,conv,run_id,_=setup_run(dsn)
    thread,_=link_focus(dsn,user,instance,conv,run_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        before=conn.execute(
            "SELECT count(*) FROM events WHERE user_id=%s",(user,),
        ).fetchone()[0]
        from psycopg.rows import dict_row
        with conn.cursor(row_factory=dict_row) as cur:
            block=procedure_dialogue_context_block(
                cur,user_id=user,instance_id=instance,
                conversation_id=conv,focus_thread_id=thread,
                latest_text="What should I do next?",max_chars=730,
            )
        after=conn.execute(
            "SELECT count(*) FROM events WHERE user_id=%s",(user,),
        ).fetchone()[0]
    assert before==after
    assert len(block)<=730
    assert "Tracked procedure (pinned)" in block
