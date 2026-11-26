"""MVP-06A: real PostgreSQL, bounded goal proposal and exact delivered approval."""
from uuid import uuid4

import pytest

from persona_agent.goal_conversation_contracts import parse_goal_dialogue,APPROVE,REJECT,ROUTE_ID

WALK="Set a goal to walk three times a week."
CHANGE="Change this goal to walk twice a week."
BROAD="My goal is to eat fewer sweets."
CRITERION="For this goal, success means at most two sweets per week."


@pytest.mark.parametrize("text,kind",[
    (WALK,"PROPOSE"),(CHANGE,"REFINE"),(BROAD,"PROPOSE"),(CRITERION,"CRITERION"),
    (APPROVE,"APPROVE"),(REJECT,"REJECT"),
    ("I want to walk three times a week.",None),
    ("I might set a goal to walk three times a week.",None),
    ("What if I set a goal to walk three times a week?",None),
    ("Yes.",None),("Approve this goal?",None),
    ('"Approve this goal."',None),
    ("Set a goal to walk and eat fewer sweets.",None),
    ("Set a goal to walk three times a week. Do it now.",None),
    ("My goal is to walk more, but maybe not.",None),
])
def test_bounded_goal_dialogue(text,kind):
    parsed=parse_goal_dialogue(text)
    assert (parsed.kind if parsed else None)==kind


def test_weekly_goal_and_explicit_criterion():
    parsed=parse_goal_dialogue(WALK)
    assert parsed.outcome=="Walk 3 times per week"
    assert parsed.criterion=="Complete at least 3 walks per week"
    assert parse_goal_dialogue(BROAD).criterion is None
    explicit=parse_goal_dialogue("Set a goal to eat fewer sweets; success means at most two sweets per week.")
    assert explicit.criterion=="at most two sweets per week"


def helpers():
    pytest.importorskip("psycopg")
    from test_phase3_goals import actor,call,seed,turn
    return actor,call,seed,turn


class NoModel:
    def generate(self,messages):
        raise AssertionError("bounded goal dialogue should not invoke inference")


class MisleadingModel:
    def __init__(self):
        self.calls=0
    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        self.calls+=1
        return ModelReply("untrusted-test","Your goal is already saved and all tasks are complete.")


def answer(dsn,user,instance,conv,text,*,deliver=True,generation=1,model=None):
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation import acknowledge_text_delivery
    actor,call,_,turn=helpers()
    request=turn(dsn,user,instance,conv,text,generation=generation)
    reply=call(dsn,respond_to_user_text,actor(user),request.turn_id,model or NoModel())
    if deliver:
        call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    return request,reply


def candidates(dsn,user,instance):
    from persona_agent.goal_conversation import list_goal_candidates
    actor,call,_,_=helpers()
    return call(dsn,list_goal_candidates,actor(user),instance)


def goals(dsn,user,instance):
    from persona_agent.goal_state import list_goals
    actor,call,_,_=helpers()
    return call(dsn,list_goals,actor(user),instance)


def test_refinement_and_separate_approval_atomic_and_idempotent(dsn):
    import psycopg
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.goal_delegation_contracts import weekly_walk_target
    actor,call,seed,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    source,proposal=answer(dsn,user,instance,conv,WALK)
    assert proposal.model_id==ROUTE_ID and APPROVE in proposal.text
    first,=candidates(dsn,user,instance)
    assert first.status=="PENDING" and first.approval_available
    assert first.source_turn_id==source.turn_id and first.proposal_response_id==proposal.response_id
    assert goals(dsn,user,instance)==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM operations WHERE user_id=%s AND instance_id=%s",
                            (user,instance)).fetchone()==(0,)
    revised,second=answer(dsn,user,instance,conv,CHANGE)
    current,old=candidates(dsn,user,instance)
    assert current.goal_id==old.goal_id==first.goal_id
    assert (current.version,old.version,old.status)==(2,1,"SUPERSEDED")
    assert current.source_turn_id==revised.turn_id
    assert "Walk 2 times per week" in second.text and "Walk 3 times" not in second.text
    assert goals(dsn,user,instance)==()
    approval,activated=answer(dsn,user,instance,conv,APPROVE)
    assert activated.text.startswith("Goal approved: Walk 2 times per week.")
    root,=goals(dsn,user,instance)
    assert root.goal_id==first.goal_id and root.version==1
    assert root.creation_authority=="USER_APPROVED" and root.parent_goal_id is None
    assert weekly_walk_target(root.desired_outcome,root.success_criterion)==2
    latest,old=candidates(dsn,user,instance)
    assert latest.status=="APPROVED" and latest.decision_turn_id==approval.turn_id
    replay=call(dsn,respond_to_user_text,actor(user),approval.turn_id,NoModel())
    assert replay.replayed and replay.response_id==activated.response_id
    _,repeat=answer(dsn,user,instance,conv,APPROVE)
    assert "no current goal proposal" in repeat.text
    assert len(goals(dsn,user,instance))==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM events WHERE instance_id=%s AND event_type='goal.created.v1'",
            (instance,),
        ).fetchone()==(1,)
        assert conn.execute(
            """SELECT count(*) FROM command_receipts
               WHERE instance_id=%s AND command_type='goal.create.conversational.v1'""",
            (instance,),
        ).fetchone()==(1,)
        assert conn.execute(
            """SELECT count(*) FROM events e JOIN outbox o USING(event_id)
               WHERE e.instance_id=%s AND e.event_type='goal.conversation_candidate.approved.v1'""",
            (instance,),
        ).fetchone()==(1,)


def test_missing_criterion_and_rejection_preserve_history(dsn):
    _,_,seed,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    source,draft=answer(dsn,user,instance,conv,BROAD)
    assert "Nothing is active yet" in draft.text and APPROVE not in draft.text
    first,=candidates(dsn,user,instance)
    assert first.success_criterion is None and not first.approval_available
    criterion,reply=answer(dsn,user,instance,conv,CRITERION)
    latest,old=candidates(dsn,user,instance)
    assert latest.source_turn_id==source.turn_id and latest.proposal_turn_id==criterion.turn_id
    assert latest.success_criterion=="at most two sweets per week" and latest.approval_available
    assert APPROVE in reply.text and old.status=="SUPERSEDED"
    rejected,response=answer(dsn,user,instance,conv,REJECT)
    assert response.text=="I won't activate that goal."
    assert candidates(dsn,user,instance)[0].decision_turn_id==rejected.turn_id
    assert candidates(dsn,user,instance)[0].status=="REJECTED"
    answer(dsn,user,instance,conv,APPROVE)
    assert goals(dsn,user,instance)==()


def test_approval_without_success_criterion_is_not_activation(dsn):
    _,_,seed,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    answer(dsn,user,instance,conv,BROAD)
    _,reply=answer(dsn,user,instance,conv,APPROVE)
    assert "success criterion" in reply.text
    assert goals(dsn,user,instance)==()
    assert candidates(dsn,user,instance)[0].status=="SUPERSEDED"


@pytest.mark.parametrize("intervening",["Yes.","Let's talk about gardening.","I want to walk more."])
def test_unrelated_or_ambiguous_reply_retires_approval(dsn,intervening):
    actor,call,seed,turn=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    answer(dsn,user,instance,conv,WALK)
    turn(dsn,user,instance,conv,intervening)
    assert candidates(dsn,user,instance)[0].status=="SUPERSEDED"
    _,reply=answer(dsn,user,instance,conv,APPROVE)
    assert "no current goal proposal" in reply.text
    assert goals(dsn,user,instance)==()


def test_undelivered_proposal_cannot_be_approved(dsn):
    _,_,seed,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    answer(dsn,user,instance,conv,WALK,deliver=False)
    assert not candidates(dsn,user,instance)[0].approval_available
    _,reply=answer(dsn,user,instance,conv,APPROVE)
    assert "no current goal proposal" in reply.text
    assert goals(dsn,user,instance)==()


def test_scope_and_persona_switch_invalidate_approval(dsn):
    from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
    from persona_agent.runtime import StaleSelection,PermissionDenied
    from persona_agent.goal_conversation import list_goal_candidates
    actor,call,seed,_=helpers()
    user,instance,other=seed(dsn,alternate=True)
    foreign,foreign_instance,_=seed(dsn)
    conv=uuid4()
    answer(dsn,user,instance,conv,WALK)
    _,reply=answer(dsn,user,instance,uuid4(),APPROVE)
    assert "no current goal proposal" in reply.text
    answer(dsn,foreign,foreign_instance,uuid4(),APPROVE)
    assert goals(dsn,foreign,foreign_instance)==()
    with pytest.raises(PermissionDenied):
        call(dsn,list_goal_candidates,actor(foreign),instance)
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleSelection):
        candidates(dsn,user,instance)
    answer(dsn,user,other,uuid4(),APPROVE,generation=2)
    assert goals(dsn,user,other)==()
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=other,target_instance_id=instance,
        expected_generation=2,idempotency_key=str(uuid4()),
    ))
    assert not candidates(dsn,user,instance)[0].approval_available
    answer(dsn,user,instance,conv,APPROVE,generation=3)
    assert goals(dsn,user,instance)==()


def test_model_output_cannot_approve_or_replace_proposal(dsn):
    from persona_agent.conversation import respond_to_text,acknowledge_text_delivery
    actor,call,seed,turn=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    gateway=MisleadingModel()
    request=turn(dsn,user,instance,conv,WALK)
    reply=call(dsn,respond_to_text,actor(user),request.turn_id,gateway)
    assert gateway.calls==1 and "Proposed goal:" in reply.text
    assert "already saved" not in reply.text and goals(dsn,user,instance)==()
    call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    _,result=answer(dsn,user,instance,conv,APPROVE)
    assert result.text.startswith("Goal approved:") and len(goals(dsn,user,instance))==1


def test_typed_owner_cannot_bypass_candidate_approval(dsn):
    from persona_agent.goal_contracts import CreateGoalCommand,goal_action_approval_phrase
    from persona_agent.goal_state import create_goal
    from persona_agent.runtime import PermissionDenied
    actor,call,seed,turn=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    source,_=answer(dsn,user,instance,conv,WALK)
    first,=candidates(dsn,user,instance)
    command=CreateGoalCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=conv,evidence_turn_id=source.turn_id,evidence_quote=WALK,
        goal_id=first.goal_id,title=first.desired_outcome,desired_outcome=first.desired_outcome,
        success_criterion=first.success_criterion,horizon="OPEN",
    )
    approval=turn(dsn,user,instance,conv,goal_action_approval_phrase(command))
    command=command.model_copy(update={"approval_turn_id":approval.turn_id})
    with pytest.raises(PermissionDenied):
        call(dsn,create_goal,actor(user),command)
    assert goals(dsn,user,instance)==()


def test_concurrent_duplicate_approval_creates_one_goal(dsn):
    from concurrent.futures import ThreadPoolExecutor
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation_contracts import StaleConversation
    actor,call,seed,turn=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    answer(dsn,user,instance,conv,WALK)
    request=turn(dsn,user,instance,conv,APPROVE)
    def run():
        try:
            return call(dsn,respond_to_user_text,actor(user),request.turn_id,NoModel())
        except StaleConversation:
            # Competing commit can precede the second pending-anchor read.
            return call(dsn,respond_to_user_text,actor(user),request.turn_id,NoModel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b=list(pool.map(lambda _:run(),range(2)))
    assert a.response_id==b.response_id and len(goals(dsn,user,instance))==1


def test_expired_proposal_cannot_activate(dsn):
    import psycopg
    _,_,seed,_=helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    source,_=answer(dsn,user,instance,conv,WALK)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE conversation_turns SET created_at=clock_timestamp()-interval '2 days' WHERE turn_id=%s",
                     (source.turn_id,))
    assert not candidates(dsn,user,instance)[0].approval_available
    _,reply=answer(dsn,user,instance,conv,APPROVE)
    assert "no current goal proposal" in reply.text and goals(dsn,user,instance)==()
