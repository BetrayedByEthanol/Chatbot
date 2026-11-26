"""MVP-05D.5: owner-observed gaps and user-reported retrieval quality."""
from decimal import Decimal
from uuid import uuid4

import pytest

from persona_agent.metamemory_contracts import (
    recall_key,recall_outcome,candidate_key,RetrievalFeedbackCommand,feedback_phrase,
)


@pytest.mark.parametrize("status,expected",[
    ("KNOWN","AVAILABLE"),("UNKNOWN","UNKNOWN"),("AMBIGUOUS","AMBIGUOUS"),
    ("DISPUTED","CONTRADICTORY"),("UNSUPPORTED_SOURCE","UNVERIFIABLE"),
    ("STALE","STALE"),("NO_EVIDENCE","NO_EVIDENCE"),("BUDGET","BUDGET"),
])
def test_owner_status_never_means_verified_truth(status,expected):
    assert recall_outcome(status)==expected


def test_exact_topic_and_candidate_keys():
    assert recall_key("KNOWLEDGE"," TLS   Certificate ")==recall_key("KNOWLEDGE","tls certificate")
    assert recall_key("KNOWLEDGE","car")!=recall_key("FACT","car")
    assert recall_key("ENTITY","PERSON/ANY/alex")!=recall_key("ENTITY","PERSON/FRIEND/alex")
    choices=(("a",1,"FRIEND"),("b",1,"COLLEAGUE"))
    assert candidate_key(choices)==candidate_key(choices[::-1])
    assert candidate_key(choices)!=candidate_key((("a",2,"FRIEND"),choices[1]))


def _helpers():
    pytest.importorskip("psycopg")
    from test_phase2_memory import actor,call,seed,turn
    return actor,call,seed,turn


class NoModel:
    def generate(self,messages):
        raise AssertionError("canonical owner recall must not infer")


def _ask(dsn,user,instance,question,*,conv=None,deliver=True):
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation import acknowledge_text_delivery
    actor,call,_,turn=_helpers()
    req=turn(dsn,user,instance,conv or uuid4(),question)
    answer=call(dsn,respond_to_user_text,actor(user),req.turn_id,NoModel())
    if deliver:
        call(dsn,acknowledge_text_delivery,actor(user),answer.response_id)
    return req,answer


def _read(dsn,user,instance,**kwargs):
    from persona_agent.metamemory import list_recall_observations
    actor,call,_,_=_helpers()
    return call(dsn,list_recall_observations,actor(user),instance,**kwargs)


def test_gap_reopens_after_expiry_and_keeps_resolution_history(dsn):
    import psycopg
    from persona_agent.memory import admit_memory
    from persona_agent.runtime import PermissionDenied
    from persona_agent.response_orchestrator import respond_to_user_text
    from test_phase2_memory import candidate
    actor,call,seed,turn=_helpers()
    user,instance,other=seed(dsn,alternative=True)
    stranger,foreign,_=seed(dsn)
    question="What car do I drive?"
    req,unknown=_ask(dsn,user,instance,question)
    gap,=_read(dsn,user,instance,route="FACT",topic="car",open_only=True)
    assert gap.outcome=="UNKNOWN" and gap.evidence_refs==()
    assert gap.turn_id==req.turn_id and gap.delivered_at is not None
    assert call(dsn,respond_to_user_text,actor(user),req.turn_id,NoModel()).replayed
    assert len(_read(dsn,user,instance))==1
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"I drive a Toyota Camry.")
    saved=call(dsn,admit_memory,actor(user),candidate(
        instance,conv,source,kind="FACT",predicate="car",value="Toyota Camry",
        quote="I drive a Toyota Camry.",
    ))
    _,known=_ask(dsn,user,instance,question)
    assert "Toyota Camry" in known.text
    assert _read(dsn,user,instance,open_only=True)==()
    history=_read(dsn,user,instance,route="FACT",topic="car")
    assert [item.outcome for item in history]==["AVAILABLE","UNKNOWN"]
    assert history[0].evidence_refs==({
        "kind":"MEMORY","id":str(saved.memory_id),"version":1,
        "source_turn_id":str(source.turn_id),
    },)
    assert _read(dsn,user,other)==() and _read(dsn,stranger,foreign)==()
    with pytest.raises(PermissionDenied):
        _read(dsn,stranger,instance)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE memory_records SET expires_at=clock_timestamp() WHERE memory_id=%s",
                     (saved.memory_id,))
    _,expired=_ask(dsn,user,instance,question)
    assert "Toyota" not in expired.text
    gap,=_read(dsn,user,instance,open_only=True)
    assert gap.outcome=="STALE" and gap.evidence_refs==()
    assert [item.outcome for item in _read(dsn,user,instance)]==["STALE","AVAILABLE","UNKNOWN"]


@pytest.mark.parametrize("status",["CONTRADICTORY","UNVERIFIABLE"])
def test_gap_reason_is_owner_derived_not_model_assessment(dsn,status):
    import psycopg
    from persona_agent.memory import admit_memory,amend_memory
    from test_phase2_memory import candidate,correction
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    original="I drive a Toyota Camry." if status=="CONTRADICTORY" else "I used to drive a Toyota Camry."
    source=turn(dsn,user,instance,conv,original)
    saved=call(dsn,admit_memory,actor(user),candidate(
        instance,conv,source,kind="FACT",predicate="car",value="Toyota Camry",quote=original,
    ))
    if status=="CONTRADICTORY":
        newer=turn(dsn,user,instance,conv,"I now drive a Honda Civic.")
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(
                """INSERT INTO memory_extraction_candidates(
                     candidate_id,user_id,instance_id,conversation_id,source_turn_id,
                     source_event_id,memory_kind,subject_key,predicate_key,value_text,
                     source_quote,confidence,status,target_memory_id,target_version
                   ) VALUES (%s,%s,%s,%s,%s,%s,'FACT','user','car','Honda Civic',
                             'I now drive a Honda Civic.',0.97,'CONFLICT',%s,1)""",
                (uuid4(),user,instance,conv,newer.turn_id,newer.event_id,saved.memory_id),
            )
    else:
        newer=turn(dsn,user,instance,conv,"I now drive a Honda Civic.")
    _,answer=_ask(dsn,user,instance,"What car do I drive?")
    assert "Toyota" not in answer.text and "Honda" not in answer.text
    gap,=_read(dsn,user,instance,open_only=True)
    assert gap.outcome==status and gap.evidence_refs==()
    call(dsn,amend_memory,actor(user),correction(
        instance,conv,newer,saved.memory_id,value="Honda Civic",quote="I now drive a Honda Civic.",
    ))
    _,resolved=_ask(dsn,user,instance,"What car do I drive?")
    assert "Honda Civic" in resolved.text
    assert _read(dsn,user,instance,open_only=True)==()
    assert _read(dsn,user,instance)[0].evidence_refs[0]["version"]==2


def test_only_delivered_unchanged_choices_suppress_repeated_questions(dsn):
    from test_mvp05c1_entity_continuity import _register,_amend
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    _,_,friend=_register(dsn,user,instance,"Alex","FRIEND")
    _register(dsn,user,instance,"Alex","COLLEAGUE")
    question="Who is Alex to me?"
    _,undelivered=_ask(dsn,user,instance,question,deliver=False)
    assert undelivered.text.endswith("?")
    _,first=_ask(dsn,user,instance,question)
    assert first.text.endswith("?")
    conv=uuid4()
    _,repeated=_ask(dsn,user,instance,question,conv=conv)
    assert not repeated.text.endswith("?") and "friend Alex" in repeated.text and "colleague Alex" in repeated.text
    gap,=_read(dsn,user,instance,open_only=True)
    assert gap.clarification_suppressed and gap.outcome=="AMBIGUOUS"
    _,resolved=_ask(dsn,user,instance,"My friend.",conv=conv)
    assert "Alex as your friend" in resolved.text
    assert _read(dsn,user,instance,open_only=True)==()
    # The ambiguous name may reopen on a new request; a candidate revision
    # invalidates suppression rather than treating a previous choice as fact.
    _amend(dsn,user,instance,friend.entity_id,1,"I also call Alex Al.","ADD_ALIAS",alias="Al")
    _,new=_ask(dsn,user,instance,question)
    assert new.text.endswith("?")
    assert not _read(dsn,user,instance,open_only=True)[0].clarification_suppressed


def _feedback(dsn,user,instance,req,answer,*,outcome,ordinal=None):
    actor,call,_,turn=_helpers()
    command=RetrievalFeedbackCommand(
        instance_id=instance,expected_generation=1,idempotency_key=str(uuid4()),
        conversation_id=req.conversation_id,response_id=answer.response_id,
        feedback_turn_id=uuid4(),outcome=outcome,passage_ordinal=ordinal,
    )
    source=turn(dsn,user,instance,req.conversation_id,feedback_phrase(command))
    return command.model_copy(update={"feedback_turn_id":source.turn_id})


def test_explicit_feedback_is_adjacent_delivered_and_idempotent(dsn):
    import psycopg
    from persona_agent.metamemory import record_retrieval_feedback
    from persona_agent.runtime import PermissionDenied,IdempotencyConflict,VersionConflict
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    stranger,_,_=seed(dsn)
    req,answer=_ask(dsn,user,instance,"What car do I drive?")
    command=_feedback(dsn,user,instance,req,answer,outcome="MISSED")
    with pytest.raises(PermissionDenied):
        call(dsn,record_retrieval_feedback,actor(stranger),command)
    with pytest.raises(PermissionDenied):
        call(dsn,record_retrieval_feedback,actor(user),command.model_copy(update={"outcome":"INCORRECT"}))
    result=call(dsn,record_retrieval_feedback,actor(user),command)
    assert not result.replayed
    assert call(dsn,record_retrieval_feedback,actor(user),command).replayed
    with pytest.raises(IdempotencyConflict):
        call(dsn,record_retrieval_feedback,actor(user),command.model_copy(update={"outcome":"INCORRECT"}))
    with pytest.raises(VersionConflict):
        call(dsn,record_retrieval_feedback,actor(user),command.model_copy(update={"idempotency_key":str(uuid4())}))
    gap,=_read(dsn,user,instance,open_only=True)
    assert gap.outcome=="UNKNOWN" and gap.feedback==("MISSED",)
    req,answer=_ask(dsn,user,instance,"What car do I drive?",deliver=False)
    no_delivery=_feedback(dsn,user,instance,req,answer,outcome="MISSED")
    with pytest.raises(PermissionDenied):
        call(dsn,record_retrieval_feedback,actor(user),no_delivery)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM memory_records WHERE instance_id=%s",(instance,)).fetchone()[0]==0
        assert conn.execute(
            "SELECT count(*) FROM outbox WHERE event_id=%s",(result.event_id,),
        ).fetchone()[0]==1


def test_document_feedback_demotes_exact_result_without_changing_eligibility(dsn):
    import psycopg
    from test_mvp05d4_document_knowledge import _save_command,QUESTION
    from persona_agent.knowledge_memory import save_knowledge,search_knowledge
    from persona_agent.metamemory import record_retrieval_feedback
    from persona_agent.runtime import PermissionDenied
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    # Same lexical scores; newer text wins before any feedback.
    a=call(dsn,save_knowledge,actor(user),_save_command(
        dsn,user,instance,content="Certificate testing requires a local key.",locator="a.txt",
    ))
    b=call(dsn,save_knowledge,actor(user),_save_command(
        dsn,user,instance,content="Certificate testing requires a local key.",locator="b.txt",
    ))
    hits=call(dsn,search_knowledge,actor(user),instance,"certificate")
    assert [h.document_id for h in hits]==[b.document_id,a.document_id]
    req,answer=_ask(dsn,user,instance,QUESTION)
    assert "Saved passage 1" in answer.text and answer.text.index("b.txt")<answer.text.index("a.txt")
    command=_feedback(dsn,user,instance,req,answer,outcome="IRRELEVANT",ordinal=1)
    with pytest.raises(PermissionDenied):
        # The authenticated phrase is specific to ordinal one.
        call(dsn,record_retrieval_feedback,actor(user),command.model_copy(update={"passage_ordinal":2}))
    call(dsn,record_retrieval_feedback,actor(user),command)
    hits=call(dsn,search_knowledge,actor(user),instance,"certificate",max_results=1)
    assert hits[0].document_id==a.document_id
    assert call(dsn,search_knowledge,actor(user),instance,"certificate",document_id=b.document_id)
    assert call(dsn,search_knowledge,actor(user),instance,"testing",max_results=1)[0].document_id==b.document_id
    req,answer=_ask(dsn,user,instance,QUESTION)
    command=_feedback(dsn,user,instance,req,answer,outcome="RELEVANT",ordinal=2)
    call(dsn,record_retrieval_feedback,actor(user),command)
    assert call(dsn,search_knowledge,actor(user),instance,"certificate",max_results=1)[0].document_id==b.document_id
    req,answer=_ask(dsn,user,instance,QUESTION)
    call(dsn,record_retrieval_feedback,actor(user),_feedback(
        dsn,user,instance,req,answer,outcome="IRRELEVANT",ordinal=1,
    ))
    revised=call(dsn,save_knowledge,actor(user),_save_command(
        dsn,user,instance,document_id=b.document_id,expected=1,
        content="Certificate testing requires a local key.",locator="b.txt",
    ))
    assert revised.version==2
    hit,=call(dsn,search_knowledge,actor(user),instance,"certificate",max_results=1)
    assert hit.document_id==b.document_id and hit.version==2
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE memory_knowledge_documents SET privacy_class='RESTRICTED' WHERE document_id=%s",
                     (b.document_id,))
    hit,=call(dsn,search_knowledge,actor(user),instance,"certificate",max_results=1)
    assert hit.document_id==a.document_id


def test_reported_incorrect_answer_flags_review_not_fact_mutation(dsn):
    import psycopg
    from test_phase2_memory import candidate
    from persona_agent.memory import admit_memory
    from persona_agent.metamemory import record_retrieval_feedback
    actor,call,seed,turn=_helpers()
    user,instance,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,"I drive a Toyota Camry.")
    saved=call(dsn,admit_memory,actor(user),candidate(
        instance,conv,source,kind="FACT",predicate="car",value="Toyota Camry",
        quote="I drive a Toyota Camry.",
    ))
    req,answer=_ask(dsn,user,instance,"What car do I drive?")
    call(dsn,record_retrieval_feedback,actor(user),_feedback(
        dsn,user,instance,req,answer,outcome="INCORRECT",
    ))
    review,=_read(dsn,user,instance,open_only=True)
    assert review.outcome=="AVAILABLE" and review.feedback==("INCORRECT",)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT version,value_text,record_status,confidence FROM memory_records WHERE memory_id=%s",
            (saved.memory_id,),
        ).fetchone()==(1,"Toyota Camry","ACTIVE",Decimal("0.960"))
