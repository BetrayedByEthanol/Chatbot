"""Dependency-light explicit Memory grammar shared by the owner and offline diagnostics.

This module has NO Psycopg imports or domain write capability. Keep the
exact bounded, positive-allowlist parsing semantics from memory_extraction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .personal_meaning_grammar import (
    parse_personal_definition,definition_components,SAFE_CONCEPTS as _SAFE_MEANINGS,
)

# Only complete, first-person, single-sentence assertions. Broader natural-
# language extraction requires an independently validated model-candidate gate.
_FAVORITE = re.compile(r"my favorite ([a-z][a-z -]{1,30}) is (.+)", re.I)
_HOBBY = re.compile(r"my hobby is (.+)", re.I)
_PET = re.compile(r"my (dog|cat)(?:'s name is| is named) (.+)", re.I)
_PREFER = re.compile(r"i prefer (.+?) over ([a-z][a-z -]{1,30})", re.I)
# High-precision conversational self-reports, not model-inferred hobbies or
# episodic observations. Only exact positive-allowlisted activities qualify.
# A negative assertion occupies the same slot and must go through the
# existing versioned conflict approval when a positive memory already exists.
_ENJOY_ACTIVITY = re.compile(
    r"i (?:really )?(?:enjoy|like|love) (?P<activity>[a-z][a-z -]{1,35})\.?",
    re.I,
)
_NOT_ENJOY_ACTIVITY = re.compile(
    r"i (?:(?:no longer|do not|don't) (?:enjoy|like)) "
    r"(?P<activity>[a-z][a-z -]{1,35}?)(?: anymore)?\.?",
    re.I,
)
_BLOCKED = re.compile(
    r"\b(address|password|pin|secret|ssn|bank|credit|account|salary|income|"
    r"health|medical|diagnos\w*|disease|medication|pregnan\w*|therapy|"
    r"religio\w*|politic\w*|ethnic\w*|sexual\w*|gender|birthday|"
    r"birthdate|age|phone|email|postcode|zip code|location|"
    r"maybe|perhaps|probably|sometimes|used to|not really|"
    r"someone|hypothetic\w*|for example|joking|sarcas\w*)\b",
    re.I,
)
_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Positive, deliberately small, non-sensitive domain vocabulary. A syntax
# match alone never authorizes admission as NORMAL privacy memory. Expand
# categories only with privacy evaluation and regression evidence.
_SAFE_FAVORITES = {
    "drink": frozenset({"tea","coffee","water","milk","juice","lemonade"}),
    "food": frozenset({"pizza","pasta","soup","salad","rice","bread","sushi"}),
    "color": frozenset({"red","blue","green","yellow","orange","purple",
                        "pink","black","white","brown","grey","gray"}),
}
_SAFE_HOBBIES = frozenset({
    "gardening","cycling","reading","painting","drawing","knitting",
    "hiking","walking","jogging","swimming","photography","cooking",
    "baking","chess","woodworking",
})
_SAFE_PET_NAMES = frozenset({
    "milo","luna","bella","max","charlie","buddy","daisy","coco",
    "rocky","lucy","oliver","leo","molly",
})
_SAFE_PREFERENCES = frozenset().union(
    *tuple(_SAFE_FAVORITES.values()),_SAFE_HOBBIES,
)


def _supported_safe_claim(kind: str, predicate: str, value: str) -> bool:
    """Positive allowlist for BOTH the subject topic and its reported value.

    Never use a blacklist or a free-text syntax match as authorization to
    write NORMAL-privacy authoritative memories.
    """
    term=value.casefold()
    if kind=="PREFERENCE" and predicate.startswith("favorite_"):
        topic=predicate.removeprefix("favorite_")
        return term in _SAFE_FAVORITES.get(topic,())
    if kind=="PREFERENCE" and predicate.startswith("prefer_over_"):
        alternative=predicate.removeprefix("prefer_over_").replace("_"," ")
        return term in _SAFE_PREFERENCES and alternative in _SAFE_PREFERENCES
    if kind=="PREFERENCE" and predicate.startswith("enjoys_"):
        activity=predicate.removeprefix("enjoys_").replace("_"," ")
        return term in {"yes","no"} and activity in _SAFE_HOBBIES
    if kind=="FACT" and predicate=="hobby":
        return term in _SAFE_HOBBIES
    if kind=="FACT" and predicate in ("pet_dog_name","pet_cat_name"):
        return term in _SAFE_PET_NAMES
    if kind=="PERSONAL_MEANING" and predicate.startswith("meaning_"):
        topic=predicate.removeprefix("meaning_")
        return bool(definition_components(topic.replace("_"," "),value))
    return False


@dataclass(frozen=True)
class ExplicitClaim:
    memory_kind: str
    subject_key: str
    predicate_key: str
    value_text: str
    source_quote: str
    confidence: float = 0.97


def _slug(value: str) -> str:
    return re.sub(r"\s+", "_", value.strip().casefold())


def _conversational_activity_preference(text: str) -> ExplicitClaim | None:
    """Bounded explicit user-report bridge, not interpretation of intent.

    A user saying they enjoy an allowlisted activity reports a preference,
    not a confirmed habitual practice or Goal. Time-qualified, speculative,
    quoted and multi-activity statements deliberately abstain.
    """
    statement=text.strip()
    if (not 8<=len(statement)<=180
            or _BLOCKED.search(statement)
            or any(c in statement for c in ('\n','\r','"',';','?',':','!','[',']','{','}','<','>'))):
        return None
    negative=_NOT_ENJOY_ACTIVITY.fullmatch(statement)
    positive=_ENJOY_ACTIVITY.fullmatch(statement) if negative is None else None
    matched=negative or positive
    if matched is None:
        return None
    activity=matched["activity"].strip().casefold()
    if activity not in _SAFE_HOBBIES:
        return None
    claim=ExplicitClaim(
        "PREFERENCE","user",f"enjoys_{_slug(activity)}",
        "no" if negative is not None else "yes",statement,
    )
    if not _supported_safe_claim(
        claim.memory_kind,claim.predicate_key,claim.value_text,
    ):
        return None
    return claim


def parse_explicit_claim(text: str) -> ExplicitClaim | None:
    """High-precision grammar, not a semantic fact verifier.

    Accept one short declarative self-report and preserve the exact source
    quote. Quotes, multiple sentences, questions, sensitive or speculative
    phrasing, and unsupported topics are deliberately left unextracted.
    """
    definition=parse_personal_definition(text)
    if definition is not None:
        return ExplicitClaim("PERSONAL_MEANING","user",definition.predicate,
                             definition.value,definition.source_quote)
    conversational=_conversational_activity_preference(text)
    if conversational is not None:
        return conversational
    statement=text.strip()
    if (len(statement)>320 or len(statement)<8
        or any(c in statement for c in ('\n','\r','"',"'",";",":","?","!","[","]","{","}","<",">"))
        or _BLOCKED.search(statement)):
        # Pet possessives with an apostrophe are handled below; other quoted
        # or possessive language is too ambiguous for this first-pass parser.
        if not (statement.casefold().startswith(("my dog's name is ",
                                                 "my cat's name is "))
                and not _BLOCKED.search(statement)
                and all(c not in statement for c in ('\n','\r','"',';',':','?','!','[',']','{','}','<','>'))
                and len(statement)<=320):
            return None
    normalized=re.sub(r"^actually,?\s+", "", statement, flags=re.I)
    if normalized.endswith("."):
        normalized=normalized[:-1]
    # Permit only the single optional "For me," introduction comma.
    if "." in normalized or "," in re.sub(r"^for me,", "", normalized, flags=re.I):
        return None
    patterns=(
        (_FAVORITE,"PREFERENCE","favorite_"),
        (_HOBBY,"FACT","hobby"),
        (_PET,"FACT","pet_"),
        (_PREFER,"PREFERENCE","prefer_over_"),
    )
    for pattern,kind,stem in patterns:
        match=pattern.fullmatch(normalized)
        if match is None:
            continue
        if pattern is _FAVORITE:
            predicate=stem+_slug(match[1])
            value=match[2].strip()
        elif pattern is _HOBBY:
            predicate=stem
            value=match[1].strip()
        elif pattern is _PET:
            predicate=f"pet_{match[1].casefold()}_name"
            value=match[2].strip()
        else:
            predicate=stem+_slug(match[2])
            value=match[1].strip()
        if (not _KEY.fullmatch(predicate) or not 1<=len(value)<=100
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &/_-]*",value)
            or re.search(r"\b(and|but|because|unless|although|however)\b",value,re.I)
            or not _supported_safe_claim(kind,predicate,value)):
            continue
        return ExplicitClaim(kind,"user",predicate,value,statement)
    return None


