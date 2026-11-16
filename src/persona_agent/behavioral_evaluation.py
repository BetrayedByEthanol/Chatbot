"""MVP-01E: read-only, evidence-rich evaluation of the real persona host.

No test-only domain mutations: only initial synthetic identity provisioning and
normal authenticated conversation, delivery and worker contracts are used.
Snapshots contain synthetic conversation text and must be handled as private.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import platform
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .acceptance_host import seed_local_demo
from .behavioral_acceptance import BehavioralAcceptanceHarness
from .behavioral_diagnostics import describe_interpretation, resolve_interpretation
from .contracts import TrustedActor
from .dialogue_state import load_active_dialogue
from .model_gateway import OllamaChatGateway
from .observability import get_turn_trace
from .tool_gateway import run_one_text_tool


# Fixed allowlist: no scenario-supplied SQL identifiers or admin data exports.
# All tables expose (user_id, instance_id) under the v2.5.1 schema.
TABLES = (
    "conversations", "conversation_turns", "conversation_responses",
    "conversation_intent_signals", "dialogue_threads",
    "dialogue_pending_acts", "dialogue_turn_links", "dialogue_referents",
    "conversation_tool_calls", "conversation_assistant_followups",
    "memory_records", "memory_model_candidates", "goal_records", "cognitive_reflections",
    "cognitive_intentions", "emotion_appraisals",
    "relationship_observations", "operations", "events",
)
TABLE_ROW_LIMIT = 500


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    """JSON-compatible copy without relying on a mutable domain object."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


# Preserve the public import contract for existing callers.
from .scenario_validation import validate_scenario


class RecordingGateway:
    """Record the real Ollama HTTP payload, or label mock preflight evidence.

    Ollama's generate_interpreted() inserts its own system instruction. For
    that gateway, observe in _chat() *after* insertion rather than logging
    the upstream messages before the instruction exists. Non-Ollama test
    gateways can only provide explicitly labeled pre-gateway evidence.
    """

    def __init__(self, inner):
        self.requests: list[dict] = []
        self.raw_outputs: list[dict] = []
        self.interpretations: list[dict] = []
        self._outbound_capture = isinstance(inner, OllamaChatGateway)
        if self._outbound_capture:
            previous_observer = inner.request_observer

            def capture(payload):
                self.requests.append({
                    "at":timestamp(),
                    "route":(
                        "generate_interpreted" if payload.get("format")=="json"
                        else "generate"
                    ),
                    "capture_boundary":"ollama.outbound",
                    "payload":jsonable(payload),
                    "messages":jsonable(payload["messages"]),
                })
                if previous_observer is not None:
                    previous_observer(payload)

            previous_response_observer = inner.response_observer

            def capture_response(raw, json_mode):
                self.raw_outputs.append({
                    "raw":raw, "json_mode":bool(json_mode),
                })
                if previous_response_observer is not None:
                    previous_response_observer(raw,json_mode)

            # Immutable clone: do not mutate a production gateway instance.
            self.inner = replace(
                inner, request_observer=capture,
                response_observer=capture_response,
            )
        else:
            self.inner = inner

    def _record_pre_gateway(self, messages, route):
        if not self._outbound_capture:
            self.requests.append({
                "at":timestamp(), "route":route,
                "capture_boundary":"pre_gateway_unverified",
                "messages":[
                    {"role":m.role, "content":m.content} for m in messages
                ],
            })

    def generate(self, messages):
        self._record_pre_gateway(messages, "generate")
        return self.inner.generate(messages)

    def generate_interpreted(self, messages):
        method = getattr(self.inner, "generate_interpreted", None)
        if method is None:
            return self.generate(messages)
        self._record_pre_gateway(messages, "generate_interpreted")
        before = len(self.raw_outputs)
        try:
            reply = method(messages)
        except Exception as exc:
            # Preserve the original gateway error, even if raw JSON was bad.
            raw = self.raw_outputs[-1]["raw"] if len(self.raw_outputs)>before else None
            self.interpretations.append({
                "at":timestamp(), **describe_interpretation(raw),
                "gateway_error_type":type(exc).__name__,
            })
            raise
        raw = self.raw_outputs[-1]["raw"] if len(self.raw_outputs)>before else None
        self.interpretations.append({
            "at":timestamp(),
            **describe_interpretation(raw,reply.intent_candidate),
        })
        return reply


def capture_state(
    dsn: str, harness: BehavioralAcceptanceHarness, *,
    turn_id: str | None, checkpoint: str,
) -> dict:
    """Independent application read and tenant-scoped READ ONLY SQL snapshot.

    The SQL snapshot is transactionally consistent across all tables.
    Historical rows are intentionally retained to inspect transitions.
    """
    identity = harness.active_identity
    actor = TrustedActor(
        user_id=identity.user_id, source_id="mvp01e-state-observer",
    )
    app = {"counts":harness.observe_state()}
    with psycopg.connect(dsn, autocommit=True) as conn:
        app["dialogue"] = load_active_dialogue(
            conn, actor, identity.conversation_id,
        ).model_dump(mode="json")
        if turn_id is not None:
            app["turn_trace"] = get_turn_trace(
                conn, actor, UUID(turn_id),
            ).model_dump(mode="json")
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            with conn.cursor(row_factory=dict_row) as cur:
                tables = {}
                for name in TABLES:
                    # TABLES is a compile-time constant, never scenario input.
                    cur.execute(
                        sql.SQL("SELECT count(*) AS n FROM {} "
                                "WHERE user_id=%s AND instance_id=%s").format(
                            sql.Identifier(name),
                        ),
                        (identity.user_id, identity.instance_id),
                    )
                    total = cur.fetchone()["n"]
                    cur.execute(
                        sql.SQL("SELECT * FROM {} WHERE user_id=%s "
                                "AND instance_id=%s ORDER BY ctid LIMIT %s").format(
                            sql.Identifier(name),
                        ),
                        (identity.user_id, identity.instance_id, TABLE_ROW_LIMIT),
                    )
                    tables[name] = {
                        "count": total,
                        "truncated": total > TABLE_ROW_LIMIT,
                        "rows": jsonable(cur.fetchall()),
                    }
    return {
        "at": timestamp(), "checkpoint": checkpoint,
        "session": harness.active_identity.conversation_id.hex,
        "source_turn_id": turn_id, "application": app, "database": tables,
    }


def _signals_for_turn(snapshot: dict, turn_id: str) -> list[dict]:
    return [
        row for row in snapshot["database"]["conversation_intent_signals"]["rows"]
        if str(row["turn_id"]) == turn_id
    ]


def _check_turn(
    step: dict, turn: dict, snapshot: dict, *,
    previous_operations: int | None = None,
) -> list[dict]:
    """Outcome-based assertions: no model-text substring guessing.

    A failed deterministic behavioral assertion is reported, not hidden by
    successful response-delivery contracts. Interpretation stays tentative.
    """
    checks = []

    def record(name, passed, actual, expected, category="behavioral"):
        checks.append({
            "check":name, "category":category, "pass":bool(passed),
            "actual":actual, "expected":expected,
        })

    trace = snapshot["application"]["turn_trace"]
    record("response_delivered",trace["response_status"]=="DELIVERED",
           trace["response_status"],"DELIVERED",category="contract")
    if "expect_model_id" in step:
        record("route",turn["model_id"]==step["expect_model_id"],
               turn["model_id"],step["expect_model_id"],category="contract")
    signals = _signals_for_turn(snapshot,turn["turn_id"])
    if "expect_intent" in step:
        kinds = [row["intent_kind"] for row in signals]
        expected=step["expect_intent"]
        record("intent",expected in kinds if expected else not kinds,
               kinds,expected)
    if "expect_related_intent" in step:
        by_id={
            str(row["signal_id"]):row
            for row in snapshot["database"]["conversation_intent_signals"]["rows"]
        }
        resolved=[
            by_id[str(row["related_signal_id"])]["intent_kind"]
            for row in signals
            if row.get("related_signal_id") is not None
            and str(row["related_signal_id"]) in by_id
        ]
        record("related_intent",step["expect_related_intent"] in resolved,
               resolved,step["expect_related_intent"])
    if "expect_focus_contains" in step:
        dialogue=snapshot["application"]["dialogue"]
        focused=next((
            row["title"] for row in dialogue["threads"]
            if str(row["thread_id"])==str(dialogue["focus_thread_id"])
        ),None)
        expected=step["expect_focus_contains"]
        record("focused_topic",focused is not None
               and expected.casefold() in focused.casefold(),focused,expected)
    if step.get("expect_no_new_operations"):
        total=snapshot["database"]["operations"]["count"]
        record("no_new_operations",previous_operations is not None
               and total==previous_operations,total,previous_operations)
    count = snapshot["application"]["counts"]["goals"]
    record("no_unapproved_goal",count == 0,count,0,category="contract")
    return checks


def _committed_input_receipt(
    dsn: str, harness: BehavioralAcceptanceHarness, idempotency_key: str,
) -> dict | None:
    """Find only this authenticated ingress receipt, never an earlier turn.

    An inference failure can occur *after* ingest_text commits its event,
    intent signal and pending user turn, but before the host returns an ID.
    The durable command receipt provides an exact idempotency-key correlation
    without rerunning ingress or assuming the last turn is this attempt.
    """
    from .conversation import _INPUT_COMMAND

    identity = harness.active_identity
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT result_json, event_id FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s
                         AND command_type=%s AND idempotency_key=%s""",
                    (identity.user_id, identity.instance_id,
                     _INPUT_COMMAND, idempotency_key),
                )
                row = cur.fetchone()
    if row is None or not isinstance(row["result_json"], dict):
        return None
    result = row["result_json"]
    if str(result.get("conversation_id")) != str(identity.conversation_id):
        return None
    return {
        "turn_id":str(result["turn_id"]),
        "source_event_id":str(row["event_id"]),
        "sequence":result["sequence"],
    }


def _compare_semantic_expectations(expected: list[dict], diagnostic: dict) -> bool:
    """Label accuracy is behavioral evidence, never an admission contract."""
    batch=diagnostic.get("batch")
    if diagnostic["status"]=="ERROR" or not isinstance(batch,dict):
        return False
    claims=batch["claims"]
    return len(expected)==len(claims) and all(
        all(observed.get(key)==value for key,value in item.items())
        for item,observed in zip(expected,claims)
    )


def run_scenario(
    dsn: str, scenario: dict, gateway,
    *, run_tool_worker: bool = True, run_memory_worker: bool = True,
    run_model_memory_worker: bool = False,
    run_semantic_diagnostics: bool = False,
    semantic_classifier=None,
) -> dict:
    """Run ordinary model/tool conversations; return full evidence on failure."""
    scenario = validate_scenario(scenario)
    if semantic_classifier is not None and not run_semantic_diagnostics:
        raise ValueError("semantic classifier supplied without diagnostics mode")
    if not run_semantic_diagnostics and any(
        "expect_semantics" in step for step in scenario["steps"]
    ):
        raise ValueError("semantic expectations require --semantic-diagnostics")
    if run_semantic_diagnostics and semantic_classifier is None:
        from .semantic_classification import RuleSemanticClassifier
        semantic_classifier = RuleSemanticClassifier()
    identity = seed_local_demo(dsn)
    observed_gateway = RecordingGateway(gateway)
    harness = BehavioralAcceptanceHarness(dsn, identity, observed_gateway)
    if run_memory_worker:
        harness.enable_explicit_memory_worker()
    model_memory_consumer = None
    if run_model_memory_worker:
        from uuid import uuid4
        from .memory_model_extraction import register_model_memory_consumer
        model_memory_consumer = "mvp02c2-e2e-" + uuid4().hex
        with psycopg.connect(dsn,autocommit=True) as conn:
            register_model_memory_consumer(
                conn,consumer_id=model_memory_consumer,
            )
    report: dict[str, Any] = {
        "format_version": "mvp01e.1", "name":scenario["name"],
        "started_at":timestamp(), "synthetic":True,
        "identity":{"user_id":str(identity.user_id),
                    "instance_id":str(identity.instance_id)},
        "transcript":[], "snapshots":[], "checks":[], "errors":[],
        "review_items":[],
        "interpretation_diagnostics":observed_gateway.interpretations,
        "semantic_diagnostics":[],
        "semantic_diagnostics_enabled":run_semantic_diagnostics,
        "model_requests":observed_gateway.requests, "deliveries":harness.deliveries,
    }
    for index, step in enumerate(scenario["steps"]):
        recorded_message = False
        interpretation_before = len(observed_gateway.interpretations)
        try:
            action = step["action"]
            if action == "session":
                harness.begin_session(step["name"])
                report["transcript"].append({
                    "index":index,"action":"session",
                    "name":step["name"],
                    "conversation_id":str(harness.active_identity.conversation_id),
                })
                continue
            if action == "restart":
                harness.restart_host()
                report["transcript"].append({
                    "index":index,"action":"restart",
                    "conversation_id":str(harness.active_identity.conversation_id),
                })
                continue
            if action == "approve_model_memory":
                from .memory_model_extraction import (
                    list_model_memory_candidates,approve_model_memory_candidate,
                )
                identity=harness.active_identity
                actor=TrustedActor(
                    user_id=identity.user_id,source_id="mvp02c2-e2e-user-review",
                )
                with psycopg.connect(dsn,autocommit=True) as conn:
                    candidates=[
                        c for c in list_model_memory_candidates(
                            conn,actor,identity.instance_id,
                        ) if c.predicate_key==step["predicate_key"]
                    ]
                    if len(candidates)!=1:
                        raise ValueError(
                            "expected exactly one reviewable model-memory candidate"
                        )
                    result=approve_model_memory_candidate(
                        conn,actor,candidates[0].candidate_id,
                        expected_generation=identity.generation,
                    )
                report["transcript"].append({
                    "index":index,"action":"approve_model_memory",
                    "candidate_id":str(candidates[0].candidate_id),
                    "predicate_key":step["predicate_key"],
                    "memory_id":str(result.memory_id),"version":result.version,
                })
                snap=capture_state(
                    dsn,harness,turn_id=None,
                    checkpoint="after_explicit_model_memory_approval",
                )
                snap["step_index"]=index
                report["snapshots"].append(snap)
                continue
            previous_operations=(
                report["snapshots"][-1]["database"]["operations"]["count"]
                if report["snapshots"] else 0
            )
            turn = harness.send(
                step["text"],idempotency_key=f"mvp01e-{index:04d}",
                mode=step.get("mode","auto"),
            )
            recorded = {
                "index":index,"action":"message",
                "conversation_id":str(harness.active_identity.conversation_id),
                "input":step["text"],"assistant":turn.text,
                "turn_id":turn.turn_id,"response_id":turn.response_id,
                "model_id":turn.model_id,"replayed":turn.replayed,
            }
            report["transcript"].append(recorded)
            recorded_message = True
            current = capture_state(
                dsn,harness,turn_id=turn.turn_id,checkpoint="after_delivery",
            )
            current["step_index"] = index
            report["snapshots"].append(current)
            # Stage A classification is evaluated in shadow AFTER delivery:
            # no inference latency or model output can affect this response.
            # Classifier proposals never write Memory, Goals or Dialogue.
            if run_semantic_diagnostics:
                from .semantic_classification import diagnostic_for_turn
                diagnostic=diagnostic_for_turn(
                    semantic_classifier,text=step["text"],
                    source_turn_id=UUID(turn.turn_id),
                )
                diagnostic.update({
                    "step_index":index,
                    "conversation_id":recorded["conversation_id"],
                })
                report["semantic_diagnostics"].append(diagnostic)
                recorded["semantic_diagnostic_status"]=diagnostic["status"]
                report["checks"].append({
                    "step_index":index,
                    "check":"semantic_candidate_contract",
                    "category":"contract",
                    "pass":diagnostic["status"]!="ERROR",
                    "actual":diagnostic["status"],
                    "expected":"ANNOTATED or ABSTAINED",
                })
                if "expect_semantics" in step:
                    report["checks"].append({
                        "step_index":index,
                        "check":"semantic_labels",
                        "category":"behavioral",
                        "pass":_compare_semantic_expectations(
                            step["expect_semantics"],diagnostic,
                        ),
                        "actual":(
                            diagnostic.get("batch",{}).get("claims",[])
                        ),
                        "expected":step["expect_semantics"],
                    })
            if len(observed_gateway.interpretations)>interpretation_before:
                item=observed_gateway.interpretations[-1]
                item.update({
                    "step_index":index, "turn_id":turn.turn_id,
                    "conversation_id":recorded["conversation_id"],
                })
                resolve_interpretation(
                    item,input_text=step["text"],turn_id=turn.turn_id,
                    snapshot=current,
                )
            report["checks"].extend({
                "step_index":index, **c,
            } for c in _check_turn(
                step,recorded,current,previous_operations=previous_operations,
            ))
            if "review_response" in step:
                report["review_items"].append({
                    "step_index":index,"turn_id":turn.turn_id,
                    "review_question":step["review_response"],
                    "user":step["text"],"assistant":turn.text,
                    "status":"PENDING_HUMAN_REVIEW",
                })
            if run_tool_worker and turn.model_id == "tool.initial.deferred.v1":
                # Use the real registered worker/claim/lease lifecycle,
                # not a fabricated completed operation or test-side mutation.
                with psycopg.connect(dsn,autocommit=True) as conn:
                    lease = run_one_text_tool(
                        conn, worker_id="mvp01e-registered-tool-worker",
                    )
                recorded["worker_claimed"] = lease is not None
                delivered = harness.poll_followups()
                recorded["followups_delivered"] = delivered
                extra = capture_state(
                    dsn,harness,turn_id=turn.turn_id,
                    checkpoint="after_worker_and_followup",
                )
                extra["step_index"] = index
                report["snapshots"].append(extra)
                report["checks"].append({
                    "step_index":index, "check":"tool_followup",
                    "pass":lease is not None and delivered == 1,
                    "actual":{"claimed":lease is not None,"delivered":delivered},
                    "expected":{"claimed":True,"delivered":1},
                })
            if run_memory_worker:
                recorded["memory_worker"] = harness.tick_explicit_memory_worker()
                memory_state = capture_state(
                    dsn,harness,turn_id=turn.turn_id,
                    checkpoint="after_explicit_memory_worker",
                )
                memory_state["step_index"] = index
                report["snapshots"].append(memory_state)
            if run_model_memory_worker:
                from .memory_model_extraction import run_model_memory_tick
                with psycopg.connect(dsn,autocommit=True) as conn:
                    model_tick=run_model_memory_tick(
                        conn,observed_gateway,
                        consumer_id=model_memory_consumer,
                        worker_id="mvp02c2-e2e-model-memory",
                    )
                recorded["model_memory_worker"]=vars(model_tick)
                model_state=capture_state(
                    dsn,harness,turn_id=turn.turn_id,
                    checkpoint="after_reviewed_model_memory_worker",
                )
                model_state["step_index"]=index
                report["snapshots"].append(model_state)
                if "expect_model_memory_proposed" in step:
                    actual=model_tick.proposed==1
                    report["checks"].append({
                        "step_index":index,
                        "check":"model_memory_proposal",
                        "pass":actual==step["expect_model_memory_proposed"],
                        "actual":actual,
                        "expected":step["expect_model_memory_proposed"],
                    })
        except Exception as exc:
            # Preserve the original failure first. Neither receipt lookup
            # nor evidence collection may mask it if observation also fails.
            report["errors"].append({
                "step_index":index,"type":type(exc).__name__,
                "message":str(exc)[:1000],
            })
            if step["action"] == "message" and not recorded_message:
                if len(observed_gateway.interpretations)>interpretation_before:
                    observed_gateway.interpretations[-1]["step_index"]=index
                key = f"mvp01e-{index:04d}"
                failed = {
                    "index":index, "action":"message_failed",
                    "conversation_id":str(harness.active_identity.conversation_id),
                    "input":step["text"],"idempotency_key":key,
                    "assistant":None,"turn_id":None,"ingress_committed":False,
                    "error_type":type(exc).__name__,
                }
                report["transcript"].append(failed)
                try:
                    receipt = _committed_input_receipt(dsn,harness,key)
                    if receipt is not None:
                        failed.update(receipt)
                        failed["ingress_committed"] = True
                        snapshot = capture_state(
                            dsn,harness,turn_id=receipt["turn_id"],
                            checkpoint="after_failed_turn",
                        )
                        snapshot["step_index"] = index
                        report["snapshots"].append(snapshot)
                        if len(observed_gateway.interpretations)>interpretation_before:
                            item=observed_gateway.interpretations[-1]
                            item["turn_id"]=receipt["turn_id"]
                            resolve_interpretation(
                                item,input_text=step["text"],
                                turn_id=receipt["turn_id"],snapshot=snapshot,
                            )
                except Exception as observation_exc:
                    report["errors"].append({
                        "step_index":index, "phase":"failure_observation",
                        "type":type(observation_exc).__name__,
                        "message":str(observation_exc)[:1000],
                    })
            break
    report["completed_at"] = timestamp()
    contracts = [x for x in report["checks"] if x.get("category","contract")=="contract"]
    behavior = [x for x in report["checks"] if x.get("category")=="behavioral"]
    report["contract_passed"] = (
        not report["errors"] and bool(contracts)
        and all(x["pass"] for x in contracts)
    )
    report["behavioral_checks_passed"] = (
        not report["errors"] and all(x["pass"] for x in behavior)
    )
    report["passed"] = (
        report["contract_passed"] and report["behavioral_checks_passed"]
    )
    # No automated test declares semantic or capability-claim acceptance.
    report["requires_human_review"] = True
    report["behavioral_accepted"] = None
    return report


def write_bundle(
    report: dict, scenario: dict, destination: Path,
    *, model: str, git_sha: str | None, migration_hashes: dict,
) -> Path:
    """Atomically publish a self-contained artifact before any DB teardown."""
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version":report["format_version"],"started_at":report["started_at"],
        "finished_at":report["completed_at"],"synthetic_data":True,
        "model":model,"git_sha":git_sha,
        "python":sys.version.split()[0],
        "platform":platform.platform(),
        "migration_sha256":migration_hashes,
        "contract_passed":report.get("contract_passed",report["passed"]),
        "behavioral_checks_passed":report.get("behavioral_checks_passed"),
        "behavioral_accepted":None,
        "conversational_quality":"NOT_AUTOMATICALLY_GRADED",
        "snapshot_row_limit":TABLE_ROW_LIMIT,
        "semantic_diagnostics_enabled":report.get(
            "semantic_diagnostics_enabled",False,
        ),
        "warnings":[
            "Contains full synthetic conversations and model prompt context.",
            "Do not upload a bundle created using real user data.",
        ],
    }
    files = {
        "manifest.json":manifest,
        "scenario.json":scenario,
        "transcript.json":report["transcript"],
        "state_snapshots.json":report["snapshots"],
        "model_requests.json":report["model_requests"],
        "interpretation_diagnostics.json":report.get("interpretation_diagnostics",[]),
        "semantic_diagnostics.json":report.get("semantic_diagnostics",[]),
        "review_items.json":report.get("review_items",[]),
        "delivery_events.json":report["deliveries"],
        "evaluation.json":{
            "contract_passed":report.get("contract_passed",report["passed"]),
            "behavioral_checks_passed":report.get("behavioral_checks_passed"),
            "behavioral_accepted":None,
            "checks":report["checks"],
            "conversational_quality":"REQUIRES_HUMAN_REVIEW",
        },
        "errors.json":report["errors"],
    }
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with ZipFile(temporary,"w",compression=ZIP_DEFLATED) as archive:
            for name,value in files.items():
                archive.writestr(
                    name,json.dumps(value,indent=2,ensure_ascii=False,
                                    default=str),
                )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def git_revision() -> str | None:
    result = subprocess.run(
        ["git","rev-parse","HEAD"],capture_output=True,text=True,
        check=False,timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def hash_migrations(paths: list[Path]) -> dict:
    return {path.name:sha256(path.read_bytes()).hexdigest() for path in paths}
