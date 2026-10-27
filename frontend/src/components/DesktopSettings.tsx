import { useEffect, useState } from "react";
import {
  Bell,
  BellOff,
  Check,
  Command,
  FolderTree,
  Keyboard,
  Loader2,
  MonitorSmartphone,
  RotateCcw,
  Sparkles,
  TriangleAlert,
} from "lucide-react";

import { prettyShortcut, shortcutFromEvent } from "../lib/desktop";
import type { DesktopSettings, DesktopState } from "../lib/desktop";

type DesktopSettingsSectionProps = {
  settings: DesktopSettings;
  state: DesktopState;
  contextMenuCommand: string | null;
  busy: boolean;
  error: string;
  onUpdate: (patch: Partial<DesktopSettings>) => void;
  onRefreshState: () => void;
};

function Toggle({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-10 shrink-0 rounded-full border transition-colors duration-200 ease-out disabled:opacity-40 ${
        checked
          ? "border-violet-300/35 bg-violet-300/25"
          : "border-white/10 bg-white/[0.05]"
      }`}
    >
      <span
        className={`absolute top-1/2 size-[18px] -translate-y-1/2 rounded-full transition-all duration-200 ease-out ${
          checked ? "left-[20px] bg-violet-100" : "left-[2px] bg-white/45"
        }`}
      />
    </button>
  );
}

function Row({
  icon,
  title,
  detail,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  detail: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start gap-x-2.5 gap-y-2">
      <span className="mt-px flex size-7 shrink-0 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.04] text-white/45">
        {icon}
      </span>
      <div className="min-w-[140px] flex-1">
        <p className="text-[11.5px] leading-4 text-white/80">{title}</p>
        <p className="mt-0.5 text-[10.5px] leading-4 text-white/56">{detail}</p>
      </div>
      {children && <div className="ml-auto flex shrink-0 items-center">{children}</div>}
    </div>
  );
}

function notificationChip(state: DesktopState): { label: string; tone: string } {
  const status = state.notifications_status;
  if (status === "active") {
    const polls = state.notification_polls;
    return {
      label: `Listening · ${polls} ${polls === 1 ? "poll" : "polls"}`,
      tone: "border-emerald-300/20 bg-emerald-300/[0.07] text-emerald-200/75",
    };
  }
  if (status === "starting") {
    return {
      label: "Starting listener…",
      tone: "border-violet-300/20 bg-violet-300/[0.07] text-violet-100/75",
    };
  }
  if (status === "disabled" || status === "") {
    return {
      label: "Hooks unregistered",
      tone: "border-white/[0.08] bg-white/[0.03] text-white/58",
    };
  }
  return {
    label: status,
    tone: "border-amber-300/20 bg-amber-300/[0.06] text-amber-100/75",
  };
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
    <section className="rounded-xl border border-white/[0.06] bg-white/[0.018] p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex size-7 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.04] text-white/45">
          <MonitorSmartphone className="size-3.5" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-[11.5px] leading-4 text-white/80">Windows integration</p>
          <p className="text-[10.5px] leading-4 text-white/56">
            Shortcut, Explorer menu and notification hooks.
          </p>
        </div>
      </div>

      <div className="mt-3 space-y-3 border-t border-white/[0.05] pt-3">
        <Row
          icon={<Command className="size-3.5" />}
          title="Interface"
          detail="Classic keeps the full chat window. Modern turns Compa into a floating command palette."
        >
          <div className="flex shrink-0 overflow-hidden rounded-lg border border-white/[0.07] bg-black/25 p-0.5">
            {(["classic", "modern"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                disabled={busy}
                aria-pressed={settings.mode === mode}
                onClick={() => onUpdate({ mode })}
                className={`px-2 py-1 text-[10.5px] capitalize transition-colors duration-150 ease-out disabled:opacity-40 ${
                  settings.mode === mode
                    ? "rounded-md bg-white/[0.09] text-white/90"
                    : "rounded-md text-white/58 hover:text-white/70"
                }`}
              >
                {mode}
              </button>
            ))}
          </div>
        </Row>

        {settings.mode === "modern" && (
          <Row
            icon={<Sparkles className="size-3.5" />}
            title="Backdrop"
            detail={
              settings.backdrop === "mica"
                ? "Native Windows 11 Mica blur with a 12% desktop bleed."
                : "Pure #000000 for QD-OLED contrast. No blur layer."
            }
          >
            <div className="flex shrink-0 overflow-hidden rounded-lg border border-white/[0.07] bg-black/25 p-0.5">
              {(["mica", "solid"] as const).map((backdrop) => (
                <button
                  key={backdrop}
                  type="button"
                  disabled={busy}
                  aria-pressed={settings.backdrop === backdrop}
                  onClick={() => onUpdate({ backdrop })}
                  className={`px-2 py-1 text-[10.5px] transition-colors duration-150 ease-out disabled:opacity-40 ${
                    settings.backdrop === backdrop
                      ? "rounded-md bg-white/[0.09] text-white/90"
                      : "rounded-md text-white/58 hover:text-white/70"
                  }`}
                >
                  {backdrop === "mica" ? "Mica" : "OLED black"}
                </button>
              ))}
            </div>
          </Row>
        )}

        <div className="space-y-2">
          <Row
            icon={<Keyboard className="size-3.5" />}
            title="Summon shortcut"
            detail="Works system-wide while Compa runs in the tray."
          >
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setCandidate(null);
                  setRecording((value) => !value);
                }}
                className={`rounded-md border px-2 py-1 font-mono text-[10.5px] transition-colors duration-150 ease-out disabled:opacity-40 ${
                  recording
                    ? "border-violet-300/40 bg-violet-300/10 text-violet-100"
                    : "border-white/[0.08] bg-white/[0.03] text-white/60 hover:text-white/85"
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
                className="flex size-7 items-center justify-center rounded-md text-white/54 transition-colors duration-150 ease-out hover:bg-white/[0.06] hover:text-white/70 disabled:opacity-30"
              >
                <RotateCcw className="size-3.5" />
              </button>
            </div>
          </Row>
          {candidate && (
            <p className="pl-9 font-mono text-[10px] text-white/54">
              requested {prettyShortcut(candidate)}
            </p>
          )}
          {shortcutError ? (
            <p className="flex items-start gap-1.5 pl-9 text-[10px] leading-4 text-rose-200/80">
              <TriangleAlert className="mt-px size-3 shrink-0" />
              {shortcutError}
            </p>
          ) : (
            <p className="flex items-center gap-1.5 pl-9 text-[10px] text-emerald-200/50">
              <Check className="size-3" /> registered with Windows
            </p>
          )}
        </div>

        <Row
          icon={<FolderTree className="size-3.5" />}
          title="Explorer context menu"
          detail={
            contextMenuCommand
              ? contextMenuCommand
              : 'Adds "Analyze with Compa" to file and folder right-click menus.'
          }
        >
          <Toggle
            label="Explorer context menu"
            checked={settings.context_menu}
            disabled={busy}
            onChange={(next) => onUpdate({ context_menu: next })}
          />
        </Row>
        {state.context_menu_error && (
          <p className="pl-9 text-[10px] leading-4 text-rose-200/80">
            {state.context_menu_error}
          </p>
        )}

        <div className="space-y-2">
          <Row
            icon={
              settings.notifications ? (
                <Bell className="size-3.5" />
              ) : (
                <BellOff className="size-3.5" />
              )
            }
            title="Smart notifications"
            detail="Reads new Windows toasts so Compa can react to them. Off unregisters the hook thread completely."
          >
            <Toggle
              label="Smart notifications"
              checked={settings.notifications}
              disabled={busy}
              onChange={(next) => onUpdate({ notifications: next })}
            />
          </Row>
          <div className="flex items-center gap-1.5 pl-9">
            <span
              className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-px text-[10px] ${chip.tone}`}
            >
              {state.notifications_status === "starting" && (
                <Loader2 className="size-2.5 animate-spin" />
              )}
              {chip.label}
            </span>
            {!settings.notifications && (
              <span className="text-[10px] text-white/48">0 threads · 0 polls</span>
            )}
          </div>
        </div>
      </div>

      {error && (
        <p className="mt-3 flex items-start gap-1.5 rounded-lg border border-rose-300/20 bg-rose-400/[0.06] px-2 py-1.5 text-[10.5px] leading-4 text-rose-100/80">
          <TriangleAlert className="mt-px size-3 shrink-0" />
          {error}
        </p>
      )}
      {state.backdrop_error && settings.mode === "modern" && (
        <p className="mt-2 pl-9 text-[10px] leading-4 text-amber-100/60">
          {state.backdrop_error}
        </p>
      )}
    </section>
  );
}
