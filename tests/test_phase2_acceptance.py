"""P2-08: cross-domain Phase 2 acceptance against real PostgreSQL.

Unlike individual P2-01..07 tests, these scenarios drive several domain
owners across the same authenticated user conversation and fresh connections.
Architecture v2.5 Phase 2 exit gate:
  (a) long conversation survives compression/restart and old evidence recall;
  (b) independent cognition, memory and affect updates do not overwrite
      each other or bypass their owner/selection authority.
No model inference or speculative claims on the foreground preparation path.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from persona_agent.affect_observers import (
    register_affect_consumers,run_affect_tick,
)
from persona_agent.cognitive_contracts import (
    COGNITIVE_CAPABILITY,COGNITIVE_OPERATION,COGNITIVE_RESOURCE,
)
from persona_agent.cognitive_engine import (
    run_cognition,schedule_cognition,list_cognitive_proposals,
)
from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text,prepare_text_context
from persona_agent.conversation_contracts import ModelReply,TextInputCommand
from persona_agent.dialogue_contracts import OpenThreadCommand
from persona_agent.dialogue_state import open_thread,load_active_dialogue
from persona_agent.emotion_state import read_emotion_state
from persona_agent.event_delivery import dispatch_outbox
from persona_agent.lifecycle import (
    SwitchPersonaCommand,TrustedPlatformActor,switch_persona,
)
from persona_agent.memory import search_memories
from persona_agent.memory_extraction import (
    register_extraction_consumer,run_extraction_tick,
)
from persona_agent.memory_retrieval import (
    load_topic_attention,search_relevant_history,
)
from persona_agent.operation_scheduler import claim_operation,configure_resource_pool
from persona_agent.relationship_state import read_relationship_state
from persona_agent.runtime import PermissionDenied,StaleSelection
from persona_agent.summary_contracts import (
    SUMMARY_CAPABILITY,SUMMARY_OPERATION,SUMMARY_RESOURCE,
)
from persona_agent.summary_memory import (
    episode_source_turn,run_summary,schedule_summary,search_episodes,
)


def dbcall(dsn,fn,*args,**kwargs):
    # Each action uses a fresh connection to exercise durable restoration.
    with psycopg.connect(dsn,autocommit=True) as connection:
        return fn(connection,*args,**kwargs)


def actor(user):
    return TrustedActor(user_id=user,source_id="phase2-acceptance")


def platform(user):
    return TrustedPlatformActor(user_id=user,source_id="cognition.auto_reflection.v1")


def create_identity(dsn,other=False):
    user=uuid4()
    instance=uuid4()
    persona=uuid4()
    alternative=None
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"phase2-acceptance-{persona}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'Reflective')""",
                (instance,user,persona),
            )
            if other:
                alternative=uuid4()
                p2=uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (p2,f"phase2-acceptance-{p2}"),
                )
                conn.execute(
                    """INSERT INTO persona_instances(id,user_id,persona_id,label)
                       VALUES (%s,%s,%s,'Alternative')""",
                    (alternative,user,p2),
                )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",
                (user,instance),
            )
    return user,instance,alternative


def add_turn(dsn,user,instance,conversation,text,generation=1):
    return dbcall(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=generation,idempotency_key=str(uuid4()),
        text=text,
    ))


def init_consumers(dsn):
    # The CI PostgreSQL fixture is reused between test cases. Subscribing
    # unique IDs after dispatching earlier outbox entries prevents the new
    # consumers from accidentally ingesting pre-existing users' events.
    names=tuple("p208-"+domain+"-"+uuid4().hex for domain in (
        "memory","emotion","relationship",
    ))
    with psycopg.connect(dsn,autocommit=True) as conn:
        for _ in range(1000):
            if dispatch_outbox(conn,limit=1000)==0:
                break
        else:
            raise AssertionError("shared P0 outbox did not drain")
        register_extraction_consumer(conn,consumer_id=names[0])
        register_affect_consumers(
            conn,emotion_consumer_id=names[1],
            relationship_consumer_id=names[2],
        )
    return names


def apply_inputs(dsn,consumers):
    memory=dbcall(
        dsn,run_extraction_tick,worker_id="p208-memory",
        consumer_id=consumers[0],max_deliveries=128,dispatch_batches=64,
    )
    affect=dbcall(
        dsn,run_affect_tick,worker_id="p208-affect",
        emotion_consumer_id=consumers[1],
        relationship_consumer_id=consumers[2],
        max_deliveries_per_domain=128,dispatch_batches=64,
    )
    assert memory.retry_pending==0 and affect.retry_pending==0
    return memory,affect


def prepare_resources(dsn):
    dbcall(
        dsn,configure_resource_pool,COGNITIVE_RESOURCE,
        capacity_units=3,p0_reserved_units=1,
    )


def claimed(dsn,op_type,capability,worker):
    lease=dbcall(
        dsn,claim_operation,worker_id=worker,
        capabilities=(capability,),operation_types=(op_type,),
        lease_seconds=120,
    )
    assert lease is not None
    return lease


class DeterministicSummary:
    """Strict source-linked fake; real Ollama is covered by separate CI."""

    def __init__(self):
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        rows=json.loads(messages[-1].content)
        dog=next((x for x in rows if "rescue dog" in x["user"]),None)
        episodes=[] if dog is None else [{
            "source_turn_id":dog["turn_id"],
            "evidence_quote":"adopted a rescue dog",
            "topic":"rescue dog adoption",
            "description":"User reported adopting a rescue dog",
            "confidence":0.8,
        }]
        return ModelReply(
            model_id="p208-deterministic-summary",
            text=json.dumps({
                "summary":"Conversation covered gardening and a rescue dog.",
                "episodes":episodes,
            }),
        )


class DeterministicCognition:
    """Interpretation must not become a verified memory or user affect."""

    def __init__(self):
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        source=json.loads(messages[-1].content)
        garden=next(x for x in source if "Garden progress" in x["user"])
        return ModelReply(
            model_id="p208-deterministic-cognition",
            text=json.dumps({"proposals":[{
                "kind":"HYPOTHESIS","subject":"USER",
                "content":"Garden scheduling might affect progress.",
                "source_turn_id":garden["turn_id"],
                "evidence_quote":"Garden progress",
                "confidence":0.55,
                "temporal_scope":"CURRENT",
                "trigger_text":None,"expiry_days":None,
            }]}),
        )


def test_phase2_same_conversation_independent_domain_updates_and_restart(dsn):
    """The Phase 2 domains operate on one evidence stream, not duplicate state."""
    consumers=init_consumers(dsn)
    user,instance,_=create_identity(dsn)
    prepare_resources(dsn)
    conversation=uuid4()
    statements=[
        "My favorite drink is tea.",
        "I'm happy.",
        "Please keep replies short.",
        "I adopted a rescue dog in September.",
        "Garden progress was difficult this week.",
        "Garden progress got easier yesterday.",
        "Thank you.",
        "We discussed gardening methods.",
        "I am frustrated.",
        "We can discuss something else now.",
    ]
    source_turns=[
        add_turn(dsn,user,instance,conversation,utterance)
        for utterance in statements
    ]
    memory_tick,affect_tick=apply_inputs(dsn,consumers)
    assert memory_tick.admitted==1
    assert affect_tick.relationship_recorded==len(statements)
    assert affect_tick.emotion_applied==3
    first_memory=dbcall(
        dsn,search_memories,actor(user),instance,"favorite drink",
    )
    assert len(first_memory)==1
    assert first_memory[0].value_text=="tea"
    assert first_memory[0].source_turn_id==source_turns[0].turn_id

    summary=dbcall(
        dsn,schedule_summary,actor(user),conversation,instance,1,
        max_turns=8,retain_recent=2,
    )
    cognition=dbcall(
        dsn,schedule_cognition,platform(user),conversation,instance,1,
        max_turns=8,retain_recent=1,min_closed_turns=3,
    )
    assert summary is not None and cognition is not None
    assert (summary.start_sequence,summary.end_sequence)==(1,8)
    assert (cognition.start_sequence,cognition.end_sequence)==(1,8)
    summary_lease=claimed(dsn,SUMMARY_OPERATION,SUMMARY_CAPABILITY,"p208-summary")
    cognition_lease=claimed(dsn,COGNITIVE_OPERATION,COGNITIVE_CAPABILITY,"p208-cognition")
    assert summary_lease.operation_id==summary.operation_id
    assert cognition_lease.operation_id==cognition.operation_id
    summary_model=DeterministicSummary()
    cognition_model=DeterministicCognition()
    # Independent domains share a source conversation and resource pool.
    # Concurrent PostgreSQL transactions must not lose either watermark.
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(dbcall,dsn,run_summary,summary_lease,summary_model)
        b=pool.submit(dbcall,dsn,run_cognition,cognition_lease,cognition_model)
        summary_result=a.result(timeout=30)
        cognition_result=b.result(timeout=30)
    assert summary_result.episode_count==1
    assert cognition_result.reflection_count==1
    assert summary_model.calls==cognition_model.calls==1

    with psycopg.connect(dsn,autocommit=True) as conn:
        state=conn.execute(
            """SELECT summary_watermark,cognitive_watermark
               FROM conversations WHERE conversation_id=%s""",
            (conversation,),
        ).fetchone()
        assert state==(8,8)
        assert conn.execute(
            """SELECT count(*) FROM memory_records
               WHERE user_id=%s AND instance_id=%s AND privacy_class='NORMAL'""",
            (user,instance),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM cognitive_reflections
               WHERE user_id=%s AND instance_id=%s
                 AND epistemic_status='MODEL_INTERPRETED'
                 AND privacy_class='INTERNAL'""",
            (user,instance),
        ).fetchone()[0]==1

    mood=dbcall(dsn,read_emotion_state,actor(user),instance)
    relationship=dbcall(dsn,read_relationship_state,actor(user),instance)
    assert mood.version==3
    assert relationship.interaction_count==10
    assert any(p.preference_key=="RESPONSE_LENGTH"
               and p.preference_value=="SHORT" for p in relationship.preferences)
    assert len(dbcall(dsn,list_cognitive_proposals,actor(user),instance))==1

    # Simulate a new session/process: only PostgreSQL persists; all reads
    # reopen a connection. Memory and episode keep their epistemic types.
    later_conversation=uuid4()
    recalled=add_turn(
        dsn,user,instance,later_conversation,
        "Earlier, did I discuss rescue dog adoption or favorite drink?",
    )
    context=dbcall(dsn,prepare_text_context,actor(user),recalled.turn_id)
    assert context.messages[-1].content.startswith("Earlier, did I discuss")
    assert "response length short" in context.messages[0].content
    hits=dbcall(
        dsn,search_relevant_history,actor(user),instance,
        "rescue dog adoption favorite drink",
        max_results=6,max_chars=2400,
    )
    sources={h.source_type:h for h in hits}
    assert "USER_MEMORY" in sources and "EPISODE" in sources
    assert sources["USER_MEMORY"].epistemic_status=="USER_REPORTED"
    assert sources["USER_MEMORY"].source_turn_id==source_turns[0].turn_id
    assert sources["EPISODE"].epistemic_status=="MODEL_INTERPRETED"
    assert sources["EPISODE"].source_turn_id==source_turns[3].turn_id
    assert all(h.source_turn_at.tzinfo is not None for h in hits)
    # A model-origin hypothesis was not silently converted into verified memory.
    assert all("scheduling might" not in h.excerpt for h in hits)


def test_phase2_50_turn_compression_topic_restore_and_provenance(dsn):
    """Phase 2 exit gate: long-running dialogue remains evidence-recoverable."""
    user,instance,_=create_identity(dsn)
    prepare_resources(dsn)
    conversation=uuid4()
    source=[]
    for i in range(50):
        text=("I adopted a rescue dog in September."
              if i==1 else f"Garden progress update {i}: planning a new routine.")
        source.append(add_turn(dsn,user,instance,conversation,text))
    # Dialogue commands are causal: their evidence must be the latest
    # authenticated input, not an arbitrarily old turn. Revisit the old
    # topic explicitly before creating its new focused thread.
    topic_reference=add_turn(
        dsn,user,instance,conversation,
        "Let's return to rescue dog adoption.",
    )
    thread=uuid4()
    current=dbcall(dsn,load_active_dialogue,actor(user),conversation)
    dbcall(dsn,open_thread,actor(user),OpenThreadCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,expected_revision=current.revision,
        evidence_turn_id=topic_reference.turn_id,
        idempotency_key=str(uuid4()),
        thread_id=thread,title="rescue dog adoption",
    ))
    generated=DeterministicSummary()
    processed=[]
    for batch in range(6):
        staged=dbcall(
            dsn,schedule_summary,actor(user),conversation,instance,1,
            retain_recent=2,max_turns=8,
        )
        assert staged is not None
        assert staged.start_sequence==batch*8+1
        assert staged.end_sequence==(batch+1)*8
        lease=claimed(dsn,SUMMARY_OPERATION,SUMMARY_CAPABILITY,
                      "p208-long-summary")
        assert lease.operation_id==staged.operation_id
        processed.append(dbcall(dsn,run_summary,lease,generated))
    assert generated.calls==6
    assert sum(x.episode_count for x in processed)==1
    assert dbcall(
        dsn,schedule_summary,actor(user),conversation,instance,1,
        retain_recent=2,max_turns=8,
    ) is None
    resumed=add_turn(dsn,user,instance,conversation,
                     "Please continue from our previous conversation.")
    context=dbcall(
        dsn,prepare_text_context,actor(user),resumed.turn_id,
        max_turns=3,max_context_chars=3600,
    )
    assert context.messages[-1].content=="Please continue from our previous conversation."
    assert "Conversation" in context.messages[0].content
    assert sum(len(m.content) for m in context.messages)<=3600

    # After fresh connections, dialogue focus, exact evidence and dated
    # episodic retrieval remain, despite 48 turns being summarized.
    snapshot=dbcall(dsn,load_topic_attention,actor(user),conversation)
    assert snapshot.focus_thread_id==thread
    assert snapshot.topics[0].thread_id==thread
    episodes=dbcall(dsn,search_episodes,actor(user),instance,"rescue dog")
    assert len(episodes)==1
    assert episodes[0].source_turn_id==source[1].turn_id
    evidence=dbcall(dsn,episode_source_turn,actor(user),instance,
                    episodes[0].episode_id)
    assert evidence["user_text"]=="I adopted a rescue dog in September."
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==48
        assert conn.execute(
            "SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==52


def test_phase2_switch_defers_other_domain_events_without_cross_persona_leak(dsn):
    """Switch isolation across Memory, Emotion and Relationship consumers."""
    consumers=init_consumers(dsn)
    user,instance,other=create_identity(dsn,other=True)
    conversation=uuid4()
    source=add_turn(dsn,user,instance,conversation,"My favorite drink is tea.")
    mood_source=add_turn(dsn,user,instance,conversation,"I'm happy.")
    boundary=add_turn(dsn,user,instance,conversation,"Please keep replies short.")
    switch=dbcall(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert switch.generation==2
    deferred_memory,deferred_affect=apply_inputs(dsn,consumers)
    # P0 per-stream head-of-line ordering may defer only the first
    # delivery; later turns remain blocked behind it until reselection.
    assert deferred_memory.deferred>=1
    assert deferred_affect.deferred>=2
    assert dbcall(dsn,read_emotion_state,actor(user),instance).version==0
    assert dbcall(dsn,read_relationship_state,actor(user),instance).interaction_count==0
    assert dbcall(dsn,search_memories,actor(user),instance,"favorite drink")==()
    assert dbcall(dsn,read_relationship_state,actor(user),other).interaction_count==0
    assert dbcall(dsn,search_memories,actor(user),other,"favorite drink")==()

    restored=dbcall(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=other,target_instance_id=instance,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    assert restored.generation==3
    memory,affect=apply_inputs(dsn,consumers)
    assert memory.admitted==1
    assert affect.relationship_recorded==3
    assert affect.emotion_applied==1
    assert dbcall(dsn,search_memories,actor(user),instance,"favorite drink")[0].source_turn_id==source.turn_id
    assert dbcall(dsn,read_emotion_state,actor(user),instance).tentative_user_affect=="HAPPY"
    assert dbcall(dsn,read_relationship_state,actor(user),instance).interaction_count==3
    assert dbcall(dsn,read_relationship_state,actor(user),other).interaction_count==0
    assert dbcall(dsn,search_memories,actor(user),other,"favorite drink")==()
    assert apply_inputs(dsn,consumers)[1].relationship_recorded==0
    with pytest.raises(PermissionDenied):
        outsider,_i,_o=create_identity(dsn)
        dbcall(dsn,read_emotion_state,actor(outsider),instance)


def test_phase2_foreground_selectivity_does_not_run_background_models(dsn,monkeypatch):
    """Ordinary context assembly cannot trigger summary/cognition inference."""
    user,instance,_=create_identity(dsn)
    conv=uuid4()
    source=add_turn(dsn,user,instance,conv,"Please explain TCP socket options.")
    import persona_agent.memory_retrieval as retrieval_module
    import persona_agent.summary_memory as summary_module
    import persona_agent.cognitive_engine as cognition_module

    def unexpected(*args,**kwargs):
        raise AssertionError("foreground attempted optional background processing")

    monkeypatch.setattr(retrieval_module,"_candidates",unexpected)
    monkeypatch.setattr(summary_module,"run_summary",unexpected)
    monkeypatch.setattr(cognition_module,"run_cognition",unexpected)
    start=monotonic()
    context=dbcall(dsn,prepare_text_context,actor(user),source.turn_id,
                   max_context_chars=1200)
    elapsed_ms=round((monotonic()-start)*1000,2)
    assert context.messages[-1].content=="Please explain TCP socket options."
    assert sum(len(m.content) for m in context.messages)<=1200
    assert "Potentially relevant historical context" not in context.messages[0].content
    # Report latency as an informative CI measurement; fixed absolute model
    # latency targets belong to hardware-specific benchmark acceptance.
    print(json.dumps({"phase2_foreground_context_ms":elapsed_ms,
                      "model_calls":0,"historical_topic_queries":0}))
