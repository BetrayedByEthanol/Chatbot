"""MVP-07B2: durable response evidence fences, not physical erasure."""
from datetime import timedelta
from uuid import uuid4

import pytest


def setup(dsn,*,expiry=None):
    pytest.importorskip("psycopg")
    from test_phase1_conversation import seed,call,actor
    from test_mvp05d4_document_knowledge import _save_command
    from persona_agent.knowledge_memory import save_knowledge
    user,instance=seed(dsn)
    cmd=_save_command(dsn,user,instance,expiry=expiry)
    saved=call(dsn,save_knowledge,actor(user),cmd)
    return user,instance,cmd,saved


def revoke(dsn,user,instance,doc):
    from test_phase1_conversation import call,actor
    from test_mvp05d4_document_knowledge import _withdraw_command
    from persona_agent.knowledge_memory import withdraw_knowledge
    return call(dsn,withdraw_knowledge,actor(user),
        _withdraw_command(dsn,user,instance,doc.document_id,doc.version))


def local_recall(dsn,user,instance,conversation=None):
    from test_phase1_conversation import call,actor,command
    from test_mvp05d4_document_knowledge import QUESTION,NoModel
    from persona_agent.conversation import ingest_text
    from persona_agent.response_orchestrator import respond_to_user_text
    turn=call(dsn,ingest_text,actor(user),command(instance,conversation,text=QUESTION))
    reply=call(dsn,respond_to_user_text,actor(user),turn.turn_id,NoModel())
    return turn,reply


def lineage(dsn,reply_id):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT l.*,response_lineage_current(l.response_id,l.user_id,l.instance_id) AS current
               FROM conversation_response_lineage l WHERE response_id=%s""",
            (reply_id,),
        ).fetchone()


def test_source_withdrawal_invalidates_generated_and_delivered_replay(dsn):
    import psycopg
    from test_phase1_conversation import actor,call,FakeGateway
    from test_mvp07a_application import provision,invoke,message
    from test_mvp05d4_document_knowledge import QUESTION,CONTENT,NoModel
    from persona_agent.application_host import AuthenticatedTextApplication
    from persona_agent.conversation import lookup_response,acknowledge_text_delivery,respond_to_text
    from persona_agent.conversation_contracts import InvalidatedResponse
    user,instance,_,doc=setup(dsn)
    a=provision(dsn,user,instance)
    b=provision(dsn,user,instance)
    ordinary=provision(dsn,user,instance)
    app=AuthenticatedTextApplication(dsn,FakeGateway("Hello back."))
    delivered=message(app,a,QUESTION,"recall")
    generated=message(app,b,QUESTION,"recall")
    plain=message(app,ordinary,"Hello there","plain")
    assert delivered["type"]==generated["type"]=="reply"
    assert CONTENT in generated["text"]
    assert plain["type"]=="reply" and lineage(dsn,plain["response_id"]) is None
    assert invoke(app,a,"ack",response_id=delivered["response_id"])["type"]=="ack"
    assert invoke(app,b,"status")["latest"]["source_status"]=="CURRENT"
    row=lineage(dsn,generated["response_id"])
    assert row["historical_revision"]>=0 and row["current"] and not row["legacy_unbound"]
    with psycopg.connect(dsn,autocommit=True) as conn:
        refs=conn.execute(
            "SELECT evidence_refs FROM memory_recall_observations WHERE response_id=%s",
            (generated["response_id"],)).fetchone()[0]
        assert refs[0]["id"]==str(doc.document_id) and refs[0]["version"]==1
    revoke(dsn,user,instance,doc)
    # Fresh host and independently authenticated credentials still cannot
    # recover stale text or turn a stale offer into a delivered question.
    recovered=AuthenticatedTextApplication(dsn,NoModel())
    for credential,reply in ((a,delivered),(b,generated)):
        assert invoke(recovered,credential,"status")["latest"]["source_status"]=="INVALIDATED"
        assert invoke(recovered,credential,"resume",turn_id=reply["turn_id"])=={
            "type":"error","code":"STALE_EVIDENCE"}
        assert invoke(recovered,credential,"ack",response_id=reply["response_id"])=={
            "type":"error","code":"STALE_EVIDENCE"}
        assert message(recovered,credential,QUESTION,"recall")["code"]=="STALE_EVIDENCE"
        assert not lineage(dsn,reply["response_id"])["current"]
        with pytest.raises(InvalidatedResponse):
            call(dsn,lookup_response,actor(user),reply["turn_id"])
        with pytest.raises(InvalidatedResponse):
            call(dsn,respond_to_text,actor(user),reply["turn_id"],NoModel())
        with pytest.raises(InvalidatedResponse):
            call(dsn,acknowledge_text_delivery,actor(user),reply["response_id"])
    # The untracked ordinary reply is not indiscriminately invalidated.
    assert invoke(recovered,ordinary,"ack",response_id=plain["response_id"])["type"]=="ack"
    assert invoke(recovered,ordinary,"status")["latest"]["source_status"]=="UNTRACKED"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (delivered["response_id"],)).fetchone()==("DELIVERED",)
        status,body=conn.execute(
            "SELECT delivery_status,response_text FROM conversation_responses WHERE response_id=%s",
            (generated["response_id"],)).fetchone()
        assert status=="GENERATED" and CONTENT in body  # retained, not erased
        assert conn.execute(
            """SELECT count(*) FROM events WHERE event_type='conversation.response.delivered.v1'
               AND payload->>'response_id'=%s""",(generated["response_id"],)).fetchone()==(0,)
    fresh=message(recovered,b,QUESTION,"fresh-recall")
    assert fresh["type"]=="reply" and CONTENT not in fresh["text"]


def test_withdrawal_between_commit_and_application_offer(dsn,monkeypatch):
    from test_mvp07a_application import provision,message
    from test_mvp05d4_document_knowledge import QUESTION,NoModel
    from persona_agent import application_host as host
    import psycopg
    user,instance,_,doc=setup(dsn)
    cred=provision(dsn,user,instance)
    original=host.respond_to_user_text
    def changed(*args,**kwargs):
        reply=original(*args,**kwargs)
        revoke(dsn,user,instance,doc)
        return reply
    monkeypatch.setattr(host,"respond_to_user_text",changed)
    app=host.AuthenticatedTextApplication(dsn,NoModel())
    assert message(app,cred,QUESTION,"recall")=={"type":"error","code":"STALE_EVIDENCE"}
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM application_delivery_offers WHERE session_id=%s",
            (cred.session_id,)).fetchone()==(0,)
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE conversation_id=%s",
            (cred.conversation_id,)).fetchone()==("GENERATED",)


def test_time_only_expiry_invalidates_without_revision_write(dsn):
    import psycopg
    from test_phase1_conversation import call,actor
    from persona_agent.conversation import acknowledge_text_delivery
    from persona_agent.conversation_contracts import InvalidatedResponse
    with psycopg.connect(dsn,autocommit=True) as conn:
        now=conn.execute("SELECT clock_timestamp()").fetchone()[0]
    user,instance,_,doc=setup(dsn,expiry=now+timedelta(seconds=5))
    _,reply=local_recall(dsn,user,instance)
    before=lineage(dsn,reply.response_id)
    assert before["current"] and before["next_expiry"] is not None
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (%s::timestamptz-clock_timestamp())))+0.05)",
                     (before["next_expiry"],))
        assert conn.execute(
            "SELECT revision FROM historical_recall_revisions WHERE user_id=%s AND instance_id=%s",
            (user,instance)).fetchone()==(before["historical_revision"],)
    assert not lineage(dsn,reply.response_id)["current"]
    with pytest.raises(InvalidatedResponse):
        call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)


def test_fact_correction_preserves_source_attribution_and_invalidates_cached_answer(dsn):
    from test_phase2_memory import seed,turn,call,actor,candidate,correction
    from persona_agent.memory import admit_memory,amend_memory
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation import acknowledge_text_delivery,lookup_response,prepare_text_context
    from persona_agent.conversation_contracts import InvalidatedResponse
    from test_mvp05d4_document_knowledge import NoModel
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"I drive a Toyota Camry.")
    saved=call(dsn,admit_memory,actor(user),candidate(instance,conv,source,
        kind="FACT",predicate="car",value="Toyota Camry",quote="I drive a Toyota Camry."))
    question_conv=uuid4()
    q=turn(dsn,user,instance,question_conv,"What car do I drive?")
    old=call(dsn,respond_to_user_text,actor(user),q.turn_id,NoModel())
    assert "Toyota Camry" in old.text and "last saved report" in old.text
    call(dsn,acknowledge_text_delivery,actor(user),old.response_id)
    assert lineage(dsn,old.response_id)["current"]
    newer=turn(dsn,user,instance,conv,"Actually, I now drive a Honda Civic.")
    call(dsn,amend_memory,actor(user),correction(instance,conv,newer,saved.memory_id,
        value="Honda Civic",quote="Actually, I now drive a Honda Civic."))
    with pytest.raises(InvalidatedResponse):
        call(dsn,lookup_response,actor(user),q.turn_id)
    fresh=turn(dsn,user,instance,question_conv,"Tell me a joke.")
    context=call(dsn,prepare_text_context,actor(user),fresh.turn_id)
    assert all("Toyota Camry" not in m.content for m in context.messages if m.role=="assistant")
    assert any("Evidence-bearing assistant text omitted" in m.content for m in context.messages)
    q2=turn(dsn,user,instance,uuid4(),"What car do I drive?")
    new=call(dsn,respond_to_user_text,actor(user),q2.turn_id,NoModel())
    assert "Honda Civic" in new.text and "Toyota" not in new.text
    assert lineage(dsn,new.response_id)["current"]


def test_relationship_preference_exact_revision_guard(dsn):
    from test_phase1_conversation import seed,call,actor,command
    from persona_agent.affect_contracts import SetRelationshipPreference
    from persona_agent.relationship_state import set_relationship_preference
    from persona_agent.conversation import ingest_text,lookup_response,acknowledge_text_delivery
    from persona_agent.conversation_contracts import InvalidatedResponse
    from persona_agent.response_orchestrator import respond_to_user_text
    from test_mvp05d4_document_knowledge import NoModel
    user,instance=seed(dsn)
    conv=uuid4()
    def set_value(key,value,version,text):
        source=call(dsn,ingest_text,actor(user),command(instance,conv,text=text))
        return call(dsn,set_relationship_preference,actor(user),SetRelationshipPreference(
            instance_id=instance,expected_generation=1,expected_version=version,
            idempotency_key=uuid4().hex,conversation_id=conv,evidence_turn_id=source.turn_id,
            evidence_quote=text,preference_key=key,preference_value=value))
    set_value("RESPONSE_LENGTH","SHORT",0,"Please keep replies short.")
    question=call(dsn,ingest_text,actor(user),command(instance,text="What is my current response length preference?"))
    old=call(dsn,respond_to_user_text,actor(user),question.turn_id,NoModel())
    row=lineage(dsn,old.response_id)
    assert row["preference_keys"]==["RESPONSE_LENGTH"]
    assert row["preference_revisions"]==[["RESPONSE_LENGTH","ACTIVE",1]]
    assert row["historical_revision"] is None
    set_value("EMOJI_USE","OFF",0,"Please do not use emojis.")
    assert lineage(dsn,old.response_id)["current"]  # unrelated key
    assert call(dsn,lookup_response,actor(user),question.turn_id).text==old.text
    set_value("RESPONSE_LENGTH","DETAILED",1,"Please use detailed replies.")
    assert not lineage(dsn,old.response_id)["current"]
    with pytest.raises(InvalidatedResponse):
        call(dsn,acknowledge_text_delivery,actor(user),old.response_id)


def test_worker_range_omits_guarded_assistant_and_does_not_wait_on_invalidated_reply(dsn):
    import psycopg
    from psycopg.rows import dict_row
    from test_phase1_conversation import call,actor,command
    from persona_agent.conversation import acknowledge_text_delivery,ingest_text
    from persona_agent.summary_memory import _read_range,_model_source,_digest as summary_digest
    from persona_agent.cognitive_engine import _range,_digest as cognition_digest
    from persona_agent.summary_contracts import SummaryNotReady
    from persona_agent.cognitive_contracts import CognitiveNotReady
    from test_mvp05d4_document_knowledge import CONTENT
    user,instance,_,doc=setup(dsn)
    conv=uuid4()
    _,first=local_recall(dsn,user,instance,conv)
    call(dsn,acknowledge_text_delivery,actor(user),first.response_id)
    _,second=local_recall(dsn,user,instance,conv)
    assert CONTENT in first.text and CONTENT in second.text
    job=dict(user_id=user,instance_id=instance,conversation_id=conv,
             start_sequence=1,end_sequence=1,expected_generation=1)
    def ranges(job):
        with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                return _read_range(cur,job),_range(cur,job)
    summaries,cognition=ranges(job)
    assert CONTENT not in _model_source(summaries)
    assert "Saved document excerpts omitted" in cognition[0]["response_text"]
    before=(summary_digest(summaries),cognition_digest(cognition))
    with pytest.raises(SummaryNotReady):
        ranges(dict(job,end_sequence=2))
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            with pytest.raises(CognitiveNotReady):
                _range(cur,dict(job,end_sequence=2))
    revoke(dsn,user,instance,doc)
    after=ranges(job)
    assert (summary_digest(after[0]),cognition_digest(after[1]))==before
    summaries,cognition=ranges(dict(job,end_sequence=2))
    assert summaries[1]["delivery_status"]==cognition[1]["delivery_status"]=="INVALIDATED"
    assert CONTENT not in _model_source(summaries)
    assert all(CONTENT not in row["response_text"] for row in cognition)


def test_speech_source_revocation_denies_new_clips_and_late_delivery(dsn):
    from test_phase1_conversation import call,actor
    from test_phase5_speech_output import clip_command,ack
    from persona_agent.speech_output_state import prepare_speech_clip,acknowledge_speech_clip
    from persona_agent.conversation_contracts import InvalidatedResponse
    import psycopg
    user,instance,_,doc=setup(dsn)
    _,reply=local_recall(dsn,user,instance)
    first=call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    for status in ("QUEUED","STARTED"):
        call(dsn,acknowledge_speech_clip,actor(user),ack(first,status))
    revoke(dsn,user,instance,doc)
    with pytest.raises(InvalidatedResponse):
        call(dsn,prepare_speech_clip,actor(user),clip_command(reply.response_id))
    with pytest.raises(InvalidatedResponse):
        call(dsn,acknowledge_speech_clip,actor(user),ack(first,"STARTED"))
    result=call(dsn,acknowledge_speech_clip,actor(user),ack(first,"PLAYED"))
    assert result.status=="PLAYED" and not result.response_delivered
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,)).fetchone()==("GENERATED",)


def test_foreign_revision_change_does_not_invalidate_scoped_reply(dsn):
    from test_phase1_conversation import call,actor
    from persona_agent.conversation import lookup_response
    first,instance,_,_=setup(dsn)
    second,other,_,doc=setup(dsn)
    turn,reply=local_recall(dsn,first,instance)
    revoke(dsn,second,other,doc)
    assert lineage(dsn,reply.response_id)["current"]
    assert call(dsn,lookup_response,actor(first),turn.turn_id).text==reply.text
    assert call(dsn,lookup_response,actor(second),turn.turn_id) is None


def test_legacy_unbound_output_fails_closed_without_rewriting_history(dsn):
    import psycopg
    from test_phase1_conversation import call,actor,seed,command,FakeGateway
    from persona_agent.conversation import ingest_text,respond_to_text,lookup_response,acknowledge_text_delivery
    from persona_agent.conversation_contracts import InvalidatedResponse
    user,instance=seed(dsn)
    turn=call(dsn,ingest_text,actor(user),command(instance))
    reply=call(dsn,respond_to_text,actor(user),turn.turn_id,FakeGateway("Legacy response"))
    # Emulate exactly the 0057 backfill row, without inferring an old revision.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """INSERT INTO conversation_response_lineage(response_id,user_id,instance_id,legacy_unbound)
               VALUES (%s,%s,%s,true)""",(reply.response_id,user,instance))
    assert not lineage(dsn,reply.response_id)["current"]
    with pytest.raises(InvalidatedResponse,match="no recorded source fence"):
        call(dsn,lookup_response,actor(user),turn.turn_id)
    with pytest.raises(InvalidatedResponse):
        call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT response_text,delivery_status FROM conversation_responses WHERE response_id=%s",
            (reply.response_id,)).fetchone()==("Legacy response","GENERATED")
