"""MVP-01F.3: small, source-scoped grounding for recollection and summaries.

Conversation owns this evidence view. It does not retrieve historical memory,
replace memory ownership, change dialogue focus, or perform inference.
"""
import json
import re
from typing import Sequence

from .memory_retrieval import is_historical_recall_query
from .intent_interpretation import explicit_unresolved_named_topic

_SUMMARIZE = re.compile(
    r"\b(?:summari[sz]e|recap|summary of|sum up)\b",re.I,
)
_WORDS = re.compile(r"[a-z]{4,}",re.I)
_STOP = frozenset({
    "what","about","that","this","have","with","would","could",
    "your","from","earlier","discussed","discussion","remember",
    "previous","before","again","please","summarize","recap",
    "talked","what","time","when","where",
})


def grounding_mode(text: str) -> str | None:
    """Select grounding only for actual recollection/summary requests."""
    value=text.strip()
    if not value or len(value)>500:
        return None
    if _SUMMARIZE.search(value):
        return "SUMMARY"
    if is_historical_recall_query(value):
        return "RECALL"
    if explicit_unresolved_named_topic(value) is not None:
        return "NAMED_DISCUSSION"
    return None


def grounding_context_block(
    latest_text: str, prior_user_turns: Sequence[str], *, max_chars: int,
) -> str:
    """Return source-attributed user statements without assistant assertions.

    Absence of evidence in the bounded view is NOT proof that no older
    conversation or Memory-domain record exists. The prompt must never make
    unsupported recollection claims when no supporting source is supplied.
    """
    mode=grounding_mode(latest_text)
    if mode is None or max_chars<260:
        return ""
    if mode=="NAMED_DISCUSSION":
        # A named plan may be a newly discussed subject, not retrieved
        # historical evidence. Only a separate linked source can support
        # specific claims about an earlier plan.
        note=(
            "Named discussion request: discuss the subject the user named. "
            "Do not assert you remember details of an earlier plan unless "
            "source-linked user evidence is actually supplied in this "
            "model context. If missing, ask what the user wants to do "
            "with the plan. A topic switch does not approve a Goal.\n"
        )
        return note if len(note)<=max_chars else ""
    if mode=="RECALL":
        keywords={
            word for word in _WORDS.findall(latest_text.casefold())
            if word not in _STOP
        }
        matched=[
            t for t in prior_user_turns
            if keywords and any(
                re.search(r"\b"+re.escape(k)+r"\b",t,re.I)
                for k in keywords
            )
        ]
        relevant=matched[-4:]
    else:
        relevant=list(prior_user_turns)[-10:]

    guidance=(
        "Conversation grounding (source: exact user messages in THIS "
        "conversation, not memories of prior sessions). "
        "Prior assistant messages are NOT independent evidence of user "
        "history. Never present inferred or assistant-proposed details "
        "as something the user said or as remembered fact. "
    )
    if mode=="RECALL":
        guidance+=(
            "Only claim recollection supported by the quoted user turns "
            "or explicitly supplied retrieved, source-linked memory. "
            "If none supports the requested past discussion, say you "
            "cannot reliably recall its details and ask for a reminder. "
        )
    else:
        guidance+=(
            "Summarize source-supported user statements faithfully, "
            "preserve corrections and temporal distinctions (yesterday "
            "versus today), and label any suggested next step as advice. "
            "Omit claims unsupported by user statements or verified results. "
        )
    header=guidance+"\nUser statements in accessible current conversation: "
    if not relevant:
        result=header+"none matching the requested earlier discussion.\n"
        return result if len(result)<=max_chars else ""
    # Keep the most recent evidence, respecting source order and budget.
    usable=[]
    for quote in reversed(relevant):
        candidate=[quote[:240],*usable]
        rendered=header+json.dumps(candidate,ensure_ascii=False)+"\n"
        if len(rendered)>max_chars:
            break
        usable=candidate
    if not usable:
        result=header+"not enough space to include an exact quotation.\n"
        return result if len(result)<=max_chars else ""
    return header+json.dumps(usable,ensure_ascii=False)+"\n"
