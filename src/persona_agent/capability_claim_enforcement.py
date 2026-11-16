"""MVP-01F.4: bounded, deterministic enforcement of unavailable host claims.

The direct Tool Gateway has no shell, remote-server mutation, or data-deletion
capability. A registered tool would not by itself grant execution permission.
This guard protects conversational claims, not tool admission.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence


_REMOTE_TOPIC = re.compile(
    r"\b(?:nginx|tls|ssl|certificate|server|ssh|shell|terminal|sudo|"
    r"bash|script|command(?:s)?|openssl|deploy(?:ment)?|install(?:ation)?)\b",
    re.I,
)
_REQUEST = re.compile(
    r"^(?:(?:please|hey|so|okay|ok)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you(?:\s+please)?|"
    r"(?:please\s+)?(?:go ahead and|just))\s+"
    r"(?P<action>run|execute|install|deploy|configure|delete|erase|remove)\b"
    r"(?P<object>[^\n]{0,160})[?.!]?\s*$",
    re.I,
)
_HYPOTHETICAL_DELETION = re.compile(
    r"^what\s+if\s+(?:i|we)\s+(?:asked|requested|wanted)\s+"
    r"(?:you\s+to\s+|that\s+you\s+)"
    r"(?:delete|erase|remove)\s+(?:my|our|the)\s+"
    r"(?:data|account|files|records)[?.!]?\s*$",
    re.I,
)
_DELETION_OBJECT = re.compile(
    r"\b(?:my|your|our|user|all|the)\s+(?:data|account|files|records)\b", re.I,
)
# The requested operation is determined by its direct object, not by
# incidental nouns in the data being analyzed. "Run a word count on this
# code" asks for statistics; "run this code" asks for code execution.
# This intentionally abstains on unfamiliar or ambiguous constructions.
_REMOTE_OBJECT = re.compile(
    r"^\s*(?:(?:a|an|the|this|that|these|those|my|your|our)\s+){0,3}"
    r"(?:command(?:s)?|script(?:s)?|code|nginx|server|ssh|shell|"
    r"terminal|sudo|openssl|certificate|software|app(?:lication)?)\b",
    re.I,
)
_ANAPHORA = re.compile(
    r"^\s*(?:this|that|these|those|it)(?:\s+for\s+me)?\s*[?.!]?\s*$", re.I,
)
# Model promises/offers to carry out commands; excludes "I can't run" and
# "I can help you run" (guidance, not personal execution).
_OFFER = re.compile(
    r"\b(?:"
    r"(?:(?:i|we)\s+(?:(?:can|could|will|would|shall|might)\s+|"
    r"(?:am|are)\s+able\s+to\s+)"
    r"|(?:i|we)(?:'ll|’ll)\s*)"
    r"|(?:would|do)\s+you\s+(?:like|want)\s+(?:me|us)\s+to\s+"
    r"|(?:should|shall)\s+(?:i|we)\s+"
    r"|let\s+(?:me|us)\s+"
    r")(?P<action>run|execute|install|deploy|configure|delete|erase|remove)\b"
    r"(?P<object>[^.!?\n]{0,110})",
    re.I,
)


@dataclass(frozen=True)
class CapabilityEnforcement:
    text: str
    reason: str | None = None
    policy_revision: str = "mvp01f4.1"


def _category(action: str, obj: str, *, remote_context: bool) -> str | None:
    if action.lower() in ("delete", "erase", "remove"):
        return "data_deletion" if _DELETION_OBJECT.search(obj) else None
    if _REMOTE_OBJECT.search(obj):
        return "remote"
    if _ANAPHORA.fullmatch(obj) and remote_context:
        return "remote"
    return None


def _fallback(category: str) -> str:
    if category == "data_deletion":
        return (
            "I can't delete your data through this host; no data-deletion "
            "capability is available. I can explain the available steps, "
            "but I haven't deleted anything."
        )
    return (
        "I can't run commands or make changes on your server from this "
        "host. I can walk you through the steps and help interpret the "
        "results, but I haven't executed anything."
    )


def enforce_capability_claims(
    user_text: str, model_text: str, *, recent_user_texts: Sequence[str] = (),
) -> CapabilityEnforcement:
    """Apply narrow checks before response persistence; never issue operations.

    A clear request for an unavailable operation is handled irrespective of
    model compliance. Other replies are changed only if they offer a matching
    unauthorized action. Routine technical guidance is preserved.

    A future executable tool requires a reviewed, capability-specific policy
    and authorization contract, not simply bypassing these checks.
    """
    # Resolve deictic requests ("run this") only against the immediately
    # preceding user topic. Older Nginx context must not shadow a newer
    # read-only request (e.g. word statistics).
    prior_user_text = recent_user_texts[-1] if recent_user_texts else ""
    remote_context = bool(_REMOTE_TOPIC.search(prior_user_text))
    cleaned = user_text.strip()
    if not cleaned.startswith(('"', "'", "“", "‘")):
        # A hypothetical is not a deletion command. Nevertheless, it must
        # never receive a promise that this host can perform the deletion.
        if _HYPOTHETICAL_DELETION.fullmatch(cleaned):
            return CapabilityEnforcement(
                _fallback("data_deletion"), "unsupported_hypothetical:data_deletion",
            )
        request = _REQUEST.fullmatch(cleaned)
        if request:
            category = _category(
                request.group("action"), request.group("object"),
                remote_context=remote_context,
            )
            if category:
                return CapabilityEnforcement(
                    _fallback(category), "unsupported_request:" + category,
                )

    for match in _OFFER.finditer(model_text):
        category = _category(
            match.group("action"), match.group("object"),
            remote_context=remote_context or bool(_REMOTE_TOPIC.search(cleaned)),
        )
        if category:
            return CapabilityEnforcement(
                _fallback(category), "unsupported_offer:" + category,
            )
    return CapabilityEnforcement(model_text)
