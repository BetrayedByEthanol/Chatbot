"""MVP-02C.11: pure, read-only memory-conflict eligibility contract tests.

Structured metadata is hand-supplied; these tests DO NOT measure the ability
to extract metadata or achieve real-world memory conflict detection.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from persona_agent.memory_conflict_eligibility import (
    ClaimScope, EligibilityDecision, NewClaim, PriorClaimSnapshot,
    evaluate_conflict_eligibility,
)

USER = UUID(int=1)
OTHER_USER = UUID(int=2)
PERSONA = UUID(int=3)
OTHER_PERSONA = UUID(int=4)
TURN_A = UUID(int=5)
TURN_B = UUID(int=6)
MEMORY = UUID(int=7)
V2 = Path(__file__).resolve().parents[1] / "scenarios" / "mvp02c10_memory_conflict_diagnostic_v2.json"


def _scope(*, subject="user", predicate="enjoys_gardening",
           context="general", temporal="CURRENT",
           start=None, end=None):
    return ClaimScope(
        subject_key=subject, predicate_key=predicate, context_key=context,
        temporal_scope=temporal, interval_start=start, interval_end=end,
    )


def _prior(*, scope=None, admission="ADMITTED_ASSERTED",
           user_id=USER, instance_id=PERSONA, kind="PREFERENCE"):
    admitted = admission == "ADMITTED_ASSERTED"
    return PriorClaimSnapshot(
        user_id=user_id, instance_id=instance_id, source_turn_id=TURN_A,
        memory_kind=kind, scope=scope or _scope(), admission=admission,
        memory_id=MEMORY if admitted else None,
        memory_version=3 if admitted else None,
    )


def _candidate(*, scope=None, user_id=USER, instance_id=PERSONA,
               kind="PREFERENCE", statement="PREFERENCE",
               modality="ASSERTED", pragmatic="INFORMING",
               value="I do not enjoy gardening."):
    return NewClaim(
        user_id=user_id, instance_id=instance_id, source_turn_id=TURN_B,
        memory_kind=kind, scope=scope or _scope(),
        statement_type=statement, modality=modality,
        pragmatic_function=pragmatic, value_text=value,
    )


def test_asserted_same_scope_is_nli_candidate_even_if_values_agree():
    old = _prior()
    for value in ("I do not enjoy gardening.", "I really enjoy gardening."):
        decision = evaluate_conflict_eligibility(old, _candidate(value=value))
        assert (decision.disposition, decision.reason, decision.scope_compatible) == (
            "NLI_CANDIDATE", "ASSERTED_COMPARABLE", True,
        )
        assert decision.nli_allowed is True
        assert "memory_id" not in decision.model_dump()
        assert "approved" not in decision.model_dump()


def test_explicit_correction_has_no_nli_threshold_requirement():
    decision = evaluate_conflict_eligibility(
        _prior(), _candidate(pragmatic="CORRECTING", value="Correction: I do not enjoy gardening.")
    )
    assert (decision.disposition, decision.reason, decision.nli_allowed) == (
        "CORRECTION_PATH", "EXPLICIT_CORRECTION", False
    )


@pytest.mark.parametrize("admission,expected", [
    ("TENTATIVE_UNADMITTED", ("TENTATIVE_RESOLUTION", "TENTATIVE_SOURCE")),
    ("OTHER_UNADMITTED", ("SKIP", "SOURCE_NOT_ADMITTED")),
])
def test_unadmitted_old_sources_never_start_memory_conflict(admission, expected):
    got = evaluate_conflict_eligibility(
        _prior(admission=admission), _candidate(pragmatic="CORRECTING")
    )
    assert (got.disposition, got.reason) == expected
    assert not got.nli_allowed


@pytest.mark.parametrize("modality,pragmatic,statement,expect", [
    ("TENTATIVE", "INFORMING", "PREFERENCE", "TENTATIVE_NEW_CLAIM"),
    ("CONDITIONAL", "INFORMING", "PREFERENCE", "NONASSERTED_NEW_CLAIM"),
    ("HYPOTHETICAL", "INFORMING", "PREFERENCE", "NONASSERTED_NEW_CLAIM"),
    ("ASSERTED", "ASKING", "PREFERENCE", "NONASSERTED_NEW_CLAIM"),
    ("ASSERTED", "REQUESTING", "PREFERENCE", "NONASSERTED_NEW_CLAIM"),
    ("ASSERTED", "INFORMING", "INTENTION", "INCOMPARABLE_STATEMENT"),
    ("ASSERTED", "CORRECTING", "UNKNOWN", "INCOMPARABLE_STATEMENT"),
])
def test_nonasserted_or_unrelated_pragmatic_claims_do_not_enter_nli(
    modality, pragmatic, statement, expect,
):
    decision = evaluate_conflict_eligibility(
        _prior(), _candidate(modality=modality, pragmatic=pragmatic,
                             statement=statement)
    )
    assert (decision.disposition, decision.reason, decision.nli_allowed) == (
        "SKIP", expect, False
    )


@pytest.mark.parametrize("modality,pragmatic", [
    ("UNKNOWN", "INFORMING"), ("ASSERTED", "UNKNOWN"),
])
def test_unknown_interpretation_abstains(modality, pragmatic):
    decision = evaluate_conflict_eligibility(
        _prior(), _candidate(modality=modality, pragmatic=pragmatic)
    )
    assert (decision.disposition, decision.reason, decision.scope_compatible) == (
        "ABSTAIN", "UNKNOWN_INTERPRETATION", True
    )
    assert not decision.nli_allowed


@pytest.mark.parametrize("updates,reason", [
    ({"subject":"brother"}, "DIFFERENT_SUBJECT"),
    ({"predicate":"enjoys_running"}, "DIFFERENT_PREDICATE"),
    ({"context":"office"}, "DIFFERENT_CONTEXT"),
])
def test_structurally_distinct_claims_are_excluded_before_nli(updates, reason):
    decision = evaluate_conflict_eligibility(
        _prior(), _candidate(scope=_scope(**updates))
    )
    assert (decision.disposition, decision.reason, decision.scope_compatible) == (
        "SKIP", reason, False
    )


def test_cross_user_and_cross_persona_both_fail_closed():
    for user_id, persona in [(OTHER_USER, PERSONA), (USER, OTHER_PERSONA)]:
        decision = evaluate_conflict_eligibility(
            _prior(), _candidate(user_id=user_id, instance_id=persona)
        )
        assert decision.reason == "CROSS_INSTANCE"
        assert not decision.nli_allowed


def _instant(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("old_temporal,new_temporal,expected", [
    ("PAST_HABITUAL", "CURRENT", ("SKIP", "DISJOINT_TIME")),
    ("PAST_EVENT", "CURRENT", ("SKIP", "DISJOINT_TIME")),
    ("FUTURE", "PAST_EVENT", ("SKIP", "DISJOINT_TIME")),
    ("CURRENT", "FUTURE", ("SKIP", "DISJOINT_TIME")),
    ("PAST_EVENT", "PAST_EVENT", ("ABSTAIN", "UNCERTAIN_TIME")),
    ("FUTURE", "FUTURE", ("ABSTAIN", "UNCERTAIN_TIME")),
    ("UNSPECIFIED", "CURRENT", ("ABSTAIN", "UNCERTAIN_TIME")),
    ("CURRENT", "HABITUAL", ("NLI_CANDIDATE", "ASSERTED_COMPARABLE")),
])
def test_coarse_temporal_scopes_are_conservative(old_temporal, new_temporal, expected):
    decision = evaluate_conflict_eligibility(
        _prior(scope=_scope(temporal=old_temporal)),
        _candidate(scope=_scope(temporal=new_temporal)),
    )
    assert (decision.disposition, decision.reason) == expected


def test_bounded_intervals_reject_disjoint_and_allow_overlap():
    old = _scope(
        temporal="PAST_HABITUAL",
        start=_instant("2024-01-01"), end=_instant("2024-12-31"),
    )
    disjoint = _scope(
        temporal="PAST_HABITUAL",
        start=_instant("2026-01-01"), end=_instant("2026-12-31"),
    )
    overlap = _scope(
        temporal="PAST_HABITUAL",
        start=_instant("2024-06-01"), end=_instant("2024-10-01"),
    )
    assert evaluate_conflict_eligibility(_prior(scope=old), _candidate(scope=disjoint)).reason == "DISJOINT_TIME"
    assert evaluate_conflict_eligibility(_prior(scope=old), _candidate(scope=overlap)).nli_allowed


def test_single_unpaired_temporal_window_abstains():
    old = _scope(
        temporal="CURRENT", start=_instant("2024-01-01"),
        end=_instant("2024-12-31"),
    )
    result = evaluate_conflict_eligibility(_prior(scope=old), _candidate())
    assert (result.disposition, result.reason) == ("ABSTAIN", "UNCERTAIN_TIME")


@pytest.mark.parametrize("kw,part", [
    ({"interval_start": _instant("2026-01-01")}, "both endpoints"),
    ({"interval_end": _instant("2026-01-01")}, "both endpoints"),
    ({"interval_start": _instant("2026-07-01"),
      "interval_end": _instant("2026-06-01")}, "ends before"),
    ({"interval_start": datetime(2026, 1, 1),
      "interval_end": datetime(2026, 2, 1)}, "timezone"),
    ({"context_key": "HOME"}, "string_pattern_mismatch"),
    ({"context_key": ""}, "string_too_short"),
])
def test_invalid_claim_scopes_rejected(kw, part):
    with pytest.raises(ValidationError):
        ClaimScope.model_validate({
            "subject_key": "user", "predicate_key": "enjoys_gardening",
            "context_key": "general", "temporal_scope": "CURRENT", **kw,
        })


def test_unadmitted_memory_identity_fails_closed_even_if_partial():
    with pytest.raises(ValidationError, match="unadmitted source"):
        PriorClaimSnapshot(
            user_id=USER, instance_id=PERSONA, source_turn_id=TURN_A,
            memory_kind="PREFERENCE", scope=_scope(),
            admission="TENTATIVE_UNADMITTED", memory_id=MEMORY,
        )
    with pytest.raises(ValidationError, match="admitted Memory snapshot"):
        PriorClaimSnapshot(
            user_id=USER, instance_id=PERSONA, source_turn_id=TURN_A,
            memory_kind="PREFERENCE", scope=_scope(),
            admission="ADMITTED_ASSERTED", memory_id=MEMORY,
        )


def test_invalid_types_and_forged_output_cannot_be_accepted():
    with pytest.raises(TypeError, match="validated"):
        evaluate_conflict_eligibility({}, _candidate())
    with pytest.raises(ValidationError, match="only comparable asserted"):
        EligibilityDecision(
            disposition="SKIP", reason="SOURCE_NOT_ADMITTED",
            scope_compatible=False, nli_allowed=True,
        )
    with pytest.raises(ValidationError, match="compatible scope"):
        EligibilityDecision(
            disposition="CORRECTION_PATH", reason="EXPLICIT_CORRECTION",
            scope_compatible=False, nli_allowed=False,
        )


# Selected v2 cases are mapped to manually supplied *structured* metadata,
# not parsed from free text or copied from eligibility oracle flags. These
# tests prove behavior for known inputs, NOT real-world classifier accuracy.
# Each tuple: ID, prior admission, old subject, old predicate, old context,
# old temporal, new subject, new predicate, new context, new temporal,
# candidate modality, pragmatic act, result.
V2_CASES = [
    ("MC001","ADMITTED_ASSERTED","user","residence","general","CURRENT",
     "user","residence","general","CURRENT","ASSERTED","INFORMING","NLI_CANDIDATE"),
    ("MC013","ADMITTED_ASSERTED","user","enjoys_reading","general","CURRENT",
     "user","enjoys_reading","general","CURRENT","ASSERTED","INFORMING","NLI_CANDIDATE"),
    ("MC024","ADMITTED_ASSERTED","user","running","general","PAST_EVENT",
     "user","reading","general","PAST_EVENT","ASSERTED","INFORMING","SKIP"),
    ("MC029","ADMITTED_ASSERTED","user","residence","general","PAST_HABITUAL",
     "user","residence","general","CURRENT","ASSERTED","INFORMING","SKIP"),
    ("MC039","OTHER_UNADMITTED","user","join_running_club","general","FUTURE",
     "user","runs_daily","general","HABITUAL","ASSERTED","INFORMING","SKIP"),
    ("MC041","OTHER_UNADMITTED","user","italian_course","general","FUTURE",
     "user","italian_fluency","general","CURRENT","ASSERTED","INFORMING","SKIP"),
    ("MC048","OTHER_UNADMITTED","user","guitar_lessons","general","FUTURE",
     "user","plays_guitar","general","HABITUAL","ASSERTED","INFORMING","SKIP"),
    ("MC049","OTHER_UNADMITTED","user","owns_dog","general","CURRENT",
     "user","owns_dog","general","CURRENT","ASSERTED","INFORMING","SKIP"),
    ("MC056","OTHER_UNADMITTED","user","likes_mushrooms","general","CURRENT",
     "user","likes_mushrooms","general","CURRENT","ASSERTED","INFORMING","SKIP"),
    ("MC065","ADMITTED_ASSERTED","user","pet_name","pet_dog","CURRENT",
     "user","pet_name","pet_cat","CURRENT","ASSERTED","INFORMING","SKIP"),
    ("MC069","ADMITTED_ASSERTED","user","residence","weekdays","CURRENT",
     "user","residence","weekends","CURRENT","ASSERTED","INFORMING","SKIP"),
    ("MC071","TENTATIVE_UNADMITTED","user","enjoys_gardening","general","CURRENT",
     "user","enjoys_gardening","general","CURRENT","ASSERTED","INFORMING","TENTATIVE_RESOLUTION"),
    ("MC072","TENTATIVE_UNADMITTED","user","appointment_day","general","CURRENT",
     "user","appointment_day","general","CURRENT","ASSERTED","INFORMING","TENTATIVE_RESOLUTION"),
    ("MC074","TENTATIVE_UNADMITTED","user","owns_bicycle","general","CURRENT",
     "user","owns_bicycle","general","CURRENT","ASSERTED","INFORMING","TENTATIVE_RESOLUTION"),
    ("MC077","TENTATIVE_UNADMITTED","user","swimming","general","FUTURE",
     "user","office_attendance","general","FUTURE","ASSERTED","INFORMING","SKIP"),
    ("MC079","ADMITTED_ASSERTED","user","residence","general","CURRENT",
     "user","residence","general","CURRENT","ASSERTED","CORRECTING","CORRECTION_PATH"),
    ("MC080","ADMITTED_ASSERTED","user","owns_car","general","CURRENT",
     "user","owns_car","general","CURRENT","ASSERTED","CORRECTING","CORRECTION_PATH"),
    ("MC089","ADMITTED_ASSERTED","user","owns_bicycle","general","CURRENT",
     "user","owns_bicycle","general","CURRENT","ASSERTED","INFORMING","NLI_CANDIDATE"),
    ("MC092","ADMITTED_ASSERTED","user","residence","general","PAST_HABITUAL",
     "user","residence","general","CURRENT","ASSERTED","INFORMING","SKIP"),
    ("MC093","OTHER_UNADMITTED","user","running","general","FUTURE",
     "user","running","general","PAST_EVENT","ASSERTED","INFORMING","SKIP"),
    ("MC094","ADMITTED_ASSERTED","brother","residence","general","CURRENT",
     "user","residence","general","CURRENT","ASSERTED","INFORMING","SKIP"),
]


@pytest.mark.parametrize("row", V2_CASES, ids=[row[0] for row in V2_CASES])
def test_selected_v2_pairs_with_hand_supplied_metadata(row):
    (case_id, status, old_sub, old_pred, old_context, old_time,
     new_sub, new_pred, new_context, new_time, modality, pragmatic, disposition) = row
    data = json.loads(V2.read_text(encoding="utf-8"))
    case = next(c for c in data["examples"] if c["id"] == case_id)
    old = _prior(admission=status, kind="FACT", scope=_scope(
        subject=old_sub, predicate=old_pred, context=old_context, temporal=old_time,
    ))
    new = _candidate(kind="FACT", scope=_scope(
        subject=new_sub, predicate=new_pred, context=new_context, temporal=new_time,
    ), statement="FACT", modality=modality, pragmatic=pragmatic,
        value=case["candidate_text"])
    got = evaluate_conflict_eligibility(old, new)
    assert got.disposition == disposition
    assert got.nli_allowed is (disposition == "NLI_CANDIDATE")
    if case_id in ("MC071", "MC074", "MC072"):
        assert case["expected_resolution"] == "TENTATIVE_CLAIM_RESOLVED"
        assert got.nli_allowed is False
