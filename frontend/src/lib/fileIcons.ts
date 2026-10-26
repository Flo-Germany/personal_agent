import {
  File,
  FileArchive,
  FileAudio,
  FileCode,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileVideo,
  Presentation,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { IMAGE_EXTENSIONS } from "./localFiles";

const DOCUMENT_EXTENSIONS = new Set([
  "pdf",
  "doc",
  "docx",
  "odt",
  "rtf",
  "txt",
  "md",
  "markdown",
]);
const CODE_EXTENSIONS = new Set([
  "py",
  "js",
  "jsx",
  "ts",
  "tsx",
  "css",
  "html",
  "htm",
  "json",
  "xml",
  "yaml",
  "yml",
  "ps1",
  "sh",
  "bat",
  "cmd",
  "java",
  "c",
  "cpp",
  "h",
  "rs",
  "go",
  "sql",
]);
const SHEET_EXTENSIONS = new Set(["xls", "xlsx", "csv", "ods"]);
const ARCHIVE_EXTENSIONS = new Set(["zip", "rar", "7z", "tar", "gz", "iso"]);
const VIDEO_EXTENSIONS = new Set(["mp4", "mov", "avi", "mkv", "webm", "wmv"]);
const AUDIO_EXTENSIONS = new Set(["mp3", "wav", "flac", "ogg", "m4a", "aac"]);
const PRESENTATION_EXTENSIONS = new Set(["ppt", "pptx", "odp"]);

/** Pick a file-type icon from an extension (no leading dot). */
export function iconForExtension(extension: string): LucideIcon {
  if (DOCUMENT_EXTENSIONS.has(extension)) {
    return FileText;
  }
  if (CODE_EXTENSIONS.has(extension)) {
    return FileCode;
  }
  if (SHEET_EXTENSIONS.has(extension)) {
    return FileSpreadsheet;
  }
  if (ARCHIVE_EXTENSIONS.has(extension)) {
    return FileArchive;
  }
  if (VIDEO_EXTENSIONS.has(extension)) {
    return FileVideo;
  }
  if (AUDIO_EXTENSIONS.has(extension)) {
    return FileAudio;
  }
  if (PRESENTATION_EXTENSIONS.has(extension)) {
    return Presentation;
  }
  if (IMAGE_EXTENSIONS.has(extension)) {
    return FileImage;
  }
  return File;
}

export function extensionLabel(extension: string): string {
  return extension ? `${extension.toUpperCase()} file` : "File";
}
