#!/usr/bin/env python3
"""MVP-02C.14E: LOCAL, read-only, reference-span-conditioned model comparison.

Models receive source text and a reference-provided target claim quote, but
NEVER reference semantic labels. This measures *conditional classification*,
NOT extraction, qualifier preservation, relations, Memory correctness or real
deployment accuracy. Sources are synthetic author drafts, not reviewed gold.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from persona_agent.semantic_contracts import StatementType
from persona_agent.semantic_dimension_contracts import (
    SemanticDimensionsV2, validate_semantic_dimensions,
)
from evaluate_semantic_dimensions import DRAFT_STATUS, SUITE_VERSION, _read_json

VERSION = "mvp02c14e.semantic_model_probe.v1"
PROMPT_VERSION = "mvp02c14e.span_conditioned.v1"
LABEL_FIELDS = (
    "statement_type", "certainty", "polarity", "habituality",
    "conditionality", "hypotheticality", "temporal_references",
)
TEMPORAL = ("PAST", "PRESENT", "FUTURE")
# Fixed hypotheses. Each pair is a model inference problem, not a rule for
# deriving reliable semantic truth; confidence is uncalibrated.
HYPOTHESES = {
    "statement_type": {
        "FACT": "The target claim states a factual situation.",
        "PREFERENCE": "The target claim expresses a preference.",
        "EXPERIENCE": "The target claim describes an experience.",
        "INTENTION": "The target claim expresses a plan or intention.",
        "OPINION": "The target claim expresses an opinion or judgment.",
    },
    "certainty": {
        "ASSERTED": "The target claim is presented as asserted.",
        "TENTATIVE": "The target claim expresses uncertainty.",
    },
    "polarity": {
        "POSITIVE": "The target claim expresses an affirmative or positive proposition.",
        "NEGATIVE": "The target claim expresses a negative or negated proposition.",
    },
    "habituality": {
        "TRUE": "The target claim describes a recurring or habitual action.",
        "FALSE": "The target claim does not describe a recurring or habitual action.",
    },
    "conditionality": {
        "TRUE": "The target claim is conditional on a stated condition.",
        "FALSE": "The target claim is not conditional on a stated condition.",
    },
    "hypotheticality": {
        "TRUE": "The target claim describes an imagined or hypothetical possibility.",
        "FALSE": "The target claim is not hypothetical.",
    },
    "past": {
        "TRUE": "The target claim refers to the past.",
        "FALSE": "The target claim does not refer to the past.",
    },
    "present": {
        "TRUE": "The target claim refers to the present.",
        "FALSE": "The target claim does not refer to the present.",
    },
    "future": {
        "TRUE": "The target claim refers to the future.",
        "FALSE": "The target claim does not refer to the future.",
    },
}
assert all(len(set(group.values())) == len(group) for group in HYPOTHESES.values())


class ProbeLabels(BaseModel):
    """No evidence, IDs or source text: only validated candidate dimensions."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    statement_type: StatementType = "UNKNOWN"
    certainty: str = "UNKNOWN"
    polarity: str = "UNKNOWN"
    habituality: bool | None = None
    conditionality: bool | None = None
    hypotheticality: bool | None = None
    temporal_references: tuple[str, ...] = Field(default=(), max_length=3)

    @classmethod
    def from_mapping(cls, value: dict) -> "ProbeLabels":
        if not isinstance(value, dict):
            raise ValueError("prediction must be a JSON object")
        if set(value) != set(LABEL_FIELDS):
            raise ValueError("all and only dimension fields are required")
        # Parse as JSON so legitimate arrays become typed tuples while
        # Pydantic's strict scalar validation still rejects coercions.
        item = cls.model_validate_json(json.dumps(value))
        if item.certainty not in ("ASSERTED", "TENTATIVE", "UNKNOWN"):
            raise ValueError("invalid certainty")
        if item.polarity not in ("POSITIVE", "NEGATIVE", "UNKNOWN"):
            raise ValueError("invalid polarity")
        if len(set(item.temporal_references)) != len(item.temporal_references):
            raise ValueError("duplicate temporal references")
        if any(label not in TEMPORAL for label in item.temporal_references):
            raise ValueError("invalid temporal reference")
        return item


class ProbeBackend(Protocol):
    backend_id: str

    def predict(self, *, source: str, quote: str) -> ProbeLabels:
        ...


class SpacyStructuralProbe:
    """Explicitly lightweight tokenizer cues, NOT a semantic classifier."""

    backend_id = "spacy.blank_en.lexical_cues.v1"

    def __init__(self):
        try:
            import spacy
        except ImportError as exc:
            raise RuntimeError("Install optional dependency: pip install spacy") from exc
        self.nlp = spacy.blank("en")

    def predict(self, *, source: str, quote: str) -> ProbeLabels:
        # Claim-specific quote only; cues in another claim cannot be borrowed.
        tokens = [token.lower_ for token in self.nlp(quote)]
        words = set(tokens)
        joined = " ".join(tokens)
        past = "used to" in joined or bool(words & {"yesterday", "ago", "2023"})
        present = bool(words & {"now", "currently", "today"})
        future = "next month" in joined or bool(words & {"tomorrow", "next"})
        negative = bool(words & {"not", "n't", "no", "never", "dislike", "hate", "don't"})
        tentative = bool(words & {"might", "maybe", "perhaps", "possibly"})
        conditional = "if" in words
        hypothetical = "would" in words
        habitual = bool(words & {"every", "usually", "regularly", "weekly"})
        return ProbeLabels.from_mapping({
            "statement_type": "UNKNOWN",
            "certainty": "TENTATIVE" if tentative else "UNKNOWN",
            "polarity": "NEGATIVE" if negative else "UNKNOWN",
            "habituality": True if habitual else None,
            "conditionality": True if conditional else None,
            "hypotheticality": True if hypothetical else None,
            "temporal_references": tuple(
                label for label, match in (
                    ("PAST", past), ("PRESENT", present), ("FUTURE", future)
                ) if match
            ),
        })


class DebertaNliProbe:
    """Reuse MVP-02C.14B local Transformers backend and strict label parsing."""

    def __init__(self, model_dir: Path, *, device: str = "cpu",
                 min_score: float = 0.65, min_margin: float = 0.10):
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 <= value <= 1 for value in (min_score, min_margin)):
            raise ValueError("invalid NLI thresholds")
        from evaluate_zeroshot_semantics import LocalTransformersZeroShot, _select
        self.client = LocalTransformersZeroShot(model_dir, device=device)
        self.backend_id = "deberta.local_nli." + model_dir.name
        self._select = _select
        self.min_score, self.min_margin = min_score, min_margin

    def predict(self, *, source: str, quote: str) -> ProbeLabels:
        premise = f"Full message: {source}\nTarget claim: {quote}"
        outcomes = {}
        for dimension, hypotheses in HYPOTHESES.items():
            result = self.client.classify([premise], list(hypotheses.values()))
            if not isinstance(result, list) or len(result) != 1:
                raise ValueError("invalid local NLI batch")
            selected = self._select(
                result[0], hypotheses, min_score=self.min_score,
                min_margin=self.min_margin,
            )
            outcomes[dimension] = selected["prediction"]
        times = [label for label, dimension in (
            ("PAST", "past"), ("PRESENT", "present"), ("FUTURE", "future")
        ) if outcomes[dimension] == "TRUE"]
        # A partially unknown temporal classification must abstain as a whole.
        if any(outcomes[dimension] is None for dimension in ("past", "present", "future")):
            times = []
        return ProbeLabels.from_mapping({
            "statement_type": outcomes["statement_type"] or "UNKNOWN",
            "certainty": outcomes["certainty"] or "UNKNOWN",
            "polarity": outcomes["polarity"] or "UNKNOWN",
            "habituality": (
                None if outcomes["habituality"] is None
                else outcomes["habituality"] == "TRUE"
            ),
            "conditionality": (
                None if outcomes["conditionality"] is None
                else outcomes["conditionality"] == "TRUE"
            ),
            "hypotheticality": (
                None if outcomes["hypotheticality"] is None
                else outcomes["hypotheticality"] == "TRUE"
            ),
            "temporal_references": times,
        })


class OllamaJsonProbe:
    """Local-only Ollama structured prediction; no external service fallback."""

    def __init__(self, *, model: str, url: str = "http://127.0.0.1:11434",
                 timeout_s: float = 60.0):
        parsed = urlsplit(url)
        if (parsed.scheme != "http"
                or (parsed.hostname or "").casefold() not in (
                    "127.0.0.1", "localhost", "::1"
                )
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ("", "/")
                or parsed.port is None or parsed.port <= 0):
            raise ValueError("Ollama endpoint must be an explicit local HTTP host:port")
        if not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise ValueError("model name required")
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 1 <= timeout_s <= 300:
            raise ValueError("timeout must be 1..300 seconds")
        self.url, self.model, self.timeout_s = url.rstrip("/"), model, timeout_s
        self.backend_id = "ollama.local_json." + model

    def predict(self, *, source: str, quote: str) -> ProbeLabels:
        instruction = (
            "Classify the TARGET CLAIM in the context of the FULL MESSAGE. "
            "Output ONLY JSON with exactly: statement_type "
            "(FACT, PREFERENCE, EXPERIENCE, INTENTION, OPINION, UNKNOWN), "
            "certainty (ASSERTED, TENTATIVE, UNKNOWN), polarity "
            "(POSITIVE, NEGATIVE, UNKNOWN), habituality, conditionality, "
            "hypotheticality (each true, false or null), temporal_references "
            "(array of zero or more of PAST, PRESENT, FUTURE). "
            "Use null or UNKNOWN if uncertain. A present intention can mention "
            "future time; a change in a preference does not necessarily correct "
            "the prior conversation. Never invent missing source text."
        )
        payload = json.dumps({
            "model": self.model, "stream": False, "format": "json",
            "options": {"temperature": 0, "num_predict": 350},
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": (
                    f"FULL MESSAGE:\n{source}\nTARGET CLAIM:\n{quote}"
                )},
            ],
        }).encode("utf-8")
        request = Request(
            self.url + "/api/chat", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise RuntimeError("non-200 Ollama response")
            raw = response.read(262144 + 1)
        if len(raw) > 262144:
            raise ValueError("Ollama response too large")
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("message"), dict):
            raise ValueError("invalid Ollama envelope")
        content = result["message"].get("content")
        if not isinstance(content, str) or len(content) > 10000:
            raise ValueError("invalid Ollama content")
        return ProbeLabels.from_mapping(json.loads(content))


def _blank_counts() -> dict:
    return {
        "eligible": 0, "supported": 0, "correct": 0, "incorrect": 0,
        "abstained": 0, "unscored_reference": 0,
    }


def _unknown(field: str, value) -> bool:
    return not value if field == "temporal_references" else (
        value is None or value == "UNKNOWN"
    )


def _add_score(counts: dict, field: str, reference, prediction):
    if _unknown(field, reference):
        counts["unscored_reference"] += 1
        return
    counts["eligible"] += 1
    if _unknown(field, prediction):
        counts["abstained"] += 1
    else:
        counts["supported"] += 1
        if ((set(reference) == set(prediction))
                if field == "temporal_references" else reference == prediction):
            counts["correct"] += 1
        else:
            counts["incorrect"] += 1


def evaluate_probe(suite_path: Path, *, backend: ProbeBackend,
                   case_ids: tuple[str, ...] | None = None) -> dict:
    """Run one backend; compute draft agreement WITHOUT feeding labels to it."""
    suite, digest = _read_json(suite_path)
    if (suite.get("version") != SUITE_VERSION
            or suite.get("annotation_status") != DRAFT_STATUS
            or not isinstance(suite.get("examples"), list)):
        raise ValueError("unexpected or invalid provisional suite")
    if case_ids is not None and (not case_ids or len(set(case_ids)) != len(case_ids)):
        raise ValueError("duplicate or empty case selection")
    selected = set(case_ids) if case_ids is not None else None
    all_ids = set()
    rows = []
    for entry in suite["examples"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "text", "reference"}:
            raise ValueError("invalid case")
        case_id, source = entry["id"], entry["text"]
        if not isinstance(case_id, str) or case_id in all_ids:
            raise ValueError("invalid or duplicate case IDs")
        all_ids.add(case_id)
        reference = SemanticDimensionsV2.model_validate_json(
            json.dumps(entry["reference"])
        )
        validate_semantic_dimensions(
            reference, source_turn_id=reference.source_turn_id, source_text=source
        )
        rows.append((case_id, source, reference))
    if selected is not None and not selected <= all_ids:
        raise ValueError("unknown case ID")
    rows = [row for row in rows if selected is None or row[0] in selected]
    if not rows:
        raise ValueError("no selected cases")

    scores = {field: _blank_counts() for field in LABEL_FIELDS}
    temporal = {
        label: {"tp": 0, "fp": 0, "fn": 0, "tn": 0,
                "abstained": 0, "unscored_reference": 0}
        for label in TEMPORAL
    }
    case_reports = []
    latencies = []
    errors = 0
    total_claims = 0
    for case_id, source, reference in rows:
        case_error = 0
        for claim in reference.claims:
            total_claims += 1
            start = perf_counter()
            try:
                guess = backend.predict(source=source, quote=claim.evidence.text)
                if not isinstance(guess, ProbeLabels):
                    raise TypeError("backend must return validated ProbeLabels")
                # Guard against backends constructed using unsafe model_copy.
                guess = ProbeLabels.from_mapping(guess.model_dump())
            except Exception:
                guess = ProbeLabels()
                case_error += 1
                errors += 1
            latencies.append((perf_counter() - start) * 1000)
            for field in LABEL_FIELDS:
                _add_score(scores[field], field, getattr(claim, field),
                           getattr(guess, field))
            if not claim.temporal_references:
                for label in TEMPORAL:
                    temporal[label]["unscored_reference"] += 1
            elif not guess.temporal_references:
                for label in TEMPORAL:
                    temporal[label]["abstained"] += 1
            else:
                for label in TEMPORAL:
                    expected, found = (
                        label in claim.temporal_references,
                        label in guess.temporal_references,
                    )
                    key = ("tp" if expected and found else "fn" if expected
                           else "fp" if found else "tn")
                    temporal[label][key] += 1
        case_reports.append({
            "id": case_id, "reference_claims": len(reference.claims),
            "errors": case_error,
        })
    return {
        "version": VERSION, "prompt_version": PROMPT_VERSION,
        "reference_mode": "ORACLE_CLAIM_SPANS_CLASSIFICATION_ONLY",
        "annotation_status": DRAFT_STATUS,
        "backend": backend.backend_id,
        "suite_sha256": digest,
        "sample_cases": len(rows), "sample_claims": total_claims,
        "error_claims": errors,
        "median_claim_inference_ms": (
            round(median(latencies), 3) if latencies else None
        ),
        "total_inference_ms": round(sum(latencies), 3),
        "by_dimension": scores, "by_temporal_label": temporal,
        "cases": case_reports,
        "limits": [
            "All labels are provisional AUTHOR DRAFTS, not adjudicated ground truth.",
            "Reference-provided claim spans are inputs, not model extractions.",
            "Qualifiers, relations, extraction and actual Memory safety NOT scored.",
            "Uncalibrated model scores are not deployment thresholds.",
            "No production routing, Memory writes, training or private-data export.",
        ],
    }


def main(argv=None, *, backend: ProbeBackend | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path(
        "scenarios/mvp02c14d_semantic_drafts.json"))
    parser.add_argument("--backend", choices=("spacy", "deberta", "ollama"),
                        required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout-s", type=float, default=60)
    parser.add_argument("--min-score", type=float, default=0.65)
    parser.add_argument("--min-margin", type=float, default=0.10)
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("refusing to overwrite an existing report")
    try:
        if backend is None:
            if args.backend == "spacy":
                backend = SpacyStructuralProbe()
            elif args.backend == "deberta":
                if args.model_dir is None:
                    raise ValueError("--model-dir required for DeBERTa")
                backend = DebertaNliProbe(
                    args.model_dir, device=args.device,
                    min_score=args.min_score, min_margin=args.min_margin,
                )
            else:
                if not args.model:
                    raise ValueError("--model required for Ollama")
                backend = OllamaJsonProbe(
                    model=args.model, url=args.ollama_url,
                    timeout_s=args.timeout_s,
                )
        report = evaluate_probe(
            args.suite, backend=backend,
            case_ids=tuple(args.case_ids) if args.case_ids else None,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
    except (ValueError, TypeError, OSError, json.JSONDecodeError, ValidationError,
            RuntimeError, ImportError):
        parser.error("invalid evaluation inputs or unavailable local backend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
