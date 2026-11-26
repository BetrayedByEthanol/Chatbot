"""MVP-06C: owner-derived recurring tasks and current evidence projections."""
from datetime import date,timedelta
from uuid import uuid4

import pytest

from persona_agent.goal_cycle_contracts import recurring_count,cycle_bounds
from persona_agent.goal_decomposition_contracts import proven_count_milestone
from persona_agent.goal_conversation_contracts import APPROVE

@pytest.mark.parametrize("outcome,criterion,measure",[
    ("Walk 3 times per week","Complete at least 3 walks per week",("complete","walk","week",3)),
    ("Exercise 3 times per week","Complete at least 3 exercise sessions per week",
     ("complete","exercise session","week",3)),
    ("Read 2 books per month","Read at least two books per month",("read","book","month",2)),
    ("Prepare 2 meals per week","Prepare at least 2 meals per week",("prepare","meal","week",2)),
    ("Walk 3 times per week","Complete at least 2 walks per week",None),
    ("Read 3 times per week","Read 3 books per week",None),
    ("Eat fewer sweets","At most two sweets per week",None),
    ("Avoid 3 meals per week","Avoid at least 3 meals per week",None),
    ("Read 13 books per month","Read at least 13 books per month",None),
    ("Complete 4 hour marathons per month","Complete at least 4 hour marathons per month",None),
])
def test_bounded_recurring_measure(outcome,criterion,measure):
    assert recurring_count(outcome,criterion)==measure


def test_cycle_windows_and_normalized_contribution():
    assert cycle_bounds("day",date(2026,12,31))==(date(2026,12,31),date(2027,1,1))
    assert cycle_bounds("week",date(2026,11,26))==(date(2026,11,23),date(2026,11,30))
    assert cycle_bounds("month",date(2028,2,29))==(date(2028,2,1),date(2028,3,1))
    assert proven_count_milestone(
        "Walk 3 times per week","Complete at least 3 walks per week",
        "Complete 2 walks per week","Complete at least 2 walks per week",
    )
    assert proven_count_milestone(
        "Exercise 3 times per week","Complete at least 3 exercise sessions per week",
        "Complete 1 exercise session per week","Complete at least 1 exercise session per week",
    )
    assert not proven_count_milestone(
        "Exercise 3 times per week","Complete at least 3 exercise sessions per week",
        "Complete 3 exercise sessions per week","Complete at least 3 exercise sessions per week",
    )


def helpers():
    pytest.importorskip("psycopg")
    from test_phase3_tasks import actor,call,seed,turn,approve
    return actor,call,seed,turn,approve


def approved(dsn,text="Set a goal to walk twice a week."):
    from test_mvp06a_goal_conversation import answer,candidates,goals
    actor,call,seed,_,_=helpers()
    from persona_agent.goal_cycle import read_recurring_goal
    from persona_agent.runtime import PermissionDenied
    user,instance,_=seed(dsn)
    conv=uuid4()
    answer(dsn,user,instance,conv,text)
    pending,=candidates(dsn,user,instance)
    assert goals(dsn,user,instance)==()
    with pytest.raises(PermissionDenied):
        call(dsn,read_recurring_goal,actor(user),instance,pending.goal_id)
    answer(dsn,user,instance,conv,APPROVE)
    goal,=goals(dsn,user,instance)
    return user,instance,conv,goal.goal_id


def plan(dsn,user,instance,goal,**kwargs):
    from persona_agent.goal_cycle import read_recurring_goal
    actor,_,_,_,_=helpers()
    import psycopg
    with psycopg.connect(dsn,autocommit=True) as conn:
        return read_recurring_goal(conn,actor(user),instance,goal,**kwargs)


def command_for(dsn,user,instance,conv,draft,*,approved=True):
    from persona_agent.task_contracts import CreateRecurringTaskCommand
    actor,call,_,turn,approve=helpers()
    text="Please create this specific recurring occurrence task."
    source=turn(dsn,user,instance,conv,text)
    cmd=CreateRecurringTaskCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,evidence_quote=text,
        task_id=uuid4(),**draft.model_dump(),
    )
    return approve(dsn,user,instance,cmd) if approved else cmd


def create(dsn,user,cmd):
    from persona_agent.task_state import create_task
    actor,call,_,_,_=helpers()
    return call(dsn,create_task,actor(user),cmd)


def test_recurring_goal_lazy_tasks_and_corrected_progress(dsn):
    import psycopg
    from test_phase3_tasks import report_done,confirm_done,correction_command
    from persona_agent.task_state import correct_task_outcome,list_tasks
    from persona_agent.runtime import PermissionDenied,VersionConflict
    from persona_agent.goal_state import list_goals
    from test_mvp06a_goal_conversation import answer
    from persona_agent.conversation_contracts import ModelReply
    actor,call,_,_,_=helpers()
    user,instance,conv,goal=approved(dsn)
    first=plan(dsn,user,instance,goal)
    assert first.decision=="PROPOSE_ONE_TASK" and first.target==2
    assert first.next_task.ordinal==1 and first.next_task.executor=="USER"
    assert first.reported_count==first.confirmed_count==first.independently_verified_count==0
    assert call(dsn,list_tasks,actor(user),instance,goal)==()
    unapproved=command_for(dsn,user,instance,conv,first.next_task,approved=False)
    with pytest.raises(PermissionDenied):
        create(dsn,user,unapproved)
    # A separately approved but mismatched task cannot mutate the target.
    _,_,_,_,approve=helpers()
    wrong=unapproved.model_copy(update={"action_description":"Complete three walks instead."})
    with pytest.raises(VersionConflict,match="owner-derived"):
        create(dsn,user,approve(dsn,user,instance,wrong))
    cmd=command_for(dsn,user,instance,conv,first.next_task)
    result=create(dsn,user,cmd)
    assert result.version==1
    assert create(dsn,user,cmd).replayed
    current=plan(dsn,user,instance,goal)
    assert current.decision=="WAIT_FOR_EVIDENCE" and current.next_task is None
    assert current.reported_count==current.confirmed_count==0
    tasks=call(dsn,list_tasks,actor(user),instance,goal)
    assert len(tasks)==1 and tasks[0].actionable and not tasks[0].dispatch_authorized

    # Conversation about a new target is not an edit or a completion.
    class Chat:
        def generate(self,messages):
            return ModelReply("casual-test","We can discuss walking more.")
    answer(dsn,user,instance,conv,"Maybe I should walk five times instead.",model=Chat())
    assert plan(dsn,user,instance,goal).target==2
    assert plan(dsn,user,instance,goal).reported_count==0

    report=report_done(dsn,user,instance,conv,cmd.task_id,1)
    reported=plan(dsn,user,instance,goal)
    assert reported.reported_count==1 and reported.confirmed_count==0
    assert reported.occurrences[0].report_id==report.report_id
    assert reported.decision=="WAIT_FOR_EVIDENCE"
    confirmation=confirm_done(dsn,user,instance,conv,cmd.task_id,report.version,report.report_id)
    next_plan=plan(dsn,user,instance,goal)
    assert next_plan.reported_count==next_plan.confirmed_count==1
    assert next_plan.next_task.ordinal==2 and next_plan.independently_verified_count==0
    second=command_for(dsn,user,instance,conv,next_plan.next_task)
    correction=correction_command(dsn,user,instance,conv,cmd.task_id,confirmation.version,
                                  confirmation_id=confirmation.confirmation_id)
    corrected=call(dsn,correct_task_outcome,actor(user),correction)
    after=plan(dsn,user,instance,goal)
    assert after.reported_count==after.confirmed_count==0
    assert after.occurrences[0].evidence_state=="REQUIRES_REASSESSMENT"
    # Task correction doesn't increment the Goal version. Recheck evidence
    # at admission, not merely the approved parent CAS.
    assert after.goal_version==next_plan.goal_version
    with pytest.raises(VersionConflict,match="WAIT_FOR_EVIDENCE"):
        create(dsn,user,second)
    report=report_done(dsn,user,instance,conv,cmd.task_id,corrected.version)
    confirm_done(dsn,user,instance,conv,cmd.task_id,report.version,report.report_id)
    second=command_for(dsn,user,instance,conv,plan(dsn,user,instance,goal).next_task)
    create(dsn,user,second)
    report2=report_done(dsn,user,instance,conv,second.task_id,1)
    confirm_done(dsn,user,instance,conv,second.task_id,report2.version,report2.report_id)
    final=plan(dsn,user,instance,goal)
    assert final.decision=="USER_CONFIRMED_TARGET_REPORTED_MET"
    assert final.reported_count==final.confirmed_count==2
    assert final.independently_verified_count==0 and final.next_task is None
    root,=call(dsn,list_goals,actor(user),instance)
    assert root.status=="ACTIVE" and root.desired_outcome=="Walk 2 times per week"
    assert root.success_criterion=="Complete at least 2 walks per week"
    prior=plan(dsn,user,instance,goal,window_start=final.window_start-timedelta(days=7))
    assert prior.decision=="OUTSIDE_APPROVED_WINDOW" and prior.reported_count==prior.confirmed_count==0
    future=plan(dsn,user,instance,goal,window_start=final.window_end)
    assert future.decision=="WINDOW_CLOSED" and future.reported_count==future.confirmed_count==0
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_recurring_task_slots WHERE goal_id=%s",(goal,)
        ).fetchone()==(2,)
        assert conn.execute(
            "SELECT count(*) FROM task_outcome_corrections WHERE task_id=%s",(cmd.task_id,)
        ).fetchone()==(1,)
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s AND instance_id=%s",
            (user,instance),
        ).fetchone()==(0,)


def test_recurring_decomposition_abstains_for_unproved_and_competing_work(dsn):
    from test_phase3_goals import generalized_command,goal_evaluator
    from persona_agent.goal_decomposition import propose_derived_subgoal
    from persona_agent.runtime import VersionConflict
    actor,call,_,_,_=helpers()
    user,instance,conv,goal=approved(dsn,"Set a goal to eat fewer sweets; success means at most two sweets per week.")
    assert plan(dsn,user,instance,goal).decision=="UNSUPPORTED"
    user,instance,conv,goal=approved(dsn,"Set a goal to exercise three times a week.")
    child=generalized_command(dsn,instance,goal,
        outcome="Complete 1 exercise session per week",criterion="Complete at least 1 exercise session per week")
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),child).status=="ACCEPTED"
    assert plan(dsn,user,instance,goal).decision=="CHILDREN_OR_REVIEW"
    child_plan=plan(dsn,user,instance,child.proposed_goal_id)
    assert child_plan.target==1 and child_plan.next_task is not None
    create(dsn,user,command_for(dsn,user,instance,conv,child_plan.next_task))
    assert plan(dsn,user,instance,goal).reported_count==0
    with pytest.raises(VersionConflict,match="existing tasks"):
        call(dsn,propose_derived_subgoal,goal_evaluator(user),
             generalized_command(dsn,instance,child.proposed_goal_id))


def test_recurring_priority_and_scope_stop_admission(dsn):
    from test_phase3_tasks import make_goal,make_task,priority
    from persona_agent.runtime import PermissionDenied,VersionConflict
    actor,call,seed,_,_=helpers()
    user,instance,conv,goal=approved(dsn,"Set a goal to prepare 2 meals per week; success means prepare at least 2 meals per week.")
    before=plan(dsn,user,instance,goal)
    assert before.next_task.title=="Prepare one meal (1/2)"
    cmd=command_for(dsn,user,instance,conv,before.next_task)
    other_goal=make_goal(dsn,user,instance,conv)
    other_task,_,_=make_task(dsn,user,instance,conv,other_goal,1)
    only=priority(dsn,user,instance,conv,other_task,1,"ONLY")
    assert plan(dsn,user,instance,goal).decision=="EXCLUSIVE_OTHER"
    with pytest.raises(VersionConflict,match="EXCLUSIVE_OTHER"):
        create(dsn,user,cmd)
    priority(dsn,user,instance,conv,other_task,only.version,"CLEAR_ONLY")
    create(dsn,user,cmd)
    foreign,foreign_instance,_=seed(dsn)
    with pytest.raises(PermissionDenied):
        plan(dsn,foreign,foreign_instance,goal)
    assert plan(dsn,user,instance,goal).decision=="WAIT_FOR_EVIDENCE"


def test_reused_precreation_report_does_not_count_as_occurrence(dsn):
    from persona_agent.task_contracts import ReportTaskOutcomeCommand
    from persona_agent.task_state import report_task_outcome
    from test_phase3_tasks import confirm_done
    actor,call,_,turn,approve=helpers()
    user,instance,conv,goal=approved(dsn)
    source=turn(dsn,user,instance,conv,"I completed a previous walk.")
    cmd=command_for(dsn,user,instance,conv,plan(dsn,user,instance,goal).next_task)
    create(dsn,user,cmd)
    report=ReportTaskOutcomeCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="I completed a previous walk.",task_id=cmd.task_id,
        expected_task_version=1,report="DONE",
    )
    result=call(dsn,report_task_outcome,actor(user),approve(dsn,user,instance,report))
    confirm_done(dsn,user,instance,conv,cmd.task_id,result.version,result.report_id)
    current=plan(dsn,user,instance,goal)
    assert current.reported_count==current.confirmed_count==0
    assert current.occurrences[0].evidence_state=="OUTSIDE_WINDOW"
    assert current.decision=="WAIT_FOR_EVIDENCE"


def test_stale_cycle_draft_and_concurrent_duplicate_slot_are_rejected(dsn):
    from concurrent.futures import ThreadPoolExecutor
    from persona_agent.runtime import VersionConflict
    actor,call,_,_,_=helpers()
    user,instance,conv,goal=approved(dsn)
    draft=plan(dsn,user,instance,goal).next_task
    one=command_for(dsn,user,instance,conv,draft)
    two=command_for(dsn,user,instance,conv,draft)
    def admit(command):
        try:
            return create(dsn,user,command)
        except VersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(admit,[one,two]))
    assert sum(r is not None for r in results)==1
    current=plan(dsn,user,instance,goal)
    assert len(current.occurrences)==1 and current.decision=="WAIT_FOR_EVIDENCE"


def test_prerequisite_correction_revokes_effective_cycle_credit(dsn):
    from test_phase3_tasks import make_goal,make_task,edge,report_done,confirm_done,correction_command
    from persona_agent.task_state import list_tasks,correct_task_outcome
    from persona_agent.runtime import VersionConflict
    actor,call,_,_,_=helpers()
    user,instance,conv,goal=approved(dsn)
    prerequisite_goal=make_goal(dsn,user,instance,conv)
    prerequisite,_,_=make_task(dsn,user,instance,conv,prerequisite_goal,1)
    first=command_for(dsn,user,instance,conv,plan(dsn,user,instance,goal).next_task)
    create(dsn,user,first)
    linked=edge(dsn,user,instance,conv,first.task_id,prerequisite,
                plan(dsn,user,instance,goal).goal_version)
    tasks=call(dsn,list_tasks,actor(user),instance,goal)
    assert not tasks[0].actionable and "PREREQUISITE_NOT_CONFIRMED" in tasks[0].blockers
    before=report_done(dsn,user,instance,conv,prerequisite,1)
    prerequisite_confirmation=confirm_done(dsn,user,instance,conv,prerequisite,
                                            before.version,before.report_id)
    report=report_done(dsn,user,instance,conv,first.task_id,linked.version)
    confirm_done(dsn,user,instance,conv,first.task_id,report.version,report.report_id)
    current=plan(dsn,user,instance,goal)
    assert current.confirmed_count==1 and current.next_task.ordinal==2
    second=command_for(dsn,user,instance,conv,current.next_task)
    correction=correction_command(dsn,user,instance,conv,prerequisite,
        prerequisite_confirmation.version,confirmation_id=prerequisite_confirmation.confirmation_id)
    call(dsn,correct_task_outcome,actor(user),correction)
    current=plan(dsn,user,instance,goal)
    assert current.occurrences[0].task_status=="USER_CONFIRMED"
    assert current.occurrences[0].evidence_state=="REQUIRES_REASSESSMENT"
    assert current.confirmed_count==current.reported_count==0
    with pytest.raises(VersionConflict,match="WAIT_FOR_EVIDENCE"):
        create(dsn,user,second)


def test_explicit_goal_dates_are_not_silently_extended(dsn):
    from datetime import datetime,time,timezone
    import psycopg
    from persona_agent.goal_contracts import CreateGoalCommand,goal_action_approval_phrase
    from persona_agent.goal_state import create_goal
    actor,call,seed,turn,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        now=conn.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(timezone.utc)
    start,end=cycle_bounds("week",now.date())
    text="Track this exercise outcome only after the current UTC week."
    source=turn(dsn,user,instance,conv,text)
    cmd=CreateGoalCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,evidence_quote=text,
        goal_id=uuid4(),title="Exercise",desired_outcome="Exercise 2 times per week",
        success_criterion="Complete at least 2 exercise sessions per week",horizon="SHORT",
        evaluation_start_at=datetime.combine(end,time.min,tzinfo=timezone.utc),
    )
    approval=turn(dsn,user,instance,conv,goal_action_approval_phrase(cmd))
    call(dsn,create_goal,actor(user),cmd.model_copy(update={"approval_turn_id":approval.turn_id}))
    current=plan(dsn,user,instance,cmd.goal_id)
    assert current.decision=="OUTSIDE_APPROVED_WINDOW" and current.next_task is None


def _date_bounded_root(dsn,user,instance,conv,*,outcome,criterion,dates):
    """Approve actual root dates; do not mutate authoritative rows in tests."""
    from persona_agent.goal_contracts import CreateGoalCommand,goal_action_approval_phrase
    from persona_agent.goal_state import create_goal
    actor,call,_,turn,_=helpers()
    text="Track this outcome only within these agreed dates."
    source=turn(dsn,user,instance,conv,text)
    cmd=CreateGoalCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,evidence_quote=text,
        goal_id=uuid4(),title=outcome,desired_outcome=outcome,
        success_criterion=criterion,horizon="SHORT",**dates,
    )
    approval=turn(dsn,user,instance,conv,goal_action_approval_phrase(cmd))
    call(dsn,create_goal,actor(user),cmd.model_copy(update={"approval_turn_id":approval.turn_id}))
    return cmd.goal_id


def _current_utc_cycle(dsn,period):
    from datetime import timezone
    import psycopg
    with psycopg.connect(dsn,autocommit=True) as conn:
        now=conn.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(timezone.utc)
    return cycle_bounds(period,now.date())


def _assert_window_blocks_admission(dsn,user,instance,conv,goal,start,end):
    import psycopg
    from persona_agent.goal_cycle_contracts import task_draft
    from persona_agent.runtime import VersionConflict
    from persona_agent.task_state import list_tasks
    actor,call,_,_,_=helpers()
    blocked=plan(dsn,user,instance,goal,window_start=start)
    assert blocked.decision=="OUTSIDE_APPROVED_WINDOW"
    assert blocked.goal_status=="ACTIVE" and blocked.next_task is None
    assert blocked.reported_count==blocked.confirmed_count==0
    # Even a separately approved exact-shaped draft cannot extend an
    # ancestor's authority. Exercise the write gate, not just the read API.
    draft=task_draft(goal,blocked.goal_version,
        recurring_count(blocked.desired_outcome,blocked.success_criterion),start,end,1)
    command=command_for(dsn,user,instance,conv,draft)
    with pytest.raises(VersionConflict,match="OUTSIDE_APPROVED_WINDOW"):
        create(dsn,user,command)
    assert call(dsn,list_tasks,actor(user),instance,goal)==()
    assert plan(dsn,user,instance,goal,window_start=start).goal_version==blocked.goal_version
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_recurring_task_slots WHERE goal_id=%s",(goal,)
        ).fetchone()==(0,)
        assert conn.execute(
            """SELECT count(*) FROM events WHERE instance_id=%s
               AND event_type='goal.task.created.v1' AND payload->>'goal_id'=%s""",
            (instance,str(goal)),
        ).fetchone()==(0,)


@pytest.mark.parametrize("bound,offset,nested",[
    ("evaluation_start_at","next",False),
    ("evaluation_start_at","inside",False),
    ("evaluation_end_at","past",True),
    ("evaluation_end_at","inside",False),
    ("target_at","past",False),
    ("target_at","inside",True),
])
def test_delegated_recurring_leaf_inherits_all_ancestor_windows(dsn,bound,offset,nested):
    from datetime import datetime,time,timezone
    import psycopg
    from test_phase3_goals import generalized_command,goal_evaluator
    from persona_agent.goal_decomposition import propose_derived_subgoal
    _,call,seed,_,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    start,end=_current_utc_cycle(dsn,"week")
    boundary={"next":end,"inside":start+timedelta(days=3),"past":start}[offset]
    root=_date_bounded_root(dsn,user,instance,conv,
        outcome="Complete 3 exercise sessions per week",
        criterion="Complete at least 3 exercise sessions per week",
        dates={bound:datetime.combine(boundary,time.min,tzinfo=timezone.utc)})
    child=generalized_command(dsn,instance,root,
        outcome="Complete 2 exercise sessions per week",
        criterion="Complete at least 2 exercise sessions per week")
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),child).status=="ACCEPTED"
    leaf=child.proposed_goal_id
    if nested:
        grandchild=generalized_command(dsn,instance,leaf,
            outcome="Complete 1 exercise session per week",
            criterion="Complete at least 1 exercise session per week")
        assert call(dsn,propose_derived_subgoal,goal_evaluator(user),grandchild).status=="ACCEPTED"
        leaf=grandchild.proposed_goal_id
    # Delegated definitions themselves intentionally carry no copied dates.
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT evaluation_start_at,evaluation_end_at,target_at
               FROM goal_records WHERE goal_id=%s""",(leaf,)
        ).fetchone()==(None,None,None)
    _assert_window_blocks_admission(dsn,user,instance,conv,leaf,start,end)


def test_semantically_reviewed_recurring_leaf_inherits_parent_window(dsn):
    from datetime import datetime,time,timezone
    from test_phase3_goals import (
        generalized_command,goal_evaluator,semantic_reviewer,semantic_command,
        semantic_finding,FakeSemanticGateway,
    )
    from persona_agent.goal_decomposition import propose_derived_subgoal
    from persona_agent.goal_semantic_review import review_pending_subgoal
    _,call,seed,_,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    start,end=_current_utc_cycle(dsn,"day")
    root=_date_bounded_root(dsn,user,instance,conv,
        outcome="Read 3 books per month",criterion="Read at least 3 books per month",
        dates={"evaluation_start_at":datetime.combine(end,time.min,tzinfo=timezone.utc)})
    child=generalized_command(dsn,instance,root,
        title="Daily reading",outcome="Read 2 pages per day",
        criterion="Read at least 2 pages per day")
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),child).status=="PENDING_REVIEW"
    gateway=FakeSemanticGateway(semantic_finding(child_quote="Read 2 pages per day"))
    reviewed=call(dsn,review_pending_subgoal,semantic_reviewer(user),
        semantic_command(instance,child.proposal_id),gateway)
    assert reviewed.decision=="ACCEPTED"
    _assert_window_blocks_admission(dsn,user,instance,conv,child.proposed_goal_id,start,end)


def test_ancestor_window_exact_boundaries_allow_recurring_child(dsn):
    from datetime import datetime,time,timezone
    from test_phase3_goals import generalized_command,goal_evaluator
    from persona_agent.goal_decomposition import propose_derived_subgoal
    _,call,seed,_,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    start,end=_current_utc_cycle(dsn,"week")
    left=datetime.combine(start,time.min,tzinfo=timezone.utc)
    right=datetime.combine(end,time.min,tzinfo=timezone.utc)
    root=_date_bounded_root(dsn,user,instance,conv,
        outcome="Complete 3 exercise sessions per week",
        criterion="Complete at least 3 exercise sessions per week",
        dates={"evaluation_start_at":left,"evaluation_end_at":right,"target_at":right})
    child=generalized_command(dsn,instance,root,
        outcome="Complete 1 exercise session per week",
        criterion="Complete at least 1 exercise session per week")
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),child).status=="ACCEPTED"
    current=plan(dsn,user,instance,child.proposed_goal_id,window_start=start)
    assert current.decision=="PROPOSE_ONE_TASK"
    assert current.next_task.window_end==end
    create(dsn,user,command_for(dsn,user,instance,conv,current.next_task))
    assert plan(dsn,user,instance,child.proposed_goal_id,window_start=start).decision=="WAIT_FOR_EVIDENCE"
    assert plan(dsn,user,instance,child.proposed_goal_id,window_start=end).decision=="OUTSIDE_APPROVED_WINDOW"
