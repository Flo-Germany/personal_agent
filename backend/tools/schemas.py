"""OpenAI-compatible tool schemas for the local agent.

The descriptions are the model's only guide for choosing tools, so they spell
out full-filesystem access, the approval boundary, and the safety rules.
"""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "start_program",
            "description": (
                "Launch one of the user's allowed local Windows applications. "
                "This is a modifying action and requires explicit user approval "
                "before it runs. Allowed programs: 'calculator' (Calculator), "
                "'notepad' (Notepad), 'explorer' (File Explorer), and 'paint' "
                "(Paint). It launches exactly one program and cannot pass "
                "arguments, run shell commands, run scripts, or start any other "
                "application."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "program_name": {
                        "type": "string",
                        "enum": ["calculator", "notepad", "explorer", "paint"],
                        "description": "Allowlisted program to launch.",
                    }
                },
                "required": ["program_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": (
                "Create a folder, including missing parent folders, anywhere on "
                "the user's file system outside protected Windows locations. This "
                "is a modifying action and requires explicit user approval before "
                "it runs. Prefer an absolute path. If the folder already exists, "
                "the operation succeeds. Windows, Program Files, ProgramData, and "
                "drive roots are protected and will be refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Folder path. Prefer an absolute path such as "
                            "'C:\\Users\\Flo\\Downloads\\Invoices'."
                        ),
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a UTF-8 text file anywhere outside protected Windows "
                "locations. This is a modifying action and requires explicit user "
                "approval before it runs. Missing parent folders are created. By "
                "default an existing file is not overwritten; only set overwrite=true "
                "when the user explicitly asks to replace it. Protected Windows "
                "locations cannot be modified. Content is limited to 1 MB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Exact new file path, preferably absolute, for example "
                            "'C:\\Users\\Flo\\Downloads\\AgentFeatureTest\\hello.txt'."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "UTF-8 text content for the new file.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": (
                            "Replace an existing file only when the user explicitly "
                            "asked to overwrite it. Defaults to false."
                        ),
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": (
                "Copy an existing file to a new destination. This is a modifying "
                "action and requires explicit user approval before it runs. The "
                "destination must not already exist; choose a new name instead of "
                "overwriting. Missing destination parent folders are created. "
                "Protected Windows locations cannot be modified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Existing source file, preferably absolute.",
                    },
                    "destination_path": {
                        "type": "string",
                        "description": (
                            "New destination file path. Must not already exist."
                        ),
                    },
                },
                "required": ["source_path", "destination_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file_or_folder",
            "description": (
                "Move a file or folder to a new location, including across drives. "
                "This is a modifying action and requires explicit user approval "
                "before it runs. The destination must not already exist; choose a "
                "new name instead of overwriting. Missing destination parents are "
                "created. A folder cannot be moved into itself. Protected Windows "
                "locations cannot be moved or modified. Use an exact existing path "
                "from search_system; do not pass a prefix or wildcard."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Existing file or folder, preferably absolute.",
                    },
                    "destination_path": {
                        "type": "string",
                        "description": (
                            "New destination path. Must not already exist."
                        ),
                    },
                },
                "required": ["source_path", "destination_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file_or_folder",
            "description": (
                "Move a file or folder to the Windows Recycle Bin. This is a "
                "destructive action and requires explicit user approval before it "
                "runs. Protected Windows locations (Windows, Program Files, "
                "ProgramData, drive roots, and the user profile root) are refused "
                "even with approval. Use search_system first to verify the exact "
                "path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Exact file or folder path, preferably absolute.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unzip_file",
            "description": (
                "Extract a .zip archive into a folder anywhere outside protected "
                "Windows locations. This is a modifying action and requires "
                "explicit user approval before it runs. Nested folders are created, "
                "existing files may be overwritten inside the extraction folder, "
                "archive entries that escape the destination are rejected, and "
                "extraction is limited by entry count and total size."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "archive_path": {
                        "type": "string",
                        "description": "Existing .zip archive, preferably absolute.",
                    },
                    "extract_to": {
                        "type": "string",
                        "description": "Destination folder for extracted files.",
                    },
                },
                "required": ["archive_path", "extract_to"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_content",
            "description": (
                "Read a file from anywhere on the local file system. Read-only, so "
                "no approval is required. Returns text for text files and metadata "
                "without raw bytes for binary or image files. Files larger than the "
                "configured limit are refused. Use search_system first when the "
                "path is unknown."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Exact file path, preferably absolute.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_system",
            "description": (
                "Recursively search the local file system for files by name, "
                "extension, size, and modification time. Read-only, so no approval "
                "is required. Provide a root folder (use a drive root such as "
                "'C:\\' or set all_drives=true for every drive). Results include "
                "full paths and metadata. Searches are capped by result count, "
                "files scanned, and elapsed time; check 'truncated' before "
                "concluding that no more matches exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "description": (
                            "Folder or drive root to search, preferably absolute. "
                            "Relative paths resolve against the user's home folder."
                        ),
                    },
                    "name_contains": {
                        "type": "string",
                        "description": (
                            "Case-insensitive part of the file name, for example "
                            "'invoice'."
                        ),
                    },
                    "extension": {
                        "type": "string",
                        "description": (
                            "File extension filter, for example '.pdf' or 'pdf'."
                        ),
                    },
                    "modified_after": {
                        "type": "string",
                        "description": "ISO date/time; only newer files match.",
                    },
                    "modified_before": {
                        "type": "string",
                        "description": "ISO date/time; only older files match.",
                    },
                    "min_size": {
                        "type": "integer",
                        "description": "Minimum size in bytes.",
                    },
                    "max_size": {
                        "type": "integer",
                        "description": "Maximum size in bytes.",
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "Include hidden files and folders.",
                    },
                    "all_drives": {
                        "type": "boolean",
                        "description": (
                            "Search every available drive. Only set this when the "
                            "user explicitly asks for all drives or the whole "
                            "computer."
                        ),
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": (
                            "Search subfolders. Set false when the user asks for "
                            "files directly in a folder (for example the newest "
                            "files directly in Downloads or Documents)."
                        ),
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": [
                            "modified_desc",
                            "modified_asc",
                            "name",
                            "size_desc",
                            "size_asc",
                            "none",
                        ],
                        "description": (
                            "Result order. Use 'modified_desc' for questions about "
                            "the latest/newest files, 'modified_asc' for the oldest, "
                            "and 'name' or size orders when requested."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": (
                            "Maximum number of matches to return. Use the number "
                            "the user asked for (for example 5 for 'the last 5 "
                            "files')."
                        ),
                    },
                },
                "required": ["root"],
                "additionalProperties": False,
            },
        },
    },
]
