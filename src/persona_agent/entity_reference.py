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
    value=value.strip()
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
