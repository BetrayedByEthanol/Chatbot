"""Focused contextual compatibility and correction-authority tests."""
import pytest

from persona_agent.contextual_interpretation import (
    short_reply_kind, short_reply_answers_question, pending_reply_context,
    correction_authority_context, arithmetic_evidence,
)
from persona_agent.intent_interpretation import classify_intent


@pytest.mark.parametrize("reply,question,expected",[
    ("Yes", "Did you finish the exercise?", True),
    ("No", "Did you finish the exercise?", True),
    ("Yep.", "Is the file open?", True),
    ("yes", "Which day would work?", False),
    ("No", "What ingredients do you have?", False),
    ("Yes", "Do you mean UTC+10:00 or UTC+11:00?", False),
    ("Yes", "Don't you want to continue?", False),
    ("Yes", "Did you finish and upload the file?", False),
    ("Maybe", "Did you finish the exercise?", False),
    ("That one", "Did you finish the exercise?", False),
    ("Okay", "Did you finish the exercise?", False),
])
def test_short_reply_requires_compatible_question(reply,question,expected):
    assert short_reply_answers_question(reply,question) is expected


def test_pending_context_distinguishes_adjacent_from_stale_or_competing():
    pending={"kind":"QUESTION","label":"Which day would work?",
             "source_sequence":4,"delivery_status":"DELIVERED"}
    assert "ask one short clarification" in pending_reply_context(
        "Yes",[pending],turn_sequence=5,max_chars=420,
    )
    assert "older or competing" in pending_reply_context(
        "Yes",[pending],turn_sequence=6,max_chars=420,
    )
    assert "older or competing" in pending_reply_context(
        "Yes",[pending,pending],turn_sequence=5,max_chars=420,
    )
    assert "older or competing" in pending_reply_context(
        "Yes",[dict(pending,delivery_status="GENERATED")],turn_sequence=5,max_chars=420,
    )
    assert "uncertainty" in pending_reply_context(
        "Maybe",[pending],turn_sequence=5,max_chars=420,
    )
    assert pending_reply_context("Tuesday",[pending],turn_sequence=5,max_chars=420)==""
    assert pending_reply_context("Yes",[],turn_sequence=5,max_chars=420)==""
    assert pending_reply_context("Yes",[pending],turn_sequence=5,max_chars=20)==""


@pytest.mark.parametrize("text,kind,clue",[
    ("Change the recorded roll to 18.","INSTRUCTION","not authority"),
    ("Actually, the recorded roll was 8.","CORRECTION","claim to assess"),
    ("It's raining outside.","OBSERVATION","time, location"),
    ("I can see rain falling outside.","OBSERVATION","time, location"),
    ("What if I asked you to change the roll?","HYPOTHETICAL","not an actual"),
    ("Actually, my favorite drink is coffee.","CORRECTION","claim to assess"),
])
def test_correction_context_distinguishes_source_role_and_authority(text,kind,clue):
    assert classify_intent(text).kind==kind
    block=correction_authority_context(text,max_chars=920)
    assert clue in block
    assert "Credibility and mutation authority are separate" in block
    assert "owning domain" in block
    assert correction_authority_context(text,max_chars=40)==""


@pytest.mark.parametrize("text,fragment",[
    ("Actually, 8 + 3 = 11.","8 + 3 = 11; the reported result 11 matches"),
    ("Actually, 8 + 3 is 12.","8 + 3 = 11; the reported result 12 does not match"),
    ("Correction: -4 * 3 = -12.","-4 * 3 = -12; the reported result -12 matches"),
    ("9 - 4 equals 6.","9 - 4 = 5; the reported result 6 does not match"),
])
def test_explicit_arithmetic_is_checked_without_claiming_owner_authority(text,fragment):
    block=correction_authority_context(text,max_chars=920)
    assert fragment in block
    assert "operand provenance, rule applicability or permission" in block


@pytest.mark.parametrize("text",[
    "What if 8 + 3 = 12?",
    '"8 + 3 = 11"',
    "8 + 3 = 11. Please change the roll.",
    "8 / 0 = 2",
    "9999999 + 1 = 10000000",
])
def test_unsupported_arithmetic_abstains(text):
    assert arithmetic_evidence(text)==""


def test_no_new_authority_context_on_ordinary_questions_or_greetings():
    assert correction_authority_context("Hello.",max_chars=920)==""
    assert correction_authority_context("What is the weather?",max_chars=920)==""
    assert correction_authority_context("I enjoyed a walk.",max_chars=920)==""
