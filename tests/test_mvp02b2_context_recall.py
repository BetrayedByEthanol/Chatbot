"""MVP-02B.2: independent paraphrase and source-linked discussion continuity.

These are PostgreSQL-backed, real-host tests. Model text is intentionally
noncommittal; assertions inspect the actual context before generation.
"""
import pytest

psycopg=pytest.importorskip("psycopg")
from psycopg.rows import dict_row

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import ModelReply,TextInputCommand
from persona_agent.contracts import TrustedActor
from persona_agent.historical_user_turns import historical_user_turn_context_block

PARAPHRASE="What outdoor activity had I been considering?"
DIRECT="Do you remember what we discussed about walking?"
SOURCE="I've been thinking about walking more regularly."
NOTE="Maybe three times per week."
CORRECTION="Actually, twice per week would be better."
LATEST_CORRECTION="Actually, once per week would be better."


class InspectModel:
    def __init__(self):
        self.contexts=[]

    def generate(self,messages):
        self.contexts.append(tuple(messages))
        return ModelReply(
            model_id="mvp02b2.inspect",
            text="I can discuss the evidence without inventing a commitment.",
        )


def fresh_host(dsn):
    model=InspectModel()
    host=BehavioralAcceptanceHarness(dsn,seed_local_demo(dsn),model)
    return host,model


def system(model):
    return model.contexts[-1][0].content


def test_first_message_in_new_session_paraphrases_original_activity(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    source=host.send(SOURCE,idempotency_key="activity")
    host.begin_session("new")
    host.restart_host()
    host.send(PARAPHRASE,idempotency_key="paraphrase")
    prompt=system(model)
    assert "Prior-session user statements" in prompt
    assert SOURCE in prompt and source.turn_id in prompt
    assert "USER_REPORTED" in prompt
    assert model.contexts[-1][-1].content==PARAPHRASE
    assert all(SOURCE!=m.content for m in model.contexts[-1][1:])


def test_broad_activity_paraphrase_abstains_when_multiple_activities(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    first=host.send(SOURCE,idempotency_key="walk")
    second=host.send(
        "I've been considering hiking more regularly.",
        idempotency_key="hike",
    )
    host.begin_session("new")
    host.send(PARAPHRASE,idempotency_key="ambiguous")
    assert "Prior-session user statements" not in system(model)
    assert first.turn_id not in system(model)
    assert second.turn_id not in system(model)


def test_historical_correction_and_tentative_cadence_remain_ordered(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    source=host.send(SOURCE,idempotency_key="source")
    note=host.send(NOTE,idempotency_key="note")
    correction=host.send(CORRECTION,idempotency_key="correction")
    host.begin_session("new")
    host.send(DIRECT,idempotency_key="recall")
    prompt=system(model)
    assert SOURCE in prompt
    assert NOTE in prompt
    assert CORRECTION in prompt
    assert prompt.index(SOURCE)<prompt.index(NOTE)<prompt.index(CORRECTION)
    assert source.turn_id in prompt
    assert note.turn_id in prompt
    assert correction.turn_id in prompt
    assert "USER_REPORTED discussion update" in prompt
    # Tentative conversational evidence is not an approved Goal.
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM goal_records WHERE user_id=%s "
            "AND instance_id=%s",
            (host.identity.user_id,host.identity.instance_id),
        ).fetchone()[0]==0


def test_latest_of_two_successive_corrections_wins_within_lookahead(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    source=host.send(SOURCE,idempotency_key="source")
    note=host.send(NOTE,idempotency_key="note")
    first=host.send(CORRECTION,idempotency_key="first-correction")
    latest=host.send(
        LATEST_CORRECTION,idempotency_key="latest-correction",
    )
    host.begin_session("new")
    host.restart_host()
    host.send(DIRECT,idempotency_key="recall")
    prompt=system(model)
    for turn,text in (
        (source,SOURCE),(note,NOTE),(first,CORRECTION),
        (latest,LATEST_CORRECTION),
    ):
        assert text in prompt
        assert turn.turn_id in prompt
    assert (prompt.index(SOURCE)<prompt.index(NOTE)
            <prompt.index(CORRECTION)<prompt.index(LATEST_CORRECTION))
    assert "USER_REPORTED discussion update" in prompt


def test_latest_correction_survives_tight_context_budget(dsn):
    host,model=fresh_host(dsn)
    original=host.begin_session("old")
    host.send(SOURCE,idempotency_key="source")
    host.send(NOTE,idempotency_key="note")
    host.send(CORRECTION,idempotency_key="first-correction")
    host.send(LATEST_CORRECTION,idempotency_key="latest-correction")
    host.begin_session("new")
    recall=host.send(DIRECT,idempotency_key="recall")
    # Use the same read-only retrieval contract with a deliberately smaller
    # optional prompt budget. The old cadence must not be shown without
    # the last correction when the entire chain does not fit.
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            at=conn.execute(
                "SELECT created_at FROM conversation_turns "
                "WHERE turn_id=%s AND user_id=%s AND instance_id=%s",
                (recall.turn_id,original.user_id,original.instance_id),
            ).fetchone()["created_at"]
            def retrieve(max_chars):
                with conn.cursor(row_factory=dict_row) as cur:
                    return historical_user_turn_context_block(
                        cur,user_id=original.user_id,
                        instance_id=original.instance_id,
                        conversation_id=host.active_identity.conversation_id,
                        before_at=at,latest_text=DIRECT,max_chars=max_chars,
                    )
            full=retrieve(1100)
            assert all(item in full for item in (
                SOURCE,NOTE,CORRECTION,LATEST_CORRECTION,
            ))
            lines=full.splitlines(keepends=True)
            assert len(lines)==5  # header, source, note, two corrections
            short_budget=sum(len(lines[i]) for i in (0,1,4))+4
            assert short_budget<len(full)
            constrained=retrieve(short_budget)
    assert SOURCE in constrained
    assert LATEST_CORRECTION in constrained
    assert NOTE not in constrained
    assert CORRECTION not in constrained
    assert len(constrained)<=short_budget


def test_older_matching_source_still_recovers_correction(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    source=host.send(SOURCE,idempotency_key="original")
    host.send(NOTE,idempotency_key="note")
    correction=host.send(CORRECTION,idempotency_key="correction")
    # A second, newer lexical hit must not hide correction evidence linked
    # to the earlier walking discussion.
    host.send(
        "I took another walk yesterday.",
        idempotency_key="recent-walk",
    )
    host.begin_session("new")
    host.send(DIRECT,idempotency_key="recall")
    prompt=system(model)
    assert source.turn_id in prompt
    assert correction.turn_id in prompt
    assert CORRECTION in prompt


def test_paraphrase_recalls_correction_without_direct_recall_first(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    host.send(SOURCE,idempotency_key="source")
    host.send(NOTE,idempotency_key="note")
    host.send(CORRECTION,idempotency_key="correct")
    host.begin_session("new")
    host.send(PARAPHRASE,idempotency_key="independent")
    prompt=system(model)
    assert SOURCE in prompt
    assert CORRECTION in prompt


def test_topic_switch_blocks_unrelated_frequency_correction(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    host.send(SOURCE,idempotency_key="source")
    host.send("Switch the topic to cooking.",idempotency_key="switch")
    host.send(CORRECTION,idempotency_key="correction")
    host.begin_session("new")
    host.send(DIRECT,idempotency_key="recall")
    prompt=system(model)
    assert SOURCE in prompt
    assert CORRECTION not in prompt
    assert "Switch the topic to cooking" not in prompt


@pytest.mark.parametrize("unanswered", [
    "Switch the topic to cooking.",
    "Maybe three times per week.",
])
def test_superseded_intermediate_turn_blocks_correction_lookahead(
    dsn, unanswered,
):
    """A pending user turn must stay a continuity barrier after supersession.

    Exercise normal authenticated ingest rather than changing status via
    fixture SQL: the next user command supersedes an unanswered turn.
    """
    host,model=fresh_host(dsn)
    identity=host.begin_session("old")
    source=host.send(SOURCE,idempotency_key="walking-source")
    actor=TrustedActor(
        user_id=identity.user_id,source_id="mvp02b2-supersession",
    )
    with psycopg.connect(dsn,autocommit=True) as conn:
        unfinished=ingest_text(
            conn,actor,TextInputCommand(
                conversation_id=identity.conversation_id,
                instance_id=identity.instance_id,
                expected_generation=identity.generation,
                idempotency_key="unanswered-intermediate-"+str(len(unanswered)),
                text=unanswered,
            ),
        )
        assert conn.execute(
            "SELECT status FROM conversation_turns WHERE turn_id=%s",
            (unfinished.turn_id,),
        ).fetchone()[0]=="PENDING"

    correction=host.send(CORRECTION,idempotency_key="newer-correction")
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        rows=conn.execute(
            "SELECT sequence,input_text,status FROM conversation_turns "
            "WHERE conversation_id=%s AND user_id=%s AND instance_id=%s "
            "ORDER BY sequence",
            (identity.conversation_id,identity.user_id,identity.instance_id),
        ).fetchall()
    assert [(row["input_text"],row["status"]) for row in rows]==[
        (SOURCE,"RESPONDED"),
        (unanswered,"SUPERSEDED"),
        (CORRECTION,"RESPONDED"),
    ]

    host.begin_session("new")
    host.restart_host()
    host.send(DIRECT,idempotency_key="recalled-walking")
    prompt=system(model)
    assert SOURCE in prompt and source.turn_id in prompt
    assert CORRECTION not in prompt
    assert correction.turn_id not in prompt
    assert unanswered not in prompt


def test_unrelated_intermediate_turn_blocks_correction_lookahead(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    host.send(SOURCE,idempotency_key="source")
    host.send("I cooked pasta for dinner.",idempotency_key="other")
    host.send(CORRECTION,idempotency_key="correction")
    host.begin_session("new")
    host.send(DIRECT,idempotency_key="recall")
    prompt=system(model)
    assert SOURCE in prompt
    assert CORRECTION not in prompt
    assert "cooked pasta" not in prompt


def test_general_questions_are_not_archival_recall_requests(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    host.send(SOURCE,idempotency_key="source")
    host.begin_session("new")
    for index,question in enumerate((
        "What are some outdoor activities?",
        "Can you suggest an exercise?",
        "What outdoor activity would you recommend?",
    )):
        host.send(question,idempotency_key=f"ordinary-{index}")
        assert "Prior-session user statements" not in system(model)


def test_paraphrase_retains_original_boundary_and_no_cross_user_leak(dsn):
    owner,_=fresh_host(dsn)
    owner.begin_session("old")
    secret=owner.send(SOURCE,idempotency_key="source")
    outsider,model=fresh_host(dsn)
    outsider.begin_session("new")
    outsider.send(PARAPHRASE,idempotency_key="paraphrase")
    assert secret.turn_id not in system(model)
    assert SOURCE not in system(model)


def test_context_budget_with_correlated_updates(dsn):
    host,model=fresh_host(dsn)
    host.begin_session("old")
    host.send(SOURCE,idempotency_key="source")
    host.send(NOTE,idempotency_key="note")
    host.send(CORRECTION,idempotency_key="correction")
    host.begin_session("new")
    host.send(DIRECT,idempotency_key="recall")
    request=model.contexts[-1]
    assert sum(len(message.content) for message in request)<=12000
    assert request[-1].content==DIRECT
