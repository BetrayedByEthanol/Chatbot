"""MVP-05D.4: authorized versioned reference text and scoped exact passages."""
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest

from persona_agent.knowledge_contracts import (
    SaveKnowledgeCommand,WithdrawKnowledgeCommand,ProcedureKnowledgeReference,
    knowledge_approval_phrase,knowledge_reference,split_passages,content_digest,
)
from persona_agent.response_decisions import decide_response


CONTENT="Certificate configuration: use the local test certificate for TLS.\nNever assume a self-signed certificate is publicly trusted."
UPDATED="Certificate configuration: use the new test certificate for TLS."
QUESTION="What does the saved documentation say about certificate?"


@pytest.mark.parametrize("source",[
    CONTENT,"αβγ "*400,"庭"*1301,"\n\n"+"a"*600+" "*601+"b"*600+"\n",
])
def test_passages_preserve_exact_unicode_offsets_and_content(source):
    parts=split_passages(source)
    assert all(source[a:b]==s and 1<=len(s)<=600 for a,b,s in parts)
    assert all(b<=c for (_,b,_),(c,_,_) in zip(parts,parts[1:]))
    assert "".join(c for _,_,s in parts for c in s if not c.isspace())==(
        "".join(c for c in source if not c.isspace())
    )


@pytest.mark.parametrize("text,topic",[
    (QUESTION,"certificate"),
    ("What do my saved manuals say about TLS?","TLS"),
    ("What does saved knowledge say about Nginx?", "Nginx"),
    ("What does documentation say about TLS?",None),
    ("Run my saved procedure.",None),
    ('"What does the saved documentation say about TLS?"',None),
    ("What does the saved manual say about TLS? Then run it.",None),
])
def test_document_recall_is_explicit_and_read_only(text,topic):
    assert knowledge_reference(text)==topic
    assert decide_response(text).route==("KNOWLEDGE_RECALL" if topic else "MODEL")


def _helpers():
    pytest.importorskip("psycopg")
    from test_phase2_memory import actor,call,seed,turn
    return actor,call,seed,turn


def _approve(dsn,user,instance,command):
    _,_,_,turn=_helpers()
    approval=turn(dsn,user,instance,command.conversation_id,knowledge_approval_phrase(command))
    return command.model_copy(update={"approval_turn_id":approval.turn_id})


def _save_command(dsn,user,instance,*,document_id=None,expected=0,content=CONTENT,
                  privacy="NORMAL",expiry=None,refs=(),locator="manual.txt"):
    _,_,_,turn=_helpers()
    conv=uuid4()
    text="Save this supplied reference privately."
    source=turn(dsn,user,instance,conv,text)
    return _approve(dsn,user,instance,SaveKnowledgeCommand(
        instance_id=instance,expected_generation=1,conversation_id=conv,
        evidence_turn_id=source.turn_id,evidence_quote=text,idempotency_key=str(uuid4()),
        document_id=document_id or uuid4(),expected_version=expected,
        title="Local TLS manual",source_kind="MANUAL",source_locator=locator,
        source_revision="2026-11",content=content,privacy_class=privacy,
        expires_at=expiry,procedure_refs=refs,
    ))


def _withdraw_command(dsn,user,instance,document_id,version):
    _,_,_,turn=_helpers()
    conv=uuid4()
    text="Withdraw this saved reference."
    source=turn(dsn,user,instance,conv,text)
    return _approve(dsn,user,instance,WithdrawKnowledgeCommand(
        instance_id=instance,expected_generation=1,conversation_id=conv,
        evidence_turn_id=source.turn_id,evidence_quote=text,idempotency_key=str(uuid4()),
        document_id=document_id,expected_version=version,
    ))


class NoModel:
    def generate(self,messages):
        raise AssertionError("direct document recall must not run inference")


class Guessing:
    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        assert all(CONTENT not in m.content for m in messages)
        return ModelReply(model_id="guess",text="Invented answer about certificates.")


def test_private_document_approval_provenance_replay_and_canonical_recall(dsn):
    import psycopg
    from persona_agent.knowledge_memory import save_knowledge,search_knowledge,UNKNOWN
    from persona_agent.conversation import respond_to_text,prepare_text_context,acknowledge_text_delivery
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.runtime import PermissionDenied,IdempotencyConflict
    actor,call,seed,turn=_helpers()
    user,instance,other=seed(dsn,alternative=True)
    stranger,foreign,_=seed(dsn)
    cmd=_save_command(dsn,user,instance)
    with pytest.raises(PermissionDenied,match="exact action"):
        call(dsn,save_knowledge,actor(user),cmd.model_copy(update={"content":UPDATED}))
    with pytest.raises(PermissionDenied):
        call(dsn,save_knowledge,actor(stranger),cmd)
    saved=call(dsn,save_knowledge,actor(user),cmd)
    assert saved.version==1 and not saved.replayed
    assert call(dsn,save_knowledge,actor(user),cmd).replayed
    with pytest.raises(IdempotencyConflict):
        call(dsn,save_knowledge,actor(user),cmd.model_copy(update={"content":UPDATED}))
    hit,=call(dsn,search_knowledge,actor(user),instance,"certificate")
    assert hit.document_id==saved.document_id and hit.version==1
    assert hit.passage_text==CONTENT[hit.start_offset:hit.end_offset]
    assert hit.source_revision=="2026-11" and hit.source_locator=="manual.txt"
    assert hit.content_sha256==content_digest(CONTENT)
    assert hit.epistemic_status=="SUPPLIED_SOURCE" and hit.approval_turn_id==cmd.approval_turn_id
    assert call(dsn,search_knowledge,actor(user),instance,"bicycle")==()
    assert call(dsn,search_knowledge,actor(user),other,"certificate")==()
    assert call(dsn,search_knowledge,actor(stranger),foreign,"certificate")==()
    with pytest.raises(PermissionDenied):
        call(dsn,search_knowledge,actor(stranger),instance,"certificate")
    assert call(dsn,search_knowledge,actor(user),instance,"certificate",max_chars=256)==()
    assert call(dsn,search_knowledge,actor(user),instance,"certificate",document_id=uuid4())==()

    conv=uuid4()
    request=turn(dsn,user,instance,conv,QUESTION)
    result=call(dsn,respond_to_user_text,actor(user),request.turn_id,NoModel())
    assert CONTENT in result.text and "manual.txt" in result.text
    assert "not independently verified" in result.text
    assert call(dsn,respond_to_user_text,actor(user),request.turn_id,NoModel()).replayed
    call(dsn,acknowledge_text_delivery,actor(user),result.response_id)
    ordinary=turn(dsn,user,instance,conv,"Tell me a joke.")
    ctx=call(dsn,prepare_text_context,actor(user),ordinary.turn_id)
    assert all(CONTENT not in m.content for m in ctx.messages)
    assert any("Saved document excerpts omitted" in m.content for m in ctx.messages)

    forced=turn(dsn,user,instance,uuid4(),QUESTION)
    assert call(dsn,respond_to_text,actor(user),forced.turn_id,Guessing()).text==result.text
    unknown=turn(dsn,user,instance,uuid4(),"What does the saved manual say about bicycle?")
    assert call(dsn,respond_to_text,actor(user),unknown.turn_id,Guessing()).text==UNKNOWN
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT knowledge_excerpt FROM conversation_responses WHERE response_id=%s",
            (result.response_id,),
        ).fetchone()==(True,)
        assert conn.execute(
            """SELECT payload->>'status',payload->'passages' FROM events
               WHERE event_type='conversation.response.knowledge_grounded.v1'
                 AND payload->>'response_id'=%s""",(str(result.response_id),),
        ).fetchone()==("KNOWN",[{"document_id":str(saved.document_id),"version":1,
                                 "position":hit.position,"start":hit.start_offset,"end":hit.end_offset}])
        assert conn.execute(
            """SELECT count(*) FROM events e JOIN outbox o USING(event_id)
               WHERE e.event_id=%s AND e.event_type='memory.knowledge.saved.v1'""",
            (saved.event_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            "SELECT count(*) FROM memory_records WHERE instance_id=%s",(instance,),
        ).fetchone()[0]==0
        assert conn.execute(
            "SELECT count(*) FROM personalized_procedures WHERE instance_id=%s",(instance,),
        ).fetchone()[0]==0


def test_update_and_withdraw_invalidate_stale_recall_and_retain_source(dsn):
    import psycopg
    from persona_agent.knowledge_memory import save_knowledge,withdraw_knowledge,search_knowledge,UNKNOWN
    from persona_agent.conversation import prepare_text_context,commit_text_response,respond_to_text
    from persona_agent.conversation_contracts import ModelReply,StaleHistoricalContext
    from persona_agent.runtime import VersionConflict
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    original=_save_command(dsn,user,instance)
    saved=call(dsn,save_knowledge,actor(user),original)
    request=turn(dsn,user,instance,uuid4(),QUESTION)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    changed=_save_command(dsn,user,instance,document_id=saved.document_id,expected=1,content=UPDATED)
    second=call(dsn,save_knowledge,actor(user),changed)
    assert second.version==2
    with pytest.raises(StaleHistoricalContext):
        call(dsn,commit_text_response,actor(user),ctx,ModelReply("stale","old"),model_generated=True)
    assert UPDATED in call(dsn,respond_to_text,actor(user),request.turn_id,Guessing()).text
    hit,=call(dsn,search_knowledge,actor(user),instance,"certificate")
    assert hit.version==2 and hit.passage_text==UPDATED
    stale=_save_command(dsn,user,instance,document_id=saved.document_id,expected=1)
    with pytest.raises(VersionConflict):
        call(dsn,save_knowledge,actor(user),stale)
    withdrawn=_withdraw_command(dsn,user,instance,saved.document_id,2)
    removed=call(dsn,withdraw_knowledge,actor(user),withdrawn)
    assert removed.status=="WITHDRAWN"
    assert call(dsn,withdraw_knowledge,actor(user),withdrawn).replayed
    assert call(dsn,search_knowledge,actor(user),instance,"certificate")==()
    request=turn(dsn,user,instance,uuid4(),QUESTION)
    assert call(dsn,respond_to_text,actor(user),request.turn_id,Guessing()).text==UNKNOWN
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT version,content FROM memory_knowledge_versions
               WHERE document_id=%s ORDER BY version""",(saved.document_id,),
        ).fetchall()==[(1,CONTENT),(2,UPDATED)]


@pytest.mark.parametrize("mode",["RESTRICTED","EXPIRED","CORRUPT_PASSAGE"])
def test_knowledge_eligibility_and_exact_source_verification(dsn,mode):
    import psycopg
    from persona_agent.knowledge_memory import save_knowledge,search_knowledge,UNKNOWN
    from persona_agent.response_orchestrator import respond_to_user_text
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    cmd=_save_command(dsn,user,instance,privacy="RESTRICTED" if mode=="RESTRICTED" else "NORMAL",
                     expiry=datetime.now(timezone.utc)+timedelta(hours=1) if mode=="EXPIRED" else None)
    saved=call(dsn,save_knowledge,actor(user),cmd)
    with psycopg.connect(dsn,autocommit=True) as conn:
        if mode=="EXPIRED":
            conn.execute("UPDATE memory_knowledge_documents SET expires_at=clock_timestamp() WHERE document_id=%s",
                         (saved.document_id,))
        elif mode=="CORRUPT_PASSAGE":
            conn.execute(
                """UPDATE memory_knowledge_passages SET passage_text=
                   replace(passage_text,'local','bogus') WHERE document_id=%s""",
                (saved.document_id,),
            )
    assert call(dsn,search_knowledge,actor(user),instance,"certificate")==()
    question=turn(dsn,user,instance,uuid4(),QUESTION)
    assert call(dsn,respond_to_user_text,actor(user),question.turn_id,NoModel()).text==UNKNOWN


def test_procedure_references_pin_private_revision_and_never_modify_procedure(dsn):
    from persona_agent.knowledge_memory import save_knowledge,search_knowledge
    from persona_agent.procedure_state import (
        publish_shared_procedure,create_personalized_approach,
        revise_personalized_approach,read_personalized_approach,
    )
    from persona_agent.runtime import PermissionDenied,VersionConflict
    from test_phase3_procedures import seed as proc_seed,publication,definition,variant_command,revise_command
    actor,call,_,_=_helpers()
    user,persona,instance=proc_seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),
         publication(dsn,user,persona,instance,conv,template,0,definition("PARAMETERIZED")))
    command=variant_command(dsn,user,instance,conv,template,1,
                            params={"hostname":"localhost","days":10})
    variant=call(dsn,create_personalized_approach,actor(user),command)
    before=call(dsn,read_personalized_approach,actor(user),instance,variant.entity_id)
    ref=ProcedureKnowledgeReference(variant_id=variant.entity_id,revision=1)
    saved=call(dsn,save_knowledge,actor(user),_save_command(dsn,user,instance,refs=(ref,)))
    hit,=call(dsn,search_knowledge,actor(user),instance,"certificate",variant_id=variant.entity_id)
    assert hit.document_id==saved.document_id
    assert call(dsn,read_personalized_approach,actor(user),instance,variant.entity_id)==before
    stranger,_,foreign=proc_seed(dsn,persona_id=persona)
    with pytest.raises(PermissionDenied):
        call(dsn,save_knowledge,actor(stranger),_save_command(dsn,stranger,foreign,refs=(ref,)))
    assert call(dsn,search_knowledge,actor(stranger),foreign,"certificate",variant_id=variant.entity_id)==()
    call(dsn,revise_personalized_approach,actor(user),
         revise_command(dsn,user,instance,conv,variant.entity_id,1,1,
                        params={"hostname":"localhost","days":20}))
    assert call(dsn,search_knowledge,actor(user),instance,"certificate",variant_id=variant.entity_id)==()
    assert call(dsn,search_knowledge,actor(user),instance,"certificate")
    with pytest.raises(VersionConflict):
        call(dsn,save_knowledge,actor(user),_save_command(dsn,user,instance,refs=(ref,)))
    current=ProcedureKnowledgeReference(variant_id=variant.entity_id,revision=2)
    refreshed=call(dsn,save_knowledge,actor(user),_save_command(
        dsn,user,instance,document_id=saved.document_id,expected=1,refs=(current,),
    ))
    assert refreshed.version==2
    hit,=call(dsn,search_knowledge,actor(user),instance,"certificate",variant_id=variant.entity_id)
    assert hit.version==2


def test_document_excerpts_do_not_enter_summary_or_cognitive_evidence(dsn):
    from persona_agent.knowledge_memory import save_knowledge
    from persona_agent.conversation import acknowledge_text_delivery
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.summary_memory import prepare_summary
    from persona_agent.cognitive_engine import schedule_cognition,prepare_cognition
    from test_phase2_summaries import schedule as schedule_summary,claim as claim_summary
    from test_phase2_cognition import platform,claim as claim_cognition
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    call(dsn,save_knowledge,actor(user),_save_command(dsn,user,instance))
    conv=uuid4()
    question=turn(dsn,user,instance,conv,QUESTION)
    answer=call(dsn,respond_to_user_text,actor(user),question.turn_id,NoModel())
    call(dsn,acknowledge_text_delivery,actor(user),answer.response_id)
    for i in range(3):
        turn(dsn,user,instance,conv,f"Another discussion {i}.")
    summary=schedule_summary(dsn,user,instance,conv,max_turns=1)
    lease=claim_summary(dsn)
    assert lease.operation_id==summary.operation_id
    snapshot=call(dsn,prepare_summary,lease)
    assert snapshot.turns[0]["response_text"]=="[Saved document excerpts omitted; request fresh recall.]"
    planned=call(dsn,schedule_cognition,platform(user),conv,instance,1)
    lease=claim_cognition(dsn)
    assert planned.operation_id==lease.operation_id
    cognitive=call(dsn,prepare_cognition,lease)
    assert cognitive.turns[0]["response_text"]=="[Saved document excerpts omitted; request fresh recall.]"
