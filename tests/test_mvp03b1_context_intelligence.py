"""MVP-03B.1: real PostgreSQL tests of scoped, bounded contextual reads."""
from hashlib import sha256
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

import test_phase3_goals as goals
from persona_agent.context_intelligence import goal_reflection_context_block
from persona_agent.conversation import prepare_text_context
from persona_agent.goal_contracts import (
    RecordGoalEvidenceCommand,SetGoalStatusCommand,
)
from persona_agent.goal_state import record_goal_evidence,set_goal_status


def setup_goal(dsn):
    user,instance,_=goals.seed(dsn)
    conv=uuid4()
    source=goals.turn(dsn,user,instance,conv,
                      "I want to improve my eating habits.")
    goal_id,_,_=goals.root(dsn,user,instance,conv,source)
    return user,instance,conv,goal_id


def get_context(dsn,user,instance,conv,text,*,budget=12000):
    pending=goals.turn(dsn,user,instance,conv,text)
    context=goals.call(
        dsn,prepare_text_context,goals.actor(user),pending.turn_id,
        max_context_chars=budget,
    )
    return context.messages[0].content,context


def hypothesis(dsn,user,instance,conv,source,content,*,kind="HYPOTHESIS",
               review="PROPOSED",confidence=0.87,scope="CURRENT"):
    job_id,reflection_id=uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as connection:
        with connection.transaction():
            row=connection.execute(
                """SELECT COALESCE(MAX(admission_attempt),0)+1
                   FROM cognitive_jobs
                   WHERE conversation_id=%s AND start_sequence=%s
                     AND expected_generation=1""",
                (conv,source.sequence),
            ).fetchone()
            connection.execute(
                """INSERT INTO cognitive_jobs(
                     operation_id,conversation_id,user_id,instance_id,
                     expected_generation,admission_attempt,
                     start_sequence,end_sequence
                   ) VALUES (%s,%s,%s,%s,1,%s,%s,%s)""",
                (job_id,conv,user,instance,row[0],source.sequence,source.sequence),
            )
            connection.execute(
                """INSERT INTO cognitive_reflections(
                     reflection_id,operation_id,user_id,instance_id,
                     conversation_id,evidence_turn_id,content_fingerprint,
                     evidence_quote,kind,subject,content,confidence,
                     temporal_scope,model_id,processed_through_sequence,
                     review_status
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'USER',%s,%s,
                             %s,'context-test',%s,%s)""",
                (reflection_id,job_id,user,instance,conv,source.turn_id,
                 sha256(content.encode()).hexdigest(),
                 "meal targets",kind,content,confidence,scope,
                 source.sequence,review),
            )
    return reflection_id


def test_approved_goal_reaches_relevant_response_context(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    prompt,context=get_context(
        dsn,user,instance,conv,
        "What progress have I made on my eating goal?",
    )
    assert "Approved/delegated Goal state" in prompt
    assert str(gid) in prompt
    assert "Improve eating habits" in prompt
    assert "A healthier and sustainable diet" in prompt
    assert context.messages[-1].content==(
        "What progress have I made on my eating goal?"
    )
    assert sum(len(m.content) for m in context.messages)<=12000


def test_explicit_goal_reference_with_one_topic_word(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    prompt,_=get_context(
        dsn,user,instance,conv,
        "How is the eating goal progressing?",
    )
    assert "Approved/delegated Goal state" in prompt
    assert str(gid) in prompt


def test_generic_goal_qualifiers_are_not_topics(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    for text in ("What is my active goal?","What are my approved goals?"):
        prompt,_=get_context(dsn,user,instance,conv,text)
        assert "Approved/delegated Goal state" in prompt
        assert str(gid) in prompt


def test_unrelated_topic_does_not_pull_in_goals(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    prompt,_=get_context(
        dsn,user,instance,conv,
        "Explain how to renew an nginx certificate.",
    )
    assert "Approved/delegated Goal state" not in prompt
    assert str(gid) not in prompt
    assert "Tentative model hypothesis" not in prompt


def test_goal_evidence_stays_user_reported_not_verified(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    evidence=goals.turn(
        dsn,user,instance,conv,"I successfully prepared lunch today.",
    )
    goals.call(
        dsn,record_goal_evidence,goals.actor(user),
        RecordGoalEvidenceCommand(
            instance_id=instance,expected_generation=1,
            idempotency_key=uuid4().hex,conversation_id=conv,
            evidence_turn_id=evidence.turn_id,
            evidence_quote="successfully prepared lunch",
            goal_id=gid,expected_goal_version=1,
            evidence_kind="SUCCESS_REPORTED",evidence_id=uuid4(),
        ),
    )
    prompt,_=get_context(
        dsn,user,instance,conv,"How is my eating goal progressing?",
    )
    assert "User-reported, unreviewed Goal evidence" in prompt
    assert "successfully prepared lunch" in prompt
    assert "SUCCESS_REPORTED" in prompt
    assert "not verification" in prompt


def test_topical_reflection_is_explicitly_tentative(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    source=goals.turn(
        dsn,user,instance,conv,
        "I could not meet my weekly meal targets because work ran late.",
    )
    rid=hypothesis(
        dsn,user,instance,conv,source,
        "Weekly meal targets may be affected by time constraints.",
    )
    prompt,_=get_context(
        dsn,user,instance,conv,"How is my eating goal progressing?",
    )
    assert str(rid) in prompt
    assert "Tentative model hypothesis" in prompt
    assert "not fact/instruction" in prompt


def test_unapproved_and_rejected_reflections_are_not_context(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    source=goals.turn(
        dsn,user,instance,conv,
        "I could not meet my weekly meal targets this week.",
    )
    ids=[
        hypothesis(dsn,user,instance,conv,source,
                   "Meal targets rejected conjecture.",review="REJECTED"),
        hypothesis(dsn,user,instance,conv,source,
                   "Meal targets old conjecture.",scope="HISTORICAL"),
        hypothesis(dsn,user,instance,conv,source,
                   "Meal targets hidden intention.",kind="INTENTION"),
        hypothesis(dsn,user,instance,conv,source,
                   "Meal targets low confidence.",confidence=0.49),
    ]
    prompt,_=get_context(
        dsn,user,instance,conv,"How is my eating goal progressing?",
    )
    assert all(str(reflection_id) not in prompt for reflection_id in ids)
    assert "Tentative model hypothesis" not in prompt


def test_cross_user_scope_and_tight_budget_preserve_latest_turn(dsn):
    first,instance,conv,gid=setup_goal(dsn)
    second,other,_=goals.seed(dsn)
    prompt,_=get_context(
        dsn,second,other,uuid4(),
        "Can you tell me about my eating goal?",budget=1024,
    )
    assert str(gid) not in prompt
    assert "Approved/delegated Goal state" not in prompt
    small,context=get_context(
        dsn,first,instance,conv,
        "What progress have I made on my eating goal?",budget=1024,
    )
    assert context.messages[-1].content==(
        "What progress have I made on my eating goal?"
    )
    assert sum(len(m.content) for m in context.messages)<=1024


def test_paused_goal_not_reintroduced_as_active(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    turn=goals.turn(dsn,user,instance,conv,
                    "Please pause my eating goal for now.")
    cmd=SetGoalStatusCommand(
        instance_id=instance,expected_generation=1,
        idempotency_key=uuid4().hex,conversation_id=conv,
        evidence_turn_id=turn.turn_id,evidence_quote="pause my eating goal",
        goal_id=gid,expected_goal_version=1,new_status="PAUSED",
    )
    cmd=goals.authorize(dsn,user,instance,cmd)
    goals.call(dsn,set_goal_status,goals.actor(user),cmd)
    prompt,_=get_context(
        dsn,user,instance,conv,"How is my eating goal progressing?",
    )
    assert "Approved/delegated Goal state" not in prompt


def test_unique_goal_does_not_mask_specific_unrelated_goal(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    prompt,_=get_context(
        dsn,user,instance,conv,
        "Please check my goal to learn guitar.",
    )
    assert "Approved/delegated Goal state" not in prompt
    assert str(gid) not in prompt


def test_goal_intent_adjective_is_not_a_generic_reference(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    prompt,_=get_context(
        dsn,user,instance,conv,
        "My goal is to be more active.",
    )
    assert "Approved/delegated Goal state" not in prompt
    assert str(gid) not in prompt


def test_unowned_goal_qualifier_does_not_pull_personal_state(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    for question in ("What are active goals?","What is an approved goal?"):
        prompt,_=get_context(dsn,user,instance,conv,question)
        assert "Approved/delegated Goal state" not in prompt
        assert str(gid) not in prompt


def test_direct_context_builder_is_read_only_and_bounded(dsn):
    user,instance,conv,gid=setup_goal(dsn)
    with psycopg.connect(dsn,autocommit=True) as connection:
        before=connection.execute(
            "SELECT count(*) FROM events WHERE user_id=%s",(user,),
        ).fetchone()[0]
        from psycopg.rows import dict_row
        with connection.cursor(row_factory=dict_row) as cur:
            result=goal_reflection_context_block(
                cur,user_id=user,instance_id=instance,
                conversation_id=conv,
                latest_text="How is my eating goal progressing?",
                max_chars=700,
            )
        after=connection.execute(
            "SELECT count(*) FROM events WHERE user_id=%s",(user,),
        ).fetchone()[0]
    assert before==after
    assert len(result)<=700
    assert "Approved/delegated Goal state" in result
