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
    CreateTaskCommand, AddTaskDependencyCommand, ReportTaskOutcomeCommand,
    SetTaskPriorityCommand, task_approval_phrase,
)
from persona_agent.task_state import (
    create_task, add_task_dependency, report_task_outcome,
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
        expected_goal_version=goal_version,title=title,executor=executor,
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
    assert "PREREQUISITE_NOT_VERIFIED" in items[second].blockers
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
    assert "AWAITING_VERIFICATION" in items[first].blockers
    assert "PREREQUISITE_NOT_VERIFIED" in items[second].blockers


def test_cycle_and_cross_goal_dependency_rejected(dsn):
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
    with pytest.raises(PermissionDenied):
        edge(dsn,uid,instance,conv,b,c,4)


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
            title="Check ingredients",executor="USER",
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
        expected_goal_version=1,title="Preparation",executor="USER",
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
