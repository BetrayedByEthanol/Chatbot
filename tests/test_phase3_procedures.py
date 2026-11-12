"""P3-03 acceptance: real PostgreSQL versions, authorization and private variants."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")

from persona_agent.contracts import TrustedActor
from persona_agent.conversation import ingest_text
from persona_agent.conversation_contracts import TextInputCommand
from persona_agent.goal_contracts import (
    CreateGoalCommand, ProposeGoalStrategyCommand, SelectGoalStrategyCommand,
    goal_action_approval_phrase,
)
from persona_agent.goal_state import create_goal, propose_goal_strategy, select_goal_strategy
from persona_agent.lifecycle import SwitchPersonaCommand, switch_persona
from persona_agent.procedure_contracts import (
    CreatePersonalizedApproachCommand, ParameterSpec, ProcedureDefinition,
    ProcedureStep, PublishSharedProcedureCommand, RevisePersonalizedApproachCommand,
    procedure_approval_phrase, validate_variant,
)
from persona_agent.procedure_state import (
    create_personalized_approach, publish_shared_procedure,
    read_personalized_approach, read_shared_procedure, revise_personalized_approach,
    list_shared_procedures, list_personalized_approaches,
)
from persona_agent.runtime import PermissionDenied, StaleSelection, VersionConflict


def call(dsn, fn, *args):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return fn(conn, *args)


def actor(user_id):
    return TrustedActor(user_id=user_id, source_id="p303-authenticated")


def seed(dsn, persona_id=None, grant=False):
    user=uuid4()
    persona=persona_id or uuid4()
    instance=uuid4()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO users(id) VALUES (%s)",(user,))
            if persona_id is None:
                conn.execute("INSERT INTO persona_definitions(id,stable_key) VALUES (%s,%s)",
                             (persona,"p303-"+persona.hex))
            conn.execute(
                "INSERT INTO persona_instances(id,user_id,persona_id,label) "
                "VALUES(%s,%s,%s,'Procedure reviewer')",
                (instance,user,persona),
            )
            conn.execute(
                "INSERT INTO persona_selection(user_id,instance_id,generation,mode) "
                "VALUES(%s,%s,1,'FOREGROUND')",(user,instance),
            )
            if grant:
                # Trusted test-fixture emulation of administrator provisioning;
                # application code has NO grant-mutation surface.
                conn.execute(
                    "INSERT INTO procedure_publish_grants(user_id,persona_id,granted_by)"
                    " VALUES (%s,%s,'test platform administrator')",
                    (user,persona),
                )
    return user,persona,instance


def turn(dsn,user,instance,conv,text):
    return call(dsn,ingest_text,actor(user),TextInputCommand(
        instance_id=instance,conversation_id=conv,expected_generation=1,
        idempotency_key=uuid4().hex,text=text,
    ))


def approve(dsn,user,instance,command):
    t=turn(dsn,user,instance,command.conversation_id,
           procedure_approval_phrase(command))
    return command.model_copy(update={"approval_turn_id":t.turn_id})


def definition(policy="STABLE", *, suffix=""):
    parameters=()
    instruction="Generate the certificate" + suffix
    if policy in ("PARAMETERIZED","ADAPTIVE"):
        parameters=(
            ParameterSpec(name="hostname",kind="TEXT",max_chars=80),
            ParameterSpec(name="days",kind="INTEGER",minimum=1,maximum=365),
        )
        instruction="Generate a certificate for {hostname} valid for {days} days"+suffix
    steps=(
        ProcedureStep(step_id="generate",instruction=instruction,
                      executor="USER",success_test="Certificate file exists"),
        ProcedureStep(step_id="check",instruction="Verify certificate",
                      executor="USER",success_test="TLS handshake succeeds"),
    )
    return ProcedureDefinition(
        title="Self-signed TLS" + suffix,purpose="Configure a local test TLS endpoint",
        applicability="Local Nginx development",limitations="Not publicly trusted",
        policy=policy,preconditions=("Nginx installed",),parameters=parameters,
        steps=steps,success_tests=("Client successfully negotiates TLS",),
    )


def publication(dsn,user,persona,instance,conv,template_id,expected,defined):
    source=turn(dsn,user,instance,conv,"Publish reviewed TLS setup steps.")
    cmd=PublishSharedProcedureCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Publish reviewed TLS setup steps",
        template_id=template_id,persona_id=persona,expected_current_version=expected,
        definition=defined,
        review_note="Reviewed correctness and compatibility for local TLS testing",
    )
    return approve(dsn,user,instance,cmd)


def variant_command(dsn,user,instance,conv,template_id,version,*,params=None,
                    overrides=None,reason=None,goal=None,strategy=None):
    source=turn(dsn,user,instance,conv,"Use this procedure as my personal method.")
    cmd=CreatePersonalizedApproachCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Use this procedure as my personal method",
        variant_id=uuid4(),template_id=template_id,template_version=version,
        goal_id=goal,strategy_id=strategy,
        intended_contribution=(
            "Supports the selected approach without changing its approved outcome"
            if goal is not None else None
        ),
        parameters=params or {},step_text_overrides=overrides or {},
        adaptation_reason=reason,
    )
    return approve(dsn,user,instance,cmd)


def revise_command(dsn,user,instance,conv,variant_id,expected,version,
                   *,params=None,overrides=None,reason=None):
    source=turn(dsn,user,instance,conv,"Revise my personal procedure.")
    cmd=RevisePersonalizedApproachCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Revise my personal procedure",
        variant_id=variant_id,expected_revision=expected,template_version=version,
        parameters=params or {},step_text_overrides=overrides or {},
        adaptation_reason=reason,
    )
    return approve(dsn,user,instance,cmd)


def test_shared_publication_grant_and_exact_confirmation(dsn):
    user,persona,instance=seed(dsn,grant=False)
    conv=uuid4()
    template_id=uuid4()
    cmd=publication(dsn,user,persona,instance,conv,template_id,0,definition())
    with pytest.raises(PermissionDenied,match="grant"):
        call(dsn,publish_shared_procedure,actor(user),cmd)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(
            "INSERT INTO procedure_publish_grants(user_id,persona_id,granted_by)"
            " VALUES(%s,%s,'platform test')",(user,persona),
        )
    altered=cmd.model_copy(update={"review_note":"A different, unconfirmed review note"})
    with pytest.raises(PermissionDenied,match="exact action"):
        call(dsn,publish_shared_procedure,actor(user),altered)
    result=call(dsn,publish_shared_procedure,actor(user),cmd)
    assert result.action=="SHARED_VERSION_PUBLISHED" and result.revision==1
    assert call(dsn,publish_shared_procedure,actor(user),cmd).replayed
    visible=call(dsn,read_shared_procedure,actor(user),instance,template_id)
    assert visible.version==1 and visible.definition.policy=="STABLE"
    with psycopg.connect(dsn,autocommit=True) as conn:
        assert conn.execute(
            """SELECT count(*) FROM outbox o JOIN events e ON o.event_id=e.event_id
               WHERE e.event_id=%s AND e.event_type='procedure.template.published.v1'""",
            (result.event_id,),
        ).fetchone()[0]==1


def test_private_variant_remains_pinned_after_shared_update(dsn):
    publisher,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    v1=publication(dsn,publisher,persona,instance,conv,template,0,definition("PARAMETERIZED"))
    call(dsn,publish_shared_procedure,actor(publisher),v1)
    user,_,personal_instance=seed(dsn,persona_id=persona)
    personal_conv=uuid4()
    cmd=variant_command(dsn,user,personal_instance,personal_conv,template,1,
                        params={"hostname":"localhost","days":30})
    created=call(dsn,create_personalized_approach,actor(user),cmd)
    assert created.revision==1
    v2=publication(dsn,publisher,persona,instance,conv,template,1,
                   definition("PARAMETERIZED",suffix=" (reviewed v2)"))
    published=call(dsn,publish_shared_procedure,actor(publisher),v2)
    assert published.revision==2
    current=call(dsn,read_personalized_approach,actor(user),
                 personal_instance,cmd.variant_id)
    assert current.template_version==1
    assert current.rendered_steps[0].instruction.endswith("30 days")
    assert "reviewed v2" not in current.rendered_steps[0].instruction
    latest=call(dsn,read_shared_procedure,actor(user),personal_instance,template)
    assert latest.version==2
    change=revise_command(dsn,user,personal_instance,personal_conv,cmd.variant_id,1,2,
                          params={"hostname":"localhost","days":60})
    result=call(dsn,revise_personalized_approach,actor(user),change)
    assert result.revision==2
    assert call(dsn,revise_personalized_approach,actor(user),change).replayed
    now=call(dsn,read_personalized_approach,actor(user),
             personal_instance,cmd.variant_id)
    old=call(dsn,read_personalized_approach,actor(user),
             personal_instance,cmd.variant_id,1)
    assert now.template_version==2 and now.parameters["days"]==60
    assert old.template_version==1 and old.parameters["days"]==30


def test_stable_parameter_changes_and_unauthorized_adaptation_rejected(dsn):
    publisher,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    stable=uuid4()
    call(dsn,publish_shared_procedure,actor(publisher),
         publication(dsn,publisher,persona,instance,conv,stable,0,definition()))
    invalid=variant_command(dsn,publisher,instance,conv,stable,1,
                            params={"days":14})
    with pytest.raises(ValueError,match="unknown/too many"):
        call(dsn,create_personalized_approach,actor(publisher),invalid)
    changed=variant_command(dsn,publisher,instance,conv,stable,1,
                            overrides={"generate":"Alter certificate generation"},
                            reason="Specific evidence supports adjustment")
    with pytest.raises(ValueError,match="only adaptive"):
        call(dsn,create_personalized_approach,actor(publisher),changed)
    valid=variant_command(dsn,publisher,instance,conv,stable,1)
    call(dsn,create_personalized_approach,actor(publisher),valid)
    with pytest.raises(ValueError,match="only adaptive"):
        call(dsn,revise_personalized_approach,actor(publisher),
             revise_command(dsn,publisher,instance,conv,valid.variant_id,1,1,
                            overrides={"generate":"Do a different step"},
                            reason="Specific evidence supports adjustment"))


def test_adaptive_override_boundaries_and_strict_parameters(dsn):
    publisher,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(publisher),
         publication(dsn,publisher,persona,instance,conv,template,0,definition("ADAPTIVE")))
    with pytest.raises(ValueError,match="integer"):
        validate_variant(definition("ADAPTIVE"),{"hostname":"local","days":True},{},None)
    with pytest.raises(ValueError,match="integer"):
        validate_variant(definition("ADAPTIVE"),{"hostname":"local","days":366},{},None)
    with pytest.raises(ValueError,match="missing required"):
        validate_variant(definition("ADAPTIVE"),{"hostname":"local"},{},None)
    without_reason=variant_command(dsn,publisher,instance,conv,template,1,
        params={"hostname":"test.local","days":60},
        overrides={"generate":"Generate certificate for {hostname}"})
    with pytest.raises(ValueError,match="documented reason"):
        call(dsn,create_personalized_approach,actor(publisher),without_reason)
    cmd=variant_command(dsn,publisher,instance,conv,template,1,
        params={"hostname":"test.local","days":60},
        overrides={"generate":"Generate certificate for {hostname} on my laptop"},
        reason="The local development environment requires adjusted instructions")
    result=call(dsn,create_personalized_approach,actor(publisher),cmd)
    view=call(dsn,read_personalized_approach,actor(publisher),instance,result.entity_id)
    assert view.rendered_steps[0].instruction=="Generate certificate for test.local on my laptop"
    assert view.rendered_steps[0].success_test=="Certificate file exists"
    assert view.definition_policy=="ADAPTIVE"


def test_private_scope_and_no_cross_persona_publication(dsn):
    owner,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(owner),
         publication(dsn,owner,persona,instance,conv,template,0,definition()))
    cmd=variant_command(dsn,owner,instance,conv,template,1)
    call(dsn,create_personalized_approach,actor(owner),cmd)
    outsider,_,outsider_instance=seed(dsn,persona_id=persona)
    with pytest.raises(PermissionDenied):
        call(dsn,read_personalized_approach,actor(outsider),
             outsider_instance,cmd.variant_id)
    assert call(dsn,read_shared_procedure,actor(outsider),
                outsider_instance,template).version==1
    other_user,other_persona,other_instance=seed(dsn)
    with pytest.raises(PermissionDenied):
        call(dsn,read_shared_procedure,actor(other_user),other_instance,template)
    with pytest.raises(PermissionDenied):
        call(dsn,publish_shared_procedure,actor(other_user),
             publication(dsn,other_user,other_persona,other_instance,uuid4(),
                         template,1,definition()))


def test_immutable_history_even_with_direct_sql(dsn):
    user,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),
         publication(dsn,user,persona,instance,conv,template,0,definition()))
    private=variant_command(dsn,user,instance,conv,template,1)
    call(dsn,create_personalized_approach,actor(user),private)
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "UPDATE procedure_template_versions SET review_note='changed' "
                "WHERE template_id=%s",(template,),
            )
        with pytest.raises(psycopg.errors.RaiseException,match="immutable"):
            conn.execute(
                "DELETE FROM personalized_procedure_revisions WHERE variant_id=%s",
                (private.variant_id,),
            )


def test_pub_and_variant_cas_concurrency(dsn):
    publisher,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(publisher),
         publication(dsn,publisher,persona,instance,conv,template,0,
                     definition("PARAMETERIZED")))
    commands=(
        publication(dsn,publisher,persona,instance,conv,template,1,
                    definition("PARAMETERIZED",suffix=" v2A")),
        publication(dsn,publisher,persona,instance,conv,template,1,
                    definition("PARAMETERIZED",suffix=" v2B")),
    )
    def publish_once(cmd):
        try:
            return call(dsn,publish_shared_procedure,actor(publisher),cmd)
        except VersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(publish_once,commands[0])
        b=pool.submit(publish_once,commands[1])
        assert sum(item is not None for item in (a.result(timeout=15),b.result(timeout=15)))==1
    cmd=variant_command(dsn,publisher,instance,conv,template,1,
                        params={"hostname":"localhost","days":30})
    call(dsn,create_personalized_approach,actor(publisher),cmd)
    revisions=(
        revise_command(dsn,publisher,instance,conv,cmd.variant_id,1,1,
                       params={"hostname":"localhost","days":90}),
        revise_command(dsn,publisher,instance,conv,cmd.variant_id,1,1,
                       params={"hostname":"localhost","days":180}),
    )
    def update_once(c):
        try:
            return call(dsn,revise_personalized_approach,actor(publisher),c)
        except VersionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(update_once,revisions[0])
        b=pool.submit(update_once,revisions[1])
        assert sum(item is not None for item in (a.result(timeout=15),b.result(timeout=15)))==1


def test_wrong_confirmation_generation_and_strategy_binding(dsn):
    user,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    command=publication(dsn,user,persona,instance,conv,template,0,definition())
    with pytest.raises(PermissionDenied,match="exact action"):
        call(dsn,publish_shared_procedure,actor(user),
             command.model_copy(update={"definition":definition(suffix="tampered"),
                                       "idempotency_key":uuid4().hex}))
    call(dsn,publish_shared_procedure,actor(user),command)

    source=turn(dsn,user,instance,conv,"Please help me set up a local TLS endpoint.")
    root=CreateGoalCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Please help me set up",goal_id=uuid4(),
        title="Local TLS",desired_outcome="Local TLS endpoint works",
        success_criterion="TLS test passes",horizon="SHORT",
    )
    confirmation=turn(dsn,user,instance,conv,goal_action_approval_phrase(root))
    root=root.model_copy(update={"approval_turn_id":confirmation.turn_id})
    call(dsn,create_goal,actor(user),root)
    strategy=ProposeGoalStrategyCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Please help me set up",goal_id=root.goal_id,
        strategy_id=uuid4(),title="Self-signed cert",
        approach="Generate and configure local SSL",
        suitability_note="Local development only",
    )
    call(dsn,propose_goal_strategy,actor(user),strategy)
    linked=variant_command(dsn,user,instance,conv,template,1,
                           goal=root.goal_id,strategy=strategy.strategy_id)
    with pytest.raises(VersionConflict,match="current selected"):
        call(dsn,create_personalized_approach,actor(user),linked)
    choose=SelectGoalStrategyCommand(
        instance_id=instance,expected_generation=1,idempotency_key=uuid4().hex,
        conversation_id=conv,evidence_turn_id=source.turn_id,
        evidence_quote="Please help me set up",goal_id=root.goal_id,
        strategy_id=strategy.strategy_id,expected_goal_version=1,
    )
    conf=turn(dsn,user,instance,conv,goal_action_approval_phrase(choose))
    choose=choose.model_copy(update={"approval_turn_id":conf.turn_id})
    call(dsn,select_goal_strategy,actor(user),choose)
    assert call(dsn,create_personalized_approach,actor(user),linked).revision==1
    linked_view=call(dsn,read_personalized_approach,actor(user),instance,
                     linked.variant_id)
    assert linked_view.suitability_status=="UNREVIEWED"
    assert "approved outcome" in linked_view.intended_contribution
    with pytest.raises(ValueError,match="intended contribution"):
        CreatePersonalizedApproachCommand(
            **linked.model_dump(exclude={"intended_contribution"}),
            intended_contribution=None,
        )

    second_user,_,second_instance=seed(dsn,persona_id=persona)
    with pytest.raises(PermissionDenied):
        call(dsn,read_personalized_approach,actor(second_user),
             second_instance,linked.variant_id)


def test_scoped_library_discovery(dsn):
    user,persona,instance=seed(dsn,grant=True)
    conv=uuid4()
    template=uuid4()
    call(dsn,publish_shared_procedure,actor(user),
         publication(dsn,user,persona,instance,conv,template,0,definition()))
    local=variant_command(dsn,user,instance,conv,template,1)
    call(dsn,create_personalized_approach,actor(user),local)
    public=call(dsn,list_shared_procedures,actor(user),instance)
    personal=call(dsn,list_personalized_approaches,actor(user),instance)
    assert any(x.template_id==template and x.version==1 for x in public)
    assert any(x.variant_id==local.variant_id and x.revision==1 for x in personal)
    outsider,_,outsider_instance=seed(dsn,persona_id=persona)
    assert any(x.template_id==template for x in
               call(dsn,list_shared_procedures,actor(outsider),outsider_instance))
    assert call(dsn,list_personalized_approaches,actor(outsider),outsider_instance)==()
    with psycopg.connect(dsn,autocommit=True) as conn:
        with pytest.raises(ValueError):
            list_shared_procedures(conn,actor(user),instance,limit=0)
