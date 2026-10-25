"""OpenAI-compatible tool schemas for the local agent.

The descriptions are the model's only guide for choosing tools, so they spell
out full-filesystem access, the approval boundary, and the safety rules.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class FunctionSchema(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolSchema(TypedDict):
    type: Literal["function"]
    function: FunctionSchema


TOOL_SCHEMAS: list[ToolSchema] = [
    {
        "type": "function",
        "function": {
            "name": "propose_plan",
            "description": (
                "Record the ordered plan for a multi-step or sequential user "
                "request before executing any modification. Call this once with "
                "short, concrete steps, then immediately call the first real "
                "tool. The plan is shown to the user on the approval card; the "
                "user approves the whole request once and the remaining steps "
                "run automatically. Do not use it for a single action and do "
                "not ask for approval in a text reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 8,
                        "description": (
                            "Short, concrete actions in execution order, for "
                            "example 'Create the reports folder'."
                        ),
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_powershell_query",
            "description": (
                "Run a READ-ONLY PowerShell command to inspect this Windows "
                "machine. No approval is required. Use it for RAM/CPU per "
                "process, process and service details, registry reads, files, "
                "network connections, event logs, and other information "
                "gathering. It rejects state-changing cmdlets, redirection, "
                "file writes, and dynamic code execution. For a process, "
                "'Get-Process -Id <pid> | Select-Object Id,ProcessName,"
                "WorkingSet64,CPU' returns memory in bytes and CPU in processor "
                "seconds. Investigate first and use run_powershell_action for "
                "any change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The PowerShell command to run, for example "
                            "'Get-Process chrome | Sort-Object WorkingSet64 "
                            "-Descending | Select-Object -First 5'."
                        ),
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_powershell_action",
            "description": (
                "Run a state-changing PowerShell command after explicit user "
                "approval. This is the ONLY PowerShell tool allowed to kill or "
                "stop processes, change services or settings, write the "
                "registry, move or delete files, install software, or otherwise "
                "modify Windows. Always query first to find exact process IDs "
                "and paths, then explain precisely what the action will do in "
                "the description field so the user can approve it safely."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The exact PowerShell command to execute, for "
                            "example 'Stop-Process -Id 1234'."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "One clear sentence describing exactly what will "
                            "change on the system, for example 'Stop the "
                            "Notepad process with PID 1234 to free its memory.'"
                        ),
                    },
                },
                "required": ["command", "description"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_program",
            "description": (
                "Launch any installed Windows program by name or executable path. "
                "This is a system-changing action and always requires explicit "
                "user approval before it runs. Accepts an absolute path to an "
                ".exe/.com file, an executable available on PATH, a Start Menu "
                "application name, or an installed app name such as 'chrome', "
                "'code', or 'notepad'. It launches exactly one program with no "
                "arguments and never uses a shell. To open a specific document, "
                "use open_file instead and choose the application explicitly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "program_name": {
                        "type": "string",
                        "description": (
                            "Program name or executable path, for example "
                            "'notepad', 'chrome', or "
                            "'C:\\\\Program Files\\\\App\\\\app.exe'."
                        ),
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
            "name": "open_file",
            "description": (
                "Open one existing local file in an explicitly chosen editor or "
                "program. This launches an application and always requires "
                "explicit user approval. The program is required: if the user did "
                "not name an application, ask which editor or program to use before "
                "calling this tool; never assume a default handler. The program can "
                "be an installed app name or an absolute .exe path. Use "
                "start_program only when launching a program without a document."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Exact path of the existing file to open.",
                    },
                    "program": {
                        "type": "string",
                        "description": (
                            "Editor or program to use, for example 'notepad', "
                            "'code', 'chrome', or an absolute .exe path. Ask the "
                            "user when they have not specified one."
                        ),
                    },
                },
                "required": ["path", "program"],
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
    {
        "type": "function",
        "function": {
            "name": "semantic_image_search",
            "description": (
                "Find local image files anywhere on the user's computer by what "
                "they show, not by filename. Use this when the user asks for "
                "pictures of something (for example 'a red car', 'a dog on a "
                "beach', 'my vacation photos', 'a screenshot of a chart') or asks "
                "to search the whole PC for an image by description. Uses a local "
                "CLIP index that is built and refreshed continuously in the "
                "background. Read-only, so no approval is required. It already "
                "covers the whole indexed computer; do not pass a folder, root, "
                "drive, or all_drives argument. Returns up to "
                "10 absolute paths ranked by visual similarity, with scores, "
                "modified times, and a confidence value. If confidence is low, "
                "the best few candidates are still returned as tentative matches: "
                "present them as possible matches, state that visual similarity is "
                "approximate, sanity-check the file names, and never claim "
                "certainty. If results is empty, say "
                "no visual match was found and ask for a more specific English "
                "description; do not invent images. English descriptions work "
                "best. If the index is still building, the result says so; retry "
                "a little later or tell the user how many images are indexed so "
                "far. Use search_system instead when the user asks by filename, "
                "extension, size, or date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Visual description of the image to find, such as "
                            "'a red car' or 'beach vacation'. Prefer English."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": (
                            "Maximum number of matches to return. Defaults to 5."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_diagnosis",
            "description": (
                "Run a comprehensive, read-only Windows system diagnosis and "
                "generate charts. Use this ONCE for broad requests such as 'analyze "
                "my computer', 'do a system diagnosis', 'check my PC health and "
                "performance', or 'why is my computer slow?'. It collects OS and "
                "hardware facts, live CPU/memory/GPU usage, top processes, disk and "
                "SSD health, services, startup apps, Defender/firewall state, recent "
                "system errors, pending reboot, and small CPU/memory benchmarks, then "
                "writes PNG charts under memory/diagnostics and returns their paths. "
                "Prefer this over many individual PowerShell queries for a general "
                "diagnosis. Read-only; no approval is needed. After it returns, "
                "summarize the findings and include the chart links; do not repeat "
                "the collection with more queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sample_seconds": {
                        "type": "number",
                        "minimum": 2,
                        "maximum": 10,
                        "description": (
                            "Live sampling window in seconds. Defaults to 4."
                        ),
                    },
                    "include_benchmark": {
                        "type": "boolean",
                        "description": (
                            "Run short in-memory CPU and memory benchmarks. "
                            "Defaults to true."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_windows",
            "description": (
                "List all visible top-level application windows on the user's "
                "Windows desktop, including each exact window title and whether "
                "it is the active window. Read-only, so no approval is needed. "
                "Use this first when the user asks which programs are open or "
                "when you need the exact title for read_window_content. Minimized "
                "or hidden windows are not listed."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_window_content",
            "description": (
                "Read the readable text from an open native Windows application "
                "by its exact or partial window title, using Windows UI Automation. "
                "Read-only, so no approval is needed. Use get_open_windows first to "
                "get the title, then use this when the user asks what is written in "
                "a document or open app. Works best with text editors, dialogs, and "
                "accessible documents; some browsers and custom-rendered apps expose "
                "little or no text. If the result says no text was found, use "
                "analyze_screen to look at the visible screen instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {
                        "type": "string",
                        "description": (
                            "Exact or partial title of an open window, for example "
                            "'Untitled - Notepad' or 'report.docx - Word'."
                        ),
                    }
                },
                "required": ["window_title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_screen",
            "description": (
                "Look at the user's screen and answer a question about what is "
                "visible. Read-only and local: captures one display in memory, "
                "sends it only to the locally configured vision model in Ollama, "
                "and returns its text answer. No file is left on disk. Use this "
                "for questions such as 'What is on my screen?', 'Look at my "
                "screen', 'What does this error say?', or any question about "
                "visual layout. When several displays are connected, the app "
                "always asks the user which display to analyze before capturing; "
                "call this tool normally and do not ask which display in a text "
                "reply. Do not use this when the user asks about a specific open "
                "document that read_window_content can read, and do not capture "
                "the screen without a clear screen-related request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_question": {
                        "type": "string",
                        "description": (
                            "The user's question about the screen, including any "
                            "specific visual details to look for."
                        ),
                    },
                    "display": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Internal: 1-based display number. Do not pass this "
                            "yourself. The app sets it after the user chooses a "
                            "display when more than one is connected."
                        ),
                    },
                },
                "required": ["user_question"],
                "additionalProperties": False,
            },
        },
    },
]
