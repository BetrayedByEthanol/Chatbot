"""P5-03 durable speech clip delivery, using the existing Conversation owner.

Only clip metadata/status and evidence-linked events are durable; audio stays
transient. No TTS inference or external playback occurs under DB locks.
"""

from collections.abc import Iterator
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _event, _selection, _require_actor
from .operation_contracts import require_idle
from .runtime import IdempotencyConflict, PermissionDenied, StaleSelection
from .speech_output_audio import split_speech_text, validate_wav_pcm16
from .speech_output_contracts import (
    GeneratedSpeechClip, PlaybackAck, SpeechClipCommand, SpeechClipResult,
    SpeechDeliveryConflict, SpeechOutputTarget, SpeechSynthesizer,
)


def _response(cur, actor: TrustedActor, response_id: UUID):
    cur.execute(
        """SELECT response_id,conversation_id,turn_id,user_id,instance_id,
                  selection_generation,context_revision,response_text,
                  delivery_status
           FROM conversation_responses
           WHERE response_id=%s AND user_id=%s""",
        (response_id, actor.user_id),
    )
    row = cur.fetchone()
    if row is None:
        raise PermissionDenied("response outside authenticated scope")
    return row


def _is_current(cur, response):
    cur.execute(
        """SELECT instance_id,generation,mode FROM persona_selection
           WHERE user_id=%s FOR SHARE""",
        (response["user_id"],),
    )
    selected = cur.fetchone()
    return bool(
        selected and selected["instance_id"] == response["instance_id"]
        and selected["generation"] == response["selection_generation"]
        and selected["mode"] == "FOREGROUND"
    )


def _latest_response(cur, response):
    cur.execute(
        "SELECT last_input_turn_id FROM conversations "
        "WHERE conversation_id=%s AND user_id=%s AND instance_id=%s",
        (response["conversation_id"], response["user_id"], response["instance_id"]),
    )
    current = cur.fetchone()
    return bool(current and current["last_input_turn_id"] == response["turn_id"])


def _clip_result(row, *, delivered=False, replayed=False):
    return SpeechClipResult(
        clip_id=row["clip_id"], response_id=row["response_id"],
        clip_index=row["clip_index"], clip_count=row["clip_count"],
        channel_id=row["channel_id"], playback_token=row["playback_token"],
        status=row["status"], response_delivered=delivered, replayed=replayed,
    )


def _clip_event(cur, response, row, *, status, source_id, trust):
    return _event(
        cur,
        user_id=response["user_id"],instance_id=response["instance_id"],
        conversation_id=response["conversation_id"],
        event_type="communication.speech_clip." + status.lower() + ".v1",
        source_id=source_id, trust_level=trust,
        detail={
            "clip_id":str(row["clip_id"]), "response_id":str(row["response_id"]),
            "channel_id":row["channel_id"], "clip_index":row["clip_index"],
            "clip_count":row["clip_count"], "status":status,
        },
        correlation_id=row["clip_id"],
    )


def prepare_speech_clip(
    connection: Connection, actor: TrustedActor,
    command: SpeechClipCommand,
) -> SpeechClipResult:
    """Admit one validated clip after synthesis, fenced to latest Foreground.

    The host must authorize the channel. Repeating the same index with
    conflicting audio metadata is rejected; never silently replace user audio.
    """
    require_idle(connection)
    _require_actor(actor)
    if command.clip_index >= command.clip_count:
        raise ValueError("clip index outside total count")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            response = _response(cur, actor, command.response_id)
            _selection(cur,actor,response["instance_id"],response["selection_generation"])
            cur.execute(
                "SELECT * FROM conversations WHERE conversation_id=%s FOR UPDATE",
                (response["conversation_id"],),
            )
            if not _latest_response(cur,response):
                raise StaleSelection("speech response was superseded by newer input")
            cur.execute(
                "SELECT delivery_status FROM conversation_responses "
                "WHERE response_id=%s FOR UPDATE",
                (command.response_id,),
            )
            if cur.fetchone()["delivery_status"] != "GENERATED":
                raise SpeechDeliveryConflict("response already marked delivered")
            cur.execute(
                "SELECT * FROM speech_clips WHERE response_id=%s AND clip_index=%s",
                (command.response_id, command.clip_index),
            )
            existing = cur.fetchone()
            if existing is not None:
                if any([
                    existing["channel_id"] != command.channel_id,
                    existing["clip_count"] != command.clip_count,
                    existing["sample_rate_hz"] != command.sample_rate_hz,
                    existing["duration_ms"] != command.duration_ms,
                    existing["payload_sha256"] != command.payload_sha256,
                ]):
                    raise IdempotencyConflict("speech clip index reused with changed audio")
                return _clip_result(existing, replayed=True)
            cur.execute(
                "SELECT channel_id,clip_count FROM speech_clips "
                "WHERE response_id=%s LIMIT 1",
                (command.response_id,),
            )
            first = cur.fetchone()
            if first is not None and (
                first["channel_id"] != command.channel_id
                or first["clip_count"] != command.clip_count
            ):
                raise SpeechDeliveryConflict("response audio target/count is immutable")
            clip_id,token=uuid4(),uuid4()
            cur.execute(
                """INSERT INTO speech_clips(
                       clip_id,response_id,user_id,instance_id,
                       selection_generation,response_revision,channel_id,
                       clip_index,clip_count,playback_token,format,
                       sample_rate_hz,duration_ms,payload_sha256
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                             'WAV_PCM16_MONO',%s,%s,%s)
                   RETURNING *""",
                (clip_id,command.response_id,actor.user_id,response["instance_id"],
                 response["selection_generation"],response["context_revision"],
                 command.channel_id,command.clip_index,command.clip_count,token,
                 command.sample_rate_hz,command.duration_ms,command.payload_sha256),
            )
            created=cur.fetchone()
            _clip_event(cur,response,created,status="GENERATED",
                        source_id=actor.source_id,trust="AUTHENTICATED_USER")
            return _clip_result(created)


def stream_response_speech(
    connection: Connection, actor: TrustedActor, response_id: UUID,
    target: SpeechOutputTarget, synthesizer: SpeechSynthesizer,
) -> Iterator[GeneratedSpeechClip]:
    """Synthesize a bounded response in promptly dispatchable audio clips.

    Each yield is a whole validated PCM16 WAV clip; the host can queue/play it
    while the next segment is still synthesizing. No claims of sample-level
    provider streaming are made. Call from a bounded executor, NOT a DB worker
    transaction or an unbounded thread per speech segment.
    """
    require_idle(connection)
    _require_actor(actor)
    if type(target) is not SpeechOutputTarget:
        raise TypeError("target must be a trusted output-channel binding")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            response = _response(cur, actor, response_id)
            _selection(cur,actor,response["instance_id"],response["selection_generation"])
            if not _latest_response(cur,response):
                raise StaleSelection("cannot synthesize a superseded response")
            if response["delivery_status"] != "GENERATED":
                raise SpeechDeliveryConflict("response already delivered")
            text = response["response_text"]
    segments = split_speech_text(text)
    for index, segment in enumerate(segments):
        # External computation takes place only after the read tx commits.
        wav = synthesizer.synthesize_wav(segment)
        info = validate_wav_pcm16(wav)
        record = prepare_speech_clip(
            connection,actor,SpeechClipCommand(
                response_id=response_id,clip_index=index,clip_count=len(segments),
                channel_id=target.channel_id,sample_rate_hz=info.sample_rate_hz,
                duration_ms=info.duration_ms,payload_sha256=info.payload_sha256,
            ),
        )
        if record.status in ("GENERATED",):
            yield GeneratedSpeechClip(record=record,audio_wav=wav)
        # Replays for queued/terminal clips must not trigger duplicate playback.


_NEXT = {
    "GENERATED": frozenset({"QUEUED","FAILED"}),
    "QUEUED": frozenset({"STARTED","INTERRUPTED","FAILED"}),
    "STARTED": frozenset({"PLAYED","INTERRUPTED","FAILED"}),
}


def acknowledge_speech_clip(
    connection: Connection, actor: TrustedActor, ack: PlaybackAck,
) -> SpeechClipResult:
    """Correlated playback acknowledgment; never marks interrupted audio heard.

    Post-switch callbacks can record historical clip state, but NEVER restore
    persona authority or mark the conversation response delivered.
    """
    require_idle(connection)
    _require_actor(actor)
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM speech_clips
                   WHERE clip_id=%s AND response_id=%s AND user_id=%s
                     AND channel_id=%s AND playback_token=%s""",
                (ack.clip_id,ack.response_id,actor.user_id,
                 ack.channel_id,ack.playback_token),
            )
            located=cur.fetchone()
            if located is None:
                raise PermissionDenied("playback acknowledgment outside scoped clip")
            response=_response(cur,actor,ack.response_id)
            # Lock order: selection -> conversation -> response -> clip.
            current_selection=_is_current(cur,response)
            cur.execute(
                "SELECT * FROM conversations WHERE conversation_id=%s FOR UPDATE",
                (response["conversation_id"],),
            )
            current_dialogue=_latest_response(cur,response)
            cur.execute(
                "SELECT delivery_status FROM conversation_responses "
                "WHERE response_id=%s FOR UPDATE",
                (ack.response_id,),
            )
            response_delivery=cur.fetchone()["delivery_status"]
            cur.execute("SELECT * FROM speech_clips WHERE clip_id=%s FOR UPDATE",
                        (ack.clip_id,))
            clip=cur.fetchone()
            if clip["status"]==ack.status:
                return _clip_result(
                    clip,delivered=(response_delivery=="DELIVERED"),replayed=True,
                )
            if ack.status not in _NEXT.get(clip["status"],frozenset()):
                raise SpeechDeliveryConflict(
                    f"invalid speech playback transition {clip['status']} -> {ack.status}"
                )
            # A late user command or persona switch cannot start or queue
            # *new* speech; a late PLAYED/INTERRUPTED/FAILED status remains
            # useful provenance for a playback that had already started.
            if ack.status in ("QUEUED","STARTED") and (
                not current_selection or not current_dialogue
            ):
                raise StaleSelection("cannot begin stale speech playback")
            cur.execute(
                """UPDATE speech_clips SET status=%s,updated_at=clock_timestamp(),
                      queued_at=CASE WHEN %s='QUEUED'
                          THEN clock_timestamp() ELSE queued_at END,
                      started_at=CASE WHEN %s='STARTED'
                          THEN clock_timestamp() ELSE started_at END,
                      played_at=CASE WHEN %s='PLAYED'
                          THEN clock_timestamp() ELSE played_at END
                   WHERE clip_id=%s RETURNING *""",
                (ack.status,ack.status,ack.status,ack.status,ack.clip_id),
            )
            updated=cur.fetchone()
            _clip_event(cur,response,updated,status=ack.status,
                        source_id=actor.source_id,trust="AUTHENTICATED_USER")
            delivered=response_delivery=="DELIVERED"
            if (ack.status=="PLAYED" and not delivered and current_selection
                    and current_dialogue):
                cur.execute(
                    "SELECT count(*) AS total,"
                    "count(*) FILTER (WHERE status='PLAYED') AS played,"
                    "max(clip_count) AS expected "
                    "FROM speech_clips WHERE response_id=%s",
                    (ack.response_id,),
                )
                counts=cur.fetchone()
                if counts["total"]==counts["expected"]==counts["played"]:
                    cur.execute(
                        """UPDATE conversation_responses
                           SET delivery_status='DELIVERED',
                               delivered_at=clock_timestamp()
                           WHERE response_id=%s AND delivery_status='GENERATED'
                           RETURNING response_id""",
                        (ack.response_id,),
                    )
                    if cur.fetchone() is not None:
                        cur.execute(
                            "UPDATE conversations SET revision=revision+1,"
                            "updated_at=clock_timestamp() WHERE conversation_id=%s",
                            (response["conversation_id"],),
                        )
                        _event(
                            cur,user_id=actor.user_id,instance_id=response["instance_id"],
                            conversation_id=response["conversation_id"],
                            event_type="conversation.response.delivered.v1",
                            source_id=actor.source_id,trust_level="AUTHENTICATED_USER",
                            detail={
                                "response_id":str(ack.response_id),
                                "turn_id":str(response["turn_id"]),
                                "output_modality":"VOICE",
                            },
                        )
                        delivered=True
            return _clip_result(updated,delivered=delivered)


def timeout_stale_speech_clips(
    connection: Connection, *, max_age_seconds: int=120, limit: int=32,
) -> int:
    """Trusted host periodic check; uses existing worker/scheduler, not a new one.

    Timeout records a conservative lack of playback acknowledgment. It never
    selects a persona or marks a response as heard.
    """
    require_idle(connection)
    if not 1 <= max_age_seconds <= 86400 or not 1 <= limit <= 128:
        raise ValueError("invalid clip timeout budget")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT clip_id FROM speech_clips
                   WHERE status IN ('GENERATED','QUEUED','STARTED')
                     AND updated_at <= clock_timestamp()
                         - (%s * interval '1 second')
                   ORDER BY updated_at,clip_id LIMIT %s
                   FOR UPDATE SKIP LOCKED""",
                (max_age_seconds,limit),
            )
            clip_ids=[r["clip_id"] for r in cur.fetchall()]
            for clip_id in clip_ids:
                cur.execute(
                    "UPDATE speech_clips SET status='TIMEOUT',"
                    "updated_at=clock_timestamp() WHERE clip_id=%s RETURNING *",
                    (clip_id,),
                )
                row=cur.fetchone()
                cur.execute(
                    "SELECT conversation_id,turn_id,user_id,instance_id "
                    "FROM conversation_responses WHERE response_id=%s",
                    (row["response_id"],),
                )
                response=cur.fetchone()
                _clip_event(cur,response,row,status="TIMEOUT",
                            source_id="communication.timeout",trust="TRUSTED_PLATFORM")
            return len(clip_ids)
