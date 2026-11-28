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
from test_phase1_dialogue import actor,incoming,response,seed


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
    """Count actual eligibility invocations, including NULL-root handling."""
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
        ):
            if owner is not None:
                _seed_other_conversations(dsn,*owner,scale)
            result=_measure(conn,scope,request,focus)
            result["stage"]=stage
            result["eligibility_calls"]=_candidate_count(conn,scope,focus,source.turn_id)
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*) AS signals,
                    count(*) FILTER(WHERE user_id=%s AND instance_id=%s) AS scoped_signals
                    FROM conversation_intent_signals""",(user,instance))
                result.update(cur.fetchone())
            report["stages"].append(result)
    # The hard invariant is independent of CPU speed and table cardinality.
    baseline,foreign,local=report["stages"]
    assert foreign["signals"]>=baseline["signals"]+scale
    assert local["scoped_signals"]>=foreign["scoped_signals"]+scale
    assert baseline["eligibility_calls"]>0
    assert foreign["eligibility_calls"]==local["eligibility_calls"]==baseline["eligibility_calls"]
    record_property("07b2_foreground_scaling",json.dumps(report,default=str))
    # With -s, emit a concise human-readable report without the full plan.
    print(json.dumps({**report,"stages":[
        {k:v for k,v in stage.items() if k!="explain"} for stage in report["stages"]
    ]},default=str,indent=2))
