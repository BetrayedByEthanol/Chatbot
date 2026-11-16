"""Small deterministic reference adapter for Stage A semantic diagnostics.

Intentionally narrow: no general-purpose sentiment/temporal understanding,
new inference backend, or automatic persistence. Unknown statements abstain.
This gives later Hugging Face adapters an executable contract and baseline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from time import perf_counter_ns
from uuid import UUID

from .semantic_contracts import (
    SemanticAnnotationBatch, SemanticClaim, SemanticClassifier,
    validate_semantic_evidence,
)

_ACTIVITY = r"(?P<topic>gardening|reading|walking)"
# Ordered, anchored expressions: only statements supported by the baseline.
_RULES = (
    (re.compile(rf"^I don't enjoy {_ACTIVITY} anymore\.$", re.I),
     "PREFERENCE", "CURRENT", "ASSERTED", "CORRECTING"),
    (re.compile(rf"^I used to enjoy {_ACTIVITY}\.$", re.I),
     "PREFERENCE", "PAST_HABITUAL", "ASSERTED", "INFORMING"),
    (re.compile(rf"^I enjoyed {_ACTIVITY} yesterday\.$", re.I),
     "EXPERIENCE", "PAST_EVENT", "ASSERTED", "INFORMING"),
    (re.compile(rf"^Maybe I should go {_ACTIVITY} tomorrow\.$", re.I),
     "INTENTION", "FUTURE", "TENTATIVE", "INFORMING"),
    (re.compile(rf"^I've gotten into {_ACTIVITY} lately\.$", re.I),
     "PREFERENCE", "CURRENT", "ASSERTED", "INFORMING"),
    (re.compile(rf"^I (?:really )?(?:enjoy|like|love) {_ACTIVITY}\.$", re.I),
     "PREFERENCE", "CURRENT", "ASSERTED", "INFORMING"),
)


@dataclass(frozen=True)
class RuleSemanticClassifier:
    classifier_id: str = "semantic.stage_a.reference_rules"
    classifier_version: str = "mvp02c3.rules.1"

    def classify(
        self, text: str, *, source_turn_id: UUID,
    ) -> SemanticAnnotationBatch:
        if not text or len(text) > 500 or text.strip() != text:
            reason = "OUT_OF_SCOPE"
        else:
            reason = "NO_SUPPORTED_CLAIM"
            for expression, statement, temporal, modality, pragmatic in _RULES:
                match = expression.fullmatch(text)
                if match is not None:
                    return SemanticAnnotationBatch(
                        source_turn_id=source_turn_id,
                        classifier_id=self.classifier_id,
                        classifier_version=self.classifier_version,
                        route="DETERMINISTIC",
                        claims=(SemanticClaim(
                            evidence_quote=text,
                            statement_type=statement,
                            temporal_scope=temporal,
                            modality=modality,
                            pragmatic_function=pragmatic,
                            topic_label=match["topic"].casefold(),
                            confidence=0.9,  # Rule match; NOT calibrated truth.
                        ),),
                    )
        return SemanticAnnotationBatch(
            source_turn_id=source_turn_id,
            classifier_id=self.classifier_id,
            classifier_version=self.classifier_version,
            route="DETERMINISTIC",
            abstention_reason=reason,
        )


def diagnostic_for_turn(
    classifier: SemanticClassifier, *, text: str, source_turn_id: UUID,
) -> dict:
    """Read-only shadow evaluation. Errors are visible, never silent success."""
    start=perf_counter_ns()
    try:
        proposal=validate_semantic_evidence(
            classifier.classify(text,source_turn_id=source_turn_id),
            source_turn_id=source_turn_id,source_text=text,
        )
        result={
            "status":"ANNOTATED" if proposal.claims else "ABSTAINED",
            "batch":proposal.model_dump(mode="json"),
        }
    except Exception as exc:
        result={
            "status":"ERROR",
            "error_type":type(exc).__name__,
            "error":str(exc)[:300],
        }
    result["latency_ms"]=round((perf_counter_ns()-start)/1_000_000,3)
    result["stage"]="SHADOW_AFTER_DELIVERY"
    result["source_turn_id"]=str(source_turn_id)
    return result
