"""MVP-02C.3: Stage A semantic annotations are advisory and source-grounded."""
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from persona_agent.semantic_contracts import (
    SemanticAnnotationBatch, SemanticClaim, validate_semantic_evidence,
)
from persona_agent.semantic_classification import (
    RuleSemanticClassifier, diagnostic_for_turn,
)
from persona_agent.scenario_validation import validate_scenario


def _claim(**changes):
    data={
        "evidence_quote":"I enjoy gardening.",
        "statement_type":"PREFERENCE",
        "temporal_scope":"CURRENT",
        "modality":"ASSERTED",
        "pragmatic_function":"INFORMING",
        "topic_label":"gardening",
        "confidence":0.9,
    }
    data.update(changes)
    return SemanticClaim(**data)


def _batch(**changes):
    data={
        "source_turn_id":uuid4(),
        "classifier_id":"hf.test.intent",
        "classifier_version":"sha256:abc123",
        "route":"HUGGING_FACE",
        "claims":(_claim(),),
    }
    data.update(changes)
    return SemanticAnnotationBatch(**data)


def test_semantic_contract_validates_source_and_multiple_claims():
    turn_id=uuid4()
    batch=_batch(
        source_turn_id=turn_id,
        claims=(
            _claim(),
            _claim(
                evidence_quote="I used to enjoy walking.",
                temporal_scope="PAST_HABITUAL",topic_label="walking",
            ),
        ),
    )
    assert validate_semantic_evidence(
        batch,source_turn_id=turn_id,
        source_text="I enjoy gardening. I used to enjoy walking.",
    ) is batch
    assert len(batch.claims)==2
    with pytest.raises(ValueError,match="source turn"):
        validate_semantic_evidence(batch,source_turn_id=uuid4(),
                                   source_text="I enjoy gardening.")
    with pytest.raises(ValueError,match="not verbatim"):
        validate_semantic_evidence(batch,source_turn_id=turn_id,
                                   source_text="I enjoy gardening.")
    with pytest.raises(TypeError,match="validated annotation"):
        validate_semantic_evidence({},source_turn_id=turn_id,
                                   source_text="I enjoy gardening.")


@pytest.mark.parametrize("field,value",[
    ("statement_type","EMOTIONAL_FACT"),
    ("temporal_scope","PERMANENT"),
    ("modality","DEFINITE_TRUTH"),
    ("pragmatic_function","APPROVE_MEMORY"),
    ("confidence",1.3),
    ("topic_label","x"*81),
])
def test_semantic_claim_rejects_invalid_metadata(field,value):
    with pytest.raises(ValidationError):
        _claim(**{field:value})


def test_specialist_classifier_can_abstain_on_unhandled_dimensions():
    # An independent temporal-only model should not invent a statement type,
    # dialogue intent, or modality to satisfy this shared output contract.
    turn=uuid4()
    batch=_batch(
        source_turn_id=turn,
        classifier_id="hf.temporal-only",
        claims=(_claim(
            statement_type="UNKNOWN",
            temporal_scope="PAST_EVENT",
            modality="UNKNOWN",
            pragmatic_function="UNKNOWN",
            topic_label=None,
        ),),
    )
    assert validate_semantic_evidence(
        batch,source_turn_id=turn,source_text="I enjoy gardening.",
    ).claims[0].statement_type=="UNKNOWN"


def test_semantic_annotation_abstention_and_metadata_constraints():
    with pytest.raises(ValidationError):
        _batch(claims=(),abstention_reason=None)
    with pytest.raises(ValidationError):
        _batch(abstention_reason="AMBIGUOUS")
    with pytest.raises(ValidationError):
        _batch(claims=(_claim(),)*5)
    with pytest.raises(ValidationError):
        _batch(route="UNTRUSTED_COMMAND")
    with pytest.raises(ValidationError):
        _batch(extra_permissions=True)
    abstained=_batch(claims=(),abstention_reason="AMBIGUOUS")
    assert abstained.claims==()
    assert abstained.classifier_version=="sha256:abc123"


@pytest.mark.parametrize("statement,expected",[
    ("I enjoy gardening.",("PREFERENCE","CURRENT","ASSERTED","INFORMING","gardening")),
    ("I enjoyed gardening yesterday.",("EXPERIENCE","PAST_EVENT","ASSERTED","INFORMING","gardening")),
    ("I used to enjoy gardening.",("PREFERENCE","PAST_HABITUAL","ASSERTED","INFORMING","gardening")),
    ("I don't enjoy gardening anymore.",("PREFERENCE","CURRENT","ASSERTED","CORRECTING","gardening")),
    ("Maybe I should go walking tomorrow.",("INTENTION","FUTURE","TENTATIVE","INFORMING","walking")),
    ("I've gotten into gardening lately.",("PREFERENCE","CURRENT","ASSERTED","INFORMING","gardening")),
    ("Hello.",None),
    ("I used to enjoy gardening, but now I find it frustrating.",None),
    ("My friend enjoys gardening.",None),
])
def test_reference_classifier_never_guesses_unsupported_statements(statement,expected):
    turn=uuid4()
    result=RuleSemanticClassifier().classify(statement,source_turn_id=turn)
    validate_semantic_evidence(result,source_turn_id=turn,source_text=statement)
    assert result.route=="DETERMINISTIC"
    if expected is None:
        assert not result.claims and result.abstention_reason is not None
    else:
        assert len(result.claims)==1
        claim=result.claims[0]
        assert (
            claim.statement_type,claim.temporal_scope,claim.modality,
            claim.pragmatic_function,claim.topic_label,
        )==expected
        assert claim.evidence_quote==statement


def test_hugging_face_style_adapter_is_pluggable_and_diagnostic_is_not_authority():
    class FakeHF:
        def classify(self,text,*,source_turn_id):
            return _batch(source_turn_id=source_turn_id,claims=(
                _claim(evidence_quote=text),
            ))
    turn=uuid4()
    record=diagnostic_for_turn(
        FakeHF(),text="I enjoy gardening.",source_turn_id=turn,
    )
    assert record["status"]=="ANNOTATED"
    assert record["batch"]["classifier_id"]=="hf.test.intent"
    assert record["batch"]["classifier_version"]=="sha256:abc123"
    assert record["batch"]["route"]=="HUGGING_FACE"
    assert record["latency_ms"]>=0
    assert record["stage"]=="SHADOW_AFTER_DELIVERY"


def test_invalid_classifier_evidence_and_exceptions_are_visible():
    class InventedQuote:
        def classify(self,text,*,source_turn_id):
            return _batch(source_turn_id=source_turn_id,claims=(
                _claim(evidence_quote="I have completed ten workouts."),
            ))
    class Crashed:
        def classify(self,text,*,source_turn_id):
            raise TimeoutError("model timed out")
    turn=uuid4()
    for model,kind in (
        (InventedQuote(),"ValueError"),(Crashed(),"TimeoutError"),
    ):
        record=diagnostic_for_turn(
            model,text="I enjoy gardening.",source_turn_id=turn,
        )
        assert record["status"]=="ERROR"
        assert record["error_type"]==kind


def test_semantic_scenario_preflight_and_gold_labels():
    path=Path(__file__).resolve().parents[1]/"scenarios"/"mvp02c3_semantic_diagnostics.json"
    scenario=validate_scenario(json.loads(path.read_text(encoding="utf-8")))
    labels=[s["expect_semantics"] for s in scenario["steps"]
            if "expect_semantics" in s]
    assert len(labels)==8
    assert sum(not x for x in labels)==2
    bad=json.loads(path.read_text(encoding="utf-8"))
    bad["steps"][2]["expect_semantics"]=[{"temporal_scope":"PAST_FOREVER"}]
    with pytest.raises(ValueError,match="invalid semantic label"):
        validate_scenario(bad)


@pytest.mark.parametrize("expected",[
    None, "PREFERENCE", [1],
    [{"pragmatic_function":"AUTHORIZE_GOAL"}],
    [{"not_a_dimension":"FACT"}],
    [{"evidence_quote":"invented source"}],
    [{}],
    [{"topic_label":"g"}],
    [{"statement_type":"FACT"}]*5,
])
def test_semantic_preflight_rejects_unsafe_expectations(expected):
    scenario={"name":"bad","steps":[
        {"action":"session","name":"first"},
        {"action":"message","text":"I enjoy gardening.","expect_semantics":expected},
    ]}
    with pytest.raises(ValueError):
        validate_scenario(scenario)
