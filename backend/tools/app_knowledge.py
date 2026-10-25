"""Per-application knowledge used by the GUI automation tools.

A human knows that Outlook opens a *separate* compose window when you press
Ctrl+N, that Explorer navigates with Ctrl+L, and that Enter confirms the
default button of a dialog. The agent gets the same small, curated guide with
every Set-of-Mark snapshot instead of having to rediscover the application from
scratch.

This module is intentionally data-only: it describes shortcuts and workflows,
never performs input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class AppProfile:
    key: str
    label: str
    category: str
    processes: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()
    shortcuts: dict[str, str] = field(default_factory=dict)
    workflow: str = ""
    # Patterns that extract the current conversation from a composer label such
    # as "Nachricht an Paul" or "Message @Paul".
    conversation_patterns: tuple[str, ...] = ()
    # Names that identify a notification/bot chat where user messages must not
    # be sent.
    notification_patterns: tuple[str, ...] = ()
    # Titles of *secondary* windows of this app. When one is already open the
    # agent should work in it instead of triggering the same command again.
    secondary_patterns: tuple[str, ...] = ()
    secondary_hint: str = ""

    def matches(self, process_name: str, window_title: str) -> bool:
        process = str(process_name or "").casefold()
        title = str(window_title or "")
        if process and process in {name.casefold() for name in self.processes}:
            return True
        return any(
            re.search(pattern, title, re.IGNORECASE)
            for pattern in self.title_patterns
        )

    def secondary_windows(
        self,
        titles: Sequence[str],
        current_title: str,
    ) -> list[str]:
        current = str(current_title or "").casefold()
        found: list[str] = []
        for title in titles:
            text = str(title or "").strip()
            if not text or text.casefold() == current:
                continue
            if any(
                re.search(pattern, text, re.IGNORECASE)
                for pattern in self.secondary_patterns
            ):
                found.append(text)
        return found


_CHAT_WORKFLOW = (
    "The window title and the bottom message composer identify the active "
    "conversation. For a person, first click their existing entry in the "
    "direct-message/chat list. If there is no chat, open the new-message or "
    "search dialog, type the exact name into its Search field, select the "
    "person, and only then open/confirm the chat. The composer at the bottom "
    "must name the requested person before you type. Never type a message into "
    "the search or recipient field, and never send a user message to a bot or "
    "notification chat. Press Enter only after verifying the recipient; pass "
    "recipient='<name>' to keyboard_type_text."
)

_PROFILES: tuple[AppProfile, ...] = (
    AppProfile(
        key="discord",
        label="Discord",
        category="chat",
        processes=("discord.exe",),
        title_patterns=(r"- Discord$",),
        shortcuts={
            "quick switcher": "ctrl+k",
            "search": "ctrl+f",
            "send": "enter",
            "newline": "shift+enter",
            "close dialog": "esc",
        },
        workflow=(
            "Use the existing Direct Messages list first; a person appears as "
            "'<Name> (Direktnachricht)' and can be clicked directly. Ctrl+K opens "
            "the quick switcher. 'Nachricht erstellen' opens the New Message "
            "dialog: type the exact name into its Search field, select the "
            "person, and only then click the final 'Nachricht erstellen' button. "
            "The bottom composer must say 'Nachricht an <name>' or "
            "'Message <name>' before you type. Never press the dialog's Create "
            "button with no selected person, never type the message into the "
            "search/recipient field, and never send user messages to a "
            "notification or bot DM. "
        )
        + _CHAT_WORKFLOW,
        conversation_patterns=(
            r"^(?:Nachricht an|Message(?: @)?|Message to)\s+(.+)$",
        ),
        notification_patterns=(
            r"agent notification",
            r"\bnotifications?\b",
            r"\bbot\b",
        ),
    ),
    AppProfile(
        key="whatsapp",
        label="WhatsApp",
        category="chat",
        processes=("whatsapp.exe",),
        title_patterns=(r"- WhatsApp$",),
        shortcuts={"search": "ctrl+f", "send": "enter", "newline": "shift+enter"},
        workflow=_CHAT_WORKFLOW,
        notification_patterns=(r"agent notification", r"\bnotifications?\b"),
    ),
    AppProfile(
        key="telegram",
        label="Telegram",
        category="chat",
        processes=("telegram.exe",),
        title_patterns=(r"- Telegram$",),
        shortcuts={"search": "ctrl+f", "send": "enter", "newline": "shift+enter"},
        workflow=_CHAT_WORKFLOW,
        notification_patterns=(r"agent notification", r"\bnotifications?\b"),
    ),
    AppProfile(
        key="teams",
        label="Microsoft Teams",
        category="chat",
        processes=("ms-teams.exe", "teams.exe"),
        title_patterns=(r"\| Microsoft Teams$",),
        shortcuts={"search": "ctrl+e", "send": "enter", "newline": "shift+enter"},
        workflow=_CHAT_WORKFLOW,
        notification_patterns=(r"agent notification", r"\bnotifications?\b"),
    ),
    AppProfile(
        key="slack",
        label="Slack",
        category="chat",
        processes=("slack.exe",),
        title_patterns=(r"- Slack$",),
        shortcuts={"search": "ctrl+f", "send": "enter", "newline": "shift+enter"},
        workflow=_CHAT_WORKFLOW,
        notification_patterns=(r"agent notification", r"\bnotifications?\b"),
    ),
    AppProfile(
        key="outlook",
        label="Microsoft Outlook",
        category="email",
        processes=("outlook.exe",),
        title_patterns=(r"\boutlook\b",),
        shortcuts={
            "new email": "ctrl+n",
            "send": "ctrl+enter",
            "reply": "ctrl+r",
            "forward": "ctrl+f",
            "search": "ctrl+e",
            "save draft": "ctrl+s",
            "close window": "alt+f4",
        },
        workflow=(
            "New Email (Ctrl+N) opens a SEPARATE compose window. Work inside "
            "that window: To, Cc, and Subject are Edit controls and the message "
            "area is a Document. Click the named field, type, then click the "
            "next named field; do not rely on Tab order. Check "
            "outcome.focused_control before typing. Ctrl+N again creates "
            "another draft, so check app_windows and outcome.window_opened "
            "first. Send only when the user explicitly asked: Ctrl+Enter sends "
            "immediately."
        ),
        secondary_patterns=(
            r"\bmessage\b",
            r"\bnachricht\b",
            r"\buntitled\b",
            r"\bunbenannt\b",
            r"\bcompose\b",
            r"\bverfassen\b",
        ),
        secondary_hint=(
            "A message compose window is already open. Fill that window "
            "instead of clicking New Email again."
        ),
    ),
    AppProfile(
        key="notepad",
        label="Notepad",
        category="editor",
        processes=("notepad.exe",),
        title_patterns=(r" - Notepad$", r" - Editor$"),
        shortcuts={
            "new": "ctrl+n",
            "open": "ctrl+o",
            "save": "ctrl+s",
            "save as": "ctrl+shift+s",
            "find": "ctrl+f",
            "replace": "ctrl+h",
            "select all": "ctrl+a",
            "close window": "alt+f4",
        },
        workflow=(
            "Type into the document area (a Document/Edit control). Ctrl+S on "
            "an untitled document opens the save dialog: type the full path in "
            "the file name field and press Enter."
        ),
    ),
    AppProfile(
        key="explorer",
        label="File Explorer",
        category="files",
        processes=("explorer.exe",),
        shortcuts={
            "address bar": "ctrl+l",
            "new folder": "ctrl+shift+n",
            "rename": "f2",
            "delete": "delete",
            "refresh": "f5",
            "back": "alt+left",
            "up one level": "alt+up",
            "search": "ctrl+f",
            "properties": "alt+enter",
        },
        workflow=(
            "Navigate with Ctrl+L, type a full path, and press Enter. Never "
            "click the breadcrumb address bar; it opens a pop-up and does not "
            "focus the edit. Use the arrow keys to select items and F2/Delete "
            "for renames/deletions. File-changing actions still require "
            "approval."
        ),
    ),
    AppProfile(
        key="browser",
        label="Web browser",
        category="browser",
        processes=("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"),
        shortcuts={
            "address bar": "ctrl+l",
            "new tab": "ctrl+t",
            "close tab": "ctrl+w",
            "reopen tab": "ctrl+shift+t",
            "reload": "f5",
            "find on page": "ctrl+f",
            "back": "alt+left",
            "developer tools": "f12",
        },
        workflow=(
            "If the user gives a URL or address, it is the destination: press "
            "Ctrl+L, type it, press Enter, and wait for the window title to "
            "change before reading anything. Never describe the currently open "
            "page instead of navigating. The window title is not the page "
            "heading: read the page with read_window_content (or "
            "analyze_screen_ui for visual layout) and quote the actual heading "
            "or text. Prefer keyboard navigation over clicking arbitrary page "
            "areas."
        ),
    ),
    AppProfile(
        key="vscode",
        label="Visual Studio Code",
        category="editor",
        processes=("code.exe", "code - insiders.exe"),
        title_patterns=(r"Visual Studio Code$",),
        shortcuts={
            "quick open file": "ctrl+p",
            "command palette": "ctrl+shift+p",
            "terminal": "ctrl+`",
            "save": "ctrl+s",
            "find": "ctrl+f",
            "search in files": "ctrl+shift+f",
            "close editor": "ctrl+w",
        },
        workflow=(
            "Use Ctrl+P to open a file by name and Ctrl+Shift+P for commands "
            "instead of clicking menus."
        ),
    ),
    AppProfile(
        key="office",
        label="Microsoft Office document",
        category="office",
        processes=("winword.exe", "excel.exe", "powerpnt.exe"),
        shortcuts={
            "save": "ctrl+s",
            "find": "ctrl+f",
            "print": "ctrl+p",
            "close window": "alt+f4",
        },
        workflow=(
            "The document body is a Document/Edit control. Use the ribbon only "
            "when no shortcut is known; save with Ctrl+S."
        ),
    ),
)

_GENERIC = AppProfile(
    key="generic",
    label="Windows application",
    category="generic",
    shortcuts={
        "menus": "alt",
        "next control": "tab",
        "previous control": "shift+tab",
        "activate": "enter",
        "cancel or close dialog": "esc",
        "save": "ctrl+s",
        "close window": "alt+f4",
    },
    workflow=(
        "Read the window title, the open app windows, and the focused control "
        "before acting. Alt opens the menu bar; arrows and Enter navigate it. "
        "After every action, check outcome.window_opened, outcome.title_changed, "
        "outcome.focused_control, and outcome.ui_changed before deciding the "
        "next step. Prefer clicking a named field over guessing the Tab order."
    ),
)


def profile_for(process_name: str, window_title: str) -> AppProfile:
    for profile in _PROFILES:
        if profile.matches(process_name, window_title):
            return profile
    return _GENERIC


def guide_for(
    process_name: str,
    window_title: str,
    *,
    open_window_titles: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the compact app guide embedded in an analyze_screen_ui result."""
    profile = profile_for(process_name, window_title)
    guide: dict[str, Any] = {
        "app": profile.key,
        "label": profile.label,
        "category": profile.category,
        "shortcuts": dict(profile.shortcuts),
        "workflow": profile.workflow,
    }

    if profile.secondary_patterns:
        secondaries = profile.secondary_windows(
            open_window_titles,
            window_title,
        )
        if secondaries:
            guide["open_secondary_windows"] = secondaries[:4]
            if profile.secondary_hint:
                guide["warning"] = profile.secondary_hint

    return guide


def conversation_hint(
    profile: AppProfile,
    window_title: str,
    element_names: Sequence[str] = (),
) -> str | None:
    """Identify the active conversation for a chat app.

    Prefers the composer label ("Nachricht an Paul", "Message @Paul") and
    falls back to the window title ("Paul - Discord"). Returns None when the
    app is not a chat app or no conversation can be determined.
    """
    if profile.category != "chat":
        return None

    for name in element_names:
        text = str(name or "").strip()
        if not text:
            continue
        for pattern in profile.conversation_patterns:
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                value = re.sub(
                    r"\s*\((?:Direktnachricht|direct message|dm)\)\s*$",
                    "",
                    value,
                    flags=re.IGNORECASE,
                )
                value = value.strip(" @")
                if value:
                    return value

    title = str(window_title or "").strip()
    for suffix in (
        " - Discord",
        " - WhatsApp",
        " - Telegram",
        " - Slack",
        " | Microsoft Teams",
        " - Microsoft Teams",
    ):
        if title.casefold().endswith(suffix.casefold()):
            title = title[: -len(suffix)].strip()
            break
    if title and title.casefold() not in {
        "discord",
        "whatsapp",
        "telegram",
        "slack",
        "microsoft teams",
        "teams",
    }:
        return title
    return None


def is_notification_conversation(
    profile: AppProfile,
    conversation: str | None,
) -> bool:
    text = str(conversation or "").casefold()
    if not text:
        return False
    return any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in profile.notification_patterns
    )
