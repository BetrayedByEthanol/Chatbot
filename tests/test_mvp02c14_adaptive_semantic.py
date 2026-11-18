"""MVP-02C.14 offline adaptive semantic interpretation tests.

Scripted model results test evaluation infrastructure only, not learned
accuracy or independent semantic annotation. No Ollama server is required.
"""
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from persona_agent.semantic_adaptive_evaluation import (
    ModelScopeCandidate, OllamaScopeGateway, interpret_adaptive,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CORPUS = ROOT / "scenarios" / "mvp02c13_semantic_scope_corpus.json"


def _runner():
    # This script intentionally reuses the v2.13 offline evaluation module.
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "mvp02c14_script", SCRIPTS / "evaluate_adaptive_semantic_scope.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline(text, key="SC999"):
    from persona_agent.semantic_classification import RuleSemanticClassifier
    from persona_agent.semantic_contracts import validate_semantic_evidence
    from uuid import UUID
    # Load the real previous baseline to ensure routing parity.
    sys.path.insert(0, str(SCRIPTS))
    from evaluate_semantic_scope import extract_scope
    return extract_scope(text, key)


def _proposal(text, *, candidate=True, evidence=None, scope=True,
              subject="user", predicate="enjoys_gardening", context="general",
              value="yes", temporal="CURRENT", modality="ASSERTED",
              pragmatic="INFORMING", statement="PREFERENCE", more=None):
    if not candidate:
        return json.dumps({"candidate": None})
    body = {
        "evidence_quote": text if evidence is None else evidence,
        "statement_type": statement, "temporal_scope": temporal,
        "modality": modality, "pragmatic_function": pragmatic,
        "subject_key": subject, "predicate_key": predicate,
        "context_key": context, "normalized_value": value,
        "narrow_current_scope": scope, "confidence": 0.83,
    }
    if more is not None:
        body.update(more)
    return json.dumps({"candidate": body})


class ScriptedGateway:
    model_id = "test-stub-not-a-real-model"

    def __init__(self, by_text):
        self.by_text = by_text
        self.calls = []

    def propose(self, text):
        self.calls.append(text)
        answer = self.by_text[text]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _check(text, model_result):
    from uuid import UUID
    return interpret_adaptive(
        text, source_turn_id=UUID(int=1), baseline=_baseline(text),
        gateway=ScriptedGateway({text: model_result}),
    )


def test_stage_a_supported_scope_never_calls_model():
    text = "I enjoy gardening."
    gateway = ScriptedGateway({})
    from uuid import UUID
    result = interpret_adaptive(
        text, source_turn_id=UUID(int=1), baseline=_baseline(text),
        gateway=gateway,
    )
    assert result["route"] == "DETERMINISTIC"
    assert result["status"] == "NARROW_SCOPE"
    assert result["narrow_scope"]["predicate_key"] == "enjoys_gardening"
    assert gateway.calls == []


def test_off_topic_message_bypasses_model():
    text = "Hello!"
    gateway = ScriptedGateway({})
    from uuid import UUID
    result = interpret_adaptive(
        text, source_turn_id=UUID(int=1),
        baseline=_baseline(text), gateway=gateway,
    )
    assert result["route"] == "DETERMINISTIC"
    assert result["reason"] == "NO_ACTIVITY_CUE"
    assert not result["model_attempted"]


def test_offline_model_can_recognize_paraphrase_and_definite_correction():
    positive = "I absolutely love gardening."
    negative = "I do not enjoy gardening."
    first = _check(positive, _proposal(positive))
    second = _check(negative, _proposal(
        negative, value="no", pragmatic="CORRECTING",
    ))
    assert first["status"] == second["status"] == "NARROW_SCOPE"
    assert first["route"] == second["route"] == "MODEL"
    assert first["narrow_scope"]["normalized_value"] == "yes"
    assert second["narrow_scope"]["normalized_value"] == "no"
    assert "model_latency_ms" in second


@pytest.mark.parametrize("text", [
    "I enjoy gardening only during summer.",
    "I enjoy gardening on weekends.",
    "I enjoy gardening at the office.",
    "I might enjoy gardening.",
    "Maybe I enjoy gardening.",
    "If I had time, I would enjoy gardening.",
    "My brother enjoys gardening.",
    "My friend said, \"I enjoy gardening.\"",
    "I enjoy gardening and reading.",
    "I enjoy gardening. I don't enjoy reading.",
])
def test_source_qualifiers_veto_even_a_false_positive_model_candidate(text):
    out = _check(text, _proposal(text))
    assert out["status"] == "SCOPE_VETOED"
    assert out["narrow_scope"] is None
    assert out["raw_model_scope"] is True
    assert out["model_attempted"]


def test_model_not_authorized_to_rewrite_quote_or_invent_extra_fields():
    text = "I absolutely love gardening."
    shorter = _check(text, _proposal(text, evidence="love gardening"))
    assert shorter["status"] == "MODEL_REJECTED"
    assert shorter["narrow_scope"] is None
    extra = _check(text, _proposal(text, more={"memory_id": "magic"}))
    assert extra["status"] == "MODEL_REJECTED"
    assert extra["narrow_scope"] is None
    assert "magic" not in str(extra)


@pytest.mark.parametrize("raw", [
    "{invalid", "[]", json.dumps({"candidate": 7}), "x" * 4001,
    _proposal("I absolutely love gardening.", modality="TENTATIVE"),
    _proposal("I absolutely love gardening.", subject="brother"),
    _proposal("I absolutely love gardening.", context="summer"),
    _proposal("I absolutely love gardening.", predicate="owns_house"),
])
def test_invalid_or_self_contradictory_model_output_is_rejected(raw):
    out = _check("I absolutely love gardening.", raw)
    assert out["status"] == "MODEL_REJECTED"
    assert out["narrow_scope"] is None


def test_model_semantics_without_narrow_scope_are_advisory():
    text = "I might enjoy gardening."
    out = _check(text, _proposal(
        text, scope=False, modality="TENTATIVE",
    ))
    assert out["status"] == "MODEL_SEMANTIC_ONLY"
    assert out["labels"]["modality"] == "TENTATIVE"
    assert out["narrow_scope"] is None
    empty = _check(text, _proposal(text, candidate=False))
    assert empty["status"] == "MODEL_ABSTAINED"
    assert empty["labels"] is None


def test_provider_failure_is_explicit_and_never_treated_as_success():
    text = "I absolutely love gardening."
    out = _check(text, RuntimeError("fake outage"))
    assert out["status"] == "MODEL_ERROR"
    assert out["reason"] == "RuntimeError"
    assert out["narrow_scope"] is None


def test_real_ollama_adapter_uses_one_json_call_without_server(monkeypatch):
    from persona_agent.model_gateway import OllamaChatGateway
    captured = []
    def fake_chat(self, messages, *, json_mode=False):
        captured.append((messages, json_mode, self.model))
        return '{"candidate":null}'
    monkeypatch.setattr(OllamaChatGateway, "_chat", fake_chat)
    output = OllamaScopeGateway(model_id="local-model").propose("I enjoy gardening.")
    assert json.loads(output)["candidate"] is None
    assert len(captured) == 1 and captured[0][1] is True
    messages = captured[0][0]
    assert messages[-1].content == "I enjoy gardening."
    assert "ENTIRE" in messages[0].content
    assert captured[0][2] == "local-model"


def test_model_call_budget_and_offline_metrics_do_not_leak_source_text():
    runner = _runner()
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    selected = [item for item in data["examples"] if item["id"] in
                ("SC001", "SC015", "SC019", "SC025", "SC078")]
    scripted = ScriptedGateway({
        "I absolutely love gardening.": _proposal("I absolutely love gardening."),
        "I do not enjoy gardening.": _proposal(
            "I do not enjoy gardening.", value="no", pragmatic="CORRECTING"
        ),
        "I enjoy gardening only during summer.": _proposal(
            "I enjoy gardening only during summer."
        ),
    })
    outcome = runner.evaluate_adaptive_cases(
        selected, gateway=scripted, max_model_calls=3,
    )
    assert outcome["total"] == 5
    assert outcome["model_calls"] == len(scripted.calls) == 3
    assert outcome["baseline"]["narrow_confusion"]["tp"] == 1
    assert outcome["adaptive"]["narrow_confusion"]["tp"] == 3
    assert outcome["adaptive"]["narrow_confusion"]["fp"] == 0
    assert outcome["model_scope_vetoes"] == 1
    assert outcome["new_true_scope"] == ["SC015", "SC019"]
    assert outcome["new_false_scope"] == []
    for row in outcome["cases"]:
        assert "text" not in row
        assert "evidence_quote" not in row
        assert "raw_output" not in row
        for field in row["fields"].values():
            if field["predicted"] is None:
                assert field["match"] is False
    for key, counts in outcome["by_dimension"].items():
        assert counts["correct"] + counts["incorrect"] == counts["supported"], key
        assert counts["supported"] + counts["abstained"] == 5, key


def test_model_budget_does_not_call_when_exhausted():
    runner = _runner()
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    selected = [item for item in data["examples"]
                if item["id"] in ("SC015", "SC019", "SC025")]
    gateway = ScriptedGateway({
        item["text"]: _proposal(item["text"])
        for item in selected
    })
    result = runner.evaluate_adaptive_cases(
        selected, gateway=gateway, max_model_calls=1,
    )
    assert result["model_calls"] == len(gateway.calls) == 1
    assert result["adaptive"]["scope_emissions"] == 1


def test_cli_requires_opt_in_model_and_never_overwrites(tmp_path):
    from subprocess import run
    output = tmp_path / "adaptive.json"
    cmd = [sys.executable, str(SCRIPTS / "evaluate_adaptive_semantic_scope.py"),
           "--report", str(output)]
    r = run(cmd, text=True, capture_output=True, timeout=10)
    assert r.returncode != 0 and "required" in r.stderr
    output.write_text("leave me alone", encoding="utf-8")
    r = run(cmd + ["--model", "local-only"], text=True,
            capture_output=True, timeout=10)
    assert r.returncode != 0
    assert "refusing to overwrite" in r.stderr
    assert output.read_text(encoding="utf-8") == "leave me alone"


def test_offline_module_import_does_not_require_psycopg():
    from subprocess import run
    code = (
        "import builtins; old=builtins.__import__; "
        "builtins.__import__=lambda name,*a,**kw: "
        "(_ for _ in ()).throw(ImportError('psycopg forbidden')) "
        "if name=='psycopg' or name.startswith('psycopg.') "
        "else old(name,*a,**kw); "
        "import persona_agent.semantic_adaptive_evaluation"
    )
    r = run([sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
