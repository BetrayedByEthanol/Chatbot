#!/usr/bin/env python3
"""MVP-00A real-model observational baseline (never injects domain outcomes).

Requires disposable migrated PostgreSQL and a running Ollama model. Responses
are recorded for human review; only deterministic persistence facts are
automatically graded. This is NOT a behavioral quality pass.
"""
import argparse
import json
import os
from pathlib import Path

import psycopg

from persona_agent.acceptance_host import seed_local_demo
from persona_agent.behavioral_acceptance import BehavioralAcceptanceHarness
from persona_agent.model_gateway import OllamaChatGateway


SCENARIO = (
    ("goal_discovery", (
        "I want to eat healthier. Could we track this as a long-term goal?",
        "Yes. Please make eating healthier an ongoing goal for me.",
        "Could we start by planning two balanced lunches this week?",
    )),
    ("preferences", (
        "My favorite drink is tea.",
        "I prefer tea over coffee.",
        "Do you remember what my favorite drink is?",
    )),
    ("progress", (
        "I prepared a balanced lunch yesterday.",
        "I managed to repeat it today.",
    )),
    ("obstacles", (
        "I struggle to cook when work gets busy.",
        "I would like to find an easier approach.",
    )),
    ("adaptation", (
        "What could I change about my lunch strategy?",
        "What did we learn from my previous attempts?",
    )),
    ("continuity", (
        "We have not talked for a while. What should we focus on next?",
    )),
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("ACCEPTANCE_DATABASE_URL"))
    parser.add_argument("--model", default=os.getenv("ACCEPTANCE_MODEL", "qwen2.5:0.5b"))
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("ACCEPTANCE_OLLAMA_URL", "http://127.0.0.1:11434"),
    )
    parser.add_argument(
        "--cognition", action="store_true",
        help="Also run existing cognitive scheduling and one model operation per session",
    )
    parser.add_argument("--report", type=Path, help="Write evidence and full synthetic transcript")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn or ACCEPTANCE_DATABASE_URL is required")

    identity = seed_local_demo(args.dsn)
    gateway = OllamaChatGateway(
        model=args.model, base_url=args.ollama_url, timeout_seconds=180,
    )
    harness = BehavioralAcceptanceHarness(args.dsn, identity, gateway)
    harness.enable_explicit_memory_worker()
    errors = []
    if args.cognition:
        from persona_agent.automatic_cognition import (
            CognitivePolicy, register_cognitive_consumer,
            run_cognitive_scheduling_tick,
        )
        from persona_agent.cognitive_contracts import COGNITIVE_RESOURCE
        from persona_agent.cognitive_engine import run_one_cognitive_operation
        from persona_agent.operation_scheduler import configure_resource_pool

        with psycopg.connect(args.dsn, autocommit=True) as conn:
            configure_resource_pool(
                conn, COGNITIVE_RESOURCE, capacity_units=3,
                p0_reserved_units=1,
            )
            register_cognitive_consumer(conn)

    transcript = []
    worker_ticks = []
    for index, (name, messages) in enumerate(SCENARIO):
        harness.begin_session(name)
        if name == "continuity":
            harness.restart_host()
        for n, message in enumerate(messages):
            reply = harness.send(
                message, idempotency_key=f"mvp00a-{index:02d}-{n:02d}",
            )
            transcript.append({
                "session": name, "user": message, "persona": reply.text,
                "turn_id": reply.turn_id, "response_id": reply.response_id,
            })
        worker_ticks.append({
            "session": name,
            "explicit_memory": harness.tick_explicit_memory_worker(),
        })
        if args.cognition:
            try:
                with psycopg.connect(args.dsn, autocommit=True) as conn:
                    scheduled = run_cognitive_scheduling_tick(
                        conn, worker_id="mvp00a-cognitive-scheduler",
                        policy=CognitivePolicy(
                            min_closed_turns=2, retain_recent=1, max_turns=8,
                        ),
                        reconcile_user_id=identity.user_id,
                    )
                    completed = run_one_cognitive_operation(
                        conn, gateway, worker_id="mvp00a-cognitive-inference",
                    )
                worker_ticks[-1]["cognitive_admitted"] = scheduled.admitted
                worker_ticks[-1]["cognitive_completed"] = completed is not None
            except Exception as exc:
                # Preserve failed model/workflow evidence; do not claim success.
                errors.append({"session": name, "error": type(exc).__name__})

    state = harness.observe_state()
    expected_turns = sum(len(messages) for _, messages in SCENARIO)
    contract_results = {
        "persisted_text": (
            "WORKING" if state["delivered_responses"] == expected_turns
            and state["turns"] == expected_turns else "MISSING"
        ),
        "multiple_sessions": (
            "WORKING" if state["conversations"] == len(SCENARIO) else "MISSING"
        ),
        "explicit_memory_extraction": (
            "WORKING" if state["memories"] >= 1 else "MISSING"
        ),
        "natural_goal_approval": (
            "WORKING" if state["goals"] >= 1 else "MISSING"
        ),
        "cognitive_reflection": (
            ("PARTIAL" if state["reflections"] else "MISSING")
            if args.cognition else "NOT_EVALUATED"
        ),
        "intention_utilization": "NOT_EVALUATED",
        "memory_retrieval_quality": "REQUIRES_HUMAN_REVIEW",
        "intent_interpretation_quality": "REQUIRES_HUMAN_REVIEW",
        "emotional_continuity_quality": "REQUIRES_HUMAN_REVIEW",
        "conversation_coherence": "REQUIRES_HUMAN_REVIEW",
        "session_recovery_behavior": "REQUIRES_HUMAN_REVIEW",
    }
    report = {
        "checkpoint": "MVP-00A",
        "baseline_only": True,
        "model": args.model,
        "test_user": str(identity.user_id),
        "test_instance": str(identity.instance_id),
        "state_counts": state,
        "contracts": contract_results,
        "worker_ticks": worker_ticks,
        "errors": errors,
        "transcript": transcript,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        key: value for key, value in report.items()
        if key != "transcript"
    }, indent=2))
    # Baseline behavioral gaps do NOT fail CI; runtime errors do.
    return 1 if errors or contract_results["persisted_text"] == "MISSING" else 0


if __name__ == "__main__":
    raise SystemExit(main())
