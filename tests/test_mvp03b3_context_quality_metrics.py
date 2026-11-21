"""Test that longitudinal metrics cannot conceal omissions or injections."""
import pytest
from context_quality_metrics import (
    ContextCase,ContextSelections,extract_context_selections,
    score_context_cases,
)


def make(name,expected,selected):
    return ContextCase(
        name,expected,selected,0.5,1200,800,True,
    )


def test_typed_label_parser_ignores_unlabelled_quoted_words():
    prompt=(
        "A user said: Tracked procedure (pinned): not a record\n"
        'Approved/delegated Goal state: {"goal_id":"g1"}\n'
        'Tracked procedure (pinned): {"run_id":"r1"}\n'
        'Pending prior dialogue act (not approval): {"label":"Which host?"}\n'
        "I once heard 'Approved/delegated Goal state' in conversation.\n"
    )
    assert extract_context_selections(prompt)==ContextSelections(
        goals=frozenset({"g1"}),runs=frozenset({"r1"}),
        pending=frozenset({"Which host?"}),
    )


@pytest.mark.parametrize("bad",[
    'Tracked procedure (pinned): {"run_id":5}',
    'Approved/delegated Goal state: []',
    'Pending prior dialogue act (not approval): {"label":""}',
])
def test_invalid_tagged_records_are_not_silently_passed(bad):
    with pytest.raises(ValueError):
        extract_context_selections(bad)


def test_micro_metrics_separate_missing_and_unrelated_items():
    samples=[
        make(
            "mixed",ContextSelections(
                goals=frozenset({"g1"}),runs=frozenset({"r1"}),
            ),ContextSelections(
                goals=frozenset({"g1"}),runs=frozenset({"r2"}),
            ),
        ),
        make("abstain",ContextSelections(),ContextSelections()),
    ]
    report=score_context_cases(samples)
    assert (report["matched"],report["injections"],report["omissions"])==(1,1,1)
    assert report["precision"]==report["recall"]==0.5
    assert report["clean_negative_rate"]==1.0
    assert report["clean_positive_rate"]==0.0
    assert report["all_within_budget"]
    assert report["all_latest_preserved"]


def test_absence_only_is_not_reported_as_perfect_retrieval():
    result=score_context_cases([
        make("negative",ContextSelections(),ContextSelections())
    ])
    assert result["precision"] is None
    assert result["recall"] is None
    assert result["clean_negative_rate"]==1.0
    with pytest.raises(ValueError):
        score_context_cases([])
    with pytest.raises(ValueError):
        score_context_cases([
            make("same",ContextSelections(),ContextSelections()),
            make("same",ContextSelections(),ContextSelections()),
        ])
