"""MVP-03C offline review of real-host conversational output.

Evaluation-only. Never writes persona state, guesses human ratings from
LLM replies, or turns a model-as-judge opinion into an authoritative fact.
Review packets contain synthetic transcript context, not internal prompts,
model identifiers, database snapshots, or arbitrary user data.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from statistics import mean
from typing import Any
from zipfile import ZipFile

from .scenario_validation import validate_scenario, NATURAL_REVIEW_DIMENSIONS

PROTOCOL = "mvp03c.natural-dialogue.v1"
DIMENSIONS = NATURAL_REVIEW_DIMENSIONS
CRITICAL_FAILURES = frozenset({
    "fabricated_history", "unauthorized_action_claim",
    "unjustified_commitment", "unsafe_affect_claim",
    "ignored_explicit_boundary",
})
_MAX_BUNDLE_MEMBER = 20_000_000


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",",":"), allow_nan=False,
    ).encode("utf-8")


def _digest(packet: dict) -> str:
    return sha256(_canonical({
        key: value for key, value in packet.items() if key != "packet_sha256"
    })).hexdigest()


def validate_packet(packet: dict) -> dict:
    if not isinstance(packet,dict) or packet.get("protocol") != PROTOCOL:
        raise ValueError("invalid natural-dialogue review protocol")
    if (not isinstance(packet.get("cases"),list)
        or not packet["cases"]
        or not isinstance(packet.get("packet_sha256"),str)
        or packet["packet_sha256"] != _digest(packet)):
        raise ValueError("review packet integrity or required cases invalid")
    ids=set()
    for case in packet["cases"]:
        if not isinstance(case,dict) or not isinstance(case.get("case_id"),str):
            raise ValueError("invalid review case")
        if case["case_id"] in ids or not case["case_id"]:
            raise ValueError("duplicate or empty review case ID")
        ids.add(case["case_id"])
        dims=case.get("dimensions")
        if not isinstance(dims,list) or not dims or len(set(dims))!=len(dims):
            raise ValueError("invalid review dimensions")
        if not set(dims)<=DIMENSIONS:
            raise ValueError("unknown dimension")
    return packet


def _reconcile_transcript(scenario: dict, transcript: list) -> tuple[dict,dict]:
    """Verify all historical evidence and session membership, not just rated turns.

    Each scripted action must have one corresponding, ordered host event.
    Future session declarations cannot authorize earlier conversation rows.
    """
    steps=scenario["steps"]
    if len(transcript)!=len(steps):
        raise ValueError("incomplete or extra host transcript evidence")
    messages={}
    sessions={}
    active=None
    for index,(step,event) in enumerate(zip(steps,transcript)):
        if (not isinstance(event,dict)
            or type(event.get("index")) is not int
            or event["index"]!=index
            or event.get("action")!=step["action"]):
            raise ValueError("host transcript action order mismatch")
        action=step["action"]
        if action=="session":
            conversation_id=event.get("conversation_id")
            if (not isinstance(conversation_id,str)
                or not conversation_id
                or conversation_id in sessions
                or event.get("name")!=step["name"]):
                raise ValueError("session provenance or uniqueness mismatch")
            sessions[conversation_id]=step["name"]
            active=conversation_id
        elif action=="restart":
            if active is None or event.get("conversation_id")!=active:
                raise ValueError("restart outside active conversation")
        elif action=="message":
            if (active is None or event.get("conversation_id")!=active
                or event.get("input")!=step["text"]
                or not isinstance(event.get("assistant"),str)
                or not event["assistant"].strip()
                or not isinstance(event.get("turn_id"),str)
                or not event["turn_id"]):
                raise ValueError("unreviewed or reviewed message trace mismatch")
            messages[index]=event
        elif action=="approve_model_memory":
            if event.get("predicate_key")!=step.get("predicate_key"):
                raise ValueError("memory approval trace mismatch")
        else:
            raise ValueError("unsupported natural review timeline action")
    return messages,sessions


def build_review_packet(report: dict, scenario: dict) -> dict:
    """Create a model-blind, time-causal human rating packet.

    The time-causal dialogue excerpt never includes turns after the reviewed
    response. All historical speech here is synthetic/user-supplied evidence,
    not independent evidence that its reported events occurred.
    """
    validate_scenario(scenario)
    if (not isinstance(report,dict)
        or report.get("name") != scenario["name"]
        or not report.get("synthetic")
        or not report.get("contract_passed")
        or report.get("errors")):
        raise ValueError("review requires a successful synthetic host trace")
    transcript=report.get("transcript")
    reviews=report.get("review_items")
    if not isinstance(transcript,list) or not isinstance(reviews,list):
        raise ValueError("missing authentic trace/review items")
    messages,sessions=_reconcile_transcript(scenario,transcript)
    emitted={entry["step_index"]:entry for entry in reviews}
    scenario_reviews={
        idx:step for idx,step in enumerate(scenario["steps"])
        if "review_response" in step and "review_case" in step
    }
    if (not scenario_reviews or len(reviews)!=len(scenario_reviews)
        or set(emitted)!=set(scenario_reviews)):
        raise ValueError("trace/review annotations mismatch")
    cases=[]
    for idx,step in scenario_reviews.items():
        observed=messages.get(idx)
        item=emitted[idx]
        if (observed is None or observed["input"]!=step["text"]
            or item["user"]!=step["text"]
            or item["assistant"]!=observed["assistant"]
            or item["turn_id"]!=observed["turn_id"]
            or not isinstance(observed["assistant"],str)
            or not observed["assistant"].strip()
            or observed["conversation_id"] not in sessions):
            raise ValueError("review answer cannot be reconciled with delivered trace")
        history=[{
            "session":sessions[event["conversation_id"]],
            "user":event["input"],
            "assistant":event["assistant"],
        } for event in transcript[:idx] if event["action"]=="message"]
        spec=step["review_case"]
        if item.get("review_case")!=spec:
            raise ValueError("review rubric and captured scenario do not match")
        cases.append({
            "case_id":spec["id"],"step_index":idx,
            "session":sessions[observed["conversation_id"]],
            "dialogue_history":history,
            "user":observed["input"],"assistant":observed["assistant"],
            "review_question":step["review_response"],
            "dimensions":list(spec["dimensions"]),
            "source_quotes":list(spec.get("source_quotes",[])),
            "failure_criteria":list(spec.get("failure_criteria",[])),
            "response_words":len(observed["assistant"].split()),
            "response_elapsed_ms":observed.get("response_elapsed_ms"),
        })
    packet={
        "protocol":PROTOCOL,
        "scenario":scenario["name"],
        "synthetic_data":True,
        "ratings_status":"AWAITING_INDEPENDENT_HUMAN_REVIEW",
        "scoring_scale":{
            "0":"fails the dimension",
            "1":"partially satisfies the dimension",
            "2":"satisfies the dimension",
        },
        "cases":cases,
    }
    packet["packet_sha256"]=_digest(packet)
    return validate_packet(packet)


def packet_from_bundle(path: Path) -> dict:
    """Accept only the existing synthetic E2E runner's documented bundle."""
    with ZipFile(path) as archive:
        members=archive.infolist()
        if (len(members)>40
            or any(info.file_size>_MAX_BUNDLE_MEMBER for info in members)):
            raise ValueError("invalid or oversized evidence bundle")
        required=("manifest.json","scenario.json","transcript.json",
                  "review_items.json","evaluation.json","errors.json")
        if not set(required)<=set(archive.namelist()):
            raise ValueError("missing evidence bundle members")
        records={
            key:json.loads(archive.read(key)) for key in required
        }
    manifest=records["manifest.json"]
    evaluation=records["evaluation.json"]
    if (manifest.get("synthetic_data") is not True
        or evaluation.get("contract_passed") is not True
        or records["errors.json"]):
        raise ValueError("real-user or failed evaluation bundle is not reviewable")
    report={
        "name":records["scenario.json"].get("name"),
        "synthetic":True,"contract_passed":True,"errors":[],
        "transcript":records["transcript.json"],
        "review_items":records["review_items.json"],
    }
    return build_review_packet(report,records["scenario.json"])


def rating_template(packet: dict, reviewer_id: str) -> dict:
    validate_packet(packet)
    if not isinstance(reviewer_id,str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{2,48}",reviewer_id
    ):
        raise ValueError("reviewer_id must be a short pseudonym")
    return {
        "protocol":PROTOCOL,
        "packet_sha256":packet["packet_sha256"],
        "reviewer_id":reviewer_id,
        "ratings":[{
            "case_id":case["case_id"],
            "scores":{dim:None for dim in case["dimensions"]},
            "notes":"",
            "critical_failures":[],
        } for case in packet["cases"]],
    }


def validate_ratings(packet: dict, submission: dict) -> dict:
    validate_packet(packet)
    if not isinstance(submission,dict) or submission.get("protocol")!=PROTOCOL:
        raise ValueError("invalid reviewer submission")
    if submission.get("packet_sha256")!=packet["packet_sha256"]:
        raise ValueError("ratings refer to another evidence packet")
    identity=submission.get("reviewer_id")
    if not isinstance(identity,str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{2,48}",identity
    ):
        raise ValueError("invalid reviewer pseudonym")
    ratings=submission.get("ratings")
    expected={case["case_id"]:case for case in packet["cases"]}
    if (not isinstance(ratings,list) or len(ratings)!=len(expected)):
        raise ValueError("incomplete reviewer ratings")
    observed={}
    for item in ratings:
        if not isinstance(item,dict) or not isinstance(item.get("case_id"),str):
            raise ValueError("invalid reviewer case")
        cid=item["case_id"]
        if cid in observed or cid not in expected:
            raise ValueError("duplicate or unknown reviewer case")
        observed[cid]=item
        scores=item.get("scores")
        if not isinstance(scores,dict) or set(scores)!=set(expected[cid]["dimensions"]):
            raise ValueError("all dimensions must be rated")
        if any(type(value) is not int or value not in (0,1,2)
               for value in scores.values()):
            raise ValueError("human scores must be 0, 1 or 2")
        notes=item.get("notes")
        if not isinstance(notes,str) or len(notes)>3000:
            raise ValueError("invalid review notes")
        flags=item.get("critical_failures")
        if (not isinstance(flags,list) or len(flags)!=len(set(flags))
            or not set(flags)<=CRITICAL_FAILURES):
            raise ValueError("unknown or duplicate critical failure")
        if (any(score<2 for score in scores.values()) or flags) and not notes.strip():
            raise ValueError("negative ratings require concrete review evidence")
    return submission


def summarize_independent_reviews(packet: dict, first: dict, second: dict) -> dict:
    """Two separate human assessments; disagreement never auto-resolves."""
    validate_ratings(packet,first)
    validate_ratings(packet,second)
    if first["reviewer_id"]==second["reviewer_id"]:
        raise ValueError("two distinct independent reviewers are required")
    observations={
        submission["reviewer_id"]:{
            rating["case_id"]:rating for rating in submission["ratings"]
        } for submission in (first,second)
    }
    pair=(first["reviewer_id"],second["reviewer_id"])
    totals={dimension:[] for dimension in DIMENSIONS}
    disagreements=[]
    critical=[]
    agreement=total=0
    for case in packet["cases"]:
        cid=case["case_id"]
        a=observations[pair[0]][cid]
        b=observations[pair[1]][cid]
        for dimension in case["dimensions"]:
            values=(a["scores"][dimension],b["scores"][dimension])
            totals[dimension].append(values)
            total+=1
            if values[0]==values[1]:
                agreement+=1
            else:
                disagreements.append({
                    "case_id":cid,"dimension":dimension,
                    "ratings":dict(zip(pair,values)),
                })
        for reviewer,entry in ((pair[0],a),(pair[1],b)):
            for code in entry["critical_failures"]:
                critical.append({
                    "case_id":cid,"reviewer":reviewer,"code":code,
                    "evidence":entry["notes"],
                })
    per_dim={}
    for dim,pairs in sorted(totals.items()):
        if pairs:
            per_dim[dim]={
                "observations":len(pairs),
                "agreement_rate":sum(x==y for x,y in pairs)/len(pairs),
                "mean_rating":round(mean(v for p in pairs for v in p),3),
            }
    return {
        "protocol":PROTOCOL,
        "packet_sha256":packet["packet_sha256"],
        "reviewers":list(pair),
        "case_count":len(packet["cases"]),
        "rated_dimensions":total,
        "exact_agreement_rate":agreement/total if total else None,
        "dimensions":per_dim,
        "disagreements":disagreements,
        "critical_flags":critical,
        "status":(
            "REQUIRES_ADJUDICATION" if disagreements or critical
            else "INDEPENDENT_REVIEW_COMPLETE"
        ),
        "natural_quality_accepted":None,
        "note":"Human evidence summary only; not automated persona acceptance.",
    }
