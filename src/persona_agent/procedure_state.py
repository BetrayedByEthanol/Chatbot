"""Procedure Manager P3-03: curated template publication and pinned private variants.

Shared definitions require pre-provisioned platform publisher grant. Every
persona-owned change is idempotent, selected-foreground fenced, user-confirmed,
and journaled with the P0 transactional outbox. No run or tool dispatch.
"""
import json
from datetime import timedelta
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor
from .goal_state import _goal, _read_selected, _selected, _source
from .operation_contracts import require_idle
from .procedure_contracts import (
    CreatePersonalizedApproachCommand, PersonalizedApproachHandle,
    PersonalizedApproachView,
    ProcedureChangeResult, ProcedureDefinition, ProcedureUserCommand,
    PublishSharedProcedureCommand, RevisePersonalizedApproachCommand,
    SharedProcedureView, procedure_approval_phrase, procedure_content_hash,
    validate_variant,
)
from .runtime import (
    IdempotencyConflict, PermissionDenied, StaleSelection, VersionConflict,
    _fingerprint,
)


def _publish(cur, actor: TrustedActor, instance_id: UUID,
             kind: str, payload: dict, receipt_id: UUID) -> UUID:
    """Procedure-owned journal, kept distinct from the Goal/Task streams."""
    stream_id = uuid5(NAMESPACE_URL, f"persona-agent/procedure/{actor.user_id}/{instance_id}")
    cur.execute(
        """INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
           VALUES (%s,%s,%s,'procedure') ON CONFLICT(stream_id) DO NOTHING""",
        (stream_id,actor.user_id,instance_id),
    )
    cur.execute(
        """UPDATE event_streams SET last_sequence=last_sequence+1
           WHERE stream_id=%s AND user_id=%s AND instance_id=%s
           RETURNING last_sequence""",
        (stream_id,actor.user_id,instance_id),
    )
    seq=cur.fetchone()
    if seq is None:
        raise PermissionDenied("procedure event stream outside caller scope")
    event_id=uuid4()
    cur.execute(
        """INSERT INTO events(
             event_id,event_type,schema_version,user_id,instance_id,
             stream_id,stream_sequence,source_id,trust_level,correlation_id,payload
           ) VALUES(%s,%s,1,%s,%s,%s,%s,%s,'AUTHENTICATED_USER',%s,%s::jsonb)""",
        (event_id,kind,actor.user_id,instance_id,stream_id,
         seq["last_sequence"],actor.source_id,receipt_id,json.dumps(payload)),
    )
    cur.execute("INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",
                (uuid4(),event_id))
    return event_id


def _approval(cur, actor: TrustedActor, command: ProcedureUserCommand, source: dict) -> None:
    if command.approval_turn_id is None or command.approval_turn_id == command.evidence_turn_id:
        raise PermissionDenied("procedure change requires separate user approval")
    cur.execute(
        """SELECT input_text,created_at FROM conversation_turns
           WHERE conversation_id=%s AND turn_id=%s AND user_id=%s AND instance_id=%s""",
        (command.conversation_id,command.approval_turn_id,
         actor.user_id,command.instance_id),
    )
    row=cur.fetchone()
    if (row is None or row["input_text"].strip()!=procedure_approval_phrase(command)
        or row["created_at"]<source["created_at"]
        or row["created_at"]>source["created_at"]+timedelta(days=1)):
        raise PermissionDenied("procedure approval is not bound to the exact action")


def _command(connection: Connection, actor: TrustedActor, command: ProcedureUserCommand,
             command_type: str, apply) -> ProcedureChangeResult:
    require_idle(connection)
    _require_actor(actor)
    receipt_id=uuid4()
    fingerprint=_fingerprint(command)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("procedure command outside user/persona scope")
            cur.execute(
                """INSERT INTO command_receipts(
                     receipt_id,user_id,instance_id,command_type,
                     idempotency_key,request_fingerprint
                   ) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                   DO NOTHING RETURNING receipt_id""",
                (receipt_id,actor.user_id,command.instance_id,command_type,
                 command.idempotency_key,fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s AND command_type=%s
                         AND idempotency_key=%s""",
                    (actor.user_id,command.instance_id,command_type,command.idempotency_key),
                )
                prior=cur.fetchone()
                if prior is None or prior["request_fingerprint"]!=fingerprint or prior["result_json"] is None:
                    raise IdempotencyConflict("procedure idempotency key reused differently")
                return ProcedureChangeResult.model_validate(prior["result_json"]).model_copy(
                    update={"replayed":True},
                )
            _selected(cur,actor,command)
            source=_source(cur,actor,command)
            _approval(cur,actor,command,source)
            result=apply(cur,receipt_id)
            cur.execute(
                """UPDATE command_receipts SET event_id=%s,result_json=%s::jsonb
                   WHERE receipt_id=%s""",
                (result.event_id,json.dumps(result.model_dump(mode="json")),receipt_id),
            )
            return result


def _current_persona(cur, actor: TrustedActor, instance_id: UUID) -> UUID:
    cur.execute(
        "SELECT persona_id FROM persona_instances WHERE user_id=%s AND id=%s",
        (actor.user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None:
        raise PermissionDenied("selected persona not owned by authenticated user")
    return row["persona_id"]


def _definition(cur, template_id: UUID, version: int, persona_id: UUID) -> ProcedureDefinition:
    cur.execute(
        """SELECT v.definition,v.definition_sha256 FROM procedure_template_versions v
           JOIN procedure_templates t ON t.template_id=v.template_id
           WHERE v.template_id=%s AND v.version=%s AND t.persona_id=%s
             AND t.approved""",
        (template_id,version,persona_id),
    )
    row=cur.fetchone()
    if row is None:
        raise PermissionDenied("procedure version unavailable for selected persona")
    definition=ProcedureDefinition.model_validate(row["definition"])
    if procedure_content_hash(definition)!=row["definition_sha256"]:
        raise VersionConflict("procedure definition integrity mismatch")
    return definition


def _selected_strategy(cur, actor: TrustedActor, instance_id: UUID,
                       goal_id: UUID | None, strategy_id: UUID | None) -> None:
    if goal_id is None:
        if strategy_id is not None:
            raise ValueError("strategy requires selected goal")
        return
    goal=_goal(cur,actor,instance_id,goal_id,lock="SHARE")
    if goal["status"]!="ACTIVE":
        raise VersionConflict("personalized approach requires active goal")
    # Parent pause can invalidate the derived method. Hold all ancestors'
    # SHARE locks until this short procedure transaction commits.
    cur.execute(
        """WITH RECURSIVE ancestors AS (
             SELECT goal_id,parent_goal_id FROM goal_records
               WHERE goal_id=%s AND user_id=%s AND instance_id=%s
             UNION ALL
             SELECT g.goal_id,g.parent_goal_id FROM goal_records g
               JOIN ancestors a ON g.goal_id=a.parent_goal_id
               WHERE g.user_id=%s AND g.instance_id=%s
           ) SELECT goal_id FROM ancestors""",
        (goal_id,actor.user_id,instance_id,actor.user_id,instance_id),
    )
    ancestor_ids=[row["goal_id"] for row in cur.fetchall()]
    cur.execute(
        """SELECT status FROM goal_records
           WHERE goal_id=ANY(%s) AND user_id=%s AND instance_id=%s
           ORDER BY goal_id FOR SHARE""",
        (ancestor_ids,actor.user_id,instance_id),
    )
    statuses=cur.fetchall()
    if len(statuses)!=len(ancestor_ids) or any(x["status"]!="ACTIVE" for x in statuses):
        raise VersionConflict("goal or ancestor inactive for personalized approach")
    cur.execute(
        """SELECT strategy_id FROM goal_strategy_selections
           WHERE goal_id=%s AND user_id=%s AND instance_id=%s
           ORDER BY goal_version DESC LIMIT 1""",
        (goal_id,actor.user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None or row["strategy_id"]!=strategy_id:
        raise VersionConflict("procedure approach must match current selected strategy")


def publish_shared_procedure(connection: Connection, actor: TrustedActor,
                             command: PublishSharedProcedureCommand) -> ProcedureChangeResult:
    """Explicit publishing grant is platform-provisioned, never self-issued."""
    def apply(cur,receipt):
        persona_id=_current_persona(cur,actor,command.instance_id)
        if persona_id!=command.persona_id:
            raise PermissionDenied("shared procedure persona mismatch")
        cur.execute(
            """SELECT 1 FROM procedure_publish_grants
               WHERE user_id=%s AND persona_id=%s AND active""",
            (actor.user_id,persona_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("no approved procedure publishing grant")
        # FOR UPDATE cannot lock a missing template. Serialize by template
        # identity before looking up version zero, including across publishers
        # with different users/instances and therefore different event streams.
        # Hash collisions only cause unnecessary serialization, not conflicts.
        # pg_advisory_xact_lock releases automatically on commit/rollback.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"persona-agent/procedure-template/{command.template_id}",),
        )
        cur.execute(
            "SELECT current_version FROM procedure_templates WHERE template_id=%s FOR UPDATE",
            (command.template_id,),
        )
        old=cur.fetchone()
        version=0 if old is None else old["current_version"]
        if version!=command.expected_current_version:
            raise VersionConflict("shared template version changed")
        if old is not None:
            cur.execute(
                "SELECT persona_id FROM procedure_templates WHERE template_id=%s",
                (command.template_id,),
            )
            if cur.fetchone()["persona_id"]!=persona_id:
                raise PermissionDenied("cannot alter another persona's global template")
        next_version=version+1
        digest=procedure_content_hash(command.definition)
        event=_publish(cur,actor,command.instance_id,"procedure.template.published.v1",{
            "template_id":str(command.template_id),"persona_id":str(persona_id),
            "version":next_version,"policy":command.definition.policy,
            "hash":digest,"review_note":command.review_note,
            "approval_turn_id":str(command.approval_turn_id),
            "publisher_grant_checked":True,
        },receipt)
        if old is None:
            cur.execute(
                """INSERT INTO procedure_templates(
                     template_id,persona_id,current_version,publisher_user_id,created_event_id
                   ) VALUES(%s,%s,%s,%s,%s)""",
                (command.template_id,persona_id,next_version,actor.user_id,event),
            )
        else:
            cur.execute(
                """UPDATE procedure_templates SET current_version=%s,
                     updated_at=clock_timestamp() WHERE template_id=%s""",
                (next_version,command.template_id),
            )
        cur.execute(
            """INSERT INTO procedure_template_versions(
                 template_id,persona_id,version,definition,definition_sha256,
                 review_note,source_user_id,source_instance_id,source_conversation_id,
                 source_turn_id,approval_turn_id,published_event_id
               ) VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.template_id,persona_id,next_version,
             json.dumps(command.definition.model_dump(mode="json")),digest,
             command.review_note,actor.user_id,command.instance_id,
             command.conversation_id,command.evidence_turn_id,
             command.approval_turn_id,event),
        )
        return ProcedureChangeResult(entity_id=command.template_id,
                                     revision=next_version,event_id=event,
                                     action="SHARED_VERSION_PUBLISHED")
    return _command(connection,actor,command,"procedure.template.publish.v1",apply)


def _variant_revision(cur, actor: TrustedActor, instance_id: UUID, variant_id: UUID,
                      *, for_update: bool) -> dict:
    lock="FOR UPDATE" if for_update else "FOR SHARE"
    cur.execute(
        f"""SELECT * FROM personalized_procedures WHERE variant_id=%s
            AND user_id=%s AND instance_id=%s {lock}""",
        (variant_id,actor.user_id,instance_id),
    )
    result=cur.fetchone()
    if result is None:
        raise PermissionDenied("personal procedure outside user/persona scope")
    return result


def _save_variant_revision(cur, actor: TrustedActor, command: ProcedureUserCommand,
                           variant_id: UUID, template_id: UUID, revision: int,
                           template_version: int, parameters: dict,
                           overrides: dict, reason: str | None, event: UUID) -> None:
    cur.execute(
        """INSERT INTO personalized_procedure_revisions(
             variant_id,template_id,user_id,instance_id,revision,template_version,
             parameters,step_text_overrides,adaptation_reason,source_conversation_id,
             source_turn_id,source_quote,approval_turn_id,event_id
           ) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s)""",
        (variant_id,template_id,actor.user_id,command.instance_id,revision,
         template_version,json.dumps(parameters),json.dumps(overrides),reason,
         command.conversation_id,command.evidence_turn_id,
         command.evidence_quote,command.approval_turn_id,event),
    )


def create_personalized_approach(connection: Connection, actor: TrustedActor,
                                 command: CreatePersonalizedApproachCommand) -> ProcedureChangeResult:
    def apply(cur,receipt):
        persona_id=_current_persona(cur,actor,command.instance_id)
        definition=_definition(cur,command.template_id,command.template_version,persona_id)
        validate_variant(definition,command.parameters,
                         command.step_text_overrides,command.adaptation_reason)
        _selected_strategy(cur,actor,command.instance_id,
                           command.goal_id,command.strategy_id)
        event=_publish(cur,actor,command.instance_id,"procedure.variant.created.v1",{
            "variant_id":str(command.variant_id),"template_id":str(command.template_id),
            "template_version":command.template_version,
            "goal_id":str(command.goal_id) if command.goal_id else None,
            "strategy_id":str(command.strategy_id) if command.strategy_id else None,
            "intended_contribution":command.intended_contribution,
            "suitability_status":"UNREVIEWED",
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        cur.execute(
            """INSERT INTO personalized_procedures(
                 variant_id,user_id,instance_id,persona_id,template_id,
                 goal_id,strategy_id,intended_contribution,
                 current_revision,created_event_id
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s)""",
            (command.variant_id,actor.user_id,command.instance_id,
             persona_id,command.template_id,command.goal_id,
             command.strategy_id,command.intended_contribution,event),
        )
        _save_variant_revision(cur,actor,command,command.variant_id,command.template_id,
                               1,command.template_version,command.parameters,
                               command.step_text_overrides,command.adaptation_reason,event)
        return ProcedureChangeResult(entity_id=command.variant_id,revision=1,
                                     event_id=event,action="PERSONAL_VARIANT_CREATED")
    return _command(connection,actor,command,"procedure.variant.create.v1",apply)


def revise_personalized_approach(connection: Connection, actor: TrustedActor,
                                 command: RevisePersonalizedApproachCommand) -> ProcedureChangeResult:
    """A version-pinned variant never follows a shared update implicitly."""
    def apply(cur,receipt):
        variant=_variant_revision(cur,actor,command.instance_id,command.variant_id,
                                  for_update=True)
        if variant["current_revision"]!=command.expected_revision:
            raise VersionConflict("personal procedure revision changed")
        _selected_strategy(cur,actor,command.instance_id,
                           variant["goal_id"],variant["strategy_id"])
        definition=_definition(cur,variant["template_id"],command.template_version,
                               variant["persona_id"])
        validate_variant(definition,command.parameters,
                         command.step_text_overrides,command.adaptation_reason)
        cur.execute(
            """SELECT template_version,parameters,step_text_overrides
               FROM personalized_procedure_revisions
               WHERE variant_id=%s AND revision=%s""",
            (command.variant_id,command.expected_revision),
        )
        prior=cur.fetchone()
        if (prior["template_version"]==command.template_version
            and prior["parameters"]==command.parameters
            and prior["step_text_overrides"]==command.step_text_overrides):
            raise VersionConflict("personal procedure revision has no changes")
        revision=variant["current_revision"]+1
        event=_publish(cur,actor,command.instance_id,"procedure.variant.revised.v1",{
            "variant_id":str(command.variant_id),"template_id":str(variant["template_id"]),
            "template_version":command.template_version,"revision":revision,
            "approval_turn_id":str(command.approval_turn_id),
        },receipt)
        _save_variant_revision(cur,actor,command,command.variant_id,
                               variant["template_id"],revision,
                               command.template_version,command.parameters,
                               command.step_text_overrides,command.adaptation_reason,event)
        cur.execute(
            """UPDATE personalized_procedures SET current_revision=%s,
                 updated_at=clock_timestamp()
               WHERE variant_id=%s AND user_id=%s AND instance_id=%s""",
            (revision,command.variant_id,actor.user_id,command.instance_id),
        )
        return ProcedureChangeResult(entity_id=command.variant_id,revision=revision,
                                     event_id=event,action="PERSONAL_VARIANT_REVISED")
    return _command(connection,actor,command,"procedure.variant.revise.v1",apply)


def read_shared_procedure(connection: Connection, actor: TrustedActor, instance_id: UUID,
                          template_id: UUID, version: int | None = None) -> SharedProcedureView:
    """Selected persona may access approved persona-global versions, not other personas."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            persona_id=_current_persona(cur,actor,instance_id)
            cur.execute(
                """SELECT current_version FROM procedure_templates
                   WHERE template_id=%s AND persona_id=%s AND approved""",
                (template_id,persona_id),
            )
            template=cur.fetchone()
            if template is None:
                raise PermissionDenied("shared template is not visible to this persona")
            pinned=version if version is not None else template["current_version"]
            definition=_definition(cur,template_id,pinned,persona_id)
            return SharedProcedureView(template_id=template_id,persona_id=persona_id,
                                       version=pinned,definition=definition,
                                       content_hash=procedure_content_hash(definition))


def read_personalized_approach(connection: Connection, actor: TrustedActor,
                               instance_id: UUID, variant_id: UUID,
                               revision: int | None = None) -> PersonalizedApproachView:
    """Private revision retrieval for restart/rollback; no global publication."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            variant=_variant_revision(cur,actor,instance_id,variant_id,for_update=False)
            pinned=revision if revision is not None else variant["current_revision"]
            cur.execute(
                """SELECT * FROM personalized_procedure_revisions
                   WHERE variant_id=%s AND revision=%s""",
                (variant_id,pinned),
            )
            record=cur.fetchone()
            if record is None:
                raise PermissionDenied("private procedure revision unavailable")
            definition=_definition(cur,variant["template_id"],record["template_version"],
                                   variant["persona_id"])
            rendered=validate_variant(definition,record["parameters"],
                                      record["step_text_overrides"],
                                      record["adaptation_reason"])
            return PersonalizedApproachView(
                variant_id=variant_id,template_id=variant["template_id"],
                template_version=record["template_version"],revision=pinned,
                goal_id=variant["goal_id"],strategy_id=variant["strategy_id"],
                intended_contribution=variant["intended_contribution"],
                suitability_status=variant["suitability_status"],
                parameters=record["parameters"],
                step_text_overrides=record["step_text_overrides"],
                rendered_steps=rendered,
                adaptation_reason=record["adaptation_reason"],
                definition_policy=definition.policy,
            )


def list_shared_procedures(connection: Connection, actor: TrustedActor,
                           instance_id: UUID, *, limit: int = 50
                           ) -> tuple[SharedProcedureView, ...]:
    """Browse only approved definitions for the currently selected persona."""
    require_idle(connection)
    _require_actor(actor)
    if not 1 <= limit <= 50:
        raise ValueError("shared procedure list limit must be 1..50")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            persona_id=_current_persona(cur,actor,instance_id)
            cur.execute(
                """SELECT t.template_id,t.current_version,v.definition,
                          v.definition_sha256
                   FROM procedure_templates t
                   JOIN procedure_template_versions v
                     ON v.template_id=t.template_id
                    AND v.version=t.current_version
                   WHERE t.persona_id=%s AND t.approved
                   ORDER BY t.created_at,t.template_id LIMIT %s""",
                (persona_id,limit),
            )
            result=[]
            for row in cur.fetchall():
                definition=ProcedureDefinition.model_validate(row["definition"])
                digest=procedure_content_hash(definition)
                if digest!=row["definition_sha256"]:
                    raise VersionConflict("shared procedure definition checksum mismatch")
                result.append(SharedProcedureView(
                    template_id=row["template_id"],persona_id=persona_id,
                    version=row["current_version"],definition=definition,
                    content_hash=digest,
                ))
            return tuple(result)


def list_personalized_approaches(connection: Connection, actor: TrustedActor,
                                 instance_id: UUID, *, limit: int = 50
                                 ) -> tuple[PersonalizedApproachHandle, ...]:
    """List private variant handles; content remains scoped/version-pinned."""
    require_idle(connection)
    _require_actor(actor)
    if not 1 <= limit <= 50:
        raise ValueError("personal procedure list limit must be 1..50")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            cur.execute(
                """SELECT p.variant_id,p.template_id,p.current_revision,
                          p.goal_id,p.strategy_id,r.template_version,
                          v.definition->>'title' AS title
                   FROM personalized_procedures p
                   JOIN personalized_procedure_revisions r
                     ON r.variant_id=p.variant_id
                    AND r.revision=p.current_revision
                   JOIN procedure_template_versions v
                     ON v.template_id=p.template_id
                    AND v.version=r.template_version
                   WHERE p.user_id=%s AND p.instance_id=%s
                   ORDER BY p.created_at,p.variant_id LIMIT %s""",
                (actor.user_id,instance_id,limit),
            )
            return tuple(PersonalizedApproachHandle(
                variant_id=row["variant_id"],template_id=row["template_id"],
                template_version=row["template_version"],
                revision=row["current_revision"],goal_id=row["goal_id"],
                strategy_id=row["strategy_id"],title=row["title"],
            ) for row in cur.fetchall())
