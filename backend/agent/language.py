"""Detect explicit reply-language requests in a conversation.

No language is preferred or enforced by this module. It only extracts the
language the user explicitly asked for, so the prompt can follow that request.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

_VERB_REQUEST = re.compile(
    r"\b(?:speak|talk|write|answer|reply|respond|communicate|continue|switch|change)"
    r"\s+(?:back\s+)?(?:in|to|into)\s+(?P<lang>[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*)?)",
    re.I,
)
_LETS_REQUEST = re.compile(
    r"\b(?:let'?s|let\s+us|i\s+want\s+to|i'?d\s+like\s+to|we\s+should|please)"
    r"\s+(?:speak|talk|write|communicate)\s+"
    r"(?P<lang>[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*)?)",
    re.I,
)
_DIRECT_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:speak|talk|write|answer|reply|respond|continue)"
    r"\s+(?P<lang>[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*)?)",
    re.I,
)
_GERMAN_REQUEST = re.compile(
    r"\b(?:sprich|sprechen|antworte|antworten|schreibe|schreiben)"
    r"(?:\s+bitte)?(?:\s+auf)?\s+"
    r"(?P<lang>[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß-]*)",
    re.I,
)
_BARE_PREPOSITION = re.compile(
    r"^(?:(?:please|bitte|jetzt|now)[,\s]+){1,2}(?:auf|in|en|im)\s+"
    r"(?P<lang>[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß-]*)",
    re.I,
)
_POLITE_SUFFIX = re.compile(
    r"\b(?:auf|in|en|im)\s+"
    r"(?P<lang>[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß-]*)\s*[,;]?\s*"
    r"(?:bitte|please)\s*[.!?]*$",
    re.I,
)

_STOPWORDS = {
    "the",
    "a",
    "an",
    "this",
    "that",
    "it",
    "my",
    "your",
    "our",
    "from",
    "on",
    "now",
    "please",
    "bitte",
    "jetzt",
    "for",
    "and",
    "or",
    "but",
    "so",
    "as",
    "with",
    "without",
    "to",
    "in",
    "into",
    "at",
    "of",
    "detail",
    "short",
    "brief",
    "general",
    "private",
    "full",
    "plain",
    "simple",
    "every",
    "all",
    "conversation",
    "chat",
    "me",
    "us",
    "briefly",
    "clearly",
    "again",
    "louder",
    "slower",
    "faster",
}


def _clean_language(candidate: str) -> str | None:
    words = re.findall(r"[A-Za-zÄÖÜäöüß-]+", candidate)
    for index, word in enumerate(words):
        if word.lower() in _STOPWORDS:
            words = words[:index]
            break

    if not words:
        return None

    language = " ".join(words).strip("-")
    if not language or len(language) > 40:
        return None

    return language.title() if language.islower() else language


def detect_language_request(text: str) -> str | None:
    """Return the explicitly requested language in one message, if any."""
    for pattern in (
        _VERB_REQUEST,
        _LETS_REQUEST,
        _DIRECT_REQUEST,
        _GERMAN_REQUEST,
        _BARE_PREPOSITION,
        _POLITE_SUFFIX,
    ):
        match = pattern.search(text)
        if match:
            language = _clean_language(match.group("lang"))
            if language:
                return language

    return None


def detect_requested_language(
    messages: Iterable[Mapping[str, Any]],
) -> str | None:
    """Find the most recent explicit language request in the conversation."""
    for message in reversed(list(messages)):
        if message.get("role") != "user":
            continue

        language = detect_language_request(str(message.get("content", "")))
        if language:
            return language

    return None
