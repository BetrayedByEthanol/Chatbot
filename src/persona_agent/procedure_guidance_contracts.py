"""Whole-turn guided procedure dialogue; no implicit tool or completion authority."""
from dataclasses import dataclass
import re
ROUTE_ID="conversation.procedure_guidance.v1"
@dataclass(frozen=True)
class GuidanceAction:
    kind: str
    title: str | None=None
    detail: str | None=None

_NAMED=re.compile(r"(?P<action>guide me through|continue|resume|pause|cancel) procedure:\s*(?P<title>[^.!?;\r\n\t]{1,120})[.!]?",re.I)
_ACTIONS={
    "next":"NEXT","continue":"NEXT","repeat":"REPEAT","back":"BACK","skip":"SKIP",
    "clarify":"CLARIFY","pause":"PAUSE","resume":"RESUME","cancel":"CANCEL",
    "next step":"NEXT","repeat step":"REPEAT","repeat this step":"REPEAT",
    "previous step":"BACK","back one step":"BACK","skip step":"SKIP",
    "skip this step":"SKIP","clarify step":"CLARIFY","clarify this step":"CLARIFY",
    "pause this procedure":"PAUSE","resume this procedure":"RESUME","cancel this procedure":"CANCEL",
    "confirm procedure pause":"CONFIRM_PAUSE","confirm procedure resume":"CONFIRM_RESUME",
    "confirm procedure cancellation":"CONFIRM_CANCEL",
    "i tried this step":"ATTEMPTED","i completed this step":"USER_REPORTED_DONE",
    "i confirm this step passed its check":"USER_CONFIRMED",
}
_OBSTACLE=re.compile(r"this step (?P<kind>failed|is blocked): (?P<detail>[^;\r\n\t]{10,250})",re.I)

def parse_guidance(text: str) -> GuidanceAction | None:
    if not isinstance(text,str) or not 1<=len(text)<=300:
        return None
    text=text.strip().replace("’","'")
    if any(c in text for c in ("\r","\n","\t")):
        return None
    named=_NAMED.fullmatch(text)
    if named:
        title=named["title"].strip()
        if not title:
            return None
        kind={"guide me through":"NEXT","continue":"NEXT","resume":"RESUME",
              "pause":"PAUSE","cancel":"CANCEL"}[named["action"].casefold()]
        return GuidanceAction(kind,title=title)
    obstacle=_OBSTACLE.fullmatch(text)
    if obstacle:
        detail=obstacle["detail"].strip()
        if len(detail)<10:
            return None
        return GuidanceAction("FAILED" if obstacle["kind"].casefold()=="failed" else "BLOCKED",detail=detail)
    # Questions, quoted commands, hypotheticals, and bare 'yes/done' abstain.
    kind=_ACTIONS.get(text.casefold().rstrip(".!"))
    return GuidanceAction(kind) if kind else None

CONFIRMATION={"PAUSE":"Confirm procedure pause.","RESUME":"Confirm procedure resume.",
              "CANCEL":"Confirm procedure cancellation."}
STEP_REPORTS=frozenset({"ATTEMPTED","USER_REPORTED_DONE","USER_CONFIRMED","FAILED","BLOCKED"})
