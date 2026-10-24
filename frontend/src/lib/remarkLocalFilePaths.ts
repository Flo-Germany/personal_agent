import type { Plugin } from "unified";

import { localPathFromHref, localPathFromText, toFileUrl } from "./localFiles";

type MdNode = {
  type?: string;
  value?: string;
  url?: string;
  children?: MdNode[];
};

const FILE_EXTENSIONS = [
  "png",
  "jpe?g",
  "webp",
  "gif",
  "bmp",
  "tiff?",
  "svg",
  "avif",
  "ico",
  "pdf",
  "docx?",
  "xlsx?",
  "pptx?",
  "txt",
  "md",
  "markdown",
  "csv",
  "json",
  "xml",
  "ya?ml",
  "zip",
  "rar",
  "7z",
  "tar",
  "gz",
  "mp4",
  "mov",
  "avi",
  "mkv",
  "webm",
  "mp3",
  "wav",
  "flac",
  "ogg",
  "exe",
  "msi",
  "iso",
  "py",
  "jsx?",
  "tsx?",
  "css",
  "html?",
  "ps1",
  "sh",
  "bat",
  "cmd",
  "java",
  "c(?:pp)?",
  "h",
  "rs",
  "go",
  "sql",
  "db",
  "sqlite3?",
].join("|");

const LOCAL_PATH_PATTERN = new RegExp(
  String.raw`(?<![\w/])(?:file:\/\/\/?[A-Za-z]:[\\/][^\r\n<>"|?*]*?\.(?:${FILE_EXTENSIONS})|[A-Za-z]:[\\/][^\r\n<>"|?*]*?\.(?:${FILE_EXTENSIONS}))(?![\w])`,
  "gi",
);

function splitText(value: string): MdNode[] | null {
  LOCAL_PATH_PATTERN.lastIndex = 0;
  const parts: MdNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = LOCAL_PATH_PATTERN.exec(value)) !== null) {
    const raw = match[0];
    const path = raw.toLowerCase().startsWith("file:")
      ? localPathFromHref(raw)
      : localPathFromText(raw);

    if (!path) {
      continue;
    }

    if (match.index > cursor) {
      parts.push({ type: "text", value: value.slice(cursor, match.index) });
    }

    parts.push({
      type: "link",
      url: toFileUrl(path),
      children: [{ type: "text", value: path }],
    });

    cursor = match.index + raw.length;
  }

  if (parts.length === 0) {
    return null;
  }

  if (cursor < value.length) {
    parts.push({ type: "text", value: value.slice(cursor) });
  }

  return parts;
}

const SKIP_NODE_TYPES = new Set([
  "link",
  "image",
  "code",
  "inlineCode",
  "html",
]);

function walk(node: MdNode): void {
  if (!node.children) {
    return;
  }

  const next: MdNode[] = [];

  for (const child of node.children) {
    if (child.type === "text" && typeof child.value === "string") {
      const parts = splitText(child.value);
      if (parts) {
        next.push(...parts);
        continue;
      }
    }

    if (!SKIP_NODE_TYPES.has(child.type ?? "")) {
      walk(child);
    }

    next.push(child);
  }

  node.children = next;
}

/** Turn bare Windows paths in prose into file links before rendering. */
export const remarkLocalFilePaths: Plugin = () => (tree) => {
  walk(tree as unknown as MdNode);
};
