"""P5-04 durable stop of one voice response; never cancel unrelated tasks.

The trusted output host must immediately stop/flush the physical player
before this short transaction, and signal the in-memory generator to stop.
Database stop barriers fence in-flight TTS after its external inference.
"""

from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from .contracts import TrustedActor
from .conversation import _event, _require_actor, _selection
from .operation_contracts import require_idle
from .runtime import PermissionDenied, StaleSelection
from .speech_output_contracts import SpeechDeliveryConflict
from .speech_output_state import _clip_event, _latest_response, _response
from .turn_taking_contracts import SpeechInterruptCommand, SpeechInterruptResult


def interrupt_speech_response(
    connection: Connection, actor: TrustedActor,
    command: SpeechInterruptCommand,
) -> SpeechInterruptResult:
    """Atomically stop new clips and mark unplayed clips interrupted.

    The one-row barrier is committed before returning; clip preparation and
    playback acknowledgement serialize on the same conversation row. Already
    PLAYED clips preserve evidence of actual progress; the complete response
    remains GENERATED unless it had been fully delivered before this call.
    """
    require_idle(connection)
    _require_actor(actor)
    if type(command) is not SpeechInterruptCommand:
        raise TypeError("expected trusted speech interruption command")
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cur:
            response = _response(cur,actor,command.response_id)
            if response["instance_id"] != command.instance_id:
                raise PermissionDenied("interruption cannot target another persona")
            _selection(cur,actor,command.instance_id,command.expected_generation)
            if response["selection_generation"] != command.expected_generation:
                raise StaleSelection("speech interruption belongs to old generation")
            cur.execute(
                "SELECT 1 FROM conversations WHERE conversation_id=%s FOR UPDATE",
                (response["conversation_id"],),
            )
            if not _latest_response(cur,response):
                raise StaleSelection("speech was superseded by a newer user turn")
            cur.execute(
                "SELECT delivery_status FROM conversation_responses "
                "WHERE response_id=%s FOR UPDATE",
                (command.response_id,),
            )
            if cur.fetchone()["delivery_status"] != "GENERATED":
                raise SpeechDeliveryConflict("speech response is already delivered")
            cur.execute(
                "SELECT * FROM speech_output_interruptions WHERE response_id=%s",
                (command.response_id,),
            )
            prior=cur.fetchone()
            if prior is not None:
                if (prior["output_channel_id"]!=command.output_channel_id
                        or prior["input_channel_id"]!=command.input_channel_id
                        or prior["utterance_id"]!=command.utterance_id
                        or prior["reason"]!=command.reason):
                    raise SpeechDeliveryConflict(
                        "speech response already interrupted by another origin"
                    )
                return SpeechInterruptResult(
                    interruption_id=prior["interruption_id"],
                    response_id=command.response_id,
                    interrupted_clips=0,replayed=True,
                )
            cur.execute(
                "SELECT DISTINCT channel_id FROM speech_clips WHERE response_id=%s",
                (command.response_id,),
            )
            channels={row["channel_id"] for row in cur.fetchall()}
            if channels and channels!={command.output_channel_id}:
                raise PermissionDenied("output channel mismatch for response clips")
            interruption_id=uuid4()
            cur.execute(
                """INSERT INTO speech_output_interruptions(
                       interruption_id,response_id,user_id,instance_id,
                       selection_generation,output_channel_id,input_channel_id,
                       utterance_id,reason
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (interruption_id,command.response_id,actor.user_id,
                 command.instance_id,command.expected_generation,
                 command.output_channel_id,command.input_channel_id,
                 command.utterance_id,command.reason),
            )
            # Per-response serialization already acquired. Use deterministic
            # clip order to coordinate overlapping client callbacks safely.
            cur.execute(
                """UPDATE speech_clips SET status='INTERRUPTED',
                       updated_at=clock_timestamp()
                   WHERE response_id=%s AND status IN ('GENERATED','QUEUED','STARTED')
                   RETURNING *""",
                (command.response_id,),
            )
            interrupted=sorted(cur.fetchall(),key=lambda row:row["clip_index"])
            for clip in interrupted:
                _clip_event(cur,response,clip,status="INTERRUPTED",
                            source_id=actor.source_id,trust="AUTHENTICATED_USER")
            _event(
                cur,user_id=actor.user_id,instance_id=command.instance_id,
                conversation_id=response["conversation_id"],
                event_type="communication.speech_output.interrupted.v1",
                source_id=actor.source_id,trust_level="AUTHENTICATED_USER",
                detail={
                    "interruption_id":str(interruption_id),
                    "response_id":str(command.response_id),
                    "output_channel_id":command.output_channel_id,
                    "input_channel_id":command.input_channel_id,
                    "utterance_id":str(command.utterance_id) if command.utterance_id else None,
                    "reason":command.reason,
                    "interrupted_clips":len(interrupted),
                },
                correlation_id=interruption_id,
            )
            return SpeechInterruptResult(
                interruption_id=interruption_id,response_id=command.response_id,
                interrupted_clips=len(interrupted),
            )
