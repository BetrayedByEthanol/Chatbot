"""Conversation-owned receipt-time snapshots; no presence or execution authority.

Timezone is explicit host metadata, not a location guess, model extraction or
a notification-delivery policy. Relative expressions are resolved against
the input receipt, never against a later retry or model inference timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


class InteractionAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID
    conversation_id: UUID
    at: AwareDatetime


class RelativeTime(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expression: str
    status: Literal[
        "DATE", "INSTANT", "UNKNOWN_TIMEZONE",
        "AMBIGUOUS_LOCAL_TIME", "NONEXISTENT_LOCAL_TIME",
    ]
    local_date: date | None = None
    instant: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_resolution(self):
        if (self.status=="INSTANT")!=(self.instant is not None):
            raise ValueError("only an unambiguous instant has an absolute timestamp")
        if (self.status=="DATE" and self.local_date is None):
            raise ValueError("calendar date requires a local date")
        return self


class TemporalContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: InteractionAnchor
    timezone_name: str | None
    local_receipt_at: AwareDatetime | None
    first_recorded: InteractionAnchor | None
    previous_message: InteractionAnchor | None
    previous_other_session: InteractionAnchor | None
    relative: RelativeTime | None
    basis: Literal["INGRESS", "LEGACY_READ"] = "INGRESS"

    def elapsed_seconds(self, anchor: InteractionAnchor | None) -> int | None:
        if anchor is None:
            return None
        # Compare instants in UTC, not local wall time across DST transitions.
        seconds=(self.receipt.at.astimezone(timezone.utc)
                 -anchor.at.astimezone(timezone.utc)).total_seconds()
        return int(seconds) if seconds>=0 else None


def validate_timezone(name: str | None) -> ZoneInfo | None:
    if name is None:
        return None
    if not isinstance(name,str) or not 1<=len(name)<=100:
        raise ValueError("timezone must be an explicit IANA identifier")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError,ValueError,KeyError) as exc:
        raise ValueError("unknown IANA timezone") from exc


_CALENDAR = re.compile(
    r"(?P<day>today|tomorrow|yesterday)(?: at (?P<hour>[01]\d|2[0-3]):"
    r"(?P<minute>[0-5]\d))?[.!]?", re.I,
)
_DATE_QUESTION = re.compile(
    r"what (?:date|day) (?:is|was|will be) (?P<day>today|tomorrow|yesterday)\?", re.I,
)
_DURATION = re.compile(r"in (?P<count>[1-9]\d{0,2}) (?P<unit>minutes?|hours?)[.!]?",re.I)
_DAYS = {"yesterday":-1,"today":0,"tomorrow":1}


def resolve_relative_time(text: str, receipt_at: datetime,
                          zone: ZoneInfo | None) -> RelativeTime | None:
    """Small, complete-expression grammar; no scheduling or intent admission.

    Calendar days are local dates, not assumed midnight instants. Durations
    are elapsed UTC time. Ambiguous folds and nonexistent gap wall times
    require clarification rather than silently choosing a DST policy.
    """
    if receipt_at.tzinfo is None or receipt_at.utcoffset() is None:
        raise ValueError("receipt instant must be timezone-aware")
    value=text.strip()
    if not 1<=len(value)<=160:
        return None
    duration=_DURATION.fullmatch(value)
    if duration:
        count=int(duration["count"])
        delta=(timedelta(minutes=count) if duration["unit"].lower().startswith("minute")
               else timedelta(hours=count))
        return RelativeTime(
            expression=value,status="INSTANT",
            instant=receipt_at.astimezone(timezone.utc)+delta,
        )
    match=_CALENDAR.fullmatch(value)
    question=_DATE_QUESTION.fullmatch(value) if match is None else None
    if match is None and question is None:
        return None
    if zone is None:
        return RelativeTime(expression=value,status="UNKNOWN_TIMEZONE")
    day=(receipt_at.astimezone(zone).date()
         +timedelta(days=_DAYS[(match or question)["day"].lower()]))
    if match is None or match["hour"] is None:
        return RelativeTime(expression=value,status="DATE",local_date=day)
    wall=datetime(day.year,day.month,day.day,int(match["hour"]),int(match["minute"]))
    candidates=set()
    for fold in (0,1):
        instant=wall.replace(tzinfo=zone,fold=fold).astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None)==wall:
            candidates.add(instant)
    if len(candidates)!=1:
        return RelativeTime(
            expression=value,local_date=day,
            status="AMBIGUOUS_LOCAL_TIME" if candidates else "NONEXISTENT_LOCAL_TIME",
        )
    return RelativeTime(
        expression=value,status="INSTANT",local_date=day,instant=candidates.pop(),
    )


def capture_temporal_context(cur, *, user_id: UUID, instance_id: UUID,
                             receipt: InteractionAnchor, text: str,
                             timezone_name: str | None,
                             basis: Literal["INGRESS","LEGACY_READ"]="INGRESS",
                             ) -> TemporalContext:
    """Read only previously committed primary-user inputs in the same scope.

    Receipt time is the database-generated conversation_turns.created_at.
    The previous session anchor is the most recent earlier input in another
    conversation, not a claimed logout/end time. Concurrent uncommitted
    conversations and unavailable legacy evidence are not guessed.
    """
    zone=validate_timezone(timezone_name)
    # Three index-eligible point/range reads in one SQL statement. Excluding
    # the current conversation may scan its recent rows; do not consult
    # lifecycle heartbeats, response times, outbox, or notification delivery.
    cur.execute(
        """
        (SELECT 'first' AS kind,turn_id,conversation_id,created_at AS at
         FROM conversation_turns
         WHERE user_id=%s AND instance_id=%s AND created_at<%s
           AND source_input_event_id IS NOT NULL
         ORDER BY created_at,turn_id LIMIT 1)
        UNION ALL
        (SELECT 'previous' AS kind,turn_id,conversation_id,created_at AS at
         FROM conversation_turns
         WHERE user_id=%s AND instance_id=%s AND created_at<%s
           AND source_input_event_id IS NOT NULL
         ORDER BY created_at DESC,turn_id DESC LIMIT 1)
        UNION ALL
        (SELECT 'other' AS kind,turn_id,conversation_id,created_at AS at
         FROM conversation_turns
         WHERE user_id=%s AND instance_id=%s AND created_at<%s
           AND source_input_event_id IS NOT NULL AND conversation_id<>%s
         ORDER BY created_at DESC,turn_id DESC LIMIT 1)
        """,
        (user_id,instance_id,receipt.at,user_id,instance_id,receipt.at,
         user_id,instance_id,receipt.at,receipt.conversation_id),
    )
    anchors={}
    for row in cur.fetchall():
        anchors[row["kind"]]=InteractionAnchor(
            turn_id=row["turn_id"],conversation_id=row["conversation_id"],at=row["at"],
        )
    return TemporalContext(
        receipt=receipt,timezone_name=timezone_name,
        local_receipt_at=receipt.at.astimezone(zone) if zone is not None else None,
        first_recorded=anchors.get("first",receipt),
        previous_message=anchors.get("previous"),
        previous_other_session=anchors.get("other"),
        relative=resolve_relative_time(text,receipt.at,zone),basis=basis,
    )


def temporal_from_anchor(cur, *, user_id: UUID, item, turn_id: UUID) -> TemporalContext:
    """Reuse the immutable snapshot, or qualify old pre-migration inputs."""
    stored=item["temporal_context"]
    if stored is not None:
        return TemporalContext.model_validate(stored)
    return capture_temporal_context(
        cur,user_id=user_id,instance_id=item["instance_id"],
        receipt=InteractionAnchor(turn_id=turn_id,conversation_id=item["conversation_id"],
                                  at=item["turn_at"]),
        text=item["input_text"],timezone_name=None,basis="LEGACY_READ",
    )


_TEMPORAL_CUE = re.compile(
    r"\b(?:today|tomorrow|yesterday|tonight|last (?:time|session|message|week)|"
    r"previous (?:session|message)|how long (?:ago|since|have)|"
    r"first (?:met|talked|interaction)|since we (?:last|first)|"
    r"current (?:date|time)|what (?:date|time|day) is it|"
    r"(?:next|last) (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"in \d+ (?:minutes?|hours?|days?|weeks?))\b",re.I,
)
_GREETING = re.compile(r"(?:hi|hello|hey|good morning|good evening|i'm back)[.!]?",re.I)


def temporal_context_block(text: str, context: TemporalContext, *, max_chars: int) -> str:
    """Only inject response-relevant time data; preserve provenance caveats."""
    value=text.strip().replace("’","'")
    if (context.relative is None and not _TEMPORAL_CUE.search(value)
            and _GREETING.fullmatch(value) is None):
        return ""
    parts=["Trusted receipt-time context (not a fresh clock reading): "+
           context.receipt.at.astimezone(timezone.utc).isoformat()+" UTC."]
    if context.local_receipt_at is None:
        parts.append("User timezone unknown; ask if local date/time is needed.")
    else:
        parts.append(f"Host-supplied timezone {context.timezone_name}; local receipt "
                     f"{context.local_receipt_at.isoformat()}.")
    if context.relative is not None:
        relative=context.relative
        if relative.status=="INSTANT":
            parts.append("Resolved relative instant: "+relative.instant.isoformat()+".")
        elif relative.status=="DATE":
            parts.append("Resolved local calendar date: "+relative.local_date.isoformat()+
                         "; no time of day specified.")
        else:
            parts.append("Relative expression unresolved ("+relative.status+
                         "); clarify timezone or local time if needed.")
    elif re.search(
        r"\b(?:today|tomorrow|yesterday|tonight|next|in \d+ (?:minutes?|hours?|days?|weeks?))\b",
        value,re.I,
    ):
        parts.append("No exact relative-time resolution recorded for this wording; "
                     "clarify an ambiguous date/time before treating it as definite.")
    if _GREETING.fullmatch(value) or re.search(
        r"\b(?:last|previous|since|first|how long)\b",value,re.I,
    ):
        for label,anchor in (
            ("Previous recorded message",context.previous_message),
            ("Last input in another conversation",context.previous_other_session),
            ("Earliest recorded interaction",context.first_recorded),
        ):
            seconds=context.elapsed_seconds(anchor)
            parts.append(f"{label}: {seconds} s before receipt." if seconds is not None
                         else f"{label}: unknown.")
    parts.append("Elapsed time is not user presence or permission to act.")
    block=" ".join(parts)+"\n"
    return block if len(block)<=max_chars else ""


# Explicit historical questions only. This is a retrieval window, not an
# event timestamp or a scheduling instruction.
_DATED_RECALL = re.compile(
    r"^what did (?:i|we) (?:say|tell you|discuss|mention) about "
    r"(?P<body>[^\r\n?]{1,300})\?$", re.I,
)
_PAST_PERIOD = re.compile(
    r"\b(?:yesterday|today|tomorrow|last (?:week|month|year|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday)|"
    r"(?:days?|weeks?|months?|years?|hours?|minutes?) ago|"
    r"\d{4}-\d{1,2}-\d{1,2}|a while ago|recently|"
    r"(?:earlier )?this (?:week|month|year)|"
    r"on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"on \d{1,2} (?:january|february|march|april|may|june|july|"
    r"august|september|october|november|december)|"
    r"(?:days?|weeks?|months?|years?) back)\b", re.I,
)
_PERIOD_SUFFIX = re.compile(
    r"(?P<topic>.+?)\s+(?P<period>yesterday|today|last week|last month|"
    r"on \d{4}-\d{2}-\d{2}|"
    r"(?:[1-9]\d?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve) (?:days?|weeks?) ago)$", re.I,
)
_NUMBERS = dict(zip(
    ("one","two","three","four","five","six","seven","eight","nine","ten","eleven","twelve"),
    range(1,13),
))


@dataclass(frozen=True)
class HistoricalWindow:
    status: Literal["RESOLVED","UNKNOWN_TIMEZONE","AMBIGUOUS_PERIOD","UNSUPPORTED_PERIOD"]
    topic: str = ""
    label: str = ""
    start: datetime | None = None
    end: datetime | None = None  # exclusive


def _unique_midnight(day: date, zone: ZoneInfo) -> datetime | None:
    wall=datetime.combine(day,datetime.min.time())
    candidates=set()
    for fold in (0,1):
        instant=wall.replace(tzinfo=zone,fold=fold).astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None)==wall:
            candidates.add(instant)
    return next(iter(candidates)) if len(candidates)==1 else None


def historical_recall_window(text: str, context: TemporalContext) -> HistoricalWindow | None:
    """Resolve one explicit topic/date question against immutable receipt time.

    Days use local calendar dates; weeks use Monday-Sunday calendar weeks.
    N weeks ago means the calendar week containing local receipt minus N
    weeks, not a guessed event day. Unsupported or competing period wording
    never widens this recognized request to an undated search.
    """
    request=_DATED_RECALL.fullmatch(text.strip())
    if request is None or not _PAST_PERIOD.search(request["body"]):
        return None
    match=_PERIOD_SUFFIX.fullmatch(request["body"])
    if match is None:
        return HistoricalWindow("UNSUPPORTED_PERIOD")
    topic=match["topic"].strip()
    period=match["period"].casefold()
    if (_PAST_PERIOD.search(topic) or re.search(r"\b(?:or|and)\b",topic,re.I)
            or not topic or topic.casefold() in ("it","that","this","the topic","my plans")):
        return HistoricalWindow("AMBIGUOUS_PERIOD")
    if context.timezone_name is None:
        return HistoricalWindow("UNKNOWN_TIMEZONE",topic)
    zone=validate_timezone(context.timezone_name)
    receipt=context.receipt.at.astimezone(timezone.utc)
    today=receipt.astimezone(zone).date()
    try:
        if period=="today":
            start_day=today
            end_day=today+timedelta(days=1)
        elif period=="yesterday":
            start_day=today-timedelta(days=1)
            end_day=today
        elif period=="last week":
            end_day=today-timedelta(days=today.weekday())
            start_day=end_day-timedelta(weeks=1)
        elif period=="last month":
            end_day=today.replace(day=1)
            start_day=(end_day-timedelta(days=1)).replace(day=1)
        elif period.startswith("on "):
            start_day=date.fromisoformat(period[3:])
            end_day=start_day+timedelta(days=1)
        else:
            number,unit,_=period.split()
            count=int(number) if number.isdigit() else _NUMBERS[number]
            if unit.startswith("week"):
                if count>52:
                    return HistoricalWindow("UNSUPPORTED_PERIOD",topic)
                day=today-timedelta(weeks=count)
                start_day=day-timedelta(days=day.weekday())
                end_day=start_day+timedelta(weeks=1)
            else:
                start_day=today-timedelta(days=count)
                end_day=start_day+timedelta(days=1)
        start=_unique_midnight(start_day,zone)
        end=_unique_midnight(end_day,zone)
    except (OverflowError,ValueError):
        return HistoricalWindow("UNSUPPORTED_PERIOD",topic)
    if start is None or end is None:
        return HistoricalWindow("AMBIGUOUS_PERIOD",topic)
    end=min(end,receipt)
    if start>=end:
        return HistoricalWindow("UNSUPPORTED_PERIOD",topic)
    label=f"{start_day.isoformat()} through {(end_day-timedelta(days=1)).isoformat()} ({zone.key}; local calendar dates)"
    return HistoricalWindow("RESOLVED",topic,label,start,end)
