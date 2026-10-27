import type {
  ChatMessage,
  PendingAction,
  SearchResultData,
  ToolStatusData,
} from "../types";

/**
 * Deterministic fixtures for the Phase 24 visual QA matrix.
 *
 * They are only mounted when the shell receives `--qa demo=<kind>`; the live
 * conversation is never touched. This is how every core state can be captured
 * without typing into the app or waiting for a real model turn.
 */
export type DemoKind = "none" | "empty" | "reply" | "search" | "tools" | "approval";

export type DemoScene = {
  messages: ChatMessage[];
  pending: PendingAction | null;
  streaming: boolean;
};

const QUESTION = "What changed in my Downloads folder this week?";

const REPLY = `## Downloads this week

You added **12 files** and removed 3. The newest item is
\`Invoice_2026-10-21.pdf\` from yesterday at 17:41.

- \`Invoice_2026-10-21.pdf\` - 480 KB
- \`screenshot-2026-10-24.png\` - 1.2 MB
- \`project-brief.docx\` - 96 KB

Most of the new files are PDFs. I can move them into a folder by type if you
want.`;

const PARTIAL_SEARCH = `Found 4 invoice PDFs in Downloads. The newest one is
\`Invoice_2026-10-21.pdf\` from yesterday, followed by three older invoices from
September.`;

const SEARCH_TREE: SearchResultData = {
  results: [
    {
      path: "C:\\Users\\Flo\\Downloads\\Invoice_2026-10-21.pdf",
      name: "Invoice_2026-10-21.pdf",
      type: "file",
      extension: "pdf",
      size: 491_520,
      modified: "2026-10-26 17:41",
    },
    {
      path: "C:\\Users\\Flo\\Downloads\\Invoice_2026-09-30.pdf",
      name: "Invoice_2026-09-30.pdf",
      type: "file",
      extension: "pdf",
      size: 458_752,
      modified: "2026-09-30 09:12",
    },
    {
      path: "C:\\Users\\Flo\\Downloads\\Invoice_2026-09-14.pdf",
      name: "Invoice_2026-09-14.pdf",
      type: "file",
      extension: "pdf",
      size: 502_784,
      modified: "2026-09-14 18:03",
    },
  ],
  tree: [
    {
      name: "Downloads",
      path: "C:\\Users\\Flo\\Downloads",
      type: "folder",
      matched: true,
      is_root: true,
      children_count: 3,
      has_children: true,
      children: [
        {
          name: "Invoice_2026-10-21.pdf",
          path: "C:\\Users\\Flo\\Downloads\\Invoice_2026-10-21.pdf",
          type: "file",
          matched: true,
          extension: "pdf",
          size: 491_520,
          modified: "2026-10-26 17:41",
        },
        {
          name: "Invoice_2026-09-30.pdf",
          path: "C:\\Users\\Flo\\Downloads\\Invoice_2026-09-30.pdf",
          type: "file",
          matched: true,
          extension: "pdf",
          size: 458_752,
          modified: "2026-09-30 09:12",
        },
        {
          name: "Invoice_2026-09-14.pdf",
          path: "C:\\Users\\Flo\\Downloads\\Invoice_2026-09-14.pdf",
          type: "file",
          matched: true,
          extension: "pdf",
          size: 502_784,
          modified: "2026-09-14 18:03",
        },
      ],
    },
  ],
  query: { name_contains: "invoice", kind: "files", recursive: true },
  summary: { total: 3, files: 3, folders: 0, total_size: 1_453_056 },
  roots: ["C:\\Users\\Flo\\Downloads"],
  duration_seconds: 0.42,
};

const TOOL_STATUS: ToolStatusData = {
  state: "executing",
  label: "Organizing your Downloads folder",
  tool: "move_file",
  plannedTotal: 3,
  startedAt: Date.now() - 4200,
  tasks: [
    {
      id: "scan",
      label: "Scanning Downloads",
      tool: "search_files",
      state: "done",
      detail: "12 files",
    },
    {
      id: "plan",
      label: "Grouping files by type",
      state: "done",
      detail: "3 folders",
    },
    {
      id: "move",
      label: "Moving invoice PDFs",
      tool: "move_file",
      state: "running",
    },
  ],
};

const APPROVAL: PendingAction = {
  approval_id: "demo-approval",
  conversation_id: "demo",
  kind: "approval",
  message: "I will tidy the invoice files now.",
  request: "Organize my Downloads folder by file type.",
  actions: [
    {
      tool: "move_file",
      description: "Move 4 invoice PDFs into Documents\\Invoices",
      arguments: {
        source: "C:\\Users\\Flo\\Downloads",
        destination: "C:\\Users\\Flo\\Documents\\Invoices",
      },
    },
  ],
  plan: [
    "Scan Downloads for invoice PDFs",
    "Create Documents\\Invoices",
    "Move the 4 PDFs",
    "Report the result",
  ],
  expires_in_seconds: 600,
};

function message(
  id: string,
  role: ChatMessage["role"],
  content: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage {
  return { id: `demo-${id}`, role, content, ...extra };
}

export function demoScene(kind: DemoKind): DemoScene | null {
  if (kind === "none") {
    return null;
  }
  if (kind === "empty") {
    // Hide any real history so the palette can be captured collapsed.
    return { messages: [], pending: null, streaming: false };
  }
  const question = message("question", "user", QUESTION);

  if (kind === "reply") {
    return {
      messages: [
        question,
        message("answer", "assistant", REPLY, {
          streaming: true,
          metrics: { live: true, completion_tokens: 84, tokens_per_second: 21.4 },
        }),
      ],
      pending: null,
      streaming: true,
    };
  }

  if (kind === "search") {
    return {
      messages: [
        message("search-question", "user", "Find the newest invoices in Downloads."),
        message("search-status", "status", "", { status: { ...TOOL_STATUS, label: "Searching your files", tool: "search_files", tasks: undefined } }),
        message("search-tree", "search", "", { search: SEARCH_TREE }),
        message("search-answer", "assistant", PARTIAL_SEARCH, { streaming: true }),
      ],
      pending: null,
      streaming: true,
    };
  }

  if (kind === "tools") {
    return {
      messages: [
        message("tools-question", "user", "Organize my Downloads folder by file type."),
        message("tools-status", "status", "", { status: TOOL_STATUS }),
        message(
          "tools-answer",
          "assistant",
          "Scanning finished. I am moving the invoice PDFs into `Documents\\Invoices` now.",
          { streaming: true },
        ),
      ],
      pending: null,
      streaming: true,
    };
  }

  // approval
  return {
    messages: [
      message("approval-question", "user", "Organize my Downloads folder by file type."),
      message(
        "approval-answer",
        "assistant",
        "That touches 12 files across three folders, so it needs your approval first.",
      ),
    ],
    pending: APPROVAL,
    streaming: false,
  };
}
