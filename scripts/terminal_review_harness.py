#!/usr/bin/env python3
"""Disposable local terminal reviews using the existing persona runtime contracts.

Human mode captures real, non-sensitive test conversations for manual review.
Goal mode injects a fixed conversation and verifies explicit owner-goal admission.
Neither mode provides a remotely authenticated API or authorizes model actions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from uuid import UUID, uuid4

import psycopg

from persona_e2e import create_isolated_database, drop_isolated_database
from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.contracts import TrustedActor
from persona_agent.goal_contracts import CreateGoalCommand, goal_action_approval_phrase
from persona_agent.goal_state import create_goal, list_goals
from persona_agent.model_gateway import OllamaChatGateway
from persona_agent.runtime import PermissionDenied

ROOT = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_private(path: Path, content: object) -> None:
    """Publish one report with 0600 permissions; never overwrite prior evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(content, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class PrivateJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.stream = os.fdopen(
            os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
            "w", encoding="utf-8",
        )

    def append(self, event: dict) -> None:
        self.stream.write(json.dumps({"at": _now(), **event}, ensure_ascii=False) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self) -> None:
        self.stream.close()


def _prepare(admin_dsn: str):
    """Create and migrate only a newly owned, random-name test database."""
    name, dsn = create_isolated_database(admin_dsn)
    try:
        migrations = sorted((ROOT / "migrations").glob("*.sql"))
        if not migrations:
            raise RuntimeError("no database migrations found")
        with psycopg.connect(dsn, autocommit=True) as connection:
            for migration in migrations:
                with connection.transaction():
                    connection.execute(migration.read_text(encoding="utf-8"))
        return name, dsn
    except Exception:
        # No report has been published; preserve the DB for diagnosis.
        print(f"Setup failed; isolated database retained: {name}", file=sys.stderr)
        raise


def goal_injection(dsn: str, gateway, report: dict | None = None) -> dict:
    """Inject user turns; use only authenticated Goal owner APIs for mutations."""
    identity = seed_local_demo(dsn)
    harness = BehavioralAcceptanceHarness(dsn, identity, gateway)
    harness.begin_session("tentative_goal")
    actor = TrustedActor(user_id=identity.user_id, source_id="terminal-goal-review-user")
    if report is None:
        report = {}
    report.update({
        "kind": "terminal-goal-injection-v1", "synthetic": True,
        "started_at": _now(), "checks": [], "transcript": [],
        "manual_review": "PENDING_HUMAN_REVIEW",
    })

    def check(name: str, result: bool, detail: object = None) -> None:
        report["checks"].append({"check": name, "pass": bool(result), "detail": detail})
        if not result:
            raise AssertionError(f"goal-injection check failed: {name}: {detail}")

    def send(text: str, *, label: str):
        response = harness.send(
            text, idempotency_key=f"terminal-goal-{uuid4().hex}", mode="model",
        )
        report["transcript"].append({
            "label": label, "user": text, "assistant": response.text,
            "turn_id": response.turn_id, "response_id": response.response_id,
            "session": response.session,
        })
        return response

    first = send("I want to walk three times a week.", label="tentative_objective")
    correction = send("Actually, twice per week is more realistic.", label="correction")
    send("Let's switch topics to Nginx certificates.", label="topic_switch")
    send("What was my walking plan?", label="preapproval_recall")
    harness.restart_host()
    check("no_implicit_goal_before_approval", harness.observe_state()["goals"] == 0)
    signals = harness.observe_intent_signals()
    check("objective_signal_source_linked", any(
        x["intent_kind"] == "OBJECTIVE" and x["turn_id"] == first.turn_id
        for x in signals
    ), signals)

    command = CreateGoalCommand(
        instance_id=identity.instance_id,
        expected_generation=identity.generation,
        idempotency_key=f"terminal-approve-{uuid4().hex}",
        conversation_id=harness.active_identity.conversation_id,
        evidence_turn_id=UUID(correction.turn_id),
        evidence_quote="twice per week",
        approval_turn_id=None,
        goal_id=uuid4(),
        title="Walk twice weekly",
        desired_outcome="Establish a sustainable walking habit",
        success_criterion="User reports completing two walks per week",
        horizon="MEDIUM",
    )
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            create_goal(conn, actor, command)
    except PermissionDenied:
        rejected = True
    else:
        rejected = False
    check("owner_rejects_missing_confirmation", rejected)
    check("negative_attempt_commits_no_goal", harness.observe_state()["goals"] == 0)

    # This is a *scripted, authenticated user approval turn*, not model consent.
    # The current Goal domain intentionally requires its exact action challenge.
    confirmation = send(goal_action_approval_phrase(command), label="explicit_approval")
    approved = command.model_copy(update={"approval_turn_id": UUID(confirmation.turn_id)})
    with psycopg.connect(dsn, autocommit=True) as conn:
        created = create_goal(conn, actor, approved)
        replay = create_goal(conn, actor, approved)
        goals = list_goals(conn, actor, identity.instance_id)
    check("goal_created_by_owner", created.action == "GOAL_CREATED")
    check("duplicate_command_replays", replay.replayed)
    check("exactly_one_approved_root", len(goals) == 1 and
          goals[0].goal_id == command.goal_id and goals[0].parent_goal_id is None)
    report["goal_id"] = str(command.goal_id)
    harness.begin_session("post_restart_goal_recall")
    harness.restart_host()
    send("What is my approved walking goal?", label="post_restart_recall")
    check("goal_survives_new_session", harness.observe_state()["goals"] == 1)
    report["completed_at"] = _now()
    report["passed"] = all(x["pass"] for x in report["checks"])
    return report


def human_session(dsn: str, gateway, journal: PrivateJournal) -> None:
    identity = seed_local_demo(dsn)
    harness = BehavioralAcceptanceHarness(dsn, identity, gateway)
    harness.begin_session("session_1")
    journal.append({"type": "start", "mode": "human", "model": gateway.model,
                    "identity": {"user_id": str(identity.user_id),
                                 "instance_id": str(identity.instance_id)}})
    print("Local interactive persona review. Commands: /session NAME, /restart, /state, /rate 0|1|2 NOTE, /quit")
    print("Personal text is saved locally in the private JSONL report; avoid sensitive data.")
    last_turn = None
    while True:
        try:
            line = input("You> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/session "):
            name = line.partition(" ")[2].strip()
            harness.begin_session(name)
            journal.append({"type": "session", "name": name})
            print(f"Started conversation {name!r}")
            continue
        if line == "/restart":
            harness.restart_host()
            journal.append({"type": "restart", "session": harness.active_identity.conversation_id.hex})
            print("Transport recreated; persistent state retained")
            continue
        if line == "/state":
            counts = harness.observe_state()
            journal.append({"type": "state_counts", "counts": counts})
            print(json.dumps(counts, indent=2))
            continue
        if line.startswith("/rate "):
            pieces = line.split(maxsplit=2)
            if last_turn is None or len(pieces) < 2 or pieces[1] not in ("0", "1", "2"):
                print("Usage after a reply: /rate 0|1|2 optional note")
                continue
            journal.append({"type": "human_rating", "turn_id": last_turn,
                            "score": int(pieces[1]),
                            "note": pieces[2] if len(pieces) > 2 else ""})
            print("Human rating saved; no automatic quality verdict inferred")
            continue
        if line.startswith("/"):
            print("Unknown command; /quit ends the review")
            continue
        key = "terminal-human-" + uuid4().hex
        journal.append({"type": "user", "text": line, "idempotency_key": key})
        start = perf_counter()
        try:
            turn = harness.send(line, idempotency_key=key, mode="model")
        except Exception as exc:
            journal.append({"type": "error", "error": type(exc).__name__,
                            "message": str(exc)[:500], "idempotency_key": key})
            raise  # Fail closed: do not advance dialogue after uncertain delivery.
        elapsed = round((perf_counter() - start) * 1000, 1)
        journal.append({"type": "assistant", "text": turn.text,
                        "turn_id": turn.turn_id, "response_id": turn.response_id,
                        "model_id": turn.model_id, "elapsed_ms": elapsed})
        last_turn = turn.turn_id
        print(f"Persona> {turn.text}\n")
    journal.append({"type": "end", "human_quality_verdict": "NOT_AUTOMATICALLY_EVALUATED"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for name in ("human", "goal-creation"):
        command = commands.add_parser(name)
        command.add_argument("--admin-dsn", default=os.environ.get("PERSONA_E2E_ADMIN_DSN"))
        command.add_argument("--model", default=os.environ.get("ACCEPTANCE_MODEL", "qwen2.5:7b"))
        command.add_argument("--ollama-url", default=os.environ.get("ACCEPTANCE_OLLAMA_URL", "http://127.0.0.1:11434"))
        command.add_argument("--output", type=Path)
        command.add_argument("--keep-db", action="store_true", help="Retain the isolated database for debugging")
    args = parser.parse_args(argv)
    if not args.admin_dsn:
        parser.error("PERSONA_E2E_ADMIN_DSN or --admin-dsn is required; use an isolated PostgreSQL server")
    extension = ".jsonl" if args.mode == "human" else ".json"
    output = args.output or ROOT / "artifacts" / (
        f"terminal-{args.mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}{extension}"
    )
    if output.exists():
        parser.error("refusing to overwrite an existing report")
    if output.suffix.lower() != extension:
        parser.error(f"output must have the {extension} extension")
    name = None
    saved = False
    exit_code = 0
    journal = None
    try:
        name, dsn = _prepare(args.admin_dsn)
        gateway = OllamaChatGateway(model=args.model, base_url=args.ollama_url,
                                    timeout_seconds=180)
        if args.mode == "human":
            journal = PrivateJournal(output)
            human_session(dsn, gateway, journal)
            saved = True
        else:
            report = {"kind": "terminal-goal-injection-v1", "passed": False,
                      "errors": [], "started_at": _now()}
            try:
                report = goal_injection(dsn, gateway, report)
            except Exception as exc:
                report["errors"].append({"type": type(exc).__name__,
                                         "message": str(exc)[:500]})
                exit_code = 1
            _save_private(output, report)
            saved = True
            print(f"Goal injection checks: {sum(x['pass'] for x in report.get('checks', []))} passed; quality review pending")
    except Exception as exc:
        exit_code = 1
        print(f"Review failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        if journal is not None:
            journal.close()
            saved = output.exists()
        if name is not None:
            if saved and not args.keep_db:
                try:
                    drop_isolated_database(args.admin_dsn, name)
                except Exception as exc:
                    print(f"Cleanup failed for {name}: {exc}", file=sys.stderr)
                    exit_code = 1
            else:
                print(f"Isolated database retained for diagnosis: {name}", file=sys.stderr)
        if saved:
            print(f"Private review report: {output.resolve()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
