"""Reusable bounded admission, not a predictor or learned-content store."""
from uuid import UUID

from .learning_control_contracts import PredictiveFence
from .conversation_contracts import HistoricalReadFence,StaleHistoricalContext
from .memory_read_policy import capture_historical_read_fence,require_historical_read_fence


def _ids(source_turn_ids):
    ids=tuple(source_turn_ids)
    if not 1<=len(ids)<=128 or any(type(i) is not UUID for i in ids) or len(set(ids))!=len(ids):
        raise ValueError("predictive sources require 1..128 distinct exact raw turn IDs")
    return ids


def capture_predictive_fence(cur,user_id,instance_id,source_turn_ids):
    """Optional read: no control or any missing/stale dependency -> cold start.

    The owning path supplies scope and the complete dependency set from its
    own authenticated context. This helper grants neither authority nor
    verification of what a user reported. No row locks over inference.
    """
    ids=_ids(source_turn_ids)
    cur.execute("""SELECT version,reset_generation FROM predictive_learning_controls
                   WHERE user_id=%s AND instance_id=%s AND learning_enabled""",
                (user_id,instance_id))
    control=cur.fetchone()
    if control is None:
        return None
    historical=capture_historical_read_fence(cur,user_id,instance_id)
    cur.execute("SELECT predictive_fence_current(%s,%s,%s,%s,%s::uuid[]) AS eligible",
                (user_id,instance_id,control["version"],control["reset_generation"],list(ids)))
    if not cur.fetchone()["eligible"]:
        return None
    return PredictiveFence(control["version"],control["reset_generation"],ids,
                           historical.revision,historical.next_expiry)


def require_predictive_fence(cur,user_id,instance_id,fence:PredictiveFence):
    """Late owner/controlled-output check. Caller owns selection/lease and
    owner locks first. Memory SHARE precedes control SHARE. Governance
    transitions hold selection UPDATE but never take Memory/owner locks.
    Replay of an old version after disable/re-enable must fail.
    """
    ids=_ids(fence.source_turn_ids)
    require_historical_read_fence(cur,user_id,instance_id,
        HistoricalReadFence(fence.historical_revision,fence.next_expiry))
    cur.execute("""SELECT version FROM predictive_learning_controls
                   WHERE user_id=%s AND instance_id=%s FOR SHARE""",(user_id,instance_id))
    if cur.fetchone() is None:
        raise StaleHistoricalContext("predictive learning control unavailable")
    cur.execute("SELECT predictive_fence_current(%s,%s,%s,%s,%s::uuid[]) AS eligible",
                (user_id,instance_id,fence.control_version,fence.reset_generation,list(ids)))
    if not cur.fetchone()["eligible"]:
        raise StaleHistoricalContext("predictive learning authority or source changed")
