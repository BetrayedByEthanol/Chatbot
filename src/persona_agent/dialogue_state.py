"""P1-03 Conversation Manager: explicit, evidence-linked dialogue state.

One conversation revision and row lock serialize all dialogue changes with
text input/response. No LLM calls, task success claims or external tools.
Untrusted labels are stored as metadata; user consent is not inferred from
acknowledging an ordinary conversational instruction.
"""

import json
from typing import Callable
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from pydantic import BaseModel

from .contracts import TrustedActor
from .conversation import _event, _require_actor, _selection
from .dialogue_contracts import (
    ActiveDialogue, Attention, DialogueActKind, DialogueChangeResult,
    DialogueThread, FocusThreadCommand, OfferDialogueActCommand,
    OpenThreadCommand, PendingAct, Referent, RegisterReferentCommand,
    Resolution, ResolveDialogueActCommand, SetThreadProgressCommand,
)
from .operation_contracts import require_idle
from .runtime import (
    IdempotencyConflict, PermissionDenied, VersionConflict, _fingerprint,
)


def _scoped_thread(cur, conversation_id: UUID, user_id: UUID, thread_id: UUID):
    cur.execute(
        """SELECT * FROM dialogue_threads
           WHERE conversation_id=%s AND user_id=%s AND thread_id=%s""",
        (conversation_id,user_id,thread_id),
    )
    row=cur.fetchone()
    if row is None:
        raise PermissionDenied("thread outside authenticated conversation")
    return row


def _link_turn(cur, conversation_id: UUID, user_id: UUID,
               instance_id: UUID, thread_id: UUID, turn_id: UUID):
    """A single text input can be evidence for only one active thread."""
    cur.execute(
        """
        INSERT INTO dialogue_turn_links(
            turn_id,conversation_id,thread_id,user_id,instance_id
        ) VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT(turn_id) DO NOTHING RETURNING thread_id
        """,
        (turn_id,conversation_id,thread_id,user_id,instance_id),
    )
    if cur.fetchone() is None:
        cur.execute(
            "SELECT thread_id FROM dialogue_turn_links "
            "WHERE turn_id=%s AND user_id=%s",
            (turn_id,user_id),
        )
        prior=cur.fetchone()
        if prior is None or prior["thread_id"]!=thread_id:
            raise VersionConflict("input turn is already linked to another thread")


def _change(
    connection: Connection,
    actor: TrustedActor,
    command: BaseModel,
    command_type: str,
    event_type: str,
    mutate: Callable,
) -> DialogueChangeResult:
    """Idempotent, selection-fenced, revision-CAS dialogue domain commit."""
    require_idle(connection)
    _require_actor(actor)
    request_fingerprint=_fingerprint(command)
    receipt_id=uuid4()
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
                (actor.user_id,command.instance_id),
            )
            if cur.fetchone() is None:
                raise PermissionDenied("dialogue instance outside authenticated user")
            cur.execute(
                """
                INSERT INTO command_receipts (
                    receipt_id,user_id,instance_id,command_type,
                    idempotency_key,request_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(user_id,instance_id,command_type,idempotency_key)
                DO NOTHING RETURNING receipt_id
                """,
                (receipt_id,actor.user_id,command.instance_id,
                 command_type,command.idempotency_key,request_fingerprint),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT request_fingerprint,result_json FROM command_receipts
                       WHERE user_id=%s AND instance_id=%s
                         AND command_type=%s AND idempotency_key=%s""",
                    (actor.user_id,command.instance_id,
                     command_type,command.idempotency_key),
                )
                prior=cur.fetchone()
                if (prior is None or
                    prior["request_fingerprint"]!=request_fingerprint or
                    prior["result_json"] is None):
                    raise IdempotencyConflict("dialogue key reused with different command")
                return DialogueChangeResult.model_validate(prior["result_json"]).model_copy(
                    update={"replayed":True},
                )

            _selection(cur,actor,command.instance_id,command.expected_generation)
            cur.execute(
                """SELECT * FROM conversations
                   WHERE conversation_id=%s AND user_id=%s AND instance_id=%s
                   FOR UPDATE""",
                (command.conversation_id,actor.user_id,command.instance_id),
            )
            conversation=cur.fetchone()
            if conversation is None:
                raise PermissionDenied("conversation outside authenticated scope")
            if conversation["revision"]!=command.expected_revision:
                raise VersionConflict("dialogue proposal uses stale conversation revision")
            if conversation["last_input_turn_id"]!=command.evidence_turn_id:
                raise VersionConflict("dialogue proposal refers to an obsolete input turn")
            thread_id=mutate(cur,conversation)
            cur.execute(
                """UPDATE conversations SET revision=revision+1,
                   updated_at=clock_timestamp()
                   WHERE conversation_id=%s AND user_id=%s
                   RETURNING revision""",
                (command.conversation_id,actor.user_id),
            )
            revision=cur.fetchone()["revision"]
            event_id=_event(
                cur,user_id=actor.user_id,instance_id=command.instance_id,
                conversation_id=command.conversation_id,
                event_type=event_type,source_id=actor.source_id,
                trust_level="AUTHENTICATED_USER",
                detail={"thread_id":str(thread_id),
                        "evidence_turn_id":str(command.evidence_turn_id),
                        "revision":revision},
                correlation_id=receipt_id,
            )
            result=DialogueChangeResult(
                conversation_id=command.conversation_id,
                thread_id=thread_id,revision=revision,event_id=event_id,
            )
            cur.execute(
                """UPDATE command_receipts
                   SET event_id=%s,result_json=%s::jsonb WHERE receipt_id=%s""",
                (event_id,result.model_dump_json(),receipt_id),
            )
            return result


def open_thread(connection: Connection, actor: TrustedActor,
                command: OpenThreadCommand) -> DialogueChangeResult:
    def mutate(cur,conversation):
        cur.execute(
            """
            INSERT INTO dialogue_threads(
                thread_id,conversation_id,user_id,instance_id,title,
                origin_turn_id,last_turn_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (command.thread_id,command.conversation_id,actor.user_id,
             command.instance_id,command.title,
             command.evidence_turn_id,command.evidence_turn_id),
        )
        old=conversation["focus_thread_id"]
        if old is not None:
            cur.execute(
                """UPDATE dialogue_threads
                   SET attention_status='RECENT',revision=revision+1,
                       updated_at=clock_timestamp()
                   WHERE thread_id=%s AND attention_status='FOCUSED'""",
                (old,),
            )
        cur.execute(
            "UPDATE conversations SET focus_thread_id=%s WHERE conversation_id=%s",
            (command.thread_id,command.conversation_id),
        )
        _link_turn(cur,command.conversation_id,actor.user_id,command.instance_id,
                   command.thread_id,command.evidence_turn_id)
        return command.thread_id
    return _change(
        connection,actor,command,"conversation.dialogue.open.v1",
        "conversation.dialogue.thread_opened.v1",mutate,
    )


def focus_thread(connection: Connection, actor: TrustedActor,
                 command: FocusThreadCommand) -> DialogueChangeResult:
    def mutate(cur,conversation):
        _scoped_thread(cur,command.conversation_id,actor.user_id,command.thread_id)
        old=conversation["focus_thread_id"]
        if old is not None and old!=command.thread_id:
            cur.execute(
                """UPDATE dialogue_threads
                   SET attention_status='RECENT',revision=revision+1,
                       updated_at=clock_timestamp()
                   WHERE thread_id=%s AND attention_status='FOCUSED'""",
                (old,),
            )
        _link_turn(cur,command.conversation_id,actor.user_id,command.instance_id,
                   command.thread_id,command.evidence_turn_id)
        cur.execute(
            """UPDATE dialogue_threads SET attention_status='FOCUSED',
               last_turn_id=%s,revision=revision+1,
               updated_at=clock_timestamp() WHERE thread_id=%s""",
            (command.evidence_turn_id,command.thread_id),
        )
        cur.execute(
            "UPDATE conversations SET focus_thread_id=%s WHERE conversation_id=%s",
            (command.thread_id,command.conversation_id),
        )
        return command.thread_id
    return _change(
        connection,actor,command,"conversation.dialogue.focus.v1",
        "conversation.dialogue.focus_changed.v1",mutate,
    )


def set_thread_progress(connection: Connection, actor: TrustedActor,
                        command: SetThreadProgressCommand) -> DialogueChangeResult:
    """Resolution is about the discussion, not execution/goal verification."""
    def mutate(cur,conversation):
        _scoped_thread(cur,command.conversation_id,actor.user_id,command.thread_id)
        focused=conversation["focus_thread_id"]==command.thread_id
        if focused != (command.attention==Attention.FOCUSED):
            raise VersionConflict("focused thread must have FOCUSED attention")
        if command.resolution!=Resolution.OPEN:
            cur.execute(
                """SELECT 1 FROM dialogue_pending_acts
                   WHERE thread_id=%s AND status='PENDING' LIMIT 1""",
                (command.thread_id,),
            )
            if cur.fetchone() is not None:
                raise VersionConflict("resolve or cancel pending dialogue before closure")
        cur.execute(
            """UPDATE dialogue_threads SET attention_status=%s,
               resolution_status=%s,next_conversational_action=%s,
               last_turn_id=%s,revision=revision+1,
               updated_at=clock_timestamp()
               WHERE thread_id=%s""",
            (command.attention.value,command.resolution.value,
             command.next_conversational_action,command.evidence_turn_id,
             command.thread_id),
        )
        return command.thread_id
    return _change(
        connection,actor,command,"conversation.dialogue.progress.v1",
        "conversation.dialogue.progress_updated.v1",mutate,
    )


def offer_dialogue_act(connection: Connection, actor: TrustedActor,
                       command: OfferDialogueActCommand) -> DialogueChangeResult:
    def mutate(cur,conversation):
        _scoped_thread(cur,command.conversation_id,actor.user_id,command.thread_id)
        if conversation["focus_thread_id"]!=command.thread_id:
            raise VersionConflict("only the focused thread can ask for input")
        cur.execute(
            """SELECT r.turn_id FROM conversation_responses r
               WHERE r.conversation_id=%s AND r.user_id=%s
                 AND r.instance_id=%s AND r.response_id=%s
                 AND r.delivery_status='DELIVERED'""",
            (command.conversation_id,actor.user_id,command.instance_id,
             command.source_response_id),
        )
        source=cur.fetchone()
        if source is None or source["turn_id"]!=command.evidence_turn_id:
            raise VersionConflict("pending act requires delivered current response")
        cur.execute(
            """INSERT INTO dialogue_pending_acts(
                act_id,conversation_id,thread_id,user_id,instance_id,
                kind,label,source_response_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.act_id,command.conversation_id,command.thread_id,
             actor.user_id,command.instance_id,command.kind.value,
             command.label,command.source_response_id),
        )
        return command.thread_id
    return _change(
        connection,actor,command,"conversation.dialogue.offer.v1",
        "conversation.dialogue.act_offered.v1",mutate,
    )


def resolve_dialogue_act(connection: Connection, actor: TrustedActor,
                         command: ResolveDialogueActCommand) -> DialogueChangeResult:
    def mutate(cur,conversation):
        cur.execute(
            """SELECT * FROM dialogue_pending_acts
               WHERE act_id=%s AND conversation_id=%s AND user_id=%s
               FOR UPDATE""",
            (command.act_id,command.conversation_id,actor.user_id),
        )
        act=cur.fetchone()
        if act is None:
            raise PermissionDenied("pending act outside authenticated scope")
        if conversation["focus_thread_id"]!=act["thread_id"]:
            raise VersionConflict("resolve pending act only on focused thread")
        if act["status"]!="PENDING":
            raise VersionConflict("dialogue act has already been resolved")
        _link_turn(cur,command.conversation_id,actor.user_id,command.instance_id,
                   act["thread_id"],command.evidence_turn_id)
        cur.execute(
            """UPDATE dialogue_pending_acts SET status=%s,
               resolution_kind=%s,resolution_turn_id=%s,
               updated_at=clock_timestamp() WHERE act_id=%s""",
            ("CANCELLED" if command.cancel else "RESOLVED",
             None if command.cancel else command.outcome.value,
             command.evidence_turn_id,command.act_id),
        )
        cur.execute(
            """UPDATE dialogue_threads SET last_turn_id=%s,
               revision=revision+1,updated_at=clock_timestamp()
               WHERE thread_id=%s""",
            (command.evidence_turn_id,act["thread_id"]),
        )
        return act["thread_id"]
    return _change(
        connection,actor,command,"conversation.dialogue.resolve.v1",
        "conversation.dialogue.act_resolved.v1",mutate,
    )


def register_referent(connection: Connection, actor: TrustedActor,
                      command: RegisterReferentCommand) -> DialogueChangeResult:
    def mutate(cur,conversation):
        _scoped_thread(cur,command.conversation_id,actor.user_id,command.thread_id)
        if conversation["focus_thread_id"]!=command.thread_id:
            raise VersionConflict("new referents require the focused thread")
        cur.execute(
            """INSERT INTO dialogue_referents(
                referent_id,conversation_id,thread_id,user_id,instance_id,
                kind,label,source_turn_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (command.referent_id,command.conversation_id,command.thread_id,
             actor.user_id,command.instance_id,command.kind,command.label,
             command.evidence_turn_id),
        )
        return command.thread_id
    return _change(
        connection,actor,command,"conversation.dialogue.referent.v1",
        "conversation.dialogue.referent_recorded.v1",mutate,
    )


def load_active_dialogue(
    connection: Connection, actor: TrustedActor, conversation_id: UUID,
) -> ActiveDialogue:
    """Scoped read after restart; no side effects and no persona activation."""
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        # A coherent read of state, pending acts and referents at one MVCC
        # snapshot; later commands still fence on the returned revision.
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT instance_id,revision,focus_thread_id
                   FROM conversations WHERE conversation_id=%s AND user_id=%s""",
                (conversation_id,actor.user_id),
            )
            conv=cur.fetchone()
            if conv is None:
                raise PermissionDenied("conversation outside authenticated scope")
            cur.execute(
                """SELECT thread_id,title,attention_status,resolution_status,
                          next_conversational_action,revision
                   FROM dialogue_threads
                   WHERE conversation_id=%s AND user_id=%s
                   ORDER BY CASE WHEN thread_id=%s THEN 0 ELSE 1 END,
                            updated_at DESC,thread_id LIMIT 32""",
                (conversation_id,actor.user_id,conv["focus_thread_id"]),
            )
            threads=tuple(DialogueThread(**t) for t in cur.fetchall())
            acts=()
            refs=()
            if conv["focus_thread_id"] is not None:
                cur.execute(
                    """SELECT act_id,thread_id,kind,label,source_response_id
                       FROM dialogue_pending_acts
                       WHERE thread_id=%s AND user_id=%s AND status='PENDING'
                       ORDER BY created_at,act_id LIMIT 8""",
                    (conv["focus_thread_id"],actor.user_id),
                )
                acts=tuple(PendingAct(**a) for a in cur.fetchall())
                cur.execute(
                    """SELECT referent_id,thread_id,kind,label,source_turn_id
                       FROM dialogue_referents WHERE thread_id=%s AND user_id=%s
                       ORDER BY created_at DESC,referent_id LIMIT 8""",
                    (conv["focus_thread_id"],actor.user_id),
                )
                refs=tuple(Referent(**r) for r in cur.fetchall())
            return ActiveDialogue(
                conversation_id=conversation_id,instance_id=conv["instance_id"],
                revision=conv["revision"],focus_thread_id=conv["focus_thread_id"],
                threads=threads,pending_acts=acts,referents=refs,
            )


_ACK_TEXT={"ok","okay","got it","understood"}


def resolve_trivial_ack_on_ingress(
    cur, *, user_id: UUID, instance_id: UUID,
    conversation_id: UUID, turn_id: UUID, text: str, source_id: str,
) -> UUID | None:
    """Called only from authenticated ingest_text with its conversation lock.

    An unambiguous ACKNOWLEDGMENT is dialogue progress, never authorization
    for a tool, financial action, decision, task or procedure completion.
    """
    if text.casefold().strip(" .!?") not in _ACK_TEXT:
        return None
    cur.execute(
        """SELECT focus_thread_id FROM conversations
           WHERE conversation_id=%s AND user_id=%s""",
        (conversation_id,user_id),
    )
    conv=cur.fetchone()
    if conv is None or conv["focus_thread_id"] is None:
        return None
    thread=conv["focus_thread_id"]
    cur.execute(
        """SELECT a.act_id,source.sequence AS source_sequence
           FROM dialogue_pending_acts a
           JOIN conversation_responses response
             ON response.response_id=a.source_response_id
            AND response.user_id=a.user_id
            AND response.delivery_status='DELIVERED'
           JOIN conversation_turns source ON source.turn_id=response.turn_id
           WHERE a.conversation_id=%s AND a.thread_id=%s
             AND a.user_id=%s AND a.kind='ACKNOWLEDGMENT'
             AND a.status='PENDING'
           ORDER BY a.created_at,a.act_id LIMIT 2""",
        (conversation_id,thread,user_id),
    )
    acts=cur.fetchall()
    if len(acts)!=1:
        return None
    cur.execute(
        "SELECT sequence FROM conversation_turns "
        "WHERE turn_id=%s AND conversation_id=%s AND user_id=%s",
        (turn_id,conversation_id,user_id),
    )
    current=cur.fetchone()
    if current is None or acts[0]["source_sequence"]+1!=current["sequence"]:
        # A dangling ACK from an older exchange does not establish what
        # the user's short acknowledgment refers to. Leave it pending.
        return None
    act_id=acts[0]["act_id"]
    _link_turn(cur,conversation_id,user_id,instance_id,thread,turn_id)
    cur.execute(
        """UPDATE dialogue_pending_acts
           SET status='RESOLVED',resolution_kind='ANSWERED',
               resolution_turn_id=%s,updated_at=clock_timestamp()
           WHERE act_id=%s AND status='PENDING'""",
        (turn_id,act_id),
    )
    cur.execute(
        """UPDATE dialogue_threads SET last_turn_id=%s,
           revision=revision+1,updated_at=clock_timestamp()
           WHERE thread_id=%s""",
        (turn_id,thread),
    )
    _event(
        cur,user_id=user_id,instance_id=instance_id,
        conversation_id=conversation_id,
        event_type="conversation.dialogue.ack_resolved.v1",
        source_id=source_id,trust_level="AUTHENTICATED_USER",
        detail={"thread_id":str(thread),"act_id":str(act_id),
                "evidence_turn_id":str(turn_id)},
    )
    return act_id
