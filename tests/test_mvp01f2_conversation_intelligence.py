"""MVP-01F.2 behavioral regressions, using real host/SQL state and no new LLM pass."""
import pytest
pytest.importorskip("psycopg")
import psycopg

from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.behavioral_evaluation import run_scenario
from persona_agent.acceptance_host import seed_local_demo
from persona_agent.conversation_contracts import ModelIntentCandidate, ModelReply
from persona_agent.dialogue_state import load_active_dialogue
from persona_agent.contracts import TrustedActor
from persona_agent.capability_claim_context import capability_claim_context
from persona_agent.conversation import ingest_text, prepare_text_context
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.intent_interpretation import resolved_objective_context_block
from persona_agent.intent_interpretation import (
    conservative_implicit_objective, classify_intent, should_interpret_with_model,
)


WALKING="I've been thinking about walking more regularly."


class OnePass:
    def __init__(self):
        self.requests=[]
    def generate(self,messages):
        self.requests.append(("normal",tuple(messages)))
        return ModelReply(model_id="mock.local",text="We can discuss that.")
    def generate_interpreted(self,messages):
        self.requests.append(("interpreted",tuple(messages)))
        # Simulate the real model omitting optional intent metadata.
        return ModelReply(model_id="mock.local",text="Let's discuss that.",
                          intent_candidate=None)


def test_narrow_rule_does_not_approve_vague_or_hypothetical_objectives():
    assert conservative_implicit_objective(WALKING).kind=="OBJECTIVE"
    assert conservative_implicit_objective(
        "I've been thinking about exercising more often."
    ).kind=="OBJECTIVE"
    for text in (
        "I've been thinking about walking.",
        "I've been thinking about my friend walking more regularly.",
        "What if I've been thinking about walking more regularly?",
        "I've been thinking about not walking more regularly.",
        "I never want to walk again.",
        "Hello there.",
    ):
        assert conservative_implicit_objective(text) is None,text
    assert should_interpret_with_model(WALKING)
    assert classify_intent(WALKING) is None


def test_real_host_walking_frequency_correction_and_topic_return(dsn):
    scenario={"name":"mvp01f2-walking","steps":[
        {"action":"session","name":"day-one"},
        {"action":"message","text":"Hello."},
        {"action":"message","text":WALKING,"expect_intent":"OBJECTIVE",
         "expect_focus_contains":"walking"},
        {"action":"message","text":"Maybe three times per week.",
         "expect_intent":"OBSERVATION","expect_related_intent":"OBJECTIVE"},
        {"action":"message","text":"Actually, twice per week would be better.",
         "expect_intent":"CORRECTION","expect_related_intent":"OBJECTIVE"},
        {"action":"message","text":"Switch the topic to cooking.",
         "expect_intent":"REQUEST","expect_focus_contains":"cooking"},
        {"action":"message","text":"I made pasta for dinner yesterday."},
        {"action":"restart"},
        {"action":"message","text":"What about that walking goal?",
         "expect_focus_contains":"walking","expect_related_intent":"OBJECTIVE"},
        {"action":"message","text":"Let's discuss my walking plan instead.",
         "expect_focus_contains":"walking","expect_related_intent":"OBJECTIVE"},
    ]}
    model=OnePass()
    report=run_scenario(dsn,scenario,model,run_memory_worker=False)
    assert not report["errors"],report["errors"]
    assert report["passed"],[x for x in report["checks"] if not x["pass"]]
    assert len(model.requests)==8
    assert sum(route=="interpreted" for route,_ in model.requests)==1
    rows=report["snapshots"][-1]["database"]
    signals=rows["conversation_intent_signals"]["rows"]
    assert any(x["intent_kind"]=="OBJECTIVE" and
               x["method_revision"]=="mvp01f2.routine_fallback.1" for x in signals)
    threads=rows["dialogue_threads"]["rows"]
    assert len(threads)==2
    assert rows["goal_records"]["count"]==0
    assert rows["operations"]["count"]==0


def test_ambiguous_named_objective_does_not_guess_or_refocus(dsn):
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),OnePass())
    host.begin_session("ambiguous")
    host.send("I want to practice walking daily.",idempotency_key="w1")
    host.send("I want to track walking habits.",idempotency_key="w2")
    host.send("Switch the topic to cooking.",idempotency_key="cook")
    host.send("What about that walking goal?",idempotency_key="reference")
    host.send("Let's discuss my walking plan instead.",idempotency_key="ambiguous-topic")
    with psycopg.connect(dsn,autocommit=True) as conn:
        actor=TrustedActor(user_id=host.active_identity.user_id,
                           source_id="mvp01f2-test")
        dialogue=load_active_dialogue(conn,actor,host.active_identity.conversation_id)
        focused=next(t.title for t in dialogue.threads
                     if t.thread_id==dialogue.focus_thread_id)
        assert focused=="cooking"
        assert len(dialogue.threads)==3  # Two objectives + cooking, no invented fourth.
        row=conn.execute(
            """SELECT related_signal_id FROM conversation_intent_signals
               WHERE conversation_id=%s ORDER BY turn_sequence DESC LIMIT 1""",
            (host.active_identity.conversation_id,),
        ).fetchone()
        assert row[0] is None


def test_frequency_correction_does_not_link_to_unrelated_topic(dsn):
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),OnePass())
    host.begin_session("unrelated")
    host.send("I want to walk more.",idempotency_key="walking")
    host.send("Switch the topic to cooking.",idempotency_key="cooking")
    host.send("Actually, twice per week would be better.",idempotency_key="correction")
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            """SELECT intent_kind,related_signal_id
               FROM conversation_intent_signals
               WHERE conversation_id=%s ORDER BY turn_sequence DESC LIMIT 1""",
            (host.active_identity.conversation_id,),
        ).fetchone()
        assert row==("CORRECTION",None)


def test_capability_grounding_is_selective_and_tracks_registered_tools(dsn):
    assert capability_claim_context("How do I configure Nginx?")==""
    assert "clock.now.v1" in capability_claim_context("Can you run this for me?")
    assert "text.statistics.v1" in capability_claim_context(
        "What if I asked you to delete my data?"
    )
    assert "deletion tool" in capability_claim_context(
        "What if I asked you to delete my data?"
    )
    scenario={"name":"mvp01f2-capabilities","steps":[
        {"action":"session","name":"tools"},
        {"action":"message","text":"Hello."},
        {"action":"message","text":"How do I configure Nginx?"},
        {"action":"message","text":"Can you run this for me?",
         "expect_no_new_operations":True},
        {"action":"message","text":"What if I asked you to delete my data?",
         "expect_no_new_operations":True},
    ]}
    model=OnePass()
    result=run_scenario(dsn,scenario,model,run_memory_worker=False)
    assert not result["errors"],result["errors"]
    assert result["passed"],[x for x in result["checks"] if not x["pass"]]
    prompts=[msgs[0].content for _,msgs in model.requests]
    assert all("Trusted host capability boundary:" not in p for p in prompts[:2])
    assert all("Trusted host capability boundary:" in p for p in prompts[2:])
    assert result["snapshots"][-1]["database"]["operations"]["count"]==0


def test_wrong_model_label_cannot_shadow_grounded_routine_objective(dsn):
    class MisclassifyingModel(OnePass):
        def generate_interpreted(self,messages):
            self.requests.append(("interpreted",tuple(messages)))
            return ModelReply(
                model_id="misclassified",text="Okay.",
                intent_candidate=ModelIntentCandidate(
                    kind="OBSERVATION",evidence_quote=WALKING,
                    confidence=0.93,reference="NONE",
                ),
            )
    report=run_scenario(
        dsn,{"name":"misclassified-objective","steps":[
            {"action":"session","name":"first"},
            {"action":"message","text":WALKING,
             "expect_intent":"OBJECTIVE","expect_focus_contains":"walking"},
        ]},MisclassifyingModel(),run_memory_worker=False,
    )
    assert not report["errors"],report["errors"]
    assert report["passed"],[c for c in report["checks"] if not c["pass"]]
    signals=report["snapshots"][-1]["database"]["conversation_intent_signals"]["rows"]
    assert len(signals)==1
    assert signals[0]["intent_kind"]=="OBJECTIVE"
    assert signals[0]["epistemic_status"]=="RULE_INFERRED"
    assert signals[0]["source_response_id"] is None
    assert report["interpretation_diagnostics"][0][
        "admission"]=="RULE_FALLBACK_PERSISTED"
    assert report["snapshots"][-1]["database"]["goal_records"]["count"]==0


def test_resolved_named_objective_is_present_at_inference_before_refocus(dsn):
    """The old objective is outside recent verbatim history, yet model sees it.

    Reading the target context is not a durable focus update. It must remain
    cooking while the reply is being generated, then switch upon delivery.
    """
    class ContextModel(OnePass):
        def __init__(self):
            super().__init__()
            self.final_context=None
            self.focus_during_inference=None

        def generate(self,messages):
            if messages[-1].content=="What about that walking goal?":
                self.final_context=tuple(messages)
                with psycopg.connect(dsn,autocommit=True) as conn:
                    row=conn.execute(
                        """SELECT d.title FROM conversations c
                           JOIN dialogue_threads d
                             ON d.thread_id=c.focus_thread_id
                           WHERE c.conversation_id=%s""",
                        (host.active_identity.conversation_id,),
                    ).fetchone()
                    self.focus_during_inference=row[0]
            return super().generate(messages)

    model=ContextModel()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("long-topic-reentry")
    host.send(WALKING,idempotency_key="walk")
    host.send("Maybe three times per week.",idempotency_key="frequency")
    host.send("Actually, twice per week would be better.",idempotency_key="correction")
    host.send("Switch the topic to cooking.",idempotency_key="cook")
    for i in range(16):
        host.send(
            f"The weather is pleasant this afternoon, note {i}.",
            idempotency_key=f"weather-{i}",
        )
    host.restart_host()
    host.send("What about that walking goal?",idempotency_key="reentry")
    assert model.final_context is not None
    prompt=model.final_context[0].content
    assert "Resolved prior conversation reference" in prompt
    assert WALKING in prompt
    assert "Actually, twice per week would be better." in prompt
    assert "Switch the topic to cooking." not in prompt.split(
        "Resolved prior conversation reference",1,
    )[1].split("Stored dialogue metadata",1)[0]
    assert model.focus_during_inference=="cooking"
    assert all(WALKING not in x.content for x in model.final_context[1:])
    with psycopg.connect(dsn,autocommit=True) as conn:
        active=load_active_dialogue(
            conn,TrustedActor(
                user_id=host.active_identity.user_id,
                source_id="mvp01f2-reference-review",
            ),host.active_identity.conversation_id,
        )
        focused=next(x.title for x in active.threads
                     if x.thread_id==active.focus_thread_id)
        assert "walking" in focused.lower()
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s",
            (host.active_identity.user_id,),
        ).fetchone()[0]==0


def test_bounded_context_without_premature_focus_or_history_leak(dsn):
    model=OnePass()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("bounded-target")
    host.send(WALKING,idempotency_key="walk")
    host.send("Switch the topic to cooking.",idempotency_key="cook")
    for i in range(15):
        host.send(
            f"The weather seems nice today, observation {i}.",
            idempotency_key=f"gap-{i}",
        )

    identity=host.active_identity
    actor=TrustedActor(user_id=identity.user_id,source_id="mvp01f2-bounded")
    with psycopg.connect(dsn,autocommit=True) as conn:
        admitted=ingest_text(
            conn,actor,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                idempotency_key="resume-walking",
                text="What about that walking goal?",
            ),
        )
        context=prepare_text_context(
            conn,actor,admitted.turn_id,
            max_turns=12,max_context_chars=2200,
        )
        assert context.messages[-1].content=="What about that walking goal?"
        joined="".join(m.content for m in context.messages)
        assert len(joined)<=2200
        assert "Resolved prior conversation reference" in context.messages[0].content
        assert WALKING in context.messages[0].content
        assert all(WALKING not in m.content for m in context.messages[1:])
        active=load_active_dialogue(conn,actor,identity.conversation_id)
        assert next(x.title for x in active.threads
                    if x.thread_id==active.focus_thread_id)=="cooking"
        assert conn.execute(
            """SELECT count(*) FROM dialogue_turn_links
               WHERE turn_id=%s""",(admitted.turn_id,),
        ).fetchone()[0]==0


def test_ambiguous_reference_omits_resolved_context(dsn):
    model=OnePass()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    host.begin_session("reference-is-ambiguous")
    host.send("I want to practice walking daily.",idempotency_key="w1")
    host.send("I want to track walking habits.",idempotency_key="w2")
    host.send("Switch the topic to cooking.",idempotency_key="cook")
    identity=host.active_identity
    actor=TrustedActor(user_id=identity.user_id,source_id="mvp01f2-ambiguous")
    with psycopg.connect(dsn,autocommit=True) as conn:
        admitted=ingest_text(conn,actor,TextInputCommand(
            conversation_id=identity.conversation_id,
            instance_id=identity.instance_id,
            expected_generation=identity.generation,
            idempotency_key="reference-ambiguous",
            text="What about that walking goal?",
        ))
        context=prepare_text_context(conn,actor,admitted.turn_id)
        assert "Resolved prior conversation reference" not in (
            context.messages[0].content
        )
        assert resolved_objective_context_block(
            conn.cursor(),user_id=identity.user_id,
            instance_id=identity.instance_id,
            conversation_id=identity.conversation_id,
            turn_id=admitted.turn_id,latest_text="What about that walking goal?",
            max_chars=1100,
        )==""
