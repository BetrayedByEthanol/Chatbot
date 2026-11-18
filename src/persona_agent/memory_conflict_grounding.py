"""MVP-02C.12: source-grounded, database-backed read-only eligibility shadow.

Deliberately limited to existing deterministic Stage A activity-preference
grammar. This bridge does not generalize to other subjects/predicates, does
not register an event subscriber, and cannot approve Memory writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _require_actor
from .memory_conflict_eligibility import (
    ClaimScope, EligibilityDecision, NewClaim, PriorClaimSnapshot,
    evaluate_conflict_eligibility,
)
from .memory_extraction import parse_explicit_claim
from .operation_contracts import require_idle
from .runtime import PermissionDenied, StaleSelection
from .semantic_classification import RuleSemanticClassifier
from .semantic_contracts import SemanticClassifier, validate_semantic_evidence


Status = Literal[
    "EVALUATED", "SEMANTIC_ABSTENTION", "GRAMMAR_ABSTENTION",
    "NO_ACTIVE_MEMORY", "MEMORY_EVIDENCE_ABSTENTION",
]


@dataclass(frozen=True)
class GroundedEligibilityResult:
    """Read-only shadow observation, never a correction command.

    The result intentionally omits source quote, user text and Memory values.
    The memory ID/version are informational snapshots, not write authority.
    """
    status: Status
    source_turn_id: UUID
    reason: str
    classifier_id: str | None = None
    memory_id: UUID | None = None
    memory_version: int | None = None
    decision: EligibilityDecision | None = None


def _verified_selected(cur, actor: TrustedActor, instance_id: UUID,
                       generation: int) -> None:
    # We cannot use FOR SHARE in a READ ONLY transaction. Observations are
    # snapshots, fenced again on the second read; never action authority.
    cur.execute(
        "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
        (actor.user_id, instance_id),
    )
    if cur.fetchone() is None:
        raise PermissionDenied("persona outside authenticated user scope")
    cur.execute(
        """SELECT instance_id,generation,mode FROM persona_selection
           WHERE user_id=%s""",
        (actor.user_id,),
    )
    selected = cur.fetchone()
    if (selected is None or selected["instance_id"] != instance_id
            or selected["generation"] != generation
            or selected["mode"] != "FOREGROUND"):
        raise StaleSelection("eligibility requires selected Foreground generation")


def _current_memory(cur, actor: TrustedActor, instance_id: UUID,
                    kind: str, subject: str, predicate: str):
    """Retrieve exact normal-privacy ACTIVE slot and its actual source turn.

    Match conditions mirror Memory's user-facing recall restrictions, in
    particular suppression during newer unresolved extracted conflicts.
    """
    cur.execute(
        """SELECT m.memory_id,m.version,m.memory_kind,m.subject_key,
                  m.predicate_key,m.value_text,m.source_quote,m.source_turn_id,
                  t.input_text AS original_source_text
           FROM memory_records m
           JOIN conversation_turns t
             ON t.turn_id=m.source_turn_id
            AND t.conversation_id=m.source_conversation_id
            AND t.user_id=m.user_id AND t.instance_id=m.instance_id
           WHERE m.user_id=%s AND m.instance_id=%s
             AND m.memory_kind=%s AND m.subject_key=%s
             AND m.predicate_key=%s AND m.record_status='ACTIVE'
             AND m.privacy_class='NORMAL'
             AND (m.expires_at IS NULL OR m.expires_at>clock_timestamp())
             AND NOT EXISTS (
                 SELECT 1 FROM memory_extraction_candidates unresolved
                 JOIN conversation_turns newer_turn
                   ON newer_turn.turn_id=unresolved.source_turn_id
                  AND newer_turn.conversation_id=unresolved.conversation_id
                  AND newer_turn.user_id=unresolved.user_id
                  AND newer_turn.instance_id=unresolved.instance_id
                 WHERE unresolved.target_memory_id=m.memory_id
                   AND unresolved.user_id=m.user_id
                   AND unresolved.instance_id=m.instance_id
                   AND unresolved.status='CONFLICT'
                   AND newer_turn.created_at>m.source_turn_at
                   AND unresolved.value_text<>m.value_text
             )
           LIMIT 1""",
        (actor.user_id, instance_id, kind, subject, predicate),
    )
    return cur.fetchone()


def _source_metadata_matches(source) -> bool:
    """Do not invent temporal or context fields for an opaque Memory value.

    Only the narrow source-grounded grammar that originally admits a
    CURRENT preference can be lifted to the corresponding canonical slot.
    """
    quote = source["source_quote"]
    original = source["original_source_text"]
    if not isinstance(quote, str) or not isinstance(original, str):
        return False
    if quote.casefold() not in original.casefold():
        return False
    # A Memory admission only proves the quote occurs within its turn:
    # "I enjoy gardening" can be admitted from the unsupported sentence
    # "I enjoy gardening only during summer." Parsing the quote alone
    # would silently discard the time qualifier and fabricate CURRENT scope.
    # Both the complete authenticated turn and the stored quote must parse
    # to the same conservative, unqualified canonical assertion.
    full_claim = parse_explicit_claim(original)
    quoted_claim = parse_explicit_claim(quote)
    if full_claim is None or quoted_claim is None:
        return False
    return (
        full_claim.memory_kind == quoted_claim.memory_kind == source["memory_kind"]
        and full_claim.subject_key == quoted_claim.subject_key == source["subject_key"]
        and full_claim.predicate_key == quoted_claim.predicate_key == source["predicate_key"]
        and full_claim.value_text == quoted_claim.value_text == source["value_text"]
        and full_claim.predicate_key.startswith("enjoys_")
    )


def observe_grounded_memory_eligibility(
    connection: Connection, actor: TrustedActor, instance_id: UUID,
    generation: int, source_turn_id: UUID, *,
    classifier: SemanticClassifier | None = None,
) -> GroundedEligibilityResult:
    """Read one authenticated user turn; compare with one Memory-owned slot.

    1. Source input and selected Foreground state are read from PostgreSQL.
    2. Stage A classifier and deterministic key derivation run WITHOUT any
       transaction or lock.
    3. A new short READ ONLY snapshot rechecks selection and scopes the active
       Memory record and its provenance.
    4. Pure eligibility gate runs outside all transactions.

    This function must not be used as a write-authorization oracle. A later
    Memory owner action would need fresh generation/version/identity checks.
    """
    require_idle(connection)
    _require_actor(actor)
    if not isinstance(instance_id, UUID) or not isinstance(source_turn_id, UUID):
        raise TypeError("expected instance and source turn UUIDs")
    if type(generation) is not int or generation < 1:
        raise ValueError("expected positive selection generation")

    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with connection.cursor(row_factory=dict_row) as cur:
            _verified_selected(cur, actor, instance_id, generation)
            cur.execute(
                """SELECT input_text FROM conversation_turns
                   WHERE turn_id=%s AND user_id=%s AND instance_id=%s""",
                (source_turn_id, actor.user_id, instance_id),
            )
            source = cur.fetchone()
            if source is None:
                raise PermissionDenied("turn outside authenticated selected persona")

    text = source["input_text"]
    reference = RuleSemanticClassifier().classify(text, source_turn_id=source_turn_id)
    baseline = validate_semantic_evidence(
        reference, source_turn_id=source_turn_id, source_text=text,
    )
    proposed = validate_semantic_evidence(
        (classifier or RuleSemanticClassifier()).classify(text, source_turn_id=source_turn_id),
        source_turn_id=source_turn_id, source_text=text,
    )
    if not baseline.claims or not proposed.claims:
        return GroundedEligibilityResult(
            "SEMANTIC_ABSTENTION", source_turn_id, "NO_SUPPORTED_STAGE_A_CLAIM",
            proposed.classifier_id,
        )
    # Never grant the classifier free authority over correction intent, time,
    # modality or subject identity. This slice trusts only the reviewed exact
    # deterministic reference labels; other outputs are evaluated later.
    if (len(baseline.claims) != 1 or len(proposed.claims) != 1
            or proposed.claims[0] != baseline.claims[0]):
        return GroundedEligibilityResult(
            "SEMANTIC_ABSTENTION", source_turn_id, "UNVERIFIED_SEMANTIC_LABELS",
            proposed.classifier_id,
        )
    semantic = proposed.claims[0]
    parsed = parse_explicit_claim(semantic.evidence_quote)
    if (parsed is None or parsed.memory_kind != "PREFERENCE"
            or parsed.subject_key != "user"
            or not parsed.predicate_key.startswith("enjoys_")
            or semantic.topic_label != parsed.predicate_key.removeprefix("enjoys_")
            or semantic.statement_type != "PREFERENCE"
            or semantic.temporal_scope != "CURRENT"
            or semantic.modality != "ASSERTED"):
        return GroundedEligibilityResult(
            "GRAMMAR_ABSTENTION", source_turn_id, "CANONICAL_SLOT_NOT_GROUNDED",
            proposed.classifier_id,
        )

    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with connection.cursor(row_factory=dict_row) as cur:
            _verified_selected(cur, actor, instance_id, generation)
            existing = _current_memory(
                cur, actor, instance_id, parsed.memory_kind,
                parsed.subject_key, parsed.predicate_key,
            )

    if existing is None:
        return GroundedEligibilityResult(
            "NO_ACTIVE_MEMORY", source_turn_id, "NO_COMPARABLE_CURRENT_SLOT",
            proposed.classifier_id,
        )
    if not _source_metadata_matches(existing):
        return GroundedEligibilityResult(
            "MEMORY_EVIDENCE_ABSTENTION", source_turn_id, "UNVERIFIED_MEMORY_SOURCE",
            proposed.classifier_id,
        )

    scope = ClaimScope(
        subject_key=parsed.subject_key,
        predicate_key=parsed.predicate_key,
        context_key="general",  # Guaranteed only for this tightly anchored grammar.
        temporal_scope="CURRENT",
    )
    prior = PriorClaimSnapshot(
        user_id=actor.user_id, instance_id=instance_id,
        source_turn_id=existing["source_turn_id"],
        memory_kind=existing["memory_kind"], scope=scope,
        admission="ADMITTED_ASSERTED", memory_id=existing["memory_id"],
        memory_version=existing["version"],
    )
    new = NewClaim(
        user_id=actor.user_id, instance_id=instance_id,
        source_turn_id=source_turn_id, memory_kind=parsed.memory_kind,
        scope=scope, statement_type=semantic.statement_type,
        modality=semantic.modality, pragmatic_function=semantic.pragmatic_function,
        value_text=parsed.value_text,
    )
    decision = evaluate_conflict_eligibility(prior, new)
    return GroundedEligibilityResult(
        "EVALUATED", source_turn_id, decision.reason, proposed.classifier_id,
        existing["memory_id"], existing["version"], decision,
    )
