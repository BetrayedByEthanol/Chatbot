"""MVP-07A: server-only credential issuance and short owner commit fences.

A random bearer is exchanged through an operator-controlled confidential
channel. Only its digest is persisted. This is not a public login service.
"""
from dataclasses import dataclass,field
from datetime import datetime
import hashlib
import re
import secrets
from uuid import UUID,uuid4

from psycopg.rows import dict_row

from .contracts import TrustedActor
from .lifecycle import TrustedPlatformActor
from .operation_contracts import require_idle
from .runtime import PermissionDenied,StaleSelection

_TOKEN=re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class ApplicationCredential:
    session_id:UUID
    conversation_id:UUID
    token:str=field(repr=False)
    expires_at:datetime


@dataclass(frozen=True)
class ApplicationIdentity:
    session_id:UUID
    user_id:UUID
    instance_id:UUID
    conversation_id:UUID
    generation:int

    @property
    def actor(self):
        return TrustedActor(user_id=self.user_id,
            source_id=f"application-session:{self.session_id}",
            application_session_id=self.session_id)


def _platform(actor):
    if type(actor) is not TrustedPlatformActor:
        raise PermissionDenied("session provisioning requires trusted operator")


def _digest(token):
    if not isinstance(token,str) or not _TOKEN.fullmatch(token):
        raise PermissionDenied("invalid application credential")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue_application_session(connection,actor,instance_id,expected_generation,
                              *,conversation_id=None,ttl_seconds=3600):
    """Trusted operator provisions a single selected, Foreground scope.

    Return the secret only once; operator delivers it over a confidential
    channel. No caller-supplied token, identity claims or capability grants.
    """
    require_idle(connection)
    _platform(actor)
    if type(ttl_seconds) is not int or not 1<=ttl_seconds<=86400:
        raise ValueError("credential lifetime must be 1..86400 seconds")
    session_id=uuid4()
    conversation_id=conversation_id or uuid4()
    token=secrets.token_urlsafe(32)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT instance_id,generation,mode FROM persona_selection
                   WHERE user_id=%s FOR SHARE""",(actor.user_id,),
            )
            selected=cur.fetchone()
            if (selected is None or selected["instance_id"]!=instance_id
                    or selected["generation"]!=expected_generation
                    or selected["mode"]!="FOREGROUND"):
                raise StaleSelection("session requires selected Foreground persona")
            cur.execute(
                "SELECT user_id,instance_id FROM conversations WHERE conversation_id=%s",
                (conversation_id,),
            )
            existing=cur.fetchone()
            if existing is not None and (existing["user_id"]!=actor.user_id
                                         or existing["instance_id"]!=instance_id):
                raise PermissionDenied("conversation outside operator-provisioned scope")
            cur.execute(
                """INSERT INTO application_sessions(session_id,user_id,instance_id,
                       conversation_id,selection_generation,token_sha256,issued_by,expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+make_interval(secs=>%s))
                   RETURNING expires_at""",
                (session_id,actor.user_id,instance_id,conversation_id,expected_generation,
                 _digest(token),actor.source_id,ttl_seconds),
            )
            expires=cur.fetchone()["expires_at"]
    return ApplicationCredential(session_id,conversation_id,token,expires)


def revoke_application_session(connection,actor,session_id):
    """Idempotent operator revocation; no selection/lifecycle side effect."""
    require_idle(connection)
    _platform(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT revoked_at FROM application_sessions
                   WHERE session_id=%s AND user_id=%s FOR UPDATE""",
                (session_id,actor.user_id),
            )
            row=cur.fetchone()
            if row is None:
                raise PermissionDenied("session outside operator scope")
            if row["revoked_at"] is not None:
                return False
            cur.execute(
                """UPDATE application_sessions SET revoked_at=clock_timestamp(),revoked_by=%s
                   WHERE session_id=%s AND user_id=%s""",
                (actor.source_id,session_id,actor.user_id),
            )
            return True


def check_application_actor(cur,actor,instance_id,generation,conversation_id=None):
    """Selection lock precedes session lock; revoke waits for short commits.

    Called by Conversation and Lifecycle before authoritative writes, never
    across inference or transport. The server-minted actor carries no token.
    """
    if (type(actor) is not TrustedActor or actor.application_session_id is None
            or actor.source_id!=f"application-session:{actor.application_session_id}"):
        raise PermissionDenied("invalid application actor")
    cur.execute(
        """SELECT conversation_id,instance_id,selection_generation,
                  revoked_at,expires_at,clock_timestamp() AS now
           FROM application_sessions WHERE session_id=%s AND user_id=%s FOR SHARE""",
        (actor.application_session_id,actor.user_id),
    )
    row=cur.fetchone()
    if (row is None or row["revoked_at"] is not None or row["expires_at"]<=row["now"]
            or row["instance_id"]!=instance_id or row["selection_generation"]!=generation
            or (conversation_id is not None and row["conversation_id"]!=conversation_id)):
        raise PermissionDenied("application session unavailable")


def authenticate_application(connection,token):
    """Resolve scope from server state, never from untrusted input fields."""
    from .conversation import _selection
    require_idle(connection)
    digest=_digest(token)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT session_id,user_id,instance_id,conversation_id,selection_generation
                   FROM application_sessions WHERE token_sha256=%s""",(digest,),
            )
            row=cur.fetchone()
            if row is None:
                raise PermissionDenied("invalid application credential")
            identity=ApplicationIdentity(row["session_id"],row["user_id"],row["instance_id"],
                row["conversation_id"],row["selection_generation"])
            _selection(cur,identity.actor,identity.instance_id,identity.generation,identity.conversation_id)
            return identity


def require_delivery_offer(cur,actor,response_id):
    cur.execute(
        """SELECT 1 FROM application_delivery_offers
           WHERE session_id=%s AND user_id=%s AND response_id=%s""",
        (actor.application_session_id,actor.user_id,response_id),
    )
    if cur.fetchone() is None:
        raise PermissionDenied("response not offered to this application session")
