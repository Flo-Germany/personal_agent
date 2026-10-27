import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchEditableSettings,
  saveEditableSettings,
} from "../lib/settingsApi";
import type { EditablePatch, EditableSettings } from "../lib/settingsApi";

/**
 * Loads the editable backend settings and saves patches optimistically.
 *
 * Engine and hardware values only take effect after a reload, so the hook keeps
 * a `restartRequired` flag instead of pretending the change is live.
 */
export function useEditableSettings(onSaved?: () => void) {
  const [settings, setSettings] = useState<EditableSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [restartRequired, setRestartRequired] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  const savedRef = useRef(onSaved);
  savedRef.current = onSaved;

  const reload = useCallback(async () => {
    try {
      const next = await fetchEditableSettings();
      setSettings(next);
      setError("");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Could not load settings",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const patch = useCallback(
    async (changes: EditablePatch): Promise<boolean> => {
      const previous = settings;
      // The controls must react immediately; the server response wins after.
      if (previous) {
        setSettings({ ...previous, ...changes });
      }
      setSaving(true);
      setError("");
      try {
        const result = await saveEditableSettings(changes);
        setSettings(result.settings);
        if (result.restartRequired) {
          setRestartRequired(true);
        }
        setSavedAt(Date.now());
        savedRef.current?.();
        return true;
      } catch (cause) {
        setSettings(previous);
        setError(cause instanceof Error ? cause.message : "Could not save");
        return false;
      } finally {
        setSaving(false);
      }
    },
    [settings],
  );

  return {
    settings,
    loading,
    saving,
    error,
    restartRequired,
    savedAt,
    patch,
    reload,
    clearRestart: () => setRestartRequired(false),
  };
}
