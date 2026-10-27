import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { UnlistenFn } from "@tauri-apps/api/event";

/** Mirror of `DesktopSettings` in `src-tauri/src/desktop.rs`. */
export type DesktopMode = "classic" | "modern";
export type DesktopBackdrop = "mica" | "solid";

export type DesktopSettings = {
  shortcut: string;
  notifications: boolean;
  mode: DesktopMode;
  backdrop: DesktopBackdrop;
  context_menu: boolean;
};

export type FrameStats = {
  fps: number;
  max_frame_ms: number;
  frames: number;
  long_frames: number;
  mode: string;
  at: number;
};

export type DesktopState = {
  shortcut_registered: boolean;
  shortcut_error: string;
  notifications_status: string;
  notification_polls: number;
  context_menu_error: string;
  backdrop_error: string;
  qa_error: string;
  exited: boolean;
  frame_stats: FrameStats;
};

export type DesktopSnapshot = {
  settings: DesktopSettings;
  state: DesktopState;
  context_menu_command: string | null;
  version: string;
};

export const DEFAULT_DESKTOP_SETTINGS: DesktopSettings = {
  shortcut: "Ctrl+Shift+Space",
  notifications: false,
  mode: "classic",
  backdrop: "mica",
  context_menu: false,
};

export const EMPTY_DESKTOP_STATE: DesktopState = {
  shortcut_registered: false,
  shortcut_error: "",
  notifications_status: "disabled",
  notification_polls: 0,
  context_menu_error: "",
  backdrop_error: "",
  qa_error: "",
  exited: false,
  frame_stats: {
    fps: 0,
    max_frame_ms: 0,
    frames: 0,
    long_frames: 0,
    mode: "",
    at: 0,
  },
};

export const EVENTS = {
  summon: "compa://summon",
  analyze: "compa://analyze",
  settings: "compa://settings",
  notification: "compa://notification",
  qaRender: "compa://qa-render",
  ui: "compa://ui",
  demo: "compa://qa-demo",
  qaAudit: "compa://qa-audit",
} as const;

export function isDesktop(): boolean {
  return isTauri();
}

const EMPTY_SNAPSHOT: DesktopSnapshot = {
  settings: DEFAULT_DESKTOP_SETTINGS,
  state: EMPTY_DESKTOP_STATE,
  context_menu_command: null,
  version: "",
};

/** Read settings + live hook state. Browser previews get safe defaults. */
export async function loadDesktop(): Promise<DesktopSnapshot> {
  if (!isDesktop()) {
    return browserPreviewSnapshot();
  }
  try {
    const snapshot = await invoke<DesktopSnapshot>("get_desktop_state");
    return {
      ...EMPTY_SNAPSHOT,
      ...snapshot,
      settings: { ...DEFAULT_DESKTOP_SETTINGS, ...snapshot.settings },
      state: { ...EMPTY_DESKTOP_STATE, ...snapshot.state },
    };
  } catch {
    return EMPTY_SNAPSHOT;
  }
}

/**
 * Plain `vite` previews cannot call the shell. Query flags let the design be
 * reviewed in a browser without faking Tauri: `?mode=modern&backdrop=solid`.
 */
function browserPreviewSnapshot(): DesktopSnapshot {
  const params = new URLSearchParams(window.location.search);
  const mode: DesktopMode = params.get("mode") === "modern" ? "modern" : "classic";
  const backdrop: DesktopBackdrop =
    params.get("backdrop") === "solid" ? "solid" : "mica";
  const notifications = params.get("notifications") === "on";
  return {
    settings: {
      ...DEFAULT_DESKTOP_SETTINGS,
      mode,
      backdrop,
      notifications,
      context_menu: params.get("context_menu") === "on",
    },
    state: {
      ...EMPTY_DESKTOP_STATE,
      shortcut_registered: true,
      notifications_status: notifications ? "active" : "disabled",
    },
    context_menu_command: null,
    version: "preview",
  };
}

/** Persist settings. Rejects with the Rust error string (shortcut conflicts). */
export async function saveDesktop(
  settings: DesktopSettings,
): Promise<DesktopSettings> {
  if (!isDesktop()) {
    return settings;
  }
  return invoke<DesktopSettings>("save_desktop_settings", { settings });
}

/** Resize the modern palette; ignored by the classic fixed-size window. */
export async function setPaletteHeight(height: number): Promise<void> {
  if (!isDesktop()) {
    return;
  }
  try {
    await invoke("set_palette_height", { height });
  } catch {
    // Resizing is cosmetic; never break the chat over it.
  }
}

export async function setPaletteExpanded(expanded: boolean): Promise<void> {
  if (!isDesktop()) {
    return;
  }
  try {
    await invoke("set_palette_expanded", { expanded });
  } catch {
    // Cosmetic.
  }
}

export async function takePendingAnalyze(): Promise<string | null> {
  if (!isDesktop()) {
    return null;
  }
  try {
    return await invoke<string | null>("take_pending_analyze");
  } catch {
    return null;
  }
}

export async function recordFrameStats(stats: {
  fps: number;
  maxFrameMs: number;
  frames: number;
  longFrames: number;
  mode: "live" | "stress";
}): Promise<void> {
  if (!isDesktop()) {
    return;
  }
  try {
    await invoke("record_frame_stats", {
      fps: stats.fps,
      maxFrameMs: stats.maxFrameMs,
      frames: stats.frames,
      longFrames: stats.longFrames,
      mode: stats.mode,
    });
  } catch {
    // Metrics are diagnostics only.
  }
}

export async function quitApp(): Promise<void> {
  if (!isDesktop()) {
    return;
  }
  try {
    await invoke("quit_app");
  } catch {
    // Nothing else to do if the shell is already gone.
  }
}

/** Hide the palette back to the tray (same as Esc in modern mode). */
export async function hidePalette(): Promise<void> {
  if (!isDesktop()) {
    return;
  }
  try {
    await invoke("hide_main");
  } catch {
    // Best effort.
  }
}

async function on(event: string, handler: (payload: unknown) => void) {
  if (!isDesktop()) {
    return () => {};
  }
  try {
    const unlisten: UnlistenFn = await listen(event, (message) =>
      handler(message.payload),
    );
    return unlisten;
  } catch {
    return () => {};
  }
}

export function onSummon(handler: () => void) {
  return on(EVENTS.summon, () => handler());
}

export function onAnalyze(handler: (path: string) => void) {
  return on(EVENTS.analyze, (payload) => {
    if (typeof payload === "string" && payload) {
      handler(payload);
    }
  });
}

export function onDesktopSettings(handler: (settings: DesktopSettings) => void) {
  return on(EVENTS.settings, (payload) => {
    if (payload && typeof payload === "object") {
      handler({ ...DEFAULT_DESKTOP_SETTINGS, ...(payload as DesktopSettings) });
    }
  });
}

export function onQaRender(handler: () => void) {
  return on(EVENTS.qaRender, () => handler());
}

/** Phase 24: deterministic fixture state for the visual QA matrix. */
export function onDemo(handler: (kind: string) => void) {
  return on(EVENTS.demo, (payload) => {
    if (typeof payload === "string") {
      handler(payload);
    }
  });
}

/** Phase 24: force a fresh layout/contrast audit for the current state. */
export function onQaAudit(handler: () => void) {
  return on(EVENTS.qaAudit, () => handler());
}

/** QA hook: force a UI view without synthetic mouse input. */
export function onUi(handler: (view: string) => void) {
  return on(EVENTS.ui, (payload) => {
    if (typeof payload === "string") {
      handler(payload);
    }
  });
}

const MODIFIER_CODES = new Set([
  "ControlLeft",
  "ControlRight",
  "ShiftLeft",
  "ShiftRight",
  "AltLeft",
  "AltRight",
  "MetaLeft",
  "MetaRight",
  "OSLeft",
  "OSRight",
  "CapsLock",
]);

/** Build a Rust-compatible shortcut (`Ctrl+Shift+Space`) from a keydown. */
export function shortcutFromEvent(event: KeyboardEvent): string | null {
  if (MODIFIER_CODES.has(event.code) || !event.code) {
    return null;
  }
  const parts: string[] = [];
  if (event.ctrlKey) parts.push("Ctrl");
  if (event.shiftKey) parts.push("Shift");
  if (event.altKey) parts.push("Alt");
  if (event.metaKey) parts.push("Win");
  if (parts.length === 0) {
    return null;
  }
  parts.push(event.code);
  return parts.join("+");
}

/** Human label: `Ctrl+Shift+KeyK` -> `Ctrl + Shift + K`. */
export function prettyShortcut(value: string): string {
  if (!value) {
    return "";
  }
  return value
    .split("+")
    .map((part) => {
      const token = part.trim();
      if (/^Key[A-Z]$/.test(token)) return token.slice(3);
      if (/^Digit\d$/.test(token)) return token.slice(5);
      if (/^Numpad\d$/.test(token)) return `Num ${token.slice(6)}`;
      if (token === "Escape") return "Esc";
      if (token === "Space") return "Space";
      if (token === "ArrowUp") return "↑";
      if (token === "ArrowDown") return "↓";
      if (token === "ArrowLeft") return "←";
      if (token === "ArrowRight") return "→";
      return token;
    })
    .join(" + ");
}
