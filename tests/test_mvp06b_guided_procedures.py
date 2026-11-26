"""MVP-06B: deterministic guidance, real PostgreSQL, synthetic Nginx run.

No command below is executed. All outcomes are authenticated user testimony.
"""
from uuid import uuid4

import pytest

from persona_agent.procedure_guidance_contracts import parse_guidance,ROUTE_ID

TITLE="Nginx self-signed TLS"
START="Guide me through procedure: "+TITLE+"."
CONTINUE="Continue procedure: "+TITLE+"."
RESUME="Resume procedure: "+TITLE+"."
DONE="I completed this step."
CONFIRM="I confirm this step passed its check."


@pytest.mark.parametrize("text,kind",[
    (START,"NEXT"),(CONTINUE,"NEXT"),(RESUME,"RESUME"),
    ("Next.","NEXT"),("Repeat.","REPEAT"),("Back.","BACK"),("Skip.","SKIP"),
    ("Clarify.","CLARIFY"),("Pause.","PAUSE"),("Resume.","RESUME"),
    ("Next step.","NEXT"),("Repeat this step.","REPEAT"),
    ("Previous step.","BACK"),("Skip this step.","SKIP"),
    ("Clarify this step.","CLARIFY"),("Pause this procedure.","PAUSE"),
    ("Confirm procedure pause.","CONFIRM_PAUSE"),("Resume this procedure.","RESUME"),
    ("Confirm procedure resume.","CONFIRM_RESUME"),("Cancel this procedure.","CANCEL"),
    ("Confirm procedure cancellation.","CONFIRM_CANCEL"),
    ("I tried this step.","ATTEMPTED"),(DONE,"USER_REPORTED_DONE"),(CONFIRM,"USER_CONFIRMED"),
    ("This step failed: Nginx reports a missing certificate file.","FAILED"),
    ("This step is blocked: I do not have the necessary file permissions.","BLOCKED"),
    ("What if I completed this step?",None),("Done.",None),("Yes.",None),
    ('"I completed this step."',None),("Next step?",None),
    ("I completed this step. Skip the check.",None),
    ("Tell me about the next Nginx version.",None),
])
def test_guidance_grammar(text,kind):
    parsed=parse_guidance(text)
    assert (parsed.kind if parsed else None)==kind


def helpers():
    pytest.importorskip("psycopg")
    from test_phase3_procedures import actor,call,seed,turn
    return actor,call,seed,turn


def nginx_definition(*,suffix="",agent=False):
    from persona_agent.procedure_contracts import ProcedureDefinition,ProcedureStep
    steps=(
        ProcedureStep(step_id="generate",executor="USER",
            instruction=("In a protected local test directory, generate the certificate and key: "
                "openssl req -x509 -newkey rsa:2048 -nodes -days 30 -keyout tls.key -out tls.crt "
                "-subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost'. "
                "Keep the key readable only by the authorized service account."+suffix),
            success_test="Inspect the certificate and verify its localhost subject alternative name and validity"),
        ProcedureStep(step_id="configure",executor="USER",
            instruction=("In the local test server block use listen 443 ssl; "
                "set server_name localhost and point ssl_certificate and ssl_certificate_key "
                "to the absolute paths of tls.crt and tls.key. Do not expose this test endpoint publicly."),
            success_test="The TLS configuration refers to the intended files and correct hostname"),
        ProcedureStep(step_id="test_reload",executor="USER",
            instruction="Run nginx -t, and reload Nginx only if that test succeeds.",
            success_test="The configuration test passes and the reload succeeds"),
        ProcedureStep(step_id="verify",executor="AGENT" if agent else "USER",
            capability="local.tls.verify" if agent else None,
            instruction="Verify the local HTTPS endpoint with the expected certificate.",
            success_test="A TLS handshake succeeds with the intended self-signed certificate"),
    )
    return ProcedureDefinition(title=TITLE,purpose="Local HTTPS development",
        applicability="Local test Nginx instance",limitations="Not for public trust",
        policy="STABLE",preconditions=("Nginx and OpenSSL are installed",),
        steps=steps,success_tests=("The local HTTPS endpoint negotiates TLS",))


def setup(dsn,*,agent=False):
    actor,call,_,_=helpers()
    from test_phase3_runs import published,start_command
    from persona_agent.procedure_run_state import start_procedure_run
    user,persona,instance,conv,template=published(dsn,defined=nginx_definition(agent=agent))
    command=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),command)
    return user,persona,instance,conv,template,command.run_id


class NoModel:
    def generate(self,messages):
        raise AssertionError("bounded guided reply must not invoke inference")


class CasualModel:
    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        return ModelReply("casual-test","We can discuss gardening.")


class MisleadingModel:
    def generate(self,messages):
        from persona_agent.conversation_contracts import ModelReply
        return ModelReply("untrusted-test","I executed all commands and verified the whole procedure.")


def answer(dsn,user,instance,conv,text,*,deliver=True,model=None,generation=1,forced=False):
    actor,call,_,turn=helpers()
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation import ingest_text,respond_to_text,acknowledge_text_delivery
    from persona_agent.conversation_contracts import TextInputCommand
    request=call(dsn,ingest_text,actor(user),TextInputCommand(
        instance_id=instance,conversation_id=conv,expected_generation=generation,
        idempotency_key=uuid4().hex,text=text,
    ))
    reply=call(dsn,respond_to_text if forced else respond_to_user_text,
        actor(user),request.turn_id,model or NoModel())
    if deliver:
        call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    return request,reply


def view(dsn,user,instance,run):
    actor,call,_,_=helpers()
    from persona_agent.procedure_run_state import read_procedure_run
    return call(dsn,read_procedure_run,actor(user),instance,run)


def statuses(dsn,user,instance,run):
    return [s.status for s in view(dsn,user,instance,run).steps]


def test_nginx_guided_run_preserves_progress_pin_and_restart(dsn):
    import psycopg
    from persona_agent.procedure_state import publish_shared_procedure
    from test_phase3_procedures import publication
    actor,call,_,_=helpers()
    user,persona,instance,conv,template,run=setup(dsn)
    source,reply=answer(dsn,user,instance,conv,START,deliver=False)
    assert reply.model_id==ROUTE_ID and "Step 1" in reply.text
    assert statuses(dsn,user,instance,run)==["NOT_STARTED"]*4
    from persona_agent.conversation import acknowledge_text_delivery
    call(dsn,acknowledge_text_delivery,actor(user),reply.response_id)
    assert statuses(dsn,user,instance,run)==["INSTRUCTED"]+["NOT_STARTED"]*3
    before=view(dsn,user,instance,run)
    assert call(dsn,acknowledge_text_delivery,actor(user),reply.response_id).replayed
    assert view(dsn,user,instance,run)==before

    _,repeat=answer(dsn,user,instance,conv,"Repeat this step.")
    assert "Step 1" in repeat.text and before.steps[0].step.instruction in repeat.text
    _,skip=answer(dsn,user,instance,conv,"Skip this step.")
    assert "cannot be marked complete" in skip.text
    assert statuses(dsn,user,instance,run)[0]=="INSTRUCTED"
    answer(dsn,user,instance,conv,"I tried this step.")
    assert statuses(dsn,user,instance,run)[0]=="ATTEMPTED"
    answer(dsn,user,instance,conv,"This step failed: The certificate file was not created.")
    assert statuses(dsn,user,instance,run)[0]=="FAILED"
    _,report=answer(dsn,user,instance,conv,DONE)
    assert "user-reported done, not independently verified" in report.text
    assert statuses(dsn,user,instance,run)[0]=="USER_REPORTED_DONE"
    assert view(dsn,user,instance,run).next_step_id=="generate"

    # Interruption, topic switch, publication of a newer STABLE template,
    # fresh connection and a fresh conversation do not advance the old run.
    answer(dsn,user,instance,conv,"Let's talk about gardening.",model=CasualModel())
    call(dsn,publish_shared_procedure,actor(user),publication(
        dsn,user,persona,instance,conv,template,1,nginx_definition(suffix=" Revised wording."),
    ))
    fresh=uuid4()
    _,resumed=answer(dsn,user,instance,fresh,CONTINUE)
    assert "user-reported done" in resumed.text and "localhost" in resumed.text
    assert view(dsn,user,instance,run).template_version==1
    assert view(dsn,user,instance,run).next_step_id=="generate"
    # New session requires actual fresh user evidence, never manufactured.
    _,not_confirmed=answer(dsn,user,instance,fresh,CONFIRM)
    assert "user-reported done" in not_confirmed.text
    assert statuses(dsn,user,instance,run)[0]=="USER_REPORTED_DONE"
    answer(dsn,user,instance,fresh,DONE)
    _,next_step=answer(dsn,user,instance,fresh,CONFIRM)
    assert "Step 2" in next_step.text
    assert statuses(dsn,user,instance,run)==["USER_CONFIRMED","INSTRUCTED","NOT_STARTED","NOT_STARTED"]

    _,back=answer(dsn,user,instance,fresh,"Previous step.")
    assert "Review step 1" in back.text and "Review only" in back.text
    assert statuses(dsn,user,instance,run)[0]=="USER_CONFIRMED"
    _,clarify=answer(dsn,user,instance,fresh,"Clarify this step.")
    assert "Review step 1" in clarify.text
    _,next_step=answer(dsn,user,instance,fresh,"Next step.")
    assert "Step 2" in next_step.text
    assert view(dsn,user,instance,run).next_step_id=="configure"

    _,challenge=answer(dsn,user,instance,fresh,"Pause this procedure.")
    assert "Confirm procedure pause." in challenge.text
    assert view(dsn,user,instance,run).status=="ACTIVE"
    answer(dsn,user,instance,fresh,"Confirm procedure pause.")
    assert view(dsn,user,instance,run).status=="PAUSED"
    _,paused=answer(dsn,user,instance,fresh,"Next step.")
    assert "is paused" in paused.text
    new=uuid4()
    _,challenge=answer(dsn,user,instance,new,RESUME)
    assert "Confirm procedure resume." in challenge.text
    assert view(dsn,user,instance,run).status=="PAUSED"
    _,resumed=answer(dsn,user,instance,new,"Confirm procedure resume.")
    assert "Step 2" in resumed.text
    assert view(dsn,user,instance,run).status=="ACTIVE"

    # Every remaining step needs a report and a separate user confirmation.
    for expected in ("test_reload","verify",None):
        answer(dsn,user,instance,new,DONE)
        answer(dsn,user,instance,new,CONFIRM)
        assert view(dsn,user,instance,run).next_step_id==expected
    final=view(dsn,user,instance,run)
    assert final.status=="ACTIVE" and final.template_version==1
    assert all(s.status=="USER_CONFIRMED" for s in final.steps)
    assert not any(s.independently_verified for s in final.steps)
    assert final.task_completion_changed is False and final.operation_dispatch_authorized is False
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s AND instance_id=%s",
            (user,instance),
        ).fetchone()==(0,)
        assert conn.execute(
            """SELECT count(*) FROM procedure_run_step_events
               WHERE run_id=%s AND step_id='generate' AND kind='USER_REPORTED_DONE'""",
            (run,),
        ).fetchone()==(2,)


def test_undelivered_and_late_guidance_cannot_admit_report(dsn):
    actor,call,_,turn=helpers()
    from persona_agent.conversation import acknowledge_text_delivery
    user,_,instance,conv,_,run=setup(dsn)
    _,guidance=answer(dsn,user,instance,conv,START,deliver=False)
    _,reply=answer(dsn,user,instance,conv,DONE)
    assert "can't identify" in reply.text
    assert statuses(dsn,user,instance,run)[0]=="NOT_STARTED"
    call(dsn,acknowledge_text_delivery,actor(user),guidance.response_id)
    assert statuses(dsn,user,instance,run)[0]=="NOT_STARTED"


def test_scope_and_same_title_ambiguity_abstain(dsn):
    actor,call,seed,_=helpers()
    from test_phase3_runs import start_command
    from persona_agent.procedure_run_state import start_procedure_run
    user,_,instance,conv,template,run=setup(dsn)
    foreign,_,other=seed(dsn)
    _,reply=answer(dsn,foreign,other,uuid4(),CONTINUE)
    assert "can't identify" in reply.text
    duplicate=start_command(dsn,user,instance,conv,template)
    call(dsn,start_procedure_run,actor(user),duplicate)
    _,reply=answer(dsn,user,instance,uuid4(),CONTINUE)
    assert "can't identify" in reply.text
    assert statuses(dsn,user,instance,run)[0]=="NOT_STARTED"
    assert statuses(dsn,user,instance,duplicate.run_id)[0]=="NOT_STARTED"
    _,identified=answer(dsn,user,instance,uuid4(),f"Continue procedure: {run}.")
    assert "Step 1" in identified.text
    assert statuses(dsn,user,instance,run)[0]=="INSTRUCTED"
    assert statuses(dsn,user,instance,duplicate.run_id)[0]=="NOT_STARTED"


def test_typed_progress_invalidates_delivered_report_binding(dsn):
    from test_phase3_runs import record
    user,_,instance,conv,_,run=setup(dsn)
    answer(dsn,user,instance,conv,START)
    before=view(dsn,user,instance,run)
    # Another session updates the same step before the next report.
    record(dsn,user,instance,uuid4(),run,before.version,"generate",
        before.steps[0].version,"ATTEMPTED")
    _,reply=answer(dsn,user,instance,conv,DONE)
    assert "tracked progress changed" in reply.text
    assert statuses(dsn,user,instance,run)[0]=="ATTEMPTED"


def test_status_challenge_requires_delivered_adjacent_confirmation(dsn):
    user,_,instance,conv,_,run=setup(dsn)
    answer(dsn,user,instance,conv,START)
    answer(dsn,user,instance,conv,"Pause this procedure.",deliver=False)
    answer(dsn,user,instance,conv,"Confirm procedure pause.")
    assert view(dsn,user,instance,run).status=="ACTIVE"
    answer(dsn,user,instance,conv,CONTINUE)
    answer(dsn,user,instance,conv,"Cancel this procedure.")
    answer(dsn,user,instance,conv,"Yes.",model=CasualModel())
    answer(dsn,user,instance,conv,"Confirm procedure cancellation.")
    assert view(dsn,user,instance,run).status=="ACTIVE"
    answer(dsn,user,instance,conv,CONTINUE)
    answer(dsn,user,instance,conv,"Cancel this procedure.")
    answer(dsn,user,instance,conv,"Confirm procedure cancellation.")
    assert view(dsn,user,instance,run).status=="CANCELLED"


def test_forced_model_cannot_claim_completion_or_execute_agent_step(dsn):
    user,_,instance,conv,_,run=setup(dsn,agent=True)
    _,reply=answer(dsn,user,instance,conv,START,model=MisleadingModel(),forced=True)
    assert reply.model_id=="untrusted-test" and "Step 1" in reply.text
    assert "executed all commands" not in reply.text
    for _ in range(3):
        answer(dsn,user,instance,conv,DONE)
        _,reply=answer(dsn,user,instance,conv,CONFIRM)
    assert "authorized agent execution" in reply.text
    assert statuses(dsn,user,instance,run)==["USER_CONFIRMED"]*3+["NOT_STARTED"]
    answer(dsn,user,instance,conv,DONE)
    assert statuses(dsn,user,instance,run)[-1]=="NOT_STARTED"


def test_concurrent_duplicate_step_confirmation_creates_single_event(dsn):
    import psycopg
    from concurrent.futures import ThreadPoolExecutor
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation_contracts import StaleConversation
    actor,call,_,turn=helpers()
    user,_,instance,conv,_,run=setup(dsn)
    answer(dsn,user,instance,conv,START)
    answer(dsn,user,instance,conv,DONE)
    request=turn(dsn,user,instance,conv,CONFIRM)
    def respond(_):
        try:
            return call(dsn,respond_to_user_text,actor(user),request.turn_id,NoModel())
        except StaleConversation:
            return call(dsn,respond_to_user_text,actor(user),request.turn_id,NoModel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b=list(pool.map(respond,range(2)))
    assert a.response_id==b.response_id
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM procedure_run_step_events WHERE run_id=%s
               AND step_id='generate' AND kind='USER_CONFIRMED'""",(run,),
        ).fetchone()==(1,)
    assert view(dsn,user,instance,run).next_step_id=="configure"


def test_persona_switch_invalidates_old_step_confirmation(dsn):
    import psycopg
    from persona_agent.lifecycle import SwitchPersonaCommand,switch_persona
    from persona_agent.conversation import ingest_text
    from persona_agent.conversation_contracts import TextInputCommand
    actor,call,_,_=helpers()
    user,_,instance,conv,_,run=setup(dsn)
    answer(dsn,user,instance,conv,START)
    answer(dsn,user,instance,conv,DONE)
    persona,other=uuid4(),uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO persona_definitions(id,stable_key) VALUES(%s,%s)",
                         (persona,"guide-other-"+persona.hex))
            conn.execute("INSERT INTO persona_instances(id,user_id,persona_id,label) VALUES(%s,%s,%s,'Other')",
                         (other,user,persona))
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=instance,target_instance_id=other,
        expected_generation=1,idempotency_key=uuid4().hex,
    ))
    _,other_reply=answer(dsn,user,other,uuid4(),CONTINUE,generation=2)
    assert "can't identify" in other_reply.text
    call(dsn,switch_persona,actor(user),SwitchPersonaCommand(
        expected_instance_id=other,target_instance_id=instance,
        expected_generation=2,idempotency_key=uuid4().hex,
    ))
    _,stale=answer(dsn,user,instance,conv,CONFIRM,generation=3)
    assert "can't identify" in stale.text
    assert statuses(dsn,user,instance,run)[0]=="USER_REPORTED_DONE"
    assert view(dsn,user,instance,run).next_step_id=="generate"


@pytest.mark.parametrize("kind,prefix",[
    ("FAILED","This step failed: "),
    ("BLOCKED","This step is blocked: "),
])
def test_same_status_obstacle_reports_append_distinct_evidence(dsn,kind,prefix):
    import psycopg
    from persona_agent.response_orchestrator import respond_to_user_text
    from persona_agent.conversation import acknowledge_text_delivery
    from persona_agent.procedure_run_contracts import RecordProcedureStepCommand
    from persona_agent.procedure_run_state import record_procedure_step
    from persona_agent.runtime import VersionConflict
    actor,call,_,_=helpers()
    user,_,instance,conv,_,run=setup(dsn)
    answer(dsn,user,instance,conv,START)
    first_detail="The certificate directory does not exist."
    second_detail="The destination file cannot be opened."
    first,first_reply=answer(dsn,user,instance,conv,prefix+first_detail)
    before=view(dsn,user,instance,run)
    second,second_reply=answer(dsn,user,instance,conv,prefix+second_detail)
    after=view(dsn,user,instance,run)
    assert first_reply.text.startswith("Obstacle recorded;")
    assert second_reply.text.startswith("Obstacle recorded;")
    assert after.status==before.status=="ACTIVE"
    assert after.next_step_id==before.next_step_id=="generate"
    assert after.steps[0].status==before.steps[0].status==kind
    assert after.version==before.version+1
    assert after.steps[0].version==before.steps[0].version+1
    assert after.steps[0].last_step_event_id!=before.steps[0].last_step_event_id
    assert after.steps[1:]==before.steps[1:]
    # Replaying the same response or acknowledgment does not append evidence.
    replay=call(dsn,respond_to_user_text,actor(user),second.turn_id,NoModel())
    assert replay.replayed and replay.response_id==second_reply.response_id
    assert call(dsn,acknowledge_text_delivery,actor(user),second_reply.response_id).replayed
    # A new owner receipt with a reused report cannot fabricate a second
    # observation even when it supplies the latest versions.
    duplicate=RecordProcedureStepCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=second.turn_id,
        evidence_quote=prefix+second_detail,run_id=run,
        expected_run_version=after.version,step_id="generate",
        expected_step_version=after.steps[0].version,outcome=kind,detail=second_detail,
    )
    with pytest.raises(VersionConflict,match="obstacle evidence already recorded"):
        call(dsn,record_procedure_step,actor(user),duplicate)
    assert view(dsn,user,instance,run)==after
    # Identical wording on another authenticated turn is still fresh evidence.
    third,third_reply=answer(dsn,user,instance,conv,prefix+second_detail)
    assert third_reply.text.startswith("Obstacle recorded;")
    final=view(dsn,user,instance,run)
    assert final.version==after.version+1
    assert final.next_step_id=="generate"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT old_status,new_status,detail,source_turn_id,source_quote
               FROM procedure_run_step_events WHERE run_id=%s AND step_id='generate'
                 AND kind=%s ORDER BY step_version""",
            (run,kind),
        ).fetchall()==[
            ("INSTRUCTED",kind,first_detail,first.turn_id,prefix+first_detail),
            (kind,kind,second_detail,second.turn_id,prefix+second_detail),
            (kind,kind,second_detail,third.turn_id,prefix+second_detail),
        ]
        assert conn.execute(
            """SELECT count(*) FROM procedure_run_step_events s
               JOIN outbox o ON o.event_id=s.event_id
               WHERE s.run_id=%s AND s.step_id='generate' AND s.kind=%s""",
            (run,kind),
        ).fetchone()==(3,)
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE user_id=%s AND instance_id=%s",
            (user,instance),
        ).fetchone()==(0,)


def test_repeated_attempt_does_not_claim_new_evidence(dsn):
    _,_,_,_=helpers()
    user,_,instance,conv,_,run=setup(dsn)
    answer(dsn,user,instance,conv,START)
    answer(dsn,user,instance,conv,"I tried this step.")
    before=view(dsn,user,instance,run)
    _,reply=answer(dsn,user,instance,conv,"I tried this step.")
    assert reply.text.startswith("An attempt is already recorded;")
    assert view(dsn,user,instance,run)==before
