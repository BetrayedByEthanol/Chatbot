"""Bounded identity vocabulary and exact conversational reference grammar.

No database dependency, extraction, public-figure lookup, or permission.
Names are user-supplied labels, not globally unique identity keys.
"""
from dataclasses import dataclass
import re
from typing import Literal

EntityKind = Literal["PERSON","PLACE","OBJECT"]
EntityRelation = Literal[
    "FRIEND","COLLEAGUE","SIBLING","FAVORITE_PLACE",
    "BICYCLE","GUITAR","CAMERA","BOOK","OBJECT",
]
RELATIONS: dict[str, tuple[EntityKind,str]] = {
    "FRIEND":("PERSON","friend"),
    "COLLEAGUE":("PERSON","colleague"),
    "SIBLING":("PERSON","sibling"),
    "FAVORITE_PLACE":("PLACE","favorite place"),
    "BICYCLE":("OBJECT","bicycle"),
    "GUITAR":("OBJECT","guitar"),
    "CAMERA":("OBJECT","camera"),
    "BOOK":("OBJECT","book"),
    "OBJECT":("OBJECT","object"),
}
_NAME = re.compile(r"[A-Za-z][A-Za-z'-]{0,23}(?: [A-Za-z][A-Za-z'-]{0,23}){0,2}")
_BLOCK = re.compile(
    r"\b(?:my|your|our|their|who|what|which|they|them|he|she|him|her|it|"
    r"this|that|and|or|not|maybe|password|secret|token|account|address|"
    r"email|phone|medical|diagnosis|hypothetical)\b",re.I,
)
_LABEL_TO_RELATION={label:key for key,(_,label) in RELATIONS.items()}
_QUALIFIED=re.compile(
    r"do you remember my (?P<relation>"
    +"|".join(re.escape(label) for label in sorted(_LABEL_TO_RELATION,key=len,reverse=True))
    +r") (?P<name>.+)\?",re.I,
)
_NAMED=re.compile(r"do you remember (?P<kind>who|what) (?P<name>.+) is\?",re.I)
_TO_ME=re.compile(r"who is (?P<name>.+) to me\?",re.I)
_PRONOUN={
    "do you remember who they are?":"PERSON",
    "do you remember who he is?":"PERSON",
    "do you remember who she is?":"PERSON",
    "how do i know them?":"PERSON",
    "do you remember what it is?":"NONPERSON",
}


def normalize_name(value: str) -> str:
    if not isinstance(value,str):
        raise ValueError("entity name must be text")
    # Match the punctuation accepted by the response dispatcher. Keep the
    # original display name and source evidence intact; normalize lookup keys.
    value=value.strip().replace("’", "'")
    if not 1<=len(value)<=64 or _NAME.fullmatch(value) is None or _BLOCK.search(value):
        raise ValueError("unsupported entity name")
    return value.casefold()


@dataclass(frozen=True)
class EntityReference:
    name_key: str | None = None
    kind: Literal["PERSON","NONPERSON","ANY"]="ANY"
    relation: EntityRelation | None = None
    adjacent: bool=False


def entity_reference(text: str) -> EntityReference | None:
    """Only standalone private recollection questions; never generic 'Who is X?'."""
    if not isinstance(text,str) or not 1<=len(text)<=180:
        return None
    value=text.strip()
    if value.casefold() in _PRONOUN:
        return EntityReference(kind=_PRONOUN[value.casefold()],adjacent=True)
    match=_QUALIFIED.fullmatch(value)
    if match is not None:
        relation=_LABEL_TO_RELATION[match["relation"].casefold()]
        try:
            return EntityReference(name_key=normalize_name(match["name"]),relation=relation)
        except ValueError:
            return None
    match=_NAMED.fullmatch(value)
    if match is not None:
        kind="PERSON" if match["kind"].casefold()=="who" else "NONPERSON"
    else:
        match=_TO_ME.fullmatch(value)
        kind="PERSON"
    if match is None:
        return None
    try:
        return EntityReference(name_key=normalize_name(match["name"]),kind=kind)
    except ValueError:
        return None


# Whole-source reports only. A bare first-person relation with a proper
# name is a user report, not verified identity. "Another" explicitly
# requests a distinct identity; "Actually" explicitly corrects a relation.
_REPORT = re.compile(
    r"(?:(?P<marker>remember that|actually,?|another) +)?"
    r"(?P<name>[^.\r\n?!]+?) is my (?P<relation>"
    +"|".join(re.escape(label) for label in sorted(_LABEL_TO_RELATION,key=len,reverse=True))
    +r")\.?", re.I,
)
_NON_NAME_WORDS = re.compile(
    r"\b(?:i|we|you|a|an|the|another|actually|remember|please|if|"
    r"perhaps|probably|possibly|likely|suppose|imagine|pretend|formerly|example|"
    r"fictional|would|could|might|was|is|joke|yes|no|never|someone|somebody)\b",re.I,
)
_PROPER_NAME = re.compile(r"[A-Z][A-Za-z'’-]*(?: [A-Z][A-Za-z'’-]*){0,2}")
_CLARIFICATION = re.compile(
    r"(?:my|the) (?P<relation>"
    +"|".join(re.escape(label) for label in sorted(_LABEL_TO_RELATION,key=len,reverse=True))
    +r")[.!]?",re.I,
)


@dataclass(frozen=True)
class EntityReport:
    name: str
    relation: EntityRelation
    action: Literal["REPORT","DISTINCT","CORRECT"]


def entity_report(text: str) -> EntityReport | None:
    """Bounded explicit relationship reports; never quoted or qualified claims."""
    if not isinstance(text,str) or not 1<=len(text)<=180:
        return None
    match=_REPORT.fullmatch(text.strip())
    if match is None:
        return None
    name=match["name"]
    marker=(match["marker"] or "").casefold().rstrip(",")
    try:
        normalize_name(name)
    except ValueError:
        return None
    if _NON_NAME_WORDS.search(name):
        return None
    # For unmarked speech require a name-like label, not arbitrary nouns.
    # Explicit remember/correction/distinction can use lowercase labels.
    if not marker and _PROPER_NAME.fullmatch(name) is None:
        return None
    action="CORRECT" if marker=="actually" else "DISTINCT" if marker=="another" else "REPORT"
    return EntityReport(name,_LABEL_TO_RELATION[match["relation"].casefold()],action)


def entity_clarification_relation(text: str) -> EntityRelation | None:
    """A possible short selection, not a route or permission on its own."""
    if not isinstance(text,str) or not 1<=len(text)<=64:
        return None
    match=_CLARIFICATION.fullmatch(text.strip())
    return _LABEL_TO_RELATION[match["relation"].casefold()] if match else None
