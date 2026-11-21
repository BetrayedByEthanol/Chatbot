"""MVP-03B.3: offline evidence scorer for real Context Builder snapshots.

Evaluation-only utility. Checks tagged source-derived prompt records rather
than text resemblance to the model reply (or the user's latest input). The
labels are the existing v2.5.1 Goal/ProcedureRun/Dialogue prompt contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from statistics import median
from typing import Iterable

LABELS={
    "Approved/delegated Goal state: ":"goal_id",
    "Tracked procedure (pinned): ":"run_id",
    "Pending prior dialogue act (not approval): ":"label",
}


@dataclass(frozen=True)
class ContextSelections:
    goals: frozenset[str] = frozenset()
    runs: frozenset[str] = frozenset()
    pending: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ContextCase:
    name: str
    expected: ContextSelections
    selected: ContextSelections
    elapsed_ms: float
    budget_chars: int
    total_chars: int
    latest_preserved: bool


def extract_context_selections(system_prompt: str) -> ContextSelections:
    """Parse only labelled structured context, not arbitrary prompt substrings.

    A prior conversation quotation mentioning "Approved/delegated Goal" or
    "Tracked procedure" cannot count unless it is itself an authored labelled
    context line with a valid JSON record and expected ID/key.
    """
    found={key:set() for key in ("goals","runs","pending")}
    for line in system_prompt.splitlines():
        for index,(prefix,field_name) in enumerate(LABELS.items()):
            if not line.startswith(prefix):
                continue
            record=json.loads(line[len(prefix):])
            if not isinstance(record,dict) or not isinstance(record.get(field_name),str):
                raise ValueError("malformed tagged context record")
            value=record[field_name]
            if not value:
                raise ValueError("empty tagged context identity")
            found[("goals","runs","pending")[index]].add(value)
            break
    return ContextSelections(**{key:frozenset(value) for key,value in found.items()})


def score_context_cases(cases: Iterable[ContextCase]) -> dict:
    """Micro-precision/recall plus meaningful negative-case abstention.

    A missing positive is an omission. An unexpected selected item is an
    injection. No-positive benchmarks cannot report perfect recall/precision
    by convention; those denominators remain null instead.
    """
    material=list(cases)
    if not material or len({case.name for case in material})!=len(material):
        raise ValueError("evaluation requires distinct named cases")
    tp=fp=fn=negatives=clean_negatives=positive_cases=clean_positive_cases=0
    lengths=[]
    case_details=[]
    for case in material:
        if case.budget_chars<=0 or case.elapsed_ms<0:
            raise ValueError("invalid observation budget or timing")
        expected=set()
        actual=set()
        for name in ("goals","runs","pending"):
            expected.update((name,id) for id in getattr(case.expected,name))
            actual.update((name,id) for id in getattr(case.selected,name))
        hits=expected&actual
        unexpected=actual-expected
        missing=expected-actual
        tp+=len(hits)
        fp+=len(unexpected)
        fn+=len(missing)
        if expected:
            positive_cases+=1
            clean_positive_cases+=not missing and not unexpected
        else:
            negatives+=1
            clean_negatives+=not actual
        lengths.append(case.elapsed_ms)
        case_details.append({
            "name":case.name,
            "matched":len(hits),
            "injected":len(unexpected),
            "omitted":len(missing),
            "within_budget":case.total_chars<=case.budget_chars,
            "latest_preserved":case.latest_preserved,
            "elapsed_ms":round(case.elapsed_ms,2),
        })
    rank=sorted(lengths)
    p95_index=max(0,(95*len(rank)+99)//100-1)
    return {
        "cases":len(material),
        "positive_cases":positive_cases,
        "negative_cases":negatives,
        "matched":tp,"injections":fp,"omissions":fn,
        "precision":tp/(tp+fp) if tp+fp else None,
        "recall":tp/(tp+fn) if tp+fn else None,
        "clean_negative_rate":clean_negatives/negatives if negatives else None,
        "clean_positive_rate":(
            clean_positive_cases/positive_cases if positive_cases else None
        ),
        "all_within_budget":all(x["within_budget"] for x in case_details),
        "all_latest_preserved":all(x["latest_preserved"] for x in case_details),
        "median_context_ms":round(median(lengths),2),
        "p95_context_ms":round(rank[p95_index],2),
        "case_details":case_details,
    }
