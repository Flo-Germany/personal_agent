import { useEffect, useState } from "react";
import {
  Check,
  Loader2,
  MonitorSmartphone,
  RotateCcw,
  TriangleAlert,
} from "lucide-react";

import { prettyShortcut, shortcutFromEvent } from "../lib/desktop";
import type { DesktopSettings, DesktopState } from "../lib/desktop";
import { Badge, SettingsCard, SettingsRow, Switch } from "./SettingsUi";

type DesktopSettingsSectionProps = {
  settings: DesktopSettings;
  state: DesktopState;
  contextMenuCommand: string | null;
  busy: boolean;
  error: string;
  onUpdate: (patch: Partial<DesktopSettings>) => void;
  onRefreshState: () => void;
};

function notificationChip(state: DesktopState): {
  label: string;
  tone: "neutral" | "positive" | "warning" | "accent";
} {
  const status = state.notifications_status;
  if (status === "active") {
    const polls = state.notification_polls;
    return {
      label: `Listening · ${polls} ${polls === 1 ? "poll" : "polls"}`,
      tone: "positive",
    };
  }
  if (status === "starting") {
    return { label: "Starting listener…", tone: "accent" };
  }
  if (status === "disabled" || status === "") {
    return { label: "Hooks unregistered", tone: "neutral" };
  }
  return { label: status, tone: "warning" };
}

export function DesktopSettingsSection({
  settings,
  state,
  contextMenuCommand,
  busy,
  error,
  onUpdate,
  onRefreshState,
}: DesktopSettingsSectionProps) {
  const [recording, setRecording] = useState(false);
  const [candidate, setCandidate] = useState<string | null>(null);

  // Capture the next chord while recording; Esc cancels instead of closing.
  useEffect(() => {
    if (!recording) {
      return;
    }
    const handler = (event: KeyboardEvent) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "Escape") {
        setRecording(false);
        setCandidate(null);
        return;
      }
      const chord = shortcutFromEvent(event);
      if (chord) {
        setCandidate(chord);
        setRecording(false);
        onUpdate({ shortcut: chord });
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [recording, onUpdate]);

  // The WinRT access prompt can resolve after the toggle; keep the chip fresh.
  useEffect(() => {
    if (!settings.notifications) {
      return;
    }
    const timer = window.setInterval(() => onRefreshState(), 3000);
    return () => window.clearInterval(timer);
  }, [settings.notifications, onRefreshState]);

  const chip = notificationChip(state);
  const shortcutError = state.shortcut_error;

  return (
    <SettingsCard
      title="Windows integration"
      description="System-wide shortcut, Explorer verb and notification hooks."
      action={
        <Badge tone={state.shortcut_registered ? "positive" : "warning"}>
          <MonitorSmartphone className="size-2.5" />
          {state.shortcut_registered ? "Active" : "Check shortcut"}
        </Badge>
      }
    >
      <SettingsRow
        label="Summon shortcut"
        detail="Works system-wide while Compa runs in the tray."
      >
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setCandidate(null);
            setRecording((value) => !value);
          }}
          className={`rounded-lg border px-2 py-1 font-mono text-[10.5px] transition-colors duration-150 ease-out disabled:opacity-40 ${
            recording
              ? "border-accent/50 bg-accent/10 text-accent-ink"
              : "border-fg/[0.1] bg-fg/[0.04] text-fg/74 hover:text-fg/90"
          }`}
        >
          {recording ? "Press keys…" : prettyShortcut(settings.shortcut)}
        </button>
        <button
          type="button"
          disabled={busy || settings.shortcut === "Ctrl+Shift+Space"}
          onClick={() => onUpdate({ shortcut: "Ctrl+Shift+Space" })}
          title="Reset to Ctrl+Shift+Space"
          aria-label="Reset shortcut"
          className="flex size-7 items-center justify-center rounded-lg text-fg/68 transition-colors duration-150 ease-out hover:bg-fg/[0.06] hover:text-fg/80 disabled:opacity-30"
        >
          <RotateCcw className="size-3.5" />
        </button>
      </SettingsRow>
      {candidate && (
        <p className="font-mono text-[10px] text-fg/68">
          requested {prettyShortcut(candidate)}
        </p>
      )}
      {shortcutError ? (
        <p className="flex items-start gap-1.5 text-[10px] leading-4 text-danger-ink">
          <TriangleAlert className="mt-px size-3 shrink-0" />
          {shortcutError}
        </p>
      ) : (
        <p className="flex items-center gap-1.5 text-[10px] text-positive-ink">
          <Check className="size-3" /> registered with Windows
        </p>
      )}

      <SettingsRow
        label="Explorer context menu"
        detail={
          contextMenuCommand ??
          'Adds "Analyze with Compa" to file and folder right-click menus.'
        }
      >
        <Switch
          label="Explorer context menu"
          checked={settings.context_menu}
          disabled={busy}
          onChange={(next) => onUpdate({ context_menu: next })}
        />
      </SettingsRow>
      {state.context_menu_error && (
        <p className="text-[10px] leading-4 text-danger-ink">
          {state.context_menu_error}
        </p>
      )}

      <div className="space-y-2">
        <SettingsRow
          label="Smart notifications"
          detail="Reads new Windows toasts so Compa can react. Off unregisters the hook thread completely."
        >
          <Switch
            label="Smart notifications"
            checked={settings.notifications}
            disabled={busy}
            onChange={(next) => onUpdate({ notifications: next })}
          />
        </SettingsRow>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={chip.tone}>
            {state.notifications_status === "starting" && (
              <Loader2 className="size-2.5 animate-spin" />
            )}
            {chip.label}
          </Badge>
          {!settings.notifications && (
            <span className="text-[10px] text-fg/68">0 threads · 0 polls</span>
          )}
        </div>
      </div>

      {error && (
        <p className="flex items-start gap-1.5 rounded-xl border border-danger/25 bg-danger/[0.07] px-2.5 py-1.5 text-[10.5px] leading-4 text-danger-ink">
          <TriangleAlert className="mt-px size-3 shrink-0" />
          {error}
        </p>
      )}
    </SettingsCard>
  );
}
