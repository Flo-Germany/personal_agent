import { convertFileSrc, isTauri } from "@tauri-apps/api/core";

export const IMAGE_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "webp",
  "gif",
  "bmp",
  "tif",
  "tiff",
  "svg",
  "avif",
  "ico",
]);

export function fileName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function fileExtension(path: string): string {
  const name = fileName(path);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function isImagePath(path: string): boolean {
  return IMAGE_EXTENSIONS.has(fileExtension(path));
}

/** Decode a file URL or raw Windows path into a native absolute path. */
export function localPathFromHref(
  href: string | null | undefined,
): string | null {
  if (!href) {
    return null;
  }

  let value = href.trim();
  if (!value) {
    return null;
  }

  if (
    /^(?:https?|mailto|tel|data|javascript):/i.test(value) ||
    value.startsWith("#")
  ) {
    return null;
  }

  if (/^file:/i.test(value)) {
    let rest = value.replace(/^file:\/\//i, "");
    rest = rest.replace(/^localhost(?=\/)/i, "");
    rest = rest.replace(/^\/+/, "");

    try {
      rest = decodeURIComponent(rest);
    } catch {
      // Keep the undecoded value; it may still be a valid path.
    }

    rest = rest.replace(/\//g, "\\");

    if (/^[A-Za-z]:\\/.test(rest)) {
      return rest;
    }

    return null;
  }

  if (/^[A-Za-z]:(?:%5c|[\\/])/i.test(value)) {
    try {
      value = decodeURIComponent(value);
    } catch {
      // Ignore malformed escapes and try the raw value.
    }

    value = value.replace(/\//g, "\\");

    if (/^[A-Za-z]:\\/.test(value)) {
      return value;
    }
  }

  // UNC paths are rejected: revealing one makes Windows authenticate to a
  // remote host (NTLM hash disclosure). The agent only handles local drives.
  return null;
}

/** True only for a local drive path (never UNC, never a device path). */
export function isSafeLocalPath(path: string | null | undefined): boolean {
  if (!path) return false;
  return /^[A-Za-z]:[\\/]/.test(path) && !path.startsWith("\\\\");
}

/** Accept a raw absolute path, optionally wrapped in quotes. */
export function localPathFromText(
  text: string | null | undefined,
): string | null {
  if (!text) {
    return null;
  }

  const value = text.trim().replace(/^["'`]|["'`]$/g, "");
  if (/^(?:file:\/\/\/?)?[A-Za-z]:[\\/]/.test(value)) {
    return localPathFromHref(value) ?? value.replace(/\//g, "\\");
  }

  // UNC paths are rejected: revealing one makes Windows authenticate to a
  // remote host (NTLM hash disclosure). The agent only handles local drives.
  return null;
}

/** Build a valid file:/// URL for markdown/preview use. */
export function toFileUrl(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").map((part) =>
    /^[A-Za-z]:$/.test(part) ? part : encodeURIComponent(part),
  );
  return `file:///${parts.join("/")}`;
}

/** Tauri-safe image source. In a browser preview, falls back to file:///. */
export function localFileSrc(path: string): string {
  if (isTauri()) {
    return convertFileSrc(path);
  }
  return toFileUrl(path);
}

/** Collect canonical local paths returned by tool calls, for guaranteed rendering. */
export function extractToolFiles(
  tool: string | undefined,
  result: Record<string, unknown> | undefined,
): string[] {
  if (!result || result.ok === false) {
    return [];
  }

  // Diagnosis charts are drawn natively inside the chat report; do not also
  // show them as a separate image attachment gallery.
  if (tool === "system_diagnosis") {
    return [];
  }

  // Search hits are rendered as a file tree card, including folders.
  if (tool === "search_system") {
    return [];
  }

  const candidates: string[] = [];
  const results = result.results;

  if (Array.isArray(results)) {
    for (const item of results) {
      if (item && typeof item === "object") {
        const path = (item as { path?: unknown }).path;
        if (typeof path === "string") {
          candidates.push(path);
        }
      }
    }
  }

  // Tools such as system_diagnosis return a canonical file list.
  const fileList = result.files;
  if (Array.isArray(fileList)) {
    for (const item of fileList) {
      if (typeof item === "string") {
        candidates.push(item);
      }
    }
  }

  if (tool === "create_file" || tool === "read_file_content") {
    if (typeof result.path === "string") {
      candidates.push(result.path);
    }
  }

  if (tool === "copy_file" || tool === "move_file_or_folder") {
    if (typeof result.destination_path === "string") {
      candidates.push(result.destination_path);
    }
  }

  const seen = new Set<string>();
  const files: string[] = [];

  for (const candidate of candidates) {
    const path = localPathFromText(candidate);
    if (!path) {
      continue;
    }

    const key = path.toLowerCase();
    if (seen.has(key)) {
      continue;
    }

    seen.add(key);
    files.push(path);

    if (files.length >= 12) {
      break;
    }
  }

  return files;
}

/** True when the model's reply already contains this exact local path/URL. */
export function replyMentionsPath(reply: string, path: string): boolean {
  const haystack = reply.toLowerCase();
  const forward = path.replace(/\\/g, "/");
  const variants = [
    path,
    forward,
    toFileUrl(path),
    encodeURI(forward),
  ];

  return variants.some((variant) =>
    haystack.includes(variant.toLowerCase()),
  );
}

/** Remove invented external image embeds from local-file answer turns. */
export function stripExternalImages(reply: string): string {
  return reply
    .replace(/!\[[^\]]*\]\((?:https?:)?\/\/[^)\s]+\)/gi, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
