import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Boxes,
  Cpu,
  Fingerprint,
  Lock,
  Palette,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";

import { EASE_IN, EASE_OUT } from "../lib/motion";
import { useEditableSettings } from "../hooks/useEditableSettings";
import type { Identity } from "../types";
import {
  AppearanceSettings,
  EngineSettings,
  GeneralSettings,
  HardwareSettings,
  SystemSettings,
} from "./SettingsPanes";
import type {
  DesktopController,
  EditableController,
} from "./SettingsPanes";
import { MemorySettings } from "./MemorySettings";
import { Badge } from "./SettingsUi";

export type SettingsCategory =
  | "general"
  | "memory"
  | "appearance"
  | "system"
  | "engine"
  | "hardware";

type Category = {
  id: SettingsCategory;
  label: string;
  short: string;
  hint: string;
  icon: ReactNode;
  tint: string;
};

const CATEGORIES: Category[] = [
  {
    id: "general",
    label: "General",
    short: "General",
    hint: "Name, language and behaviour",
    icon: <SlidersHorizontal className="size-4" />,
    tint: "bg-[#0a84ff]",
  },
  {
    id: "memory",
    label: "Identity & Memory",
    short: "Memory",
    hint: "Who Compa is and what it remembers",
    icon: <Fingerprint className="size-4" />,
    tint: "bg-[#af52de]",
  },
  {
    id: "appearance",
    label: "Appearance",
    short: "Look",
    hint: "Theme, backdrop and motion",
    icon: <Palette className="size-4" />,
    tint: "bg-[#ff9f0a]",
  },
  {
    id: "system",
    label: "System & Permissions",
    short: "System",
    hint: "Shortcut, notifications and autonomy",
    icon: <ShieldCheck className="size-4" />,
    tint: "bg-[#30d158]",
  },
  {
    id: "engine",
    label: "Engine",
    short: "Engine",
    hint: "Models, context and standby",
    icon: <Boxes className="size-4" />,
    tint: "bg-[#5e5ce6]",
  },
  {
    id: "hardware",
    label: "Hardware",
    short: "Hardware",
    hint: "GPU, CPU and vision limits",
    icon: <Cpu className="size-4" />,
    tint: "bg-[#ff375f]",
  },
];

type SettingsViewProps = {
  identity: Identity | null;
  onClose: () => void;
  desktop: DesktopController;
  engine: ReactNode;
  onIdentityChanged: () => void;
  initialCategory?: SettingsCategory;
};

export function SettingsView({
  identity,
  onClose,
  desktop,
  engine,
  onIdentityChanged,
  initialCategory = "general",
}: SettingsViewProps) {
  const [active, setActive] = useState<SettingsCategory>(initialCategory);
  useEffect(() => {
    setActive(initialCategory);
  }, [initialCategory]);
  const [showSaved, setShowSaved] = useState(false);
  const editable = useEditableSettings(onIdentityChanged);
  const current = useMemo(
    () => CATEGORIES.find((item) => item.id === active) ?? CATEGORIES[0],
    [active],
  );

  useEffect(() => {
    if (!editable.savedAt) {
      return;
    }
    setShowSaved(true);
    const timer = window.setTimeout(() => setShowSaved(false), 1800);
    return () => window.clearTimeout(timer);
  }, [editable.savedAt]);

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      <aside className="flex w-[58px] shrink-0 flex-col border-r border-fg/[0.07] bg-fg/[0.018] p-2 min-[560px]:w-[186px]">
        <div className="hidden px-2 pt-1 pb-2.5 min-[560px]:block">
          <p className="text-[10px] font-semibold tracking-[0.14em] text-fg/68 uppercase">
            Settings
          </p>
          <p className="mt-0.5 truncate text-[10px] text-fg/68">
            {identity?.name ?? "Compa"}
          </p>
        </div>

        <nav className="flex flex-col gap-1" aria-label="Settings categories">
          {CATEGORIES.map((category) => {
            const selected = category.id === active;
            return (
              <button
                key={category.id}
                type="button"
                onClick={() => setActive(category.id)}
                aria-current={selected ? "page" : undefined}
                title={category.hint}
                className={`group flex items-center gap-2.5 rounded-xl px-1.5 py-1.5 text-left transition-colors duration-150 ease-out min-[560px]:px-2 ${
                  selected
                    ? "bg-fg/[0.07] text-fg/92"
                    : "text-fg/68 hover:bg-fg/[0.04] hover:text-fg/82"
                }`}
              >
                <span
                  className={`flex size-7 shrink-0 items-center justify-center rounded-[9px] text-white shadow-[0_6px_14px_-8px_var(--compa-shadow-strong)] ${category.tint}`}
                >
                  {category.icon}
                </span>
                <span className="hidden min-w-0 flex-1 truncate text-[10.5px] leading-4 min-[560px]:block">
                  {category.label}
                </span>
              </button>
            );
          })}
        </nav>

        <div className="mt-auto hidden px-2 pb-1 min-[560px]:block">
          <p className="flex items-center gap-1 text-[9.5px] leading-4 text-fg/68">
            <Lock className="size-3" />
            Local only
          </p>
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex shrink-0 items-center gap-2 border-b border-fg/[0.06] px-4 py-2.5">
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-1.5">
              <span className="truncate text-[13px] font-semibold text-fg/90">
                {current.label}
              </span>
              {showSaved && <Badge tone="positive">Saved</Badge>}
            </span>
            <span className="block truncate text-[10px] text-fg/68">
              {current.hint}
            </span>
          </span>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close settings"
            className="flex size-7 items-center justify-center rounded-lg text-fg/68 transition-colors hover:bg-fg/[0.06] hover:text-fg/80"
          >
            <X className="size-4" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3.5">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={active}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{
                opacity: 0,
                y: -5,
                transition: { duration: 0.12, ease: EASE_IN },
              }}
              transition={{ duration: 0.2, ease: EASE_OUT }}
              className="space-y-3.5"
            >
              <Pane
                category={active}
                identity={identity}
                desktop={desktop}
                editable={editable}
                engine={engine}
              />
            </motion.div>
          </AnimatePresence>
        </div>
      </section>
    </div>
  );
}

function Pane({
  category,
  identity,
  desktop,
  editable,
  engine,
}: {
  category: SettingsCategory;
  identity: Identity | null;
  desktop: DesktopController;
  editable: EditableController;
  engine: ReactNode;
}) {
  switch (category) {
    case "general":
      return <GeneralSettings editable={editable} desktop={desktop} />;
    case "memory":
      return <MemorySettings identity={identity} />;
    case "appearance":
      return <AppearanceSettings desktop={desktop} />;
    case "system":
      return <SystemSettings editable={editable} desktop={desktop} />;
    case "engine":
      return <EngineSettings editable={editable}>{engine}</EngineSettings>;
    case "hardware":
      return <HardwareSettings editable={editable} />;
    default:
      return null;
  }
}
