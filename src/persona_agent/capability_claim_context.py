"""Bounded capability-truth context for *claims* about executing user actions.

This is a read-only description of currently registered direct tool adapters.
It never selects, enables, installs or authorizes an operation. Procedure
execution has separate Task/Run/Step permission and is not a generic shell.
"""
import re

from .tool_contracts import CAPABILITIES


_EXECUTION_CLAIM = re.compile(
    r"\b(?:can|could|would|will)\s+you\s+"
    r"(?:run|execute|install|configure|modify|delete|remove|deploy|"
    r"do\s+this\s+for\s+me)\b|"
    r"\b(?:run|execute|deploy|delete)\s+(?:this|these|it)\s+for\s+me\b|"
    r"\b(?:delete|erase|remove)\s+(?:my|our)\s+data\b",
    re.I,
)


def capability_claim_context(text: str) -> str:
    """Add a factual constraint only to turns likely to prompt action claims.

    The model still generates the natural reply; command admission continues
    exclusively through the existing trusted Tool Gateway.
    """
    if not _EXECUTION_CLAIM.search(text):
        return ""
    descriptions=", ".join(
        f"{name} ({item.effect_class.lower()})"
        for name,item in sorted(CAPABILITIES.items())
    )
    return (
        "Trusted host capability boundary: registered direct tools are "
        f"{descriptions}. No shell, SSH, remote-server configuration, "
        "arbitrary code execution, or user-data deletion tool is registered "
        "in this host. Do not claim, promise, or offer to perform any such "
        "operation, including conditionally. When asked, explain that you "
        "can provide instructions but cannot execute them here. A "
        "hypothetical deletion question is not a deletion request and "
        "does not establish a deletion workflow. Tool availability is not "
        "permission to execute, and prior assistant claims are not evidence "
        "of completed actions.\n"
    )
