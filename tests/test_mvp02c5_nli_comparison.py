"""MVP-02C.5: deterministic, offline NLI benchmarking contract tests."""
import importlib.util
import json
from pathlib import Path
from subprocess import run
import sys

import pytest

from persona_agent.nli_evaluation import (
    LABELS, NLIPair, compare_nli_runs, load_nli_suite, summarize_nli,
)

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"scenarios"/"mvp02c5_nli_comparison.json"


def _cli_module():
    path=ROOT/"scripts"/"compare_local_nli.py"
    spec=importlib.util.spec_from_file_location("mvp02c5_cli",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewable_gold_suite_covers_all_labels_and_reproduces_original_failure():
    pairs,digest=load_nli_suite(DATA)
    assert len(pairs)==54 and len(digest)==64
    assert {label:sum(p.label==label for p in pairs) for label in LABELS}=={
        "contradiction":18,"entailment":18,"neutral":18,
    }
    assert next(p for p in pairs if p.id=="N01")==NLIPair(
        id="N01",label="neutral",group="unrelated_original",
        premise="I enjoy gardening.",hypothesis="I work as a programmer.",
    )
    assert next(p for p in pairs if p.id=="N02").premise=="I work as a programmer."
    assert any(p.group=="temporal_scope" for p in pairs)
    assert any(p.group=="future_decision" for p in pairs)


@pytest.mark.parametrize("change,message",[
    (lambda d: d.update(version="invalid"),"version"),
    (lambda d: d["examples"].append(d["examples"][0]),"duplicate"),
    (lambda d: d["examples"][0].update(label="unsupported"),"label"),
    (lambda d: d["examples"][0].update(fake_field="x"),"fields"),
    (lambda d: d["examples"][0].update(premise="  "),"length"),
])
def test_suite_rejects_incorrect_or_unbounded_inputs(tmp_path,change,message):
    data=json.loads(DATA.read_text())
    change(data)
    path=tmp_path/"suite.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match=message):
        load_nli_suite(path)


def _scores(predicted,conf=0.98):
    return {name:conf if name==predicted else (1-conf)/2 for name in LABELS}


def test_false_contradictions_and_class_metrics_are_evidence_linked():
    pairs,_=load_nli_suite(DATA)
    outputs={p.id:_scores(p.label) for p in pairs}
    outputs["N01"]=_scores("contradiction",0.997)
    report=summarize_nli(pairs,outputs)
    assert report["total"]==54 and report["correct"]==53
    assert report["confusion_gold_rows"]["neutral"]["contradiction"]==1
    assert report["false_contradiction_count"]==1
    assert report["high_confidence_false_contradictions"]==1
    assert report["false_contradiction_rate_noncontradiction"]==round(1/36,4)
    assert report["per_label"]["neutral"]["recall"]==round(17/18,4)
    assert report["per_group"]["unrelated_original"]["accuracy"]==0
    row=next(c for c in report["cases"] if c["id"]=="N01")
    assert row["predicted"]=="contradiction"
    assert row["premise"]=="I enjoy gardening."


def test_bad_model_outputs_never_count_as_success():
    pairs,_=load_nli_suite(DATA)
    outputs={p.id:_scores(p.label) for p in pairs}
    outputs["N01"]={"contradiction":0.6,"neutral":0.3,"entailment":0.3}
    with pytest.raises(ValueError,match="normalized"):
        summarize_nli(pairs,outputs)
    outputs["N01"]={"contradiction":float("nan"),"neutral":0,"entailment":1}
    with pytest.raises(ValueError,match="non-finite"):
        summarize_nli(pairs,outputs)
    del outputs["N01"]
    with pytest.raises(ValueError,match="match suite IDs"):
        summarize_nli(pairs,outputs)


def test_diff_reports_model_changes_and_error_direction():
    pairs,_=load_nli_suite(DATA)
    base={p.id:_scores(p.label) for p in pairs}
    base["N01"]=_scores("contradiction")
    large={p.id:_scores(p.label) for p in pairs}
    left={"key":"base:int8","metrics":summarize_nli(pairs,base)}
    right={"key":"large:int8","metrics":summarize_nli(pairs,large)}
    difference=compare_nli_runs([left,right])
    assert len(difference)==1
    assert difference[0]["prediction_disagreements"]==["N01"]
    assert difference[0]["right_only_correct"]==["N01"]
    assert difference[0]["left_only_correct"]==[]
    assert difference[0]["contradiction_false_alert_delta_right_minus_left"]==-1


def test_variant_benchmark_uses_same_pairs_without_ml_dependencies(tmp_path,monkeypatch):
    cli=_cli_module()
    pairs,_=load_nli_suite(DATA)
    (tmp_path/"config.json").write_text('{"id2label":{"0":"contradiction","1":"entailment","2":"neutral"}}')
    (tmp_path/"model.onnx").write_bytes(b"synthetic-model")
    mapping={(p.premise,p.hypothesis):p for p in pairs}
    class FakeModel:
        def __init__(self,config):
            self.ort_version="test-runtime"
            self.model_file=tmp_path/"model.onnx"
            self.labels={"contradiction":0,"entailment":1,"neutral":2}
            self.config=config
        def nli(self,premise,hypothesis):
            pair=mapping[(premise,hypothesis)]
            return _scores("contradiction" if pair.id=="N01" else pair.label)
    monkeypatch.setattr(cli,"input_diagnostics",lambda model,first:{
        "pair_example_id":first.id,"nli_label_indices":model.labels,
    })
    output=cli.evaluate_variant(
        pairs,model_dir=tmp_path,precision="int8",threads=2,
        repetitions=2,warmup=1,model_factory=FakeModel,
    )
    assert output["metrics"]["total"]==54
    assert output["metrics"]["false_contradiction_count"]==1
    assert output["precision"]=="int8"
    assert output["repetitions"]==2
    assert output["median_pair_latency_ms"]>=0
    assert len(output["model_config_sha256"])==64
    assert output["input_diagnostics"]["nli_label_indices"]["neutral"]==2


def test_comparison_cli_help_and_fail_closed_preflight(tmp_path):
    source=ROOT/"scripts"/"compare_local_nli.py"
    help_result=run([sys.executable,str(source),"--help"],
                    capture_output=True,text=True,timeout=10)
    assert help_result.returncode==0,help_result.stderr
    assert "--models" in help_result.stdout
    report=tmp_path/"not_created.json"
    bad=run([
        sys.executable,str(source),
        "--suite",str(DATA),"--models-root",str(tmp_path),
        "--models","nli-deberta-v3-large","--precisions","int8",
        "--report",str(report),
    ],capture_output=True,text=True,timeout=10)
    assert bad.returncode!=0
    assert "No compatible INT8" in bad.stderr
    assert not report.exists()


def test_report_overwrite_is_prevented_before_any_model_load(tmp_path):
    source=ROOT/"scripts"/"compare_local_nli.py"
    report=tmp_path/"existing.json"
    report.write_text("original")
    result=run([
        sys.executable,str(source),"--report",str(report),
    ],capture_output=True,text=True,timeout=10)
    assert result.returncode!=0
    assert "refusing to overwrite" in result.stderr
    assert report.read_text()=="original"
