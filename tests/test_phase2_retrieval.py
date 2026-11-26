"""P2-05 PostgreSQL acceptance: scoped, selective topic-aware retrieval."""

import json
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from unittest.mock import patch

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text,prepare_text_context
from persona_agent.conversation_contracts import TextInputCommand,ModelReply
from persona_agent.dialogue_contracts import (
    OpenThreadCommand,FocusThreadCommand,SetThreadProgressCommand,
    Attention,Resolution,
)
from persona_agent.dialogue_state import open_thread,focus_thread,set_thread_progress,load_active_dialogue
from persona_agent.memory import admit_memory
from persona_agent.memory_contracts import AdmitMemoryCommand
from persona_agent.memory_retrieval import (
    load_topic_attention,search_relevant_history,topic_recall_block,
    is_historical_recall_query,
)
from persona_agent.operation_scheduler import configure_resource_pool,claim_operation
from persona_agent.summary_contracts import (
    SUMMARY_CAPABILITY,SUMMARY_OPERATION,SUMMARY_RESOURCE,
)
from persona_agent.summary_memory import schedule_summary,run_summary
from persona_agent.runtime import PermissionDenied


def call(dsn,func,*args,**kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return func(conn,*args,**kwargs)


def actor(user):
    return TrustedActor(user_id=user,source_id="p205-user")


def seed(dsn,*,second_persona=False):
    user,persona,instance=uuid4(),uuid4(),uuid4()
    other=None
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona,f"p205-{persona}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'Memory')""",(instance,user,persona),
            )
            if second_persona:
                second_definition,other=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (second_definition,f"p205-{second_definition}"),
                )
                conn.execute(
                    """INSERT INTO persona_instances(id,user_id,persona_id,label)
                       VALUES (%s,%s,%s,'Other')""",
                    (other,user,second_definition),
                )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",(user,instance),
            )
    return user,instance,other


def turn(dsn,user,instance,conversation,text):
    return call(dsn,ingest_text,actor(user),TextInputCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,idempotency_key=str(uuid4()),text=text,
    ))


def remember(dsn,user,instance,conversation,text,*,kind="PREFERENCE",
             predicate="favorite_drink",value="tea",quote=None):
    source=turn(dsn,user,instance,conversation,text)
    result=call(dsn,admit_memory,actor(user),AdmitMemoryCommand(
        instance_id=instance,expected_generation=1,
        conversation_id=conversation,evidence_turn_id=source.turn_id,
        evidence_quote=quote or text,memory_kind=kind,
        subject_key="user",predicate_key=predicate,value_text=value,
        confidence=0.96,idempotency_key=str(uuid4()),
    ))
    return source,result


def revision(dsn,user,conversation):
    return call(dsn,load_active_dialogue,actor(user),conversation).revision


def open_topic(dsn,user,instance,conversation,turn_ref,title):
    thread_id=uuid4()
    call(dsn,open_thread,actor(user),OpenThreadCommand(
        conversation_id=conversation,instance_id=instance,
        expected_generation=1,expected_revision=revision(dsn,user,conversation),
        evidence_turn_id=turn_ref.turn_id,idempotency_key=str(uuid4()),
        thread_id=thread_id,title=title,
    ))
    return thread_id


class EpisodeSummary:
    def generate(self,messages):
        rows=json.loads(messages[-1].content)
        evidence=next(row for row in rows if "rescue dog" in row["user"])
        return ModelReply(model_id="p205-summary",text=json.dumps({
            "summary":"We discussed a rescue dog and garden memories.",
            "episodes":[{
                "source_turn_id":evidence["turn_id"],
                "evidence_quote":"rescue dog",
                "topic":"rescue dog adoption",
                "description":"User reported adopting a rescue dog",
                "confidence":0.8,
            }],
        }))


def episode(dsn,user,instance):
    call(dsn,configure_resource_pool,SUMMARY_RESOURCE,
         capacity_units=3,p0_reserved_units=1)
    conversation=uuid4()
    source=None
    for i in range(9):
        response=turn(
            dsn,user,instance,conversation,
            "I adopted a rescue dog." if i==1 else f"Garden conversation turn {i}.",
        )
        if i==1:
            source=response
    admitted=call(dsn,schedule_summary,actor(user),conversation,instance,1,
                  max_turns=5)
    assert admitted is not None
    lease=call(
        dsn,claim_operation,worker_id="p205-summary-worker",
        capabilities=(SUMMARY_CAPABILITY,),
        operation_types=(SUMMARY_OPERATION,),lease_seconds=120,
    )
    assert lease is not None
    completed=call(dsn,run_summary,lease,EpisodeSummary())
    assert completed.episode_count==1
    return source


def test_scoped_hybrid_retrieval_with_user_memory_and_interpreted_episode(dsn):
    user,instance,other=seed(dsn,second_persona=True)
    outsider,outsider_instance,_=seed(dsn)
    original=uuid4()
    source,memory=remember(
        dsn,user,instance,original,"My favorite drink is tea.",
    )
    ep_source=episode(dsn,user,instance)

    memories=call(
        dsn,search_relevant_history,actor(user),instance,"favorite drink",
    )
    assert len(memories)==1 and memories[0].source_type=="USER_MEMORY"
    assert memories[0].source_id==memory.memory_id
    assert memories[0].source_turn_id==source.turn_id
    assert memories[0].record_version==1
    assert memories[0].epistemic_status=="USER_REPORTED"

    experiences=call(
        dsn,search_relevant_history,actor(user),instance,"rescue dog adoption",
    )
    assert len(experiences)>=1
    assert experiences[0].source_type=="EPISODE"
    assert experiences[0].source_turn_id==ep_source.turn_id
    assert experiences[0].epistemic_status=="MODEL_INTERPRETED"
    assert experiences[0].source_turn_at.tzinfo is not None
    assert call(dsn,search_relevant_history,actor(user),other,"rescue dog")==()
    assert call(dsn,search_relevant_history,actor(user),other,"favorite drink")==()
    with pytest.raises(PermissionDenied):
        call(dsn,search_relevant_history,actor(outsider),instance,"favorite drink")
    with pytest.raises(PermissionDenied):
        call(dsn,load_topic_attention,actor(outsider),original)


def test_bounded_time_filtered_recall_and_privacy_exclusion(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    source,memory=remember(dsn,user,instance,conv,"My favorite drink is tea.")
    assert call(
        dsn,search_relevant_history,actor(user),instance,"favorite drink",
        since=datetime.now(timezone.utc)-timedelta(days=1),
    )
    assert call(
        dsn,search_relevant_history,actor(user),instance,"favorite drink",
        since=datetime.now(timezone.utc)+timedelta(days=1),
    )==()
    assert call(
        dsn,search_relevant_history,actor(user),instance,"favorite drink",
        until=datetime.now(timezone.utc)-timedelta(days=1),
    )==()
    for kwargs in (
        {"since":datetime.now().replace(tzinfo=None)},
        {"since":datetime.now(timezone.utc),"until":datetime.now(timezone.utc)-timedelta(days=1)},
    ):
        with pytest.raises(ValueError):
            call(dsn,search_relevant_history,actor(user),instance,"favorite drink",**kwargs)
    with pytest.raises(ValueError):
        call(dsn,search_relevant_history,actor(user),instance,"favorite drink",max_results=11)
    with pytest.raises(ValueError):
        call(dsn,search_relevant_history,actor(user),instance,"favorite drink",max_chars=200)

    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE memory_records SET privacy_class='SENSITIVE' WHERE memory_id=%s",
            (memory.memory_id,),
        )
    assert call(dsn,search_relevant_history,actor(user),instance,"favorite drink")==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE memory_records SET privacy_class='NORMAL',"
            "expires_at=clock_timestamp()-interval '1 day' WHERE memory_id=%s",
            (memory.memory_id,),
        )
    assert call(dsn,search_relevant_history,actor(user),instance,"favorite drink")==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at=NULL,"
            "record_status='RETRACTED' WHERE memory_id=%s",(memory.memory_id,),
        )
    assert call(dsn,search_relevant_history,actor(user),instance,"favorite drink")==()


def test_topic_attention_is_persistent_and_decay_does_not_change_resolution(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    first=turn(dsn,user,instance,conv,"Discuss gardening equipment")
    garden=open_topic(dsn,user,instance,conv,first,"garden memories")
    second=turn(dsn,user,instance,conv,"Now discuss weekend plans")
    weekend=open_topic(dsn,user,instance,conv,second,"weekend plans")
    state=call(dsn,load_topic_attention,actor(user),conv)
    by_id={topic.thread_id:topic for topic in state.topics}
    assert state.focus_thread_id==weekend
    assert by_id[garden].attention_status=="RECENT"
    assert by_id[garden].resolution_status=="OPEN"
    assert by_id[weekend].attention_status=="FOCUSED"
    assert by_id[weekend].effective_attention=="FOCUSED"

    # Time affects a read-only relevance view; not persisted topic or goal state.
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """UPDATE dialogue_threads SET updated_at=clock_timestamp()-interval '40 days'
               WHERE thread_id=%s""",(garden,),
        )
    stale=call(dsn,load_topic_attention,actor(user),conv,recent_days=14)
    by_id={topic.thread_id:topic for topic in stale.topics}
    assert by_id[garden].attention_status=="RECENT"
    assert by_id[garden].effective_attention=="DORMANT"
    assert by_id[garden].resolution_status=="OPEN"
    assert call(dsn,load_active_dialogue,actor(user),conv).threads[1].attention_status==Attention.RECENT

    resumed=turn(dsn,user,instance,conv,"Resume the garden discussion")
    call(dsn,focus_thread,actor(user),FocusThreadCommand(
        conversation_id=conv,instance_id=instance,expected_generation=1,
        expected_revision=revision(dsn,user,conv),
        evidence_turn_id=resumed.turn_id,idempotency_key=str(uuid4()),
        thread_id=garden,
    ))
    restored=call(dsn,load_topic_attention,actor(user),conv)
    assert restored.focus_thread_id==garden
    assert restored.topics[0].effective_attention=="FOCUSED"
    assert restored.topics[0].resolution_status=="OPEN"


def test_deictic_topic_reference_retrieves_history_without_new_inference(dsn):
    user,instance,_=seed(dsn)
    oldconv=uuid4()
    source,memory=remember(
        dsn,user,instance,oldconv,"My favorite drink is tea.",
    )
    conv=uuid4()
    topic_turn=turn(dsn,user,instance,conv,"Let's discuss favorite drinks.")
    topic=open_topic(dsn,user,instance,conv,topic_turn,"favorite drink")
    current=turn(dsn,user,instance,conv,"What about that?")
    context=call(dsn,prepare_text_context,actor(user),current.turn_id)
    system=context.messages[0].content
    assert "Potentially relevant historical context" in system
    assert "USER_REPORTED" in system
    assert "favorite_drink" in system
    assert str(source.turn_id) in system
    assert str(memory.memory_id) not in system  # quote source turns, not hidden keys
    assert context.messages[-1].content=="What about that?"
    assert sum(len(m.content) for m in context.messages)<=12000


def test_unrelated_messages_never_search_and_topic_switch_is_respected(dsn):
    user,instance,_=seed(dsn)
    oldconv=uuid4()
    remember(dsn,user,instance,oldconv,"My favorite drink is tea.")
    conv=uuid4()
    initial=turn(dsn,user,instance,conv,"Discuss favorite drinks")
    open_topic(dsn,user,instance,conv,initial,"favorite drink")
    unrelated=turn(dsn,user,instance,conv,"Please explain how TCP sockets work")
    with patch("persona_agent.memory_retrieval._candidates",
               side_effect=AssertionError("unnecessary historical search")):
        context=call(dsn,prepare_text_context,actor(user),unrelated.turn_id)
    assert "Potentially relevant historical context" not in context.messages[0].content

    switch=turn(dsn,user,instance,conv,"Actually discuss lunar astronomy")
    open_topic(dsn,user,instance,conv,switch,"lunar astronomy")
    current=turn(dsn,user,instance,conv,"What about that?")
    context=call(dsn,prepare_text_context,actor(user),current.turn_id)
    assert "favorite_drink" not in context.messages[0].content
    assert context.messages[-1].content=="What about that?"


def test_retrieval_budget_and_stable_ranking_preserve_evidence(dsn):
    user,instance,_=seed(dsn)
    oldconv=uuid4()
    for i in range(9):
        remember(
            dsn,user,instance,oldconv,f"My favorite garden plant {i} is lavender.",
            predicate=f"favorite_garden_plant_{i}",value="lavender",
        )
    first=call(dsn,search_relevant_history,actor(user),instance,"garden plant",
               max_results=6,max_chars=550)
    second=call(dsn,search_relevant_history,actor(user),instance,"garden plant",
                max_results=6,max_chars=550)
    assert 1<=len(first)<=6
    assert [x.source_id for x in first]==[x.source_id for x in second]
    assert all(x.source_turn_id is not None and x.source_turn_at for x in first)
    assert all(x.excerpt and x.epistemic_status=="USER_REPORTED" for x in first)
    assert sum(len(x.excerpt)+len(x.topic)+95 for x in first)<=550

    latest=turn(dsn,user,instance,uuid4(),"What about that?")
    # A bare anaphora with no focused thread cannot invent a prior topic.
    ctx=call(dsn,prepare_text_context,actor(user),latest.turn_id,
             max_context_chars=512)
    assert ctx.messages[-1].content=="What about that?"
    assert sum(len(x.content) for x in ctx.messages)<=512
    assert "Potentially relevant historical context" not in ctx.messages[0].content


def test_recall_signal_is_not_topic_or_memory_creation():
    assert is_historical_recall_query("Do you remember the garden?")
    assert is_historical_recall_query("What did I say about tea?")
    assert not is_historical_recall_query("Can you explain Docker networking?")


def _change_memory(dsn,user,instance,source,saved,text,*,action="CORRECT",value=None):
    from persona_agent.memory import amend_memory
    from persona_agent.memory_contracts import AmendMemoryCommand
    return call(dsn,amend_memory,actor(user),AmendMemoryCommand(
        instance_id=instance,expected_generation=1,memory_id=saved.memory_id,
        expected_version=saved.version,conversation_id=source.conversation_id,
        evidence_turn_id=source.turn_id,evidence_quote=text,
        action=action,new_value=value if action=="CORRECT" else None,
        confidence=0.99 if action=="CORRECT" else None,idempotency_key=str(uuid4()),
    ))


def _conflict(dsn,user,instance,source,saved,*,kind,predicate,value,text):
    """Seed the existing candidate shape, not a new extraction capability."""
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """INSERT INTO memory_extraction_candidates(
                 candidate_id,user_id,instance_id,conversation_id,source_turn_id,
                 source_event_id,memory_kind,subject_key,predicate_key,value_text,
                 source_quote,confidence,status,target_memory_id,target_version
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,'user',%s,%s,%s,0.97,'CONFLICT',%s,%s)""",
            (uuid4(),user,instance,source.conversation_id,source.turn_id,
             source.event_id,kind,predicate,value,text,saved.memory_id,saved.version),
        )


def test_disputed_slot_is_excluded_from_hybrid_and_focused_recall(dsn):
    from persona_agent.memory import search_memories
    user,instance,_=seed(dsn)
    old=uuid4()
    original,saved=remember(dsn,user,instance,old,"My favorite drink is tea.")
    text="My favorite drink is coffee."
    newer=turn(dsn,user,instance,old,text)
    _conflict(dsn,user,instance,newer,saved,kind="PREFERENCE",
              predicate="favorite_drink",value="coffee",text=text)
    query="favorite drink"
    assert call(dsn,search_memories,actor(user),instance,query)==()
    assert call(dsn,search_relevant_history,actor(user),instance,query)==()
    current=uuid4()
    entered=turn(dsn,user,instance,current,"Discuss favorite drinks.")
    open_topic(dsn,user,instance,current,entered,query)
    request=turn(dsn,user,instance,current,"What about that?")
    context=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert "unresolved. Do not assert the old value" in context.messages[0].content
    assert "favorite_drink: tea" not in context.messages[0].content
    assert "favorite_drink: coffee" not in context.messages[0].content
    corrected=_change_memory(dsn,user,instance,newer,saved,text,value="coffee")
    hits=call(dsn,search_relevant_history,actor(user),instance,query)
    assert [(h.excerpt,h.record_version,h.source_turn_id) for h in hits]==[
        ("coffee",corrected.version,newer.turn_id)
    ]
    context=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert "favorite_drink: coffee" in context.messages[0].content
    assert "favorite_drink: tea" not in context.messages[0].content
    assert "unresolved. Do not assert the old value" not in context.messages[0].content


@pytest.mark.parametrize("change",["CORRECT","RETRACT","SENSITIVE","EXPIRED","CONFLICT"])
def test_episode_source_cannot_bypass_current_memory_eligibility(dsn,change):
    from persona_agent.summary_memory import search_episodes,episode_source_turn
    user,instance,_=seed(dsn)
    source=episode(dsn,user,instance)
    assert source is not None
    saved=call(dsn,admit_memory,actor(user),AdmitMemoryCommand(
        instance_id=instance,expected_generation=1,conversation_id=source.conversation_id,
        evidence_turn_id=source.turn_id,evidence_quote="I adopted a rescue dog.",
        memory_kind="FACT",subject_key="user",predicate_key="rescue_dog_adoption",
        value_text="adopted",confidence=0.96,idempotency_key=str(uuid4()),
    ))
    eligible=call(dsn,search_episodes,actor(user),instance,"rescue dog")
    assert len(eligible)==1
    episode_id=eligible[0].episode_id
    assert call(dsn,episode_source_turn,actor(user),instance,episode_id)["evidence_quote"]=="rescue dog"
    if change in ("CORRECT","RETRACT","CONFLICT"):
        text="Actually, I did not adopt a rescue dog."
        newer=turn(dsn,user,instance,source.conversation_id,text)
        if change=="CONFLICT":
            _conflict(dsn,user,instance,newer,saved,kind="FACT",
                      predicate="rescue_dog_adoption",value="not adopted",text=text)
        else:
            _change_memory(dsn,user,instance,newer,saved,text,
                           action=change,value="not adopted")
    else:
        with psycopg.connect(dsn,autocommit=True) as conn:
            if change=="SENSITIVE":
                conn.execute("UPDATE memory_records SET privacy_class='SENSITIVE' WHERE memory_id=%s",
                             (saved.memory_id,))
            else:
                conn.execute("UPDATE memory_records SET expires_at=clock_timestamp() WHERE memory_id=%s",
                             (saved.memory_id,))
    assert call(dsn,search_episodes,actor(user),instance,"rescue dog")==()
    with pytest.raises(PermissionDenied):
        call(dsn,episode_source_turn,actor(user),instance,episode_id)
    hits=call(dsn,search_relevant_history,actor(user),instance,"rescue dog")
    assert all(h.source_type!="EPISODE" and h.source_turn_id!=source.turn_id for h in hits)
    current=turn(dsn,user,instance,uuid4(),"Do you remember the rescue dog adoption?")
    ctx=call(dsn,prepare_text_context,actor(user),current.turn_id)
    assert "User reported adopting a rescue dog" not in ctx.messages[0].content
    assert "rescue_dog_adoption: adopted" not in ctx.messages[0].content
    if change=="CORRECT":
        assert "rescue_dog_adoption: not adopted" in ctx.messages[0].content
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_episodes WHERE source_turn_id=%s",
            (source.turn_id,),
        ).fetchone()[0]==1


def test_small_budget_never_abridges_a_current_memory_value(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    value="Lavender "+("in the northern garden " * 18)+"except in winter"
    remember(dsn,user,instance,conv,"My garden meaning is "+value,
             kind="PERSONAL_MEANING",predicate="garden_meaning",value=value)
    source,small=remember(dsn,user,instance,conv,"My favorite garden plant is sage.",
                         predicate="garden_plant",value="sage")
    hits=call(dsn,search_relevant_history,actor(user),instance,"garden",
              max_chars=256,max_results=4)
    assert any(h.source_id==small.memory_id for h in hits)
    assert all(not h.abridged for h in hits if h.source_type=="USER_MEMORY")
    assert all("Lavender" not in h.excerpt for h in hits)
    all_hits=call(dsn,search_relevant_history,actor(user),instance,"garden",
                  max_chars=2400,max_results=4)
    assert any(h.excerpt==value and not h.abridged for h in all_hits)
