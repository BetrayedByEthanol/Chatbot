"""Pure E2E scenario validation shared by preflight, runner and tests.

No PostgreSQL or model runtime dependencies: invalid input is rejected
before container creation, and validation remains testable without Psycopg.
"""
from __future__ import annotations

from typing import Any
import re

from typing import get_args
from .semantic_contracts import (
    StatementType, TemporalScope, Modality, PragmaticFunction,
)

NATURAL_REVIEW_DIMENSIONS = frozenset({
    "grounding", "continuity", "calibration", "brevity",
    "affect", "goal_support", "agency", "coherence", "repetition",
})


_SEMANTIC_LABELS = {
    "statement_type": set(get_args(StatementType)),
    "temporal_scope": set(get_args(TemporalScope)),
    "modality": set(get_args(Modality)),
    "pragmatic_function": set(get_args(PragmaticFunction)),
}


def _validate_expected_semantics(expected, text: str) -> None:
    if not isinstance(expected,list) or len(expected)>4:
        raise ValueError("expect_semantics must be a list of at most four claims")
    for claim in expected:
        if not isinstance(claim,dict) or not claim:
            raise ValueError("expected semantic claims must be nonempty objects")
        if set(claim)-set(_SEMANTIC_LABELS)-{"topic_label","evidence_quote"}:
            raise ValueError("unknown semantic expectation field")
        for key,allowed in _SEMANTIC_LABELS.items():
            if key in claim and (
                not isinstance(claim[key],str) or claim[key] not in allowed
            ):
                raise ValueError("invalid semantic label: "+key)
        if "topic_label" in claim and (
            not isinstance(claim["topic_label"],str)
            or not 2<=len(claim["topic_label"])<=80
        ):
            raise ValueError("invalid semantic topic label")
        if "evidence_quote" in claim and (
            not isinstance(claim["evidence_quote"],str)
            or not claim["evidence_quote"] in text
        ):
            raise ValueError("expected evidence quote is not in source input")



def validate_scenario(value: Any) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ValueError("scenario requires a name")
    steps = value.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("scenario requires 1..100 steps")
    seen_sessions = set()
    active = False
    message_count = 0
    source_user_texts = []
    review_case_ids = set()
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
        elif action == "approve_model_memory":
            # An explicitly scripted authenticated user action in a
            # disposable E2E fixture, never an automatic model decision.
            predicate = step.get("predicate_key")
            if (not active or not isinstance(predicate, str)
                    or not re.fullmatch(r"enjoys_[a-z_]{3,45}", predicate)):
                raise ValueError("model-memory approval requires active session and predicate")
        elif action == "message":
            text = step.get("text")
            if (not active or not isinstance(text, str)
                    or not 1 <= len(text) <= 12000):
                raise ValueError("message requires an active session and text")
            if step.get("mode", "auto") not in ("auto", "model", "time", "analyze"):
                raise ValueError("invalid message mode")
            if "expect_semantics" in step:
                _validate_expected_semantics(step["expect_semantics"],text)
            if "expect_model_memory_proposed" in step and type(
                step["expect_model_memory_proposed"]
            ) is not bool:
                raise ValueError("model-memory proposal expectation must be boolean")
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
            case = step.get("review_case")
            if case is not None:
                if ("review_response" not in step
                    or step.get("mode","auto") not in ("model", "auto")
                    or not isinstance(case,dict)
                    or set(case)!={"id","dimensions","source_quotes","failure_criteria"}):
                    raise ValueError("natural review case needs a model response and exact rubric")
                cid=case["id"]
                if (not isinstance(cid,str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}",cid)
                    or cid in review_case_ids):
                    raise ValueError("invalid or duplicate natural review case ID")
                review_case_ids.add(cid)
                dims=case["dimensions"]
                if (not isinstance(dims,list) or not 1<=len(dims)<=6
                    or any(not isinstance(d,str) for d in dims)
                    or len(set(dims))!=len(dims)
                    or not set(dims)<=NATURAL_REVIEW_DIMENSIONS):
                    raise ValueError("invalid natural review dimensions")
                quotes=case["source_quotes"]
                if (not isinstance(quotes,list) or len(quotes)>6
                    or any(not isinstance(q,str) or not 4<=len(q)<=200
                           or not any(q in source for source in (
                               *source_user_texts,text
                           )) for q in quotes)):
                    raise ValueError("natural review source quote not supported by earlier user evidence")
                failures=case["failure_criteria"]
                if (not isinstance(failures,list) or not 1<=len(failures)<=6
                    or any(not isinstance(x,str) or not 8<=len(x)<=300
                           for x in failures)):
                    raise ValueError("natural review requires bounded failure criteria")
            source_user_texts.append(text)
            message_count += 1
        else:
            raise ValueError("unsupported scenario action")
    if not message_count:
        raise ValueError("scenario must contain at least one message")
    return value

