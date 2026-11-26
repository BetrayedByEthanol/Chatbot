"""MVP-07A authenticated application boundary, including revocation races."""
import io
import json
from uuid import uuid4

import pytest

from persona_agent.application_contracts import ApplicationEnvelope


@pytest.mark.parametrize("payload",[
    {"action":"message","text":"Hello","idempotency_key":"one","user_id":str(uuid4())},
    {"action":"message","text":"Hello","idempotency_key":"one","source_id":"trusted-platform"},
    {"action":"message","text":"Hello","idempotency_key":"one","instance_id":str(uuid4())},
    {"action":"message","text":"Hello","idempotency_key":"one","mode":"time"},
    {"action":"message","text":"Hello","idempotency_key":"one","provider_url":"http://example.test"},
    {"action":"message","text":"Hello","idempotency_key":"one","capabilities":["external"]},
    {"action":"platform_callback","user_id":str(uuid4())},
    {"action":"complete_operation","claim_generation":1},
    {"action":"switch_persona","target_instance_id":str(uuid4())},
    {"action":"presence","idempotency_key":"one","mode":"BACKGROUND"},
])
def test_untrusted_application_contract_rejects_authority_fields(payload):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ApplicationEnvelope.model_validate({"token":"secret","request":payload})


def helpers():
    pytest.importorskip("psycopg")
    from test_phase1_conversation import seed,call,actor,command,FakeGateway
    return seed,call,actor,command,FakeGateway


def provision(dsn,user,instance,*,conversation_id=None,generation=1):
    from persona_agent.application_auth import issue_application_session
    from persona_agent.lifecycle import TrustedPlatformActor
    _,call,_,_,_=helpers()
    return call(dsn,issue_application_session,
        TrustedPlatformActor(user_id=user,source_id="test.operator"),
        instance,generation,conversation_id=conversation_id)


def invoke(app,credential,action,**fields):
    return app.handle(json.dumps({"token":credential.token,
        "request":{"action":action,**fields}}).encode())


def message(app,credential,text="Hello there",key="message-one"):
    return invoke(app,credential,"message",text=text,idempotency_key=key)


def test_authenticated_reconnect_delivery_and_tenant_isolation(dsn):
    import psycopg
    from persona_agent.application_host import AuthenticatedTextApplication
    from persona_agent.application_auth import issue_application_session,authenticate_application
    from persona_agent.lifecycle import TrustedPlatformActor
    from persona_agent.runtime import PermissionDenied
    seed,call,actor,_,FakeGateway=helpers()
    user,instance,alternate=seed(dsn,two=True)
    foreign,other=seed(dsn)
    a=provision(dsn,user,instance)
    b=provision(dsn,foreign,other)
    same_user_other_conversation=provision(dsn,user,instance)
    same_conversation_new_credential=provision(dsn,user,instance,conversation_id=a.conversation_id)
    gateway=FakeGateway("Hello back")
    app=AuthenticatedTextApplication(dsn,gateway)
    assert a.token not in repr(a)
    assert app.handle(b'{"token":"wrong","request":{"action":"status"}}')["code"]=="NOT_AUTHORIZED"
    assert invoke(app,a,"status")["latest"] is None
    with pytest.raises(PermissionDenied):
        call(dsn,issue_application_session,actor(user),instance,1)
    first=message(app,a)
    assert first["type"]=="reply" and first["delivery_status"]=="GENERATED"
    assert len(gateway.calls)==1
    assert invoke(app,b,"status")["latest"] is None
    for other_session in (b,same_user_other_conversation):
        assert invoke(app,other_session,"resume",turn_id=first["turn_id"])["code"]=="NOT_AUTHORIZED"
        assert invoke(app,other_session,"ack",response_id=first["response_id"])["code"]=="NOT_AUTHORIZED"
    assert invoke(app,same_conversation_new_credential,"ack",
        response_id=first["response_id"])["code"]=="NOT_AUTHORIZED"
    # No transport acknowledgement from successful generation alone.
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (first["response_id"],)).fetchone()==("GENERATED",)
        token_hash=conn.execute("SELECT token_sha256 FROM application_sessions WHERE session_id=%s",
            (a.session_id,)).fetchone()[0]
        assert a.token!=token_hash and len(token_hash)==64
    # Fresh host, same credential; exact key and generated reply replay.
    recovered=AuthenticatedTextApplication(dsn,FakeGateway("Must not regenerate"))
    assert invoke(recovered,a,"status")["latest"]["response_id"]==first["response_id"]
    replay=message(recovered,a)
    assert replay["response_id"]==first["response_id"] and replay["replayed"]
    assert recovered._gateway.calls==[]
    assert message(recovered,a,"Different content")["code"]=="CONFLICT"
    assert invoke(recovered,a,"ack",response_id=first["response_id"])=={
        "type":"ack","response_id":first["response_id"],"replayed":False}
    assert invoke(recovered,a,"ack",response_id=first["response_id"])["replayed"]
    # A new credential in the same scope can resume the durable response
    # after presenting its own bearer; ACK then has its own offer.
    replay=invoke(recovered,same_conversation_new_credential,"resume",turn_id=first["turn_id"])
    assert replay["delivery_status"]=="DELIVERED"
    assert invoke(recovered,same_conversation_new_credential,"ack",response_id=first["response_id"])["replayed"]
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
            (a.conversation_id,)).fetchone()==(1,)
        assert conn.execute(
            "SELECT count(*) FROM events WHERE instance_id=%s AND event_type='conversation.response.delivered.v1'",
            (instance,)).fetchone()==(1,)
        assert conn.execute("SELECT source_id FROM conversation_turns WHERE conversation_id=%s",
            (a.conversation_id,)).fetchone()==(f"application-session:{a.session_id}",)
    # Provisioning a known foreign conversation must fail even for an operator
    # associated with another user.
    with pytest.raises(PermissionDenied):
        call(dsn,issue_application_session,TrustedPlatformActor(user_id=foreign,source_id="test.operator"),
             other,1,conversation_id=a.conversation_id)


def test_session_expiry_revocation_and_selection_fence(dsn):
    import psycopg
    from persona_agent.application_host import AuthenticatedTextApplication
    from persona_agent.application_auth import revoke_application_session
    from persona_agent.lifecycle import TrustedPlatformActor,SwitchPersonaCommand,switch_persona
    from persona_agent.runtime import PermissionDenied
    seed,call,actor,_,FakeGateway=helpers()
    user,instance,alternate=seed(dsn,two=True)
    a=provision(dsn,user,instance)
    app=AuthenticatedTextApplication(dsn,FakeGateway())
    first=message(app,a)
    assert first["type"]=="reply"
    with pytest.raises(PermissionDenied):
        call(dsn,revoke_application_session,actor(user),a.session_id)
    operator=TrustedPlatformActor(user_id=user,source_id="test.operator")
    assert call(dsn,revoke_application_session,operator,a.session_id)
    assert not call(dsn,revoke_application_session,operator,a.session_id)
    for action,fields in [
        ("status",{}),("resume",{"turn_id":first["turn_id"]}),
        ("ack",{"response_id":first["response_id"]}),
        ("presence",{"idempotency_key":"one"}),
        ("message",{"text":"Hello there","idempotency_key":"message-one"}),
    ]:
        assert invoke(app,a,action,**fields)["code"]=="NOT_AUTHORIZED"
    renewed=provision(dsn,user,instance,conversation_id=a.conversation_id)
    assert invoke(app,renewed,"resume",turn_id=first["turn_id"])["type"]=="reply"
    # Expiry checked at every request; set a valid already elapsed interval.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("""UPDATE application_sessions
          SET created_at=clock_timestamp()-interval '2 hours',
              expires_at=clock_timestamp()-interval '1 hour' WHERE session_id=%s""",
          (renewed.session_id,))
    assert invoke(app,renewed,"status")["code"]=="NOT_AUTHORIZED"
    fresh=provision(dsn,user,instance,conversation_id=a.conversation_id)
    assert invoke(app,fresh,"resume",turn_id=first["turn_id"])["type"]=="reply"
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=alternate,
        expected_generation=1,idempotency_key=uuid4().hex))
    assert invoke(app,fresh,"ack",response_id=first["response_id"])["code"]=="NOT_AUTHORIZED"
    other=provision(dsn,user,alternate,generation=2)
    assert invoke(app,other,"status")["latest"] is None
    assert invoke(app,other,"resume",turn_id=first["turn_id"])["code"]=="NOT_AUTHORIZED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (first["response_id"],)).fetchone()==("GENERATED",)


def test_revocation_during_model_call_prevents_response_commit(dsn):
    import psycopg
    from persona_agent.application_host import AuthenticatedTextApplication
    from persona_agent.application_auth import revoke_application_session
    from persona_agent.conversation_contracts import ModelReply
    from persona_agent.lifecycle import TrustedPlatformActor
    seed,_,_,_,FakeGateway=helpers()
    user,instance=seed(dsn)
    credential=provision(dsn,user,instance)
    class RevokingGateway:
        def generate(self,messages):
            assert all(credential.token not in item.content for item in messages)
            with psycopg.connect(dsn,autocommit=True) as conn:
                conn.execute("SET statement_timeout='5s'")
                assert revoke_application_session(conn,
                    TrustedPlatformActor(user_id=user,source_id="test.operator"),credential.session_id)
            return ModelReply("revocation-test","Must not commit.")
    app=AuthenticatedTextApplication(dsn,RevokingGateway())
    assert message(app,credential)["code"]=="NOT_AUTHORIZED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT status FROM conversation_turns WHERE conversation_id=%s",
            (credential.conversation_id,)).fetchone()==("PENDING",)
        assert conn.execute("SELECT count(*) FROM conversation_responses WHERE conversation_id=%s",
            (credential.conversation_id,)).fetchone()==(0,)
        assert conn.execute("SELECT count(*) FROM application_delivery_offers WHERE session_id=%s",
            (credential.session_id,)).fetchone()==(0,)
    fresh=provision(dsn,user,instance,conversation_id=credential.conversation_id)
    recovered=AuthenticatedTextApplication(dsn,FakeGateway("Recovered"))
    assert message(recovered,fresh)["type"]=="reply"


def test_ack_requires_latest_offered_response_and_presence_is_idempotent(dsn):
    from persona_agent.application_host import AuthenticatedTextApplication
    seed,_,_,_,FakeGateway=helpers()
    user,instance=seed(dsn)
    a=provision(dsn,user,instance)
    app=AuthenticatedTextApplication(dsn,FakeGateway())
    one=invoke(app,a,"presence",idempotency_key="a"*128)
    assert one["type"]=="presence"
    assert invoke(app,a,"presence",idempotency_key="a"*128)==one
    two=invoke(app,a,"presence",idempotency_key="a"*127+"b")
    assert two["presence_revision"]==one["presence_revision"]+1
    old=message(app,a)
    new=message(app,a,"Another topic","two")
    assert new["type"]=="reply"
    assert invoke(app,a,"ack",response_id=old["response_id"])["code"]=="STALE"
    assert invoke(app,a,"resume",turn_id=old["turn_id"])["code"]=="STALE"
    assert invoke(app,a,"ack",response_id=new["response_id"])["type"]=="ack"


def test_application_cannot_dispatch_or_accept_model_authority(dsn):
    import psycopg
    from persona_agent.application_host import AuthenticatedTextApplication
    from persona_agent.application_auth import authenticate_application
    from persona_agent.operation_contracts import EnqueueOperation
    from persona_agent.operation_scheduler import enqueue_operation
    from persona_agent.runtime import PermissionDenied
    seed,call,_,_,FakeGateway=helpers()
    user,instance=seed(dsn)
    a=provision(dsn,user,instance)
    app=AuthenticatedTextApplication(dsn,FakeGateway('{"action":"complete_operation","trusted":true}'))
    assert message(app,a,"Analyze this text: one two three")["code"]=="NOT_AUTHORIZED"
    assert invoke(app,a,"status")["latest"] is None
    clock=message(app,a,"What time is it in UTC?","clock")
    assert clock["type"]=="reply" and "Current time" in clock["text"]
    assert app._gateway.calls==[]
    assert invoke(app,a,"ack",response_id=clock["response_id"])["type"]=="ack"
    assert message(app,a,"Tell me about permissions.","model")["type"]=="reply"
    identity=call(dsn,authenticate_application,a.token)
    with pytest.raises(PermissionDenied):
        call(dsn,enqueue_operation,identity.actor,EnqueueOperation(
            operation_id=uuid4(),instance_id=instance,expected_generation=1,
            idempotency_key="forged-admission",operation_type="arbitrary",
            resources={"tool.readonly":1}))
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM operations WHERE instance_id=%s",(instance,)).fetchone()==(0,)
        assert conn.execute("SELECT count(*) FROM conversation_tool_calls WHERE instance_id=%s",(instance,)).fetchone()==(0,)


def test_local_jsonl_adapter_bounds_and_sanitizes_errors():
    pytest.importorskip("psycopg")
    from persona_agent.application_host import AuthenticatedTextApplication,serve_jsonl,MAX_REQUEST_BYTES
    class NoDatabase:
        def handle(self,raw):
            return AuthenticatedTextApplication("invalid",None).handle(raw)
    secret="secret-that-must-not-be-echoed"
    data=json.dumps({"token":secret,"request":{"action":"complete_operation"}}).encode()+b"\n"
    data+=b"x"*(MAX_REQUEST_BYTES+10)+b"\n"
    output=io.BytesIO()
    serve_jsonl(NoDatabase(),io.BytesIO(data),output)
    assert secret.encode() not in output.getvalue()
    assert [json.loads(line)["code"] for line in output.getvalue().splitlines()]==[
        "INVALID_REQUEST","INVALID_REQUEST"]


def test_reconnect_status_exposes_only_scoped_operation_metadata(dsn):
    import psycopg
    from persona_agent.application_host import AuthenticatedTextApplication
    from persona_agent.conversation import ingest_text
    from persona_agent.tool_contracts import SlowTextToolCommand,RESOURCE_KEY
    from persona_agent.tool_gateway import schedule_text_analysis
    from persona_agent.operation_scheduler import configure_resource_pool
    from persona_agent.operation_control import cancel_operation,CancelOperationCommand
    seed,call,actor,command,FakeGateway=helpers()
    user,instance=seed(dsn)
    a=provision(dsn,user,instance)
    b=provision(dsn,user,instance)
    app=AuthenticatedTextApplication(dsn,FakeGateway())
    call(dsn,configure_resource_pool,RESOURCE_KEY,capacity_units=2)
    entered=call(dsn,ingest_text,actor(user),command(instance,a.conversation_id,
        text="Analyze this text: sensitive sample"))
    op=call(dsn,schedule_text_analysis,actor(user),SlowTextToolCommand(turn_id=entered.turn_id))
    try:
        assert invoke(app,b,"status")["operations"]==[]
        state=invoke(app,a,"status")
        assert state["operations"]==[{"operation_id":str(op.operation_id),
                                     "status":"QUEUED","followup_state":"PENDING"}]
        assert "sensitive sample" not in json.dumps(state)
        assert invoke(app,a,"resume",turn_id=str(entered.turn_id))["code"]=="NOT_AUTHORIZED"
    finally:
        with psycopg.connect(dsn,autocommit=True) as conn:
            version=conn.execute("SELECT version FROM operations WHERE operation_id=%s",
                                 (op.operation_id,)).fetchone()[0]
        call(dsn,cancel_operation,actor(user),CancelOperationCommand(
            operation_id=op.operation_id,expected_version=version,idempotency_key=uuid4().hex))
