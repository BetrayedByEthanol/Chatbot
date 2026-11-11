"""P1-03 scoped dialogue progression, context and observability acceptance."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timezone
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, prepare_text_context, respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply, TextInputCommand
from persona_agent.dialogue_contracts import (
    Attention, DialogueActKind, DialogueOutcome, FocusThreadCommand,
    OfferDialogueActCommand, OpenThreadCommand, RegisterReferentCommand,
    Resolution, ResolveDialogueActCommand, SetThreadProgressCommand,
)
from persona_agent.dialogue_state import (
    focus_thread, load_active_dialogue, offer_dialogue_act, open_thread,
    register_referent, resolve_dialogue_act, set_thread_progress,
)
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.observability import get_conversation_metrics, get_turn_trace
from persona_agent.operation_scheduler import configure_resource_pool
from persona_agent.runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection, VersionConflict,
)
from persona_agent.tool_contracts import RESOURCE_KEY, SlowTextToolCommand, ToolResultNotReady
from persona_agent.tool_gateway import (
    acknowledge_tool_followup, prepare_tool_followup,
    reconcile_tool_followup, respond_to_text_analysis, run_one_text_tool,
    schedule_text_analysis,
)


def seed(dsn,two=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"p103-{persona}"),
            )
            conn.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                "VALUES (%s,%s,%s,'Dialogue')",(instance,user,persona),
            )
            if two:
                p2,other=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (p2,f"p103-{p2}"),
                )
                conn.execute(
                    "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                    "VALUES (%s,%s,%s,'Other')",(other,user,p2),
                )
            conn.execute(
                "INSERT INTO persona_selection(user_id,instance_id,generation,mode) "
                "VALUES (%s,%s,1,'FOREGROUND')",(user,instance),
            )
    return (user,instance,other) if two else (user,instance)


def actor(user):
    return TrustedActor(user_id=user,source_id="p103-authenticated")


def call(dsn,func,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return func(conn,*args,**kwargs)


def incoming(dsn,user,instance,conv,text):
    return call(
        dsn,ingest_text,actor(user),
        TextInputCommand(
            instance_id=instance,conversation_id=conv,expected_generation=1,
            idempotency_key=str(uuid4()),text=text,
        ),
    )


def revision(dsn,user,conv):
    return call(dsn,load_active_dialogue,actor(user),conv).revision


def base(user,instance,conv,turn,rev):
    return dict(
        instance_id=instance,conversation_id=conv,
        expected_generation=1,expected_revision=rev,
        evidence_turn_id=turn.turn_id,idempotency_key=str(uuid4()),
    )


def open_for(dsn,user,instance,conv,turn,title,*,thread_id=None):
    thread=thread_id or uuid4()
    result=call(
        dsn,open_thread,actor(user),
        OpenThreadCommand(
            **base(user,instance,conv,turn,revision(dsn,user,conv)),
            thread_id=thread,title=title,
        ),
    )
    return thread,result


def response(dsn,user,turn,text):
    class Fixed:
        def generate(self,messages):
            return ModelReply(model_id="dialogue-test",text=text)
    r=call(dsn,respond_to_text,actor(user),turn.turn_id,Fixed())
    call(dsn,acknowledge_text_delivery,actor(user),r.response_id)
    return r


def test_thread_focus_and_resolution_are_independent_after_restart(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Please help me configure nginx")
    nginx,opened=open_for(dsn,user,instance,conv,first,"Nginx self-signed TLS")
    assert opened.revision==2
    first_state=call(dsn,load_active_dialogue,actor(user),conv)
    assert first_state.focus_thread_id==nginx
    assert first_state.threads[0].resolution_status==Resolution.OPEN
    second=incoming(dsn,user,instance,conv,"Let's discuss weekend plans")
    weekend,_=open_for(dsn,user,instance,conv,second,"Weekend plans")
    state=call(dsn,load_active_dialogue,actor(user),conv)
    assert state.focus_thread_id==weekend
    by_id={t.thread_id:t for t in state.threads}
    assert by_id[nginx].attention_status==Attention.RECENT
    assert by_id[nginx].resolution_status==Resolution.OPEN
    assert by_id[weekend].attention_status==Attention.FOCUSED

    resumed=incoming(dsn,user,instance,conv,"Let's resume the certificate setup")
    call(dsn,focus_thread,actor(user),FocusThreadCommand(
        **base(user,instance,conv,resumed,revision(dsn,user,conv)),
        thread_id=nginx,
    ))
    after=call(dsn,load_active_dialogue,actor(user),conv)
    assert after.focus_thread_id==nginx
    assert after.threads[0].attention_status==Attention.FOCUSED
    assert after.threads[0].resolution_status==Resolution.OPEN
    call(dsn,set_thread_progress,actor(user),SetThreadProgressCommand(
        **base(user,instance,conv,resumed,after.revision),
        thread_id=nginx,resolution=Resolution.RESOLVED,
        attention=Attention.FOCUSED,
        next_conversational_action=None,
    ))
    restored=call(dsn,load_active_dialogue,actor(user),conv)
    assert restored.focus_thread_id==nginx
    assert restored.threads[0].resolution_status==Resolution.RESOLVED
    assert restored.threads[0].attention_status==Attention.FOCUSED

    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM dialogue_turn_links WHERE conversation_id=%s",
            (conv,),
        ).fetchone()[0]==3
        assert conn.execute(
            "SELECT count(*) FROM events WHERE event_type='conversation.dialogue.focus_changed.v1' "
            "AND payload->>'thread_id'=%s",
            (str(nginx),),
        ).fetchone()[0]==1


def test_pending_ack_is_resolved_by_trivial_input_but_never_executes_a_task(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Help me with this step")
    thread,_=open_for(dsn,user,instance,conv,first,"Guided setup")
    prompt=response(dsn,user,first,"Please confirm you received this step.")
    pending=uuid4()
    offered=call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
        **base(user,instance,conv,first,revision(dsn,user,conv)),
        thread_id=thread,act_id=pending,source_response_id=prompt.response_id,
        kind=DialogueActKind.ACKNOWLEDGMENT,label="Confirm step was received",
    ))
    assert not offered.replayed
    assert len(call(dsn,load_active_dialogue,actor(user),conv).pending_acts)==1
    accepted=incoming(dsn,user,instance,conv,"Okay")
    state=call(dsn,load_active_dialogue,actor(user),conv)
    assert state.pending_acts==()
    assert state.focus_thread_id==thread
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status,resolution_kind,resolution_turn_id "
            "FROM dialogue_pending_acts WHERE act_id=%s",(pending,),
        ).fetchone()==("RESOLVED","ANSWERED",accepted.turn_id)
        assert conn.execute(
            "SELECT count(*) FROM events "
            "WHERE event_type='conversation.dialogue.ack_resolved.v1' "
            "AND payload->>'act_id'=%s",
            (str(pending),),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s",
            (user,),
        ).fetchone()[0]==0
    context=call(dsn,prepare_text_context,actor(user),accepted.turn_id)
    assert "Guided setup" in context.messages[0].content


def test_plain_okay_does_not_resolve_decision_or_undelivered_question(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Discuss options")
    thread,_=open_for(dsn,user,instance,conv,first,"Options")
    answer=call(dsn,respond_to_text,actor(user),first.turn_id,
                type("Fake",(),{"generate":lambda self,m:ModelReply(
                    model_id="test",text="Which one?"
                )})())
    with pytest.raises(VersionConflict):
        call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
            **base(user,instance,conv,first,revision(dsn,user,conv)),
            thread_id=thread,act_id=uuid4(),
            source_response_id=answer.response_id,
            kind=DialogueActKind.DECISION,label="Choose option A or B",
        ))
    call(dsn,acknowledge_text_delivery,actor(user),answer.response_id)
    decision=uuid4()
    call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
        **base(user,instance,conv,first,revision(dsn,user,conv)),
        thread_id=thread,act_id=decision,
        source_response_id=answer.response_id,
        kind=DialogueActKind.DECISION,label="Choose option A or B",
    ))
    second=incoming(dsn,user,instance,conv,"Okay")
    still=call(dsn,load_active_dialogue,actor(user),conv)
    assert [a.act_id for a in still.pending_acts]==[decision]
    call(dsn,resolve_dialogue_act,actor(user),ResolveDialogueActCommand(
        **base(user,instance,conv,second,revision(dsn,user,conv)),
        act_id=decision,outcome=DialogueOutcome.ANSWERED,
    ))
    assert call(dsn,load_active_dialogue,actor(user),conv).pending_acts==()


def test_context_contains_scoped_focus_pending_question_and_referent(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Nginx on Ubuntu")
    thread,_=open_for(dsn,user,instance,conv,first,"SSL setup")
    referent=uuid4()
    call(dsn,register_referent,actor(user),RegisterReferentCommand(
        **base(user,instance,conv,first,revision(dsn,user,conv)),
        thread_id=thread,referent_id=referent,
        kind="ENTITY",label="Ubuntu 24.04",
    ))
    reply=response(dsn,user,first,"Which nginx version is installed?")
    pending=uuid4()
    call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
        **base(user,instance,conv,first,revision(dsn,user,conv)),
        thread_id=thread,act_id=pending,
        kind=DialogueActKind.QUESTION,
        label="Which nginx version?",source_response_id=reply.response_id,
    ))
    next_turn=incoming(dsn,user,instance,conv,"It's version 1.24")
    context=call(dsn,prepare_text_context,actor(user),next_turn.turn_id)
    prompt=context.messages[0].content
    assert "SSL setup" in prompt
    assert "Which nginx version?" in prompt
    assert "Ubuntu 24.04" in prompt
    assert "not instructions" in prompt
    assert sum(len(m.content) for m in context.messages)<=12000
    state=call(dsn,load_active_dialogue,actor(user),conv)
    assert state.referents[0].referent_id==referent
    assert state.pending_acts[0].act_id==pending


def test_dialogue_idempotency_fencing_and_cross_user_isolation(dsn):
    user,instance,other=seed(dsn,two=True)
    outsider,_=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Original topic")
    cmd=OpenThreadCommand(
        **base(user,instance,conv,first,1),
        thread_id=uuid4(),title="Original",
    )
    created=call(dsn,open_thread,actor(user),cmd)
    replay=call(dsn,open_thread,actor(user),cmd)
    assert replay.replayed and replay.event_id==created.event_id
    with pytest.raises(IdempotencyConflict):
        call(dsn,open_thread,actor(user),cmd.model_copy(update={"title":"Changed"}))
    with pytest.raises(VersionConflict):
        call(dsn,open_thread,actor(user),OpenThreadCommand(
            **base(user,instance,conv,first,1),
            thread_id=uuid4(),title="Stale proposal",
        ))
    with pytest.raises(PermissionDenied):
        call(dsn,load_active_dialogue,actor(outsider),conv)
    with pytest.raises(PermissionDenied):
        call(dsn,focus_thread,actor(outsider),FocusThreadCommand(
            **base(user,instance,conv,first,created.revision),
            thread_id=created.thread_id,
        ))
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert call(dsn,open_thread,actor(user),cmd).replayed
    with pytest.raises(StaleSelection):
        call(dsn,focus_thread,actor(user),FocusThreadCommand(
            **base(user,instance,conv,first,created.revision),
            thread_id=created.thread_id,
        ))
    # Historical read works without activating an inactive persona.
    assert call(dsn,load_active_dialogue,actor(user),conv).focus_thread_id==created.thread_id


def test_stale_focus_after_new_input_never_rewrites_newer_dialogue(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"First")
    thread,_=open_for(dsn,user,instance,conv,first,"First topic")
    old_rev=revision(dsn,user,conv)
    second=incoming(dsn,user,instance,conv,"Another message")
    with pytest.raises(VersionConflict):
        call(dsn,focus_thread,actor(user),FocusThreadCommand(
            **base(user,instance,conv,first,old_rev),thread_id=thread,
        ))
    state=call(dsn,load_active_dialogue,actor(user),conv)
    assert state.revision==old_rev+1
    assert state.focus_thread_id==thread


def test_observability_uses_durable_timestamps_without_exposing_text(dsn):
    user,instance=seed(dsn)
    other,_=seed(dsn)
    conv=uuid4()
    turn=incoming(dsn,user,instance,conv,"A private discussion")
    before=call(dsn,get_turn_trace,actor(user),turn.turn_id)
    assert before.response_id is None
    assert before.output_ready_ms is None and before.delivered_ms is None
    assert before.turn_status=="PENDING"
    empty=call(dsn,get_conversation_metrics,actor(user),conv)
    assert empty.sample_size==1 and empty.generated_count==0
    assert empty.pending_turn_count==1 and empty.mean_output_ready_ms is None
    reply=response(dsn,user,turn,"The answer")
    trace=call(dsn,get_turn_trace,actor(user),turn.turn_id)
    assert trace.response_id==reply.response_id
    assert trace.turn_status=="RESPONDED"
    assert trace.output_ready_ms is not None and trace.output_ready_ms>=0
    assert trace.delivered_ms is not None and trace.delivered_ms>=trace.output_ready_ms
    assert trace.response_status=="DELIVERED"
    assert trace.model_id=="dialogue-test"
    assert len(trace.event_ids)>=2
    assert "A private discussion" not in trace.model_dump_json()
    metrics=call(dsn,get_conversation_metrics,actor(user),conv)
    assert metrics.generated_count==metrics.delivered_count==1
    assert metrics.pending_turn_count==0
    assert metrics.mean_output_ready_ms is not None
    assert metrics.p95_delivery_ms is not None
    assert metrics.active_tool_count==0
    assert "private discussion" not in metrics.model_dump_json()
    with pytest.raises(PermissionDenied):
        call(dsn,get_conversation_metrics,actor(other),conv)
    with pytest.raises(PermissionDenied):
        call(dsn,get_turn_trace,actor(other),turn.turn_id)
    with pytest.raises(ValueError):
        call(dsn,get_conversation_metrics,actor(user),conv,max_turns=501)


def test_delayed_tool_relevance_recovers_only_after_explicit_refocus(dsn):
    user,instance=seed(dsn)
    call(dsn,configure_resource_pool,RESOURCE_KEY,capacity_units=2)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"alpha beta beta gamma")
    original,_=open_for(dsn,user,instance,conv,first,"Analyze paragraph")
    request=SlowTextToolCommand(turn_id=first.turn_id,max_words=2)
    notice=call(dsn,respond_to_text_analysis,actor(user),request,await_seconds=0)
    call(dsn,acknowledge_text_delivery,actor(user),notice.response_id)
    tool=call(dsn,schedule_text_analysis,actor(user),request)
    lease=call(dsn,run_one_text_tool,worker_id="dialogue-p103-worker")
    assert lease.operation_id==tool.operation_id

    other_turn=incoming(dsn,user,instance,conv,"Different discussion")
    other_topic,_=open_for(dsn,user,instance,conv,other_turn,"Weekend plans")
    response(dsn,user,other_turn,"Let's discuss the weekend.")
    deferred=call(dsn,reconcile_tool_followup,actor(user),tool.tool_call_id)
    assert deferred.followup_state=="DEFERRED"
    with pytest.raises(ToolResultNotReady):
        call(dsn,prepare_tool_followup,actor(user),tool.tool_call_id)
    # Reenter original thread with an explicit evidence-linked focus update.
    resumed=incoming(dsn,user,instance,conv,"Back to that analysis")
    call(dsn,focus_thread,actor(user),FocusThreadCommand(
        **base(user,instance,conv,resumed,revision(dsn,user,conv)),
        thread_id=original,
    ))
    # Even after refocusing, an unanswered newer user turn should not be
    # interrupted with an unrelated separate output.
    assert call(dsn,reconcile_tool_followup,actor(user),
                tool.tool_call_id).followup_state=="DEFERRED"
    response(dsn,user,resumed,"Continuing the analysis.")
    ready=call(dsn,reconcile_tool_followup,actor(user),tool.tool_call_id)
    assert ready.followup_state=="READY"
    prepared=call(dsn,prepare_tool_followup,actor(user),tool.tool_call_id)
    assert "beta (2)" in prepared.text
    delivered=call(dsn,acknowledge_tool_followup,actor(user),tool.tool_call_id)
    assert delivered.text==prepared.text
    assert call(dsn,acknowledge_tool_followup,actor(user),
                tool.tool_call_id).replayed
    assert call(dsn,load_active_dialogue,actor(user),conv).focus_thread_id==original
    trace=call(dsn,get_turn_trace,actor(user),first.turn_id)
    assert trace.operation_id==tool.operation_id
    assert trace.operation_status=="COMPLETED"
    assert trace.followup_status=="DELIVERED"
    assert trace.followup_delivered_ms is not None


def test_same_topic_new_turn_without_explicit_focus_does_not_release_old_tool(dsn):
    user,instance=seed(dsn)
    call(dsn,configure_resource_pool,RESOURCE_KEY,capacity_units=2)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"beta beta")
    thread,_=open_for(dsn,user,instance,conv,first,"Statistics")
    request=SlowTextToolCommand(turn_id=first.turn_id)
    notice=call(dsn,respond_to_text_analysis,actor(user),request,await_seconds=0)
    call(dsn,acknowledge_text_delivery,actor(user),notice.response_id)
    tool=call(dsn,schedule_text_analysis,actor(user),request)
    call(dsn,run_one_text_tool,worker_id="dialogue-p103-no-refocus")
    second=incoming(dsn,user,instance,conv,"What about beta?")
    response(dsn,user,second,"Let me explain.")
    # The focused label alone does not count as a typed relevance warrant.
    assert call(dsn,reconcile_tool_followup,actor(user),
                tool.tool_call_id).followup_state=="DEFERRED"


def test_pending_dialogue_is_not_confused_with_completed_procedure(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Generate a certificate")
    thread,_=open_for(dsn,user,instance,conv,first,"Nginx certificate procedure")
    response(dsn,user,first,"Run the certificate generation command first.")
    state=call(dsn,load_active_dialogue,actor(user),conv)
    assert state.threads[0].resolution_status==Resolution.OPEN
    with psycopg.connect(dsn,autocommit=True) as conn:
        # Dialogue instructions are not task-step evidence or verification.
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s",(user,),
        ).fetchone()[0]==0



def test_nonadjacent_ack_does_not_close_older_pending_instruction(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"Guide me")
    thread,_=open_for(dsn,user,instance,conv,first,"Guided install")
    first_response=response(dsn,user,first,"Let me know when you saw this step.")
    act=uuid4()
    call(dsn,offer_dialogue_act,actor(user),OfferDialogueActCommand(
        **base(user,instance,conv,first,revision(dsn,user,conv)),
        thread_id=thread,act_id=act,kind=DialogueActKind.ACKNOWLEDGMENT,
        label="Confirm receipt",source_response_id=first_response.response_id,
    ))
    intervening=incoming(dsn,user,instance,conv,"Which version should I use?")
    response(dsn,user,intervening,"Use the version approved for the deployment.")
    latest=incoming(dsn,user,instance,conv,"Okay")
    pending=call(dsn,load_active_dialogue,actor(user),conv).pending_acts
    assert [a.act_id for a in pending]==[act]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status,resolution_turn_id FROM dialogue_pending_acts WHERE act_id=%s",
            (act,),
        ).fetchone()==("PENDING",None)


def test_competing_dialogue_commands_use_conversation_revision_cas(dsn):
    user,instance=seed(dsn)
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"What should we discuss?")
    opening_rev=revision(dsn,user,conv)
    gate=Barrier(2)

    def propose(name):
        cmd=OpenThreadCommand(
            **base(user,instance,conv,first,opening_rev),
            thread_id=uuid4(),title=name,
        )
        with psycopg.connect(dsn,autocommit=True) as conn:
            gate.wait(timeout=10)
            return open_thread(conn,actor(user),cmd)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures=[workers.submit(propose,title) for title in ("A","B")]
        outcomes=[]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=12))
            except VersionConflict as exc:
                outcomes.append(exc)
    assert sum(isinstance(x,VersionConflict) for x in outcomes)==1
    assert sum(not isinstance(x,VersionConflict) for x in outcomes)==1
    state=call(dsn,load_active_dialogue,actor(user),conv)
    assert state.revision==opening_rev+1
    assert len(state.threads)==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM events WHERE "
            "event_type='conversation.dialogue.thread_opened.v1' "
            "AND stream_id=(SELECT stream_id FROM event_streams WHERE "
            "domain=%s)",
            (f"conversation:{conv}",),
        ).fetchone()[0]==1



def test_small_context_budget_omits_metadata_without_losing_latest_input(dsn):
    """Optional active-state metadata cannot overflow a valid 512-char budget."""
    user,instance=seed(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE persona_definitions SET stable_key=%s "
            "WHERE id=(SELECT persona_id FROM persona_instances WHERE id=%s)",
            ("s"+uuid4().hex[:8],instance),
        )
    conv=uuid4()
    first=incoming(dsn,user,instance,conv,"First request")
    before=call(
        dsn,prepare_text_context,actor(user),first.turn_id,
        max_turns=1,max_context_chars=512,
    )
    assert before.messages[-1].content=="First request"
    thread,_=open_for(dsn,user,instance,conv,first,"T")
    after=call(
        dsn,prepare_text_context,actor(user),first.turn_id,
        max_turns=1,max_context_chars=512,
    )
    assert after.messages[-1].role=="user"
    assert after.messages[-1].content=="First request"
    assert sum(len(m.content) for m in after.messages)<=512
    assert "Active dialogue:" not in after.messages[0].content
    assert after.conversation_revision>before.conversation_revision

    call(dsn,register_referent,actor(user),RegisterReferentCommand(
        **base(user,instance,conv,first,revision(dsn,user,conv)),
        thread_id=thread,referent_id=uuid4(),kind="ENTITY",
        label="very long but still valid reference",
    ))
    with_referent=call(
        dsn,prepare_text_context,actor(user),first.turn_id,
        max_turns=1,max_context_chars=512,
    )
    assert with_referent.messages[-1].content=="First request"
    assert sum(len(m.content) for m in with_referent.messages)<=512


def test_resumed_followup_is_positioned_at_delivery_even_when_source_outside_window(dsn):
    """The late analysis follows the resume reply, not its original request."""
    user,instance=seed(dsn)
    call(dsn,configure_resource_pool,RESOURCE_KEY,capacity_units=2)
    conv=uuid4()
    source=incoming(dsn,user,instance,conv,"alpha beta beta gamma")
    source_thread,_=open_for(dsn,user,instance,conv,source,"Analysis")
    request=SlowTextToolCommand(turn_id=source.turn_id,max_words=2)
    notice=call(dsn,respond_to_text_analysis,actor(user),request,await_seconds=0)
    call(dsn,acknowledge_text_delivery,actor(user),notice.response_id)
    tool=call(dsn,schedule_text_analysis,actor(user),request)
    call(dsn,run_one_text_tool,worker_id="chronology-worker")

    unrelated=incoming(dsn,user,instance,conv,"Unrelated trip planning")
    open_for(dsn,user,instance,conv,unrelated,"Trip planning")
    unrelated_reply=response(dsn,user,unrelated,"We can discuss the trip.")
    resumed=incoming(dsn,user,instance,conv,"Back to the text analysis")
    call(dsn,focus_thread,actor(user),FocusThreadCommand(
        **base(user,instance,conv,resumed,revision(dsn,user,conv)),
        thread_id=source_thread,
    ))
    resumed_reply=response(dsn,user,resumed,"The analysis is ready.")
    assert call(dsn,reconcile_tool_followup,actor(user),
                tool.tool_call_id).followup_state=="READY"
    published=call(dsn,prepare_tool_followup,actor(user),tool.tool_call_id)
    call(dsn,acknowledge_tool_followup,actor(user),tool.tool_call_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT turn_id,delivery_turn_id FROM conversation_assistant_followups "
            "WHERE tool_call_id=%s",(tool.tool_call_id,),
        ).fetchone()==(source.turn_id,resumed.turn_id)

    next_turn=incoming(dsn,user,instance,conv,"What did beta count?")
    window=call(dsn,prepare_text_context,actor(user),next_turn.turn_id,max_turns=2)
    assert [(m.role,m.content) for m in window.messages[1:]]==[
        ("user","Back to the text analysis"),
        ("assistant",resumed_reply.text),
        ("assistant",published.text),
        ("user","What did beta count?"),
    ]
    # Full recent history preserves the intervening topic *before* the
    # resumed output, rather than moving that output back to the source.
    full=call(dsn,prepare_text_context,actor(user),next_turn.turn_id,max_turns=4)
    assert [(m.role,m.content) for m in full.messages[1:]]==[
        ("user","alpha beta beta gamma"),
        ("assistant",notice.text),
        ("user","Unrelated trip planning"),
        ("assistant",unrelated_reply.text),
        ("user","Back to the text analysis"),
        ("assistant",resumed_reply.text),
        ("assistant",published.text),
        ("user","What did beta count?"),
    ]
    assert len([m for m in full.messages if m.content==published.text])==1
