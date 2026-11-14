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
    "memory_records", "goal_records", "cognitive_reflections",
    "cognitive_intentions", "emotion_appraisals",
    "relationship_observations", "operations", "events",
)
TABLE_ROW_LIMIT = 500


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    """JSON-compatible copy without relying on a mutable domain object."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def validate_scenario(value: Any) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ValueError("scenario requires a name")
    steps = value.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("scenario requires 1..100 steps")
    seen_sessions = set()
    active = False
    message_count = 0
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("every scenario step must be an object")
        action = step.get("action")
        if action == "session":
            name = step.get("name")
            if not isinstance(name, str) or not name or name in seen_sessions:
                raise ValueError("session names must be unique and nonempty")
            seen_sessions.add(name)
            active = True
        elif action == "restart":
            if not active:
                raise ValueError("restart requires an active session")
        elif action == "message":
            text = step.get("text")
            if (not active or not isinstance(text, str)
                    or not 1 <= len(text) <= 12000):
                raise ValueError("message requires an active session and text")
            if step.get("mode", "auto") not in ("auto", "model", "time", "analyze"):
                raise ValueError("invalid message mode")
            if "expect_model_id" in step and not isinstance(
                step["expect_model_id"], str
            ):
                raise ValueError("expect_model_id must be a string")
            if "expect_intent" in step and step["expect_intent"] not in (
                "OBJECTIVE", "REQUEST", "CORRECTION", "HYPOTHETICAL",
                "OBSERVATION", "INSTRUCTION", None,
            ):
                raise ValueError("unknown expected intent kind")
            message_count += 1
        else:
            raise ValueError("unsupported scenario action")
    if not message_count:
        raise ValueError("scenario must contain at least one message")
    return value


class RecordingGateway:
    """Record the real Ollama HTTP payload, or label mock preflight evidence.

    Ollama's generate_interpreted() inserts its own system instruction. For
    that gateway, observe in _chat() *after* insertion rather than logging
    the upstream messages before the instruction exists. Non-Ollama test
    gateways can only provide explicitly labeled pre-gateway evidence.
    """

    def __init__(self, inner):
        self.requests: list[dict] = []
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

            # Immutable clone: do not mutate a production gateway instance.
            self.inner = replace(inner, request_observer=capture)
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
        return method(messages)


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


def _intent_from_snapshot(snapshot: dict, turn_id: str) -> str | None:
    for row in snapshot["database"]["conversation_intent_signals"]["rows"]:
        if str(row["turn_id"]) == turn_id:
            return row["intent_kind"]
    return None


def _check_turn(step: dict, turn: dict, snapshot: dict) -> list[dict]:
    checks = []
    def record(name, passed, actual, expected):
        checks.append({
            "check": name, "pass": bool(passed),
            "actual": actual, "expected": expected,
        })
    trace = snapshot["application"]["turn_trace"]
    record("response_delivered",trace["response_status"] == "DELIVERED",
           trace["response_status"],"DELIVERED")
    if "expect_model_id" in step:
        record("route",turn["model_id"] == step["expect_model_id"],
               turn["model_id"],step["expect_model_id"])
    if "expect_intent" in step:
        observed = _intent_from_snapshot(snapshot, turn["turn_id"])
        record("intent",observed == step["expect_intent"],
               observed,step["expect_intent"])
    # All MVP-01E scenario objectives are discussions, never authorized goals.
    count = snapshot["application"]["counts"]["goals"]
    record("no_unapproved_goal",count == 0,count,0)
    return checks


def run_scenario(
    dsn: str, scenario: dict, gateway,
    *, run_tool_worker: bool = True, run_memory_worker: bool = True,
) -> dict:
    """Run ordinary model/tool conversations; return full evidence on failure."""
    scenario = validate_scenario(scenario)
    identity = seed_local_demo(dsn)
    observed_gateway = RecordingGateway(gateway)
    harness = BehavioralAcceptanceHarness(dsn, identity, observed_gateway)
    if run_memory_worker:
        harness.enable_explicit_memory_worker()
    report: dict[str, Any] = {
        "format_version": "mvp01e.1", "name":scenario["name"],
        "started_at":timestamp(), "synthetic":True,
        "identity":{"user_id":str(identity.user_id),
                    "instance_id":str(identity.instance_id)},
        "transcript":[], "snapshots":[], "checks":[], "errors":[],
        "model_requests":observed_gateway.requests, "deliveries":harness.deliveries,
    }
    for index, step in enumerate(scenario["steps"]):
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
            current = capture_state(
                dsn,harness,turn_id=turn.turn_id,checkpoint="after_delivery",
            )
            current["step_index"] = index
            report["snapshots"].append(current)
            report["checks"].extend({
                "step_index":index, **c,
            } for c in _check_turn(step,recorded,current))
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
        except Exception as exc:
            report["errors"].append({
                "step_index":index,"type":type(exc).__name__,
                "message":str(exc)[:1000],
            })
            break
    report["completed_at"] = timestamp()
    report["passed"] = (
        not report["errors"]
        and len(report["checks"]) > 0
        and all(c["pass"] for c in report["checks"])
    )
    # All passes are contract passes, never a claim of semantic intelligence.
    report["requires_human_review"] = True
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
        "contract_passed":report["passed"],
        "conversational_quality":"NOT_AUTOMATICALLY_GRADED",
        "snapshot_row_limit":TABLE_ROW_LIMIT,
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
        "delivery_events.json":report["deliveries"],
        "evaluation.json":{
            "contract_passed":report["passed"],
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
