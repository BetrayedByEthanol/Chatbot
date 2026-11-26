"""Bounded, explicitly user-authored personal definition grammar.

No database or inference dependencies. A positive vocabulary, not merely
syntactic validity, is required for unattended NORMAL-privacy extraction.
"""
from dataclasses import dataclass
import re


SAFE_CONCEPTS={
    "home":frozenset({"feeling safe","belonging","comfort"}),
    "friendship":frozenset({"trust","support","companionship"}),
    "learning":frozenset({"curiosity","growth","exploration"}),
}
ROUTINES=frozenset({"morning routine","evening routine","weekend routine","bedtime routine"})
# Deliberately bounded action vocabulary. Associations classify the action,
# not the user's intent, motives, habits, preferences or completed work.
_ACTIONS={
    "preparing tomorrow's lunch":"meal_preparation",
    "preparing lunch":"meal_preparation",
    "preparing breakfast":"meal_preparation",
    "packing lunch":"meal_preparation",
    "cooking":"meal_preparation",
    "walking the dog":"pet_care",
    "feeding the dog":"pet_care",
    "walking":"walking",
    "reading":"reading",
    "gardening":"gardening",
    "cycling":"cycling",
    "stretching":"exercise",
    "tidying up":"household_care",
    "brushing my teeth":"personal_care",
}
_RULE_VERSION="personal-meaning.rules.1"
_READING=re.compile(r"reading for ([1-9][0-9]{0,2}) minutes",re.I)
_TOPIC=r"[a-z][a-z -]{1,30}"
_PUNCTUATION=str.maketrans({"’":"\'","‘":"\'","“":'"',"”":'"'})
# Quoted term is allowed only in the explicit definition frame, not a
# quoted or hypothetical statement as a whole.
_LABEL=rf'(?:\"{_TOPIC}\"|\'{_TOPIC}\'|{_TOPIC})'
_REPORT=re.compile(
    rf"(?:actually,?\s+)?(?:for me,?\s+(?P<for>{_LABEL})\s+means\s+"
    rf"|when i say\s+(?P<say>{_LABEL}),\s+i mean\s+)(?P<value>[^.\r\n?!;:]+)\.?",
    re.I,
)
_QUESTIONS=(
    re.compile(rf"what do i mean (?:by|when i say)\s+(?P<label>{_LABEL})\?",re.I),
    re.compile(rf"what does\s+(?P<label>{_LABEL})\s+mean to me\?",re.I),
    re.compile(rf"what is my\s+(?P<label>{_LABEL})\?",re.I),
)
_USE=re.compile(
    r"(?:help me (?:plan|with)|(?:let's|can we) (?:plan|review|discuss)|plan|review)"
    r"\s+my\s+(?P<label>morning routine|evening routine|weekend routine|bedtime routine)[?.!]?",
    re.I,
)


@dataclass(frozen=True)
class Definition:
    topic: str
    value: str
    source_quote: str
    components: tuple[str,...]

    @property
    def predicate(self):
        return "meaning_"+self.topic.replace(" ","_")


def normalize_topic(label: str) -> str | None:
    text=" ".join(label.translate(_PUNCTUATION).strip().strip("\"'").casefold().split())
    if text.startswith("my ") and text[3:] in ROUTINES:
        text=text[3:]
    return text if text in SAFE_CONCEPTS or text in ROUTINES else None


def association_for(topic: str,component: str) -> str | None:
    if topic not in ROUTINES:
        return None
    term=component.translate(_PUNCTUATION).casefold()
    if term in _ACTIONS:
        return _ACTIONS[term]
    match=_READING.fullmatch(term)
    if match is not None and 1<=int(match[1])<=120:
        return "reading"
    return None


def definition_components(topic: str,value: str) -> tuple[str,...]:
    """Return complete ordered source spans or abstain; never partial admission."""
    if not isinstance(value,str) or not 1<=len(value)<=100 or value!=value.strip():
        return ()
    parts=tuple(part.strip() for part in re.split(r",\s*(?:and\s+)?|\s+and\s+",value,flags=re.I))
    if not 1<=len(parts)<=5 or len({part.casefold() for part in parts})!=len(parts):
        return ()
    if topic in SAFE_CONCEPTS:
        if not all(part.casefold() in SAFE_CONCEPTS[topic] for part in parts):
            return ()
    elif topic in ROUTINES:
        if not all(association_for(topic,part) is not None for part in parts):
            return ()
    else:
        return ()
    return parts


def parse_personal_definition(text: str) -> Definition | None:
    if not isinstance(text,str) or not 8<=len(text.strip())<=320:
        return None
    source=text.strip()
    if any(c in source for c in ("\r","\n","\t")):
        return None
    # One-to-one punctuation normalization for matching only; source/value
    # remain exact original spans.
    match=_REPORT.fullmatch(source.translate(_PUNCTUATION))
    if match is None:
        return None
    topic=normalize_topic(match["for"] or match["say"])
    if topic is None:
        return None
    start,end=match.span("value")
    value=source[start:end].strip()
    components=definition_components(topic,value)
    return Definition(topic,value,source,components) if components else None


def unresolved_meaning_topic(text: str) -> str | None:
    """Recognize a complete explicit definition whose value cannot be admitted.

    Reuse the same syntax and known-topic boundary, not broad correction
    inference. Store only a notice, never the proposed value. Quoted whole
    statements, external hypothetical frames and compound sentences are not
    recognized. Uncertain values inside the explicit frame block conservatively.
    """
    if not isinstance(text,str) or not 8<=len(text.strip())<=320:
        return None
    source=text.strip()
    if any(c in source for c in ("\r","\n","\t")):
        return None
    match=_REPORT.fullmatch(source.translate(_PUNCTUATION))
    if match is None or parse_personal_definition(source) is not None:
        return None
    return normalize_topic(match["for"] or match["say"])


def meaning_reference(text: str) -> str | None:
    """Explicit, standalone personal-definition recall, not dictionary lookup."""
    if not isinstance(text,str) or len(text)>180:
        return None
    for index,pattern in enumerate(_QUESTIONS):
        match=pattern.fullmatch(text.strip().translate(_PUNCTUATION))
        if match is not None:
            topic=normalize_topic(match["label"])
            if index==2 and topic not in ROUTINES:
                return None
            return topic
    return None


def meaning_use_reference(text: str) -> str | None:
    """Only a bounded, explicit request to discuss a named routine."""
    if not isinstance(text,str) or len(text)>180:
        return None
    match=_USE.fullmatch(text.strip().replace("’","'"))
    return match["label"].casefold() if match else None
