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
from .scenario_validation import validate_scenario

REPORT_FORMAT = "mvp03c1.target-model-benchmark.v1"
PRIVATE_MEMBERS = ("manifest.json", "scenario.json", "transcript.json",
                   "evaluation.json", "errors.json")
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
    # Validates complete causal ordering and all 15 human review rubrics.
    # The review packet is built in memory and NEVER included in this report.
    packet=packet_from_bundle(evidence_zip)
    transcript=records["transcript.json"]
    observed=[event for event in transcript if event["action"]=="message"]
    expected_count=sum(step["action"]=="message"
                       for step in expected_scenario["steps"])
    if len(observed)!=expected_count or len(packet["cases"])!=sum(
        "review_case" in step for step in expected_scenario["steps"]
    ):
        raise ValueError("target message or review-case count mismatch")
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
