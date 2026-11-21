"""MVP-03C.1: descriptive target-model benchmark from a real E2E evidence ZIP.

Pure offline aggregation. Human conversation quality is *never* inferred from
word counts, model identity, contract success, or response timing. The output
contains no conversation, system prompt, or reviewer submissions.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from math import ceil, isfinite
from pathlib import Path
from statistics import median
from zipfile import ZipFile

from .natural_dialogue_review import packet_from_bundle
from .intent_interpretation import should_interpret_with_model
from .scenario_validation import validate_scenario

REPORT_FORMAT = "mvp03c1.target-model-benchmark.v1"
PRIVATE_MEMBERS = (
    "manifest.json", "scenario.json", "transcript.json",
    "evaluation.json", "errors.json", "model_requests.json",
    "delivery_events.json", "state_snapshots.json", "review_items.json",
)
MAX_MEMBER_BYTES = 20_000_000


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",",":"),
        allow_nan=False,
    ).encode("utf-8")


def _read_evidence(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 140_000_000:
        raise ValueError("missing or oversized local evidence bundle")
    with ZipFile(path) as archive:
        info=archive.infolist()
        names=[item.filename for item in info]
        if (len(info)>40 or len(set(names))!=len(names)
            or not set(PRIVATE_MEMBERS)<=set(names)
            or any(item.file_size>MAX_MEMBER_BYTES for item in info)):
            raise ValueError("unsafe or incomplete local evidence bundle")
        return {
            name:json.loads(archive.read(name)) for name in PRIVATE_MEMBERS
        }


def _quantiles(values: list[float]) -> dict:
    ordered=sorted(values)
    if not ordered:
        raise ValueError("no completed observations")
    return {
        "min":round(ordered[0],2),
        "median":round(median(ordered),2),
        "p95_nearest_rank":round(ordered[max(ceil(.95*len(ordered))-1,0)],2),
        "max":round(ordered[-1],2),
    }


def _reconcile_host_evidence(records: dict, observed: list, scenario: dict,
                             model: str) -> None:
    """Require actual outbound, delivery, persisted and contract observations.

    A model label on a transcript by itself is never evidence of execution.
    This validates consistency across the runner's independent observation
    boundaries; it is not a cryptographic attestation of runner origin.
    """
    requests=records["model_requests.json"]
    deliveries=records["delivery_events.json"]
    snapshots=records["state_snapshots.json"]
    checks=records["evaluation.json"].get("checks")
    if (not all(isinstance(x,list) for x in (
        requests,deliveries,snapshots,checks,
    ))
        or len(requests)!=len(observed)
        or len(deliveries)!=len(observed)
        or any(not isinstance(item,dict) for item in (
            *requests,*deliveries,*snapshots,*checks
        ))):
        raise ValueError("host evidence missing or incomplete")
    delivered=[s for s in snapshots if s.get("checkpoint")=="after_delivery"]
    if len(delivered)!=len(observed):
        raise ValueError("missing authoritative delivery snapshots")
    by_step={s.get("step_index"):s for s in delivered}
    if len(by_step)!=len(observed):
        raise ValueError("duplicate or absent delivery snapshots")
    for event,request,receipt in zip(observed,requests,deliveries):
        index=event["index"]
        # The real Conversation Manager selects one of two *primary*
        # inference routes per latest user message. Ordinary messages
        # use generate() with no JSON format; eligible implicit cues
        # use generate_interpreted() with a JSON envelope. Both must be
        # captured at the actual outbound HTTP boundary.
        mode=scenario["steps"][index].get("mode","auto")
        if mode!="model":
            raise ValueError("target scenario must use direct model turns")
        interpreted=should_interpret_with_model(event["input"])
        expected_route=("generate_interpreted" if interpreted else "generate")
        if (request.get("capture_boundary")!="ollama.outbound"
            or request.get("route")!=expected_route):
            raise ValueError("unverified target gateway request")
        payload=request.get("payload")
        if (not isinstance(payload,dict)
            or payload.get("model")!=model
            or (payload.get("format")=="json") is not interpreted
            or ("format" in payload and not interpreted)
            or payload.get("stream") is not False
            or payload.get("messages")!=request.get("messages")
            or not isinstance(request.get("messages"),list)
            or not request["messages"]
            or request["messages"][-1]!={
                "role":"user","content":event["input"],
            }):
            raise ValueError("outbound target-model request mismatch")
        if any(receipt.get(key)!=value for key,value in (
            ("type","reply"),("turn_id",event.get("turn_id")),
            ("response_id",event.get("response_id")),
            ("model_id",model),("text",event["assistant"]),
            ("replayed",False),
        )):
            raise ValueError("observed reply delivery does not match response")
        snapshot=by_step.get(index)
        if (snapshot is None
            or snapshot.get("source_turn_id")!=event.get("turn_id")):
            raise ValueError("missing matching authoritative snapshot")
        application=snapshot.get("application")
        database=snapshot.get("database")
        if not isinstance(application,dict) or not isinstance(database,dict):
            raise ValueError("snapshot lacks authoritative state evidence")
        trace=application.get("turn_trace")
        if not isinstance(trace,dict) or any(trace.get(key)!=value for key,value in (
            ("turn_id",event.get("turn_id")),
            ("conversation_id",event.get("conversation_id")),
            ("response_id",event.get("response_id")),
            ("model_id",model),
            ("response_status","DELIVERED"),
        )):
            raise ValueError("durable turn trace does not match delivered output")
        turns=database.get("conversation_turns")
        responses=database.get("conversation_responses")
        if (not isinstance(turns,dict) or not isinstance(responses,dict)
            or turns.get("truncated") is not False
            or responses.get("truncated") is not False
            or not isinstance(turns.get("rows"),list)
            or not isinstance(responses.get("rows"),list)):
            raise ValueError("incomplete database provenance snapshot")
        source_rows=[
            row for row in turns["rows"]
            if row.get("turn_id")==event.get("turn_id")
        ]
        response_rows=[
            row for row in responses["rows"]
            if row.get("response_id")==event.get("response_id")
        ]
        if (len(source_rows)!=1 or
            source_rows[0].get("input_text")!=event["input"]
            or source_rows[0].get("conversation_id")!=event["conversation_id"]
            or len(response_rows)!=1
            or response_rows[0].get("turn_id")!=event.get("turn_id")
            or response_rows[0].get("response_text")!=event["assistant"]
            or response_rows[0].get("model_id")!=model
            or response_rows[0].get("delivery_status")!="DELIVERED"):
            raise ValueError("database turns or responses fail source reconciliation")
        required={"response_delivered","no_unapproved_goal"}
        if scenario["steps"][index].get("expect_no_new_operations"):
            required.add("no_new_operations")
        for check_name in required:
            matches=[
                item for item in checks
                if item.get("step_index")==index
                and item.get("check")==check_name
                and item.get("pass") is True
            ]
            if len(matches)!=1:
                raise ValueError("missing required per-turn contract evidence")
    if any(item.get("pass") is not True for item in checks):
        raise ValueError("failed or invalid deterministic contract check")


def summarize_target_benchmark(
    evidence_zip: Path,expected_scenario: dict,*,model: str,
    model_digest: str|None=None,
) -> dict:
    """Reject non-target, incomplete or ungrounded runs before statistics."""
    validate_scenario(expected_scenario)
    if not isinstance(model,str) or not model.strip():
        raise ValueError("target model identifier is required")
    if model_digest is not None:
        if not isinstance(model_digest,str):
            raise ValueError("model digest must be a sha256 reference")
        # Ollama /api/tags returns 64 bare hexadecimal characters. Accept
        # the optional sha256: form and publish one normalized representation.
        raw_digest=model_digest.removeprefix("sha256:")
        if (len(raw_digest)!=64 or
            any(c not in "0123456789abcdef" for c in raw_digest)):
            raise ValueError("model digest must be a complete sha256 reference")
        model_digest="sha256:"+raw_digest
    records=_read_evidence(evidence_zip)
    manifest=records["manifest.json"]
    stored_scenario=records["scenario.json"]
    evaluation=records["evaluation.json"]
    errors=records["errors.json"]
    if (manifest.get("synthetic_data") is not True
        or stored_scenario!=expected_scenario):
        raise ValueError("synthetic scenario or evidence identity mismatch")
    if manifest.get("model")!=model:
        raise ValueError("the observed model differs from the declared target")
    if (manifest.get("contract_passed") is not True
        or evaluation.get("contract_passed") is not True
        or evaluation.get("behavioral_checks_passed") is not True
        or errors):
        raise ValueError("target run incomplete or deterministic contracts failed")
    if (manifest.get("behavioral_accepted") is not None
        or evaluation.get("behavioral_accepted") is not None):
        raise ValueError("human review must not be pre-approved by the runner")
    transcript=records["transcript.json"]
    observed=[event for event in transcript if event["action"]=="message"]
    expected_count=sum(step["action"]=="message"
                       for step in expected_scenario["steps"])
    if len(observed)!=expected_count:
        raise ValueError("target message count mismatch")
    response_ms=[]
    response_words=[]
    for event in observed:
        # Model-routed scenario: a deterministic fallback/tool response
        # is not a successful target-model sample.
        if (event.get("model_id")!=model
            or event.get("replayed") is not False):
            raise ValueError("non-target or replayed response found")
        value=event.get("response_elapsed_ms")
        if (type(value) not in (int,float)
            or not isfinite(value) or value<=0):
            raise ValueError("invalid host response timing")
        response_ms.append(float(value))
        response_words.append(len(event["assistant"].split()))
    _reconcile_host_evidence(records,observed,expected_scenario,model)
    # Validate the complete causal timeline and all 15 review rubrics after
    # rejecting invalid timing (including non-finite numbers).
    # Never include the generated reviewer packet in the public summary.
    packet=packet_from_bundle(evidence_zip)
    expected_reviews=sum("review_case" in step for step in expected_scenario["steps"])
    if len(packet["cases"])!=expected_reviews:
        raise ValueError("review case count mismatch")
    dimensions=Counter(dim for case in packet["cases"]
                       for dim in case["dimensions"])
    seconds=[v/1000 for v in response_ms]
    return {
        "format_version":REPORT_FORMAT,
        "evidence_status":"COMPLETE_SYNTHETIC_TARGET_RUN",
        "natural_quality_status":"PENDING_INDEPENDENT_HUMAN_RATINGS",
        "natural_quality_accepted":None,
        "rating_data_present":False,
        "target_model":model,
        "target_digest":model_digest,
        "model_digest_recorded":model_digest is not None,
        "git_sha":manifest.get("git_sha"),
        "scenario_name":stored_scenario["name"],
        "scenario_sha256":sha256(_canonical(stored_scenario)).hexdigest(),
        "evidence_zip_sha256":sha256(evidence_zip.read_bytes()).hexdigest(),
        "migration_sha256":manifest.get("migration_sha256"),
        "started_at":manifest.get("started_at"),
        "finished_at":manifest.get("finished_at"),
        "message_count":len(observed),
        "review_case_count":len(packet["cases"]),
        "review_dimensions":dict(sorted(dimensions.items())),
        "host_response_seconds":_quantiles(seconds),
        "response_word_counts":_quantiles(response_words),
        "responses_over_80_words":sum(v>80 for v in response_words),
        "contract_passed":True,
        "behavioral_checks_passed":True,
        "limitations":[
            "Host wall time includes response handling, not streaming first-token latency.",
            "Response length and latency are descriptive, not conversational quality ratings.",
            "No model-generated or simulated human scores were collected.",
            "Independent human ratings and adjudication remain mandatory.",
            "The evidence ZIP and model-blind reviewer packet remain private.",
        ],
    }
