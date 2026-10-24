import { isTauri } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";

/** Hide the frameless window to the tray. No-op in a plain browser preview. */
export async function hideToTray(): Promise<void> {
  if (!isTauri()) {
    return;
  }

  try {
    await getCurrentWindow().hide();
  } catch {
    // Hiding is best-effort; the tray and CloseRequested handler still work.
  }
}
