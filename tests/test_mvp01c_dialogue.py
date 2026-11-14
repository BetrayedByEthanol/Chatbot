"""MVP-01C real PostgreSQL tests: automatic dialogue, never goal progress."""
from uuid import UUID, uuid4

import pytest

psycopg=pytest.importorskip("psycopg")

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, respond_to_text,
)
from persona_agent.conversation_contracts import (
    ModelIntentCandidate, ModelReply, TextInputCommand,
)
from persona_agent.dialogue_contracts import Attention, Resolution
from persona_agent.dialogue_state import load_active_dialogue


class ReplyModel:
    def __init__(self, replies):
        self.replies=iter(replies)
        self.calls=0

    def generate(self, messages):
        self.calls+=1
        return ModelReply(model_id="dialogue-mvp01c",text=next(self.replies))


def state(dsn,identity):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return load_active_dialogue(
            conn,TrustedActor(user_id=identity.user_id,source_id="mvp01c-test"),
            identity.conversation_id,
        )


def test_objective_creates_focused_topic_and_delivered_question(dsn):
    identity=seed_local_demo(dsn)
    model=ReplyModel((
        "We could start small. Which day would work for you?",
        "Tuesday is a good start.",
    ))
    harness=BehavioralAcceptanceHarness(dsn,identity,model)
    harness.begin_session("goal-dialogue")
    first=harness.send(
        "I want to walk more.",idempotency_key="goal",
    )
    active=state(dsn,identity)
    assert len(active.threads)==1
    thread=active.threads[0]
    assert thread.title=="walk more"
    assert thread.attention_status==Attention.FOCUSED
    assert thread.resolution_status==Resolution.OPEN
    assert active.focus_thread_id==thread.thread_id
    assert len(active.pending_acts)==1
    assert active.pending_acts[0].kind=="QUESTION"
    assert active.pending_acts[0].label=="Which day would work for you?"
    assert len(active.referents)==1
    assert active.referents[0].label=="walk more"
    assert thread.next_conversational_action.startswith("Await answer:")

    harness.restart_host()
    harness.send("Tuesday would work.",idempotency_key="answered")
    after=state(dsn,identity)
    assert after.focus_thread_id==thread.thread_id
    assert after.pending_acts==()
    assert after.threads[0].next_conversational_action is None
    assert after.threads[0].resolution_status==Resolution.OPEN
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT origin_kind,status,resolution_kind
               FROM dialogue_pending_acts WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==(
            "AUTO_DELIVERED_QUESTION","RESOLVED","ANSWERED",
        )
        assert conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM operations WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()[0]==2
    assert model.calls==2
    assert first.turn_id


@pytest.mark.parametrize("next_action,expected_next_action", [
    ("Summarize the agreed walking schedule",
     "Summarize the agreed walking schedule"),
    ("Await answer: A different follow-up?",
     "Await answer: A different follow-up?"),
    ("Await answer: Which day would work for you?", None),
])
def test_answer_resolves_auto_question_without_erasing_explicit_next_action(
    dsn,next_action,expected_next_action,
):
    """A newer, explicitly set next action wins over an auto question hint."""
    from persona_agent.dialogue_contracts import SetThreadProgressCommand
    from persona_agent.dialogue_state import set_thread_progress

    identity=seed_local_demo(dsn)
    actor=TrustedActor(user_id=identity.user_id,source_id="mvp01c-next-action")
    with psycopg.connect(dsn,autocommit=True) as conn:
        first=ingest_text(conn,actor,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="walking",
            text="I want to walk more.",
        ))
        reply=respond_to_text(
            conn,actor,first.turn_id,
            ReplyModel(("Which day would work for you?",)),
        )
        acknowledge_text_delivery(conn,actor,reply.response_id)

    original=state(dsn,identity)
    thread=original.focus_thread_id
    question=original.pending_acts[0]
    assert question.label=="Which day would work for you?"
    assert original.threads[0].next_conversational_action==(
        "Await answer: Which day would work for you?"
    )

    with psycopg.connect(dsn,autocommit=True) as conn:
        set_thread_progress(conn,actor,SetThreadProgressCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            expected_revision=original.revision,
            evidence_turn_id=first.turn_id,
            idempotency_key="explicit-followup",
            thread_id=thread,
            resolution=Resolution.OPEN,
            attention=Attention.FOCUSED,
            next_conversational_action=next_action,
        ))
        answered=ingest_text(conn,actor,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="answer",
            text="Tuesday would work.",
        ))

    after=state(dsn,identity)
    assert after.pending_acts==()
    assert after.focus_thread_id==thread
    assert after.threads[0].next_conversational_action==expected_next_action
    assert after.threads[0].resolution_status==Resolution.OPEN
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT status,resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts WHERE act_id=%s""",
            (question.act_id,),
        ).fetchone()==("RESOLVED","ANSWERED",answered.turn_id)
        assert conn.execute(
            """SELECT last_turn_id,next_conversational_action
               FROM dialogue_threads WHERE thread_id=%s""",
            (thread,),
        ).fetchone()==(answered.turn_id,expected_next_action)
        assert conn.execute(
            """SELECT thread_id FROM dialogue_turn_links
               WHERE turn_id=%s AND conversation_id=%s""",
            (answered.turn_id,identity.conversation_id),
        ).fetchone()==(thread,)
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type='conversation.dialogue.auto_question_answered.v1'
                 AND payload->>'act_id'=%s""",
            (str(question.act_id),),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0

    # Regression for the *delivery* stage: the original ingress-only test
    # could pass while a follow-up assistant question still overwrote the
    # caller's custom action with its own "Await answer" hint.
    followup_text="Which time would work for you?"
    with psycopg.connect(dsn,autocommit=True) as conn:
        followup=respond_to_text(
            conn,actor,answered.turn_id,ReplyModel((followup_text,)),
        )
        assert followup.delivery_status=="GENERATED"
        assert state(dsn,identity).threads[0].next_conversational_action==(
            expected_next_action
        )
        delivered=acknowledge_text_delivery(conn,actor,followup.response_id)
        replay=acknowledge_text_delivery(conn,actor,followup.response_id)
        assert delivered.delivery_status=="DELIVERED"
        assert replay.replayed

    after_delivery=state(dsn,identity)
    expected_delivered_action=(
        expected_next_action
        if expected_next_action is not None
        else "Await answer: "+followup_text
    )
    assert after_delivery.threads[0].next_conversational_action==(
        expected_delivered_action
    )
    assert after_delivery.focus_thread_id==thread
    assert len(after_delivery.pending_acts)==1
    assert after_delivery.pending_acts[0].label==followup_text
    assert after_delivery.pending_acts[0].source_response_id==followup.response_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT status,resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts WHERE act_id=%s""",
            (question.act_id,),
        ).fetchone()==("RESOLVED","ANSWERED",answered.turn_id)
        assert conn.execute(
            """SELECT count(*) FROM dialogue_pending_acts
               WHERE source_response_id=%s AND status='PENDING'""",
            (followup.response_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type='conversation.dialogue.auto_question_offered.v1'
                 AND payload->>'response_id'=%s""",
            (str(followup.response_id),),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0


def test_topic_switch_preserves_independent_resolution_and_old_question(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "What's stopping you from walking?",
        "We can discuss recipes.",
    )))
    harness.begin_session("topics")
    harness.send("I want to walk more.",idempotency_key="walk")
    walk=state(dsn,identity).focus_thread_id
    harness.send(
        "Let's discuss cooking instead.",idempotency_key="cooking",
    )
    active=state(dsn,identity)
    assert len(active.threads)==2
    assert active.focus_thread_id!=walk
    status={t.thread_id:t for t in active.threads}
    assert status[walk].attention_status==Attention.RECENT
    assert status[walk].resolution_status==Resolution.OPEN
    assert status[active.focus_thread_id].attention_status==Attention.FOCUSED
    # Switching topics never completes or auto-answers an older question.
    with psycopg.connect(dsn,autocommit=True) as conn:
        q=conn.execute(
            """SELECT a.status,a.resolution_kind FROM dialogue_pending_acts a
               WHERE a.thread_id=%s""", (walk,),
        ).fetchone()
        assert q==("PENDING",None)


def test_explicit_reference_refocuses_unique_existing_thread(dsn):
    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "We can build a walking habit.",
        "Okay, recipes are another subject.",
        "Let's pick up the walking discussion.",
    )))
    host.begin_session("refocus")
    host.send("I want to walk more.",idempotency_key="walking")
    walk=state(dsn,identity).focus_thread_id
    host.send(
        "Let's discuss cooking instead.",idempotency_key="switch",
    )
    cooking=state(dsn,identity).focus_thread_id
    assert cooking!=walk
    host.restart_host()
    host.send(
        "Can we plan the next step for that goal?",
        idempotency_key="refer-to-walking",
    )
    active=state(dsn,identity)
    assert len(active.threads)==2
    assert active.focus_thread_id==walk
    by_id={thread.thread_id:thread for thread in active.threads}
    assert by_id[cooking].attention_status==Attention.RECENT
    assert by_id[cooking].resolution_status==Resolution.OPEN


@pytest.mark.parametrize("switch", [
    "Switch the topic to cooking.",
    "Change the topic to cooking.",
    "New topic: cooking.",
])
def test_explicit_topic_switch_is_admitted_and_routed_at_delivery(dsn, switch):
    """Explicit switch wording must persist a signal before delivery routing."""
    from persona_agent.intent_interpretation import (
        classify_intent, parse_explicit_topic_switch,
        should_interpret_with_model,
    )

    draft=classify_intent(switch)
    assert draft is not None
    assert draft.kind=="REQUEST"
    assert draft.label==switch
    assert parse_explicit_topic_switch(switch)=="cooking"
    assert not should_interpret_with_model(switch)

    class DeterministicModel(ReplyModel):
        def generate_interpreted(self,messages):
            raise AssertionError("explicit switches must not need model interpretation")

    identity=seed_local_demo(dsn)
    model=DeterministicModel((
        "Which day would suit your walking plan?",
        "What would you like to cook?",
    ))
    host=BehavioralAcceptanceHarness(dsn,identity,model)
    host.begin_session("explicit-switch")
    host.send("I want to walk more.",idempotency_key="walking")
    walking_state=state(dsn,identity)
    walking=walking_state.focus_thread_id
    question=walking_state.pending_acts[0]
    assert question.thread_id==walking

    new_turn=host.send(switch,idempotency_key="switch")
    active=state(dsn,identity)
    assert len(active.threads)==2
    assert active.focus_thread_id!=walking
    assert active.threads[0].title=="cooking"
    assert active.threads[0].attention_status==Attention.FOCUSED
    assert active.pending_acts[0].thread_id==active.focus_thread_id
    assert active.pending_acts[0].label=="What would you like to cook?"
    assert len(active.pending_acts)==1

    with psycopg.connect(dsn,autocommit=True) as conn:
        signal=conn.execute(
            """SELECT intent_kind,label,epistemic_status,proposal_status,
                      source_event_id,related_signal_id
               FROM conversation_intent_signals
               WHERE turn_id=%s AND conversation_id=%s
                 AND user_id=%s AND instance_id=%s""",
            (UUID(new_turn.turn_id),identity.conversation_id,
             identity.user_id,identity.instance_id),
        ).fetchone()
        assert signal[0:4]==("REQUEST",switch,"RULE_INFERRED","UNREVIEWED")
        assert signal[4] is not None
        assert signal[5] is None
        assert conn.execute(
            """SELECT thread_id FROM dialogue_turn_links
               WHERE turn_id=%s AND conversation_id=%s""",
            (UUID(new_turn.turn_id),identity.conversation_id),
        ).fetchone()==(active.focus_thread_id,)
        assert conn.execute(
            """SELECT status,resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts WHERE act_id=%s""",
            (question.act_id,),
        ).fetchone()==("PENDING",None,None)
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s
               AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM operations WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0
    assert model.calls==2


@pytest.mark.parametrize("text", [
    "Switch the topic to", "New topic:", "What if I switched the topic to cooking?",
])
def test_incomplete_or_hypothetical_switch_is_not_a_new_topic_request(text):
    from persona_agent.intent_interpretation import (
        classify_intent, parse_explicit_topic_switch,
    )
    assert parse_explicit_topic_switch(text) is None
    candidate=classify_intent(text)
    assert candidate is None or candidate.kind!="REQUEST"


def test_cross_thread_objective_reference_does_not_answer_cooking_question(dsn):
    """A referenced walking goal outranks the adjacent cooking question."""
    identity=seed_local_demo(dsn)
    model=ReplyModel((
        "A short walk each morning could be a good starting point.",
        "What ingredients do you have available?",
        "Would you like to plan a short walk for tomorrow?",
    ))
    host=BehavioralAcceptanceHarness(dsn,identity,model)
    host.begin_session("cross-thread-reference")
    host.send("I want to walk more.",idempotency_key="walking-objective")
    original=state(dsn,identity)
    walking=original.focus_thread_id
    assert walking is not None
    assert original.pending_acts==()

    host.send(
        "Let's discuss cooking instead.",idempotency_key="cooking-topic",
    )
    cooking_state=state(dsn,identity)
    cooking=cooking_state.focus_thread_id
    assert cooking is not None and cooking!=walking
    assert len(cooking_state.pending_acts)==1
    cooking_question=cooking_state.pending_acts[0]
    assert cooking_question.label=="What ingredients do you have available?"

    text="Please suggest the next step for that goal."
    auth=TrustedActor(user_id=identity.user_id,source_id="mvp01c-reference-test")
    with psycopg.connect(dsn,autocommit=True) as conn:
        entered=ingest_text(
            conn,auth,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                idempotency_key="reference-walking",
                text=text,
            ),
        )
        signal=conn.execute(
            """SELECT intent_kind,related_signal_id FROM conversation_intent_signals
               WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
                 AND turn_id=%s""",
            (identity.conversation_id,identity.user_id,
             identity.instance_id,entered.turn_id),
        ).fetchone()
        assert signal[0]=="REQUEST"
        assert signal[1] is not None
        referenced=conn.execute(
            """SELECT linked.thread_id
               FROM conversation_intent_signals source
               JOIN dialogue_turn_links linked ON linked.turn_id=source.turn_id
               WHERE source.signal_id=%s AND source.conversation_id=%s
                 AND source.user_id=%s AND source.instance_id=%s""",
            (signal[1],identity.conversation_id,
             identity.user_id,identity.instance_id),
        ).fetchone()
        assert referenced==(walking,)
        assert conn.execute(
            """SELECT status,resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts WHERE act_id=%s""",
            (cooking_question.act_id,),
        ).fetchone()==("PENDING",None,None)
        # The input must remain unlinked until persisted reference resolution
        # has selected the original walking thread on delivery.
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE turn_id=%s AND conversation_id=%s""",
            (entered.turn_id,identity.conversation_id),
        ).fetchone()[0]==0

        response=respond_to_text(conn,auth,entered.turn_id,model)
        assert response.delivery_status=="GENERATED"
        assert state(dsn,identity).focus_thread_id==cooking
        acknowledge_text_delivery(conn,auth,response.response_id)

    active=state(dsn,identity)
    assert len(active.threads)==2
    assert active.focus_thread_id==walking
    assert len(active.pending_acts)==1
    assert active.pending_acts[0].label==(
        "Would you like to plan a short walk for tomorrow?"
    )
    assert all(a.thread_id==walking for a in active.pending_acts)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT status,resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts WHERE act_id=%s""",
            (cooking_question.act_id,),
        ).fetchone()==("PENDING",None,None)
        assert conn.execute(
            """SELECT thread_id FROM dialogue_turn_links
               WHERE turn_id=%s AND conversation_id=%s""",
            (entered.turn_id,identity.conversation_id),
        ).fetchone()==(walking,)
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s
               AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0


def test_delivered_choice_is_a_pending_decision_not_automatic_consent(dsn):
    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "Would you prefer walking indoors or outdoors?",
        "Okay, let's discuss what each would involve.",
    )))
    host.begin_session("choice")
    host.send("I want to walk more.",idempotency_key="start")
    first=state(dsn,identity)
    assert len(first.pending_acts)==1
    assert first.pending_acts[0].kind=="DECISION"
    assert "Await choice:" in first.threads[0].next_conversational_action

    # An affirmative conversational answer alone does not resolve a
    # decision or create an approved Goal/Task outcome.
    host.send("Yes",idempotency_key="not-approval")
    after=state(dsn,identity)
    assert [act.act_id for act in after.pending_acts]==[
        first.pending_acts[0].act_id
    ]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT origin_kind,status FROM dialogue_pending_acts
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==("AUTO_DELIVERED_DECISION","PENDING")
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0


def test_new_topic_request_does_not_answer_old_pending_question(dsn):
    """A request to cook dinner is not an answer to a walking question."""
    identity=seed_local_demo(dsn)
    model=ReplyModel((
        "Which day would suit your walking plan?",
        "What ingredients do you have for dinner?",
    ))
    host=BehavioralAcceptanceHarness(dsn,identity,model)
    host.begin_session("new-topic-is-not-an-answer")

    host.send("I want to walk more.",idempotency_key="walking")
    previous=state(dsn,identity)
    walk=previous.focus_thread_id
    question=previous.pending_acts[0]
    assert question.label=="Which day would suit your walking plan?"

    second=host.send("Help me cook dinner.",idempotency_key="dinner")
    current=state(dsn,identity)
    assert len(current.threads)==2
    assert current.focus_thread_id!=walk
    assert {act.act_id for act in current.pending_acts}=={
        act.act_id for act in current.pending_acts
        if act.thread_id==current.focus_thread_id
    }
    assert len(current.pending_acts)==1
    assert current.pending_acts[0].label=="What ingredients do you have for dinner?"

    with psycopg.connect(dsn,autocommit=True) as conn:
        old=conn.execute(
            """SELECT status,resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts WHERE act_id=%s""",
            (question.act_id,),
        ).fetchone()
        assert old==("PENDING",None,None)
        new_thread=conn.execute(
            """SELECT l.thread_id,t.title
               FROM dialogue_turn_links l
               JOIN dialogue_threads t ON t.thread_id=l.thread_id
               WHERE l.turn_id=%s AND l.conversation_id=%s""",
            (UUID(second.turn_id),identity.conversation_id),
        ).fetchone()
        assert new_thread==(current.focus_thread_id,"Help me cook dinner.")
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0


def test_explicit_thread_progress_between_generation_and_delivery_wins(dsn):
    """Delivery succeeds but does not revert an intervening explicit change."""
    from persona_agent.dialogue_contracts import SetThreadProgressCommand
    from persona_agent.dialogue_state import set_thread_progress

    identity=seed_local_demo(dsn)
    auth=TrustedActor(user_id=identity.user_id,source_id="mvp01c-explicit")
    with psycopg.connect(dsn,autocommit=True) as conn:
        initial=ingest_text(conn,auth,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="first-topic",
            text="I want to walk more.",
        ))
        first_reply=respond_to_text(
            conn,auth,initial.turn_id,ReplyModel(("We can discuss walking.",)),
        )
        acknowledge_text_delivery(conn,auth,first_reply.response_id)

    initial_state=state(dsn,identity)
    thread=initial_state.focus_thread_id
    assert thread is not None
    assert initial_state.pending_acts==()

    with psycopg.connect(dsn,autocommit=True) as conn:
        second=ingest_text(conn,auth,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="next-turn",
            text="Tuesday might work.",
        ))
        generated=respond_to_text(
            conn,auth,second.turn_id,
            ReplyModel(("Which day would you like to start?",)),
        )

    after_generation=state(dsn,identity)
    explicit=SetThreadProgressCommand(
        conversation_id=identity.conversation_id,
        instance_id=identity.instance_id,
        expected_generation=identity.generation,
        expected_revision=after_generation.revision,
        evidence_turn_id=second.turn_id,
        idempotency_key="explicit-resolution",
        thread_id=thread,
        resolution=Resolution.RESOLVED,
        attention=Attention.FOCUSED,
        next_conversational_action=None,
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        applied=set_thread_progress(conn,auth,explicit)
        delivered=acknowledge_text_delivery(conn,auth,generated.response_id)
    assert applied.revision==after_generation.revision+1
    assert delivered.delivery_status=="DELIVERED"

    restored=state(dsn,identity)
    assert restored.focus_thread_id==thread
    assert restored.threads[0].resolution_status==Resolution.RESOLVED
    assert restored.threads[0].next_conversational_action is None
    assert restored.pending_acts==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM dialogue_pending_acts
               WHERE source_response_id=%s""",
            (generated.response_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT delivery_status FROM conversation_responses
               WHERE response_id=%s""",
            (generated.response_id,),
        ).fetchone()==("DELIVERED",)


@pytest.mark.parametrize("auto_reply,auto_origin,explicit_kind,explicit_label", [
    (
        "Which day would suit your walking plan?",
        "AUTO_DELIVERED_QUESTION",
        "DECISION",
        "Choose indoor or outdoor walking",
    ),
    (
        "Would you prefer morning or evening?",
        "AUTO_DELIVERED_DECISION",
        "QUESTION",
        "Which day would suit you?",
    ),
])
def test_explicit_offer_replaces_automatic_act_without_losing_provenance(
    dsn,auto_reply,auto_origin,explicit_kind,explicit_label,
):
    """A delivered auto proposal must not block its explicit correction."""
    from persona_agent.dialogue_contracts import (
        DialogueActKind, OfferDialogueActCommand,
    )
    from persona_agent.dialogue_state import offer_dialogue_act
    from persona_agent.runtime import VersionConflict

    identity=seed_local_demo(dsn)
    auth=TrustedActor(
        user_id=identity.user_id,source_id="mvp01c-reconcile",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        first=ingest_text(conn,auth,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="automatic-source",
            text="I want to walk more.",
        ))
        response=respond_to_text(
            conn,auth,first.turn_id,ReplyModel((auto_reply,)),
        )
        acknowledge_text_delivery(conn,auth,response.response_id)
    auto_state=state(dsn,identity)
    thread=auto_state.focus_thread_id
    assert thread is not None
    assert len(auto_state.pending_acts)==1
    automatic=auto_state.pending_acts[0]
    explicit_id=uuid4()
    command=OfferDialogueActCommand(
        conversation_id=identity.conversation_id,
        instance_id=identity.instance_id,
        expected_generation=identity.generation,
        expected_revision=auto_state.revision,
        evidence_turn_id=first.turn_id,
        idempotency_key="correct-auto-interpretation",
        thread_id=thread,
        act_id=explicit_id,
        source_response_id=response.response_id,
        kind=DialogueActKind(explicit_kind),
        label=explicit_label,
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        accepted=offer_dialogue_act(conn,auth,command)
        replay=offer_dialogue_act(conn,auth,command)
        assert replay.replayed and replay.event_id==accepted.event_id
        assert accepted.thread_id==thread

    active=state(dsn,identity)
    assert [a.act_id for a in active.pending_acts]==[explicit_id]
    assert active.pending_acts[0].kind==DialogueActKind(explicit_kind)
    assert active.pending_acts[0].label==explicit_label
    prefix="Await choice: " if explicit_kind=="DECISION" else "Await answer: "
    assert active.threads[0].next_conversational_action==(
        prefix+explicit_label
    )

    with psycopg.connect(dsn,autocommit=True) as conn:
        rows=conn.execute(
            """SELECT act_id,origin_kind,kind,label,status,
                      resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts
               WHERE source_response_id=%s AND conversation_id=%s
               ORDER BY created_at,act_id""",
            (response.response_id,identity.conversation_id),
        ).fetchall()
        by_id={row[0]:row for row in rows}
        assert len(by_id)==2
        old=by_id[automatic.act_id]
        assert old[1]==auto_origin
        assert old[4:] == ("CANCELLED",None,first.turn_id)
        corrected=by_id[explicit_id]
        assert corrected[1:]==(
            "EXPLICIT",explicit_kind,explicit_label,"PENDING",None,None,
        )
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type='conversation.dialogue.auto_act_replaced.v1'
                 AND payload->>'automatic_act_id'=%s
                 AND payload->>'explicit_act_id'=%s""",
            (str(automatic.act_id),str(explicit_id)),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0

    # Re-registration under a fresh revision is a domain conflict, not a
    # PostgreSQL UNIQUE violation; explicit registration wins once.
    alternate=command.model_copy(update={
        "act_id":uuid4(),
        "expected_revision":active.revision,
        "idempotency_key":"different-explicit-offer",
    })
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(VersionConflict,match="already has an explicit"):
            offer_dialogue_act(conn,auth,alternate)


def test_cancelled_automatic_act_does_not_reserve_response_forever(dsn):
    from persona_agent.dialogue_contracts import (
        DialogueActKind, OfferDialogueActCommand,
        ResolveDialogueActCommand,
    )
    from persona_agent.dialogue_state import (
        offer_dialogue_act, resolve_dialogue_act,
    )

    identity=seed_local_demo(dsn)
    auth=TrustedActor(user_id=identity.user_id,source_id="mvp01c-cancel")
    with psycopg.connect(dsn,autocommit=True) as conn:
        first=ingest_text(conn,auth,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="auto-before-cancel",
            text="I want to walk more.",
        ))
        reply=respond_to_text(
            conn,auth,first.turn_id,
            ReplyModel(("Which day should we start?",)),
        )
        acknowledge_text_delivery(conn,auth,reply.response_id)
        pending=state(dsn,identity).pending_acts[0]
        resolve_dialogue_act(conn,auth,ResolveDialogueActCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            expected_revision=state(dsn,identity).revision,
            evidence_turn_id=first.turn_id,
            idempotency_key="cancel-inferred",
            act_id=pending.act_id,outcome=None,cancel=True,
        ))
        explicit_id=uuid4()
        offer_dialogue_act(conn,auth,OfferDialogueActCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            expected_revision=state(dsn,identity).revision,
            evidence_turn_id=first.turn_id,
            idempotency_key="explicit-after-cancel",
            thread_id=state(dsn,identity).focus_thread_id,
            act_id=explicit_id,source_response_id=reply.response_id,
            kind=DialogueActKind.DECISION,label="Choose an exercise plan",
        ))
    active=state(dsn,identity)
    assert [a.act_id for a in active.pending_acts]==[explicit_id]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT status FROM dialogue_pending_acts WHERE act_id=%s""",
            (pending.act_id,),
        ).fetchone()==("CANCELLED",)
        assert conn.execute(
            """SELECT origin_kind,status FROM dialogue_pending_acts
               WHERE act_id=%s""",
            (explicit_id,),
        ).fetchone()==("EXPLICIT","PENDING")


def test_same_objective_reuses_topic_without_duplicating(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "We can discuss the garden.",
        "We still have that topic.",
    )))
    harness.begin_session("repeat")
    harness.send("I want to build a garden.",idempotency_key="first")
    original=state(dsn,identity).focus_thread_id
    harness.send("I want to build a garden.",idempotency_key="second")
    current=state(dsn,identity)
    assert len(current.threads)==1
    assert current.focus_thread_id==original
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()[0]==2


def test_generated_question_does_not_become_pending_until_delivery(dsn):
    identity=seed_local_demo(dsn)
    auth=TrustedActor(
        user_id=identity.user_id,source_id="mvp01c-test",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        first=ingest_text(
            conn,auth,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                text="I want to improve my cooking.",
                idempotency_key="not-delivered",
            ),
        )
        response=respond_to_text(
            conn,auth,first.turn_id,ReplyModel((
                "Which recipe would you like to try?",
            )),
        )
    before=state(dsn,identity)
    assert before.focus_thread_id is None
    assert before.threads==()
    assert before.pending_acts==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        acknowledge_text_delivery(conn,auth,response.response_id)
        replay=acknowledge_text_delivery(conn,auth,response.response_id)
    assert replay.replayed
    after=state(dsn,identity)
    assert len(after.threads)==1
    assert len(after.pending_acts)==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM dialogue_pending_acts
               WHERE source_response_id=%s""",
            (response.response_id,),
        ).fetchone()[0]==1


def test_literal_okay_and_ambiguous_reply_do_not_auto_answer_question(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "Which day would work?",
        "I can explain.",
        "We could discuss alternatives.",
    )))
    harness.begin_session("no-false-answers")
    harness.send("I want to walk daily.",idempotency_key="one")
    first=state(dsn,identity).pending_acts[0]
    harness.send("Okay",idempotency_key="okay")
    assert [a.act_id for a in state(dsn,identity).pending_acts]==[first.act_id]
    harness.send("What about weekends?",idempotency_key="question")
    assert [a.act_id for a in state(dsn,identity).pending_acts]==[first.act_id]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT resolution_kind FROM dialogue_pending_acts
               WHERE act_id=%s""", (first.act_id,),
        ).fetchone()==(None,)


def test_manual_thread_does_not_get_auto_question_or_duplicate_topic(dsn):
    from persona_agent.dialogue_contracts import OpenThreadCommand
    from persona_agent.dialogue_state import open_thread

    identity=seed_local_demo(dsn)
    auth=TrustedActor(user_id=identity.user_id,source_id="mvp01c-manual")
    with psycopg.connect(dsn,autocommit=True) as conn:
        incoming=ingest_text(
            conn,auth,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                text="I want to learn Python.",
                idempotency_key="manual-input",
            ),
        )
        open_thread(
            conn,auth,OpenThreadCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                expected_revision=1,evidence_turn_id=incoming.turn_id,
                thread_id=uuid4(),title="Explicit caller topic",
                idempotency_key="manual-open",
            ),
        )
        response=respond_to_text(
            conn,auth,incoming.turn_id,ReplyModel((
                "What project would you like to build?",
            )),
        )
        acknowledge_text_delivery(conn,auth,response.response_id)
    active=state(dsn,identity)
    assert len(active.threads)==1
    assert active.threads[0].title=="Explicit caller topic"
    assert active.pending_acts==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT origin_kind FROM dialogue_threads
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==("EXPLICIT",)


def test_model_proposed_objective_joins_same_dialogue_flow(dsn):
    from persona_agent.conversation_contracts import ModelIntentCandidate

    class WithIntent:
        def generate_interpreted(self,messages):
            message=messages[-1].content
            return ModelReply(
                model_id="mvp01c-model",text="Which day would suit you?",
                intent_candidate=ModelIntentCandidate(
                    kind="OBJECTIVE",evidence_quote=message,
                    confidence=0.93,reference="NONE",
                ),
            )
        def generate(self,messages):
            raise AssertionError("a second inference must not be necessary")

    identity=seed_local_demo(dsn)
    host=BehavioralAcceptanceHarness(dsn,identity,WithIntent())
    host.begin_session("model-cue")
    host.send(
        "Lately I've been thinking about a regular walking routine.",
        idempotency_key="implicit-goal",
    )
    active=state(dsn,identity)
    assert len(active.threads)==1
    assert len(active.pending_acts)==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT epistemic_status FROM conversation_intent_signals
               WHERE conversation_id=%s""",
            (identity.conversation_id,),
        ).fetchone()==("MODEL_PROPOSED",)
        assert conn.execute(
            """SELECT count(*) FROM goal_records
               WHERE user_id=%s AND instance_id=%s""",
            (identity.user_id,identity.instance_id),
        ).fetchone()[0]==0


@pytest.mark.parametrize("model_proposes_objective", [True, False])
def test_model_eligible_input_does_not_answer_unrelated_pending_question(
    dsn, model_proposes_objective,
):
    """Defer answer linkage until the model's current-turn intent is known."""
    from persona_agent.conversation_contracts import ModelIntentCandidate
    from persona_agent.intent_interpretation import (
        classify_intent, should_interpret_with_model,
    )

    implicit="Lately I've been thinking about a regular walking routine."
    assert classify_intent(implicit) is None
    assert should_interpret_with_model(implicit)

    class Model:
        def __init__(self):
            self.plain_calls=0
            self.interpreted_calls=0

        def generate(self,messages):
            self.plain_calls+=1
            return ModelReply(
                model_id="mvp01c-test",
                text="What dish would you like to cook first?",
            )

        def generate_interpreted(self,messages):
            self.interpreted_calls+=1
            assert messages[-1].content==implicit
            return ModelReply(
                model_id="mvp01c-test",
                text="Which day would you like to walk?",
                intent_candidate=(
                    ModelIntentCandidate(
                        kind="OBJECTIVE",evidence_quote=implicit,
                        confidence=0.94,reference="NONE",
                    ) if model_proposes_objective else None
                ),
            )

    identity=seed_local_demo(dsn)
    model=Model()
    host=BehavioralAcceptanceHarness(dsn,identity,model)
    host.begin_session("pending-followed-by-model-eligible")
    host.send("I want to cook more.",idempotency_key="cook")
    initial=state(dsn,identity)
    cooking_thread=initial.focus_thread_id
    cooking_question=initial.pending_acts[0].act_id
    assert initial.pending_acts[0].label=="What dish would you like to cook first?"

    actor=TrustedActor(user_id=identity.user_id,source_id="mvp01c-model")
    with psycopg.connect(dsn,autocommit=True) as conn:
        turn=ingest_text(conn,actor,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="implicit-walk",
            text=implicit,
        ))
        # The earlier question must stay pending even before the model
        # has produced/validated its interpretation for this input.
        assert conn.execute(
            """SELECT status,resolution_turn_id FROM dialogue_pending_acts
               WHERE act_id=%s""",
            (cooking_question,),
        ).fetchone()==("PENDING",None)
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE turn_id=%s AND conversation_id=%s""",
            (turn.turn_id,identity.conversation_id),
        ).fetchone()[0]==0

        reply=respond_to_text(conn,actor,turn.turn_id,model)
        assert reply.delivery_status=="GENERATED"
        # A generated but not yet delivered response does not move focus.
        assert state(dsn,identity).focus_thread_id==cooking_thread
        acknowledge_text_delivery(conn,actor,reply.response_id)

    active=state(dsn,identity)
    assert model.plain_calls==1
    assert model.interpreted_calls==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT status,resolution_kind,resolution_turn_id
               FROM dialogue_pending_acts WHERE act_id=%s""",
            (cooking_question,),
        ).fetchone()==("PENDING",None,None)
        if model_proposes_objective:
            assert len(active.threads)==2
            assert active.focus_thread_id!=cooking_thread
            assert len(active.pending_acts)==1
            assert active.pending_acts[0].label=="Which day would you like to walk?"
            assert conn.execute(
                """SELECT thread_id FROM dialogue_turn_links
                   WHERE turn_id=%s AND conversation_id=%s""",
                (turn.turn_id,identity.conversation_id),
            ).fetchone()==(active.focus_thread_id,)
            assert conn.execute(
                """SELECT intent_kind,epistemic_status
                   FROM conversation_intent_signals WHERE turn_id=%s""",
                (turn.turn_id,),
            ).fetchone()==("OBJECTIVE","MODEL_PROPOSED")
        else:
            # No evidence means no automatic answer and no invented topic.
            assert len(active.threads)==1
            assert active.focus_thread_id==cooking_thread
            assert len(active.pending_acts)==1
            assert active.pending_acts[0].act_id==cooking_question
            assert conn.execute(
                """SELECT count(*) FROM dialogue_turn_links WHERE turn_id=%s""",
                (turn.turn_id,),
            ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM goal_records WHERE user_id=%s""",
            (identity.user_id,),
        ).fetchone()[0]==0


def test_multiple_sessions_do_not_share_dialogue_focus(dsn):
    identity=seed_local_demo(dsn)
    harness=BehavioralAcceptanceHarness(dsn,identity,ReplyModel((
        "What would help you?",
        "Which route?",
    )))
    first=harness.begin_session("one")
    harness.send("I want to eat healthier.",idempotency_key="food")
    first_state=state(dsn,first)
    assert first_state.focus_thread_id is not None
    second=harness.begin_session("two")
    harness.send("I want to walk more.",idempotency_key="walking")
    second_state=state(dsn,second)
    assert second_state.focus_thread_id is not None
    assert second_state.focus_thread_id!=first_state.focus_thread_id
    assert state(dsn,first).focus_thread_id==first_state.focus_thread_id
