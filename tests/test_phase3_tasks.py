"""P3-02 PostgreSQL acceptance for goal-owned task dependencies."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.goal_contracts import CreateGoalCommand, goal_action_approval_phrase
from persona_agent.goal_state import create_goal
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.runtime import PermissionDenied, StaleSelection, VersionConflict
from persona_agent.task_contracts import (
    CreateTaskCommand, AddTaskDependencyCommand, CancelTaskCommand, ConfirmUserTaskCommand,
    CorrectTaskOutcomeCommand, ReportTaskOutcomeCommand,
    SetTaskPriorityCommand, task_approval_phrase,
)
from persona_agent.task_state import (
    create_task, add_task_dependency, cancel_task, confirm_user_task,
    correct_task_outcome, report_task_outcome,
    set_task_priority, list_tasks,
)


def call(dsn,func,*args):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return func(conn,*args)


def actor(uid):
    return TrustedActor(user_id=uid,source_id="p3-02-authenticated")


def seed(dsn, two_personas=False):
    uid,pid,instance=uuid4(),uuid4(),uuid4()
    other=None
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(uid,))
            conn.execute("INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                         (pid,"p302-"+pid.hex))
            conn.execute("""INSERT INTO persona_instances(id,user_id,persona_id,label)
                            VALUES (%s,%s,%s,'Task persona')""",(instance,uid,pid))
            if two_personas:
                pid2,other=uuid4(),uuid4()
                conn.execute("INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                             (pid2,"p302-"+pid2.hex))
                conn.execute("""INSERT INTO persona_instances(id,user_id,persona_id,label)
                                VALUES (%s,%s,%s,'Other persona')""",(other,uid,pid2))
            conn.execute("""INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                            VALUES (%s,%s,1,'FOREGROUND')""",(uid,instance))
    return uid,instance,other


def turn(dsn,uid,instance,conversation,message,generation=1):
    return call(dsn,ingest_text,actor(uid),TextInputCommand(
        instance_id=instance,conversation_id=conversation,
        expected_generation=generation,idempotency_key=uuid4().hex,text=message,
    ))


def approve(dsn,uid,instance,cmd):
    confirmation=turn(dsn,uid,instance,cmd.conversation_id,
                      task_approval_phrase(cmd),cmd.expected_generation)
    return cmd.model_copy(update={"approval_turn_id":confirmation.turn_id})


def make_goal(dsn,uid,instance,conversation):
    source=turn(dsn,uid,instance,conversation,"I want to improve my meals.")
    cmd=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conversation,
        evidence_turn_id=source.turn_id,evidence_quote="I want to improve my meals",
        goal_id=uuid4(),title="Meals",desired_outcome="Balanced meals",
        success_criterion="Meal targets met",horizon="MEDIUM",
    )
    confirmation=turn(dsn,uid,instance,conversation,goal_action_approval_phrase(cmd))
    cmd=cmd.model_copy(update={"approval_turn_id":confirmation.turn_id})
    call(dsn,create_goal,actor(uid),cmd)
    return cmd.goal_id


def make_task(dsn,uid,instance,conversation,goal_id,goal_version,*,executor="USER",
              capability=None,title="Prepare lunch"):
    source=turn(dsn,uid,instance,conversation,"Please add a task to prepare lunch.")
    cmd=CreateTaskCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conversation,evidence_turn_id=source.turn_id,
        evidence_quote="Please add a task",task_id=uuid4(),goal_id=goal_id,
        expected_goal_version=goal_version,title=title,
        action_description="Prepare one observable meal or household action",executor=executor,
        capability=capability,completion_criterion="Meal is prepared",
        evidence_requirement="User report or independent confirmation",
    )
    cmd=approve(dsn,uid,instance,cmd)
    return cmd.task_id,call(dsn,create_task,actor(uid),cmd),cmd


def edge(dsn,uid,instance,conversation,task,depends,goal_version):
    source=turn(dsn,uid,instance,conversation,"Make the second task depend on the first.")
    cmd=AddTaskDependencyCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conversation,evidence_turn_id=source.turn_id,
        evidence_quote="Make the second task depend",task_id=task,
        prerequisite_task_id=depends,expected_goal_version=goal_version,
    )
    return call(dsn,add_task_dependency,actor(uid),approve(dsn,uid,instance,cmd))


def test_actionability_and_user_report_never_verify_dependency(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    first,_,_=make_task(dsn,uid,instance,conv,goal,1)
    second,_,_=make_task(dsn,uid,instance,conv,goal,2,title="Clean kitchen")
    edge(dsn,uid,instance,conv,second,first,3)
    items={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert items[first].actionable
    assert not items[second].actionable
    assert "PREREQUISITE_NOT_CONFIRMED" in items[second].blockers
    msg=turn(dsn,uid,instance,conv,"I completed preparing lunch.")
    cmd=ReportTaskOutcomeCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=msg.turn_id,
        evidence_quote="I completed preparing lunch",task_id=first,
        expected_task_version=1,report="DONE",
    )
    approved=approve(dsn,uid,instance,cmd)
    done=call(dsn,report_task_outcome,actor(uid),approved)
    again=call(dsn,report_task_outcome,actor(uid),approved)
    assert done.version==2 and again.replayed is True
    items={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert items[first].status=="USER_REPORTED_DONE"
    assert "AWAITING_USER_CONFIRMATION" in items[first].blockers
    assert "PREREQUISITE_NOT_CONFIRMED" in items[second].blockers


def test_cycle_and_same_persona_cross_goal_dependency(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    a,_,_=make_task(dsn,uid,instance,conv,goal,1)
    b,_,_=make_task(dsn,uid,instance,conv,goal,2)
    edge(dsn,uid,instance,conv,b,a,3)
    with pytest.raises(VersionConflict,match="cycle"):
        edge(dsn,uid,instance,conv,a,b,4)
    other_goal=make_goal(dsn,uid,instance,uuid4())
    c,_,_=make_task(dsn,uid,instance,conv,other_goal,1)
    linked=edge(dsn,uid,instance,conv,b,c,4)
    assert linked.action=="DEPENDENCY_ADDED"
    assert c in call(dsn,list_tasks,actor(uid),instance,goal)[1].prerequisite_ids


def test_concurrent_task_cas_and_duplicate_replay(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    source=turn(dsn,uid,instance,conv,"Add one task")
    def command():
        cmd=CreateTaskCommand(
            instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
            conversation_id=conv,evidence_turn_id=source.turn_id,evidence_quote="Add one task",
            task_id=uuid4(),goal_id=goal,expected_goal_version=1,
            title="Check ingredients",action_description="List required ingredients",executor="USER",
            completion_criterion="Ingredients listed",evidence_requirement="User statement",
        )
        return approve(dsn,uid,instance,cmd)
    first,second=command(),command()
    def submit(cmd):
        try:
            return call(dsn,create_task,actor(uid),cmd)
        except VersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(submit,first)
        b=pool.submit(submit,second)
        results=[a.result(timeout=15),b.result(timeout=15)]
    assert sum(x is not None for x in results)==1
    winner=first if results[0] is not None else second
    assert call(dsn,create_task,actor(uid),winner).replayed


def test_agent_capability_priority_and_persona_switch(dsn):
    uid,instance,other=seed(dsn,two_personas=True)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    a,_,_=make_task(dsn,uid,instance,conv,goal,1,executor="AGENT",
                    capability="browser.search")
    b,_,_=make_task(dsn,uid,instance,conv,goal,2,title="Talk to user")
    report=turn(dsn,uid,instance,conv,"Prioritize searching over talking.")
    cmd=SetTaskPriorityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=report.turn_id,
        evidence_quote="Prioritize searching over talking",task_id=a,
        expected_task_version=1,rule="PREFER_OVER",other_task_id=b,
    )
    call(dsn,set_task_priority,actor(uid),approve(dsn,uid,instance,cmd))
    tasks={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert "CAPABILITY_AND_PERMISSION_UNCHECKED" in tasks[a].blockers
    assert tasks[a].preferred_over==(b,)
    assert tasks[b].actionable
    switched=call(dsn,switch_persona,actor(uid),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=uuid4().hex,
    ))
    assert switched.generation==2
    with pytest.raises(StaleSelection):
        call(dsn,list_tasks,actor(uid),instance,goal)


def test_missing_confirmation_and_wrong_source_block_task_creation(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    source=turn(dsn,uid,instance,conv,"Perhaps add a task.")
    cmd=CreateTaskCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Perhaps add a task",task_id=uuid4(),goal_id=goal,
        expected_goal_version=1,title="Preparation",
        action_description="Prepare the kitchen area",executor="USER",
        completion_criterion="Prepared",evidence_requirement="Observation",
    )
    with pytest.raises(PermissionDenied,match="separate user approval"):
        call(dsn,create_task,actor(uid),cmd)
    authorized=approve(dsn,uid,instance,cmd)
    with pytest.raises(PermissionDenied,match="bound"):
        call(dsn,create_task,actor(uid),authorized.model_copy(update={
            "idempotency_key":uuid4().hex,"title":"Different task",
        }))
    assert call(dsn,create_task,actor(uid),authorized).action=="TASK_CREATED"


def test_priority_suspend_resume_and_deadline_revisions(dsn):
    from datetime import datetime, timezone, timedelta
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    task,_,_=make_task(dsn,uid,instance,conv,goal,1)
    def priority(version,rule,deadline=None):
        msg=turn(dsn,uid,instance,conv,"Set task priority as requested.")
        cmd=SetTaskPriorityCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=uuid4().hex,conversation_id=conv,
            evidence_turn_id=msg.turn_id,
            evidence_quote="Set task priority",task_id=task,
            expected_task_version=version,rule=rule,deadline=deadline,
        )
        return call(dsn,set_task_priority,actor(uid),approve(dsn,uid,instance,cmd))
    assert priority(1,"SUSPEND").version==2
    assert "USER_SUSPENDED" in call(dsn,list_tasks,actor(uid),instance,goal)[0].blockers
    assert priority(2,"RESUME").version==3
    assert call(dsn,list_tasks,actor(uid),instance,goal)[0].actionable
    when=datetime.now(timezone.utc)+timedelta(days=3)
    assert priority(3,"SET_DEADLINE",when).version==4
    assert call(dsn,list_tasks,actor(uid),instance,goal)[0].deadline==when
    assert priority(4,"CLEAR_DEADLINE").version==5
    assert call(dsn,list_tasks,actor(uid),instance,goal)[0].deadline is None


def test_cancellation_is_fenced_and_blocks_dependents_without_goal_mutation(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    first,_,_=make_task(dsn,uid,instance,conv,goal,1)
    second,_,_=make_task(dsn,uid,instance,conv,goal,2)
    edge(dsn,uid,instance,conv,second,first,3)
    user_turn=turn(dsn,uid,instance,conv,"Cancel my first task.")
    cmd=CancelTaskCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=user_turn.turn_id,
        evidence_quote="Cancel my first task",task_id=first,expected_task_version=1,
    )
    cmd=approve(dsn,uid,instance,cmd)
    result=call(dsn,cancel_task,actor(uid),cmd)
    assert result.action=="TASK_CANCELLED" and result.version==2
    assert call(dsn,cancel_task,actor(uid),cmd).replayed
    items={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert not items[first].actionable and "CANCELLED" in items[first].blockers
    assert not items[second].actionable and "PREREQUISITE_NOT_CONFIRMED" in items[second].blockers
    with pytest.raises(VersionConflict):
        call(dsn,cancel_task,actor(uid),cmd.model_copy(update={
            "idempotency_key":uuid4().hex,
        }))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM goal_records WHERE goal_id=%s",(goal,),
        ).fetchone()==("ACTIVE",)


def priority(dsn, uid, instance, conv, task_id, version, rule,
             *, other=None, deadline=None):
    source=turn(dsn,uid,instance,conv,"Apply this specific task ordering preference.")
    cmd=SetTaskPriorityCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Apply this specific task ordering preference",
        task_id=task_id,expected_task_version=version,
        rule=rule,other_task_id=other,deadline=deadline,
    )
    return call(dsn,set_task_priority,actor(uid),approve(dsn,uid,instance,cmd))


def report_done(dsn,uid,instance,conv,task_id,version):
    msg=turn(dsn,uid,instance,conv,"I completed the requested task.")
    cmd=ReportTaskOutcomeCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=msg.turn_id,
        evidence_quote="I completed the requested task",
        task_id=task_id,expected_task_version=version,report="DONE",
    )
    return call(dsn,report_task_outcome,actor(uid),approve(dsn,uid,instance,cmd))


def confirm_done(dsn,uid,instance,conv,task_id,version,report_id):
    msg=turn(dsn,uid,instance,conv,"I explicitly confirm that I finished this task.")
    cmd=ConfirmUserTaskCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=msg.turn_id,
        evidence_quote="I explicitly confirm that I finished this task",
        task_id=task_id,expected_task_version=version,report_id=report_id,
    )
    cmd=approve(dsn,uid,instance,cmd)
    return call(dsn,confirm_user_task,actor(uid),cmd)


def test_user_confirmation_unlocks_dependent_work_without_verified_claim(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    first,_,_=make_task(dsn,uid,instance,conv,goal,1)
    second,_,_=make_task(dsn,uid,instance,conv,goal,2,title="Second action")
    edge(dsn,uid,instance,conv,second,first,3)
    report=report_done(dsn,uid,instance,conv,first,1)
    assert report.report_id is not None
    assert not next(x for x in call(dsn,list_tasks,actor(uid),instance,goal)
                    if x.task_id==second).actionable
    with pytest.raises(VersionConflict,match="outstanding DONE"):
        confirm_done(dsn,uid,instance,conv,second,2,report.report_id)
    confirmed=confirm_done(dsn,uid,instance,conv,first,2,report.report_id)
    assert confirmed.action=="USER_COMPLETION_CONFIRMED"
    items={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert items[first].status=="USER_CONFIRMED"
    assert items[second].actionable
    assert not items[second].dispatch_authorized
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT epistemic_status FROM task_user_confirmations "
            "WHERE task_id=%s",(first,),
        ).fetchone()==("USER_CONFIRMED",)
        assert conn.execute(
            "SELECT status FROM goal_records WHERE goal_id=%s",(goal,),
        ).fetchone()==("ACTIVE",)


def test_run_after_and_dependencies_share_cycle_detection(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    a,_,_=make_task(dsn,uid,instance,conv,goal,1)
    b,_,_=make_task(dsn,uid,instance,conv,goal,2)
    assert priority(dsn,uid,instance,conv,b,1,"RUN_AFTER",other=a).version==2
    assert "PREREQUISITE_NOT_CONFIRMED" in next(
        t for t in call(dsn,list_tasks,actor(uid),instance,goal)
        if t.task_id==b
    ).blockers
    with pytest.raises(VersionConflict,match="cycle"):
        edge(dsn,uid,instance,conv,a,b,3)
    with pytest.raises(VersionConflict,match="cycle"):
        priority(dsn,uid,instance,conv,a,1,"RUN_AFTER",other=b)
    assert priority(dsn,uid,instance,conv,b,2,"CLEAR_RUN_AFTER").version==3
    assert priority(dsn,uid,instance,conv,a,1,"RUN_AFTER",other=b).version==2
    assert not next(t for t in call(dsn,list_tasks,actor(uid),instance,goal)
                    if t.task_id==a).actionable


def test_exclusive_only_priority_is_scoped_reversible_and_nonimplicit(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    a,_,_=make_task(dsn,uid,instance,conv,goal,1)
    b,_,_=make_task(dsn,uid,instance,conv,goal,2)
    assert priority(dsn,uid,instance,conv,a,1,"PREFER_OVER",other=b).version==2
    assert all(x.actionable for x in call(dsn,list_tasks,actor(uid),instance,goal))
    assert priority(dsn,uid,instance,conv,a,2,"ONLY").version==3
    items={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert items[a].exclusive_only and items[a].actionable
    assert "EXCLUSIVE_OTHER" in items[b].blockers
    with pytest.raises(VersionConflict,match="exclusive"):
        priority(dsn,uid,instance,conv,b,1,"ONLY")
    assert priority(dsn,uid,instance,conv,a,3,"CLEAR_ONLY").version==4
    assert priority(dsn,uid,instance,conv,a,4,"CLEAR_PREFERENCE").version==5
    items={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert items[b].actionable and items[a].preferred_over==()


def test_cross_persona_dependency_denied(dsn):
    uid,instance,_=seed(dsn)
    foreign,foreign_instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    a,_,_=make_task(dsn,uid,instance,conv,goal,1)
    foreign_conv=uuid4()
    foreign_goal=make_goal(dsn,foreign,foreign_instance,foreign_conv)
    b,_,_=make_task(dsn,foreign,foreign_instance,foreign_conv,foreign_goal,1)
    with pytest.raises(PermissionDenied):
        edge(dsn,uid,instance,conv,a,b,2)


def test_reported_completion_cannot_bypass_inactive_ancestor(dsn):
    # A task completion that overlaps a parent pause must observe the pause.
    from concurrent.futures import ThreadPoolExecutor
    uid,instance,_=seed(dsn)
    conv=uuid4()
    parent=make_goal(dsn,uid,instance,conv)
    source=turn(dsn,uid,instance,conv,"I want to improve my meals through lunch prep.")
    child_cmd=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=source.turn_id,
        evidence_quote="I want to improve my meals",
        approval_turn_id=None,goal_id=uuid4(),parent_goal_id=parent,
        expected_parent_version=1,title="Lunch preparation",
        desired_outcome="Prepare lunches",success_criterion="Lunch made",horizon="SHORT",
    )
    confirm=turn(dsn,uid,instance,conv,goal_action_approval_phrase(child_cmd))
    child_cmd=child_cmd.model_copy(update={"approval_turn_id":confirm.turn_id})
    call(dsn,create_goal,actor(uid),child_cmd)
    task,_,_=make_task(dsn,uid,instance,conv,child_cmd.goal_id,1)
    report=report_done(dsn,uid,instance,conv,task,1)
    msg=turn(dsn,uid,instance,conv,"I explicitly confirm that I finished this task.")
    command=ConfirmUserTaskCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=msg.turn_id,
        evidence_quote="I explicitly confirm that I finished this task",
        task_id=task,expected_task_version=2,report_id=report.report_id,
    )
    command=approve(dsn,uid,instance,command)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(dsn,autocommit=True) as conn:
            with conn.transaction():
                conn.execute("SELECT 1 FROM goal_records WHERE goal_id=%s FOR UPDATE",
                             (parent,))
                future=pool.submit(call,dsn,confirm_user_task,actor(uid),command)
                conn.execute(
                    "UPDATE goal_records SET status='PAUSED',version=version+1 "
                    "WHERE goal_id=%s",(parent,),
                )
        with pytest.raises(VersionConflict,match="ancestor"):
            future.result(timeout=15)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM task_user_confirmations WHERE task_id=%s",(task,),
        ).fetchone()[0]==0


def test_concurrent_reverse_run_after_edges_cannot_form_cycle(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    first,_,_=make_task(dsn,uid,instance,conv,goal,1)
    second,_,_=make_task(dsn,uid,instance,conv,goal,2)

    def command(task_id, predecessor):
        text=turn(dsn,uid,instance,conv,"Order these two tasks as requested.")
        cmd=SetTaskPriorityCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=uuid4().hex,conversation_id=conv,
            evidence_turn_id=text.turn_id,
            evidence_quote="Order these two tasks",
            task_id=task_id,expected_task_version=1,
            rule="RUN_AFTER",other_task_id=predecessor,
        )
        return approve(dsn,uid,instance,cmd)

    a=command(first,second)
    b=command(second,first)

    def apply(cmd):
        try:
            return call(dsn,set_task_priority,actor(uid),cmd)
        except VersionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        x=pool.submit(apply,a)
        y=pool.submit(apply,b)
        results=(x.result(timeout=15),y.result(timeout=15))
    assert sum(item is not None for item in results)==1
    items={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert sum(bool(x.run_after) for x in items.values())==1


def correction_command(dsn,uid,instance,conv,task_id,version,*,
                       report_id=None,confirmation_id=None):
    message=turn(dsn,uid,instance,conv,
                 "Actually I was mistaken about the task; the result needs correction.")
    cmd=CorrectTaskOutcomeCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=message.turn_id,
        evidence_quote="Actually I was mistaken about the task",
        task_id=task_id,expected_task_version=version,
        superseded_report_id=report_id,
        superseded_confirmation_id=confirmation_id,
        reason="The previous result was inaccurate and must be reassessed",
    )
    return approve(dsn,uid,instance,cmd)


def report_obstacle(dsn,uid,instance,conv,task_id,version):
    message=turn(dsn,uid,instance,conv,"I encountered an unexpected obstacle.")
    cmd=ReportTaskOutcomeCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=message.turn_id,
        evidence_quote="I encountered an unexpected obstacle",
        task_id=task_id,expected_task_version=version,report="OBSTACLE",
    )
    return call(dsn,report_task_outcome,actor(uid),approve(dsn,uid,instance,cmd))


def test_only_latest_done_report_can_be_confirmed(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    task,_,_=make_task(dsn,uid,instance,conv,goal,1)
    old=report_done(dsn,uid,instance,conv,task,1)
    newer=report_done(dsn,uid,instance,conv,task,2)
    with pytest.raises(VersionConflict,match="superseded"):
        confirm_done(dsn,uid,instance,conv,task,3,old.report_id)
    confirmed=confirm_done(dsn,uid,instance,conv,task,3,newer.report_id)
    assert confirmed.confirmation_id is not None
    assert confirmed.version==4 and confirmed.report_id==newer.report_id
    assert call(dsn,list_tasks,actor(uid),instance,goal)[0].effective_completion


def test_obstacle_and_correction_reject_stale_evidence(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    task,_,_=make_task(dsn,uid,instance,conv,goal,1)
    old=report_done(dsn,uid,instance,conv,task,1)
    obstacle=report_obstacle(dsn,uid,instance,conv,task,2)
    assert obstacle.version==3
    newer=report_done(dsn,uid,instance,conv,task,3)
    with pytest.raises(VersionConflict,match="superseded"):
        confirm_done(dsn,uid,instance,conv,task,4,old.report_id)
    with pytest.raises(VersionConflict,match="newer evidence"):
        call(dsn,correct_task_outcome,actor(uid),
             correction_command(dsn,uid,instance,conv,task,4,
                                report_id=old.report_id))
    cmd=correction_command(dsn,uid,instance,conv,task,4,
                           report_id=newer.report_id)
    corrected=call(dsn,correct_task_outcome,actor(uid),cmd)
    assert corrected.action=="OUTCOME_CORRECTED" and corrected.version==5
    assert corrected.correction_id is not None
    assert call(dsn,correct_task_outcome,actor(uid),cmd).replayed
    view=call(dsn,list_tasks,actor(uid),instance,goal)[0]
    assert view.status=="NEEDS_REVIEW" and view.needs_reassessment
    assert not view.actionable and not view.effective_completion
    assert "NEEDS_REVIEW" in view.blockers
    with pytest.raises(VersionConflict,match="outstanding DONE"):
        confirm_done(dsn,uid,instance,conv,task,5,newer.report_id)
    assert report_obstacle(dsn,uid,instance,conv,task,5).version==6
    assert call(dsn,list_tasks,actor(uid),instance,goal)[0].status=="PLANNED"


def test_user_confirmation_can_be_corrected_and_later_reconfirmed(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    task,_,_=make_task(dsn,uid,instance,conv,goal,1)
    first_report=report_done(dsn,uid,instance,conv,task,1)
    first=confirm_done(dsn,uid,instance,conv,task,2,first_report.report_id)
    assert first.confirmation_id is not None
    cmd=correction_command(dsn,uid,instance,conv,task,3,
                           confirmation_id=first.confirmation_id)
    with pytest.raises(PermissionDenied,match="bound"):
        call(dsn,correct_task_outcome,actor(uid),
             cmd.model_copy(update={
                 "idempotency_key":uuid4().hex,
                 "reason":"A different, unapproved correction reason",
             }))
    corrected=call(dsn,correct_task_outcome,actor(uid),cmd)
    assert corrected.version==4
    second_report=report_done(dsn,uid,instance,conv,task,4)
    second=confirm_done(dsn,uid,instance,conv,task,5,second_report.report_id)
    assert second.version==6 and second.confirmation_id!=first.confirmation_id
    with pytest.raises(VersionConflict,match="latest effective"):
        call(dsn,correct_task_outcome,actor(uid),
             correction_command(dsn,uid,instance,conv,task,6,
                                confirmation_id=first.confirmation_id))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM task_user_confirmations WHERE task_id=%s",
            (task,),
        ).fetchone()[0]==2
        assert conn.execute(
            """SELECT superseded_confirmation_id,old_status,new_status
               FROM task_outcome_corrections WHERE task_id=%s""",
            (task,),
        ).fetchone()==(first.confirmation_id,"USER_CONFIRMED","NEEDS_REVIEW")
        assert conn.execute(
            "SELECT status FROM goal_records WHERE goal_id=%s",(goal,),
        ).fetchone()==("ACTIVE",)
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "DELETE FROM task_user_confirmations WHERE task_id=%s",(task,),
            )
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "UPDATE task_outcome_corrections SET reason='tampered' "
                "WHERE task_id=%s",(task,),
            )


def test_correction_pins_transitive_confirmation_lineage(dsn):
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    first,_,_=make_task(dsn,uid,instance,conv,goal,1,title="First")
    second,_,_=make_task(dsn,uid,instance,conv,goal,2,title="Second")
    third,_,_=make_task(dsn,uid,instance,conv,goal,3,title="Third")
    edge(dsn,uid,instance,conv,second,first,4)
    edge(dsn,uid,instance,conv,third,second,5)
    first_report=report_done(dsn,uid,instance,conv,first,1)
    first_done=confirm_done(dsn,uid,instance,conv,first,2,first_report.report_id)
    second_report=report_done(dsn,uid,instance,conv,second,2)
    second_done=confirm_done(dsn,uid,instance,conv,second,3,second_report.report_id)
    before={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert before[second].effective_completion
    assert before[third].actionable
    assert first_done.confirmation_id is not None
    assert second_done.confirmation_id is not None

    corrected=call(dsn,correct_task_outcome,actor(uid),
        correction_command(dsn,uid,instance,conv,first,3,
                           confirmation_id=first_done.confirmation_id))
    assert corrected.version==4
    after={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert after[second].status=="USER_CONFIRMED"  # Historical testimony preserved
    assert not after[second].effective_completion
    assert after[second].needs_reassessment
    assert first in after[second].unmet_dependencies
    assert not after[third].actionable
    assert second in after[third].unmet_dependencies
    third_report=report_done(dsn,uid,instance,conv,third,2)
    with pytest.raises(VersionConflict,match="prerequisite"):
        confirm_done(dsn,uid,instance,conv,third,3,third_report.report_id)

    # Even after reconfirming the upstream task, the second task's previous
    # confirmation remains invalid: it was based on a DIFFERENT confirmation.
    first_new=report_done(dsn,uid,instance,conv,first,4)
    first_reconfirmed=confirm_done(dsn,uid,instance,conv,first,5,
                                   first_new.report_id)
    assert first_reconfirmed.confirmation_id!=first_done.confirmation_id
    still={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert not still[second].effective_completion
    assert not still[third].actionable

    call(dsn,correct_task_outcome,actor(uid),
         correction_command(dsn,uid,instance,conv,second,4,
                            confirmation_id=second_done.confirmation_id))
    second_new=report_done(dsn,uid,instance,conv,second,5)
    second_reconfirmed=confirm_done(dsn,uid,instance,conv,second,6,
                                    second_new.report_id)
    assert second_reconfirmed.confirmation_id!=second_done.confirmation_id
    current={x.task_id:x for x in call(dsn,list_tasks,actor(uid),instance,goal)}
    assert current[second].effective_completion
    assert current[third].status=="USER_REPORTED_DONE"
    confirmed_third=confirm_done(dsn,uid,instance,conv,third,3,third_report.report_id)
    assert confirmed_third.confirmation_id is not None
    assert all(x.effective_completion for x in
               call(dsn,list_tasks,actor(uid),instance,goal))
    with psycopg.connect(dsn,autocommit=True) as conn:
        predecessor_rows=conn.execute(
            """SELECT prerequisite_confirmation_id FROM task_confirmation_prerequisites
               WHERE confirmation_id=%s AND prerequisite_task_id=%s""",
            (second_done.confirmation_id,first),
        ).fetchall()
        assert predecessor_rows==[(first_done.confirmation_id,)]
        newer_rows=conn.execute(
            """SELECT prerequisite_confirmation_id FROM task_confirmation_prerequisites
               WHERE confirmation_id=%s AND prerequisite_task_id=%s""",
            (second_reconfirmed.confirmation_id,first),
        ).fetchall()
        assert newer_rows==[(first_reconfirmed.confirmation_id,)]


def test_correction_authority_and_cross_persona_isolation(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,user,instance,conv)
    task,_,_=make_task(dsn,user,instance,conv,goal,1)
    done=report_done(dsn,user,instance,conv,task,1)
    foreign,foreign_instance,_=seed(dsn)
    foreign_conv=uuid4()
    foreign_command=correction_command(
        dsn,foreign,foreign_instance,foreign_conv,task,2,
        report_id=done.report_id,
    )
    with pytest.raises(PermissionDenied):
        call(dsn,correct_task_outcome,actor(foreign),foreign_command)
    current=correction_command(dsn,user,instance,conv,task,2,
                               report_id=done.report_id)
    # The stale version is independently approved: modifying the already
    # approved payload should instead fail the intentional approval hash gate.
    stale=correction_command(dsn,user,instance,conv,task,1,
                             report_id=done.report_id)
    with pytest.raises(VersionConflict):
        call(dsn,correct_task_outcome,actor(user),stale)
    assert call(dsn,correct_task_outcome,actor(user),current).version==3


def test_concurrent_confirmation_vs_correction_is_one_serialized_commit(dsn):
    from threading import Barrier
    uid,instance,_=seed(dsn)
    conv=uuid4()
    goal=make_goal(dsn,uid,instance,conv)
    task,_,_=make_task(dsn,uid,instance,conv,goal,1)
    reported=report_done(dsn,uid,instance,conv,task,1)
    msg=turn(dsn,uid,instance,conv,
             "I confirm my completed task after checking its outcome.")
    confirmation=approve(dsn,uid,instance,ConfirmUserTaskCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=msg.turn_id,
        evidence_quote="I confirm my completed task",
        task_id=task,expected_task_version=2,
        report_id=reported.report_id,
    ))
    correction=correction_command(dsn,uid,instance,conv,task,2,
                                  report_id=reported.report_id)
    barrier=Barrier(2)

    def race(fn,cmd):
        barrier.wait(timeout=15)
        try:
            return call(dsn,fn,actor(uid),cmd)
        except VersionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(race,confirm_user_task,confirmation)
        b=pool.submit(race,correct_task_outcome,correction)
        results=[a.result(timeout=20),b.result(timeout=20)]
    assert sum(result is not None for result in results)==1
    view=call(dsn,list_tasks,actor(uid),instance,goal)[0]
    assert view.version==3
    assert view.status in ("USER_CONFIRMED","NEEDS_REVIEW")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE user_id=%s AND instance_id=%s AND
                 event_type IN ('goal.task.user_confirmed.v1',
                                'goal.task.outcome.corrected.v1')""",
            (uid,instance),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM command_receipts
               WHERE user_id=%s AND instance_id=%s AND
                 command_type IN ('goal.task.user.confirm.v1',
                                  'goal.task.outcome.correct.v1')""",
            (uid,instance),
        ).fetchone()[0]==1
