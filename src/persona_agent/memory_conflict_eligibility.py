"""MVP-02C.11: read-only, metadata-based Memory conflict eligibility.

This is NOT a text classifier, authorization check, Memory write, or a
substitute for domain-owned source verification. Both inputs must be built
from validated semantic evidence and an authenticated Memory/Conversation
snapshot by a future owning-domain adapter. NLI scores are not inputs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .memory_contracts import MemoryKind
from .semantic_contracts import Modality, PragmaticFunction, StatementType, TemporalScope


Admission = Literal["ADMITTED_ASSERTED", "TENTATIVE_UNADMITTED", "OTHER_UNADMITTED"]
Disposition = Literal[
    "NLI_CANDIDATE", "CORRECTION_PATH", "TENTATIVE_RESOLUTION",
    "SKIP", "ABSTAIN",
]
Reason = Literal[
    "CROSS_INSTANCE", "DIFFERENT_MEMORY_KIND", "DIFFERENT_SUBJECT",
    "DIFFERENT_PREDICATE", "DIFFERENT_CONTEXT", "DISJOINT_TIME",
    "UNCERTAIN_TIME", "SOURCE_NOT_ADMITTED", "TENTATIVE_SOURCE",
    "TENTATIVE_NEW_CLAIM", "NONASSERTED_NEW_CLAIM",
    "INCOMPARABLE_STATEMENT", "UNKNOWN_INTERPRETATION",
    "ASSERTED_COMPARABLE", "EXPLICIT_CORRECTION",
]
_KEY = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_PRESENT = frozenset(("CURRENT", "HABITUAL"))
_PAST = frozenset(("PAST_EVENT", "PAST_HABITUAL"))


class ClaimScope(BaseModel):
    """Keys are domain-canonical identities, not untrusted extracted strings.

    context_key must be explicit (e.g. 'general', 'office', 'bedroom',
    'pet_dog', 'pet_cat'). Two unknown contexts must not be made equal by
    representing them as a shared null; unresolved context requires abstention
    upstream instead of inventing 'general'.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_key: str = Field(pattern=_KEY)
    predicate_key: str = Field(pattern=_KEY)
    context_key: str = Field(pattern=_KEY)
    temporal_scope: TemporalScope
    interval_start: AwareDatetime | None = None
    interval_end: AwareDatetime | None = None

    @model_validator(mode="after")
    def interval_contract(self):
        if (self.interval_start is None) != (self.interval_end is None):
            raise ValueError("temporal interval requires both endpoints")
        if (self.interval_start is not None
                and self.interval_end < self.interval_start):
            raise ValueError("temporal interval ends before it starts")
        return self


class PriorClaimSnapshot(BaseModel):
    """Metadata supplied by an owner, never by an NLI/semantic model.

    ADMITTED_ASSERTED models an ACTIVE Memory record; an admitted record
    requires its real identity and version. This type cannot independently
    verify that a caller obtained the snapshot from PostgreSQL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    instance_id: UUID
    source_turn_id: UUID
    memory_kind: MemoryKind
    scope: ClaimScope
    admission: Admission
    memory_id: UUID | None = None
    memory_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def memory_reference_contract(self):
        admitted = self.admission == "ADMITTED_ASSERTED"
        if admitted != (self.memory_id is not None and self.memory_version is not None):
            raise ValueError("only admitted Memory snapshots have both memory ID and version")
        return self


class NewClaim(BaseModel):
    """Normalized advisory claim, with authenticated provenance supplied by caller.

    statement_type, modality and pragmatic_function correspond to Stage A
    semantic contracts; source grounding and permission checks are EXTERNAL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    instance_id: UUID
    source_turn_id: UUID
    memory_kind: MemoryKind
    scope: ClaimScope
    statement_type: StatementType
    modality: Modality
    pragmatic_function: PragmaticFunction
    value_text: str = Field(min_length=1, max_length=2000)


class EligibilityDecision(BaseModel):
    """Read-only proposal. Never usable as a Memory write or authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: Disposition
    reason: Reason
    scope_compatible: bool | None
    nli_allowed: bool

    @model_validator(mode="after")
    def consistent(self):
        if self.nli_allowed != (self.disposition == "NLI_CANDIDATE"):
            raise ValueError("only comparable asserted claims may enter NLI")
        if self.disposition in ("NLI_CANDIDATE", "CORRECTION_PATH",
                                "TENTATIVE_RESOLUTION") and self.scope_compatible is not True:
            raise ValueError("routable claims require compatible scope")
        return self


def _decision(disposition: Disposition, reason: Reason,
              scope_compatible: bool | None = None) -> EligibilityDecision:
    return EligibilityDecision(
        disposition=disposition, reason=reason,
        scope_compatible=scope_compatible,
        nli_allowed=disposition == "NLI_CANDIDATE",
    )


def _time_compatibility(old: ClaimScope, new: ClaimScope) -> bool | None:
    """True=possibly same temporal fact; False=disjoint; None=unresolved.

    Explicit bounded intervals take precedence. With only broad temporal
    labels, same current/habitual slots are reviewable but NOT established
    contradictions. Distinct past/present/future time categories are disjoint;
    historical events without dates are ambiguous.
    """
    if old.interval_start is not None and new.interval_start is not None:
        return not (old.interval_end < new.interval_start
                    or new.interval_end < old.interval_start)
    if old.temporal_scope == "UNSPECIFIED" or new.temporal_scope == "UNSPECIFIED":
        return None
    a, b = old.temporal_scope, new.temporal_scope
    if ((a in _PAST and b in _PRESENT)
            or (b in _PAST and a in _PRESENT)
            or (a == "FUTURE" and b in (_PAST | _PRESENT))
            or (b == "FUTURE" and a in (_PAST | _PRESENT))):
        return False
    if (old.interval_start is not None) != (new.interval_start is not None):
        return None
    if a in _PRESENT and b in _PRESENT:
        return True
    return None


def evaluate_conflict_eligibility(
    prior: PriorClaimSnapshot, new: NewClaim,
) -> EligibilityDecision:
    """Deterministic, no-I/O gate over supplied structured metadata.

    Inputs represent snapshots, not claims of verified truth. The caller must
    independently validate identity, selected persona generation, source
    turn, current record version, semantic grounding, privacy and policy.
    Neither CORRECTION_PATH nor TENTATIVE_RESOLUTION executes a mutation.
    """
    if not isinstance(prior, PriorClaimSnapshot) or not isinstance(new, NewClaim):
        raise TypeError("validated PriorClaimSnapshot and NewClaim required")
    if prior.user_id != new.user_id or prior.instance_id != new.instance_id:
        return _decision("SKIP", "CROSS_INSTANCE", False)
    if prior.memory_kind != new.memory_kind:
        return _decision("SKIP", "DIFFERENT_MEMORY_KIND", False)
    old_scope, new_scope = prior.scope, new.scope
    if old_scope.subject_key != new_scope.subject_key:
        return _decision("SKIP", "DIFFERENT_SUBJECT", False)
    if old_scope.predicate_key != new_scope.predicate_key:
        return _decision("SKIP", "DIFFERENT_PREDICATE", False)
    if old_scope.context_key != new_scope.context_key:
        return _decision("SKIP", "DIFFERENT_CONTEXT", False)
    temporal = _time_compatibility(old_scope, new_scope)
    if temporal is False:
        return _decision("SKIP", "DISJOINT_TIME", False)
    if temporal is None:
        return _decision("ABSTAIN", "UNCERTAIN_TIME", None)
    if new.modality == "UNKNOWN" or new.pragmatic_function == "UNKNOWN":
        return _decision("ABSTAIN", "UNKNOWN_INTERPRETATION", True)
    if new.modality == "TENTATIVE":
        return _decision("SKIP", "TENTATIVE_NEW_CLAIM", True)
    if new.modality in ("CONDITIONAL", "HYPOTHETICAL"):
        return _decision("SKIP", "NONASSERTED_NEW_CLAIM", True)
    if new.modality != "ASSERTED" or new.pragmatic_function in ("REQUESTING", "ASKING"):
        return _decision("SKIP", "NONASSERTED_NEW_CLAIM", True)
    if new.statement_type in ("UNKNOWN", "INTENTION"):
        return _decision("SKIP", "INCOMPARABLE_STATEMENT", True)
    if prior.admission == "TENTATIVE_UNADMITTED":
        return _decision("TENTATIVE_RESOLUTION", "TENTATIVE_SOURCE", True)
    if prior.admission == "OTHER_UNADMITTED":
        return _decision("SKIP", "SOURCE_NOT_ADMITTED", True)
    if new.pragmatic_function == "CORRECTING":
        return _decision("CORRECTION_PATH", "EXPLICIT_CORRECTION", True)
    return _decision("NLI_CANDIDATE", "ASSERTED_COMPARABLE", True)
