"""P2-02 PostgreSQL integration: source-linked summaries and episodic recall."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import (
    acknowledge_text_delivery, ingest_text, prepare_text_context, respond_to_text,
)
from persona_agent.conversation_contracts import ModelReply, TextInputCommand
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.operation_scheduler import claim_operation, configure_resource_pool
from persona_agent.operation_contracts import StaleOperation
from persona_agent.summary_contracts import (
    InvalidSummary, StaleSummary, SUMMARY_CAPABILITY, SUMMARY_OPERATION,
    SUMMARY_RESOURCE, SummaryDraft,
)
from persona_agent.summary_memory import (
    commit_summary, episode_source_turn, prepare_summary, run_summary,
    schedule_summary, search_episodes,
)
from persona_agent.runtime import PermissionDenied


def call(dsn, fn, *args, **kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def seed(dsn, *, other=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"p202-{persona}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'Memory')""",(instance,user,persona),
            )
            alternate=None
            if other:
                p2,alternate=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (p2,f"p202-{p2}"),
                )
                conn.execute(
                    """INSERT INTO persona_instances(id,user_id,persona_id,label)
                       VALUES (%s,%s,%s,'Alternative')""",(alternate,user,p2),
                )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",(user,instance),
            )
    call(dsn,configure_resource_pool,SUMMARY_RESOURCE,
         capacity_units=3,p0_reserved_units=1)
    return user,instance,alternate


def actor(user):
    return TrustedActor(user_id=user,source_id="p202-user")


def add_turn(dsn,user,instance,conversation,text):
    return call(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,idempotency_key=str(uuid4()),text=text,
    ))


def history(dsn,user,instance,conversation,n=9):
    rows=[]
    for i in range(n):
        text=("I adopted a rescue dog in September."
              if i==1 else f"Conversation turn {i}: discussed gardening.")
        rows.append(add_turn(dsn,user,instance,conversation,text))
    return rows


class FixedSummary:
    def __init__(self,*,bad_quote=False):
        self.calls=0
        self.bad_quote=bad_quote

    def generate(self,messages):
        self.calls+=1
        rows=json.loads(messages[-1].content)
        dog=next((r for r in rows if "rescue dog" in r["user"]),None)
        episodes=[]
        if dog is not None:
            episodes=[{
                "source_turn_id":dog["turn_id"],
                "evidence_quote":"invented statement" if self.bad_quote else
                                 "I adopted a rescue dog",
                "topic":"dog adoption",
                "description":"User reported adopting a rescue dog",
                "confidence":0.8,
            }]
        return ModelReply(
            model_id="fake-summary-v1",
            text=json.dumps({
                "summary":"The user discussed gardening and a rescue dog.",
                "episodes":episodes,
            }),
        )


def schedule(dsn,user,instance,conversation,**kwargs):
    return call(dsn,schedule_summary,actor(user),conversation,instance,1,
                **kwargs)


def claim(dsn,worker="summary-worker"):
    lease=call(
        dsn,claim_operation,worker_id=worker,
        capabilities=(SUMMARY_CAPABILITY,),
        operation_types=(SUMMARY_OPERATION,),lease_seconds=120,
    )
    assert lease is not None
    return lease


def test_incremental_summary_recall_original_evidence_and_restart(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    turns=history(dsn,user,instance,conversation)
    job=schedule(dsn,user,instance,conversation,max_turns=5)
    assert (job.start_sequence,job.end_sequence)==(1,5)
    assert schedule(dsn,user,instance,conversation,max_turns=5).replayed
    lease=claim(dsn)
    fake=FixedSummary()
    result=call(dsn,run_summary,lease,fake)
    assert fake.calls==1 and result.episode_count==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==5
        assert conn.execute(
            """SELECT count(*) FROM outbox o JOIN events e USING(event_id)
               WHERE e.event_type='memory.conversation_range.summarized.v1'
                 AND e.instance_id=%s""",(instance,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==9

    # Context is reconstructed on fresh connections, keeping recent exact text.
    current=add_turn(dsn,user,instance,conversation,"Please continue.")
    context=call(dsn,prepare_text_context,actor(user),current.turn_id,max_turns=2)
    assert "Turns 1-5" in context.messages[0].content
    assert "gardening and a rescue dog" in context.messages[0].content
    assert context.messages[-1].content=="Please continue."

    other_conversation=uuid4()
    recall=add_turn(dsn,user,instance,other_conversation,
                    "Earlier, did I discuss dog adoption?")
    restored=call(dsn,prepare_text_context,actor(user),recall.turn_id)
    assert "Historical episodes" in restored.messages[0].content
    assert "rescue dog" in restored.messages[0].content
    assert "did I discuss dog adoption?" in restored.messages[-1].content

    hits=call(dsn,search_episodes,actor(user),instance,"dog adoption")
    assert len(hits)==1 and hits[0].source_turn_id==turns[1].turn_id
    evidence=call(dsn,episode_source_turn,actor(user),hits[0].episode_id)
    assert evidence["user_text"]=="I adopted a rescue dog in September."
    assert evidence["assistant_text"] is None


def test_progressive_ranges_do_not_overlap_and_no_unnecessary_summary(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    history(dsn,user,instance,conversation,n=12)
    first=schedule(dsn,user,instance,conversation,max_turns=4)
    assert first.start_sequence==1 and first.end_sequence==4
    call(dsn,run_summary,claim(dsn),FixedSummary())
    second=schedule(dsn,user,instance,conversation,max_turns=4)
    assert second.start_sequence==5 and second.end_sequence==8
    call(dsn,run_summary,claim(dsn),FixedSummary())
    third=schedule(dsn,user,instance,conversation,max_turns=4)
    assert (third.start_sequence,third.end_sequence)==(9,10)
    call(dsn,run_summary,claim(dsn),FixedSummary())
    assert schedule(dsn,user,instance,conversation,max_turns=4) is None
    with psycopg.connect(dsn,autocommit=True) as conn:
        ranges=conn.execute(
            """SELECT start_sequence,end_sequence FROM conversation_summaries
               WHERE conversation_id=%s ORDER BY start_sequence""",
            (conversation,),
        ).fetchall()
        assert ranges==[(1,4),(5,8),(9,10)]
        assert conn.execute(
            "SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==10


def test_recovery_after_commit_before_operation_completion(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    history(dsn,user,instance,conversation)
    scheduled=schedule(dsn,user,instance,conversation,max_turns=5)
    lease=claim(dsn)
    prepared=call(dsn,prepare_summary,lease)
    generated=FixedSummary().generate((
        type("Message",(),{"content":""})(),
        type("Message",(),{"content":json.dumps([
            {"turn_id":str(row["turn_id"]),"user":row["input_text"]}
            for row in prepared.turns
        ])})(),
    ))
    draft=SummaryDraft.model_validate(json.loads(generated.text))
    done=call(dsn,commit_summary,lease,prepared,draft,generated.model_id)
    assert done.episode_count==1

    # Simulate lease loss; normal P0 recovery requeues the same operation.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE operations
               SET status='RETRY_WAIT',worker_id=NULL,lease_until=NULL,
                   claimed_at=NULL,eligible_at=clock_timestamp(),
                   version=version+1
               WHERE operation_id=%s""",(lease.operation_id,),
        )
        conn.execute(
            "DELETE FROM operation_resource_reservations WHERE operation_id=%s",
            (lease.operation_id,),
        )
        conn.execute(
            "DELETE FROM operation_concurrency_locks WHERE operation_id=%s",
            (lease.operation_id,),
        )
        conn.execute(
            """UPDATE execution_resource_pools
               SET active_units=0,active_non_p0_units=0
               WHERE resource_key=%s""",(SUMMARY_RESOURCE,),
        )
    newlease=claim(dsn,worker="recovered-worker")
    no_model=type("NoModel",(),{
        "generate":lambda self,messages: (_ for _ in ()).throw(
            AssertionError("model must not rerun after durable summary commit"))
    })()
    resumed=call(dsn,run_summary,newlease,no_model)
    assert resumed.summary_id==done.summary_id and resumed.replayed
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM conversation_summaries WHERE conversation_id=%s",
            (conversation,),
        ).fetchone()[0]==1


def test_model_evidence_rejected_and_watermark_unchanged(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    history(dsn,user,instance,conv)
    job=schedule(dsn,user,instance,conv,max_turns=5)
    lease=claim(dsn)
    with pytest.raises(InvalidSummary):
        call(dsn,run_summary,lease,FixedSummary(bad_quote=True))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
            (conv,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT status FROM operations WHERE operation_id=%s",
            (job.operation_id,),
        ).fetchone()[0]=="RETRY_WAIT"
        assert conn.execute(
            "SELECT count(*) FROM memory_episodes WHERE conversation_id=%s",
            (conv,),
        ).fetchone()[0]==0
        # This test deliberately stops retries; other tests should not claim it.
        conn.execute(
            "UPDATE operations SET status='FAILED' WHERE operation_id=%s",
            (job.operation_id,),
        )


def test_stale_persona_generation_during_model_inference(dsn):
    user,instance,other=seed(dsn,other=True)
    conv=uuid4()
    history(dsn,user,instance,conv)
    schedule(dsn,user,instance,conv,max_turns=5)
    lease=claim(dsn)
    class SwitchDuringInference(FixedSummary):
        def generate(self,messages):
            call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
                expected_instance_id=instance,target_instance_id=other,
                expected_generation=1,idempotency_key=str(uuid4()),
            ))
            return super().generate(messages)
    with pytest.raises(StaleOperation):
        call(dsn,run_summary,lease,SwitchDuringInference())
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
            (conv,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT count(*) FROM conversation_summaries WHERE conversation_id=%s",
            (conv,),
        ).fetchone()[0]==0


def test_episode_scope_date_bounds_and_relevance(dsn):
    user,instance,other=seed(dsn,other=True)
    outsider,outsider_instance,_=seed(dsn)
    conv=uuid4()
    history(dsn,user,instance,conv)
    schedule(dsn,user,instance,conv,max_turns=5)
    call(dsn,run_summary,claim(dsn),FixedSummary())
    assert call(dsn,search_episodes,actor(user),other,"dog")==()
    assert call(dsn,search_episodes,actor(user),instance,"unrelated")==()
    with pytest.raises(PermissionDenied):
        call(dsn,search_episodes,actor(outsider),instance,"dog")
    hits=call(dsn,search_episodes,actor(user),instance,"dog",
              since=datetime.now(timezone.utc)-timedelta(hours=2),
              until=datetime.now(timezone.utc)+timedelta(hours=2))
    assert len(hits)==1
    assert call(dsn,search_episodes,actor(user),instance,"dog",
                since=datetime.now(timezone.utc)+timedelta(days=1))==()
    with pytest.raises(PermissionDenied):
        call(dsn,episode_source_turn,actor(outsider),hits[0].episode_id)
    with pytest.raises(ValueError):
        call(dsn,search_episodes,actor(user),instance,"dog",max_results=100)


def test_independent_conversation_summaries_and_bounded_context(dsn):
    user,instance,_=seed(dsn)
    a,b=uuid4(),uuid4()
    history(dsn,user,instance,a)
    history(dsn,user,instance,b)
    first=schedule(dsn,user,instance,a,max_turns=5)
    second=schedule(dsn,user,instance,b,max_turns=5)
    assert first.operation_id!=second.operation_id
    # P0 claims use SKIP LOCKED: a racing claimant may return None while
    # another briefly holds the resource-pool row. Claim sequentially,
    # then demonstrate that independent claimed jobs execute concurrently.
    leases=[claim(dsn,"parallel-1"),claim(dsn,"parallel-2")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs=list(pool.map(
            lambda lease: call(dsn,run_summary,lease,FixedSummary()),leases,
        ))
    assert {x.conversation_id for x in outputs}=={a,b}
    current=add_turn(dsn,user,instance,a,"Can we continue our conversation?")
    context=call(dsn,prepare_text_context,actor(user),current.turn_id,
                 max_turns=2,max_context_chars=512)
    assert sum(map(lambda x:len(x.content),context.messages))<=512
    assert context.messages[-1].content=="Can we continue our conversation?"
