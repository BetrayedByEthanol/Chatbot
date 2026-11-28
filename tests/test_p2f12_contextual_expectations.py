"""Actual leased Cognitive owner path, not a synthetic write helper."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.cognitive_contracts import CognitiveDraft,COGNITIVE_RESOURCE,COGNITIVE_OPERATION,COGNITIVE_CAPABILITY
from persona_agent.cognitive_engine import (
    schedule_cognition,prepare_cognition,commit_cognition,run_cognition,
)
from persona_agent.contextual_expectations import (
    read_expectation,inspect_expectation_history,withdraw_expectation,parse_activity_report,
)
from persona_agent.expectation_contracts import WithdrawExpectation,ExpectationVersionConflict
from persona_agent.conversation_contracts import TextInputCommand,ModelReply
from persona_agent.conversation import ingest_text,prepare_text_context
from persona_agent.runtime import PermissionDenied
from persona_agent.operation_contracts import StaleOperation
from persona_agent.operation_control import reap_stale_operations
from persona_agent.operation_scheduler import configure_resource_pool,claim_operation
from test_phase2_cognition import actor,platform,call,seed,cancel_pending
from test_p2f11_predictive_admission import set_enabled,reset
from workflow_evidence_helpers import link_memory,invalidate


class EmptyModel:
    def __init__(self):
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        return ModelReply(model_id="expectation-fixture",text='{"proposals":[]}')


def report(day,completed=True,activity="daily walk"):
    occasion=datetime.now(timezone.utc).date()-timedelta(days=day)
    return f"On {occasion.isoformat()}, I {'completed' if completed else 'did not complete'} my {activity}."


def stage(dsn,user,instance,*texts):
    conv=uuid4()
    sources=[]
    for text in (*texts,"Continue discussing."):
        sources.append(call(dsn,ingest_text,actor(user),TextInputCommand(
            instance_id=instance,conversation_id=conv,expected_generation=1,
            idempotency_key=uuid4().hex,text=text)))
    scheduled=call(dsn,schedule_cognition,platform(user),conv,instance,1,min_closed_turns=1,max_turns=16)
    assert scheduled is not None
    lease=call(dsn,claim_operation,worker_id="expectation-acceptance",
               capabilities=(COGNITIVE_CAPABILITY,),
               operation_types=(COGNITIVE_OPERATION,),lease_seconds=120)
    if lease is None:
        with psycopg.connect(dsn,autocommit=True) as conn:
            pool=conn.execute("""SELECT capacity_units,p0_reserved_units,active_units,active_non_p0_units
                                 FROM execution_resource_pools WHERE resource_key=%s""",
                              (COGNITIVE_RESOURCE,)).fetchone()
            status=conn.execute("SELECT status FROM operations WHERE operation_id=%s",
                                (scheduled.operation_id,)).fetchone()
        pytest.fail(f"cognitive claim unavailable: operation={status}, pool(capacity,reserved,active,non_p0)={pool}")
    assert lease.operation_id==scheduled.operation_id
    return conv,sources[:-1],lease


@contextmanager
def available_cognitive_slots(dsn,user,instance,slots):
    """Provide test-local headroom without changing the existing P0 reserve.

    The integration DB and execution pool are shared across test modules.
    Total configured capacity is not the number of free P1 slots. Preserve
    existing reservations, and restore the configuration on every exit.
    """
    with psycopg.connect(dsn,autocommit=True) as conn:
        capacity,reserved,active,non_p0=conn.execute(
            """SELECT capacity_units,p0_reserved_units,active_units,active_non_p0_units
               FROM execution_resource_pools WHERE resource_key=%s""",
            (COGNITIVE_RESOURCE,)).fetchone()
    required=max(capacity,active+slots,non_p0+reserved+slots)
    call(dsn,configure_resource_pool,COGNITIVE_RESOURCE,
         capacity_units=required,p0_reserved_units=reserved)
    try:
        yield
    finally:
        # Only cancel this fixture's operations. In particular, if the
        # second claim fails, the first claim AND queued second job must
        # not contaminate the next test. Use P0 cancellation/recovery,
        # not direct reservation deletion or pool counter manipulation.
        with psycopg.connect(dsn,autocommit=True) as conn:
            pending=conn.execute(
                """SELECT operation_id,status FROM operations
                   WHERE user_id=%s AND instance_id=%s AND operation_type=%s
                     AND status NOT IN ('COMPLETED','FAILED','CANCELLED','OUTCOME_UNKNOWN')""",
                (user,instance,COGNITIVE_OPERATION)).fetchall()
        for op_id,status in pending:
            if status!="CANCEL_REQUESTED":
                cancel_pending(dsn,user,op_id)
        if any(status in ("RUNNING","CANCEL_REQUESTED","SUSPEND_REQUESTED") for _,status in pending):
            call(dsn,reap_stale_operations,limit=1000)
        with psycopg.connect(dsn,autocommit=True) as conn:
            remaining=conn.execute(
                """SELECT count(*) FROM operation_resource_reservations r
                   JOIN operations o ON o.operation_id=r.operation_id
                   WHERE o.user_id=%s AND o.instance_id=%s AND o.operation_type=%s""",
                (user,instance,COGNITIVE_OPERATION)).fetchone()[0]
        assert remaining==0,"expectation fixture leaked resource reservations"
        call(dsn,configure_resource_pool,COGNITIVE_RESOURCE,
             capacity_units=capacity,p0_reserved_units=reserved)


def reflect(dsn,user,instance,*texts):
    conv,sources,lease=stage(dsn,user,instance,*texts)
    model=EmptyModel()
    call(dsn,run_cognition,lease,model)
    assert model.calls==1
    return conv,sources


def get(dsn,user,instance,context="daily walk"):
    return call(dsn,read_expectation,actor(user),instance,context)


def test_dated_report_parser_abstains_on_ambiguous_future_or_stale_reports():
    now=datetime.now(timezone.utc)
    assert parse_activity_report(uuid4(),report(1),now).context_key=="daily walk"
    for text in ("I usually go for walks.","Maybe I will walk tomorrow.",
                 report(-1),report(31),"On 2026-02-31, I completed my daily walk.",
                 report(1)+" Just kidding."):
        assert parse_activity_report(uuid4(),text,now) is None


def test_two_comparable_histories_have_different_scoped_expectations(dsn):
    a,one,_=seed(dsn)
    b,two,_=seed(dsn)
    for user,instance in ((a,one),(b,two)):
        set_enabled(dsn,user,instance,0,True)
    _,sources=reflect(dsn,a,one,report(4),report(3),report(2),report(1,False))
    reflect(dsn,b,two,report(4,False),report(3,False),report(2,False),report(1))
    first,second=get(dsn,a,one),get(dsn,b,two)
    assert first.maturity==second.maturity=="TENTATIVE"
    assert first.direction=="COMPLETION" and second.direction=="NON_COMPLETION"
    assert (first.completion_occasions,first.non_completion_occasions)==(3,1)
    assert (second.completion_occasions,second.non_completion_occasions)==(1,3)
    assert first.evidence_class=="USER_REPORTED" and first.epistemic_status=="HYPOTHESIS"
    assert set(first.source_turn_ids)=={s.turn_id for s in sources}
    assert {r.turn_id for r in first.occasion_evidence if not r.completed}=={sources[-1].turn_id}
    assert get(dsn,a,one,"reading practice") is None
    with pytest.raises(PermissionDenied):
        get(dsn,b,one)
    request=call(dsn,ingest_text,actor(a),TextInputCommand(
        instance_id=one,conversation_id=uuid4(),expected_generation=1,
        idempotency_key="ordinary",text="Hello."))
    context=call(dsn,prepare_text_context,actor(a),request.turn_id)
    assert context.predictive_fence is None


def test_independent_occasions_revision_counterevidence_and_no_duplicate_maturity(dsn):
    user,instance,_=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    _,first=reflect(dsn,user,instance,report(4))
    early=get(dsn,user,instance)
    assert early.version==1 and early.maturity=="EARLY"
    reflect(dsn,user,instance,report(4))
    assert get(dsn,user,instance).revision_id==early.revision_id
    reflect(dsn,user,instance,report(3,False))
    balanced=get(dsn,user,instance)
    assert balanced.version==2 and balanced.direction=="MIXED"
    reflect(dsn,user,instance,report(2,False))
    changed=get(dsn,user,instance)
    assert changed.version==3 and changed.direction=="NON_COMPLETION"
    assert first[0].turn_id in changed.source_turn_ids
    reflect(dsn,user,instance,report(2,True))
    conflicted=get(dsn,user,instance)
    assert conflicted.version==4 and conflicted.maturity=="CONFLICTED"
    assert conflicted.direction=="UNKNOWN" and conflicted.conflicting_occasions==1
    assert len(conflicted.source_turn_ids)==4
    history=call(dsn,inspect_expectation_history,actor(user),instance)
    assert len(history)==4 and history[0]["effective_status"]=="CURRENT"
    assert all(r["effective_status"]=="SUPERSEDED" for r in history[1:])
    assert all("context_key" not in r and "occasion_evidence" not in r for r in history)


def test_default_off_and_disabled_period_reports_are_not_backfilled(dsn):
    user,instance,_=seed(dsn)
    _,_,lease=stage(dsn,user,instance,report(4))
    set_enabled(dsn,user,instance,0,True)
    work=call(dsn,prepare_cognition,lease)
    assert work.expectation_plans==()
    call(dsn,run_cognition,lease,EmptyModel())
    assert get(dsn,user,instance) is None
    reflect(dsn,user,instance,report(3))
    first=get(dsn,user,instance)
    set_enabled(dsn,user,instance,1,False)
    assert get(dsn,user,instance) is None
    _,_,lease=stage(dsn,user,instance,report(2,False))
    set_enabled(dsn,user,instance,2,True)
    call(dsn,run_cognition,lease,EmptyModel())
    assert get(dsn,user,instance).revision_id==first.revision_id
    reset(dsn,user,instance,3)
    assert get(dsn,user,instance) is None
    reflect(dsn,user,instance,report(1))
    new=get(dsn,user,instance)
    assert new.reset_generation==1 and new.version==1
    assert not set(new.source_turn_ids)&set(first.source_turn_ids)


@pytest.mark.parametrize("change",["CORRECT","RETRACT","EXPIRE"])
def test_any_prior_dependency_withdrawal_suppresses_latest_without_fallback(dsn,change):
    user,instance,_=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    _,_sources=reflect(dsn,user,instance,report(4))
    conv,sources=reflect(dsn,user,instance,report(3,False))
    saved=link_memory(dsn,user,instance,conv,sources[0].turn_id)
    assert get(dsn,user,instance).version==2
    set_enabled(dsn,user,instance,1,False)
    invalidate(dsn,user,instance,saved,change)
    set_enabled(dsn,user,instance,2,True)
    assert get(dsn,user,instance) is None
    reflect(dsn,user,instance,report(2))
    assert get(dsn,user,instance) is None
    history=call(dsn,inspect_expectation_history,actor(user),instance)
    assert len(history)==2
    assert history[0]["effective_status"]=="SOURCE_UNAVAILABLE"


@pytest.mark.parametrize("change",["DISABLE","RESET","DISABLE_ENABLE","PRIOR_RETRACT"])
def test_prepared_expectation_owner_write_rechecks_after_inference(dsn,change):
    user,instance,_=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    conv,sources=reflect(dsn,user,instance,report(4))
    if change=="PRIOR_RETRACT":
        saved=link_memory(dsn,user,instance,conv,sources[0].turn_id)
    _,_,lease=stage(dsn,user,instance,report(3))
    work=call(dsn,prepare_cognition,lease)
    assert len(work.expectation_plans)==1
    if change=="DISABLE":
        set_enabled(dsn,user,instance,1,False)
    elif change=="RESET":
        reset(dsn,user,instance,1)
    elif change=="DISABLE_ENABLE":
        set_enabled(dsn,user,instance,1,False)
        set_enabled(dsn,user,instance,2,True)
    else:
        invalidate(dsn,user,instance,saved,"RETRACT")
    # Stale optional learning cannot suppress unrelated ordinary cognition.
    result=call(dsn,commit_cognition,lease,work,CognitiveDraft(),"fixture")
    assert result.end_sequence==work.end_sequence
    recovered=call(dsn,run_cognition,lease,EmptyModel())
    assert recovered.replayed
    history=call(dsn,inspect_expectation_history,actor(user),instance)
    assert len(history)==1 and history[0]["version"]==1


def test_expectation_owner_withdrawal_while_disabled_is_idempotent(dsn):
    user,instance,_=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    reflect(dsn,user,instance,report(4),report(3))
    current=get(dsn,user,instance)
    set_enabled(dsn,user,instance,1,False)
    command=WithdrawExpectation(instance_id=instance,revision_id=current.revision_id,
        expected_version=current.version,expected_generation=1,idempotency_key="withdraw")
    removed=call(dsn,withdraw_expectation,actor(user),command)
    assert removed.version==2
    assert call(dsn,withdraw_expectation,actor(user),command).replayed
    set_enabled(dsn,user,instance,2,True)
    reflect(dsn,user,instance,report(2))
    assert get(dsn,user,instance) is None
    history=call(dsn,inspect_expectation_history,actor(user),instance)
    assert len(history)==2 and history[0]["effective_status"]=="WITHDRAWN"


def test_competing_range_revisions_retry_instead_of_dropping_counterevidence(dsn):
    user,instance,_=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    with available_cognitive_slots(dsn,user,instance,2):
        reflect(dsn,user,instance,report(4))
        _,_,first=stage(dsn,user,instance,report(3))
        _,_,second=stage(dsn,user,instance,report(2,False))
        one=call(dsn,prepare_cognition,first)
        two=call(dsn,prepare_cognition,second)
        assert one.expectation_plans[0].expected_version==two.expectation_plans[0].expected_version==1
        call(dsn,commit_cognition,first,one,CognitiveDraft(),"fixture")
        call(dsn,run_cognition,first,EmptyModel())
        with pytest.raises(ExpectationVersionConflict):
            call(dsn,commit_cognition,second,two,CognitiveDraft(),"fixture")
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT cognitive_watermark FROM conversations WHERE conversation_id=%s",
                                (two.conversation_id,)).fetchone()==(0,)
        fresh=call(dsn,prepare_cognition,second)
        assert fresh.expectation_plans[0].expected_version==2
        call(dsn,commit_cognition,second,fresh,CognitiveDraft(),"fixture")
        call(dsn,run_cognition,second,EmptyModel())
        current=get(dsn,user,instance)
        assert current.version==3 and current.non_completion_occasions==1
        assert current.completion_occasions==2


def test_expired_worker_cannot_commit_expectation(dsn):
    user,instance,_=seed(dsn)
    set_enabled(dsn,user,instance,0,True)
    with available_cognitive_slots(dsn,user,instance,1):
        _,_,lease=stage(dsn,user,instance,report(3),report(2))
        work=call(dsn,prepare_cognition,lease)
        assert work.expectation_plans
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=%s",
                         (lease.operation_id,))
        with pytest.raises(StaleOperation):
            call(dsn,commit_cognition,lease,work,CognitiveDraft(),"fixture")
        assert get(dsn,user,instance) is None
        call(dsn,reap_stale_operations,limit=100)
        cancel_pending(dsn,user,lease.operation_id)
