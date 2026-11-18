"""Offline v2 structured fixture evaluation: data provenance, safety and scope."""
import json
from pathlib import Path
from subprocess import run
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_memory_conflict_eligibility.py"
SUITE = ROOT / "scenarios" / "mvp02c10_memory_conflict_diagnostic_v2.json"
FIXTURES = ROOT / "scenarios" / "mvp02c11_structured_eligibility_fixtures.json"


def test_curated_metadata_fixture_agreement_without_nli(tmp_path):
    out = tmp_path / "report.json"
    r = run(
        [sys.executable, str(SCRIPT), "--suite", str(SUITE),
         "--fixtures", str(FIXTURES), "--report", str(out)],
        text=True, capture_output=True, timeout=20,
    )
    assert r.returncode == 0, r.stderr + "\n" + r.stdout
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "mvp02c11.eligibility_fixture_report.v1"
    assert doc["total_selected"] == 21
    assert doc["source_suite_total"] == 94
    assert doc["disposition_fixture_agreements"] == 21
    assert doc["scope_oracle_agreements"] == 21
    assert doc["eligibility_oracle_agreements"] == 21
    assert doc["suite_sha256"] == "46310133cd3909b7e73b310478e2fed3f0f54d54018e1cb6e56f4118ca23b462"
    assert len({case["id"] for case in doc["cases"]}) == 21
    assert [case["id"] for case in doc["cases"] if
            case["decision"]["disposition"] == "TENTATIVE_RESOLUTION"] == [
                "MC071", "MC072", "MC074",
            ]
    corrections = {case["id"] for case in doc["cases"]
                   if case["decision"]["disposition"] == "CORRECTION_PATH"}
    assert corrections == {"MC079", "MC080"}
    assert all(not case["decision"]["nli_allowed"] for case in doc["cases"]
               if case["id"] in corrections)
    assert "NOT a semantic extraction benchmark" in " ".join(doc["limitations"])


def test_fixture_data_cannot_reuse_stale_suite_hash(tmp_path):
    manifest = json.loads(FIXTURES.read_text(encoding="utf-8"))
    manifest["source_suite_sha256"] = "0" * 64
    corrupt = tmp_path / "corrupt-fixture.json"
    corrupt.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "output.json"
    r = run(
        [sys.executable, str(SCRIPT), "--suite", str(SUITE),
         "--fixtures", str(corrupt), "--report", str(out)],
        text=True, capture_output=True, timeout=10,
    )
    assert r.returncode == 2
    assert "source SHA mismatch" in r.stderr
    assert not out.exists()


def test_report_not_overwritten(tmp_path):
    out = tmp_path / "already.json"
    out.write_text("don't overwrite", encoding="utf-8")
    r = run([sys.executable, str(SCRIPT), "--report", str(out)],
            text=True, capture_output=True, timeout=10)
    assert r.returncode != 0
    assert "refusing to overwrite" in r.stderr
    assert out.read_text(encoding="utf-8") == "don't overwrite"


def test_structural_fixture_reference_is_subset_of_source():
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    ids = [item["id"] for item in data["fixtures"]]
    assert len(set(ids)) == len(ids) == 21
    assert set(ids).issubset({case["id"] for case in suite["examples"]})
    assert data["annotation_status"] == "SYNTHETIC_GOLD_AWARE_NOT_INDEPENDENT"
    assert data["source_suite_path"] == "scenarios/mvp02c10_memory_conflict_diagnostic_v2.json"
