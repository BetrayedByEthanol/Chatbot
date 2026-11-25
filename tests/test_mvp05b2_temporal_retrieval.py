"""MVP-05B.2: receipt-anchored historical retrieval and ambiguity."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from persona_agent.temporal_context import (
    HistoricalWindow, InteractionAnchor, TemporalContext, historical_recall_window,
)


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def context(at,zone):
    receipt=InteractionAnchor(turn_id=uuid4(),conversation_id=uuid4(),at=utc(at))
    return TemporalContext(
        receipt=receipt,timezone_name=zone,
        local_receipt_at=receipt.at.astimezone(ZoneInfo(zone)) if zone else None,
        first_recorded=receipt,previous_message=None,previous_other_session=None,
        relative=None,
    )


@pytest.mark.parametrize("period,at,zone,start,end",[
    ("three weeks ago","2026-11-25T12:00:00+00:00","Europe/Berlin",
     "2026-11-01T23:00:00+00:00","2026-11-08T23:00:00+00:00"),
    ("last week","2026-11-25T12:00:00+00:00","Europe/Berlin",
     "2026-11-15T23:00:00+00:00","2026-11-22T23:00:00+00:00"),
    ("yesterday","2026-03-30T12:00:00+00:00","Europe/Berlin",
     "2026-03-28T23:00:00+00:00","2026-03-29T22:00:00+00:00"),
    ("yesterday","2026-10-26T12:00:00+00:00","Europe/Berlin",
     "2026-10-24T22:00:00+00:00","2026-10-25T23:00:00+00:00"),
    ("on 2026-01-01","2026-11-25T12:00:00+00:00","America/New_York",
     "2026-01-01T05:00:00+00:00","2026-01-02T05:00:00+00:00"),
    ("last month","2026-01-10T12:00:00+00:00","UTC",
     "2025-12-01T00:00:00+00:00","2026-01-01T00:00:00+00:00"),
    ("today","2026-11-25T02:00:00+00:00","America/New_York",
     "2026-11-24T05:00:00+00:00","2026-11-25T02:00:00+00:00"),
    ("two days ago","2026-11-25T02:00:00+00:00","America/New_York",
     "2026-11-22T05:00:00+00:00","2026-11-23T05:00:00+00:00"),
])
def test_windows_use_local_calendar_and_exclusive_utc_end(period,at,zone,start,end):
    value=historical_recall_window(
        f"What did I tell you about my exercise plans {period}?",context(at,zone),
    )
    assert value.status=="RESOLVED"
    assert value.topic=="my exercise plans"
    assert value.start==utc(start) and value.end==utc(end)
    assert zone in value.label


@pytest.mark.parametrize("body,zone,status",[
    ("gardening yesterday",None,"UNKNOWN_TIMEZONE"),
    ("gardening last week or yesterday","UTC","AMBIGUOUS_PERIOD"),
    ("gardening and cooking yesterday","UTC","AMBIGUOUS_PERIOD"),
    ("that yesterday","UTC","AMBIGUOUS_PERIOD"),
    ("gardening three months ago","UTC","UNSUPPORTED_PERIOD"),
    ("gardening on 2026-02-30","UTC","UNSUPPORTED_PERIOD"),
    ("gardening on 2027-01-01","UTC","UNSUPPORTED_PERIOD"),
    ("gardening 99 weeks ago","UTC","UNSUPPORTED_PERIOD"),
    ("gardening yesterday at 15:00","UTC","UNSUPPORTED_PERIOD"),
])
def test_missing_ambiguous_or_unsupported_period_never_becomes_undated(body,zone,status):
    result=historical_recall_window(
        "What did I say about "+body+"?",context("2026-11-25T12:00:00+00:00",zone),
    )
    assert result.status==status and result.start is None and result.end is None


@pytest.mark.parametrize("text",[
    "What did I say about gardening?",
    "Explain gardening last week.",
    "I remember walking yesterday.",
    '"What did I say about gardening yesterday?"',
    "What if I asked what did I say about gardening yesterday?",
    "Do you remember our conversation about gardening?",
])
def test_ordinary_and_unsupported_question_families_do_not_activate_new_path(text):
    assert historical_recall_window(
        text,context("2026-11-25T12:00:00+00:00","UTC"),
    ) is None


def test_unresolved_and_tight_budget_do_not_query():
    from persona_agent.historical_user_turns import dated_recall_context_block
    class NoSQL:
        def execute(self,*args,**kwargs):
            raise AssertionError("unresolved date must not query history")
    for state in ("UNKNOWN_TIMEZONE","AMBIGUOUS_PERIOD","UNSUPPORTED_PERIOD"):
        block=dated_recall_context_block(
            NoSQL(),user_id=uuid4(),instance_id=uuid4(),
            window=HistoricalWindow(state),max_chars=600,
        )
        assert state in block and "Do not search an undated substitute" in block
    assert dated_recall_context_block(
        NoSQL(),user_id=uuid4(),instance_id=uuid4(),
        window=HistoricalWindow("UNKNOWN_TIMEZONE"),max_chars=40,
    )==""


# PostgreSQL fixtures backdate source receipt times only in disposable data.
# The actual requested window is resolved from authenticated ingress metadata.
def _source(dsn,user,instance,text,at,*,conversation=None,gen=1):
    import psycopg
    from test_phase1_conversation import actor,call,command,FakeGateway
    from persona_agent.conversation import ingest_text,respond_to_text,acknowledge_text_delivery
    source=call(dsn,ingest_text,actor(user),
                command(instance,conversation,text,generation=gen),timezone_name="UTC")
    reply=call(dsn,respond_to_text,actor(user),source.turn_id,FakeGateway("Understood."))
    call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE conversation_turns SET created_at=%s WHERE turn_id=%s",
                     (at,source.turn_id))
    return source


def _request(dsn,user,instance,text,zone="UTC",*,conversation=None,budget=12000):
    from test_phase1_conversation import actor,call,command
    from persona_agent.conversation import ingest_text,prepare_text_context
    request=call(dsn,ingest_text,actor(user),command(instance,conversation,text),
                 timezone_name=zone)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id,max_context_chars=budget)
    assert ctx.messages[-1].content==text
    assert sum(len(m.content) for m in ctx.messages)<=budget
    return request,ctx


def _day(dsn):
    import psycopg
    with psycopg.connect(dsn,autocommit=True) as conn:
        now=conn.execute("SELECT clock_timestamp()").fetchone()[0]
    return now.astimezone(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=2)


def test_dated_recall_uses_window_and_scoped_original_evidence(dsn):
    from test_phase1_conversation import seed,actor,call
    from persona_agent.conversation import prepare_text_context
    user,instance=seed(dsn)
    outsider,other=seed(dsn)
    day=_day(dsn)
    inside=_source(dsn,user,instance,"My exercise plans include swimming.",day)
    second=_source(dsn,user,instance,"My exercise plans include cycling.",day+timedelta(hours=8))
    before=_source(dsn,user,instance,"My exercise plans include hiking.",day-timedelta(microseconds=1))
    after=_source(dsn,user,instance,"My exercise plans include jogging.",day+timedelta(days=1))
    foreign=_source(dsn,outsider,other,"My exercise plans include climbing.",day+timedelta(hours=2))
    irrelevant=_source(dsn,user,instance,"I enjoy cooking pasta.",day+timedelta(hours=2))
    sensitive=_source(dsn,user,instance,"My exercise plans contain a secret password.",day+timedelta(hours=3))
    question=f"What did I tell you about my exercise plans on {day.date()}?"
    request,ctx=_request(dsn,user,instance,question)
    block=ctx.messages[0].content
    assert "Dated recall" in block and "Multiple matching source turns" in block
    assert "swimming" in block and str(inside.turn_id) in block
    assert "cycling" in block and str(second.turn_id) in block
    for item in (before,after,foreign,irrelevant,sensitive):
        assert str(item.turn_id) not in block
    for word in ("hiking","jogging","climbing","cooking pasta","secret password"):
        assert word not in block
    assert "Historical user-reported memory" not in block
    assert "Prior-session user statements" not in block
    # New connection, same stored receipt and the same dated evidence.
    retry=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert retry.temporal_context==ctx.temporal_context
    assert retry.messages==ctx.messages


def test_dated_recall_includes_eligible_owner_record_and_omits_superseded_source(dsn):
    from test_phase1_conversation import seed,actor,call
    from test_phase2_memory import candidate,correction
    from persona_agent.memory import admit_memory,amend_memory
    user,instance=seed(dsn)
    day=_day(dsn)
    old=_source(dsn,user,instance,"My favorite drink is tea.",day+timedelta(hours=1))
    saved=call(dsn,admit_memory,actor(user),candidate(instance,old.conversation_id,old))
    question=f"What did I say about my favorite drink on {day.date()}?"
    _,ctx=_request(dsn,user,instance,question)
    assert "favorite_drink: tea" in ctx.messages[0].content
    assert "USER_REPORTED revision 1" in ctx.messages[0].content
    later=_source(dsn,user,instance,"Actually my favorite drink is coffee.",
                  day+timedelta(days=1,hours=1))
    call(dsn,amend_memory,actor(user),correction(
        instance,later.conversation_id,later,saved.memory_id,
    ))
    _,ctx=_request(dsn,user,instance,question)
    prompt=ctx.messages[0].content
    assert "no usable dated evidence" in prompt
    assert "favorite_drink: tea" not in prompt and "favorite_drink: coffee" not in prompt


@pytest.mark.parametrize("question,zone",[
    ("What did I say about gardening yesterday?",None),
    ("What did I say about gardening last week or yesterday?","UTC"),
    ("What did I say about gardening three months ago?","UTC"),
])
def test_unresolved_request_skips_all_optional_historical_routes(dsn,monkeypatch,question,zone):
    from test_phase1_conversation import seed
    user,instance=seed(dsn)
    def forbidden(*args,**kwargs):
        raise AssertionError("undated historical fallback must not run")
    for name in (
        "persona_agent.memory.contextual_memory_block",
        "persona_agent.summary_memory.episode_context_block",
        "persona_agent.summary_memory.summary_context_block",
        "persona_agent.historical_user_turns.historical_user_turn_context_block",
        "persona_agent.memory_retrieval._candidates",
        "persona_agent.memory_retrieval.topic_recall_block",
    ):
        monkeypatch.setattr(name,forbidden)
    _,ctx=_request(dsn,user,instance,question,zone)
    assert "Dated recall unresolved" in ctx.messages[0].content
    _,small=_request(dsn,user,instance,question,zone,budget=512)
    assert "no usable dated evidence" in small.messages[0].content
