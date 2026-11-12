"""P2-07 common durable publication primitive, invoked by each domain owner.

This is infrastructure only; it does not arbitrate cross-domain state.
All callers own a short transaction and use P0's transactional outbox.
"""
import json
from uuid import UUID,NAMESPACE_URL,uuid4,uuid5


def write_domain_event(
    cur,*,user_id:UUID,instance_id:UUID,domain:str,event_type:str,
    source_id:str,trust_level:str,payload:dict,
    causation_id:UUID|None=None,correlation_id:UUID|None=None,
) -> UUID:
    if domain not in ("emotion","relationship"):
        raise ValueError("P2-07 event domain must be emotion or relationship")
    stream_id=uuid5(
        NAMESPACE_URL,f"persona-agent/{domain}/{user_id}/{instance_id}",
    )
    cur.execute(
        """INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
           VALUES (%s,%s,%s,%s) ON CONFLICT(stream_id) DO NOTHING""",
        (stream_id,user_id,instance_id,domain),
    )
    cur.execute(
        """UPDATE event_streams SET last_sequence=last_sequence+1
           WHERE stream_id=%s AND user_id=%s AND instance_id=%s
           RETURNING last_sequence""",
        (stream_id,user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None:
        raise ValueError("event stream is outside actor scope")
    event_id=uuid4()
    cur.execute(
        """INSERT INTO events(
             event_id,event_type,schema_version,user_id,instance_id,
             stream_id,stream_sequence,source_id,trust_level,
             causation_id,correlation_id,payload
           ) VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
        (event_id,event_type,user_id,instance_id,stream_id,
         row["last_sequence"],source_id,trust_level,
         causation_id,correlation_id,json.dumps(payload)),
    )
    cur.execute(
        "INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",
        (uuid4(),event_id),
    )
    return event_id
