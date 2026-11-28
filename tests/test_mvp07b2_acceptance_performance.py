"""Synthetic, disposable-DB scaling evidence for the 07B2 foreground boundary.

Measure production lookup and context assembly, excluding model inference.
Wall time is reported, not used as a fragile cross-hardware pass threshold.
An instrumented real PostgreSQL predicate independently guards exact candidate
work. No planner settings or function definitions persist beyond the probe.
"""
import json
import math
import os
import platform
from statistics import median
from time import perf_counter_ns
from uuid import uuid4

import pytest

psycopg=pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from persona_agent.conversation import prepare_text_context
from persona_agent.dialogue_integration import _focused_auto_thread
from persona_agent.dialogue_contracts import FocusThreadCommand
from persona_agent.dialogue_state import focus_thread
from test_phase1_dialogue import actor,base,call,incoming,response,revision,seed


class RecordingCursor:
    """Capture the exact production statement, not a hand-maintained copy."""
    def __init__(self,cursor):
        self.cursor=cursor
        self.statement=None
        self.params=None

    def execute(self,statement,params):
        self.statement,self.params=statement,params
        return self.cursor.execute(statement,params)

    def fetchone(self):
        return self.cursor.fetchone()


def latency(samples):
    ordered=sorted(samples)
    return {
        "samples":len(ordered),
        "p50_ms":round(median(ordered),3),
        "p95_ms":round(ordered[math.ceil(.95*len(ordered))-1],3),
        "max_ms":round(ordered[-1],3),
    }


def _measure(conn,scope,request,expected_focus):
    focused_times=[]
    context_times=[]
    with conn.cursor(row_factory=dict_row) as cur:
        probe=RecordingCursor(cur)
        for iteration in range(23):
            start=perf_counter_ns()
            found=_focused_auto_thread(probe,**scope)
            focused_ms=(perf_counter_ns()-start)/1_000_000
            assert found==expected_focus
            start=perf_counter_ns()
            context=prepare_text_context(conn,actor(scope["user_id"]),
                                         request.turn_id,max_turns=1)
            context_ms=(perf_counter_ns()-start)/1_000_000
            assert "walk more" in context.messages[0].content
            assert context.messages[-1].content=="Explain soil drainage."
            assert request.turn_id in context.raw_context_fence.source_turn_ids
            if iteration>=3:
                focused_times.append(focused_ms)
                context_times.append(context_ms)
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) "+probe.statement,
                    probe.params)
        plan=cur.fetchone()["QUERY PLAN"][0]
        return {
            "focused_lookup":latency(focused_times),
            "prepare_text_context":latency(context_times),
            "explain":plan,
            "shared_hits":plan["Plan"].get("Shared Hit Blocks",0),
            "shared_reads":plan["Plan"].get("Shared Read Blocks",0),
        }


def _candidate_count(conn,scope,expected_focus,expected_source):
    """Count actual non-null source eligibility invocations."""
    with conn.transaction(force_rollback=True):
        with conn.cursor(row_factory=dict_row) as cur:
            for name,value in (("uid",scope["user_id"]),("iid",scope["instance_id"]),
                               ("cid",scope["conversation_id"]),("sid",expected_source),
                               ("calls",0)):
                cur.execute("SELECT set_config(%s,%s,true)",
                            ("persona_perf."+name,str(value)))
            cur.execute("""
                CREATE OR REPLACE FUNCTION dialogue_turn_current(
                    uid uuid,iid uuid,cid uuid,sid uuid
                ) RETURNS boolean LANGUAGE plpgsql AS $$
                BEGIN
                    IF sid IS NOT NULL THEN
                        IF uid IS DISTINCT FROM current_setting('persona_perf.uid')::uuid
                           OR iid IS DISTINCT FROM current_setting('persona_perf.iid')::uuid
                           OR cid IS DISTINCT FROM current_setting('persona_perf.cid')::uuid
                           OR sid IS DISTINCT FROM current_setting('persona_perf.sid')::uuid THEN
                            RAISE EXCEPTION 'eligibility evaluated outside benchmark focus';
                        END IF;
                        PERFORM set_config('persona_perf.calls',
                            (current_setting('persona_perf.calls')::int+1)::text,true);
                    END IF;
                    RETURN sid IS NOT NULL AND raw_context_sources_current(uid,iid,cid,ARRAY[sid]);
                END;
                $$;
            """)
            assert _focused_auto_thread(cur,**scope)==expected_focus
            cur.execute("SELECT current_setting('persona_perf.calls')::int AS n")
            return cur.fetchone()["n"]


def _seed_other_conversations(dsn,user,instance,count):
    for _ in range(count):
        entered=incoming(dsn,user,instance,uuid4(),"I want to learn Python.")
        response(dsn,user,entered,"We can start with Python basics.")


def test_scoped_foreground_scaling_report(dsn,record_property):
    # Larger target-host experiments can reuse the same test. Fixture setup
    # uses actual ingress/delivery; it is deliberately not timed as retrieval.
    scale=int(os.environ.get("PERSONA_07B2_SCALE","32"))
    assert 1<=scale<=1024
    user,instance=seed(dsn)
    foreign_user,foreign_instance=seed(dsn)
    conv=uuid4()
    source=incoming(dsn,user,instance,conv,"I want to walk more.")
    response(dsn,user,source,"A short walk would be a good starting point.")
    request=incoming(dsn,user,instance,conv,"Explain soil drainage.")
    scope={"user_id":user,"instance_id":instance,"conversation_id":conv}
    report={"scale_per_stage":scale,"python":platform.python_version(),
            "platform":platform.platform(),"logical_cpus":os.cpu_count(),"stages":[]}
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT version() AS postgres,
                current_setting('jit') AS jit,
                current_setting('plan_cache_mode') AS plan_cache_mode""")
            report.update(cur.fetchone())
            focus=_focused_auto_thread(cur,**scope)
            assert focus is not None
        for stage,owner in (
            ("baseline",None),
            ("other_user",(foreign_user,foreign_instance)),
            ("same_persona_other_conversations",(user,instance)),
            ("same_conversation",None),
        ):
            if owner is not None:
                _seed_other_conversations(dsn,*owner,scale)
            if stage=="same_conversation":
                for index in range(scale):
                    entered=incoming(dsn,user,instance,conv,
                                     f"I want to learn Python lesson {index+1}.")
                    response(dsn,user,entered,"We can discuss that lesson.")
                # Other turns supersede the old pending anchor. Re-focus
                # the same exact thread using a new identical request.
                request=incoming(dsn,user,instance,conv,"Explain soil drainage.")
                call(dsn,focus_thread,actor(user),FocusThreadCommand(
                    **base(user,instance,conv,request,revision(dsn,user,conv)),
                    thread_id=focus,
                ))
            result=_measure(conn,scope,request,focus)
            result["stage"]=stage
            result["eligibility_calls"]=_candidate_count(conn,scope,focus,source.turn_id)
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*) AS signals,
                    count(*) FILTER(WHERE user_id=%s AND instance_id=%s) AS scoped_signals,
                    count(*) FILTER(WHERE user_id=%s AND instance_id=%s
                        AND conversation_id=%s) AS conversation_signals
                    FROM conversation_intent_signals""",(user,instance,user,instance,conv))
                result.update(cur.fetchone())
            report["stages"].append(result)
    # The hard invariant is independent of CPU speed and table cardinality.
    record_property("07b2_foreground_scaling",json.dumps(report,default=str))
    baseline,foreign,local,conversation=report["stages"]
    assert foreign["signals"]>=baseline["signals"]+scale
    assert local["scoped_signals"]>=foreign["scoped_signals"]+scale
    assert conversation["conversation_signals"]>=local["conversation_signals"]+scale
    assert baseline["eligibility_calls"]>0
    assert all(stage["eligibility_calls"]==baseline["eligibility_calls"]
               for stage in report["stages"])
    # With -s, emit a concise human-readable report without the full plan.
    print(json.dumps({**report,"stages":[
        {k:v for k,v in stage.items() if k!="explain"} for stage in report["stages"]
    ]},default=str,indent=2))



def _timed(action):
    samples=[]
    last=None
    for iteration in range(23):
        start=perf_counter_ns()
        last=action()
        elapsed=(perf_counter_ns()-start)/1_000_000
        if iteration>=3:
            samples.append(elapsed)
    return last,latency(samples)


def test_long_lived_affect_reappraisal_budget_report(dsn,record_property,monkeypatch):
    """Measure the real foreground cap and explicit owner replay separately."""
    from persona_agent.affect_context import prepare_affect_context
    from persona_agent.affect_lineage import FOREGROUND_APPRAISAL_LIMIT
    import persona_agent.emotion_state as emotion
    from persona_agent.relationship_state import read_relationship_state
    from persona_agent.event_delivery import dispatch_outbox
    from test_phase2_affect import register,seed as affect_seed,turn,tick
    from workflow_evidence_helpers import link_memory,invalidate

    consumers=register(dsn)
    user,instance,_=affect_seed(dsn)
    conv=uuid4()
    count=FOREGROUND_APPRAISAL_LIMIT+1
    first=turn(dsn,user,instance,conv,"I'm happy.")
    saved=link_memory(dsn,user,instance,conv,first.turn_id)
    for index in range(1,count):
        turn(dsn,user,instance,conv,
             "I'm happy." if index%2==0 else "Thank you.")
    # Outbox dispatch advances at most one undispatched event per stream
    # per invocation. Superseding a pending input also emits a journal event.
    # A worker's default 16 dispatch batches therefore do not imply that
    # 64 same-stream user inputs are ready to claim. Drain fixture events
    # separately, preserving production ordering and bounded worker claims.
    dispatch_passes=0
    with psycopg.connect(dsn,autocommit=True) as conn:
        for _ in range(4*count+16):
            if dispatch_outbox(conn,limit=1000)==0:
                break
            dispatch_passes+=1
        else:
            pytest.fail("fixture outbox did not drain within bounded dispatch passes")
        queued=dict(conn.execute(
            """SELECT d.consumer_id,count(*)
               FROM event_deliveries d JOIN events e ON e.event_id=d.event_id
               WHERE d.consumer_id=ANY(%s::text[]) AND e.user_id=%s
                 AND e.instance_id=%s
                 AND e.event_type='conversation.user_text.received.v1'
                 AND d.status='PENDING'
               GROUP BY d.consumer_id""",
            (list(consumers),user,instance),
        ).fetchall())
        assert queued=={consumer:count for consumer in consumers},queued

    applied=recorded=0
    for _ in range(math.ceil(count/64)+1):
        outcome=tick(dsn,consumers)
        assert outcome.retry_pending==0
        applied+=outcome.emotion_applied
        recorded+=outcome.relationship_recorded
        if applied==count and recorded==count:
            break
    assert (applied,recorded)==(count,count)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM emotion_appraisals
               WHERE user_id=%s AND instance_id=%s AND applied""",
            (user,instance),
        ).fetchone()==(count,)
        assert conn.execute(
            """SELECT count(*) FROM relationship_observations
               WHERE user_id=%s AND instance_id=%s""",
            (user,instance),
        ).fetchone()==(count,)

    def unexpected_replay(*args,**kwargs):
        raise AssertionError("bounded foreground path entered full appraisal replay")

    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        def presentation():
            with conn.transaction(),conn.cursor() as cur:
                return prepare_affect_context(cur,user,instance,650)[0]

        with monkeypatch.context() as patch:
            patch.setattr(emotion,"current_sources",unexpected_replay)
            warm,warm_latency=_timed(presentation)
            assert "Simulated persona expressive affect" in warm
            assert "Tentative user-affect signal" in warm
            invalidate(dsn,user,instance,saved,"EXPIRE")
            stale,stale_latency=_timed(presentation)
            assert "Simulated persona expressive affect" not in stale
            assert "Tentative user-affect signal" in stale
        # These are explicit owner snapshots, not the foreground path.
        # They may replay the scoped history, so report rather than hide cost.
        mood,mood_latency=_timed(lambda:emotion.read_emotion_state(
            conn,actor(user),instance))
        relationship,relationship_latency=_timed(lambda:read_relationship_state(
            conn,actor(user),instance))
        assert mood.version==count
        assert relationship.interaction_count==count
        report={
            "appraisals":count,"foreground_replay_limit":FOREGROUND_APPRAISAL_LIMIT,
            "fixture_dispatch_passes":dispatch_passes,
            "current_projection":warm_latency,"stale_overflow":stale_latency,
            "emotion_owner_snapshot":mood_latency,
            "relationship_owner_snapshot":relationship_latency,
        }
        record_property("07b2_affect_scaling",json.dumps(report))
        print(json.dumps(report,indent=2))
