"""Memory-owned admission checks, distinct from historical recall."""
from .memory_read_policy import capture_historical_read_fence
from .conversation_contracts import StaleHistoricalContext


def admission_source_current(cur,user_id,instance_id,conversation_id,turn_id):
    cur.execute("SELECT extraction_source_current(%s,%s,%s,%s) AS eligible",
                (user_id,instance_id,conversation_id,turn_id))
    return bool(cur.fetchone()["eligible"])


def verify_admission_snapshot(cur,user_id,instance_id,fence):
    # This transaction may itself have Memory writes with deferred revision
    # triggers. Take an exclusive late fence, not a SHARE-to-UPDATE upgrade.
    cur.execute("""SELECT revision,clock_timestamp() AS now FROM historical_recall_revisions
                   WHERE user_id=%s AND instance_id=%s FOR UPDATE""",(user_id,instance_id))
    row=cur.fetchone()
    if (row is None or row["revision"]!=fence.revision or
        (fence.next_expiry is not None and row["now"]>=fence.next_expiry)):
        raise StaleHistoricalContext("Memory candidate evidence changed during admission")
