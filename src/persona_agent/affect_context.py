"""P2-07 read-only derived context, never a new authoritative state store.

Explicit user communication preferences take precedence over optional mood
and tentative user-affect cues. No memory fact or model-generated intention
is promoted into relationship authority by this view.
"""

from datetime import datetime,timezone

from .emotion_state import _decay,effective_emotion_projection,current_user_affect
from .affect_lineage import FOREGROUND_APPRAISAL_LIMIT,fence_with_expiry
from .memory_read_policy import capture_historical_read_fence
from .communication_preference_rules import parse_explicit_sentence_limit


def prepare_affect_context(cur,user_id,instance_id,max_chars:int,fence=None):
    """Bounded context contribution inside the foreground's existing read.

    Nothing here authorizes tools, overrides a direct user message, or implies
    that a reported user feeling was independently verified.
    """
    if max_chars<140:
        return "",None
    # Avoid capturing an additional fence for personas with no affect or
    # preference state. All later reads are scoped and the fence is observed
    # before the evidence is used.
    cur.execute(
        """SELECT EXISTS(SELECT 1 FROM persona_emotion_state
                         WHERE user_id=%s AND instance_id=%s)
               OR EXISTS(SELECT 1 FROM relationship_preferences
                         WHERE user_id=%s AND instance_id=%s AND status='ACTIVE') AS available""",
        (user_id,instance_id,user_id,instance_id),
    )
    if not cur.fetchone()["available"]:
        return "",None
    fence=fence or capture_historical_read_fence(cur,user_id,instance_id)
    cur.execute(
        """WITH candidates AS MATERIALIZED (
               SELECT * FROM relationship_preferences
               WHERE user_id=%s AND instance_id=%s AND status='ACTIVE'
               ORDER BY CASE preference_key
                   WHEN 'RESPONSE_LENGTH' THEN 0
                   WHEN 'EMOJI_USE' THEN 1
                   WHEN 'PROACTIVE_MESSAGES' THEN 2
                   WHEN 'AVOID_PROACTIVE_TOPIC' THEN 3
                   ELSE 4 END, scope_key
               LIMIT 20
           )
           SELECT preference_key,scope_key,preference_value,source_quote
           FROM candidates p
           WHERE dialogue_turn_current(p.user_id,p.instance_id,
                                       p.source_conversation_id,p.source_turn_id)
           ORDER BY CASE preference_key
               WHEN 'RESPONSE_LENGTH' THEN 0
               WHEN 'EMOJI_USE' THEN 1
               WHEN 'PROACTIVE_MESSAGES' THEN 2
               WHEN 'AVOID_PROACTIVE_TOPIC' THEN 3
               ELSE 4 END, scope_key""",
        (user_id,instance_id),
    )
    # Global communication preferences occupy the first three query slots
    # and first context lines. Topic-specific boundaries use only the
    # remaining slots/character budget; otherwise 20 topic rows can hide
    # global instructions from the foreground model.
    preferences=cur.fetchall()
    cur.execute(
        """SELECT *
           FROM persona_emotion_state
           WHERE user_id=%s AND instance_id=%s""",
        (user_id,instance_id),
    )
    mood=effective_emotion_projection(cur,user_id,instance_id,cur.fetchone(),fence,
                                      limit=FOREGROUND_APPRAISAL_LIMIT)
    affect=current_user_affect(cur,user_id,instance_id,datetime.now(timezone.utc))

    lines=[]
    for p in preferences:
        k=p["preference_key"]
        value=p["preference_value"]
        if k=="RESPONSE_LENGTH":
            instruction=(
                parse_explicit_sentence_limit(p["source_quote"])
                if value=="SHORT" else None
            )
            if instruction is not None:
                # The exact admitted exception is domain-owned source evidence;
                # do not narrow "more detail" to "detailed instructions".
                line="Explicit user preference: keep replies to one or two sentences"
                if instruction.exception=="DETAILED_INSTRUCTIONS":
                    line+=" unless the current request asks for detailed instructions"
                elif instruction.exception=="MORE_DETAIL":
                    line+=" unless the current request asks for more detail"
                line+="."
                if instruction.exception=="DETAILED_INSTRUCTIONS":
                    line+=" Follow a direct request for detailed instructions when given."
                elif instruction.exception=="MORE_DETAIL":
                    line+=" Follow a direct request for more detail when given."
            else:
                line=f"Explicit user preference: response length {value.lower()}."
        elif k=="EMOJI_USE":
            line=("Explicit user preference: do not use emojis."
                  if value=="OFF" else
                  "Explicit user preference: emojis are allowed when appropriate.")
        elif k=="PROACTIVE_MESSAGES":
            line=("Explicit user boundary: no unsolicited proactive messages."
                  if value=="DISALLOW" else
                  "Explicit user preference: proactive messages may be considered, "
                  "subject to attention, delivery and authorization gates.")
        elif k=="AVOID_PROACTIVE_TOPIC":
            line=("Explicit user boundary: do not proactively introduce topic "
                  f"{p['scope_key']!r}; answer direct user questions normally.")
        else:
            continue
        lines.append(line)
    affect_line=None
    if affect is not None:
        affect_line=(
            "Tentative user-affect signal (not verified fact): "
            f"{affect['user_affect_label'].lower()}, "
            f"confidence {float(affect['confidence']):.2f}; "
            "ask rather than assume when it matters."
        )
        lines.append(affect_line)
    if mood is not None and mood["last_source_at"] is not None:
        now=datetime.now(timezone.utc)
        valence=_decay(
            float(mood["valence"]),float(mood["baseline_valence"]),
            mood["last_source_at"],now,
        )
        arousal=_decay(
            float(mood["arousal"]),float(mood["baseline_arousal"]),
            mood["last_source_at"],now,
        )
        if abs(valence-float(mood["baseline_valence"]))>=0.03:
            lines.append(
                "Simulated persona expressive affect (not a user emotion or "
                "reason to override instructions): "
                f"valence {valence:+.2f}, arousal {arousal:.2f}."
            )
    if not lines:
        return "",None
    header="Scoped persona presentation context (evidence-linked; preferences override affect):\n"
    result=header
    for line in lines:
        if len(result)+len(line)+1<=max_chars:
            result+=line+"\n"
            if line==affect_line:
                fence=fence_with_expiry(fence,affect["expires_at"])
    return (result,fence) if result!=header else ("",None)


def persona_affect_context_block(cur,user_id,instance_id,max_chars:int)->str:
    """Compatibility read-only text projection; generation uses the fenced form."""
    return prepare_affect_context(cur,user_id,instance_id,max_chars)[0]
