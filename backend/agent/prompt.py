"""System prompt construction for the assistant's identity."""

from __future__ import annotations

from settings import Settings


def build_system_prompt(settings: Settings, knowledge: str = "") -> str:
    """Compose the identity, base system prompt, knowledge, and memory rules."""
    parts = [
        f"You are {settings.agent_name}, a local, private desktop AI assistant "
        "running entirely on the user's machine.",
        "You are talking directly with the user through a small desktop chat window.",
    ]

    system_prompt = settings.system_prompt.strip()
    if system_prompt:
        parts.append(system_prompt)

    personality = settings.personality.strip()
    if personality:
        parts.append(f"Your personality: {personality}")

    knowledge = knowledge.strip()
    if knowledge:
        parts.append(
            "Long-term knowledge about the user, from a file the user can edit. "
            "Treat it as reliable background and use it naturally; do not recite "
            "the whole file:\n\n"
            f"{knowledge}"
        )

    parts.append(
        "The knowledge base describes a single user. The current conversation "
        "takes precedence: if the user corrects or changes something, follow the "
        "newest statement and treat the old value as outdated. Never output "
        "placeholder tokens; use the actual value or ask when you do not know it."
    )
    parts.append(
        "Use the conversation history as your memory for this chat. Stay consistent "
        "with details and preferences the user shared earlier, and refer back to "
        "them when useful. Do not claim to remember conversations outside the "
        "provided history or knowledge base."
    )
    parts.append(
        "Use plain text. Avoid Markdown formatting unless the user asks for it. "
        "Be clear and concise, and say so when you are unsure instead of inventing "
        "details."
    )

    return "\n\n".join(parts)
