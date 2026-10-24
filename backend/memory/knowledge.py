"""User-editable, file-backed knowledge base.

Unlike chat history, the knowledge base persists across chats and restarts.
It is a plain Markdown file the user can open and edit at any time. Learned
facts are written to the matching category section; uncategorized facts go to
``## Learned notes``.

The knowledge base describes a single user. Identity attributes (name, age,
location, occupation) are single-valued: writing a new value removes the old
one. Other facts can be removed explicitly through ``DELETE`` operations.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

IDENTITY = "identity"
INTERESTS = "interests"
PROJECTS = "projects"
PREFERENCES = "preferences"
HABITS = "habits"
COMMUNICATION = "communication"
OTHER = "other"

SECTION_HEADINGS = {
    IDENTITY: "Identity / name",
    INTERESTS: "Interests",
    PROJECTS: "Projects",
    PREFERENCES: "Preferences",
    HABITS: "Habits and routines",
    COMMUNICATION: "Communication style",
    OTHER: "Learned notes",
}
SECTION_ORDER = (IDENTITY, INTERESTS, PROJECTS, PREFERENCES, HABITS, COMMUNICATION, OTHER)
SECTION_ALIASES = {
    IDENTITY: {"identity", "identity / name", "identity/name", "name", "profile"},
    INTERESTS: {"interests", "interest", "hobbies", "hobby", "interests and hobbies"},
    PROJECTS: {"projects", "project", "work", "projects and work", "projects / work"},
    PREFERENCES: {
        "preferences",
        "preference",
        "likes",
        "dislikes",
        "likes and dislikes",
        "preferences and dislikes",
    },
    HABITS: {
        "habits",
        "habit",
        "routines",
        "routine",
        "plans",
        "habits and routines",
        "habits / routines",
    },
    COMMUNICATION: {
        "communication",
        "communication style",
        "communication preferences",
    },
    OTHER: {"other", "notes", "other notes", "learned notes"},
}

LEARNED_HEADING = f"## {SECTION_HEADINGS[OTHER]}"

TEMPLATE = """# User Knowledge Base

<!--
Long-term memory about the user. Edit this file freely: the agent reads it on
every chat. Facts learned automatically go into the matching section below;
uncategorized facts go to "Learned notes".
-->

## Identity / name

## Interests

## Projects

## Preferences

## Habits and routines

## Communication style

## Learned notes
"""

_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_LINE = re.compile(r"^(##\s+.+?)\s*$", re.MULTILINE)
_BULLET_LINE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")

_IDENTITY_ATTRIBUTE_PATTERNS = (
    (
        "name",
        re.compile(
            r"^(?:the user's |user's |my )?(?:full )?name\b\s*(?:is|:)"
            r"|^(?:the |my )?user'?s? name\b"
            r"|^.+\bis (?:the user's|my) name\b",
            re.I,
        ),
    ),
    (
        "age",
        re.compile(
            r"^(?:the user's |user's |my )?age\b\s*(?:is|:)"
            r"|^(?:the |my )?user (?:is|was)(?:\s+now)?\s+\d+ years? old"
            r"|^(?:i am|i'm)\s+\d+(?:\s+years? old)?\b"
            r"|^is \d+ years? old",
            re.I,
        ),
    ),
    (
        "location",
        re.compile(
            r"^(?:the user's |user's |my )?(?:location|city)\b\s*(?:is|:)"
            r"|^(?:the |my )?user (?:lives|is based|moved|has moved)\b"
            r"|^lives in\b"
            r"|^moved to\b",
            re.I,
        ),
    ),
    (
        "occupation",
        re.compile(
            r"^(?:the user's |user's |my )?(?:occupation|job|profession)\b\s*(?:is|:)"
            r"|^(?:the |my )?user works as\b"
            r"|^works as\b",
            re.I,
        ),
    ),
)

_CONTROL_PREFIX = re.compile(r"^(?:DELETE|ADD|REPLACE)\s*:", re.I)


def _normalize_fact(value: str) -> str:
    normalized = " ".join(value.lower().split()).rstrip(".")
    for prefix in ("the user's ", "the user ", "user's ", "the "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized


def _normalize_heading(value: str) -> str:
    key = re.sub(r"^#+\s*", "", value).strip().lower().rstrip(":")
    return " ".join(key.replace("&", "and").split())


def _canonical_category(value: str) -> str:
    key = _normalize_heading(str(value))
    for category, aliases in SECTION_ALIASES.items():
        if key in aliases:
            return category
    return OTHER


def _identity_attribute(fact: str) -> str | None:
    for attribute, pattern in _IDENTITY_ATTRIBUTE_PATTERNS:
        if pattern.search(fact):
            return attribute
    return None


def _parse_document(text: str) -> tuple[str, list[dict[str, str]]]:
    """Split Markdown into a preamble and top-level ``##`` sections."""
    matches = list(_HEADING_LINE.finditer(text))
    if not matches:
        return text.rstrip(), []

    preamble = text[: matches[0].start()].rstrip()
    sections: list[dict[str, str]] = []

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(
            {
                "heading": match.group(1).strip(),
                "body": text[match.end() : end].strip("\n"),
            }
        )

    return preamble, sections


def _render_document(preamble: str, sections: list[dict[str, str]]) -> str:
    blocks: list[str] = []
    if preamble.strip():
        blocks.append(preamble.strip())

    for section in sections:
        block = section["heading"]
        body = section["body"].strip()
        if body:
            block = f"{block}\n{body}"
        blocks.append(block)

    return "\n\n".join(blocks).rstrip() + "\n"


def _find_section(sections: list[dict[str, str]], category: str) -> int | None:
    aliases = SECTION_ALIASES[category]
    for index, section in enumerate(sections):
        if _normalize_heading(section["heading"]) in aliases:
            return index
    return None


def _ends_with_bullet(body: str) -> bool:
    lines = [line for line in body.splitlines() if line.strip()]
    return bool(lines) and lines[-1].lstrip().startswith(("- ", "* "))


def _bullet_facts(sections: list[dict[str, str]]) -> Iterable[str]:
    for section in sections:
        for line in section["body"].splitlines():
            match = _BULLET_LINE.match(line)
            if match:
                yield match.group(1)


def _keep_last_identity_value(
    facts: Mapping[str, list[str]],
) -> tuple[dict[str, list[str]], set[str]]:
    """Move identity facts to the identity section, keeping only the newest value."""
    grouped = {category: list(values) for category, values in facts.items()}
    latest: dict[str, str] = {}

    for category in SECTION_ORDER:
        remaining: list[str] = []
        for fact in grouped.get(category, []):
            attribute = _identity_attribute(fact)
            if attribute is None:
                remaining.append(fact)
            else:
                latest[attribute] = fact
        grouped[category] = remaining

    grouped.setdefault(IDENTITY, []).extend(latest.values())
    return grouped, set(latest)


class KnowledgeBase:
    """Read and maintain the user's persistent knowledge file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def ensure_file(self) -> None:
        """Create the file from the template if it does not exist yet."""
        with self._lock:
            if self.path.exists():
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        """Return the knowledge text without editor comments."""
        with self._lock:
            self.ensure_file()
            try:
                text = self.path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                logger.warning("Could not read knowledge base %s: %s", self.path, exc)
                return ""

        return _COMMENT_PATTERN.sub("", text).strip()

    def add_facts(
        self,
        facts: Mapping[str, Iterable[str]],
        deletions: Iterable[str] = (),
    ) -> list[tuple[str, str]]:
        """Insert, update, or delete facts, skipping repeats.

        ``facts`` maps a category key (or heading name) to fact strings.
        ``deletions`` contains exact existing facts to remove. Identity values
        replace older values automatically. Returns the added
        ``(section heading, fact)`` pairs.
        """
        grouped: dict[str, list[str]] = {category: [] for category in SECTION_ORDER}
        for category, values in facts.items():
            canonical = _canonical_category(category)
            grouped[canonical].extend(str(value) for value in values)

        grouped, identity_attributes = _keep_last_identity_value(grouped)
        incoming_norm = {
            _normalize_fact(fact)
            for values in grouped.values()
            for fact in values
            if fact.strip()
        }
        deletion_norm = {
            _normalize_fact(deletion)
            for deletion in deletions
            if deletion.strip()
        }

        with self._lock:
            self.ensure_file()
            try:
                text = self.path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                logger.warning("Could not read knowledge base %s: %s", self.path, exc)
                return []

            preamble, sections = _parse_document(text)
            changed = False

            for section in sections:
                kept_lines: list[str] = []
                for line in section["body"].splitlines():
                    match = _BULLET_LINE.match(line)
                    if match:
                        fact = match.group(1).strip()
                        normalized = _normalize_fact(fact)
                        attribute = _identity_attribute(fact)

                        remove = normalized in deletion_norm
                        if (
                            attribute is not None
                            and attribute in identity_attributes
                            and normalized not in incoming_norm
                        ):
                            remove = True

                        if remove:
                            changed = True
                            continue

                    kept_lines.append(line)

                section["body"] = "\n".join(kept_lines).strip("\n")

            existing = {_normalize_fact(fact) for fact in _bullet_facts(sections)}
            pending: dict[str, list[str]] = {}

            for category in SECTION_ORDER:
                for raw_fact in grouped.get(category, []):
                    fact = raw_fact.strip().lstrip("-*").strip()
                    if (
                        not fact
                        or len(fact) > 300
                        or _CONTROL_PREFIX.match(fact)
                    ):
                        continue

                    normalized = _normalize_fact(fact)
                    if not normalized or normalized in existing:
                        continue

                    existing.add(normalized)
                    pending.setdefault(category, []).append(fact)

            if not pending and not changed:
                return []

            added: list[tuple[str, str]] = []

            for category in SECTION_ORDER:
                new_facts = pending.get(category, [])
                if not new_facts:
                    continue

                index = _find_section(sections, category)
                if index is None:
                    other_index = _find_section(sections, OTHER)
                    index = other_index if other_index is not None else len(sections)
                    sections.insert(
                        index,
                        {"heading": f"## {SECTION_HEADINGS[category]}", "body": ""},
                    )

                section = sections[index]
                bullets = "\n".join(f"- {fact}" for fact in new_facts)
                body = section["body"].strip()

                if body:
                    separator = "\n" if _ends_with_bullet(body) else "\n\n"
                    section["body"] = f"{body}{separator}{bullets}"
                else:
                    section["body"] = bullets

                added.extend((section["heading"], fact) for fact in new_facts)

            updated = _render_document(preamble, sections)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temp_path.write_text(updated, encoding="utf-8")
            temp_path.replace(self.path)

            return added
