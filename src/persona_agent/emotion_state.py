"""P2-07 Emotion domain: simulated affect, decay, uncertain user signals.

Authenticated source statements can contribute small, independently labelled
appraisals. They never prove a user's internal emotion and never mutate
stable persona traits, memories, goals or permissions.
"""

from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from math import exp,log
from uuid import UUID,uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .affect_events import write_domain_event
from .contracts import TrustedActor
from .conversation import _require_actor
from .operation_contracts import require_idle
from .runtime import PermissionDenied


EMOTION_CONSUMER="emotion.user_signal.v1"
_BASELINE=(0.0,0.25,0.5)
_HALF_LIFE_HOURS=8.0


@dataclass(frozen=True)
class AffectSignal:
    kind:str
    user_label:str|None
    confidence:float
    valence_delta:float
    arousal_delta:float
    engagement_delta:float


@dataclass(frozen=True)
class EmotionSnapshot:
    user_id:UUID
    instance_id:UUID
    version:int
    valence:float
    arousal:float
    engagement:float
    baseline_valence:float
    baseline_arousal:float
    baseline_engagement:float
    last_source_at:datetime|None
    tentative_user_affect:str|None
    affect_confidence:float|None
    affect_expires_at:datetime|None


_SIGNALS={
    "i am happy":AffectSignal("USER_POSITIVE","HAPPY",0.82,0.20,0.02,0.04),
    "i'm happy":AffectSignal("USER_POSITIVE","HAPPY",0.82,0.20,0.02,0.04),
    "i feel happy":AffectSignal("USER_POSITIVE","HAPPY",0.82,0.20,0.02,0.04),
    "i am sad":AffectSignal("USER_NEGATIVE","SAD",0.82,-0.10,0.00,0.02),
    "i'm sad":AffectSignal("USER_NEGATIVE","SAD",0.82,-0.10,0.00,0.02),
    "i feel sad":AffectSignal("USER_NEGATIVE","SAD",0.82,-0.10,0.00,0.02),
    "i am excited":AffectSignal("USER_EXCITED","EXCITED",0.82,0.20,0.15,0.04),
    "i'm excited":AffectSignal("USER_EXCITED","EXCITED",0.82,0.20,0.15,0.04),
    "i am frustrated":AffectSignal("USER_FRUSTRATED","FRUSTRATED",0.82,-0.12,0.08,0.02),
    "i'm frustrated":AffectSignal("USER_FRUSTRATED","FRUSTRATED",0.82,-0.12,0.08,0.02),
    "thank you":AffectSignal("GRATITUDE",None,0.98,0.10,0.00,0.02),
    "thanks":AffectSignal("GRATITUDE",None,0.98,0.10,0.00,0.02),
    "that helped":AffectSignal("HELPFUL_FEEDBACK",None,0.98,0.08,0.00,0.00),
    "that was helpful":AffectSignal("HELPFUL_FEEDBACK",None,0.98,0.08,0.00,0.00),
    "that was not helpful":AffectSignal("UNHELPFUL_FEEDBACK",None,0.98,-0.08,0.00,0.00),
}


def classify_emotional_cue(text:str) -> AffectSignal|None:
    """One explicit supported utterance, not a generic emotion classifier.

    No indirect, quoted, uncertain, question or sensitive category is
    automatically classified. Syntax is only a candidate signal.
    """
    if not 2<=len(text)<=120 or "\n" in text or "\r" in text:
        return None
    phrase=text.strip().replace("’","'").casefold()
    if any(c in phrase for c in ('"',"?",":",";","<",">","{","}")):
        return None
    return _SIGNALS.get(phrase.rstrip("!.").strip())


def _clamp(value:float,low:float,high:float) -> float:
    return max(low,min(high,value))


def _decay(current:float,baseline:float,at:datetime,now:datetime)->float:
    seconds=max(0,(now-at).total_seconds())
    return baseline+(current-baseline)*exp(
        -log(2)*seconds/(_HALF_LIFE_HOURS*3600)
    )


def observe_emotion_input(cur,event,source:dict) -> bool:
    """Emotion-owned update in P0's atomic DB-only delivery transaction."""
    signal=classify_emotional_cue(source["input_text"])
    if signal is None:
        return False
    cur.execute(
        """INSERT INTO persona_emotion_state(user_id,instance_id)
           VALUES (%s,%s) ON CONFLICT(user_id,instance_id) DO NOTHING""",
        (event.user_id,event.instance_id),
    )
    cur.execute(
        """SELECT * FROM persona_emotion_state WHERE user_id=%s
           AND instance_id=%s FOR UPDATE""",
        (event.user_id,event.instance_id),
    )
    state=cur.fetchone()
    cur.execute(
        """SELECT 1 FROM emotion_appraisals WHERE source_event_id=%s
           AND user_id=%s AND instance_id=%s""",
        (event.event_id,event.user_id,event.instance_id),
    )
    if cur.fetchone() is not None:
        return False
    at=source["created_at"]
    previous=state["last_source_at"]
    applied=previous is None or at>previous
    if applied:
        old_at=previous or at
        valence=_clamp(
            _decay(float(state["valence"]),float(state["baseline_valence"]),
                   old_at,at)+signal.valence_delta,-1,1,
        )
        arousal=_clamp(
            _decay(float(state["arousal"]),float(state["baseline_arousal"]),
                   old_at,at)+signal.arousal_delta,0,1,
        )
        engagement=_clamp(
            _decay(float(state["engagement"]),float(state["baseline_engagement"]),
                   old_at,at)+signal.engagement_delta,0,1,
        )
        cur.execute(
            """UPDATE persona_emotion_state SET version=version+1,
               valence=%s,arousal=%s,engagement=%s,last_source_at=%s,
               updated_at=clock_timestamp()
               WHERE user_id=%s AND instance_id=%s""",
            (valence,arousal,engagement,at,event.user_id,event.instance_id),
        )
    event_id=write_domain_event(
        cur,user_id=event.user_id,instance_id=event.instance_id,
        domain="emotion",event_type="emotion.appraisal.recorded.v1",
        source_id=EMOTION_CONSUMER,trust_level="TRUSTED_PLATFORM",
        payload={
            "source_turn_id":str(source["turn_id"]),"kind":signal.kind,
            "applied":applied,"tentative":True,
        },
        causation_id=event.event_id,
    )
    expires=(at+timedelta(hours=4)) if signal.user_label else None
    cur.execute(
        """INSERT INTO emotion_appraisals(
             appraisal_id,user_id,instance_id,conversation_id,source_turn_id,
             source_event_id,event_id,source_turn_at,source_quote,signal_kind,
             user_affect_label,confidence,expires_at,applied,state_version
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (uuid4(),event.user_id,event.instance_id,source["conversation_id"],
         source["turn_id"],event.event_id,event_id,at,
         source["input_text"],signal.kind,signal.user_label,
         signal.confidence,expires,applied,
         state["version"]+1 if applied else state["version"]),
    )
    return applied


def read_emotion_state(
    connection:Connection,actor:TrustedActor,instance_id:UUID,
    *,at:datetime|None=None,
)->EmotionSnapshot:
    """Derived decay on read, never a write or an inferred user fact."""
    require_idle(connection)
    _require_actor(actor)
    now=at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("emotion snapshot requires timezone-aware date")
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT 1 FROM persona_instances WHERE user_id=%s AND id=%s",
            (actor.user_id,instance_id),
        )
        if cur.fetchone() is None:
            raise PermissionDenied("emotion snapshot outside authenticated persona scope")
        cur.execute(
            """SELECT * FROM persona_emotion_state
               WHERE user_id=%s AND instance_id=%s""",
            (actor.user_id,instance_id),
        )
        state=cur.fetchone()
        cur.execute(
            """SELECT user_affect_label,confidence,expires_at FROM emotion_appraisals
               WHERE user_id=%s AND instance_id=%s AND applied
                 AND user_affect_label IS NOT NULL AND expires_at>%s
               ORDER BY source_turn_at DESC,appraisal_id DESC LIMIT 1""",
            (actor.user_id,instance_id,now),
        )
        user_cue=cur.fetchone()
    baseline=(float(state["baseline_valence"]),
              float(state["baseline_arousal"]),
              float(state["baseline_engagement"])) if state else _BASELINE
    values=tuple(
        _decay(float(state[key]),base,state["last_source_at"],now)
        if state and state["last_source_at"] else base
        for key,base in zip(("valence","arousal","engagement"),baseline)
    )
    return EmotionSnapshot(
        user_id=actor.user_id,instance_id=instance_id,
        version=state["version"] if state else 0,
        valence=round(values[0],3),arousal=round(values[1],3),
        engagement=round(values[2],3),
        baseline_valence=baseline[0],baseline_arousal=baseline[1],
        baseline_engagement=baseline[2],
        last_source_at=state["last_source_at"] if state else None,
        tentative_user_affect=user_cue["user_affect_label"] if user_cue else None,
        affect_confidence=float(user_cue["confidence"]) if user_cue else None,
        affect_expires_at=user_cue["expires_at"] if user_cue else None,
    )
