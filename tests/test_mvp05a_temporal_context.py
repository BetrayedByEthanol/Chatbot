"""MVP-05A trusted receipt time, scoped anchors and bounded date resolution."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from persona_agent.temporal_context import (
    InteractionAnchor, TemporalContext, resolve_relative_time,
    temporal_context_block, validate_timezone,
)


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@pytest.mark.parametrize("text,zone,at,status,expected",[
    ("tomorrow","Europe/Berlin","2026-11-25T23:30:00+00:00","DATE",date(2026,11,27)),
    ("What date is tomorrow?","America/New_York","2026-11-25T01:00:00+00:00",
     "DATE",date(2026,11,25)),
    ("yesterday","Europe/Berlin","2026-01-01T12:00:00+00:00","DATE",date(2025,12,31)),
    ("tomorrow at 14:30","Europe/Berlin","2026-11-25T11:00:00+00:00","INSTANT",
     utc("2026-11-26T13:30:00+00:00")),
    ("tomorrow at 02:30","Europe/Berlin","2026-03-28T12:00:00+00:00",
     "NONEXISTENT_LOCAL_TIME",None),
    ("tomorrow at 02:30","Europe/Berlin","2026-10-24T12:00:00+00:00",
     "AMBIGUOUS_LOCAL_TIME",None),
    ("tomorrow at 03:30","Europe/Berlin","2026-03-28T12:00:00+00:00","INSTANT",
     utc("2026-03-29T01:30:00+00:00")),
    ("in 2 hours","Europe/Berlin","2026-03-29T00:30:00+00:00","INSTANT",
     utc("2026-03-29T02:30:00+00:00")),
    ("in 15 minutes",None,"2026-11-25T11:00:00+00:00","INSTANT",
     utc("2026-11-25T11:15:00+00:00")),
    ("tomorrow",None,"2026-11-25T11:00:00+00:00","UNKNOWN_TIMEZONE",None),
])
def test_relative_resolution_uses_receipt_timezone_and_dst(text,zone,at,status,expected):
    result=resolve_relative_time(text,utc(at),ZoneInfo(zone) if zone else None)
    assert result.status==status
    if status=="DATE":
        assert result.local_date==expected and result.instant is None
    else:
        assert result.instant==expected


@pytest.mark.parametrize("text",[
    "Maybe tomorrow at 14:30.", "Tomorrow at 2:30", "next Friday",
    "What if tomorrow at 14:30?", '"tomorrow at 14:30"',
    "Tomorrow at 14:30 or 15:00", "tomorrow at 25:00",
    "Remind me tomorrow at 14:30.", "in 1000 hours",
])
def test_unsupported_or_compound_temporal_language_abstains(text):
    assert resolve_relative_time(text,utc("2026-11-25T11:00:00+00:00"),
                                 ZoneInfo("Europe/Berlin")) is None


def test_invalid_zone_and_naive_receipt_fail_closed():
    for name in ("Berlin","../UTC","", "Not/AZone"):
        with pytest.raises(ValueError):
            validate_timezone(name)
    with pytest.raises(ValueError):
        resolve_relative_time("tomorrow",datetime(2026,11,25),None)


def test_elapsed_time_is_instant_based_and_prompt_is_selective():
    zone=ZoneInfo("Europe/Berlin")
    first=InteractionAnchor(turn_id=uuid4(),conversation_id=uuid4(),
                            at=utc("2026-03-29T00:30:00+00:00").astimezone(zone))
    now=InteractionAnchor(turn_id=uuid4(),conversation_id=uuid4(),
                          at=utc("2026-03-29T01:30:00+00:00").astimezone(zone))
    context=TemporalContext(receipt=now,timezone_name=zone.key,local_receipt_at=now.at,
        first_recorded=first,previous_message=first,previous_other_session=first,
        relative=None)
    assert context.elapsed_seconds(first)==3600
    assert context.elapsed_seconds(None) is None
    future=first.model_copy(update={"at":now.at+timedelta(hours=2)})
    assert context.elapsed_seconds(future) is None
    assert temporal_context_block("Explain Nginx.",context,max_chars=850)==""
    assert temporal_context_block("Hello",context,max_chars=10)==""
    block=temporal_context_block("How long since our last session?",context,max_chars=850)
    assert "Previous recorded message: 3600 s" in block
    assert "Last input in another conversation: 3600 s" in block
    assert "not user presence or permission to act" in block


def test_scoped_snapshots_survive_restart_replay_and_timezone_changes(dsn):
    from test_phase1_conversation import seed,actor,call,command,FakeGateway
    from persona_agent.conversation import (
        ingest_text,prepare_text_context,prepare_direct_tool_context,respond_to_text,
        acknowledge_text_delivery,
    )
    import psycopg
    user,instance=seed(dsn)
    stranger,other=seed(dsn)
    conv=uuid4()
    first=call(dsn,ingest_text,actor(user),command(instance,conv,"Hello"),
               timezone_name="Europe/Berlin")
    context=call(dsn,prepare_text_context,actor(user),first.turn_id)
    temporal=context.temporal_context
    assert temporal.previous_message is None
    assert temporal.previous_other_session is None
    assert temporal.first_recorded.turn_id==first.turn_id
    assert temporal.timezone_name=="Europe/Berlin"
    reply=call(dsn,respond_to_text,actor(user),first.turn_id,FakeGateway("Hello."))
    call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    alien=call(dsn,ingest_text,actor(stranger),command(other,text="Other user"),
               timezone_name="Asia/Tokyo")
    second=call(dsn,ingest_text,actor(user),command(instance,conv,"Explain Nginx."),
                timezone_name="America/New_York")
    c2=call(dsn,prepare_text_context,actor(user),second.turn_id)
    assert c2.temporal_context.previous_message.turn_id==first.turn_id
    assert c2.temporal_context.previous_other_session is None
    assert "Trusted receipt-time context" not in c2.messages[0].content
    next_command=command(instance,uuid4(),"tomorrow at 14:30")
    third=call(dsn,ingest_text,actor(user),next_command,timezone_name="Asia/Tokyo")
    c3=call(dsn,prepare_text_context,actor(user),third.turn_id)
    t3=c3.temporal_context
    assert t3.previous_message.turn_id==second.turn_id
    assert t3.previous_other_session.turn_id==second.turn_id
    assert t3.first_recorded.turn_id==first.turn_id
    assert t3.relative.status=="INSTANT"
    assert t3.relative.instant.astimezone(ZoneInfo("Asia/Tokyo")).hour==14
    assert "Resolved relative instant" in c3.messages[0].content
    assert str(alien.turn_id) not in t3.model_dump_json()
    replay=call(dsn,ingest_text,actor(user),next_command,timezone_name="UTC")
    assert replay.replayed and replay.turn_id==third.turn_id
    assert call(dsn,prepare_text_context,actor(user),third.turn_id).temporal_context==t3
    assert call(dsn,prepare_direct_tool_context,actor(user),third.turn_id).temporal_context==t3
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT temporal_context->>'timezone_name' FROM conversation_turns WHERE turn_id=%s",
            (third.turn_id,),
        ).fetchone()==("Asia/Tokyo",)
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s",(user,),
        ).fetchone()[0]==0


def test_legacy_snapshot_does_not_invent_timezone_or_rewrite_turn(dsn):
    from test_phase1_conversation import seed,actor,call,command
    from persona_agent.conversation import ingest_text,prepare_text_context
    import psycopg
    user,instance=seed(dsn)
    turn=call(dsn,ingest_text,actor(user),command(instance,text="tomorrow"))
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE conversation_turns SET temporal_context=NULL WHERE turn_id=%s",
                     (turn.turn_id,))
    context=call(dsn,prepare_text_context,actor(user),turn.turn_id).temporal_context
    assert context.basis=="LEGACY_READ" and context.timezone_name is None
    assert context.relative.status=="UNKNOWN_TIMEZONE"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT temporal_context FROM conversation_turns WHERE turn_id=%s",
                            (turn.turn_id,)).fetchone()==(None,)


def test_host_persists_explicit_zone_without_treating_utc_fallback_as_user_zone(dsn):
    from persona_agent.acceptance_host import LocalAcceptanceSession,seed_local_demo
    from test_phase1_conversation import FakeGateway
    import psycopg
    identity=seed_local_demo(dsn)
    model=FakeGateway("We can talk.")
    host=LocalAcceptanceSession(dsn,identity,model,lambda packet:None)
    first=host.message(text="What time is it?",idempotency_key="unknown-zone")
    assert first["model_id"]=="clock.now.v1" and "Current time in UTC:" in first["text"]
    second=host.message(text="What time is it?",idempotency_key="known-zone",
                        timezone_name="Europe/Berlin")
    assert second["model_id"]=="clock.now.v1" and "Current time in Europe/Berlin:" in second["text"]
    assert not model.calls
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT temporal_context->>'timezone_name' FROM conversation_turns
               WHERE conversation_id=%s ORDER BY sequence""",(identity.conversation_id,),
        ).fetchall()==[(None,),("Europe/Berlin",)]


@pytest.mark.parametrize("original,expected",[
    (None,"UTC"),("Europe/Berlin","Europe/Berlin"),
])
def test_pending_auto_clock_retry_uses_original_ingress_zone(dsn,original,expected):
    from test_phase1_conversation import seed,actor,call,command,FakeGateway
    from persona_agent.conversation import ingest_text
    from persona_agent.response_orchestrator import respond_to_user_text
    user,instance=seed(dsn)
    request=command(instance,text="What time is it?")
    turn=call(dsn,ingest_text,actor(user),request,timezone_name=original)
    replay=call(dsn,ingest_text,actor(user),request,timezone_name="Asia/Tokyo")
    assert replay.replayed and replay.turn_id==turn.turn_id
    model=FakeGateway("Not used")
    reply=call(dsn,respond_to_user_text,actor(user),turn.turn_id,model,
               default_timezone="Asia/Tokyo")
    assert reply.text.startswith(f"Current time in {expected}:")
    assert not model.calls


def test_temporal_anchors_do_not_cross_persona_switch(dsn):
    from test_phase1_conversation import seed,actor,call,command
    from persona_agent.conversation import ingest_text,prepare_text_context
    from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
    user,instance,other=seed(dsn,two=True)
    first=call(dsn,ingest_text,actor(user),command(instance,text="Hello"))
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    second=call(dsn,ingest_text,actor(user),
                command(other,text="Hello",generation=2))
    snapshot=call(dsn,prepare_text_context,actor(user),second.turn_id).temporal_context
    assert snapshot.previous_message is None
    assert snapshot.previous_other_session is None
    assert snapshot.first_recorded.turn_id==second.turn_id
    assert str(first.turn_id) not in snapshot.model_dump_json()


def test_unsupported_relative_wording_gets_qualified_context():
    receipt=InteractionAnchor(turn_id=uuid4(),conversation_id=uuid4(),
                              at=utc("2026-11-25T11:00:00+00:00"))
    context=TemporalContext(
        receipt=receipt,timezone_name=None,local_receipt_at=None,
        first_recorded=receipt,previous_message=None,previous_other_session=None,
        relative=None,
    )
    assert "No exact relative-time resolution recorded" in temporal_context_block(
        "Next Friday",context,max_chars=850,
    )
