"""05E cross-store acceptance; deterministic gateways, real PostgreSQL."""
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from persona_agent.personal_meaning_grammar import unresolved_meaning_topic,parse_personal_definition
from persona_agent.scenario_validation import validate_scenario

UNSUPPORTED="Actually, when I say evening routine, I mean practicing piano."
DEFINITION="When I say evening routine, I mean walking the dog and reading."
VALUE="walking the dog and reading"
REPLACEMENT="Actually, when I say evening routine, I mean stretching and reading."
NEW_VALUE="stretching and reading"
QUESTION="What do I mean by evening routine?"
PLAN="Help me plan my evening routine."


@pytest.mark.parametrize("source,expected",[
    (UNSUPPORTED,"evening routine"),
    ("For me, home means checking my password.","home"),
    ("When I say evening routine, I mean reading for 500 minutes.","evening routine"),
    (DEFINITION,None),
    ('"Actually, when I say evening routine, I mean practicing piano."',None),
    ("If I say evening routine, I mean practicing piano.",None),
    ("When I say private phrase, I mean practicing piano.",None),
    ("When I say evening routine, I mean piano. Tell me a joke.",None),
    ("What do I mean by evening routine?",None),
])
def test_unsupported_definition_notice_does_not_expand_admission(source,expected):
    assert unresolved_meaning_topic(source)==expected
    if expected:
        assert parse_personal_definition(source) is None


def test_behavioral_scenario_is_valid_and_has_unique_review_cases():
    path=Path(__file__).resolve().parents[1]/"scenarios/mvp05e_memory_acceptance.json"
    scenario=validate_scenario(json.loads(path.read_text(encoding="utf-8")))
    assert sum(step["action"]=="restart" for step in scenario["steps"])>=2
    assert len([s for s in scenario["steps"] if "review_case" in s])>=8


def _helpers():
    pytest.importorskip("psycopg")
    from test_phase2_memory import actor,call,seed,turn,candidate,correction
    return actor,call,seed,turn,candidate,correction


class NoModel:
    def generate(self,messages):
        raise AssertionError("owner recall must not invoke inference")


class OldRoutineModel:
    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        assert all(VALUE not in m.content for m in messages)
        assert all("practicing piano" not in m.content for m in messages)
        return ModelReply(model_id="untrusted-test",text="Go walking the dog and reading.")


def _answer(dsn,user,instance,question,*,conv=None,model=None,deliver=True):
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation import acknowledge_text_delivery
    actor,call,_,turn,_,_=_helpers()
    request=turn(dsn,user,instance,conv or uuid4(),question)
    answer=call(dsn,respond_to_user_text,actor(user),request.turn_id,model or NoModel())
    if deliver:
        call(dsn,acknowledge_text_delivery,actor(user),answer.response_id)
    return request,answer


def test_unsupported_change_is_read_barrier_not_admission_and_resolves(dsn):
    import psycopg
    from persona_agent.memory import admit_memory,amend_memory,search_memories
    from persona_agent.personal_meaning import get_personal_meaning,UNKNOWN,meaning_change_reply
    from persona_agent.conversation import prepare_text_context,commit_text_response,respond_to_text
    from persona_agent.conversation_contracts import ModelReply,StaleHistoricalContext
    from persona_agent.metamemory import list_recall_observations
    actor,call,seed,turn,candidate,correction=_helpers()
    user,instance,_=seed(dsn)
    stranger,foreign,_=seed(dsn)
    conv=uuid4()
    source=turn(dsn,user,instance,conv,DEFINITION)
    saved=call(dsn,admit_memory,actor(user),candidate(
        instance,conv,source,kind="PERSONAL_MEANING",predicate="meaning_evening_routine",
        value=VALUE,quote=DEFINITION,
    ))
    other_source=turn(dsn,stranger,foreign,uuid4(),DEFINITION)
    call(dsn,admit_memory,actor(stranger),candidate(
        foreign,other_source.conversation_id,other_source,kind="PERSONAL_MEANING",
        predicate="meaning_evening_routine",value=VALUE,quote=DEFINITION,
    ))
    _answer(dsn,user,instance,QUESTION,conv=conv)
    request=turn(dsn,user,instance,uuid4(),PLAN)
    old_context=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert VALUE in old_context.messages[0].content
    notice=turn(dsn,user,instance,conv,UNSUPPORTED)
    with pytest.raises(StaleHistoricalContext):
        call(dsn,commit_text_response,actor(user),old_context,ModelReply("stale",VALUE),
             model_generated=True)
    assert call(dsn,get_personal_meaning,actor(user),instance,"evening routine") is None
    assert not call(dsn,search_memories,actor(user),instance,"evening routine")
    assert call(dsn,get_personal_meaning,actor(stranger),foreign,"evening routine").definition==VALUE
    assert call(dsn,respond_to_text,actor(user),request.turn_id,OldRoutineModel()).text==meaning_change_reply("evening routine")
    _,answer=_answer(dsn,user,instance,PLAN,conv=conv,model=OldRoutineModel())
    assert answer.text==meaning_change_reply("evening routine")
    _,answer=_answer(dsn,user,instance,QUESTION)
    assert answer.text==UNKNOWN
    gaps=call(dsn,list_recall_observations,actor(user),instance,route="MEANING",
              topic="evening routine",open_only=True)
    assert len(gaps)==1 and gaps[0].outcome=="CONTRADICTORY"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT version,value_text FROM memory_records WHERE memory_id=%s",
            (saved.memory_id,),
        ).fetchone()==(1,VALUE)
        assert conn.execute(
            "SELECT predicate_key,source_event_id FROM memory_meaning_change_notices WHERE source_turn_id=%s",
            (notice.turn_id,),
        ).fetchone()==("meaning_evening_routine",notice.event_id)
        assert conn.execute(
            "SELECT count(*) FROM explicit_memory_reports WHERE source_turn_id=%s",
            (notice.turn_id,),
        ).fetchone()==(0,)
    revised=turn(dsn,user,instance,conv,REPLACEMENT)
    call(dsn,amend_memory,actor(user),correction(
        instance,conv,revised,saved.memory_id,value=NEW_VALUE,quote=REPLACEMENT,
    ))
    _,answer=_answer(dsn,user,instance,QUESTION)
    assert NEW_VALUE in answer.text
    assert call(dsn,list_recall_observations,actor(user),instance,
                route="MEANING",topic="evening routine",open_only=True)==()


def test_suppressed_reply_does_not_extend_actual_question_cooldown(dsn):
    import psycopg
    from psycopg.rows import dict_row
    from test_mvp05c1_entity_continuity import _register
    from persona_agent.metamemory import prior_clarification,list_recall_observations,entity_topic
    from persona_agent.entity_reference import entity_reference
    from persona_agent.metamemory_contracts import recall_key
    actor,call,seed,_,_,_=_helpers()
    user,instance,_=seed(dsn)
    _register(dsn,user,instance,"Alex","FRIEND")
    _register(dsn,user,instance,"Alex","COLLEAGUE")
    question="Who is Alex to me?"
    _,first=_answer(dsn,user,instance,question)
    _,repeat=_answer(dsn,user,instance,question)
    assert first.text.endswith("?") and not repeat.text.endswith("?")
    newest,original=call(dsn,list_recall_observations,actor(user),instance,route="ENTITY")
    assert newest.clarification_suppressed and not original.clarification_suppressed
    candidates=tuple((ref["id"],ref["version"],ref["relation"]) for ref in original.evidence_refs)
    key=recall_key("ENTITY",entity_topic(entity_reference(question)))
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        assert prior_clarification(conn.cursor(),user_id=user,instance_id=instance,
            query_key=key,candidates=candidates,before_at=newest.delivered_at+timedelta(seconds=1))
        assert not prior_clarification(conn.cursor(),user_id=user,instance_id=instance,
            query_key=key,candidates=candidates,before_at=original.delivered_at+timedelta(hours=24))


def test_integrated_memory_after_50_turn_compression_and_cross_session(dsn):
    """Shared identity and source history; real summary/cognition owner operations."""
    import psycopg
    import subprocess
    import sys
    from persona_agent.conversation import prepare_text_context
    from persona_agent.conversation_contracts import ModelReply
    from persona_agent.memory import admit_memory
    from persona_agent.summary_memory import schedule_summary,run_summary,search_episodes
    from persona_agent.summary_contracts import SUMMARY_OPERATION,SUMMARY_CAPABILITY
    from persona_agent.knowledge_memory import save_knowledge,search_knowledge
    from persona_agent.personal_meaning import get_personal_meaning
    from persona_agent.metamemory import list_recall_observations
    from persona_agent.meta_episodic import search_episode_patterns
    from test_phase2_acceptance import prepare_resources,claimed
    from test_mvp05d2_meta_episodic import REPORTS,_run,_proposal,HYPOTHESIS
    from test_mvp05d4_document_knowledge import _save_command,QUESTION as DOCUMENT_QUESTION,CONTENT
    from test_mvp05c1_entity_continuity import _register
    actor,call,seed,turn,candidate,_=_helpers()
    user,instance,other=seed(dsn,alternative=True)
    prepare_resources(dsn)
    conv=uuid4()
    fact=turn(dsn,user,instance,conv,"I drive a Toyota Camry.")
    meaning=turn(dsn,user,instance,conv,DEFINITION)
    for item,kind,predicate,value,quote in (
        (fact,"FACT","car","Toyota Camry","I drive a Toyota Camry."),
        (meaning,"PERSONAL_MEANING","meaning_evening_routine",VALUE,DEFINITION),
    ):
        call(dsn,admit_memory,actor(user),candidate(
            instance,conv,item,kind=kind,predicate=predicate,value=value,quote=quote,
        ))
    experiences=[turn(dsn,user,instance,conv,text) for text in REPORTS[:3]]
    for i in range(45):
        turn(dsn,user,instance,conv,f"Gardening discussion number {i}.")
    _register(dsn,user,instance,"Alex","FRIEND")
    saved=call(dsn,save_knowledge,actor(user),_save_command(dsn,user,instance))

    class SummaryGateway:
        def generate(self,messages):
            rows=json.loads(messages[-1].content)
            episodes=[{"source_turn_id":row["turn_id"],"evidence_quote":row["user"],
                       "topic":"exercise","description":"User reported: "+row["user"],
                       "confidence":0.8} for row in rows if row["user"] in REPORTS[:3]]
            return ModelReply("05e-summary",json.dumps({
                "summary":"Discussion of gardening, exercise and user routines.",
                "episodes":episodes,
            }))

    for i in range(6):
        scheduled=call(dsn,schedule_summary,actor(user),conv,instance,1,retain_recent=2,max_turns=8)
        assert scheduled.start_sequence==i*8+1 and scheduled.end_sequence==(i+1)*8
        lease=claimed(dsn,SUMMARY_OPERATION,SUMMARY_CAPABILITY,"05e-summary")
        assert lease.operation_id==scheduled.operation_id
        call(dsn,run_summary,lease,SummaryGateway())
    episodes=call(dsn,search_episodes,actor(user),instance,"exercise")
    by_source={h.source_turn_id:h.episode_id for h in episodes}
    a,b,c=[by_source[item.turn_id] for item in experiences]
    _,result,_=_run(dsn,user,instance,_proposal((a,b),(c,)))
    assert result.pattern_count==1

    # New interpreter, not just a fresh context or database cursor.
    probe=r'''
import json,sys
from uuid import UUID
import psycopg
from persona_agent.contracts import TrustedActor
from persona_agent.personal_meaning import get_personal_meaning
from persona_agent.knowledge_memory import search_knowledge
from persona_agent.meta_episodic import search_episode_patterns
dsn,user,instance=sys.argv[1:]
actor=TrustedActor(user_id=UUID(user),source_id="05e-restart")
with psycopg.connect(dsn,autocommit=True) as conn:
    meaning=get_personal_meaning(conn,actor,UUID(instance),"evening routine")
    docs=search_knowledge(conn,actor,UUID(instance),"certificate")
    patterns=search_episode_patterns(conn,actor,UUID(instance),"exercise")
    print(json.dumps([str(meaning.source_turn_id),str(docs[0].document_id),
                      patterns[0].epistemic_status,len(patterns[0].supporting)]))
'''
    child=subprocess.run([sys.executable,"-c",probe,dsn,str(user),str(instance)],
                         check=True,text=True,capture_output=True,timeout=30)
    assert json.loads(child.stdout)==[str(meaning.turn_id),str(saved.document_id),"MODEL_INTERPRETED",2]
    assert VALUE in _answer(dsn,user,instance,QUESTION)[1].text
    assert "Toyota Camry" in _answer(dsn,user,instance,"What car do I drive?")[1].text
    assert "Alex as your friend" in _answer(dsn,user,instance,"Who is Alex to me?")[1].text
    assert CONTENT in _answer(dsn,user,instance,DOCUMENT_QUESTION)[1].text

    class PatternRecall:
        def generate(self,messages):
            assert HYPOTHESIS in messages[0].content and "MODEL_INTERPRETED" in messages[0].content
            return ModelReply("05e-recall","Overtime may be an obstacle, but it is not always one.")

    _answer(dsn,user,instance,"What patterns have you noticed about my exercise?",model=PatternRecall())
    assert call(dsn,list_recall_observations,actor(user),instance,open_only=True)==()
    assert call(dsn,search_knowledge,actor(user),other,"certificate")==()
    assert call(dsn,search_episode_patterns,actor(user),other,"exercise")==()
    assert call(dsn,get_personal_meaning,actor(user),other,"evening routine") is None
    unrelated=turn(dsn,user,instance,uuid4(),"Tell me a joke.")
    ctx=call(dsn,prepare_text_context,actor(user),unrelated.turn_id)
    assert all(not any(value in msg.content for value in (VALUE,CONTENT,HYPOTHESIS,"Toyota Camry"))
               for msg in ctx.messages)
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT summary_watermark FROM conversations WHERE conversation_id=%s",
                            (conv,)).fetchone()==(48,)
        assert conn.execute("SELECT count(*) FROM conversation_turns WHERE conversation_id=%s",
                            (conv,)).fetchone()==(50,)
