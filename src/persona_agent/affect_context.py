"""P2-07 read-only derived context, never a new authoritative state store.

Explicit user communication preferences take precedence over optional mood
and tentative user-affect cues. No memory fact or model-generated intention
is promoted into relationship authority by this view.
"""

from datetime import datetime,timezone

from .emotion_state import _decay


def persona_affect_context_block(
    cur,user_id,instance_id,max_chars:int,
) -> str:
    """Bounded context contribution inside the foreground's existing read.

    Nothing here authorizes tools, overrides a direct user message, or implies
    that a reported user feeling was independently verified.
    """
    if max_chars<140:
        return ""
    cur.execute(
        """SELECT preference_key,scope_key,preference_value
           FROM relationship_preferences
           WHERE user_id=%s AND instance_id=%s AND status='ACTIVE'
           ORDER BY preference_key,scope_key LIMIT 20""",
        (user_id,instance_id),
    )
    preferences=cur.fetchall()
    cur.execute(
        """SELECT valence,arousal,engagement,baseline_valence,
                  baseline_arousal,baseline_engagement,last_source_at
           FROM persona_emotion_state
           WHERE user_id=%s AND instance_id=%s""",
        (user_id,instance_id),
    )
    mood=cur.fetchone()
    cur.execute(
        """SELECT user_affect_label,confidence,source_turn_at
           FROM emotion_appraisals
           WHERE user_id=%s AND instance_id=%s AND applied
             AND user_affect_label IS NOT NULL
             AND expires_at>clock_timestamp()
           ORDER BY source_turn_at DESC,appraisal_id DESC LIMIT 1""",
        (user_id,instance_id),
    )
    affect=cur.fetchone()

    lines=[]
    for p in preferences:
        k=p["preference_key"]
        value=p["preference_value"]
        if k=="RESPONSE_LENGTH":
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
    if affect is not None:
        lines.append(
            "Tentative user-affect signal (not verified fact): "
            f"{affect['user_affect_label'].lower()}, "
            f"confidence {float(affect['confidence']):.2f}; "
            "ask rather than assume when it matters."
        )
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
        return ""
    header="Scoped persona presentation context (evidence-linked; preferences override affect):\n"
    result=header
    for line in lines:
        if len(result)+len(line)+1<=max_chars:
            result+=line+"\n"
    return result if result!=header else ""
