"""P2-01 memory contract and real PostgreSQL acceptance tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text, prepare_text_context
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.memory import admit_memory, amend_memory, search_memories
from persona_agent.memory_contracts import (
    AdmitMemoryCommand, AmendMemoryCommand, MemoryConflict,
)
from persona_agent.runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection, VersionConflict,
)


def seed(dsn, alternative=False):
    user, persona, instance = uuid4(), uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)", (user,))
            conn.execute(
                "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                (persona, f"p201-{persona}"),
            )
            conn.execute(
                """INSERT INTO persona_instances(id,user_id,persona_id,label)
                   VALUES (%s,%s,%s,'Memory')""", (instance,user,persona),
            )
            other=None
            if alternative:
                p2,other=uuid4(),uuid4()
                conn.execute(
                    "INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                    (p2,f"p201-{p2}"),
                )
                conn.execute(
                    """INSERT INTO persona_instances(id,user_id,persona_id,label)
                       VALUES (%s,%s,%s,'Other')""", (other,user,p2),
                )
            conn.execute(
                """INSERT INTO persona_selection(user_id,instance_id,generation,mode)
                   VALUES (%s,%s,1,'FOREGROUND')""",(user,instance),
            )
    return user, instance, other


def actor(user):
    return TrustedActor(user_id=user,source_id="p201-user")


def call(dsn, fn, *args, **kwargs):
    with psycopg.connect(dsn,autocommit=True) as conn:
        return fn(conn,*args,**kwargs)


def turn(dsn,user,instance,conversation,text,gen=1):
    return call(dsn,ingest_text,actor(user),TextInputCommand(
        instance_id=instance,conversation_id=conversation,
        expected_generation=gen,idempotency_key=str(uuid4()),text=text,
    ))


def candidate(instance,conversation,source,*,value="tea",kind="PREFERENCE",
              predicate="favorite_drink",quote="My favorite drink is tea",
              key=None):
    return AdmitMemoryCommand(
        instance_id=instance,expected_generation=1,
        conversation_id=conversation,evidence_turn_id=source.turn_id,
        evidence_quote=quote,memory_kind=kind,subject_key="user",
        predicate_key=predicate,value_text=value,confidence=0.96,
        idempotency_key=key or str(uuid4()),
    )


def correction(instance,conversation,source,memory,*,version=1,value="coffee",
               quote="Actually my favorite drink is coffee",action="CORRECT"):
    return AmendMemoryCommand(
        instance_id=instance,expected_generation=1,memory_id=memory,
        expected_version=version,conversation_id=conversation,
        evidence_turn_id=source.turn_id,evidence_quote=quote,
        action=action,new_value=value if action=="CORRECT" else None,
        confidence=0.99 if action=="CORRECT" else None,
        idempotency_key=str(uuid4()),
    )


def test_memory_restart_recall_and_selective_context(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,"My favorite drink is tea.")
    cmd=candidate(instance,conversation,source)
    added=call(dsn,admit_memory,actor(user),cmd)
    assert added.version==1 and added.event_id is not None
    assert call(dsn,admit_memory,actor(user),cmd).replayed

    # A new connection and a new conversation still retrieve this memory.
    results=call(dsn,search_memories,actor(user),instance,"favorite drink")
    assert len(results)==1
    assert results[0].value_text=="tea"
    assert results[0].source_turn_id==source.turn_id
    assert results[0].epistemic_status=="USER_REPORTED"

    next_turn=turn(dsn,user,instance,uuid4(),"Do you remember my favorite drink?")
    context=call(dsn,prepare_text_context,actor(user),next_turn.turn_id)
    assert "favorite_drink: tea" in context.messages[0].content
    assert "not instructions or verified facts" in context.messages[0].content
    assert context.messages[-1].content=="Do you remember my favorite drink?"

    ordinary=turn(dsn,user,instance,uuid4(),"Hello, how are you?")
    plain=call(dsn,prepare_text_context,actor(user),ordinary.turn_id)
    assert "Historical user-reported memory" not in plain.messages[0].content


def test_conflicting_claim_needs_explicit_newer_correction(dsn):
    user,instance,_=seed(dsn)
    conversation=uuid4()
    first=turn(dsn,user,instance,conversation,"My favorite drink is tea.")
    saved=call(dsn,admit_memory,actor(user),
               candidate(instance,conversation,first))
    same=call(dsn,admit_memory,actor(user),
              candidate(instance,conversation,first))
    assert same.unchanged and same.event_id is None and same.memory_id==saved.memory_id

    later=turn(dsn,user,instance,conversation,
               "Actually my favorite drink is coffee.")
    with pytest.raises(MemoryConflict):
        call(dsn,admit_memory,actor(user),
             candidate(instance,conversation,later,value="coffee",
                       quote="Actually my favorite drink is coffee"))
    changed=call(dsn,amend_memory,actor(user),
                 correction(instance,conversation,later,saved.memory_id))
    assert changed.version==2 and changed.memory_id==saved.memory_id
    assert call(dsn,search_memories,actor(user),instance,"coffee")[0].value_text=="coffee"
    assert not call(dsn,search_memories,actor(user),instance,"tea")

    with pytest.raises(VersionConflict):
        call(dsn,amend_memory,actor(user),
             correction(instance,conversation,later,saved.memory_id,version=1))
    with psycopg.connect(dsn,autocommit=True) as conn:
        revisions=conn.execute(
            """SELECT version,action,value_text,source_turn_id
               FROM memory_revisions WHERE memory_id=%s ORDER BY version""",
            (saved.memory_id,),
        ).fetchall()
        assert revisions==[
            (1,"ADMIT","tea",first.turn_id),
            (2,"CORRECT","coffee",later.turn_id),
        ]
        assert conn.execute(
            """SELECT count(*) FROM events
               WHERE event_type LIKE 'memory.record.%%' AND instance_id=%s""",
            (instance,),
        ).fetchone()[0]==2
        assert conn.execute(
            """SELECT count(*) FROM outbox o JOIN events e USING(event_id)
               WHERE e.event_type LIKE 'memory.record.%%'
                 AND e.instance_id=%s""",(instance,),
        ).fetchone()[0]==2


def test_retraction_quote_validation_and_idempotency_collision(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    first=turn(dsn,user,instance,conv,"My favorite drink is tea.")
    bad=candidate(instance,conv,first,quote="I love coffee")
    with pytest.raises(MemoryConflict):
        call(dsn,admit_memory,actor(user),bad)
    good=candidate(instance,conv,first,key="admission-key")
    saved=call(dsn,admit_memory,actor(user),good)
    with pytest.raises(IdempotencyConflict):
        call(dsn,admit_memory,actor(user),good.model_copy(
            update={"value_text":"water"}))
    second=turn(dsn,user,instance,conv,"Please forget my favorite drink.")
    cmd=correction(instance,conv,second,saved.memory_id,action="RETRACT",
                   quote="Please forget my favorite drink")
    retracted=call(dsn,amend_memory,actor(user),cmd)
    assert retracted.version==2
    assert call(dsn,amend_memory,actor(user),cmd).replayed
    assert call(dsn,search_memories,actor(user),instance,"favorite drink")==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT record_status FROM memory_records WHERE memory_id=%s""",
            (saved.memory_id,),
        ).fetchone()[0]=="RETRACTED"


def test_scope_guards_persona_switch_and_time_bounds(dsn):
    user,instance,other=seed(dsn,alternative=True)
    stranger,stranger_instance,_=seed(dsn)
    conversation=uuid4()
    source=turn(dsn,user,instance,conversation,"My favorite drink is tea.")
    saved=call(dsn,admit_memory,actor(user),candidate(instance,conversation,source))
    assert not call(dsn,search_memories,actor(user),other,"drink")
    with pytest.raises(PermissionDenied):
        call(dsn,search_memories,actor(stranger),instance,"drink")
    with pytest.raises(PermissionDenied):
        call(dsn,admit_memory,actor(stranger),
             candidate(instance,conversation,source))
    with pytest.raises(PermissionDenied):
        call(dsn,admit_memory,actor(stranger),
             candidate(stranger_instance,conversation,source))
    future=datetime.now(timezone.utc)+timedelta(days=1)
    assert call(dsn,search_memories,actor(user),instance,"drink",since=future)==()
    past=datetime.now(timezone.utc)-timedelta(days=1)
    assert call(dsn,search_memories,actor(user),instance,"drink",
                since=past,until=future)[0].memory_id==saved.memory_id

    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=str(uuid4()),
    ))
    with pytest.raises(StaleSelection):
        call(dsn,admit_memory,actor(user),
             candidate(instance,conversation,source,predicate="other"))
    assert call(dsn,search_memories,actor(user),instance,"drink")


def test_parallel_corrections_one_version_wins(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    initial=turn(dsn,user,instance,conv,"My favorite drink is tea.")
    saved=call(dsn,admit_memory,actor(user),candidate(instance,conv,initial))
    newer=turn(dsn,user,instance,conv,
               "I now prefer either coffee or water.")
    commands=[
        correction(instance,conv,newer,saved.memory_id,value=v,
                   quote="I now prefer either coffee or water")
        for v in ("coffee","water")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(call,dsn,amend_memory,actor(user),c) for c in commands]
        outputs=[]
        for future in futures:
            try:
                outputs.append(future.result())
            except VersionConflict:
                outputs.append("conflict")
    assert len([x for x in outputs if x=="conflict"])==1
    assert len([x for x in outputs if x!="conflict"])==1
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM memory_revisions WHERE memory_id=%s",
            (saved.memory_id,),
        ).fetchone()[0]==2


def test_bounded_retrieval_and_context_preserves_latest_input(dsn):
    user,instance,_=seed(dsn)
    conv=uuid4()
    for n in range(5):
        phrase=f"I prefer topic{n} during lunch."
        source=turn(dsn,user,instance,conv,phrase)
        call(dsn,admit_memory,actor(user),candidate(
            instance,conv,source,value=f"topic{n}",predicate=f"lunch_{n}",
            quote=phrase))
    with pytest.raises(ValueError):
        call(dsn,search_memories,actor(user),instance,"lunch",max_results=11)
    found=call(dsn,search_memories,actor(user),instance,"lunch",
               max_results=2,max_chars=128)
    assert len(found)<=2

    current=turn(dsn,user,instance,conv,"What do you remember about lunch?")
    ctx=call(dsn,prepare_text_context,actor(user),current.turn_id,
             max_context_chars=512)
    assert sum(len(m.content) for m in ctx.messages)<=512
    assert ctx.messages[-1].content=="What do you remember about lunch?"
