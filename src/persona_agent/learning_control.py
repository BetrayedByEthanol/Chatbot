"""Authorization/Governance owner of predictive-learning control.

The selected-persona row is the existing authority gate: control changes
take UPDATE, authenticated ingress takes SHARE. A reset is a causal cut
across all conversations, not a local sequence or wall-clock comparison.
No learning content or inference is owned here.
"""
import json
from uuid import NAMESPACE_URL,uuid4,uuid5

from psycopg.rows import dict_row

from .contracts import TrustedActor
from .learning_control_contracts import (
    LearningControl,LearningControlResult,SetLearningEnabled,ResetLearnedState,
)
from .operation_contracts import require_idle
from .runtime import PermissionDenied,StaleSelection,VersionConflict,IdempotencyConflict,_fingerprint


def _actor(actor):
    if type(actor) is not TrustedActor:
        raise PermissionDenied("learning control requires authenticated user")


def _owned(cur,actor,instance_id):
    cur.execute("SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,instance_id))
    if cur.fetchone() is None:
        raise PermissionDenied("learning control outside authenticated scope")


def _read(cur,user_id,instance_id):
    cur.execute("""SELECT * FROM predictive_learning_controls
                   WHERE user_id=%s AND instance_id=%s""",(user_id,instance_id))
    row=cur.fetchone()
    return LearningControl.model_validate(row) if row else LearningControl(
        user_id=user_id,instance_id=instance_id)


def get_learning_control(connection,actor,instance_id):
    require_idle(connection)
    _actor(actor)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _owned(cur,actor,instance_id)
        if actor.application_session_id is not None:
            cur.execute("SELECT instance_id,generation,mode FROM persona_selection "
                        "WHERE user_id=%s FOR SHARE",(actor.user_id,))
            selection=cur.fetchone()
            if selection is None or selection["instance_id"]!=instance_id or selection["mode"]!="FOREGROUND":
                raise PermissionDenied("learning control inspection outside application scope")
            from .application_auth import check_application_actor
            check_application_actor(cur,actor,instance_id,selection["generation"])
        return _read(cur,actor.user_id,instance_id)


def _change(connection,actor,command,*,reset):
    require_idle(connection)
    _actor(actor)
    # 07A sessions are scoped conversation capabilities, not a Governance
    # management grant. Until a management ingress is authorized, only
    # the existing trusted local authenticated user command boundary may write.
    if actor.application_session_id is not None:
        raise PermissionDenied("application session has no learning management authority")
    kind="governance.learning.reset.v1" if reset else "governance.learning.set_enabled.v1"
    fingerprint=_fingerprint(command)
    with connection.transaction(),connection.cursor(row_factory=dict_row) as cur:
        _owned(cur,actor,command.instance_id)
        cur.execute("""SELECT instance_id,generation,mode FROM persona_selection
                       WHERE user_id=%s FOR UPDATE""",(actor.user_id,))
        selection=cur.fetchone()
        if (selection is None or selection["instance_id"]!=command.instance_id
            or selection["generation"]!=command.expected_generation
            or selection["mode"]!="FOREGROUND"):
            raise StaleSelection("learning management requires selected foreground generation")
        receipt=uuid4()
        cur.execute("""INSERT INTO command_receipts(
                       receipt_id,user_id,instance_id,command_type,idempotency_key,request_fingerprint)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                       DO NOTHING RETURNING receipt_id""",
                    (receipt,actor.user_id,command.instance_id,kind,command.idempotency_key,fingerprint))
        if cur.fetchone() is None:
            cur.execute("""SELECT request_fingerprint,result_json FROM command_receipts
                           WHERE user_id=%s AND instance_id=%s AND command_type=%s AND idempotency_key=%s""",
                        (actor.user_id,command.instance_id,kind,command.idempotency_key))
            prior=cur.fetchone()
            if prior is None or prior["request_fingerprint"]!=fingerprint or prior["result_json"] is None:
                raise IdempotencyConflict("learning command key reused")
            return LearningControlResult.model_validate(prior["result_json"]).model_copy(
                update={"replayed":True})
        prior=_read(cur,actor.user_id,command.instance_id)
        if prior.version!=command.expected_version:
            raise VersionConflict("learning control revision changed")
        enabled=prior.learning_enabled if reset else command.enabled
        changed=reset or enabled!=prior.learning_enabled
        event_id=None
        current=prior
        if changed:
            generation=prior.reset_generation+int(reset)
            version=prior.version+1
            stream=uuid5(NAMESPACE_URL,f"persona-agent/{command.instance_id}/governance.learning")
            cur.execute("""INSERT INTO event_streams(stream_id,user_id,instance_id,domain)
                           VALUES (%s,%s,%s,'governance.learning') ON CONFLICT DO NOTHING""",
                        (stream,actor.user_id,command.instance_id))
            cur.execute("""UPDATE event_streams SET last_sequence=last_sequence+1
                           WHERE stream_id=%s AND user_id=%s AND instance_id=%s RETURNING last_sequence""",
                        (stream,actor.user_id,command.instance_id))
            seq=cur.fetchone()["last_sequence"]
            event_id=uuid4()
            boundary=event_id if reset else prior.reset_boundary_ref
            payload={"learning_enabled":enabled,"previous_version":prior.version,"version":version,
                     "reset_generation":generation,"reset_boundary_ref":str(boundary) if boundary else None}
            cur.execute("""INSERT INTO events(event_id,event_type,schema_version,user_id,instance_id,
                           stream_id,stream_sequence,source_id,trust_level,correlation_id,payload,idempotency_key)
                           VALUES (%s,%s,1,%s,%s,%s,%s,%s,'AUTHENTICATED_USER',%s,%s::jsonb,%s)""",
                        (event_id,"governance.learning.reset.v1" if reset else "governance.learning.enabled_changed.v1",
                         actor.user_id,command.instance_id,stream,seq,actor.source_id,receipt,
                         json.dumps(payload),command.idempotency_key))
            cur.execute("""INSERT INTO predictive_learning_controls(user_id,instance_id,learning_enabled,
                           version,reset_generation,reset_boundary_ref,source_event_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(user_id,instance_id) DO UPDATE SET
                             learning_enabled=EXCLUDED.learning_enabled,version=EXCLUDED.version,
                             reset_generation=EXCLUDED.reset_generation,reset_boundary_ref=EXCLUDED.reset_boundary_ref,
                             source_event_id=EXCLUDED.source_event_id,updated_at=clock_timestamp()
                           RETURNING *""",
                        (actor.user_id,command.instance_id,enabled,version,generation,boundary,event_id))
            current=LearningControl.model_validate(cur.fetchone())
            cur.execute("INSERT INTO outbox(outbox_id,event_id) VALUES (%s,%s)",(uuid4(),event_id))
        result=LearningControlResult(control=current,changed=changed)
        cur.execute("UPDATE command_receipts SET result_json=%s::jsonb,event_id=%s WHERE receipt_id=%s",
                    (result.model_dump_json(),event_id,receipt))
        return result


def set_learning_enabled(connection,actor,command:SetLearningEnabled):
    if type(command) is not SetLearningEnabled:
        raise TypeError("expected SetLearningEnabled")
    return _change(connection,actor,command,reset=False)


def reset_learned_state(connection,actor,command:ResetLearnedState):
    if type(command) is not ResetLearnedState:
        raise TypeError("expected ResetLearnedState")
    return _change(connection,actor,command,reset=True)


def stamp_learning_input(cur,*,user_id,instance_id,turn_id,event_id):
    """Called only by live authenticated text ingress under selection SHARE.

    Missing or disabled control creates no stamp. Never call this on replay,
    historical data, assistant text or derived sources.
    """
    cur.execute("""INSERT INTO predictive_input_admissions(
                     turn_id,user_id,instance_id,source_event_id,admitted_control_version,reset_generation)
                   SELECT %s,c.user_id,c.instance_id,%s,c.version,c.reset_generation
                   FROM predictive_learning_controls c
                   WHERE c.user_id=%s AND c.instance_id=%s AND c.learning_enabled""",
                (turn_id,event_id,user_id,instance_id))
