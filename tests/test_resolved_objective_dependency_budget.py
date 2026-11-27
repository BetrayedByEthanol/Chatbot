"""Budget admission is atomic for selected corrections in a resolved reference."""
from uuid import uuid4

import pytest

from persona_agent.dialogue_lineage import DialogueDependencies
from persona_agent.intent_interpretation import resolved_objective_context_block
from persona_agent.raw_context import MAX_RAW_SOURCES


class ProjectionCursor:
    """Supply the two already scoped, eligibility-projected SQL result sets."""

    def __init__(self,resolved,rows):
        self.results=iter(([resolved],rows))

    def execute(self,query,params):
        self.result=next(self.results)

    def fetchall(self):
        return self.result


def fixture_rows():
    original,amended,optional=uuid4(),uuid4(),uuid4()
    resolved={
        "signal_id":uuid4(),"thread_id":uuid4(),"title":"walking daily",
        "resolution_status":"OPEN","source_input":"I want to practice walking daily.",
        "next_conversational_action":None,"source_ids":[original],
        "broad_dependency":False,"objective_sequence":1,"current_sequence":5,
    }
    correction={
        "intent_kind":"CORRECTION","label":"Actually, once per week would be better.",
        "turn_sequence":2,"source_ids":[original,amended],"broad_dependency":False,
    }
    observation={
        "intent_kind":"OBSERVATION","label":"I prepared my walking shoes.",
        "turn_sequence":3,"source_ids":[optional],"broad_dependency":False,
    }
    return resolved,correction,observation


def render(resolved,rows,dependencies=None,max_chars=1100):
    return resolved_objective_context_block(
        ProjectionCursor(resolved,rows),user_id=uuid4(),instance_id=uuid4(),
        conversation_id=uuid4(),turn_id=uuid4(),latest_text="What about that walking goal?",
        max_chars=max_chars,dependencies=dependencies,
    )


@pytest.mark.parametrize("failure",["raw_budget","broad_fence","ineligible","text_budget"])
def test_failed_required_correction_omits_whole_block_without_pinning(failure):
    resolved,correction,observation=fixture_rows()
    original=resolved["source_ids"][0]
    reserved={original}
    if failure=="raw_budget":
        reserved.update(uuid4() for _ in range(MAX_RAW_SOURCES-1))
    elif failure=="broad_fence":
        correction["broad_dependency"]=True
    elif failure=="ineligible":
        correction["source_ids"]=None
    dependencies=DialogueDependencies(reserved,None)
    # This exact text budget admits the original alone, but not its correction.
    limit=len(render(resolved,[])) if failure=="text_budget" else 1100
    assert render(resolved,[],max_chars=limit)
    assert render(resolved,[observation,correction],dependencies,limit)==""
    assert dependencies.source_ids==set()
    assert not dependencies.uses_broad


def test_required_correction_wins_remaining_source_budget_over_newer_optional_note():
    resolved,correction,observation=fixture_rows()
    reserved={resolved["source_ids"][0]}
    reserved.update(uuid4() for _ in range(MAX_RAW_SOURCES-2))
    dependencies=DialogueDependencies(reserved,None)
    block=render(resolved,[observation,correction],dependencies)
    assert correction["label"] in block
    assert observation["label"] not in block
    assert dependencies.source_ids==set(correction["source_ids"])


def test_required_correction_wins_remaining_text_budget_over_newer_optional_note():
    resolved,correction,observation=fixture_rows()
    limit=len(render(resolved,[correction]))
    dependencies=DialogueDependencies(set(),None)
    block=render(resolved,[observation,correction],dependencies,limit)
    assert correction["label"] in block
    assert observation["label"] not in block
    assert dependencies.source_ids==set(correction["source_ids"])


def test_available_budget_preserves_all_eligible_details_in_chronological_order():
    resolved,correction,observation=fixture_rows()
    dependencies=DialogueDependencies(set(),None)
    block=render(resolved,[observation,correction],dependencies)
    assert block.index(correction["label"]) < block.index(observation["label"])
    assert dependencies.source_ids==set(correction["source_ids"]+observation["source_ids"])
