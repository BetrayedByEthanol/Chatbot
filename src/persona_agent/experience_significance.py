"""Memory-owned, explicit significance metadata; not inferred user modeling.

A reported important experience is neither verified fact nor permission to
retain indefinitely. Attention ages independently of source retention.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg.rows import dict_row
from pydantic import BaseModel,ConfigDict

from .contracts import TrustedActor
from .operation_contracts import require_idle
from .runtime import PermissionDenied

_PREFIX="this experience is important to me: "


def explicit_experience_marker(text:str)->bool:
    """Narrow affirmative standalone marker; abstain on ambiguous language."""
    if len(text)>600:
        return False
    value=text.strip()
    if not value.casefold().startswith(_PREFIX) or not value[len(_PREFIX):].strip():
        return False
    from .historical_user_turns import _SENSITIVE
    return not _SENSITIVE.search(value)


def record_experience_marker(cur,*,user_id,instance_id,conversation_id,turn_id,event_id,text):
    """Live authenticated text ingress only, inside existing event transaction."""
    if not explicit_experience_marker(text):
        return
    cur.execute("""INSERT INTO memory_experience_markers(
                      source_turn_id,user_id,instance_id,conversation_id,source_event_id,
                      marked_at,attention_until)
                   SELECT t.turn_id,t.user_id,t.instance_id,t.conversation_id,e.event_id,
                          t.created_at,t.created_at+interval '168 hours'
                   FROM conversation_turns t JOIN events e
                     ON e.event_id=t.source_input_event_id AND e.user_id=t.user_id
                     AND e.instance_id=t.instance_id
                   WHERE t.turn_id=%s AND t.user_id=%s AND t.instance_id=%s AND t.conversation_id=%s
                     AND e.event_id=%s AND e.event_type='conversation.user_text.received.v1'
                     AND e.trust_level='AUTHENTICATED_USER' AND NOT (e.payload ? 'input_modality')""",
                (turn_id,user_id,instance_id,conversation_id,event_id))


class ExperienceSignificance(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    source_turn_id:UUID
    conversation_id:UUID
    source_event_id:UUID
    marked_at:datetime
    attention_until:datetime
    significance:Literal["USER_MARKED_IMPORTANT"]
    attention:Literal["ACTIVE","AGED"]
    evidence_class:Literal["USER_REPORTED"]="USER_REPORTED"


def list_experience_significance(connection,actor,instance_id,*,active_only=False,limit=20):
    """Authorized metadata read, independent of summary/episode selection.

    Source withdrawal suppresses the marker completely in effective reads.
    Age only changes attention; it does not change facts or delete records.
    No unrestricted conversation-app capability or background scan.
    """
    require_idle(connection)
    if type(actor) is not TrustedActor or actor.application_session_id is not None:
        raise PermissionDenied("experience inspection requires trusted local authenticated user")
    if not 1<=limit<=100:
        raise ValueError("invalid significance limit")
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                    (actor.user_id,instance_id))
        if cur.fetchone() is None:
            raise PermissionDenied("experience outside authenticated scope")
        cur.execute("""SELECT m.source_turn_id,m.conversation_id,m.source_event_id,m.marked_at,
                              m.attention_until,m.significance,
                              CASE WHEN m.attention_until>statement_timestamp() THEN 'ACTIVE'
                                   ELSE 'AGED' END AS attention
                       FROM memory_experience_markers m
                       WHERE m.user_id=%s AND m.instance_id=%s
                         AND (NOT %s OR m.attention_until>statement_timestamp())
                         AND workflow_source_current(m.user_id,m.instance_id,
                                                     m.conversation_id,m.source_turn_id,NULL)
                       ORDER BY m.marked_at DESC,m.source_turn_id LIMIT %s""",
                    (actor.user_id,instance_id,active_only,limit))
        return tuple(ExperienceSignificance.model_validate(row) for row in cur.fetchall())
