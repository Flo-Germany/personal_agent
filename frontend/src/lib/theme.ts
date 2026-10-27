/** Theme and motion preferences shared by the shell and the settings UI. */

export type ThemePreference = "system" | "light" | "dark";

export const THEME_STORAGE_KEY = "compa.theme";
export const MOTION_STORAGE_KEY = "compa.animations";

const LIGHT_QUERY = "(prefers-color-scheme: light)";

export function resolveTheme(preference: ThemePreference): "light" | "dark" {
  if (preference === "light" || preference === "dark") {
    return preference;
  }
  if (typeof window === "undefined" || !window.matchMedia) {
    return "dark";
  }
  return window.matchMedia(LIGHT_QUERY).matches ? "light" : "dark";
}

function readStoredPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") {
      return stored;
    }
  } catch {
    // Private mode or a disabled storage backend: fall back to system.
  }
  return "system";
}

function readStoredAnimations(): boolean {
  try {
    return window.localStorage.getItem(MOTION_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

/** Apply the resolved theme and motion flag to `<html>`. */
export function applyAppearance(
  preference: ThemePreference,
  animations: boolean,
): "light" | "dark" {
  const resolved = resolveTheme(preference);
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.dataset.themePreference = preference;
  root.dataset.motion = animations ? "on" : "off";
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    window.localStorage.setItem(MOTION_STORAGE_KEY, animations ? "on" : "off");
  } catch {
    // Persisting the local mirror is best effort.
  }
  return resolved;
}

/**
 * Paint the last known appearance before React mounts so a dark window never
 * flashes light (or the reverse) while desktop.json is being read.
 */
export function applyBootAppearance(): void {
  applyAppearance(readStoredPreference(), readStoredAnimations());
}

/** Subscribe to OS theme changes; returns an unsubscribe function. */
export function watchSystemTheme(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) {
    return () => {};
  }
  const query = window.matchMedia(LIGHT_QUERY);
  const handler = () => onChange();
  query.addEventListener("change", handler);
  return () => query.removeEventListener("change", handler);
}
