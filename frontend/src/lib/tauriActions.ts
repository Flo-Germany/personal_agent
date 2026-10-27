import { isTauri } from "@tauri-apps/api/core";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import { open } from "@tauri-apps/plugin-shell";

import { isSafeLocalPath } from "./localFiles";

export async function openLocalFile(path: string): Promise<void> {
  if (!isTauri() || !isSafeLocalPath(path)) {
    return;
  }

  try {
    await open(path);
  } catch {
    // The OS may not have a handler; fail quietly in the chat UI.
  }
}

export async function revealLocalFile(path: string): Promise<void> {
  if (!isTauri() || !isSafeLocalPath(path)) {
    return;
  }

  try {
    await revealItemInDir(path);
  } catch {
    // Explorer can fail for removed or protected files.
  }
}

export async function copyLocalPath(path: string): Promise<boolean> {
  try {
    if (isTauri()) {
      await writeText(path);
    } else {
      await navigator.clipboard.writeText(path);
    }
    return true;
  } catch {
    try {
      await navigator.clipboard.writeText(path);
      return true;
    } catch {
      return false;
    }
  }
}
