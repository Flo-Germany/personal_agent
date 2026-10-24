"""Validated dispatch from the model's tool calls to Python OS tools."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from settings import ToolSettings
from tools import os_tools

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Bind tool names to their implementations and HITL flags."""

    def __init__(self, settings: ToolSettings) -> None:
        self.settings = settings

        tool_settings = settings

        def search(
            root: str = str(Path.home()),
            max_results: int | None = None,
            sort_by: str = "modified_desc",
            **kwargs: Any,
        ) -> dict[str, Any]:
            # Settings own the scan/time caps; the model may ask for fewer results.
            for key in ("max_scanned", "max_seconds"):
                kwargs.pop(key, None)

            allowed_filters = {
                "name_contains",
                "extension",
                "modified_after",
                "modified_before",
                "min_size",
                "max_size",
                "include_hidden",
                "all_drives",
                "recursive",
            }
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in allowed_filters
            }

            if max_results in (None, "", 0, "0"):
                requested = tool_settings.max_search_results
            else:
                try:
                    requested = int(max_results)
                except (TypeError, ValueError):
                    requested = tool_settings.max_search_results

            effective_results = max(
                1,
                min(requested, tool_settings.max_search_results),
            )
            return os_tools.search_system(
                root,
                max_results=effective_results,
                max_scanned=tool_settings.max_search_files,
                max_seconds=tool_settings.max_search_seconds,
                sort_by=sort_by,
                **kwargs,
            )

        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "start_program": lambda program_name: os_tools.start_program(program_name),
            "create_folder": lambda path: os_tools.create_folder(path),
            "create_file": lambda path, content, overwrite=False: os_tools.create_file(
                path,
                content,
                overwrite=overwrite,
            ),
            "copy_file": lambda source_path, destination_path: os_tools.copy_file(
                source_path,
                destination_path,
            ),
            "move_file_or_folder": lambda source_path, destination_path: (
                os_tools.move_file_or_folder(source_path, destination_path)
            ),
            "delete_file_or_folder": lambda path: os_tools.delete_file_or_folder(path),
            "read_file_content": lambda path: os_tools.read_file_content(
                path,
                max_bytes=tool_settings.max_read_bytes,
            ),
            "unzip_file": lambda archive_path, extract_to: os_tools.unzip_file(
                archive_path,
                extract_to,
                max_bytes=tool_settings.max_unzip_bytes,
            ),
            "search_system": search,
        }

    def requires_confirmation(self, name: str) -> bool:
        return os_tools.requires_confirmation(name)

    def execute(self, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
        """Run one tool call and return a JSON-serializable result."""
        handler = self._handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}

        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": f"Invalid JSON arguments for {name}: {exc}",
                }
        else:
            parsed = arguments

        if not isinstance(parsed, dict):
            return {"ok": False, "error": f"Arguments for {name} must be an object."}

        try:
            return handler(**parsed)
        except TypeError as exc:
            return {"ok": False, "error": f"Invalid arguments for {name}: {exc}"}
        except os_tools.ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the model
            logger.exception("Tool %s failed", name)
            return {"ok": False, "error": f"{name} failed: {exc}"}
