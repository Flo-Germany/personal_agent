import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  DEFAULT_DESKTOP_SETTINGS,
  EMPTY_DESKTOP_STATE,
  isDesktop,
  loadDesktop,
  onDesktopSettings,
  saveDesktop,
} from "../lib/desktop";
import type {
  DesktopSettings,
  DesktopState,
} from "../lib/desktop";

/**
 * Desktop shell settings: global shortcut, Explorer menu, notification hooks
 * and the classic/modern shell mode. Every change goes through the Rust
 * `apply_settings` path so the persisted file, the OS hooks and the UI never
 * disagree.
 */
export function useDesktop() {
  const [settings, setSettings] = useState<DesktopSettings>(
    DEFAULT_DESKTOP_SETTINGS,
  );
  const [state, setState] = useState<DesktopState>(EMPTY_DESKTOP_STATE);
  const [contextMenuCommand, setContextMenuCommand] = useState<string | null>(
    null,
  );
  const [ready, setReady] = useState(!isDesktop());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const settingsRef = useRef(settings);
  settingsRef.current = settings;

  const refresh = useCallback(async () => {
    const snapshot = await loadDesktop();
    setSettings(snapshot.settings);
    setState(snapshot.state);
    setContextMenuCommand(snapshot.context_menu_command);
    setReady(true);
  }, []);

  const refreshState = useCallback(async () => {
    const snapshot = await loadDesktop();
    setState(snapshot.state);
    setContextMenuCommand(snapshot.context_menu_command);
  }, []);

  useEffect(() => {
    void refresh();
    let unlisten: (() => void) | undefined;
    void onDesktopSettings((next) => {
      setSettings(next);
      void refreshState();
    }).then((off) => {
      unlisten = off;
    });
    return () => {
      unlisten?.();
    };
  }, [refresh, refreshState]);

  const update = useCallback(
    async (patch: Partial<DesktopSettings>) => {
      const previous = settingsRef.current;
      const next = { ...previous, ...patch };
      // Optimistic UI: the panel reacts instantly, then the hook result wins.
      setSettings(next);
      setError("");
      setBusy(true);
      try {
        const saved = await saveDesktop(next);
        setSettings(saved);
        await refreshState();
        return true;
      } catch (cause) {
        setSettings(previous);
        setError(
          typeof cause === "string"
            ? cause
            : cause instanceof Error
              ? cause.message
              : "Could not save desktop settings",
        );
        await refreshState();
        return false;
      } finally {
        setBusy(false);
      }
    },
    [refreshState],
  );

  return useMemo(
    () => ({
      settings,
      state,
      contextMenuCommand,
      ready,
      busy,
      error,
      update,
      refresh,
      refreshState,
      isModern: settings.mode === "modern",
    }),
    [
      settings,
      state,
      contextMenuCommand,
      ready,
      busy,
      error,
      update,
      refresh,
      refreshState,
    ],
  );
}
