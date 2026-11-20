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
    with pytest.raises(StaleSelection):
        call(dsn,list_goals,actor(user),other)
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


# MVP-03A.1: narrow Goal-owned delegated decomposition, with NO new user
# confirmation for a contributing child and NO grant to dispatch tools.
from persona_agent.goal_delegation_contracts import (
    DeriveWeeklyWalkingMilestoneCommand, weekly_walk_target,
)
from persona_agent.goal_state import derive_weekly_walking_milestone
from persona_agent.lifecycle import TrustedPlatformActor


def approved_walking_root(dsn, *, criterion="Complete three walks per week"):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,
                "I want to walk three times a week.")
    goal_id=uuid4()
    proposed=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="I want to walk three times a week",
        goal_id=goal_id,title="Walk regularly",
        desired_outcome="Walk three times per week",
        success_criterion=criterion,horizon="OPEN",
    )
    call(dsn,create_goal,actor(user),authorize(dsn,user,instance,proposed))
    return user,instance,goal_id


def derived_walking_cmd(instance,parent,*,count=1,version=1):
    return DeriveWeeklyWalkingMilestoneCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),goal_id=uuid4(),
        parent_goal_id=parent,expected_parent_version=version,
        weekly_walks=count,
    )


def goal_decomposer(user):
    return TrustedPlatformActor(
        user_id=user,source_id="goal.delegation.v1",
    )


def test_delegated_weekly_walk_milestone_is_bounded_and_source_linked(dsn):
    user,instance,parent=approved_walking_root(dsn)
    command=derived_walking_cmd(instance,parent,count=1)
    result=call(dsn,derive_weekly_walking_milestone,
                goal_decomposer(user),command)
    assert result.action=="GOAL_CREATED"
    assert call(dsn,derive_weekly_walking_milestone,
                goal_decomposer(user),command).replayed
    goals=call(dsn,list_goals,actor(user),instance)
    assert len(goals)==2
    root_goal,child=goals
    assert root_goal.goal_id==parent and root_goal.version==2
    assert root_goal.desired_outcome=="Walk three times per week"
    assert root_goal.creation_authority=="USER_APPROVED"
    assert child.goal_id==command.goal_id
    assert child.parent_goal_id==parent and child.depth==1
    assert child.creation_authority=="DELEGATED_MILESTONE"
    assert child.desired_outcome=="Walk 1 time per week"
    assert child.success_criterion=="Complete at least 1 walk per week"
    with psycopg.connect(dsn,autocommit=True) as conn:
        event=conn.execute(
            """SELECT e.trust_level,e.payload,g.source_turn_id,
                      g.created_event_id
               FROM goal_records g JOIN events e
                 ON e.event_id=g.created_event_id
               WHERE g.goal_id=%s""",(command.goal_id,),
        ).fetchone()
        assert event[0]=="TRUSTED_PLATFORM"
        assert event[1]["authority"]=="DELEGATED_PARENT"
        assert event[1]["parent_created_event_id"]
        assert event[1]["child_weekly_walks"]==1
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id=%s",
            (event[3],),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE parent_goal_id=%s",
            (parent,),
        ).fetchone()[0]==1


def test_delegation_denies_unproven_contribution_and_duplicate_child(dsn):
    user,instance,parent=approved_walking_root(dsn)
    candidate=derived_walking_cmd(instance,parent,count=3)
    with pytest.raises(PermissionDenied,match="noncontributing"):
        call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),candidate)
    with pytest.raises(PermissionDenied):
        call(dsn,derive_weekly_walking_milestone,actor(user),
             derived_walking_cmd(instance,parent))
    wrong_source=TrustedPlatformActor(
        user_id=user,source_id="some.other.worker",
    )
    with pytest.raises(PermissionDenied):
        call(dsn,derive_weekly_walking_milestone,wrong_source,
             derived_walking_cmd(instance,parent))
    first=derived_walking_cmd(instance,parent,count=1)
    call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),first)
    duplicate=derived_walking_cmd(instance,parent,count=1,version=2)
    with pytest.raises(VersionConflict,match="equivalent"):
        call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),duplicate)
    with pytest.raises(VersionConflict,match="version"):
        call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),
             derived_walking_cmd(instance,parent,count=2,version=1))
    with pytest.raises(IdempotencyConflict):
        call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),
             first.model_copy(update={"weekly_walks":2}))
    assert len(call(dsn,list_goals,actor(user),instance))==2


def test_delegation_rejects_ambiguous_parent_and_stale_selection(dsn):
    user,instance,parent=approved_walking_root(
        dsn,criterion="Complete two walks per week",
    )
    with pytest.raises(PermissionDenied,match="unproven"):
        call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),
             derived_walking_cmd(instance,parent))
    assert len(call(dsn,list_goals,actor(user),instance))==1

    another_user,another_instance,another_parent=approved_walking_root(dsn)
    with pytest.raises(PermissionDenied):
        call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),
             derived_walking_cmd(another_instance,another_parent))
    assert len(call(dsn,list_goals,actor(another_user),another_instance))==1


def test_weekly_milestone_parser_abstains_on_unsupported_goal_forms():
    assert weekly_walk_target("Walk three times per week",
                              "Complete three walks per week")==3
    assert weekly_walk_target("Walk 1 time per week",
                              "Complete at least 1 walk per week")==1
    assert weekly_walk_target("Walk three times per week",
                              "Complete two walks per week") is None
    assert weekly_walk_target("Exercise more", "Get healthier") is None
    assert weekly_walk_target("Walk three times per week and run daily",
                              "Complete three walks per week") is None


def change_walking_root_status(dsn, user, instance, root_id, *, status, version):
    """Exercise the existing authenticated user status-command contract."""
    with psycopg.connect(dsn,autocommit=True) as conn:
        conversation=conn.execute(
            "SELECT source_conversation_id FROM goal_records WHERE goal_id=%s",
            (root_id,),
        ).fetchone()[0]
    phrase=("Pause" if status=="PAUSED" else "Abandon")+" my walking goal."
    source=turn(dsn,user,instance,conversation,phrase)
    proposed=SetGoalStatusCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,evidence_quote=phrase,
        goal_id=root_id,expected_goal_version=version,new_status=status,
    )
    return call(dsn,set_goal_status,actor(user),
                authorize(dsn,user,instance,proposed))


@pytest.mark.parametrize("root_status",["PAUSED","ABANDONED"])
def test_delegated_grandchild_denied_when_root_authority_withdrawn(
    dsn, root_status,
):
    user,instance,root_id=approved_walking_root(dsn)
    first=derived_walking_cmd(instance,root_id,count=2,version=1)
    call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),first)
    assert change_walking_root_status(
        dsn,user,instance,root_id,status=root_status,version=2,
    ).action=="STATUS_CHANGED"
    # The intermediate milestone is still ACTIVE: no cascading status edit.
    views=call(dsn,list_goals,actor(user),instance)
    assert next(x for x in views if x.goal_id==first.goal_id).status=="ACTIVE"
    grandchild=derived_walking_cmd(instance,first.goal_id,count=1,version=1)
    with pytest.raises(VersionConflict,match="ancestor"):
        call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),
             grandchild)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE parent_goal_id=%s",
            (first.goal_id,),
        ).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM events WHERE user_id=%s AND instance_id=%s
               AND event_type='goal.created.v1'""",(user,instance),
        ).fetchone()[0]==2


def test_ancestor_status_is_locked_through_delegated_grandchild_commit(
    dsn, monkeypatch,
):
    """A concurrent status writer cannot revoke root authority mid-commit."""
    from threading import Event
    import persona_agent.goal_state as goal_module

    user,instance,root_id=approved_walking_root(dsn)
    first=derived_walking_cmd(instance,root_id,count=2,version=1)
    call(dsn,derive_weekly_walking_milestone,goal_decomposer(user),first)
    grandchild=derived_walking_cmd(instance,first.goal_id,count=1,version=1)
    entered,proceed=Event(),Event()
    original_publish=goal_module._publish

    def paused_publish(cur, actor_value, instance_value, kind, detail, receipt):
        if (type(actor_value) is TrustedPlatformActor
            and kind=="goal.created.v1"
            and detail.get("goal_id")==str(grandchild.goal_id)):
            entered.set()
            if not proceed.wait(10):
                raise TimeoutError("test did not release locked transaction")
        return original_publish(cur,actor_value,instance_value,kind,detail,receipt)

    monkeypatch.setattr(goal_module,"_publish",paused_publish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(call,dsn,derive_weekly_walking_milestone,
                           goal_decomposer(user),grandchild)
        try:
            assert entered.wait(10), "delegation did not reach locked commit"
            with psycopg.connect(dsn,autocommit=True) as conn:
                # Root is a strict ancestor, NOT the immediate parent.
                # A pause uses FOR UPDATE on this row and must be blocked.
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    with conn.transaction():
                        conn.execute(
                            """SELECT goal_id FROM goal_records
                               WHERE goal_id=%s FOR UPDATE NOWAIT""",
                            (root_id,),
                        ).fetchone()
        finally:
            proceed.set()
        assert future.result(timeout=15).action=="GOAL_CREATED"
    assert change_walking_root_status(
        dsn,user,instance,root_id,status="PAUSED",version=2,
    ).action=="STATUS_CHANGED"
    assert len(call(dsn,list_goals,actor(user),instance))==3


# MVP-03A.2: arbitrary bounded candidate path, proof-or-review admission.
from persona_agent.goal_decomposition import (propose_derived_subgoal,
    list_pending_subgoal_proposals)
from persona_agent.goal_decomposition_contracts import (
    ProposeDerivedSubgoalCommand,proven_count_milestone,
)


def approved_book_goal(dsn,*,count=3):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    statement=f"I want to read {count} books per month."
    source=turn(dsn,user,instance,conversation,statement)
    command=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,evidence_quote=statement,
        goal_id=uuid4(),title="Read regularly",
        desired_outcome=f"Read {count} books per month",
        success_criterion=f"Read at least {count} books per month",
        horizon="MEDIUM",
    )
    call(dsn,create_goal,actor(user),authorize(dsn,user,instance,command))
    return user,instance,conversation,command.goal_id


def generalized_command(dsn,instance,parent,*,count=1,
                        title="Reading milestone",outcome=None,criterion=None,
                        version=None):
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            "SELECT version,created_event_id FROM goal_records WHERE goal_id=%s",
            (parent,),
        ).fetchone()
    return ProposeDerivedSubgoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),
        proposal_id=uuid4(),proposed_goal_id=uuid4(),
        parent_goal_id=parent,
        expected_parent_version=version if version is not None else row[0],
        source_parent_event_id=row[1],title=title,
        desired_outcome=outcome or f"Read {count} book per month",
        success_criterion=criterion or f"Read at least {count} book per month",
        horizon="SHORT",
        contribution_note="Initial reading milestone beneath authorized monthly target",
    )


def goal_evaluator(user):
    return TrustedPlatformActor(user_id=user,source_id="goal.decomposition.v1")


def test_generalized_count_milestone_auto_accepts_without_child_confirmation(dsn):
    user,instance,conversation,root_id=approved_book_goal(dsn)
    cmd=generalized_command(dsn,instance,root_id,count=1)
    result=call(dsn,propose_derived_subgoal,goal_evaluator(user),cmd)
    assert result.status=="ACCEPTED"
    assert result.decision_method=="DETERMINISTIC_COUNT_MILESTONE"
    assert result.accepted_goal_event_id is not None
    replay=call(dsn,propose_derived_subgoal,goal_evaluator(user),cmd)
    assert replay.replayed and replay.event_id==result.event_id
    goals=call(dsn,list_goals,actor(user),instance)
    root_goal=next(x for x in goals if x.goal_id==root_id)
    child=next(x for x in goals if x.goal_id==cmd.proposed_goal_id)
    assert len(goals)==2
    assert root_goal.version==2
    assert root_goal.desired_outcome=="Read 3 books per month"
    assert child.parent_goal_id==root_id
    assert child.creation_authority=="DELEGATED_VERIFIED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        rows=conn.execute(
            """SELECT e.event_type,e.trust_level,e.payload
               FROM events e WHERE e.event_id IN (%s,%s)
               ORDER BY e.event_type""",
            (result.event_id,result.accepted_goal_event_id),
        ).fetchall()
        assert {x[0] for x in rows}=={
            "goal.created.v1","goal.subgoal.proposed.v1",
        }
        created=next(x for x in rows if x[0]=="goal.created.v1")
        assert created[1]=="TRUSTED_PLATFORM"
        assert created[2]["authority"]=="DELEGATED_PARENT"
        assert created[2]["proposal_id"]==str(cmd.proposal_id)
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id IN (%s,%s)",
            (result.event_id,result.accepted_goal_event_id),
        ).fetchone()[0]==2


def test_arbitrary_candidate_is_durable_but_non_actionable(dsn):
    user,instance,_,root_id=approved_book_goal(dsn)
    cmd=generalized_command(
        dsn,instance,root_id,title="Join a book club",
        outcome="Attend a monthly book club",
        criterion="Attend at least one club meeting",
    )
    result=call(dsn,propose_derived_subgoal,goal_evaluator(user),cmd)
    assert result.status=="PENDING_REVIEW"
    assert result.decision_method=="REVIEW_REQUIRED"
    assert result.accepted_goal_event_id is None
    assert len(call(dsn,list_goals,actor(user),instance))==1
    queue=call(dsn,list_pending_subgoal_proposals,actor(user),instance)
    assert len(queue)==1 and queue[0].proposal_id==cmd.proposal_id
    assert queue[0].status=="PENDING_REVIEW"
    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            """SELECT status,expected_parent_version,
                      source_parent_event_id,created_event_id
               FROM goal_subgoal_proposals WHERE proposal_id=%s""",
            (cmd.proposal_id,),
        ).fetchone()
        assert row[0]=="PENDING_REVIEW" and row[1]==1
        assert row[2]==cmd.source_parent_event_id
        assert row[3]==result.event_id
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE goal_id=%s",
            (cmd.proposed_goal_id,),
        ).fetchone()[0]==0
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),cmd).replayed
    with pytest.raises(VersionConflict,match="duplicate"):
        call(dsn,propose_derived_subgoal,goal_evaluator(user),
             cmd.model_copy(update={
                 "proposal_id":uuid4(),"proposed_goal_id":uuid4(),
                 "idempotency_key":str(uuid4()),
             }))
    assert len(call(dsn,list_goals,actor(user),instance))==1


def test_unverified_count_and_stale_source_are_never_auto_accepted(dsn):
    user,instance,_,root_id=approved_book_goal(dsn)
    mismatch=generalized_command(
        dsn,instance,root_id,count=1,
        criterion="Read at least 1 podcast per month",
    )
    res=call(dsn,propose_derived_subgoal,goal_evaluator(user),mismatch)
    assert res.status=="PENDING_REVIEW"
    assert len(call(dsn,list_goals,actor(user),instance))==1
    with pytest.raises(PermissionDenied,match="source event"):
        call(dsn,propose_derived_subgoal,goal_evaluator(user),
             generalized_command(dsn,instance,root_id).model_copy(update={
                 "source_parent_event_id":uuid4(),
             }))
    with pytest.raises(PermissionDenied,match="trusted Goal evaluator"):
        call(dsn,propose_derived_subgoal,actor(user),
             generalized_command(dsn,instance,root_id))
    with pytest.raises(IdempotencyConflict):
        call(dsn,propose_derived_subgoal,goal_evaluator(user),
             mismatch.model_copy(update={"title":"Changed silently"}))


def test_generalized_grandchild_fenced_after_root_paused(dsn):
    user,instance,_,root_id=approved_book_goal(dsn,count=5)
    first=generalized_command(dsn,instance,root_id,count=3)
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),
                first).status=="ACCEPTED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        conversation=conn.execute(
            "SELECT source_conversation_id FROM goal_records WHERE goal_id=%s",
            (root_id,),
        ).fetchone()[0]
    source=turn(dsn,user,instance,conversation,"Pause my reading goal.")
    changed=SetGoalStatusCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="Pause my reading goal.",
        goal_id=root_id,expected_goal_version=2,new_status="PAUSED",
    )
    call(dsn,set_goal_status,actor(user),
         authorize(dsn,user,instance,changed))
    descendant=generalized_command(dsn,instance,first.proposed_goal_id,count=1)
    with pytest.raises(VersionConflict,match="ancestor"):
        call(dsn,propose_derived_subgoal,goal_evaluator(user),descendant)
    assert len(call(dsn,list_goals,actor(user),instance))==2


def test_generalized_decomposition_is_bounded_and_cas_fenced(dsn):
    user,instance,_,root_id=approved_book_goal(dsn,count=5)
    cmd=generalized_command(dsn,instance,root_id,count=3)
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),
                cmd).status=="ACCEPTED"
    with pytest.raises(VersionConflict,match="parent version"):
        call(dsn,propose_derived_subgoal,goal_evaluator(user),
             generalized_command(dsn,instance,root_id,count=1,version=1))
    # A supporting milestone may itself be decomposed without new approval.
    grand=generalized_command(dsn,instance,cmd.proposed_goal_id,count=1)
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),
                grand).status=="ACCEPTED"
    assert len(call(dsn,list_goals,actor(user),instance))==3


def test_count_proof_rejects_mismatched_period_unit_and_result_claims():
    assert proven_count_milestone(
        "Read 3 books per month","Read at least 3 books per month",
        "Read 1 book per month","Read at least 1 book per month",
    )
    assert not proven_count_milestone(
        "Read 3 books per month","Read at least 3 books per month",
        "Read 1 book per week","Read at least 1 book per week",
    )
    assert not proven_count_milestone(
        "Read 3 books per month","Read at least 3 books per month",
        "Read 3 books per month","Read at least 3 books per month",
    )
    assert not proven_count_milestone(
        "Read 3 books per month","Read at least 3 books per month",
        "Read 1 book per month and sell the rest",
        "Read at least 1 book per month",
    )


@pytest.mark.parametrize("parent_outcome,parent_criterion,child_outcome,child_criterion",[
    (
        "Complete 4 hour marathons per month",
        "Complete at least 4 hour marathons per month",
        "Complete 1 hour marathons per month",
        "Complete at least 1 hour marathons per month",
    ),
    (
        "Complete 4 hour-long marathons per month",
        "Complete at least 4 hour-long marathons per month",
        "Complete 1 hour-long marathons per month",
        "Complete at least 1 hour-long marathons per month",
    ),
    (
        "Complete 4 km runs per week",
        "Complete at least 4 km runs per week",
        "Complete 1 km runs per week",
        "Complete at least 1 km runs per week",
    ),
])
def test_ambiguous_numeric_modifier_is_not_quantitative_proof(
    parent_outcome,parent_criterion,child_outcome,child_criterion,
):
    assert not proven_count_milestone(
        parent_outcome,parent_criterion,child_outcome,child_criterion,
    )


def test_ambiguous_marathon_duration_stays_pending_review(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,
                "I want to complete four-hour marathons.")
    parent=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="I want to complete four-hour marathons",
        goal_id=uuid4(),title="Marathon training",
        desired_outcome="Complete 4 hour marathons per month",
        success_criterion="Complete 4 hour marathons per month",
        horizon="MEDIUM",
    )
    call(dsn,create_goal,actor(user),authorize(dsn,user,instance,parent))
    proposed=generalized_command(
        dsn,instance,parent.goal_id,title="Lower marathon duration",
        outcome="Complete 1 hour marathons per month",
        criterion="Complete 1 hour marathons per month",
    )
    result=call(dsn,propose_derived_subgoal,goal_evaluator(user),proposed)
    assert result.status=="PENDING_REVIEW"
    assert result.accepted_goal_event_id is None
    assert len(call(dsn,list_goals,actor(user),instance))==1
    assert call(dsn,list_goals,actor(user),instance)[0].version==1
    pending=call(dsn,list_pending_subgoal_proposals,actor(user),instance)
    assert len(pending)==1 and pending[0].proposal_id==proposed.proposal_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE parent_goal_id=%s",
            (parent.goal_id,),
        ).fetchone()[0]==0


@pytest.mark.parametrize("outcome,criterion,title,horizon",[
    (
        "Read one book per month",
        "Read at least one book per month",
        "Different title", "SHORT",
    ),
    (
        "Read at least 1 book per month",
        "Read 1 books per month",
        "Reading progression", "MEDIUM",
    ),
    (
        "  Read   1  books  per   month  ",
        "  Read  at least   one  book per month ",
        "Whitespace variation", "OPEN",
    ),
])
def test_normalized_accepted_milestones_cannot_duplicate(
    dsn,outcome,criterion,title,horizon,
):
    user,instance,_,root_id=approved_book_goal(dsn)
    first=generalized_command(dsn,instance,root_id)
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),
                first).status=="ACCEPTED"
    variant=generalized_command(
        dsn,instance,root_id,title=title,
        outcome=outcome,criterion=criterion,
    ).model_copy(update={"horizon":horizon})
    with pytest.raises(VersionConflict,match="equivalent|duplicate"):
        call(dsn,propose_derived_subgoal,goal_evaluator(user),variant)
    goals=call(dsn,list_goals,actor(user),instance)
    assert len(goals)==2
    assert next(x for x in goals if x.goal_id==root_id).version==2
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM goal_subgoal_proposals
               WHERE parent_goal_id=%s AND status='ACCEPTED'""",
            (root_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE user_id=%s AND instance_id=%s
                 AND event_type='goal.created.v1'""",
            (user,instance),
        ).fetchone()[0]==2


def test_qualified_countable_unit_still_proves_lower_session_count():
    assert proven_count_milestone(
        "Complete 4 exercise sessions per week",
        "Complete at least 4 exercise sessions per week",
        "Complete 1 exercise session per week",
        "Complete at least one exercise session per week",
    )


# MVP-03A.3: model findings are evidence; the Goal owner performs adoption.
import json

from persona_agent.conversation_contracts import ModelReply
from persona_agent.goal_semantic_review import review_pending_subgoal
from persona_agent.goal_semantic_review_contracts import ReviewSubgoalCommand


def semantic_reviewer(user):
    return TrustedPlatformActor(user_id=user,source_id="goal.semantic_review.v1")


def semantic_command(instance,proposal_id):
    return ReviewSubgoalCommand(
        instance_id=instance,expected_generation=1,
        proposal_id=proposal_id,review_id=uuid4(),idempotency_key=str(uuid4()),
    )


def semantic_finding(*,alignment="ALIGNED",parent_quote="Read 3 books per month",
                     child_quote="Read 15 pages per day",**overrides):
    fields={
        "alignment":alignment,
        "parent_evidence_quote":parent_quote,
        "child_evidence_quote":child_quote,
        "contribution_reason":"Daily reading practice supports the approved monthly book target.",
        "within_parent_scope":True,
        "measurable":True,
        "supports_not_replaces_parent":True,
        "changes_parent_objective":False,
        "requires_new_permissions":False,
        "creates_new_user_commitment":False,
    }
    fields.update(overrides)
    return fields


class FakeSemanticGateway:
    def __init__(self,fields,*,callback=None,raw=None):
        self.fields=fields
        self.callback=callback
        self.raw=raw
        self.calls=0

    def generate(self,messages):
        self.calls+=1
        assert len(messages)==2
        assert messages[0].role=="system"
        assert messages[1].role=="user"
        if self.callback:
            self.callback()
        return ModelReply(
            model_id="fake-goal-reviewer",
            text=self.raw if self.raw is not None else json.dumps(self.fields),
        )


def pending_reading_habit(dsn,*,outcome="Read 15 pages per day",
                          criterion="Read 15 pages per day",
                          title="Practice reading every day"):
    user,instance,_,root=approved_book_goal(dsn)
    proposed=generalized_command(
        dsn,instance,root,title=title,
        outcome=outcome,criterion=criterion,
    )
    first=call(dsn,propose_derived_subgoal,goal_evaluator(user),proposed)
    assert first.status=="PENDING_REVIEW"
    return user,instance,root,proposed


def test_semantic_subgoal_review_accepts_aligned_measurable_child(dsn):
    user,instance,root,proposal=pending_reading_habit(dsn)
    gateway=FakeSemanticGateway(semantic_finding())
    command=semantic_command(instance,proposal.proposal_id)
    result=call(dsn,review_pending_subgoal,
                semantic_reviewer(user),command,gateway)
    assert result.alignment=="ALIGNED" and result.decision=="ACCEPTED"
    assert result.accepted_goal_event_id is not None
    assert gateway.calls==1
    replay=call(dsn,review_pending_subgoal,
                semantic_reviewer(user),command,gateway)
    assert replay.replayed and replay.event_id==result.event_id
    assert gateway.calls==1  # no duplicate paid inference on replay
    goals=call(dsn,list_goals,actor(user),instance)
    assert len(goals)==2
    parent=next(x for x in goals if x.goal_id==root)
    child=next(x for x in goals if x.goal_id==proposal.proposed_goal_id)
    assert parent.version==2
    assert parent.desired_outcome=="Read 3 books per month"
    assert child.creation_authority=="DELEGATED_REVIEWED"
    assert child.desired_outcome=="Read 15 pages per day"
    assert child.title=="Read 15 pages per day"  # no untrusted candidate title
    assert child.parent_goal_id==root
    assert call(dsn,list_pending_subgoal_proposals,
                actor(user),instance)==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        ledger=conn.execute(
            """SELECT model_id,finding,owner_decision,
                      parent_evidence_quote,child_evidence_quote,
                      accepted_goal_event_id
               FROM goal_subgoal_semantic_reviews WHERE review_id=%s""",
            (command.review_id,),
        ).fetchone()
        assert ledger[:3]==("fake-goal-reviewer","ALIGNED","ACCEPTED")
        assert ledger[3]=="Read 3 books per month"
        assert ledger[4]=="Read 15 pages per day"
        assert ledger[5]==result.accepted_goal_event_id
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id IN (%s,%s)",
            (result.event_id,result.accepted_goal_event_id),
        ).fetchone()[0]==2
    with pytest.raises(VersionConflict,match="already reviewed"):
        call(dsn,review_pending_subgoal,semantic_reviewer(user),
             semantic_command(instance,proposal.proposal_id),gateway)
    assert gateway.calls==1


@pytest.mark.parametrize("alignment,decision",[
    ("UNCERTAIN","DEFERRED"),
    ("DRIFT_DETECTED","REJECTED"),
    ("BLOCKED","REJECTED"),
])
def test_semantic_unreliable_finding_never_admits_goal(dsn,alignment,decision):
    user,instance,root,proposal=pending_reading_habit(dsn)
    gateway=FakeSemanticGateway(semantic_finding(alignment=alignment))
    result=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                semantic_command(instance,proposal.proposal_id),gateway)
    assert result.decision==decision
    assert result.accepted_goal_event_id is None
    assert len(call(dsn,list_goals,actor(user),instance))==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM goal_subgoal_proposals WHERE proposal_id=%s",
            (proposal.proposal_id,),
        ).fetchone()[0]==decision
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE parent_goal_id=%s",
            (root,),
        ).fetchone()[0]==0


@pytest.mark.parametrize("bad",[
    {"requires_new_permissions":True},
    {"creates_new_user_commitment":True},
    {"within_parent_scope":False},
    {"changes_parent_objective":True},
    {"measurable":False},
])
def test_aligned_model_claim_cannot_override_owner_safety_gate(dsn,bad):
    user,instance,_,proposal=pending_reading_habit(dsn)
    finding=semantic_finding(**bad)
    result=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                semantic_command(instance,proposal.proposal_id),
                FakeSemanticGateway(finding))
    assert result.alignment=="ALIGNED"
    assert result.decision=="DEFERRED"
    assert result.accepted_goal_event_id is None
    assert len(call(dsn,list_goals,actor(user),instance))==1


def test_semantic_review_does_not_take_model_evidence_on_faith(dsn):
    user,instance,root,proposal=pending_reading_habit(dsn)
    command=semantic_command(instance,proposal.proposal_id)
    with pytest.raises(ValueError,match="invented parent evidence"):
        call(dsn,review_pending_subgoal,semantic_reviewer(user),command,
             FakeSemanticGateway(semantic_finding(
                 parent_quote="User authorized buying premium books",
             )))
    with pytest.raises(ValueError,match="schema"):
        call(dsn,review_pending_subgoal,semantic_reviewer(user),command,
             FakeSemanticGateway({},raw='{"alignment":"ALIGNED"}'))
    assert len(call(dsn,list_goals,actor(user),instance))==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM goal_subgoal_proposals WHERE proposal_id=%s",
            (proposal.proposal_id,),
        ).fetchone()[0]=="PENDING_REVIEW"
        assert conn.execute(
            "SELECT count(*) FROM goal_subgoal_semantic_reviews WHERE proposal_id=%s",
            (proposal.proposal_id,),
        ).fetchone()[0]==0


def test_semantic_review_superseded_if_parent_version_changes_during_inference(dsn):
    user,instance,root,proposal=pending_reading_habit(dsn)
    def concurrent_parent_advance():
        other=generalized_command(dsn,instance,root,count=1)
        assert call(dsn,propose_derived_subgoal,goal_evaluator(user),
                    other).status=="ACCEPTED"

    gateway=FakeSemanticGateway(
        semantic_finding(),callback=concurrent_parent_advance,
    )
    result=call(dsn,review_pending_subgoal,
                semantic_reviewer(user),semantic_command(instance,proposal.proposal_id),
                gateway)
    assert result.decision=="SUPERSEDED"
    goals=call(dsn,list_goals,actor(user),instance)
    assert len(goals)==2  # only the independent, concurrently accepted child
    assert all(g.goal_id!=proposal.proposed_goal_id for g in goals)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM goal_subgoal_proposals WHERE proposal_id=%s",
            (proposal.proposal_id,),
        ).fetchone()[0]=="SUPERSEDED"


def test_semantic_review_denied_when_root_paused_during_inference(dsn):
    user,instance,root,proposal=pending_reading_habit(dsn)
    def concurrent_pause():
        with psycopg.connect(dsn,autocommit=True) as conn:
            conversation=conn.execute(
                "SELECT source_conversation_id FROM goal_records WHERE goal_id=%s",
                (root,),
            ).fetchone()[0]
        source=turn(dsn,user,instance,conversation,"Pause my reading goal.")
        cmd=SetGoalStatusCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=str(uuid4()),conversation_id=conversation,
            evidence_turn_id=source.turn_id,
            evidence_quote="Pause my reading goal.",
            goal_id=root,expected_goal_version=1,new_status="PAUSED",
        )
        call(dsn,set_goal_status,actor(user),
             authorize(dsn,user,instance,cmd))
    gateway=FakeSemanticGateway(semantic_finding(),callback=concurrent_pause)
    with pytest.raises(VersionConflict,match="ancestor"):
        call(dsn,review_pending_subgoal,semantic_reviewer(user),
             semantic_command(instance,proposal.proposal_id),gateway)
    assert len(call(dsn,list_goals,actor(user),instance))==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT status FROM goal_subgoal_proposals WHERE proposal_id=%s",
            (proposal.proposal_id,),
        ).fetchone()[0]=="PENDING_REVIEW"


def test_reviewed_goal_retains_valid_provenance_for_further_decomposition(dsn):
    user,instance,root,proposal=pending_reading_habit(dsn)
    gateway=FakeSemanticGateway(semantic_finding())
    result=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                semantic_command(instance,proposal.proposal_id),gateway)
    assert result.decision=="ACCEPTED"
    # Second generation is mechanically checked, not treated as new user authority.
    child=generalized_command(
        dsn,instance,proposal.proposed_goal_id,
        outcome="Read 3 pages per day",criterion="Read 3 pages per day",
    )
    accepted=call(dsn,propose_derived_subgoal,goal_evaluator(user),child)
    assert accepted.status=="ACCEPTED"
    goals=call(dsn,list_goals,actor(user),instance)
    assert len(goals)==3
    assert next(x for x in goals if x.goal_id==root).desired_outcome=="Read 3 books per month"


def test_review_ignores_model_approval_of_new_external_commitment(dsn):
    user,instance,root,proposal=pending_reading_habit(
        dsn,outcome="Join one paid book club per week",
        criterion="Join one paid book club per week",
        title="Join an external club",
    )
    model=FakeSemanticGateway(semantic_finding(
        child_quote="Join one paid book club per week",
    ))
    result=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                semantic_command(instance,proposal.proposal_id),model)
    assert result.decision=="DEFERRED"
    assert len(call(dsn,list_goals,actor(user),instance))==1


def test_semantic_owner_rejects_raised_target_even_if_model_is_aligned(dsn):
    user,instance,_,proposal=pending_reading_habit(
        dsn,outcome="Read 8 books per month",
        criterion="Read 8 books per month",
        title="Read more books",
    )
    gateway=FakeSemanticGateway(semantic_finding(
        parent_quote="Read 3 books per month",
        child_quote="Read 8 books per month",
    ))
    result=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                semantic_command(instance,proposal.proposal_id),gateway)
    assert result.decision=="DEFERRED"
    assert len(call(dsn,list_goals,actor(user),instance))==1


def test_semantic_owner_defers_numeric_property_modifier(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,"I want to finish marathons.")
    root=CreateGoalCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conversation,
        evidence_turn_id=source.turn_id,
        evidence_quote="I want to finish marathons",
        goal_id=uuid4(),title="Marathon goal",
        desired_outcome="Complete 4 hour marathons per month",
        success_criterion="Complete 4 hour marathons per month",
        horizon="MEDIUM",
    )
    call(dsn,create_goal,actor(user),authorize(dsn,user,instance,root))
    proposal=generalized_command(
        dsn,instance,root.goal_id,
        title="Fast marathon",outcome="Complete 1 hour marathons per month",
        criterion="Complete 1 hour marathons per month",
    )
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),
                proposal).status=="PENDING_REVIEW"
    model=FakeSemanticGateway(semantic_finding(
        parent_quote="Complete 4 hour marathons per month",
        child_quote="Complete 1 hour marathons per month",
    ))
    review=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                semantic_command(instance,proposal.proposal_id),model)
    assert review.decision=="DEFERRED"
    assert len(call(dsn,list_goals,actor(user),instance))==1


def test_semantic_inference_not_inside_database_transaction(dsn):
    user,instance,_,proposal=pending_reading_habit(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        def assert_idle():
            assert conn.info.transaction_status==psycopg.pq.TransactionStatus.IDLE
        gateway=FakeSemanticGateway(semantic_finding(),callback=assert_idle)
        result=review_pending_subgoal(
            conn,semantic_reviewer(user),semantic_command(
                instance,proposal.proposal_id,
            ),gateway,
        )
    assert result.decision=="ACCEPTED"


def test_reviewed_child_with_embedded_purchase_is_not_auto_adopted(dsn):
    user,instance,_,proposal=pending_reading_habit(
        dsn,outcome="Read 15 pages per day and buy premium books",
        criterion="Read 15 pages per day",
        title="Daily reading habit",
    )
    reply=FakeSemanticGateway(semantic_finding(
        child_quote="Read 15 pages per day",
    ))
    result=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                semantic_command(instance,proposal.proposal_id),reply)
    assert result.decision=="DEFERRED"
    assert len(call(dsn,list_goals,actor(user),instance))==1


def test_preexisting_stale_proposal_retires_without_model_review(dsn):
    """Two proposals at v1; accepting one supersedes the unreviewed sibling."""
    user,instance,root,first=pending_reading_habit(dsn)
    stale=generalized_command(
        dsn,instance,root,title="Read a smaller daily amount",
        outcome="Read 5 pages per day",
        criterion="Read 5 pages per day",
    )
    assert call(dsn,propose_derived_subgoal,goal_evaluator(user),
                stale).status=="PENDING_REVIEW"
    assert len(call(dsn,list_pending_subgoal_proposals,
                    actor(user),instance))==2

    adopted=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                 semantic_command(instance,first.proposal_id),
                 FakeSemanticGateway(semantic_finding()))
    assert adopted.decision=="ACCEPTED"
    assert next(x for x in call(dsn,list_goals,actor(user),instance)
                if x.goal_id==root).version==2

    no_inference=FakeSemanticGateway({},raw="INVALID MODEL OUTPUT")
    review=semantic_command(instance,stale.proposal_id)
    retired=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                 review,no_inference)
    assert retired.decision=="SUPERSEDED"
    assert retired.alignment is None  # no fabricated semantic finding
    assert retired.accepted_goal_event_id is None
    assert no_inference.calls==0
    replay=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                review,no_inference)
    assert replay.replayed and replay.event_id==retired.event_id
    assert no_inference.calls==0
    assert call(dsn,list_pending_subgoal_proposals,
                actor(user),instance)==()

    with psycopg.connect(dsn,autocommit=True) as conn:
        row=conn.execute(
            """SELECT p.status,e.event_type,e.trust_level,e.payload
               FROM goal_subgoal_proposals p
               JOIN events e ON e.event_id=%s
               WHERE p.proposal_id=%s""",
            (retired.event_id,stale.proposal_id),
        ).fetchone()
        assert row[0]=="SUPERSEDED"
        assert row[1]=="goal.subgoal.superseded.v1"
        assert row[2]=="TRUSTED_PLATFORM"
        assert row[3]["reason"]=="PARENT_VERSION_CHANGED"
        assert row[3]["model_invoked"] is False
        assert row[3]["expected_parent_version"]==1
        assert row[3]["observed_parent_version"]==2
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id=%s",
            (retired.event_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM goal_subgoal_semantic_reviews WHERE proposal_id=%s",
            (stale.proposal_id,),
        ).fetchone()[0]==0

    # Retired proposals no longer contribute to the pending-review budget:
    # a new candidate at the updated parent revision can be staged.
    fresh=generalized_command(
        dsn,instance,root,title="Another supporting reading habit",
        outcome="Read 10 pages per day",
        criterion="Read 10 pages per day",
    )
    result=call(dsn,propose_derived_subgoal,goal_evaluator(user),fresh)
    assert result.status=="PENDING_REVIEW"
    pending=call(dsn,list_pending_subgoal_proposals,actor(user),instance)
    assert len(pending)==1 and pending[0].proposal_id==fresh.proposal_id


def test_pre_review_retirement_after_authenticated_parent_pause(dsn):
    user,instance,root,proposal=pending_reading_habit(dsn)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conv=conn.execute(
            "SELECT source_conversation_id FROM goal_records WHERE goal_id=%s",
            (root,),
        ).fetchone()[0]
    source=turn(dsn,user,instance,conv,"Pause my reading goal.")
    status=SetGoalStatusCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=str(uuid4()),conversation_id=conv,
        evidence_turn_id=source.turn_id,
        evidence_quote="Pause my reading goal.",
        goal_id=root,expected_goal_version=1,new_status="PAUSED",
    )
    call(dsn,set_goal_status,actor(user),
         authorize(dsn,user,instance,status))
    gateway=FakeSemanticGateway(semantic_finding())
    retired=call(dsn,review_pending_subgoal,semantic_reviewer(user),
                 semantic_command(instance,proposal.proposal_id),gateway)
    assert retired.decision=="SUPERSEDED" and retired.alignment is None
    assert gateway.calls==0
    assert len(call(dsn,list_goals,actor(user),instance))==1
    assert call(dsn,list_pending_subgoal_proposals,
                actor(user),instance)==()
