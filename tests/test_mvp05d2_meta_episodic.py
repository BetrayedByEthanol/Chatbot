"""MVP-05D.2: scoped multi-episode hypotheses through existing cognition."""
import json
from uuid import uuid4

import pytest

from persona_agent.meta_episodic_contracts import PatternProposal,pattern_reference
from pydantic import ValidationError


QUESTION="What patterns have you noticed about my exercise?"
HYPOTHESIS="Overtime may be a recurring obstacle to exercise."
REPORTS=(
    "I skipped exercise on Monday because I worked late.",
    "On Thursday overtime made me miss exercise.",
    "I completed exercise even though I worked overtime.",
    "On Friday overtime interfered with exercise again.",
)


@pytest.mark.parametrize("text,expected",[
    (QUESTION,"exercise"),
    ("What recurring patterns do you remember about gardening?","gardening"),
    ("What patterns have you observed in my work?","work"),
    ("Explain recurring patterns in Python.",None),
    ('"What patterns have you noticed about exercise?"',None),
    ("What patterns have you noticed about exercise? And gardening?",None),
])
def test_pattern_reference_is_bounded(text,expected):
    assert pattern_reference(text)==expected


def test_pattern_proposal_requires_distinct_disjoint_evidence():
    a,b=uuid4(),uuid4()
    for support,counter in [((a,),()),((a,a),()),((a,b),(a,))]:
        with pytest.raises(ValidationError):
            PatternProposal(topic="exercise",hypothesis=HYPOTHESIS,
                            supporting_episode_ids=support,counter_episode_ids=counter)
    with pytest.raises(ValidationError):
        PatternProposal(topic="exercise",hypothesis=HYPOTHESIS,
                        supporting_episode_ids=(a,b),confidence=1)


def _helpers():
    pytest.importorskip("psycopg")
    from test_phase2_cognition import actor,call,seed,platform,claim
    from test_phase2_memory import turn
    return actor,call,seed,platform,claim,turn


def _episode(dsn,user,instance,text,*,topic="exercise"):
    """Existing 05C episode fixture, with a topic and interpretation for this case."""
    import psycopg
    from test_mvp05c3_entity_evidence import _episode as fixture_episode
    source,episode_id=fixture_episode(dsn,user,instance,text)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE memory_episodes SET topic=%s,description=%s WHERE episode_id=%s",
                     (topic,"User reported: "+text,episode_id))
    return source,episode_id


def _setup(dsn,*,other=False):
    _,_,seed,_,_,_=_helpers()
    user,instance,alternate=seed(dsn,other=other)
    evidence=tuple(_episode(dsn,user,instance,text) for text in REPORTS[:3])
    return user,instance,alternate,evidence


def _schedule(dsn,user,instance):
    from persona_agent.cognitive_engine import schedule_cognition
    actor,call,_,platform,claim,turn=_helpers()
    conv=uuid4()
    for i in range(4):
        turn(dsn,user,instance,conv,f"I have been thinking about exercise, discussion {i}.")
    scheduled=call(dsn,schedule_cognition,platform(user),conv,instance,1)
    lease=claim(dsn)
    assert lease.operation_id==scheduled.operation_id
    return lease


def _proposal(support,counter=()):
    return PatternProposal(topic="exercise",hypothesis=HYPOTHESIS,
                           supporting_episode_ids=tuple(support),counter_episode_ids=tuple(counter))


class PatternGateway:
    def __init__(self,proposal,*,reflection=False,mutate=None):
        self.proposal=proposal
        self.reflection=reflection
        self.mutate=mutate
        self.calls=0
        self.visible=()

    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        self.calls+=1
        if len(messages)>2:
            assert messages[-2].role=="user"
            episodes=json.loads(messages[-2].content)["historical_episode_data"]
            self.visible=tuple(row["episode_id"] for row in episodes)
            assert all(row["source_turn_id"] and row["user_quote"] for row in episodes)
        current=json.loads(messages[-1].content)
        proposals=[]
        if self.reflection:
            proposals.append({"kind":"OBSERVATION","subject":"USER",
                              "content":"Exercise was discussed.",
                              "source_turn_id":current[0]["turn_id"],
                              "evidence_quote":"thinking about exercise","confidence":0.7})
        if self.mutate:
            self.mutate()
        return ModelReply(model_id="meta-episode-test",text=json.dumps({
            "proposals":proposals,"patterns":[self.proposal.model_dump(mode="json")],
        }))


def _run(dsn,user,instance,proposal,**kwargs):
    from persona_agent.cognitive_engine import run_cognition
    _,call,_,_,_,_=_helpers()
    lease=_schedule(dsn,user,instance)
    gateway=PatternGateway(proposal,**kwargs)
    result=call(dsn,run_cognition,lease,gateway)
    assert gateway.calls==1
    return lease,result,gateway


def test_pattern_persists_support_counter_and_scoped_recall(dsn):
    import psycopg
    from persona_agent.meta_episodic import search_episode_patterns,FALLBACK
    from persona_agent.conversation import prepare_text_context,respond_to_text
    from persona_agent.conversation_contracts import ModelReply
    from persona_agent.runtime import PermissionDenied
    actor,call,seed,_,_,turn=_helpers()
    user,instance,other,evidence=_setup(dsn,other=True)
    outsider,foreign,_=seed(dsn)
    foreign_source,foreign_episode=_episode(dsn,outsider,foreign,REPORTS[3])
    _,unrelated=_episode(dsn,user,instance,"I bought a new camera.",topic="camera")
    a,b,c=[eid for _,eid in evidence]
    lease,result,gateway=_run(dsn,user,instance,_proposal((a,b),(c,)))
    assert result.pattern_count==1 and result.reflection_count==0
    assert set(gateway.visible)=={str(a),str(b),str(c)}
    assert str(foreign_episode) not in gateway.visible and str(unrelated) not in gateway.visible

    hits=call(dsn,search_episode_patterns,actor(user),instance,"exercise")
    assert len(hits)==1
    hit=hits[0]
    assert hit.version==1 and hit.epistemic_status=="MODEL_INTERPRETED"
    assert hit.model_id=="meta-episode-test"
    assert {ep.episode_id for ep in hit.supporting}=={a,b}
    assert {ep.episode_id for ep in hit.counter}=={c}
    assert {ep.source_quote for ep in hit.supporting+hit.counter}==set(REPORTS[:3])
    assert call(dsn,search_episode_patterns,actor(user),other,"exercise")==()
    assert call(dsn,search_episode_patterns,actor(user),instance,"camera")==()
    with pytest.raises(PermissionDenied):
        call(dsn,search_episode_patterns,actor(outsider),instance,"exercise")
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM memory_records WHERE instance_id=%s",
                            (instance,)).fetchone()[0]==0
        assert conn.execute(
            """SELECT count(*) FROM events e JOIN outbox o USING(event_id)
               WHERE e.instance_id=%s AND e.event_type='memory.meta_episode.patterns.admitted.v1'
                 AND e.trust_level='TRUSTED_PLATFORM'""",(instance,),
        ).fetchone()[0]==1

    request=turn(dsn,user,instance,uuid4(),QUESTION)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert ctx.historical_fence is not None and ctx.pattern_history.status=="KNOWN"
    assert ctx.pattern_history.pattern_refs==((hit.pattern_id,1),)
    assert set(ctx.pattern_history.source_turn_ids)=={src.turn_id for src,_ in evidence}
    prompt=ctx.messages[0].content
    assert HYPOTHESIS in prompt and "MODEL_INTERPRETED" in prompt
    assert all(report in prompt for report in REPORTS[:3])
    assert '"counter":' in prompt and str(foreign_source.turn_id) not in prompt
    assert "new camera" not in prompt
    assert len(ctx.messages)==2

    class RecallGateway:
        def generate(self,messages):
            return ModelReply(model_id="recall-test",text="Overtime may be an obstacle, though you have exercised after overtime too.")

    reply=call(dsn,respond_to_text,actor(user),request.turn_id,RecallGateway())
    assert "may be" in reply.text
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT payload->>'status',payload->'patterns' FROM events
               WHERE instance_id=%s AND event_type='conversation.response.pattern_context.v1'
                 AND payload->>'response_id'=%s""",(instance,str(reply.response_id)),
        ).fetchone()==("KNOWN",[[str(hit.pattern_id),1]])
    unknown=turn(dsn,user,instance,uuid4(),"What patterns have you noticed about cooking?")
    reply=call(dsn,respond_to_text,actor(user),unknown.turn_id,RecallGateway())
    assert reply.text==FALLBACK


def test_pattern_replay_dedup_and_version_retains_counter(dsn):
    import psycopg
    from persona_agent.cognitive_engine import prepare_cognition,commit_cognition,run_cognition
    from persona_agent.cognitive_contracts import CognitiveDraft
    from persona_agent.meta_episodic import search_episode_patterns
    actor,call,_,_,_,_=_helpers()
    user,instance,_,evidence=_setup(dsn)
    a,b,c=[eid for _,eid in evidence]
    lease=_schedule(dsn,user,instance)
    work=call(dsn,prepare_cognition,lease)
    first=call(dsn,commit_cognition,lease,work,
               CognitiveDraft(patterns=(_proposal((a,b),(c,)),)),"meta-episode-test")
    assert first.pattern_count==1
    class NoRepeat:
        def generate(self,messages):
            raise AssertionError("committed cognition must replay without inference")
    replay=call(dsn,run_cognition,lease,NoRepeat())
    assert replay.replayed and replay.pattern_count==1
    assert _run(dsn,user,instance,_proposal((b,a),(c,)))[1].pattern_count==0
    _,d=_episode(dsn,user,instance,REPORTS[3])
    assert _run(dsn,user,instance,_proposal((a,b,d)))[1].pattern_count==0
    assert _run(dsn,user,instance,_proposal((a,b,d),(c,)))[1].pattern_count==1
    hit,=call(dsn,search_episode_patterns,actor(user),instance,"exercise")
    assert hit.version==2 and {ep.episode_id for ep in hit.supporting}=={a,b,d}
    assert {ep.episode_id for ep in hit.counter}=={c}
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT version FROM memory_episode_patterns WHERE instance_id=%s ORDER BY version",
            (instance,),
        ).fetchall()==[(1,),(2,)]
        # Changing an episode used only by the latest version cannot revive v1.
        conn.execute("UPDATE memory_episodes SET description='Revised interpretation' WHERE episode_id=%s",(d,))
    assert call(dsn,search_episode_patterns,actor(user),instance,"exercise")==()


@pytest.mark.parametrize("change",["SUPPORT_CORRECTION","COUNTER_CORRECTION","COUNTER_MUTATION"])
def test_pattern_eligibility_and_foreground_fence(dsn,change):
    import psycopg
    from persona_agent.meta_episodic import search_episode_patterns,FALLBACK
    from persona_agent.memory import admit_memory,amend_memory
    from test_phase2_memory import candidate,correction
    from persona_agent.conversation import prepare_text_context,commit_text_response,respond_to_text
    from persona_agent.conversation_contracts import ModelReply,StaleHistoricalContext
    actor,call,_,_,_,turn=_helpers()
    user,instance,_,evidence=_setup(dsn)
    a,b,c=[eid for _,eid in evidence]
    index=0 if change=="SUPPORT_CORRECTION" else 2
    source,eid=evidence[index]
    saved=call(dsn,admit_memory,actor(user),candidate(
        instance,source.conversation_id,source,kind="FACT",predicate="exercise_report",
        value="reported",quote=REPORTS[index],
    ))
    assert _run(dsn,user,instance,_proposal((a,b),(c,)))[1].pattern_count==1
    request=turn(dsn,user,instance,uuid4(),QUESTION)
    ctx=call(dsn,prepare_text_context,actor(user),request.turn_id)
    assert ctx.pattern_history.status=="KNOWN"
    if change=="COUNTER_MUTATION":
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE memory_episodes SET description='Corrected episode' WHERE episode_id=%s",(eid,))
    else:
        text="Actually, I misremembered that exercise report."
        revised=turn(dsn,user,instance,uuid4(),text)
        call(dsn,amend_memory,actor(user),correction(
            instance,revised.conversation_id,revised,saved.memory_id,
            value="incorrect",quote=text,
        ))
    assert call(dsn,search_episode_patterns,actor(user),instance,"exercise")==()
    with pytest.raises(StaleHistoricalContext):
        call(dsn,commit_text_response,actor(user),ctx,ModelReply(
            model_id="stale",text=HYPOTHESIS,
        ),model_generated=True)
    class Guessing:
        def generate(self,messages):
            return ModelReply(model_id="guess",text=HYPOTHESIS)
    assert call(dsn,respond_to_text,actor(user),request.turn_id,Guessing()).text==FALLBACK
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM memory_episode_patterns WHERE instance_id=%s",
                            (instance,)).fetchone()[0]==1


@pytest.mark.parametrize("change",["STALE","UNSUPPLIED"])
def test_optional_invalid_pattern_does_not_discard_reflection(dsn,change):
    import psycopg
    from persona_agent.meta_episodic import search_episode_patterns
    actor,call,seed,_,_,_=_helpers()
    user,instance,_,evidence=_setup(dsn)
    a,b,c=[eid for _,eid in evidence]
    mutate=None
    if change=="STALE":
        def mutate():
            # Unrelated scoped evidence change invalidates the captured fence
            # even though the proposed evidence is still individually eligible.
            _episode(dsn,user,instance,"I bought a camera.",topic="camera")
    else:
        stranger,foreign,_=seed(dsn)
        _,b=_episode(dsn,stranger,foreign,REPORTS[3])
    lease,result,_=_run(dsn,user,instance,_proposal((a,b),(c,)),reflection=True,mutate=mutate)
    assert result.pattern_count==0 and result.reflection_count==1
    assert call(dsn,search_episode_patterns,actor(user),instance,"exercise")==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM cognitive_reflections WHERE operation_id=%s",(lease.operation_id,),
        ).fetchone()[0]==1
        assert conn.execute(
            """SELECT count(*) FROM events WHERE instance_id=%s
               AND event_type='memory.meta_episode.patterns.admitted.v1'""",(instance,),
        ).fetchone()[0]==0


def test_duplicate_source_reports_do_not_count_twice(dsn):
    import psycopg
    from persona_agent.cognitive_engine import prepare_cognition,commit_cognition,run_cognition
    from persona_agent.cognitive_contracts import CognitiveDraft
    _,call,seed,_,_,_=_helpers()
    user,instance,_=seed(dsn)
    _,a=_episode(dsn,user,instance,REPORTS[0])
    _,b=_episode(dsn,user,instance,REPORTS[0])
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            """INSERT INTO memory_episodes(
                   episode_id,summary_id,conversation_id,user_id,instance_id,source_turn_id,
                   source_sequence,source_quote,source_turn_at,topic,description,confidence
               ) SELECT %s,summary_id,conversation_id,user_id,instance_id,source_turn_id,
                        source_sequence,source_quote,source_turn_at,topic,description,confidence
                 FROM memory_episodes WHERE episode_id=%s""",(uuid4(),a),
        )
    lease=_schedule(dsn,user,instance)
    work=call(dsn,prepare_cognition,lease)
    assert work.pattern_candidates==() and work.pattern_fence is None
    result=call(dsn,commit_cognition,lease,work,
                CognitiveDraft(patterns=(_proposal((a,b)),)),"meta-episode-test")
    assert result.pattern_count==0
    class NoRepeat:
        def generate(self,messages):
            raise AssertionError("committed operation must replay")
    assert call(dsn,run_cognition,lease,NoRepeat()).replayed



@pytest.mark.parametrize("during_inference",[False,True])
def test_reflection_depends_on_every_supplied_episode_snapshot(dsn,during_inference):
    import psycopg
    from persona_agent.cognitive_contracts import StaleCognition,CognitiveDraft,CognitiveProposal
    from persona_agent.cognitive_engine import (
        prepare_cognition,commit_cognition,run_cognition,list_cognitive_proposals,
    )
    from persona_agent.operation_control import fail_operation
    from test_phase2_cognition import cancel_pending
    actor,call,_,_,_,_=_helpers()
    user,instance,_,evidence=_setup(dsn)
    lease=_schedule(dsn,user,instance)
    work=call(dsn,prepare_cognition,lease)
    assert len(work.pattern_candidates)==3
    target=evidence[2][1]
    assert target in {item.episode_id for item in work.pattern_candidates}
    draft=CognitiveDraft(proposals=(CognitiveProposal(
        kind="OBSERVATION",subject="USER",content="Exercise was discussed.",
        source_turn_id=work.turns[0]["turn_id"],evidence_quote="thinking about exercise",
        confidence=0.8),))

    def mutate():
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE memory_episodes SET description='Corrected episode' WHERE episode_id=%s",
                         (target,))
    if during_inference:
        mutate()
        with pytest.raises(StaleCognition,match="episode"):
            call(dsn,commit_cognition,lease,work,draft,"stale-episode")
        assert call(dsn,list_cognitive_proposals,actor(user),instance)==()
        with psycopg.connect(dsn,autocommit=True) as conn:
            assert conn.execute("SELECT cognitive_watermark FROM conversations WHERE conversation_id=%s",
                                (work.conversation_id,)).fetchone()==(0,)
        call(dsn,fail_operation,lease,reason="test stale episode",delay_seconds=5)
        cancel_pending(dsn,user,lease.operation_id)
    else:
        call(dsn,commit_cognition,lease,work,draft,"episode-lineage")
        # A committed receipt must replay metadata without a second model call.
        call(dsn,run_cognition,lease,PatternGateway(_proposal(tuple(eid for _,eid in evidence[:2]))))
        assert len(call(dsn,list_cognitive_proposals,actor(user),instance))==1
        with psycopg.connect(dsn,autocommit=True) as conn:
            ids,hashes=conn.execute("SELECT source_episode_ids,source_episode_hashes FROM cognitive_jobs WHERE operation_id=%s",
                                   (lease.operation_id,)).fetchone()
            assert set(ids)=={eid for _,eid in evidence}
            assert len(hashes)==3 and all(len(h)==64 for h in hashes)
        mutate()
        assert call(dsn,list_cognitive_proposals,actor(user),instance)==()


@pytest.mark.parametrize("dependency",["UNCITED_EPISODE","RAW_TURN"])
def test_pattern_recall_checks_complete_cognitive_input_set(dsn,dependency):
    import psycopg
    from persona_agent.cognitive_engine import prepare_cognition,commit_cognition,run_cognition
    from persona_agent.cognitive_contracts import CognitiveDraft
    from persona_agent.meta_episodic import search_episode_patterns
    from workflow_evidence_helpers import link_memory,invalidate
    actor,call,_,_,_,_=_helpers()
    user,instance,_,evidence=_setup(dsn)
    a,b,c=[eid for _,eid in evidence]
    lease=_schedule(dsn,user,instance)
    initial=call(dsn,prepare_cognition,lease)
    assert {ep.episode_id for ep in initial.pattern_candidates}=={a,b,c}
    saved=None
    if dependency=="RAW_TURN":
        # A source in the raw prompt need not appear in the pattern citations.
        saved=link_memory(dsn,user,instance,initial.conversation_id,initial.turns[0]["turn_id"])
    work=call(dsn,prepare_cognition,lease)
    result=call(dsn,commit_cognition,lease,work,
                CognitiveDraft(patterns=(_proposal((a,b)),)),"whole-pattern-input")
    assert result.pattern_count==1
    call(dsn,run_cognition,lease,PatternGateway(_proposal((a,b))))
    hit,=call(dsn,search_episode_patterns,actor(user),instance,"exercise")
    assert {ep.episode_id for ep in hit.supporting}=={a,b}
    assert hit.counter==()
    if dependency=="UNCITED_EPISODE":
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute("UPDATE memory_episodes SET description='Revised uncited input' WHERE episode_id=%s",(c,))
    else:
        invalidate(dsn,user,instance,saved,"EXPIRE")
    assert call(dsn,search_episode_patterns,actor(user),instance,"exercise")==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM memory_episode_patterns WHERE operation_id=%s",
                            (lease.operation_id,)).fetchone()==(1,)
        assert conn.execute("SELECT count(*) FROM memory_episode_pattern_evidence WHERE pattern_id=%s",
                            (hit.pattern_id,)).fetchone()==(2,)
