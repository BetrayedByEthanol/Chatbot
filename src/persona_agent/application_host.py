"""MVP-07A authenticated local text application.

No HTTP listener, raw domain-command dispatcher, platform/worker callback,
external effects or client-controlled provider settings. Explicit client ACK
is a statement of accepted delivery, not proof of user perception.
"""
import hashlib
import json

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from .application_auth import authenticate_application
from .application_contracts import ApplicationEnvelope
from .conversation import _selection,ingest_text,acknowledge_text_delivery
from .conversation_contracts import TextInputCommand,StaleConversation,ModelUnavailable,InvalidatedResponse
from .lifecycle import PrimaryActivityCommand,record_primary_activity
from .response_decisions import decide_response
from .response_orchestrator import respond_to_user_text
from .runtime import PermissionDenied,StaleSelection,IdempotencyConflict

MAX_REQUEST_BYTES=65536


def _scope(cur,identity):
    _selection(cur,identity.actor,identity.instance_id,identity.generation,identity.conversation_id)


def _turn(cur,identity,turn_id):
    cur.execute(
        """SELECT input_text FROM conversation_turns WHERE turn_id=%s AND user_id=%s
           AND instance_id=%s AND conversation_id=%s AND selection_generation=%s""",
        (turn_id,identity.user_id,identity.instance_id,identity.conversation_id,identity.generation),
    )
    row=cur.fetchone()
    if row is None:
        raise PermissionDenied("turn unavailable")
    return row


def _request_key(identity,action,key):
    # Fixed-length, domain-separated key. A renewed credential for the same
    # conversation and selection retains the original message retry identity.
    scope=f"{identity.conversation_id}:{identity.generation}:{action}:"
    return "application:"+hashlib.sha256((scope+key).encode("utf-8")).hexdigest()


def _allowed_route(text):
    if decide_response(text).route=="TEXT_ANALYSIS":
        raise PermissionDenied("asynchronous capability not enabled for this application")


def _offer(connection,identity,reply):
    """Persist correlation before returning bytes; never auto-ACK a write."""
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _scope(cur,identity)
            cur.execute(
                """SELECT c.last_input_turn_id FROM conversations c
                   WHERE c.conversation_id=%s AND c.user_id=%s AND c.instance_id=%s FOR SHARE""",
                (identity.conversation_id,identity.user_id,identity.instance_id),
            )
            conversation=cur.fetchone()
            cur.execute(
                """SELECT response_id,turn_id,model_id,response_text,delivery_status
                   FROM conversation_responses WHERE response_id=%s AND turn_id=%s
                   AND user_id=%s AND instance_id=%s AND conversation_id=%s
                   AND selection_generation=%s""",
                (reply.response_id,reply.turn_id,identity.user_id,identity.instance_id,
                 identity.conversation_id,identity.generation),
            )
            row=cur.fetchone()
            if row is None or conversation is None:
                raise PermissionDenied("response unavailable")
            if row["delivery_status"]=="GENERATED" and conversation["last_input_turn_id"]!=row["turn_id"]:
                raise StaleConversation("generated response superseded")
            from .response_lineage import require_response_lineage
            require_response_lineage(cur,identity.user_id,identity.instance_id,row["response_id"])
            cur.execute(
                """INSERT INTO application_delivery_offers(session_id,response_id,user_id,instance_id,
                       conversation_id,selection_generation) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(session_id,response_id) DO NOTHING""",
                (identity.session_id,row["response_id"],identity.user_id,identity.instance_id,
                 identity.conversation_id,identity.generation),
            )
            return {"type":"reply","response_id":str(row["response_id"]),"turn_id":str(row["turn_id"]),
                    "text":row["response_text"],"model_id":row["model_id"],
                    "delivery_status":row["delivery_status"],"replayed":reply.replayed}


def _status(connection,identity):
    """Reconnect metadata only; no raw worker input/output, leases or errors."""
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            _scope(cur,identity)
            cur.execute(
                """SELECT t.turn_id,t.status,r.response_id,r.delivery_status,
                          CASE WHEN r.response_id IS NULL THEN NULL
                               WHEN NOT EXISTS (SELECT 1 FROM conversation_response_lineage l
                                   WHERE l.response_id=r.response_id AND l.user_id=r.user_id
                                     AND l.instance_id=r.instance_id) THEN 'UNTRACKED'
                               WHEN response_lineage_current(r.response_id,r.user_id,r.instance_id)
                               THEN 'CURRENT' ELSE 'INVALIDATED' END AS source_status
                   FROM conversations c JOIN conversation_turns t ON t.turn_id=c.last_input_turn_id
                   LEFT JOIN conversation_responses r ON r.turn_id=t.turn_id
                     AND r.user_id=t.user_id AND r.instance_id=t.instance_id
                     AND r.conversation_id=t.conversation_id
                   WHERE c.conversation_id=%s AND c.user_id=%s AND c.instance_id=%s
                     AND t.selection_generation=%s""",
                (identity.conversation_id,identity.user_id,identity.instance_id,identity.generation),
            )
            latest=cur.fetchone()
            cur.execute(
                """SELECT t.operation_id,o.status,t.followup_state
                   FROM conversation_tool_calls t JOIN operations o ON o.operation_id=t.operation_id
                     AND o.user_id=t.user_id AND o.instance_id=t.instance_id
                   WHERE t.conversation_id=%s AND t.user_id=%s AND t.instance_id=%s
                     AND t.selection_generation=%s AND o.selection_generation=%s
                   ORDER BY t.created_at DESC,t.tool_call_id LIMIT 33""",
                (identity.conversation_id,identity.user_id,identity.instance_id,
                 identity.generation,identity.generation),
            )
            rows=cur.fetchall()
            def public(row):
                return {k:str(v) if k.endswith("_id") and v is not None else v for k,v in row.items()}
            return {"type":"status","latest":public(latest) if latest else None,
                    "operations":[public(row) for row in rows[:32]],"truncated":len(rows)>32}


class AuthenticatedTextApplication:
    """Server-configured provider; one fresh autocommit connection per request."""
    def __init__(self,dsn,gateway):
        self._dsn=dsn
        self._gateway=gateway

    def handle(self,raw):
        """Strict JSON bytes, uniform sanitized failures, no credential logging."""
        if not isinstance(raw,bytes) or len(raw)>MAX_REQUEST_BYTES:
            return {"type":"error","code":"INVALID_REQUEST"}
        try:
            envelope=ApplicationEnvelope.model_validate_json(raw)
        except (ValidationError,ValueError):
            return {"type":"error","code":"INVALID_REQUEST"}
        try:
            with psycopg.connect(self._dsn,autocommit=True) as conn:
                identity=authenticate_application(conn,envelope.token.get_secret_value())
                request=envelope.request
                actor=identity.actor
                if request.action=="status":
                    return _status(conn,identity)
                if request.action=="presence":
                    result=record_primary_activity(conn,actor,PrimaryActivityCommand(
                        instance_id=identity.instance_id,expected_generation=identity.generation,
                        idempotency_key=_request_key(identity,"presence",request.idempotency_key),
                    ))
                    return {"type":"presence","presence_revision":result.presence_revision}
                if request.action=="ack":
                    result=acknowledge_text_delivery(conn,actor,request.response_id)
                    return {"type":"ack","response_id":str(result.response_id),"replayed":result.replayed}
                if request.action=="message":
                    _allowed_route(request.text)
                    entered=ingest_text(conn,actor,TextInputCommand(
                        conversation_id=identity.conversation_id,instance_id=identity.instance_id,
                        expected_generation=identity.generation,
                        idempotency_key=_request_key(identity,"message",request.idempotency_key),
                        text=request.text,
                    ))
                    turn_id=entered.turn_id
                else:
                    turn_id=request.turn_id
                with conn.transaction():
                    with conn.cursor(row_factory=dict_row) as cur:
                        _scope(cur,identity)
                        item=_turn(cur,identity,turn_id)
                        _allowed_route(item["input_text"])
                # No credential/session lock or transaction over provider work.
                reply=respond_to_user_text(conn,actor,turn_id,self._gateway)
                return _offer(conn,identity,reply)
        except (PermissionDenied,StaleSelection):
            return {"type":"error","code":"NOT_AUTHORIZED"}
        except InvalidatedResponse:
            return {"type":"error","code":"STALE_EVIDENCE"}
        except StaleConversation:
            return {"type":"error","code":"STALE"}
        except IdempotencyConflict:
            return {"type":"error","code":"CONFLICT"}
        except ModelUnavailable:
            return {"type":"error","code":"MODEL_UNAVAILABLE"}
        except (ValidationError,ValueError):
            return {"type":"error","code":"INVALID_REQUEST"}
        except Exception:
            # Never serialize SQL/provider diagnostics, input or token.
            return {"type":"error","code":"INTERNAL_ERROR"}


def serve_jsonl(application,reader,writer):
    """Binary local confidential stdin/stdout adapter, no network listener.

    Terminate on an oversized line rather than reading an unbounded body.
    Caller must provide a private channel; no request echo or auto delivery.
    """
    while True:
        line=reader.readline(MAX_REQUEST_BYTES+1)
        if not line:
            return
        response=application.handle(line)
        writer.write((json.dumps(response,ensure_ascii=True,separators=(",",":"))+"\n").encode("utf-8"))
        writer.flush()
        if len(line)>MAX_REQUEST_BYTES:
            return


def main():
    """Operator-owned process configuration; never accept credentials in argv."""
    import os
    import sys
    from .model_gateway import OllamaChatGateway
    dsn=os.environ.get("PERSONA_APPLICATION_DSN")
    model=os.environ.get("PERSONA_APPLICATION_MODEL")
    if not dsn or not model:
        # Do not echo environment values or provider configuration.
        sys.stderr.write("Application operator configuration is missing.\n")
        return 2
    try:
        gateway=OllamaChatGateway(model=model,
            base_url=os.environ.get("PERSONA_APPLICATION_MODEL_URL","http://127.0.0.1:11434"))
        serve_jsonl(AuthenticatedTextApplication(dsn,gateway),sys.stdin.buffer,sys.stdout.buffer)
    except Exception:
        sys.stderr.write("Application transport terminated.\n")
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
