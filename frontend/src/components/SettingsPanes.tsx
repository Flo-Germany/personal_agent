import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  Gauge,
  Info,
  Layers,
  RotateCw,
  Sparkles,
} from "lucide-react";

import type { DesktopSettings, DesktopState } from "../lib/desktop";
import { restartEngine } from "../lib/settingsApi";
import type { EditablePatch, EditableSettings } from "../lib/settingsApi";
import { DesktopSettingsSection } from "./DesktopSettings";
import {
  Badge,
  NumberField,
  QuietButton,
  Segmented,
  SettingsCard,
  SettingsNote,
  SettingsRow,
  Switch,
  TextField,
} from "./SettingsUi";

/* ------------------------------------------------------------------- types */

export type EditableController = {
  settings: EditableSettings | null;
  loading: boolean;
  saving: boolean;
  error: string;
  restartRequired: boolean;
  savedAt: number;
  patch: (changes: EditablePatch) => Promise<boolean>;
  reload: () => Promise<void>;
  clearRestart: () => void;
};

export type DesktopController = {
  settings: DesktopSettings;
  state: DesktopState;
  contextMenuCommand: string | null;
  busy: boolean;
  error: string;
  update: (patch: Partial<DesktopSettings>) => Promise<boolean>;
  refreshState: () => Promise<void>;
};

const LANGUAGE_OPTIONS: {
  value: EditableSettings["language"];
  label: string;
}[] = [
  { value: "auto", label: "Auto" },
  { value: "de", label: "German" },
  { value: "en", label: "English" },
  { value: "zh", label: "Chinese" },
];

const STYLE_OPTIONS: {
  value: EditableSettings["communication_style"];
  label: string;
}[] = [
  { value: "concise", label: "Concise" },
  { value: "conversational", label: "Warm" },
  { value: "technical", label: "Technical" },
];

const AUTONOMY_OPTIONS: {
  value: EditableSettings["autonomy"];
  label: string;
}[] = [
  { value: "cautious", label: "Cautious" },
  { value: "balanced", label: "Balanced" },
  { value: "autonomous", label: "Autonomous" },
];

const PRIORITY_OPTIONS: {
  value: EditableSettings["process_priority"];
  label: string;
}[] = [
  { value: "eco", label: "Eco" },
  { value: "normal", label: "Normal" },
  { value: "high", label: "High" },
];

const STANDBY_OPTIONS = [
  { value: "-1", label: "Always" },
  { value: "1800", label: "30 min" },
  { value: "300", label: "5 min" },
  { value: "0", label: "Unload" },
] as const;

// KV-cache precision. Long-context decode is bandwidth-bound, so this is the
// single biggest speed knob: measured at ~11k prompt tokens, f16 14 tok/s,
// q8_0 21 tok/s, q4_0 30 tok/s on the reference machine.
const KV_OPTIONS: {
  value: EditableSettings["kv_cache"];
  label: string;
  title: string;
}[] = [
  { value: "q8_0", label: "Balanced", title: "q8_0 — near-lossless, ~50% faster" },
  { value: "q4_0", label: "Fast", title: "q4_0 — maximum speed, slightly less precise" },
  { value: "f16", label: "Exact", title: "f16 — default precision, slowest" },
];

function paneGate(
  editable: EditableController,
  render: (settings: EditableSettings) => ReactNode,
): ReactNode {
  if (!editable.settings) {
    return (
      <SettingsCard
        title={editable.loading ? "Loading settings…" : "Settings unavailable"}
        description={
          editable.loading
            ? "Reading the local configuration."
            : "The backend did not return the editable settings."
        }
      >
        {editable.error && <SettingsNote tone="danger">{editable.error}</SettingsNote>}
        {!editable.loading && (
          <QuietButton onClick={() => void editable.reload()}>Retry</QuietButton>
        )}
      </SettingsCard>
    );
  }
  return render(editable.settings);
}

/* ----------------------------------------------------------------- general */

export function GeneralSettings({
  editable,
  desktop,
}: {
  editable: EditableController;
  desktop: DesktopController;
}) {
  return paneGate(editable, (settings) => (
    <GeneralForm
      settings={settings}
      editable={editable}
      desktop={desktop}
    />
  ));
}

function GeneralForm({
  settings,
  editable,
  desktop,
}: {
  settings: EditableSettings;
  editable: EditableController;
  desktop: DesktopController;
}) {
  const [name, setName] = useState(settings.agent_name);

  useEffect(() => {
    setName(settings.agent_name);
  }, [settings.agent_name]);

  const commitName = () => {
    const clean = name.replace(/\s+/g, " ").trim().slice(0, 60);
    if (!clean) {
      setName(settings.agent_name);
      return;
    }
    setName(clean);
    if (clean !== settings.agent_name) {
      void editable.patch({ agent_name: clean });
    }
  };

  return (
    <>
      <SettingsCard
        title="Assistant"
        description="The name and voice Compa uses in answers and in the window."
        action={
          editable.saving ? (
            <Badge tone="accent">Saving…</Badge>
          ) : editable.savedAt ? (
            <Badge tone="positive">Saved</Badge>
          ) : undefined
        }
      >
        <SettingsRow
          label="Agent name"
          detail="Shown in the title, identity card and system prompt."
          stacked
        >
          <TextField
            label="Agent name"
            value={name}
            onChange={setName}
            onCommit={commitName}
            placeholder="Compa"
            maxLength={60}
          />
        </SettingsRow>

        <SettingsRow
          label="Default language"
          detail="Auto mirrors the language of your message."
        >
          <Segmented
            label="Default language"
            value={settings.language}
            options={LANGUAGE_OPTIONS}
            onChange={(value) => void editable.patch({ language: value })}
          />
        </SettingsRow>

        <SettingsRow
          label="Communication style"
          detail="Shapes tone and length, not the facts."
        >
          <Segmented
            label="Communication style"
            value={settings.communication_style}
            options={STYLE_OPTIONS}
            onChange={(value) =>
              void editable.patch({ communication_style: value })
            }
          />
        </SettingsRow>

        {editable.error && (
          <SettingsNote tone="danger">{editable.error}</SettingsNote>
        )}
      </SettingsCard>

      <SettingsCard
        title="Startup"
        description="Compa lives in the tray. Startup only decides whether it is already running."
      >
        <SettingsRow
          label="Launch at login"
          detail="Starts hidden in the tray when you sign in to Windows."
        >
          <Switch
            label="Launch at login"
            checked={desktop.settings.start_with_windows}
            disabled={desktop.busy}
            onChange={(next) =>
              void desktop.update({ start_with_windows: next })
            }
          />
        </SettingsRow>
        {desktop.state.startup_error ? (
          <SettingsNote tone="warning">{desktop.state.startup_error}</SettingsNote>
        ) : (
          <SettingsNote>
            The Run key always points at the current executable, so updates do
            not break the shortcut.
          </SettingsNote>
        )}
      </SettingsCard>
    </>
  );
}

/* -------------------------------------------------------------- appearance */

const THEME_OPTIONS: {
  value: DesktopSettings["theme"];
  label: string;
}[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

const MODE_OPTIONS: { value: DesktopSettings["mode"]; label: string }[] = [
  { value: "classic", label: "Window" },
  { value: "modern", label: "Palette" },
];

const BACKDROP_OPTIONS: { value: DesktopSettings["backdrop"]; label: string }[] =
  [
    { value: "mica", label: "Mica" },
    { value: "solid", label: "OLED black" },
  ];

export function AppearanceSettings({
  desktop,
}: {
  desktop: DesktopController;
}) {
  const classic = desktop.settings.mode === "classic";

  return (
    <>
      <SettingsCard
        title="Theme"
        description="System follows the Windows light/dark setting live."
      >
        <SettingsRow
          label="Color scheme"
          detail={
            desktop.settings.theme === "dark"
              ? "Pure black surfaces for OLED displays."
              : desktop.settings.theme === "light"
                ? "Soft white surfaces with a vivid blue accent."
                : "Matches Windows as it changes."
          }
        >
          <Segmented
            label="Color scheme"
            value={desktop.settings.theme}
            options={THEME_OPTIONS}
            disabled={desktop.busy}
            onChange={(value) => void desktop.update({ theme: value })}
          />
        </SettingsRow>

        <SettingsRow
          label="Animations"
          detail="Entrances, exits and the sliding segmented controls."
        >
          <Switch
            label="Animations"
            checked={desktop.settings.animations}
            disabled={desktop.busy}
            onChange={(next) => void desktop.update({ animations: next })}
          />
        </SettingsRow>
      </SettingsCard>

      <SettingsCard
        title="Window"
        description="The classic window keeps the full chat; the palette floats above your work."
      >
        <SettingsRow
          label="Interface"
          detail="Switching re-applies the native window mode immediately."
        >
          <Segmented
            label="Interface"
            value={desktop.settings.mode}
            options={MODE_OPTIONS}
            disabled={desktop.busy}
            onChange={(value) => void desktop.update({ mode: value })}
          />
        </SettingsRow>

        <SettingsRow
          label="Backdrop effect"
          detail={
            classic
              ? "Available in the floating palette."
              : desktop.settings.backdrop === "mica"
                ? "Windows 11 Mica blur with a slight desktop bleed."
                : "Pure #000000 for QD-OLED contrast. No blur layer."
          }
        >
          <Segmented
            label="Backdrop effect"
            value={desktop.settings.backdrop}
            options={BACKDROP_OPTIONS}
            disabled={desktop.busy || classic}
            onChange={(value) => void desktop.update({ backdrop: value })}
          />
        </SettingsRow>

        {desktop.state.backdrop_error && !classic && (
          <SettingsNote tone="warning">{desktop.state.backdrop_error}</SettingsNote>
        )}
        {desktop.error && <SettingsNote tone="danger">{desktop.error}</SettingsNote>}
      </SettingsCard>
    </>
  );
}

/* ------------------------------------------------------------------ system */

export function SystemSettings({
  editable,
  desktop,
}: {
  editable: EditableController;
  desktop: DesktopController;
}) {
  return (
    <>
      <DesktopSettingsSection
        settings={desktop.settings}
        state={desktop.state}
        contextMenuCommand={desktop.contextMenuCommand}
        busy={desktop.busy}
        error={desktop.error}
        onUpdate={(patch) => void desktop.update(patch)}
        onRefreshState={() => void desktop.refreshState()}
      />
      {paneGate(editable, (settings) => (
        <>
          <SettingsCard
            title="Autonomy"
            description="How far Compa plans before pausing. Approvals stay on for every change."
            action={
              <Badge
                tone={
                  settings.autonomy === "cautious"
                    ? "warning"
                    : settings.autonomy === "autonomous"
                      ? "accent"
                      : "neutral"
                }
              >
                <Gauge className="size-2.5" />
                {settings.autonomy}
              </Badge>
            }
          >
            <SettingsRow
              label="Working style"
              detail="Cautious checks in before each step; autonomous carries routine plans through."
            >
              <Segmented
                label="Autonomy level"
                value={settings.autonomy}
                options={AUTONOMY_OPTIONS}
                onChange={(value) => void editable.patch({ autonomy: value })}
              />
            </SettingsRow>
            <SettingsNote>
              No autonomy level can skip the approval card for a tool that
              changes files, apps or settings.
            </SettingsNote>
          </SettingsCard>

          <SettingsCard
            title="Screen control"
            description="Background automation is always preferred over moving your cursor."
          >
            <SettingsRow
              label="Allow foreground fallback"
              detail="When no background route works, Compa may use the physical cursor and keyboard. Off forces UIA and the isolated workspace only."
            >
              <Switch
                label="Allow foreground fallback"
                checked={settings.allow_foreground_fallback}
                onChange={(next) =>
                  void editable.patch({ allow_foreground_fallback: next })
                }
              />
            </SettingsRow>
            {!settings.allow_foreground_fallback && (
              <SettingsNote tone="warning">
                Foreground actions are blocked. Compa will explain when a task
                cannot be completed without them.
              </SettingsNote>
            )}

            <SettingsRow
              label="UI tree depth"
              detail="How deep the accessibility tree is read for each window."
            >
              <NumberField
                label="UI tree depth"
                value={settings.uia_depth}
                min={2}
                max={60}
                onCommit={(value) =>
                  void editable.patch({ uia_depth: value ?? 20 })
                }
              />
            </SettingsRow>
          </SettingsCard>

          {editable.error && (
            <SettingsNote tone="danger">{editable.error}</SettingsNote>
          )}
        </>
      ))}
    </>
  );
}

/* ------------------------------------------------------------------ engine */

export function EngineSettings({
  editable,
  children,
}: {
  editable: EditableController;
  children: ReactNode;
}) {
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState("");

  const restart = async () => {
    setRestarting(true);
    setRestartError("");
    try {
      await restartEngine();
      editable.clearRestart();
    } catch (cause) {
      setRestartError(
        cause instanceof Error ? cause.message : "Could not restart the engine",
      );
    } finally {
      setRestarting(false);
    }
  };

  return (
    <>
      {paneGate(editable, (settings) => (
        <SettingsCard
          title="Inference"
          description="Context size and how long the model stays warm between turns."
          action={
            settings.engine_type === "llamacpp" ? (
              <Badge tone="accent">
                <Sparkles className="size-2.5" />
                llama.cpp
              </Badge>
            ) : (
              <Badge>Ollama</Badge>
            )
          }
        >
          <SettingsRow
            label="Context window"
            detail="Tokens available for the prompt, history and tools. Larger windows use more memory."
          >
            <NumberField
              label="Context window"
              value={settings.context_window}
              min={2048}
              max={131072}
              step={1024}
              unit="tokens"
              width="w-[96px]"
              onCommit={(value) =>
                void editable.patch({ context_window: value ?? 16384 })
              }
            />
          </SettingsRow>

          <SettingsRow
            label="KV cache precision"
            detail={
              settings.engine_type === "llamacpp"
                ? "Quantizing the cache is the biggest long-context speed gain. Takes effect after an engine restart."
                : "Only used by the llama.cpp engine."
            }
          >
            <Segmented
              label="KV cache precision"
              value={settings.kv_cache}
              options={KV_OPTIONS}
              disabled={settings.engine_type !== "llamacpp"}
              onChange={(value) => void editable.patch({ kv_cache: value })}
            />
          </SettingsRow>

          <SettingsRow
            label="Model standby"
            detail={
              settings.engine_type === "llamacpp"
                ? "llama.cpp keeps the model resident; this applies to the Ollama fallback."
                : "How long the model stays in memory after a reply."
            }
          >
            <Segmented
              label="Model standby"
              value={String(settings.keep_alive_seconds)}
              options={STANDBY_OPTIONS.map((option) => ({
                value: option.value,
                label: option.label,
              }))}
              onChange={(value) =>
                void editable.patch({ keep_alive_seconds: Number(value) })
              }
            />
          </SettingsRow>

          {editable.restartRequired && (
            <div className="space-y-2 rounded-xl border border-warning/25 bg-warning/[0.06] p-2.5">
              <p className="flex items-start gap-1.5 text-[10.5px] leading-4 text-warning-ink">
                <Info className="mt-px size-3 shrink-0" />
                Inference changes are saved. Restart the engine to load them.
              </p>
              <div className="flex flex-wrap gap-2">
                <QuietButton onClick={() => void restart()} disabled={restarting}>
                  <span className="flex items-center gap-1.5">
                    <RotateCw
                      className={`size-3 ${restarting ? "animate-spin" : ""}`}
                    />
                    {restarting ? "Restarting…" : "Restart engine"}
                  </span>
                </QuietButton>
                <QuietButton
                  onClick={editable.clearRestart}
                  disabled={restarting}
                >
                  Later
                </QuietButton>
              </div>
            </div>
          )}
          {restartError && <SettingsNote tone="danger">{restartError}</SettingsNote>}
          {editable.error && (
            <SettingsNote tone="danger">{editable.error}</SettingsNote>
          )}
        </SettingsCard>
      ))}

      <div className="space-y-3.5">{children}</div>
    </>
  );
}

/* ---------------------------------------------------------------- hardware */

export function HardwareSettings({
  editable,
}: {
  editable: EditableController;
}) {
  return paneGate(editable, (settings) => (
    <>
      <SettingsCard
        title="Graphics"
        description="Leave a field empty to let the planner decide from free VRAM."
        action={
          <Badge>
            <Layers className="size-2.5" />
            {settings.engine_type === "llamacpp" ? "Vulkan" : "Ollama"}
          </Badge>
        }
      >
        <SettingsRow
          label="GPU layers to offload"
          detail="Empty runs automatic layer planning. Pin a value only when you know the model fits."
        >
          <NumberField
            label="GPU layers"
            value={settings.gpu_layers}
            min={0}
            max={512}
            placeholder="Auto"
            onCommit={(value) => void editable.patch({ gpu_layers: value })}
          />
        </SettingsRow>

        <SettingsRow
          label="VRAM budget override"
          detail="Empty uses the detected card memory minus the safety reserve."
        >
          <NumberField
            label="VRAM budget"
            value={settings.vram_override_gb}
            min={1}
            max={512}
            step={0.5}
            unit="GB"
            placeholder="Auto"
            onCommit={(value) =>
              void editable.patch({ vram_override_gb: value })
            }
          />
        </SettingsRow>

        <SettingsRow
          label="Vision downscaling"
          detail="Screenshots are resized to this width before vision inference. Smaller is faster."
        >
          <NumberField
            label="Vision downscaling"
            value={settings.vision_max_width}
            min={640}
            max={3840}
            step={160}
            unit="px"
            width="w-[92px]"
            onCommit={(value) =>
              void editable.patch({ vision_max_width: value ?? 1600 })
            }
          />
        </SettingsRow>
      </SettingsCard>

      <SettingsCard
        title="Processor"
        description="Applies to the llama-server processes. Changes take effect on the next engine start."
      >
        <SettingsRow
          label="CPU thread limit"
          detail="Empty lets the runtime choose based on physical cores."
        >
          <NumberField
            label="CPU threads"
            value={settings.cpu_threads}
            min={1}
            max={256}
            placeholder="Auto"
            onCommit={(value) => void editable.patch({ cpu_threads: value })}
          />
        </SettingsRow>

        <SettingsRow
          label="Process priority"
          detail="Eco keeps the desktop responsive; high favours token throughput."
        >
          <Segmented
            label="Process priority"
            value={settings.process_priority}
            options={PRIORITY_OPTIONS}
            onChange={(value) =>
              void editable.patch({ process_priority: value })
            }
          />
        </SettingsRow>

        <div className="flex items-start gap-2 rounded-xl border border-fg/[0.07] bg-fg/[0.02] p-2.5 text-[10.5px] leading-4 text-fg/68">
          <Activity className="mt-px size-3 shrink-0 text-fg/68" />
          <span>
            The live hardware profile, VRAM plan and layer report are rendered
            by the engine section.
          </span>
        </div>
      </SettingsCard>

      {editable.error && <SettingsNote tone="danger">{editable.error}</SettingsNote>}
    </>
  ));
}
