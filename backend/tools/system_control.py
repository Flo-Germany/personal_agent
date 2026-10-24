"""Placeholder for tightly restricted system commands.

Execution is deliberately disabled. When it is enabled, only exact commands
from ALLOWED_COMMANDS may run, with no shell and no user-supplied arguments.
"""

from __future__ import annotations

# Strict allowlist: command name -> exact argv.
# Argument-free commands only. Extend deliberately, never from model output.
ALLOWED_COMMANDS: dict[str, tuple[str, ...]] = {
    "whoami": ("whoami",),
    "hostname": ("hostname",),
}


def run_system_command(command: str) -> str:
    """Validate a command against the allowlist.

    Placeholder only: no process is spawned yet.
    """
    if command not in ALLOWED_COMMANDS:
        raise PermissionError(f"Command is not allowlisted: {command}")

    raise NotImplementedError("System command execution is not enabled yet.")
