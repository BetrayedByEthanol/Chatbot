"""MVP-01C: automatic, bounded Conversation Manager dialogue progression.

These are discussion-level observations, not domain goals, permissions,
user-approved decisions or procedure outcomes. All mutations use the
existing conversation row lock and its surrounding ingress/delivery
transaction; no model inference, tools, or new scheduler are invoked.
"""
import re
from uuid import UUID, uuid4

from .dialogue_state import _link_turn
from .intent_interpretation import classify_intent
from .conversation import _event

_TOPIC_REQUEST = re.compile(
    r"^(?:please help\b|help me\b|can you help\b|could you help\b|"
    r"how (?:do|can|would)\b|"
    r"let(?:'s| us) (?:discuss|talk about)\b|"
    r"(?:switch|change) (?:the )?topic\b|"
    r"new topic\b)",
    re.I,
)
_CLEAR_SWITCH = re.compile(
    r"^(?:let(?:'s| us) (?:discuss|talk about)\b|"
    r"(?:switch|change) (?:the )?topic\b|new topic\b)",
    re.I,
)
_QUESTION = re.compile(r"(?:^|[.!]\s+)([^.!?\n]{4,180}\?)\s*$")
_NOT_ANSWERS = {"okay", "ok", "got it", "understood"}
_YES_NO = {"yes", "no", "yes please", "no thanks", "sure"}


def _event_of_progress(
    cur, *, user_id, instance_id, conversation_id,
    event_type, turn_id, thread_id, **fields,
):
    return _event(
        cur,user_id=user_id,instance_id=instance_id,
        conversation_id=conversation_id,event_type=event_type,
        source_id="conversation.dialogue.automation",
        trust_level="TRUSTED_PLATFORM",
        detail={
            "thread_id":str(thread_id),"evidence_turn_id":str(turn_id),
            "authority":"DISCUSSION_ONLY",
            **fields,
        },
    )


def _focus(
    cur, *, user_id, instance_id, conversation_id,
    turn_id, thread_id, current_focus,
):
    # Do not reassign an input which an explicit Dialogue command already
    # bound to another thread. No correction of authoritative progress.
    cur.execute(
        """SELECT thread_id FROM dialogue_turn_links
           WHERE turn_id=%s AND conversation_id=%s AND user_id=%s""",
        (turn_id,conversation_id,user_id),
    )
    linked=cur.fetchone()
    if linked is not None and linked["thread_id"]!=thread_id:
        return False
    _link_turn(cur,conversation_id,user_id,instance_id,thread_id,turn_id)
    if current_focus!=thread_id and current_focus is not None:
        cur.execute(
            """UPDATE dialogue_threads
               SET attention_status='RECENT',revision=revision+1,
                   updated_at=clock_timestamp()
               WHERE thread_id=%s AND conversation_id=%s AND user_id=%s
                 AND attention_status='FOCUSED'""",
            (current_focus,conversation_id,user_id),
        )
    cur.execute(
        """UPDATE dialogue_threads
           SET attention_status='FOCUSED',last_turn_id=%s,
               revision=revision+1,updated_at=clock_timestamp()
           WHERE thread_id=%s AND conversation_id=%s AND user_id=%s
             AND instance_id=%s""",
        (turn_id,thread_id,conversation_id,user_id,instance_id),
    )
    if current_focus!=thread_id:
        cur.execute(
            """UPDATE conversations SET focus_thread_id=%s
               WHERE conversation_id=%s AND user_id=%s
                 AND instance_id=%s""",
            (thread_id,conversation_id,user_id,instance_id),
        )
    return True


def advance_on_delivery(
    cur, *, user_id: UUID, instance_id: UUID,
    conversation_id: UUID, turn_id: UUID,
    response_id: UUID, response_text: str,
) -> None:
    """Called after delivery acknowledgment under the conversation lock.

    An admitted user-intent signal can open/focus a bounded thread; a
    delivered assistant question can become pending only on an AUTO thread.
    The response must be delivered; generated-but-undelivered text has no
    durable conversational-question effect.
    """
    cur.execute(
        """SELECT focus_thread_id FROM conversations
           WHERE conversation_id=%s AND user_id=%s AND instance_id=%s""",
        (conversation_id,user_id,instance_id),
    )
    conv=cur.fetchone()
    if conv is None:
        return
    current=conv["focus_thread_id"]
    cur.execute(
        """SELECT signal_id,intent_kind,label,related_signal_id
           FROM conversation_intent_signals
           WHERE turn_id=%s AND conversation_id=%s AND user_id=%s
             AND instance_id=%s""",
        (turn_id,conversation_id,user_id,instance_id),
    )
    signal=cur.fetchone()
    target=None
    new=False
    if signal is not None:
        if signal["related_signal_id"] is not None:
            # The Conversation Manager, not the model, resolves the
            # source signal's existing thread via authentic turn evidence.
            cur.execute(
                """SELECT l.thread_id
                   FROM conversation_intent_signals origin
                   JOIN dialogue_turn_links l
                     ON l.turn_id=origin.turn_id
                    AND l.conversation_id=origin.conversation_id
                    AND l.user_id=origin.user_id
                   JOIN dialogue_threads d ON d.thread_id=l.thread_id
                   WHERE origin.signal_id=%s AND origin.conversation_id=%s
                     AND origin.user_id=%s AND origin.instance_id=%s
                     AND d.instance_id=%s
                   LIMIT 1""",
                (signal["related_signal_id"],conversation_id,
                 user_id,instance_id,instance_id),
            )
            match=cur.fetchone()
            if match is not None:
                target=match["thread_id"]
        elif (signal["intent_kind"]=="OBJECTIVE"
              or (signal["intent_kind"]=="REQUEST"
                  and _TOPIC_REQUEST.match(signal["label"]))):
            # A fresh objective/topic request is a conversational subject,
            # not an approved Goal. Exact title reuse prevents duplicates
            # for repeated explicit requests; fuzzy matching is deferred.
            title=signal["label"].strip()[:160]
            if title:
                cur.execute(
                    """SELECT thread_id FROM dialogue_threads
                       WHERE conversation_id=%s AND user_id=%s
                         AND instance_id=%s AND origin_kind='AUTO_SIGNAL'
                         AND lower(title)=lower(%s)
                       ORDER BY updated_at DESC LIMIT 2""",
                    (conversation_id,user_id,instance_id,title),
                )
                matches=cur.fetchall()
                if len(matches)==1:
                    target=matches[0]["thread_id"]
                elif len(matches)==0:
                    target=uuid4()
                    cur.execute(
                        """INSERT INTO dialogue_threads (
                             thread_id,conversation_id,user_id,instance_id,
                             title,origin_turn_id,last_turn_id,origin_kind
                           ) VALUES (%s,%s,%s,%s,%s,%s,%s,'AUTO_SIGNAL')""",
                        (target,conversation_id,user_id,instance_id,
                         title,turn_id,turn_id),
                    )
                    new=True
    if target is not None:
        if _focus(
            cur,user_id=user_id,instance_id=instance_id,
            conversation_id=conversation_id,turn_id=turn_id,
            thread_id=target,current_focus=current,
        ):
            if new:
                cur.execute(
                    """INSERT INTO dialogue_referents (
                         referent_id,conversation_id,thread_id,user_id,
                         instance_id,kind,label,source_turn_id
                       ) VALUES (%s,%s,%s,%s,%s,'TOPIC',%s,%s)""",
                    (uuid4(),conversation_id,target,user_id,
                     instance_id,signal["label"][:160],turn_id),
                )
            _event_of_progress(
                cur,user_id=user_id,instance_id=instance_id,
                conversation_id=conversation_id,
                event_type=(
                    "conversation.dialogue.auto_thread_opened.v1"
                    if new else "conversation.dialogue.auto_focused.v1"
                ),
                turn_id=turn_id,thread_id=target,
                source_signal_id=str(signal["signal_id"]),
            )
            current=target

    # Never create automatic questions on an explicitly managed thread.
    if current is None:
        return
    cur.execute(
        """SELECT origin_kind FROM dialogue_threads
           WHERE thread_id=%s AND conversation_id=%s AND user_id=%s
             AND instance_id=%s""",
        (current,conversation_id,user_id,instance_id),
    )
    focused=cur.fetchone()
    if focused is None or focused["origin_kind"]!="AUTO_SIGNAL":
        return
    question=_QUESTION.search(response_text)
    if question is None or response_text.count("?")!=1:
        return
    label=question.group(1).strip()
    if not label or len(label)>200:
        return
    cur.execute(
        """SELECT 1 FROM dialogue_pending_acts
           WHERE thread_id=%s AND conversation_id=%s AND user_id=%s
             AND status='PENDING' LIMIT 1""",
        (current,conversation_id,user_id),
    )
    if cur.fetchone() is not None:
        return
    act_id=uuid4()
    cur.execute(
        """INSERT INTO dialogue_pending_acts (
             act_id,conversation_id,thread_id,user_id,instance_id,
             kind,label,source_response_id,origin_kind
           ) VALUES (%s,%s,%s,%s,%s,'QUESTION',%s,%s,
                     'AUTO_DELIVERED_QUESTION')""",
        (act_id,conversation_id,current,user_id,instance_id,
         label,response_id),
    )
    cur.execute(
        """UPDATE dialogue_threads
           SET next_conversational_action=%s,revision=revision+1,
               updated_at=clock_timestamp()
           WHERE thread_id=%s AND conversation_id=%s AND user_id=%s""",
        (("Await answer: "+label)[:200],current,conversation_id,user_id),
    )
    _event_of_progress(
        cur,user_id=user_id,instance_id=instance_id,
        conversation_id=conversation_id,
        event_type="conversation.dialogue.auto_question_offered.v1",
        turn_id=turn_id,thread_id=current,act_id=str(act_id),
        response_id=str(response_id),
    )


def resolve_auto_question_on_ingress(
    cur, *, user_id: UUID, instance_id: UUID,
    conversation_id: UUID, turn_id: UUID,
    turn_sequence: int, text: str,
) -> None:
    """Conservative discussion-only answer recognition at authenticated ingress.

    A nonadjacent question, topic switch, literal ACK or user question is
    never silently treated as answered. No DECISION or task state is changed.
    """
    value=text.strip()
    simple=value.casefold().strip(" .!?")
    if (not value or simple in _NOT_ANSWERS or "?" in value
            or _CLEAR_SWITCH.match(value)):
        return
    interpretation=classify_intent(value)
    if interpretation and interpretation.kind in (
        "OBJECTIVE","CORRECTION","HYPOTHETICAL","INSTRUCTION"
    ):
        return
    cur.execute(
        """SELECT c.focus_thread_id FROM conversations c
           JOIN dialogue_threads t ON t.thread_id=c.focus_thread_id
           WHERE c.conversation_id=%s AND c.user_id=%s
             AND c.instance_id=%s AND t.origin_kind='AUTO_SIGNAL'""",
        (conversation_id,user_id,instance_id),
    )
    row=cur.fetchone()
    if row is None:
        return
    current=row["focus_thread_id"]
    cur.execute(
        """SELECT a.act_id
           FROM dialogue_pending_acts a
           JOIN conversation_responses r
             ON r.response_id=a.source_response_id
            AND r.conversation_id=a.conversation_id
            AND r.user_id=a.user_id
           JOIN conversation_turns t ON t.turn_id=r.turn_id
           WHERE a.thread_id=%s AND a.conversation_id=%s AND a.user_id=%s
             AND a.instance_id=%s AND a.status='PENDING'
             AND a.origin_kind='AUTO_DELIVERED_QUESTION'
             AND r.delivery_status='DELIVERED'
             AND t.sequence=%s
           ORDER BY a.created_at,a.act_id LIMIT 2""",
        (current,conversation_id,user_id,instance_id,turn_sequence-1),
    )
    acts=cur.fetchall()
    if len(acts)!=1:
        return
    _link_turn(cur,conversation_id,user_id,instance_id,current,turn_id)
    cur.execute(
        """UPDATE dialogue_pending_acts
           SET status='RESOLVED',resolution_kind='ANSWERED',
               resolution_turn_id=%s,updated_at=clock_timestamp()
           WHERE act_id=%s AND status='PENDING'""",
        (turn_id,acts[0]["act_id"]),
    )
    cur.execute(
        """UPDATE dialogue_threads
           SET last_turn_id=%s,next_conversational_action=NULL,
               revision=revision+1,updated_at=clock_timestamp()
           WHERE thread_id=%s AND conversation_id=%s AND user_id=%s""",
        (turn_id,current,conversation_id,user_id),
    )
    _event_of_progress(
        cur,user_id=user_id,instance_id=instance_id,
        conversation_id=conversation_id,
        event_type="conversation.dialogue.auto_question_answered.v1",
        turn_id=turn_id,thread_id=current,act_id=str(acts[0]["act_id"]),
    )
