"""P3-02 PostgreSQL acceptance: bounded verifiable tasks and dependency gates."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")

from test_phase3_goals import actor, call, root, seed, turn
from persona_agent.goal_contracts import SetGoalStatusCommand, goal_action_approval_phrase
from persona_agent.goal_state import set_goal_status
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection, VersionConflict,
)
from persona_agent.task_contracts import (
    AddTaskDependencyCommand, CancelTaskCommand, ConfirmUserTaskCommand,
    CreateTaskCommand, SetTaskPriorityCommand, task_approval_phrase,
)
from persona_agent.task_state import (
    add_task_dependency, cancel_task, confirm_user_task, create_task,
    list_tasks, set_task_priority,
)


def ctx(dsn, *, alternate=False):
    user,instance,other=seed(dsn,alternate=alternate)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,
                "I want to improve my eating habits and plan next steps.")
    goal,_,_=root(dsn,user,instance,conv,source)
    instruction=turn(dsn,user,instance,conv,"I want to plan tasks for healthier meals.")
    return user,instance,other,conv,goal,instruction


def approve(dsn,user,instance,cmd):
    result=turn(dsn,user,instance,cmd.conversation_id,
                task_approval_phrase(cmd),generation=cmd.expected_generation)
    return cmd.model_copy(update={"approval_turn_id":result.turn_id})


def create(dsn,user,instance,conv,goal,source,*,executor="USER",
           title="Prepare healthy lunch",capability=None,goal_version=1):
    tid=uuid4()
    cmd=CreateTaskCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,
        evidence_quote="I want to plan tasks",
        task_id=tid,goal_id=goal,expected_goal_version=goal_version,
        title=title,action_description="Prepare a balanced lunch tomorrow",
        completion_criterion="Report whether lunch was actually prepared",
        executor=executor,capability=capability,
    )
    result=call(dsn,create_task,actor(user),approve(dsn,user,instance,cmd))
    return tid,result


def view(dsn,user,instance,task_id):
    return next(t for t in call(dsn,list_tasks,actor(user),instance)
                if t.task_id==task_id)


def depend(dsn,user,instance,conv,source,task_id,depends_on_id,version):
    cmd=AddTaskDependencyCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,evidence_quote="I want to plan tasks",
        task_id=task_id,depends_on_id=depends_on_id,expected_task_version=version,
    )
    return call(dsn,add_task_dependency,actor(user),approve(dsn,user,instance,cmd))


def priority(dsn,user,instance,conv,source,tid,version,rule,
             *,relative=None,deadline=None):
    cmd=SetTaskPriorityCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,evidence_quote="I want to plan tasks",
        task_id=tid,expected_task_version=version,rule=rule,
        related_task_id=relative,deadline=deadline,
    )
    return call(dsn,set_task_priority,actor(user),approve(dsn,user,instance,cmd))


def confirm(dsn,user,instance,conv,tid,version):
    source=turn(dsn,user,instance,conv,"I completed the task as agreed.")
    cmd=ConfirmUserTaskCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,evidence_quote="I completed the task",
        task_id=tid,expected_task_version=version,report_id=uuid4(),
    )
    approved=approve(dsn,user,instance,cmd)
    result=call(dsn,confirm_user_task,actor(user),approved)
    return result,approved


def test_user_completion_gates_dependent_work_and_preserves_provenance(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    first,_=create(dsn,user,inst,conv,goal,source,title="Buy ingredients")
    second,_=create(dsn,user,inst,conv,goal,source,title="Cook lunch")
    edge=depend(dsn,user,inst,conv,source,second,first,1)
    assert edge.task_version==2
    waiting=view(dsn,user,inst,second)
    assert waiting.actionability=="WAITING_DEPENDENCIES"
    assert waiting.unmet_dependencies==(first,)
    assert not waiting.dispatch_authorized
    with pytest.raises(VersionConflict,match="not actionable"):
        confirm(dsn,user,inst,conv,second,2)
    assert confirm(dsn,user,inst,conv,first,1)[0].task_version==2
    assert view(dsn,user,inst,first).actionability=="COMPLETE_USER_CONFIRMED"
    assert view(dsn,user,inst,second).actionability=="READY"
    result,command=confirm(dsn,user,inst,conv,second,2)
    assert result.action=="USER_COMPLETION_RECORDED"
    assert view(dsn,user,inst,second).status=="USER_CONFIRMED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT epistemic_status,source_turn_id,approval_turn_id
               FROM task_completion_reports WHERE task_id=%s""",
            (second,),
        ).fetchone()==(
            "USER_CONFIRMED",command.evidence_turn_id,command.approval_turn_id,
        )
        assert conn.execute(
            """SELECT count(*) FROM events e JOIN outbox o ON o.event_id=e.event_id
               WHERE e.instance_id=%s AND e.event_type LIKE %s""",
            (inst,"task.%"),
        ).fetchone()[0]>=5


def test_agent_tasks_are_plans_not_completed_tool_executions(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    agent,_=create(dsn,user,inst,conv,goal,source,executor="AGENT",
                   title="Research recipes",capability="research.read")
    no_cap,_=create(dsn,user,inst,conv,goal,source,executor="AGENT",
                    title="Unsupported tool",capability=None)
    assert view(dsn,user,inst,agent).actionability=="READY"
    assert view(dsn,user,inst,no_cap).actionability=="NEEDS_CAPABILITY"
    assert view(dsn,user,inst,agent).dispatch_authorized is False
    with pytest.raises(PermissionDenied,match="agent work"):
        confirm(dsn,user,inst,conv,agent,1)
    assert view(dsn,user,inst,agent).status=="OPEN"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM operations WHERE task_id=%s",
                            (agent,)).fetchone()[0]==0


def test_dependency_cycle_detection_including_run_after_and_cas(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    a,_=create(dsn,user,inst,conv,goal,source,title="Task A")
    b,_=create(dsn,user,inst,conv,goal,source,title="Task B")
    c,_=create(dsn,user,inst,conv,goal,source,title="Task C")
    depend(dsn,user,inst,conv,source,a,b,1)
    depend(dsn,user,inst,conv,source,b,c,1)
    with pytest.raises(VersionConflict,match="cycle"):
        depend(dsn,user,inst,conv,source,c,a,1)
    with pytest.raises(VersionConflict,match="cycle"):
        priority(dsn,user,inst,conv,source,c,1,"RUN_AFTER",relative=a)
    with pytest.raises(ValueError,match="itself"):
        AddTaskDependencyCommand(
            instance_id=inst,expected_generation=1,idempotency_key="self",
            conversation_id=conv,evidence_turn_id=source.turn_id,
            evidence_quote="I want to plan tasks",
            task_id=a,depends_on_id=a,expected_task_version=2,
        )
    with pytest.raises(VersionConflict,match="already exists"):
        depend(dsn,user,inst,conv,source,a,b,2)
    assert view(dsn,user,inst,a).version==2


def test_parallel_graph_edits_have_one_version_winner(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    a,_=create(dsn,user,inst,conv,goal,source,title="A")
    b,_=create(dsn,user,inst,conv,goal,source,title="B")
    c,_=create(dsn,user,inst,conv,goal,source,title="C")
    def contender(dep):
        try:
            return depend(dsn,user,inst,conv,source,a,dep,1)
        except VersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(contender,(b,c)))
    assert sum(r is not None for r in results)==1
    assert view(dsn,user,inst,a).version==2


def test_priority_rules_distinguish_preference_exclusive_and_suspend(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    a,_=create(dsn,user,inst,conv,goal,source,title="A")
    b,_=create(dsn,user,inst,conv,goal,source,title="B")
    assert priority(dsn,user,inst,conv,source,a,1,"PREFER_OVER",relative=b).task_version==2
    assert view(dsn,user,inst,b).actionability=="READY"  # prefer != only
    priority(dsn,user,inst,conv,source,a,2,"ONLY")
    assert view(dsn,user,inst,b).actionability=="EXCLUSIVE_OTHER"
    with pytest.raises(VersionConflict,match="exclusive"):
        priority(dsn,user,inst,conv,source,b,1,"ONLY")
    priority(dsn,user,inst,conv,source,a,3,"SUSPEND")
    assert view(dsn,user,inst,a).actionability=="SUSPENDED"
    priority(dsn,user,inst,conv,source,a,4,"RESUME")
    priority(dsn,user,inst,conv,source,a,5,"CLEAR_ONLY")
    assert view(dsn,user,inst,b).actionability=="READY"
    assert view(dsn,user,inst,a).preferred_over_task_id==b
    priority(dsn,user,inst,conv,source,a,6,"CLEAR_PREFERENCE")
    assert view(dsn,user,inst,a).preferred_over_task_id is None


def test_priority_deadline_and_run_after_are_separate_gates(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    a,_=create(dsn,user,inst,conv,goal,source,title="A")
    b,_=create(dsn,user,inst,conv,goal,source,title="B")
    after=priority(dsn,user,inst,conv,source,a,1,"RUN_AFTER",relative=b)
    assert after.task_version==2
    assert view(dsn,user,inst,a).actionability=="WAITING_DEPENDENCIES"
    deadline=datetime.now(timezone.utc)+timedelta(days=4)
    priority(dsn,user,inst,conv,source,a,2,"SET_DEADLINE",deadline=deadline)
    assert view(dsn,user,inst,a).deadline==deadline
    with pytest.raises(VersionConflict,match="cycle"):
        depend(dsn,user,inst,conv,source,b,a,1)
    priority(dsn,user,inst,conv,source,a,3,"CLEAR_RUN_AFTER")
    assert view(dsn,user,inst,a).actionability=="READY"
    priority(dsn,user,inst,conv,source,a,4,"CLEAR_DEADLINE")
    assert view(dsn,user,inst,a).deadline is None


def test_paused_goal_ancestor_blocks_child_task_without_mutating_progress(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    child_source=turn(dsn,user,inst,conv,
                      "I want to improve my eating habits by preparing lunch.")
    child,_,_=root(dsn,user,inst,conv,child_source,
                   title="Prepare healthy lunches",parent=goal,parent_version=1)
    tid,_=create(dsn,user,inst,conv,child,source,title="Healthy lunch")
    assert view(dsn,user,inst,tid).actionability=="READY"
    pause=turn(dsn,user,inst,conv,"Pause my meal goal now.")
    cmd=SetGoalStatusCommand(
        instance_id=inst,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conv,evidence_turn_id=pause.turn_id,
        evidence_quote="Pause my meal goal",goal_id=goal,
        expected_goal_version=2,new_status="PAUSED",
    )
    approval=turn(dsn,user,inst,conv,goal_action_approval_phrase(cmd))
    call(dsn,set_goal_status,actor(user),cmd.model_copy(
        update={"approval_turn_id":approval.turn_id},
    ))
    assert view(dsn,user,inst,tid).actionability=="GOAL_INACTIVE"
    with pytest.raises(VersionConflict,match="not actionable"):
        confirm(dsn,user,inst,conv,tid,1)


def test_selected_persona_scope_and_historical_idempotent_replay(dsn):
    user,inst,other,conv,goal,source=ctx(dsn,alternate=True)
    cmd=CreateTaskCommand(
        instance_id=inst,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,evidence_quote="I want to plan tasks",
        task_id=uuid4(),goal_id=goal,expected_goal_version=1,
        title="Meal planning",action_description="Plan weekly meals",
        completion_criterion="User confirms a meal plan",executor="USER",
    )
    approved=approve(dsn,user,inst,cmd)
    first=call(dsn,create_task,actor(user),approved)
    assert call(dsn,create_task,actor(user),approved).replayed
    with pytest.raises(IdempotencyConflict):
        call(dsn,create_task,actor(user),approved.model_copy(
            update={"title":"Changed without new approval"},
        ))
    changed=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=inst,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert changed.generation==2
    assert call(dsn,create_task,actor(user),approved).replayed
    with pytest.raises(StaleSelection):
        call(dsn,list_tasks,actor(user),inst)
    assert call(dsn,list_tasks,actor(user),other)==()
    with pytest.raises(StaleSelection):
        call(dsn,create_task,actor(user),approved.model_copy(
            update={"idempotency_key":str(uuid4()),"task_id":uuid4()},
        ))
    assert first.task_id==cmd.task_id


def test_unconfirmed_task_creation_and_wrong_approval_are_rejected(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    cmd=CreateTaskCommand(
        instance_id=inst,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="I want to plan tasks",task_id=uuid4(),
        goal_id=goal,expected_goal_version=1,title="Prepare meal",
        action_description="Prepare meal",completion_criterion="Actually prepare meal",
        executor="USER",
    )
    with pytest.raises(PermissionDenied,match="separate user confirmation"):
        call(dsn,create_task,actor(user),cmd)
    negative=turn(dsn,user,inst,conv,"Do NOT "+task_approval_phrase(cmd))
    with pytest.raises(PermissionDenied,match="does not match"):
        call(dsn,create_task,actor(user),cmd.model_copy(
            update={"approval_turn_id":negative.turn_id},
        ))
    with pytest.raises(ValueError):
        CreateTaskCommand(
            **cmd.model_dump(exclude={"capability","approval_turn_id"}),
            capability="run_shell",
        )


def test_cancelled_predecessor_never_unblocks_successor(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    a,_=create(dsn,user,inst,conv,goal,source,title="Predecessor")
    b,_=create(dsn,user,inst,conv,goal,source,title="Successor")
    depend(dsn,user,inst,conv,source,b,a,1)
    cmd=CancelTaskCommand(
        instance_id=inst,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,evidence_quote="I want to plan tasks",
        task_id=a,expected_task_version=1,
    )
    out=call(dsn,cancel_task,actor(user),approve(dsn,user,inst,cmd))
    assert out.action=="TASK_CANCELLED"
    assert view(dsn,user,inst,a).actionability=="CANCELLED"
    assert view(dsn,user,inst,b).actionability=="WAITING_DEPENDENCIES"
    with pytest.raises(VersionConflict):
        confirm(dsn,user,inst,conv,a,2)


def test_exclusive_priority_expires_when_user_completes_or_cancels_task(dsn):
    user,inst,_,conv,goal,source=ctx(dsn)
    a,_=create(dsn,user,inst,conv,goal,source,title="Task A")
    b,_=create(dsn,user,inst,conv,goal,source,title="Task B")
    c,_=create(dsn,user,inst,conv,goal,source,title="Task C")
    priority(dsn,user,inst,conv,source,a,1,"ONLY")
    assert view(dsn,user,inst,b).actionability=="EXCLUSIVE_OTHER"
    confirm(dsn,user,inst,conv,a,2)
    assert view(dsn,user,inst,b).actionability=="READY"
    priority(dsn,user,inst,conv,source,b,1,"ONLY")
    assert view(dsn,user,inst,c).actionability=="EXCLUSIVE_OTHER"
    cancelled=CancelTaskCommand(
        instance_id=inst,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="I want to plan tasks",task_id=b,expected_task_version=2,
    )
    call(dsn,cancel_task,actor(user),approve(dsn,user,inst,cancelled))
    assert view(dsn,user,inst,c).actionability=="READY"
    assert view(dsn,user,inst,b).exclusive_only is False
