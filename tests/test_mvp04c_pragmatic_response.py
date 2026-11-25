"""Focused, deterministic regressions for current-turn pragmatic safeguards."""
import pytest

from persona_agent.pragmatic_response import (
    pragmatic_cue, pragmatic_context, enforce_pragmatic_reply,
)
from persona_agent.intent_interpretation import parse_explicit_topic_switch


@pytest.mark.parametrize("message,reply,reason",[
    ("Even deciding what to do feels like too much work today.",
     "Take your time. Would you like music or a book?", "current_boundary:low_decision"),
    ("Choosing anything feels overwhelming right now.",
     "Pick one of these activities.", "current_boundary:low_decision"),
    ("I don't want to make any decisions right now.",
     "1. Read.\n2. Cook.", "current_boundary:low_decision"),
    ("Please don't ask me any questions right now.",
     "I understand. How are you feeling?", "current_boundary:no_questions"),
    ("Please don't make a big plan for me; I just want to chat.",
     "Let's make a plan for the evening.", "current_boundary:chat_only"),
    ("I'm not agreeing to save that as a goal yet.",
     "I'll save that as a goal.", "unconfirmed_commitment:no_goal"),
    ("I might work later. I haven't decided.",
     "We've agreed to work tonight.", "unconfirmed_commitment:undecided"),
    ("What time is it right now? Please check instead of guessing.",
     "It's currently 3:45 PM in my time zone. What's yours?", "unverified_clock_claim"),
    ("What's the current time in Tokyo?",
     "I checked the clock.", "unverified_clock_claim"),
])
def test_current_turn_safeguard_changes_only_conflicting_model_reply(message,reply,reason):
    result=enforce_pragmatic_reply(message,reply,model_generated=True)
    assert result.reason==reason
    assert result.text!=reply
    assert "?" not in result.text
    assert enforce_pragmatic_reply(message,reply,model_generated=False).text==reply


@pytest.mark.parametrize("message,reply",[
    ("Even deciding what to do feels like too much work today.",
     "We can just take it easy."),
    ("I just want to chat.","What have you been watching lately?"),
    ("Please give me two options for dinner.","1. Soup.\n2. Salad."),
    ("What if I said I don't want to make decisions?",
     "Would that mean you prefer fewer options?"),
    ('"I just want to chat."',"Let's make a plan for discussing this quote."),
    ("I haven't decided yet. What do you recommend?", "Would you prefer tea or coffee?"),
    ("I might work later. Please check the clock. I haven't decided.",
     "Which timezone do you mean?"),
    ("I want to walk regularly.","Which day would suit you?"),
    ("What time is it right now?","I don't have a current clock reading."),
    ("What is 12:30 in 24-hour notation?","12:30."),
])
def test_compliant_or_ambiguous_replies_are_preserved(message,reply):
    result=enforce_pragmatic_reply(message,reply,model_generated=True)
    assert result.reason is None and result.text==reply


def test_compound_input_does_not_turn_into_an_undecided_cue():
    assert pragmatic_cue("I might work later. Please check the clock. I haven't decided.") is None


def test_context_is_current_turn_only_and_budgeted():
    text="I don't want to make any decisions right now."
    assert pragmatic_cue(text)=="LOW_DECISION"
    context=pragmatic_context(text,max_chars=360)
    assert "without another choice" in context
    assert "not a diagnosis" in context
    assert pragmatic_context(text,max_chars=30)==""
    assert pragmatic_context("Could you give me options?",max_chars=360)==""


@pytest.mark.parametrize("text,topic",[
    ("Don't troubleshoot yet. Let's talk about nebulae instead.","nebulae"),
    ("Please do not debug right now; let us discuss music instead.","music"),
    ("Don't plan for now. Let's talk about gardening.","gardening"),
    ("What if I said don't troubleshoot yet. Let's talk about nebulae instead.",None),
    ('"Don't troubleshoot yet. Let's talk about nebulae instead."',None),
    ("Don't troubleshoot yet.",None),
    ("Don't troubleshoot yet. Let's talk about that instead.",None),
])
def test_pause_and_named_switch_is_not_an_execution_instruction(text,topic):
    assert parse_explicit_topic_switch(text)==topic
