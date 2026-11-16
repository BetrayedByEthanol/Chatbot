"""Pure E2E scenario validation shared by preflight, runner and tests.

No PostgreSQL or model runtime dependencies: invalid input is rejected
before container creation, and validation remains testable without Psycopg.
"""
from __future__ import annotations

from typing import Any


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
            if "expect_related_intent" in step and step[
                "expect_related_intent"
            ] not in ("OBJECTIVE", "REQUEST", "CORRECTION"):
                raise ValueError("invalid related intent expectation")
            if "expect_focus_contains" in step and (
                not isinstance(step["expect_focus_contains"], str)
                or not 1 <= len(step["expect_focus_contains"]) <= 80
            ):
                raise ValueError("invalid focus expectation")
            if "expect_no_new_operations" in step and type(
                step["expect_no_new_operations"]
            ) is not bool:
                raise ValueError("expect_no_new_operations must be boolean")
            if "review_response" in step and (
                not isinstance(step["review_response"], str)
                or not 1 <= len(step["review_response"]) <= 300
            ):
                raise ValueError("review_response must be a short instruction")
            message_count += 1
        else:
            raise ValueError("unsupported scenario action")
    if not message_count:
        raise ValueError("scenario must contain at least one message")
    return value

