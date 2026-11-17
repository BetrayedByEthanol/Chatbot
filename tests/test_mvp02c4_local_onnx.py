"""MVP-02C.4 local ONNX: portable variant selection and offline benchmark tests."""
import importlib.util
from pathlib import Path
from subprocess import run
import sys

import pytest

from persona_agent.local_onnx import (
    LocalONNXConfig, cpu_instruction_flags, nli_indices, select_onnx_file,
)


def models(tmp_path):
    folder=tmp_path/"model"/"onnx"
    folder.mkdir(parents=True)
    for name in (
        "model.onnx",
        "model_qint8_avx512_vnni.onnx",
        "model_qint8_avx512.onnx",
        "model_quint8_avx2.onnx",
    ):
        (folder/name).write_bytes(b"fixture")
    return folder.parent


def test_threadripper_avx2_only_chooses_compatible_variant(tmp_path):
    root=models(tmp_path)
    assert select_onnx_file(
        root,precision="auto",flags=frozenset({"avx2"}),machine="x86_64"
    ).name=="model_quint8_avx2.onnx"
    assert select_onnx_file(
        root,precision="int8",flags=frozenset({"avx2"}),machine="x86_64"
    ).name=="model_quint8_avx2.onnx"
    assert select_onnx_file(
        root,precision="fp32",flags=frozenset({"avx2"}),machine="x86_64"
    ).name=="model.onnx"


def test_ryzen_vnni_prefers_compatible_int8(tmp_path):
    root=models(tmp_path)
    assert select_onnx_file(
        root,precision="auto",
        flags=frozenset({"avx2","avx512f","avx512_vnni"}),
        machine="x86_64",
    ).name=="model_qint8_avx512_vnni.onnx"


def test_only_detected_process_flags_may_enable_avx512(tmp_path):
    root=models(tmp_path)
    assert select_onnx_file(
        root,flags=frozenset({"avx512_vnni","avx2"}),machine="x86_64"
    ).name=="model_quint8_avx2.onnx"
    assert select_onnx_file(
        root,flags=frozenset({"avx512f","avx2"}),machine="x86_64"
    ).name=="model_qint8_avx512.onnx"
    assert select_onnx_file(
        root,flags=frozenset(),machine="x86_64"
    ).name=="model.onnx"
    assert select_onnx_file(
        root,flags=frozenset(),machine="arm64"
    ).name=="model.onnx"


def test_int8_never_silently_claims_fp32_as_quantized(tmp_path):
    root=models(tmp_path)
    (root/"onnx"/"model_quint8_avx2.onnx").unlink()
    with pytest.raises(FileNotFoundError,match="No compatible INT8"):
        select_onnx_file(
            root,precision="int8",flags=frozenset({"avx2"}),machine="x86_64"
        )
    assert select_onnx_file(
        root,precision="auto",flags=frozenset({"avx2"}),machine="x86_64"
    ).name=="model.onnx"


def test_missing_onnx_never_downloads_anything(tmp_path):
    with pytest.raises(FileNotFoundError,match="Missing local"):
        select_onnx_file(
            tmp_path,precision="fp32",flags=frozenset(),machine="x86_64"
        )


def test_cpu_flags_are_conservative_and_vm_aware():
    text="processor : 0\nflags : fpu sse4_2 avx2\n\nprocessor : 1\nflags : avx512f"
    assert cpu_instruction_flags(text)==frozenset({"fpu","sse4_2","avx2"})
    assert cpu_instruction_flags("garbage")==frozenset()


def test_nli_labels_are_config_driven_and_fail_on_ambiguous_order():
    assert nli_indices({
        "id2label":{"0":"CONTRADICTION","1":"NEUTRAL","2":"ENTAILMENT"}
    })=={"contradiction":0,"neutral":1,"entailment":2}
    with pytest.raises(ValueError,match="ambiguous"):
        nli_indices({
            "id2label":{"0":"LABEL_0","1":"LABEL_1","2":"LABEL_2"}
        })
    assert nli_indices({},explicit_label_order=(
        "entailment","neutral","contradiction",
    ))=={"entailment":0,"neutral":1,"contradiction":2}
    with pytest.raises(ValueError,match="permutation"):
        nli_indices({},explicit_label_order=("entailment","entailment","neutral"))
    with pytest.raises(ValueError,match="three"):
        nli_indices({"id2label":{}})


def test_optional_model_config_checks_are_hardware_independent(tmp_path):
    with pytest.raises(ValueError,match="1..32"):
        LocalONNXConfig(tmp_path,"nli",intra_op_threads=0)
    with pytest.raises(ValueError,match="only applies"):
        LocalONNXConfig(tmp_path,"embedding",explicit_nli_label_order=(
            "contradiction","neutral","entailment",
        ))
    with pytest.raises(ValueError,match="unsupported"):
        LocalONNXConfig(tmp_path,"emotion")


def _benchmark_module():
    source=Path(__file__).resolve().parents[1]/"scripts"/"benchmark_local_onnx.py"
    spec=importlib.util.spec_from_file_location("mvp02c4_onnx_benchmark",source)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_reports_precise_provenance_without_real_models(tmp_path):
    runner=_benchmark_module()
    class FakeModel:
        def __init__(self, config):
            self.config=config
            self.model_file=tmp_path/"artifact.onnx"
            self.model_file.write_bytes(b"offline-fixture")
            self.ort_version="fake-unit-test"
        def nli(self, premise, hypothesis):
            gold=next(label for p,h,label in runner.NLI_EXAMPLES
                      if p==premise and h==hypothesis)
            return {label:(0.8 if label==gold else 0.1)
                    for label in ("entailment","neutral","contradiction")}
    result=runner.benchmark_one(
        "nli",models_root=tmp_path,precision="int8",threads=2,
        repetitions=3,warmup=1,model_factory=FakeModel,
    )
    assert result["smoke_correct"]==3
    assert result["smoke_total"]==3
    assert len(result["smoke_cases"])==3
    assert result["onnxruntime_version"]=="fake-unit-test"
    assert result["provider"]=="CPUExecutionProvider"
    assert result["load_ms"]>=0 and result["p95_benchmark_ms"]>=0
    assert result["model_bytes"]==len(b"offline-fixture")


def test_local_benchmark_cli_help_does_not_load_optional_onnx(tmp_path):
    source=Path(__file__).resolve().parents[1]/"scripts"/"benchmark_local_onnx.py"
    result=run([sys.executable,str(source),"--help"],
               capture_output=True,text=True,timeout=10)
    assert result.returncode==0, result.stderr
    assert "--models-root" in result.stdout
    assert "--precision" in result.stdout
