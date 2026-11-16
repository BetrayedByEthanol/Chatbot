"""MVP-02C.3: semantic diagnostics reuse the actual disposable E2E host."""
import json
from uuid import uuid4, UUID
from zipfile import ZipFile

import pytest

from persona_agent.behavioral_evaluation import run_scenario, write_bundle
from persona_agent.conversation_contracts import ModelReply
from persona_agent.semantic_contracts import SemanticAnnotationBatch, SemanticClaim


class FixedModel:
    def __init__(self):
        self.calls=0
    def generate(self,messages):
        self.calls+=1
        return ModelReply(model_id="semantic.e2e.fake",text="Thanks for sharing.")


def scenario(expected=None):
    return {
        "name":"shadow-labels",
        "steps":[
            {"action":"session","name":"first"},
            {"action":"message","mode":"model","text":"I enjoy gardening.",
             "expect_semantics":[{
                 "statement_type":"PREFERENCE",
                 "temporal_scope":"CURRENT",
                 "modality":"ASSERTED",
                 "pragmatic_function":"INFORMING",
                 "topic_label":"gardening",
             }] if expected is None else expected},
            {"action":"message","mode":"model","text":"Hello.",
             "expect_semantics":[]},
        ],
    }


def test_shadow_labels_export_provenance_without_mutating_memory_or_goals(dsn,tmp_path):
    model=FixedModel()
    src=scenario()
    report=run_scenario(
        dsn,src,model,run_semantic_diagnostics=True,
        run_memory_worker=False,run_tool_worker=False,
    )
    assert not report["errors"],report["errors"]
    assert report["passed"],report["checks"]
    assert model.calls==2
    assert report["semantic_diagnostics_enabled"]
    diagnostics=report["semantic_diagnostics"]
    assert len(diagnostics)==2
    assert [row["status"] for row in diagnostics]==["ANNOTATED","ABSTAINED"]
    assert diagnostics[0]["batch"]["classifier_version"]=="mvp02c3.rules.1"
    assert diagnostics[0]["source_turn_id"]==report["transcript"][1]["turn_id"]
    assert diagnostics[0]["batch"]["claims"][0]["evidence_quote"]=="I enjoy gardening."
    assert all(row["stage"]=="SHADOW_AFTER_DELIVERY" for row in diagnostics)
    assert all(row["latency_ms"]>=0 for row in diagnostics)
    assert all(
        row["application"]["counts"]["goals"]==0
        and row["application"]["counts"]["memories"]==0
        for row in report["snapshots"]
    )
    assert [row["check"] for row in report["checks"]].count(
        "semantic_candidate_contract",
    )==2
    assert [row["check"] for row in report["checks"]].count("semantic_labels")==2

    out=write_bundle(
        report,src,tmp_path/"semantic-evaluation.zip",
        model="semantic.e2e.fake",git_sha="local",
        migration_hashes={"0001.sql":"dummy"},
    )
    with ZipFile(out) as archive:
        saved=json.loads(archive.read("semantic_diagnostics.json"))
        manifest=json.loads(archive.read("manifest.json"))
        assert saved==diagnostics
        assert manifest["semantic_diagnostics_enabled"]
        assert manifest["behavioral_checks_passed"]


def test_semantic_mismatch_is_visible_as_behavioral_failure_not_contract_failure(dsn):
    report=run_scenario(
        dsn,scenario(expected=[{"temporal_scope":"FUTURE"}]),FixedModel(),
        run_semantic_diagnostics=True,run_memory_worker=False,
        run_tool_worker=False,
    )
    assert report["contract_passed"]
    assert not report["behavioral_checks_passed"]
    assert not report["passed"]
    assert any(c["check"]=="semantic_labels" and not c["pass"]
               for c in report["checks"])


def test_no_silent_semantic_gold_labels_when_diagnostics_disabled(dsn):
    with pytest.raises(ValueError,match="require --semantic-diagnostics"):
        run_scenario(dsn,scenario(),FixedModel(),
                     run_semantic_diagnostics=False)


def test_injected_classifier_invalid_evidence_fails_without_affecting_foreground(dsn):
    class InvalidClassifier:
        def classify(self,text,*,source_turn_id):
            return SemanticAnnotationBatch(
                source_turn_id=source_turn_id,
                classifier_id="fake.huggingface",
                classifier_version="untrusted-test",
                route="HUGGING_FACE",
                claims=(SemanticClaim(
                    evidence_quote="I am definitely a billionaire.",
                    statement_type="FACT",temporal_scope="CURRENT",
                    modality="ASSERTED",pragmatic_function="INFORMING",
                    confidence=0.99,
                ),),
            )
    model=FixedModel()
    report=run_scenario(
        dsn,scenario(),model,run_semantic_diagnostics=True,
        semantic_classifier=InvalidClassifier(),
        run_memory_worker=False,run_tool_worker=False,
    )
    assert model.calls==2
    assert report["errors"]==[]
    assert report["semantic_diagnostics"][0]["status"]=="ERROR"
    assert report["semantic_diagnostics"][0]["error_type"]=="ValueError"
    assert not report["contract_passed"]
    assert all(
        turn["action"]=="message" and turn["assistant"]=="Thanks for sharing."
        for turn in report["transcript"][1:]
    )
