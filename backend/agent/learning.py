"""Extract categorized, durable user facts from a message."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from memory.knowledge import (
    COMMUNICATION,
    HABITS,
    IDENTITY,
    INTERESTS,
    OTHER,
    PREFERENCES,
    PROJECTS,
    SECTION_ORDER,
)

logger = logging.getLogger(__name__)

LEARNING_SYSTEM_PROMPT = """You maintain a long-term knowledge base about the user of a personal assistant.

Extract only durable, useful facts about the user from the latest message.
Use exactly one of these category labels for each fact:
IDENTITY: name, age, location, occupation, living situation
INTERESTS: hobbies, activities, pets, languages being learned, sports
PROJECTS: work, projects, goals, deadlines, collaborators, requirements and materials for the user's projects
PREFERENCES: likes, dislikes, music/media tastes, diet, tools, environment, comfort
HABITS: routines, schedules, recurring behavior, plans, workspace setup
COMMUNICATION: preferred tone, format, language, social boundaries (e.g. dislikes small talk)

Rules:
- Extract facts only from the user's own message, never from the assistant.
- Be exhaustive: capture every durable fact stated in the message.
- Only include facts the user explicitly stated or clearly implied.
- Do not repeat anything already present in the knowledge base.
- Do not include one-off requests, questions, or general world knowledge.
- Do not invent, speculate, or use placeholders.
- Every fact must be concrete and self-contained.
- Keep all details of a fact together; never shorten a fact and drop details.
- The knowledge base describes a single user. Keep exactly one value for name, age, location, and occupation; the newest statement wins.
- If the user corrects or replaces an existing fact, output a line `DELETE:` followed by the exact existing fact, then output the new fact.
- When the user says they no longer like, enjoy, use, or do something, delete the existing fact about it before adding the new one.
- Only delete facts the user clearly replaced or contradicted; never delete unrelated facts.
- Always capture name, age, location, and occupation when the user states them.
- Capture travel plans, destinations, and dates when the user states them.
- Project details (deadlines, collaborators, materials) belong to PROJECTS.
- Daily routines and workspace setup belong to HABITS.
- Formatting and social preferences belong to COMMUNICATION.
- Never store a temporary language instruction for this conversation (for example "answer in Spanish" or "speak Chinese from now on") as a durable user fact. Language choices are handled separately.
- Never store search/read/list requests or the folders the user asked to search as preferences, habits, or projects. Those are one-off actions, not durable facts.
- Output one fact per line in the format `LABEL: fact`.
- If there is nothing new and durable, output exactly: NONE

Example of the output format only (do not copy the values):
IDENTITY: a fact about the user's identity
INTERESTS: a fact about an interest
PROJECTS: a fact about a project
PREFERENCES: a fact about a preference
HABITS: a fact about a routine
COMMUNICATION: a fact about communication style
DELETE: the exact text of an outdated fact
"""

_PLACEHOLDER = re.compile(r"[\[\{<][^\]\}>]*[\]\}>]")
_GENERIC_ENDINGS = (
    "discussing",
    "working on",
    "interested in",
    "has an interest in",
)
_REJECTED_MARKERS = ("unknown", "not provided", "n/a", "no information")
_ACTION_REQUEST = re.compile(
    r"\b(?:wants to|would like to|asked the assistant to)\s+"
    r"(?:create|use|delete|move|copy|extract|search|read|open)\b",
    re.I,
)
_PATH_LIKE = re.compile(
    r"(?:[A-Za-z]:\\|[/\\]|\.(?:txt|md|pdf|zip|docx?|xlsx?|pptx?|png|jpe?g)\b)",
    re.I,
)
_LANGUAGE_INSTRUCTION_FACT = re.compile(
    r"\b(?:prefers?|wants?|would\s+like|likes?|asked?|requested?|should)\s+"
    r"(?:to\s+)?(?:speak|talk|answer|reply|respond|communicate|write|switch|change|use)\b"
    r".*\b(?:assistant|conversation|chat|language)\b",
    re.I,
)
_BULLET = re.compile(r"^[-*]\s+(.+)$")
_LABEL_LINE = re.compile(r"^(?P<label>[A-Za-z][A-Za-z &/_-]{1,30})\s*:\s*(?P<fact>.+)$")
_CONTROL_LINE = re.compile(r"^(?P<verb>DELETE|ADD|REPLACE)\s*:\s*(?P<rest>.+)$", re.I)
_REPLACE_ARROW = re.compile(r"^(?P<old>.+?)\s*(?:->|=>|→)\s*(?P<new>.+)$")


def _split_control(fact: str) -> tuple[str | None, str | None]:
    """Split DELETE/REPLACE/ADD prefixes out of a fact."""
    match = _CONTROL_LINE.match(fact)
    if not match:
        return None, fact

    verb = match.group("verb").upper()
    rest = match.group("rest").strip()

    if verb == "DELETE":
        return rest, None

    if verb == "REPLACE":
        arrow = _REPLACE_ARROW.match(rest)
        if arrow:
            return arrow.group("old").strip(), arrow.group("new").strip()

    return None, rest

_LABEL_TO_CATEGORY = {
    "identity": IDENTITY,
    "name": IDENTITY,
    "profile": IDENTITY,
    "age": IDENTITY,
    "location": IDENTITY,
    "occupation": IDENTITY,
    "interests": INTERESTS,
    "interest": INTERESTS,
    "hobbies": INTERESTS,
    "hobby": INTERESTS,
    "pets": INTERESTS,
    "projects": PROJECTS,
    "project": PROJECTS,
    "work": PROJECTS,
    "goals": PROJECTS,
    "preferences": PREFERENCES,
    "preference": PREFERENCES,
    "likes": PREFERENCES,
    "dislikes": PREFERENCES,
    "diet": PREFERENCES,
    "habits": HABITS,
    "habit": HABITS,
    "routines": HABITS,
    "routine": HABITS,
    "plans": HABITS,
    "schedule": HABITS,
    "communication": COMMUNICATION,
    "communication style": COMMUNICATION,
    "communication preferences": COMMUNICATION,
    "other": OTHER,
}


@dataclass(frozen=True)
class LearnedFacts:
    """Facts to add, grouped by section, plus exact facts to delete."""

    facts: dict[str, list[str]] = field(default_factory=dict)
    deletions: list[str] = field(default_factory=list)


def _empty_groups() -> dict[str, list[str]]:
    return {category: [] for category in SECTION_ORDER}


def _normalize_label(value: str) -> str:
    key = value.strip().lower().replace("&", "and")
    key = re.sub(r"[_/-]+", " ", key)
    return " ".join(key.split())


def _is_concrete_fact(fact: str) -> bool:
    if len(fact) < 3:
        return False
    if _PLACEHOLDER.search(fact) or _CONTROL_LINE.match(fact):
        return False
    if _LANGUAGE_INSTRUCTION_FACT.search(fact):
        return False

    lowered = fact.lower().rstrip(".")
    if lowered in {"none", "(none)", "no facts", "no new facts"}:
        return False
    if lowered.endswith(":"):
        return False
    if any(marker in lowered for marker in _REJECTED_MARKERS):
        return False
    if any(
        phrase in lowered
        for phrase in (
            "asked the assistant",
            "asked me to",
            "asked you to",
            "requested that the assistant",
        )
    ):
        return False
    if _ACTION_REQUEST.search(fact) and (
        "file" in lowered or "folder" in lowered or _PATH_LIKE.search(fact)
    ):
        return False

    return not lowered.endswith(_GENERIC_ENDINGS)


def parse_facts(text: str) -> LearnedFacts:
    """Parse categorized lines and deletions from the model output."""
    grouped = _empty_groups()
    deletions: list[str] = []
    seen: set[str] = set()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.upper() == "NONE":
            continue

        bullet = _BULLET.match(stripped)
        if bullet:
            stripped = bullet.group(1).strip()

        match = _LABEL_LINE.match(stripped)
        category = OTHER
        fact = stripped

        if match:
            mapped = _LABEL_TO_CATEGORY.get(_normalize_label(match.group("label")))
            if mapped is not None:
                category = mapped
                fact = match.group("fact").strip()

        deleted, remaining = _split_control(fact)
        if deleted and not _PLACEHOLDER.search(deleted):
            deletions.append(deleted)
        if remaining is None:
            continue
        fact = remaining

        if not _is_concrete_fact(fact):
            continue

        normalized = " ".join(fact.lower().rstrip(".").split())
        if normalized in seen:
            continue

        seen.add(normalized)
        grouped[category].append(fact)

    return LearnedFacts(facts=grouped, deletions=deletions)


def build_learning_prompt(
    knowledge: str,
    user_message: str,
) -> str:
    return (
        "Current knowledge base:\n"
        f"{knowledge.strip() or '(empty)'}\n\n"
        f"User message:\n{user_message}\n\n"
        "List the new durable facts about the user, or NONE."
    )


_NEGATION = re.compile(
    r"\b(?:i\s+)?(?:don't|do not|doesn't|didn't|no longer|stopped|quit|dislike|hate)\b"
    r"(?:\s+(?:like|love|enjoy|use|want|prefer|live|work|play|eat|drink|read|watch))?"
    r"\s+(?P<topic>[^.;!?]+)",
    re.I,
)
_POSITIVE_VERB = re.compile(
    r"\b(?:enjoys?|likes?|loves?|prefers?|uses?|wants?|lives?|works?|plays?|eats?|drinks?|reads?|watches?)\b",
    re.I,
)
_BULLET_PATTERN = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.MULTILINE)
_TOPIC_STOPWORDS = {
    "i",
    "the",
    "a",
    "an",
    "my",
    "me",
    "it",
    "that",
    "this",
    "about",
    "with",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "and",
    "but",
    "really",
    "very",
    "much",
    "more",
    "any",
    "anymore",
    "now",
    "actually",
    "also",
    "just",
    "thing",
    "things",
    "stuff",
    "them",
    "they",
    "you",
    "your",
    "music",
    "food",
    "movies",
    "books",
    "games",
    "sports",
}


def infer_deletions(knowledge: str, user_message: str) -> list[str]:
    """Find outdated positive facts for simple "I no longer ..." statements.

    This complements the model's explicit DELETE output. It only looks at
    specific topic words so broad statements do not wipe unrelated facts.
    """
    topics: set[str] = set()

    for match in _NEGATION.finditer(user_message):
        topic = re.sub(
            r"\b(?:any ?more|anymore|now)\b",
            " ",
            match.group("topic").lower(),
        )
        for word in re.findall(r"[a-z0-9']+", topic):
            if len(word) > 2 and word not in _TOPIC_STOPWORDS:
                topics.add(word)

    if not topics:
        return []

    deletions: list[str] = []
    for match in _BULLET_PATTERN.finditer(knowledge):
        fact = match.group(1).strip()
        lowered = fact.lower()
        if not _POSITIVE_VERB.search(lowered):
            continue
        if any(re.search(rf"\b{re.escape(topic)}\b", lowered) for topic in topics):
            deletions.append(fact)

    return deletions


def extract_facts(
    client: Any,
    model: str,
    knowledge: str,
    user_message: str,
) -> LearnedFacts:
    """Ask the local model for new categorized facts from a user message."""
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": LEARNING_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_learning_prompt(knowledge, user_message),
            },
        ],
        temperature=0.2,
        max_tokens=400,
    )

    choices = getattr(completion, "choices", None)
    if not choices:
        return LearnedFacts(facts=_empty_groups())

    content = getattr(choices[0].message, "content", None)
    if not isinstance(content, str):
        return LearnedFacts(facts=_empty_groups())

    return parse_facts(content)
