"""P3-01 PostgreSQL acceptance: approved goals, evidence and alternatives."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.goal_contracts import (
    CreateGoalCommand,RecordGoalEvidenceCommand,ProposeGoalStrategyCommand,
    SelectGoalStrategyCommand,SetGoalStatusCommand,goal_action_approval_phrase,
)
from persona_agent.goal_state import (
    create_goal,record_goal_evidence,propose_goal_strategy,
    select_goal_strategy,set_goal_status,list_goals,list_goal_strategies,
    list_goal_evidence,
)
from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
from persona_agent.runtime import (
    IdempotencyConflict,PermissionDenied,StaleSelection,VersionConflict,
)


def call(dsn,fn,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def actor(user):
    return TrustedActor(user_id=user,source_id="phase3-authenticated-user")


def seed(dsn,*,alternate=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    other=None
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,"phase3-"+str(persona)),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'Goal companion')""",
                (instance,user,persona),
            )
            if alternate:
                p2,other=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (p2,"phase3-"+str(p2)),
                )
                conn.execute(
                    """INSERT INTO persona_instances(id,user_id,persona_id,label)
                       VALUES (%s,%s,%s,'Alternative')""",
                    (other,user,p2),
                )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",
                (user,instance),
            )
    return user,instance,other


def turn(dsn,user,instance,conversation,text,*,generation=1):
    return call(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=generation,
        idempotency_key=str(uuid4()),text=text,
    ))


def authorize(dsn,user,instance,command):
    """User confirms the exact proposed goal action in a separate turn."""
    confirmation=turn(
        dsn,user,instance,command.conversation_id,
        goal_action_approval_phrase(command),
        generation=command.expected_generation,
    )
    return command.model_copy(update={"approval_turn_id":confirmation.turn_id})


def choose(dsn,user,instance,command):
    return call(dsn,select_goal_strategy,actor(user),
                authorize(dsn,user,instance,command))


def root(dsn,user,instance,conversation,source,*,goal_id=None,
         title="Improve eating habits",parent=None,parent_version=None,
         generation=1,idempotency=None):
    goal_id=goal_id or uuid4()
    command=CreateGoalCommand(
        instance_id=instance,expected_generation=generation,
        idempotency_key=idempotency or str(uuid4()),
        conversation_id=conversation,evidence_turn_id=source.turn_id,
        evidence_quote="I want to improve my eating habits",
        goal_id=goal_id,parent_goal_id=parent,
        expected_parent_version=parent_version,title=title,
        desired_outcome="A healthier and sustainable diet",
        success_criterion="User reports meeting agreed weekly meal targets",
        horizon="MEDIUM",
        evaluation_start_at=datetime.now(timezone.utc),
        evaluation_end_at=datetime.now(timezone.utc)+timedelta(days=30),
    )
    command=authorize(dsn,user,instance,command)
    return goal_id,call(dsn,create_goal,actor(user),command),command


def strategy(dsn,user,instance,conversation,source,goal_id,title):
    sid=uuid4()
    result=call(dsn,propose_goal_strategy,actor(user),ProposeGoalStrategyCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="Try planning meals on Sundays",
        goal_id=goal_id,strategy_id=sid,title=title,
        approach="Prepare ingredients in advance and review weekly",
        suitability_note="Assess time constraints and food preferences",
    ))
    return sid,result


def test_root_subgoal_candidates_and_selection_preserve_approved_outcome(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(
        dsn,user,instance,conversation,
        "I want to improve my eating habits. Try planning meals on Sundays.",
    )
    goal_id,created,_=root(dsn,user,instance,conversation,source)
    assert created.action=="GOAL_CREATED" and created.goal_version==1
    first,proposed=strategy(
        dsn,user,instance,conversation,source,goal_id,"Weekend preparation",
    )
    second,_=strategy(
        dsn,user,instance,conversation,source,goal_id,"Daily planning",
    )
    assert proposed.action=="STRATEGY_PROPOSED"
    assert [x.status for x in call(
        dsn,list_goal_strategies,actor(user),instance,goal_id
    )]==["CANDIDATE","CANDIDATE"]
    assert call(dsn,list_goals,actor(user),instance)[0].selected_strategy_id is None

    selected=choose(dsn,user,instance,SelectGoalStrategyCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="Try planning meals on Sundays",
        goal_id=goal_id,strategy_id=first,expected_goal_version=1,
    ))
    assert selected.action=="STRATEGY_SELECTED" and selected.goal_version==2
    view=call(dsn,list_goals,actor(user),instance)[0]
    assert view.selected_strategy_id==first
    assert view.desired_outcome=="A healthier and sustainable diet"
    assert view.success_criterion=="User reports meeting agreed weekly meal targets"

    child=turn(dsn,user,instance,conversation,
               "I want to improve my eating habits by preparing lunch.")
    child_id,child_result,_=root(
        dsn,user,instance,conversation,child,
        title="Prepare weekday lunches",parent=goal_id,parent_version=2,
    )
    assert child_result.goal_version==1
    result=call(dsn,list_goals,actor(user),instance)
    assert len(result)==2
    assert result[0].version==3  # child creation is a CAS parent revision
    assert result[1].parent_goal_id==goal_id and result[1].depth==1
    assert result[0].selected_strategy_id==first
    assert result[0].desired_outcome=="A healthier and sustainable diet"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT strategy_id,goal_version
               FROM goal_strategy_selections WHERE goal_id=%s""",
            (goal_id,),
        ).fetchone()==(first,2)
        assert conn.execute(
            """SELECT count(*) FROM events WHERE user_id=%s AND instance_id=%s
               AND event_type IN ('goal.created.v1',
                   'goal.strategy.proposed.v1','goal.strategy.selected.v1')""",
            (user,instance),
        ).fetchone()[0]==5
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE privacy_class='INTERNAL' AND user_id=%s",
            (user,),
        ).fetchone()[0]==2


def test_goal_progress_is_source_linked_and_never_self_verified(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,
                "I want to improve my eating habits.")
    gid,_,_=root(dsn,user,instance,conversation,source)
    outcome=turn(
        dsn,user,instance,conversation,
        "I successfully prepared lunch today.",
    )
    evidence_id=uuid4()
    command=RecordGoalEvidenceCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key="goal-progress-"+uuid4().hex,
        conversation_id=conversation,evidence_turn_id=outcome.turn_id,
        evidence_quote="successfully prepared lunch",
        goal_id=gid,expected_goal_version=1,evidence_kind="SUCCESS_REPORTED",
        evidence_id=evidence_id,
    )
    recorded=call(dsn,record_goal_evidence,actor(user),command)
    assert recorded.entity_id==evidence_id and recorded.goal_version==2
    replay=call(dsn,record_goal_evidence,actor(user),command)
    assert replay.replayed and replay.event_id==recorded.event_id
    evidence=call(dsn,list_goal_evidence,actor(user),instance,gid)
    assert len(evidence)==1
    assert evidence[0].kind=="SUCCESS_REPORTED"
    assert evidence[0].epistemic_status=="USER_REPORTED"
    assert evidence[0].classification_status=="UNREVIEWED"
    assert evidence[0].source_turn_id==outcome.turn_id
    view=call(dsn,list_goals,actor(user),instance)[0]
    assert view.status=="ACTIVE" and view.evidence_count==1
    assert view.selected_strategy_id is None
    with pytest.raises(VersionConflict):
        call(dsn,record_goal_evidence,actor(user),command.model_copy(
            update={
                "idempotency_key":str(uuid4()),
                "expected_goal_version":2,
                # A different excerpt from the same source turn must
                # not be independently counted as new evidence.
                "evidence_quote":"prepared lunch",
            },
        ))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM cognitive_reflections
               WHERE user_id=%s AND instance_id=%s""",
            (user,instance),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT epistemic_status,source_quote FROM goal_evidence
               WHERE evidence_id=%s""",
            (evidence_id,),
        ).fetchone()==("USER_REPORTED","successfully prepared lunch")


def test_pause_resume_and_abandon_require_explicit_user_evidence(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    first=turn(dsn,user,instance,conv,"I want to improve my eating habits.")
    goal,_,_=root(dsn,user,instance,conv,first)
    pause=turn(dsn,user,instance,conv,"Pause my eating goal for now.")
    resume=turn(dsn,user,instance,conv,"Resume my eating goal now.")
    abandon=turn(dsn,user,instance,conv,"Abandon my eating goal.")
    def change(version,status,source,quote):
        command=SetGoalStatusCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=str(uuid4()),conversation_id=conv,
            evidence_turn_id=source.turn_id,
            evidence_quote=quote,
            goal_id=goal,expected_goal_version=version,
            new_status=status,
        )
        return call(dsn,set_goal_status,actor(user),
                    authorize(dsn,user,instance,command))
    assert change(1,"PAUSED",pause,"Pause my eating goal").goal_version==2
    with pytest.raises(VersionConflict):
        change(1,"ACTIVE",resume,"Resume my eating goal")
    assert change(2,"ACTIVE",resume,"Resume my eating goal").goal_version==3
    assert change(3,"ABANDONED",abandon,"Abandon my eating goal").goal_version==4
    with pytest.raises(VersionConflict):
        change(4,"ACTIVE",resume,"Resume my eating goal")
    assert call(dsn,list_goals,actor(user),instance)[0].status=="ABANDONED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT old_status,new_status,goal_version
               FROM goal_status_revisions WHERE goal_id=%s
               ORDER BY goal_version""",(goal,),
        ).fetchall()==[
            ("ACTIVE","PAUSED",2),
            ("PAUSED","ACTIVE",3),
            ("ACTIVE","ABANDONED",4),
        ]


def test_cross_persona_scope_and_generation_fencing(dsn):
    user,instance,other=seed(dsn,alternate=True)
    foreign,foreign_instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"I want to improve my eating habits.")
    gid,first,command=root(dsn,user,instance,conv,source)
    with pytest.raises(PermissionDenied):
        call(dsn,list_goals,actor(foreign),instance)
    assert call(dsn,list_goals,actor(user),other)==()
    assert call(dsn,list_goals,actor(foreign),foreign_instance)==()
    with pytest.raises(PermissionDenied):
        call(dsn,create_goal,actor(foreign),
             command.model_copy(update={
                 "goal_id":uuid4(),"idempotency_key":str(uuid4()),
             }))
    switched=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    assert switched.generation==2
    assert call(dsn,create_goal,actor(user),command).replayed
    with pytest.raises(StaleSelection):
        call(dsn,create_goal,actor(user),command.model_copy(
            update={"goal_id":uuid4(),"idempotency_key":str(uuid4())},
        ))
    with pytest.raises(IdempotencyConflict):
        call(dsn,create_goal,actor(user),command.model_copy(
            update={"title":"silent changed outcome"},
        ))
    with pytest.raises(StaleSelection):
        call(dsn,list_goals,actor(user),instance)
    with pytest.raises(StaleSelection):
        call(dsn,list_goal_strategies,actor(user),instance,gid)
    with pytest.raises(StaleSelection):
        call(dsn,list_goal_evidence,actor(user),instance,gid)
    assert call(dsn,list_goals,actor(user),other)==()
    restored=call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=other,target_instance_id=instance,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    assert restored.generation==3
    assert call(dsn,list_goals,actor(user),instance)[0].goal_id==gid
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s",(user,),
        ).fetchone()[0]==1


def test_cannot_select_other_goals_strategy_or_inferred_interpretation(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(
        dsn,user,instance,conv,
        "I want to improve my eating habits. Try planning meals on Sundays.",
    )
    gid,_,_=root(dsn,user,instance,conv,source)
    other,_,_=root(dsn,user,instance,conv,source)
    sid,_=strategy(dsn,user,instance,conv,source,gid,"Sunday plan")
    with pytest.raises(PermissionDenied):
        choose(dsn,user,instance,SelectGoalStrategyCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=str(uuid4()),conversation_id=conv,
            evidence_turn_id=source.turn_id,
            evidence_quote="Try planning meals on Sundays",
            goal_id=other,expected_goal_version=1,strategy_id=sid,
        ))
    assert call(dsn,list_goals,actor(user),instance)[0].selected_strategy_id is None
    # Input alone, even when it mentions goal progress, does not silently
    # create a goal/strategy. Only approved owner-domain commands can.
    turn(dsn,user,instance,conv,"Maybe the model should change my goal.")
    assert len(call(dsn,list_goals,actor(user),instance))==2


def test_concurrent_subgoal_cas_and_parent_budget(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    first=turn(dsn,user,instance,conv,"I want to improve my eating habits.")
    gid,_,_=root(dsn,user,instance,conv,first)
    evidence=turn(
        dsn,user,instance,conv,
        "I want to improve my eating habits with daily cooking.",
    )

    def append_child(i):
        try:
            return root(
                dsn,user,instance,conv,evidence,title=f"Healthy meal {i}",
                parent=gid,parent_version=1,
            )[1]
        except VersionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(append_child,1)
        b=pool.submit(append_child,2)
        results=(a.result(timeout=15),b.result(timeout=15))
    assert sum(x is not None for x in results)==1
    goals=call(dsn,list_goals,actor(user),instance)
    assert len(goals)==2
    assert goals[0].version==2

    for i in range(11):
        root(dsn,user,instance,conv,evidence,
             title=f"Another subgoal {i}",parent=gid,parent_version=i+2)
    assert len(call(dsn,list_goals,actor(user),instance))==13
    with pytest.raises(VersionConflict,match="subgoal budget"):
        root(dsn,user,instance,conv,evidence,
             title="Too many children",parent=gid,parent_version=13)


def test_bounded_recursive_depth_and_date_validation(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"I want to improve my eating habits.")
    goal,_,_=root(dsn,user,instance,conv,source)
    current=goal
    for depth in range(1,6):
        current,_,_=root(
            dsn,user,instance,conv,source,title=f"Subgoal depth {depth}",
            parent=current,parent_version=1,
        )
    with pytest.raises(VersionConflict,match="depth limit"):
        root(
            dsn,user,instance,conv,source,title="Excessive decomposition",
            parent=current,parent_version=1,
        )
    with pytest.raises(ValueError):
        CreateGoalCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=str(uuid4()),conversation_id=conv,
            evidence_turn_id=source.turn_id,evidence_quote="I want",
            goal_id=uuid4(),title="Bad dates",desired_outcome="Better health",
            success_criterion="Reliable evidence",
            horizon="SHORT",evaluation_start_at=datetime.now(),
        )


def test_user_report_and_strategy_switch_do_not_mutate_main_objective(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    initial=turn(
        dsn,user,instance,conv,
        "I want to improve my eating habits. Try planning meals on Sundays.",
    )
    goal,_,_=root(dsn,user,instance,conv,initial)
    first,_=strategy(dsn,user,instance,conv,initial,goal,"Weekend batch cooking")
    another,_=strategy(dsn,user,instance,conv,initial,goal,"Weekday substitutions")
    selection=choose(dsn,user,instance,SelectGoalStrategyCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=initial.turn_id,
        evidence_quote="Try planning meals on Sundays",
        goal_id=goal,strategy_id=first,expected_goal_version=1,
    ))
    obstacle=turn(
        dsn,user,instance,conv,
        "The batch cooking plan did not work this week.",
    )
    evidence=call(dsn,record_goal_evidence,actor(user),
                  RecordGoalEvidenceCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=obstacle.turn_id,
        evidence_quote="did not work this week",
        goal_id=goal,expected_goal_version=2,
        evidence_kind="OBSTACLE",evidence_id=uuid4(),
    ))
    assert evidence.goal_version==3
    # No automatic algorithm substitutes a new objective or approach.
    assert call(dsn,list_goals,actor(user),instance)[0].selected_strategy_id==first
    report=turn(dsn,user,instance,conv,
                "Instead, I want to try the second strategy.")
    changed=choose(dsn,user,instance,SelectGoalStrategyCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=report.turn_id,
        evidence_quote="I want to try the second strategy",
        goal_id=goal,strategy_id=another,expected_goal_version=3,
    ))
    assert changed.goal_version==4
    view=call(dsn,list_goals,actor(user),instance)[0]
    assert view.selected_strategy_id==another
    assert view.desired_outcome=="A healthier and sustainable diet"
    assert view.evidence_count==1 and view.status=="ACTIVE"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT strategy_id,goal_version FROM goal_strategy_selections
               WHERE goal_id=%s ORDER BY goal_version""",(goal,),
        ).fetchall()==[(first,2),(another,4)]


def test_action_bound_confirmation_rejects_nonapproval_and_tampering(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,
                "Maybe I should improve my eating habits, but I am not sure.")
    command=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,
        evidence_quote="Maybe I should improve my eating habits",
        goal_id=uuid4(),title="Healthy meals",
        desired_outcome="Eat better",
        success_criterion="User reports weekly meals",horizon="MEDIUM",
    )
    with pytest.raises(PermissionDenied,match="separate user confirmation"):
        call(dsn,create_goal,actor(user),command)
    wrong=turn(dsn,user,instance,conv,
               "I do NOT "+goal_action_approval_phrase(command))
    with pytest.raises(PermissionDenied,match="does not match"):
        call(dsn,create_goal,actor(user),command.model_copy(
            update={"approval_turn_id":wrong.turn_id},
        ))
    approved=authorize(dsn,user,instance,command)
    assert call(dsn,create_goal,actor(user),approved).action=="GOAL_CREATED"
    with pytest.raises(PermissionDenied,match="does not match"):
        call(dsn,create_goal,actor(user),approved.model_copy(update={
            "idempotency_key":str(uuid4()),"goal_id":uuid4(),
            "desired_outcome":"An unapproved substituted objective",
        }))
    with psycopg.connect(dsn,autocommit=True) as conn:
        event=conn.execute(
            "SELECT payload->>'approval_turn_id' FROM events "
            "WHERE event_type='goal.created.v1' AND user_id=%s",(user,),
        ).fetchone()
        assert event==(str(approved.approval_turn_id),)


def test_incorrectly_labelled_user_report_remains_unreviewed(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"I want to improve my eating habits.")
    gid,_,_=root(dsn,user,instance,conv,source)
    negative=turn(dsn,user,instance,conv,
                  "I did not prepare a healthy lunch this week.")
    call(dsn,record_goal_evidence,actor(user),RecordGoalEvidenceCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=negative.turn_id,
        evidence_quote="did not prepare a healthy lunch",
        goal_id=gid,expected_goal_version=1,
        evidence_kind="SUCCESS_REPORTED",evidence_id=uuid4(),
    ))
    reported=call(dsn,list_goal_evidence,actor(user),instance,gid)
    assert reported[0].kind=="SUCCESS_REPORTED"
    assert reported[0].classification_status=="UNREVIEWED"
    assert reported[0].source_quote=="did not prepare a healthy lunch"
    assert call(dsn,list_goals,actor(user),instance)[0].status=="ACTIVE"
