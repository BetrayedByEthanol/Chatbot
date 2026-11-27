"""Scope-first source validation and reappraisal of existing owner projections.

A stored watermark is a provenance stamp, not a second authoritative store.
Foreground reads never rebuild an unbounded mood history.
"""
from datetime import datetime,timezone

from .conversation_contracts import HistoricalReadFence
from .raw_context import eligible_turn_ids

FOREGROUND_APPRAISAL_LIMIT=128


def source_current(cur,user_id,instance_id,conversation_id,turn_id):
    return turn_id in eligible_turn_ids(cur,user_id,instance_id,conversation_id,[turn_id])


def current_sources(cur,user_id,instance_id,rows):
    """Batch exact candidate IDs by conversation; never search other scopes."""
    groups={}
    for row in rows:
        groups.setdefault(row["conversation_id"],set()).add(row["source_turn_id"])
    eligible=set()
    for cid,ids in groups.items():
        ordered=sorted(ids)
        for start in range(0,len(ordered),FOREGROUND_APPRAISAL_LIMIT):
            eligible.update(eligible_turn_ids(cur,user_id,instance_id,cid,
                ordered[start:start+FOREGROUND_APPRAISAL_LIMIT]))
    return eligible


def projection_current(state,fence):
    if state is None or state["evidence_revision"]!=fence.revision:
        return False
    expiry=state["evidence_next_expiry"]
    return expiry is None or expiry>datetime.now(timezone.utc)


def merge_read_fences(earlier,later):
    """Keep the first observed revision and earliest time boundary."""
    if earlier is None:
        return later
    if later is None:
        return earlier
    expiries=[x for x in (earlier.next_expiry,later.next_expiry) if x is not None]
    return HistoricalReadFence(earlier.revision,min(expiries) if expiries else None)


def fence_with_expiry(fence,expiry):
    if expiry is None:
        return fence
    return merge_read_fences(fence,HistoricalReadFence(fence.revision,expiry))
