"""P3-05 evidence-linked procedure outcomes and safe personal adaptation.

Run evidence and explicit user reports can inform *procedure effectiveness*
only. Neither assessment nor a reviewed private method revision writes Task,
Goal, Operation, shared ProcedureTemplate or any in-flight ProcedureRun.
"""
import json
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor
from .goal_state import _read_selected
from .operation_contracts import require_idle
from .procedure_contracts import validate_variant
from .procedure_state import (
    _command, _definition, _publish, _save_variant_revision,
    _selected_strategy, _variant_revision,
)
from .procedure_learning_contracts import (
    DecideProcedureAdaptationCommand, EvaluateProcedureRunCommand,
    ProcedureAdaptationView, ProcedureCriterionReport,
    ProcedureEvaluationView, ProcedureExperienceView, ProcedureLearningResult,
    ProposeProcedureAdaptationCommand,
)
from .procedure_run_state import _run
from .runtime import PermissionDenied, VersionConflict


def _latest_evaluation(cur, actor: TrustedActor, instance_id: UUID,
                       run_id: UUID):
    cur.execute(
        """SELECT * FROM procedure_run_evaluations
           WHERE run_id=%s AND user_id=%s AND instance_id=%s
           ORDER BY evaluation_revision DESC LIMIT 1""",
        (run_id,actor.user_id,instance_id),
    )
    return cur.fetchone()


def evaluate_procedure_run(connection: Connection, actor: TrustedActor,
                           command: EvaluateProcedureRunCommand
                           ) -> ProcedureLearningResult:
    """Append a tentative, attributable assessment; never assert verified success."""
    def apply(cur,receipt):
        # Run row serialization prevents racing evaluations from inventing
        # independent revisions of the same evidence.
        run=_run(cur,actor,command.instance_id,command.run_id)
        if run["version"]!=command.expected_run_version:
            raise VersionConflict("procedure run changed before outcome assessment")
        prior=_latest_evaluation(cur,actor,command.instance_id,command.run_id)
        prior_id=prior["evaluation_id"] if prior else None
        if prior_id!=command.expected_previous_evaluation_id:
            raise VersionConflict("newer evaluation exists; explicitly supersede it")
        if (command.outcome=="USER_REPORTED_SUCCESS"
            and run["status"]!="USER_CONFIRMED"):
            raise VersionConflict("user-reported success needs a finished procedure run")
        definition=_definition(cur,run["template_id"],run["template_version"],run["persona_id"])
        reported={r.position:r.state for r in command.criteria}
        if any(pos<1 or pos>len(definition.success_tests) for pos in reported):
            raise ValueError("criterion position not in pinned procedure version")
        if command.outcome=="USER_REPORTED_SUCCESS" and (
            len(reported)!=len(definition.success_tests)
            or any(state!="USER_REPORTED_MET" for state in reported.values())
        ):
            raise VersionConflict("reported success requires user-reported evidence for every criterion")
        cur.execute(
            """SELECT step_id,status,executor,last_step_event_id
               FROM procedure_run_steps WHERE run_id=%s""",
            (command.run_id,),
        )
        steps=cur.fetchall()
        if not steps:
            raise VersionConflict("cannot evaluate an empty procedure run")
        if command.outcome=="USER_REPORTED_SUCCESS" and any(
            (s["status"]!="USER_CONFIRMED" if s["executor"]=="USER"
             else s["status"]!="VERIFIED") for s in steps
        ):
            raise VersionConflict("reported success conflicts with latest step evidence")

        evidence: list[tuple[UUID,str]]=[]
        for event_id in command.supporting_step_event_ids:
            cur.execute(
                """SELECT step_id FROM procedure_run_step_events
                   WHERE step_event_id=%s AND run_id=%s
                     AND user_id=%s AND instance_id=%s""",
                (event_id,command.run_id,actor.user_id,command.instance_id),
            )
            row=cur.fetchone()
            if row is None:
                raise PermissionDenied("evidence not from this run and persona")
            if command.outcome=="USER_REPORTED_SUCCESS" and not any(
                step["step_id"]==row["step_id"]
                and step["last_step_event_id"]==event_id for step in steps
            ):
                raise VersionConflict("superseded step evidence cannot justify success")
            evidence.append((event_id,row["step_id"]))

        revision=prior["evaluation_revision"]+1 if prior else 1
        event=_publish(cur,actor,command.instance_id,"procedure.run.evaluated.v1",{
            "run_id":str(command.run_id),
            "evaluation_id":str(command.evaluation_id),
            "evaluation_revision":revision,"run_version":run["version"],
            "supersedes":str(prior_id) if prior_id else None,
            "outcome":command.outcome,"failure_cause":command.failure_cause,
            "confidence":command.confidence,
            "supporting_step_events":[str(ref) for ref,_ in evidence],
            "reported_criteria":[{"position":p,"state":v} for p,v in sorted(reported.items())],
            "independently_verified":False,
            "task_or_goal_completed":False,
        },receipt)
        cur.execute(
            """INSERT INTO procedure_run_evaluations(
                 evaluation_id,run_id,user_id,instance_id,evaluation_revision,
                 run_version,supersedes_evaluation_id,outcome,failure_cause,
                 confidence,rationale,source_conversation_id,source_turn_id,
                 source_quote,event_id
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.evaluation_id,command.run_id,actor.user_id,
             command.instance_id,revision,run["version"],prior_id,
             command.outcome,command.failure_cause,command.confidence,
             command.rationale.strip(),command.conversation_id,
             command.evidence_turn_id,command.evidence_quote,event),
        )
        for event_id,step_id in evidence:
            cur.execute(
                """INSERT INTO procedure_evaluation_step_evidence(
                     evaluation_id,run_id,step_id,step_event_id,user_id,instance_id
                   ) VALUES(%s,%s,%s,%s,%s,%s)""",
                (command.evaluation_id,command.run_id,step_id,event_id,
                 actor.user_id,command.instance_id),
            )
        for position,state in sorted(reported.items()):
            cur.execute(
                """INSERT INTO procedure_evaluation_criteria(
                     evaluation_id,run_id,user_id,instance_id,criterion_position,
                     criterion_text,reported_state
                   ) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (command.evaluation_id,command.run_id,actor.user_id,
                 command.instance_id,position,
                 definition.success_tests[position-1],state),
            )
        return ProcedureLearningResult(
            entity_id=command.evaluation_id,event_id=event,
            action="RUN_EVALUATED",revision=revision,
        )
    return _command(
        connection,actor,command,"procedure.run.evaluate.v1",apply,
        result_type=ProcedureLearningResult,requires_approval=False,
    )


def propose_procedure_adaptation(connection: Connection, actor: TrustedActor,
                                 command: ProposeProcedureAdaptationCommand
                                 ) -> ProcedureLearningResult:
    """Store a private *candidate*. Never apply a revision on proposal."""
    def apply(cur,receipt):
        variant=_variant_revision(
            cur,actor,command.instance_id,command.variant_id,for_update=True,
        )
        if variant["current_revision"]!=command.expected_revision:
            raise VersionConflict("personalized method changed before proposal")
        cur.execute(
            """SELECT * FROM procedure_run_evaluations
               WHERE evaluation_id=%s AND user_id=%s AND instance_id=%s""",
            (command.evaluation_id,actor.user_id,command.instance_id),
        )
        evaluation=cur.fetchone()
        if evaluation is None:
            raise PermissionDenied("evaluation is not owned by this persona")
        run=_run(cur,actor,command.instance_id,evaluation["run_id"],lock=False)
        if (run["variant_id"]!=command.variant_id
            or run["variant_revision"]!=command.expected_revision
            or run["template_id"]!=variant["template_id"]
            or run["version"]!=evaluation["run_version"]):
            raise VersionConflict("evaluation does not describe current pinned approach")
        latest=_latest_evaluation(cur,actor,command.instance_id,run["run_id"])
        if latest["evaluation_id"]!=command.evaluation_id:
            raise VersionConflict("assessment superseded; cannot propose from stale evidence")
        if evaluation["outcome"] not in ("USER_REPORTED_FAILURE","INCOMPLETE"):
            raise VersionConflict("insufficient negative or incomplete outcome evidence")
        cur.execute(
            """SELECT template_version,parameters,step_text_overrides
               FROM personalized_procedure_revisions
               WHERE variant_id=%s AND revision=%s""",
            (command.variant_id,command.expected_revision),
        )
        current=cur.fetchone()
        if current is None or current["template_version"]!=run["template_version"]:
            raise VersionConflict("private revision changed or was repinned")
        definition=_definition(
            cur,variant["template_id"],run["template_version"],variant["persona_id"],
        )
        if definition.policy=="STABLE":
            raise VersionConflict("stable method requires diagnosis, not step rewriting")
        if definition.policy=="PARAMETERIZED" and command.step_text_overrides:
            raise VersionConflict("parameterized method cannot rewrite step instructions")
        # Reuse EXACT existing P3-03 policy, rendering and override budgets.
        validate_variant(
            definition,command.parameters,command.step_text_overrides,command.reason,
        )
        if (command.parameters==current["parameters"]
            and command.step_text_overrides==current["step_text_overrides"]):
            raise VersionConflict("proposal makes no change to the private method")
        event=_publish(cur,actor,command.instance_id,
                       "procedure.adaptation.proposed.v1",{
            "proposal_id":str(command.proposal_id),
            "evaluation_id":str(command.evaluation_id),
            "variant_id":str(command.variant_id),
            "expected_revision":command.expected_revision,
            "policy":definition.policy,
            "applied":False,
            "shared_template_unchanged":True,
        },receipt)
        cur.execute(
            """INSERT INTO procedure_adaptation_proposals(
                 proposal_id,evaluation_id,run_id,variant_id,user_id,instance_id,
                 expected_revision,template_version,policy,parameters,
                 step_text_overrides,reason,source_conversation_id,
                 source_turn_id,source_quote,event_id
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,
                        %s,%s,%s,%s,%s)""",
            (command.proposal_id,command.evaluation_id,run["run_id"],
             command.variant_id,actor.user_id,command.instance_id,
             command.expected_revision,run["template_version"],
             definition.policy,json.dumps(command.parameters),
             json.dumps(command.step_text_overrides),command.reason.strip(),
             command.conversation_id,command.evidence_turn_id,
             command.evidence_quote,event),
        )
        return ProcedureLearningResult(
            entity_id=command.proposal_id,event_id=event,
            action="ADAPTATION_PROPOSED",revision=command.expected_revision,
        )
    return _command(
        connection,actor,command,"procedure.adaptation.propose.v1",apply,
        result_type=ProcedureLearningResult,requires_approval=False,
    )


def decide_procedure_adaptation(connection: Connection, actor: TrustedActor,
                                command: DecideProcedureAdaptationCommand
                                ) -> ProcedureLearningResult:
    """Only a separately approved decision may create a private revision."""
    def apply(cur,receipt):
        cur.execute(
            """SELECT * FROM procedure_adaptation_proposals
               WHERE proposal_id=%s AND user_id=%s AND instance_id=%s
               FOR UPDATE""",
            (command.proposal_id,actor.user_id,command.instance_id),
        )
        proposal=cur.fetchone()
        if proposal is None:
            raise PermissionDenied("adaptation proposal outside user/persona scope")
        cur.execute(
            "SELECT 1 FROM procedure_adaptation_decisions WHERE proposal_id=%s",
            (command.proposal_id,),
        )
        if cur.fetchone() is not None:
            raise VersionConflict("adaptation proposal already decided")
        variant=_variant_revision(
            cur,actor,command.instance_id,proposal["variant_id"],for_update=True,
        )
        if command.expected_revision!=proposal["expected_revision"]:
            raise VersionConflict("decision does not match proposal's pinned revision")
        if (command.decision=="APPLY"
            and variant["current_revision"]!=proposal["expected_revision"]):
            raise VersionConflict("proposal pinned revision is no longer current")
        # REJECT is always admissible for an owned, undecided proposal:
        # a user can dismiss an obsolete candidate without reactivating it.
        applied_revision=None
        if command.decision=="APPLY":
            # Serialize with new outcome assessments before reading the
            # latest evaluation. Checking the head first and only then
            # waiting for the run lock could approve a superseded report.
            run=_run(cur,actor,command.instance_id,proposal["run_id"],lock=False)
            evaluation=_latest_evaluation(
                cur,actor,command.instance_id,proposal["run_id"],
            )
            if (evaluation is None or evaluation["evaluation_id"]!=proposal["evaluation_id"]
                or run["version"]!=evaluation["run_version"]):
                raise VersionConflict("procedure outcome evidence changed since proposal")
            _selected_strategy(
                cur,actor,command.instance_id,variant["goal_id"],
                variant["strategy_id"],
            )
            definition=_definition(
                cur,variant["template_id"],proposal["template_version"],
                variant["persona_id"],
            )
            if definition.policy!=proposal["policy"] or definition.policy=="STABLE":
                raise VersionConflict("method policy no longer permits adaptation")
            validate_variant(
                definition,proposal["parameters"],proposal["step_text_overrides"],
                proposal["reason"],
            )
            applied_revision=variant["current_revision"]+1
        event=_publish(
            cur,actor,command.instance_id,
            ("procedure.adaptation.applied.v1" if command.decision=="APPLY"
             else "procedure.adaptation.rejected.v1"),{
                "proposal_id":str(command.proposal_id),
                "variant_id":str(proposal["variant_id"]),
                "decision":command.decision,"applied_revision":applied_revision,
                "approval_turn_id":str(command.approval_turn_id),
                "shared_definition_unchanged":True,
                "active_run_unmodified":True,
            },receipt,
        )
        if command.decision=="APPLY":
            _save_variant_revision(
                cur,actor,command,proposal["variant_id"],
                variant["template_id"],applied_revision,
                proposal["template_version"],proposal["parameters"],
                proposal["step_text_overrides"],proposal["reason"],event,
            )
            cur.execute(
                """UPDATE personalized_procedures
                   SET current_revision=%s,updated_at=clock_timestamp()
                   WHERE variant_id=%s AND user_id=%s AND instance_id=%s""",
                (applied_revision,proposal["variant_id"],
                 actor.user_id,command.instance_id),
            )
        cur.execute(
            """INSERT INTO procedure_adaptation_decisions(
                 proposal_id,variant_id,user_id,instance_id,decision,
                 applied_revision,source_conversation_id,source_turn_id,
                 source_quote,approval_turn_id,event_id
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.proposal_id,proposal["variant_id"],actor.user_id,
             command.instance_id,command.decision,applied_revision,
             command.conversation_id,command.evidence_turn_id,
             command.evidence_quote,command.approval_turn_id,event),
        )
        return ProcedureLearningResult(
            entity_id=command.proposal_id,event_id=event,
            action=("ADAPTATION_APPLIED" if command.decision=="APPLY"
                    else "ADAPTATION_REJECTED"),
            revision=(applied_revision if applied_revision is not None
                      else variant["current_revision"]),
        )
    return _command(
        connection,actor,command,"procedure.adaptation.decide.v1",apply,
        result_type=ProcedureLearningResult,
    )


def read_procedure_evaluations(connection: Connection, actor: TrustedActor,
                               instance_id: UUID, run_id: UUID
                               ) -> tuple[ProcedureEvaluationView, ...]:
    """Return bounded chronological evidence for a single selected run."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            _run(cur,actor,instance_id,run_id,lock=False)
            cur.execute(
                """SELECT * FROM procedure_run_evaluations
                   WHERE run_id=%s AND user_id=%s AND instance_id=%s
                   ORDER BY evaluation_revision DESC LIMIT 50""",
                (run_id,actor.user_id,instance_id),
            )
            evaluations=cur.fetchall()
            ids=[e["evaluation_id"] for e in evaluations]
            evidence:dict[UUID,list[UUID]]={}
            criteria:dict[UUID,list[ProcedureCriterionReport]]={}
            if ids:
                cur.execute(
                    """SELECT evaluation_id,step_event_id
                       FROM procedure_evaluation_step_evidence
                       WHERE evaluation_id=ANY(%s) AND user_id=%s AND instance_id=%s
                       ORDER BY step_event_id""",
                    (ids,actor.user_id,instance_id),
                )
                for row in cur.fetchall():
                    evidence.setdefault(row["evaluation_id"],[]).append(row["step_event_id"])
                cur.execute(
                    """SELECT evaluation_id,criterion_position,reported_state
                       FROM procedure_evaluation_criteria
                       WHERE evaluation_id=ANY(%s) AND user_id=%s AND instance_id=%s
                       ORDER BY criterion_position""",
                    (ids,actor.user_id,instance_id),
                )
                for row in cur.fetchall():
                    criteria.setdefault(row["evaluation_id"],[]).append(
                        ProcedureCriterionReport(
                            position=row["criterion_position"],state=row["reported_state"],
                        )
                    )
            return tuple(ProcedureEvaluationView(
                evaluation_id=e["evaluation_id"],run_id=e["run_id"],
                evaluation_revision=e["evaluation_revision"],
                run_version=e["run_version"],
                supersedes_evaluation_id=e["supersedes_evaluation_id"],
                outcome=e["outcome"],failure_cause=e["failure_cause"],
                rationale=e["rationale"],confidence=e["confidence"],
                supporting_step_event_ids=tuple(evidence.get(e["evaluation_id"],())),
                criteria=tuple(criteria.get(e["evaluation_id"],())),
            ) for e in evaluations)


def read_procedure_adaptation(connection: Connection, actor: TrustedActor,
                              instance_id: UUID, proposal_id: UUID
                              ) -> ProcedureAdaptationView:
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            cur.execute(
                """SELECT p.*,d.decision,d.applied_revision
                   FROM procedure_adaptation_proposals p
                   LEFT JOIN procedure_adaptation_decisions d
                     ON d.proposal_id=p.proposal_id
                   WHERE p.proposal_id=%s AND p.user_id=%s AND p.instance_id=%s""",
                (proposal_id,actor.user_id,instance_id),
            )
            p=cur.fetchone()
            if p is None:
                raise PermissionDenied("private adaptation unavailable")
            status=("APPLIED" if p["decision"]=="APPLY" else
                    "REJECTED" if p["decision"]=="REJECT" else "PROPOSED")
            if p["decision"] is None:
                # Effective readiness is derived, not written over immutable
                # proposal history: the private revision, run and latest
                # evaluation can all change after the proposal was recorded.
                cur.execute(
                    """SELECT current_revision FROM personalized_procedures
                       WHERE variant_id=%s AND user_id=%s AND instance_id=%s""",
                    (p["variant_id"],actor.user_id,instance_id),
                )
                current=cur.fetchone()
                run=_run(cur,actor,instance_id,p["run_id"],lock=False)
                latest=_latest_evaluation(cur,actor,instance_id,p["run_id"])
                if (current is None
                    or current["current_revision"]!=p["expected_revision"]
                    or latest is None
                    or latest["evaluation_id"]!=p["evaluation_id"]
                    or run["version"]!=latest["run_version"]):
                    status="STALE"
            return ProcedureAdaptationView(
                proposal_id=p["proposal_id"],evaluation_id=p["evaluation_id"],
                run_id=p["run_id"],variant_id=p["variant_id"],
                expected_revision=p["expected_revision"],
                template_version=p["template_version"],
                policy=p["policy"],parameters=p["parameters"],
                step_text_overrides=p["step_text_overrides"],
                reason=p["reason"],
                status=status,applied_revision=p["applied_revision"],
            )


def summarize_personal_procedure_experience(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    template_id: UUID, *, variant_id: UUID | None = None,
    limit: int = 50,
) -> ProcedureExperienceView:
    """Latest assessment per run, bounded and scoped, with no quality score.

    Distinct procedures and contexts are never pooled across personas.
    An evaluation correction replaces its predecessor in this projection,
    but append-only history remains separately readable.
    """
    require_idle(connection)
    _require_actor(actor)
    if not 1 <= limit <= 100:
        raise ValueError("experience sample limit must be 1..100")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _read_selected(cur,actor,instance_id)
            if variant_id is not None:
                variant=_variant_revision(
                    cur,actor,instance_id,variant_id,for_update=False,
                )
                if variant["template_id"] != template_id:
                    raise PermissionDenied("private method does not match template")
            cur.execute(
                """WITH latest AS (
                     SELECT DISTINCT ON (run_id)
                         run_id,outcome,failure_cause
                     FROM procedure_run_evaluations
                     WHERE user_id=%s AND instance_id=%s
                     ORDER BY run_id,evaluation_revision DESC
                   )
                   SELECT run.run_id,latest.outcome,latest.failure_cause
                   FROM procedure_runs run
                   LEFT JOIN latest ON latest.run_id=run.run_id
                   WHERE run.user_id=%s AND run.instance_id=%s
                     AND run.template_id=%s
                     AND (%s::uuid IS NULL OR run.variant_id=%s::uuid)
                   ORDER BY run.created_at DESC,run.run_id
                   LIMIT %s""",
                (actor.user_id,instance_id,actor.user_id,instance_id,
                 template_id,variant_id,variant_id,limit+1),
            )
            rows=cur.fetchall()
    truncated=len(rows)>limit
    rows=rows[:limit]
    counts={
        "USER_REPORTED_SUCCESS":0,
        "USER_REPORTED_FAILURE":0,
        "INCOMPLETE":0,"UNKNOWN":0,None:0,
    }
    causes:dict[str,int]={}
    for row in rows:
        counts[row["outcome"]]+=1
        if row["failure_cause"] is not None:
            causes[row["failure_cause"]]=causes.get(row["failure_cause"],0)+1
    return ProcedureExperienceView(
        template_id=template_id,variant_id=variant_id,
        observed_run_count=len(rows),
        reported_success_count=counts["USER_REPORTED_SUCCESS"],
        reported_failure_count=counts["USER_REPORTED_FAILURE"],
        incomplete_count=counts["INCOMPLETE"],
        unknown_count=counts["UNKNOWN"],
        unevaluated_run_count=counts[None],
        failure_causes=causes,
        run_ids=tuple(row["run_id"] for row in rows),
        truncated=truncated,
    )
