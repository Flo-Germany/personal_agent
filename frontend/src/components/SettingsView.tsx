import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Accessibility,
  Boxes,
  Cpu,
  ShieldCheck,
  Sparkles,
  UserRound,
  X,
} from "lucide-react";

import type { Identity } from "../types";

export type SettingsCategory =
  | "general"
  | "permissions"
  | "engine"
  | "hardware";

type Category = {
  id: SettingsCategory;
  label: string;
  hint: string;
  icon: ReactNode;
  tint: string;
  activeTint: string;
};

const CATEGORIES: Category[] = [
  {
    id: "general",
    label: "General",
    hint: "Identity, memory and appearance",
    icon: <UserRound className="size-4" />,
    tint: "border-sky-300/20 bg-sky-400/12 text-sky-200",
    activeTint: "bg-sky-400/15 text-sky-100",
  },
  {
    id: "permissions",
    label: "Permissions",
    hint: "Shortcut, Explorer, notifications",
    icon: <ShieldCheck className="size-4" />,
    tint: "border-violet-300/20 bg-violet-400/12 text-violet-200",
    activeTint: "bg-violet-400/15 text-violet-100",
  },
  {
    id: "engine",
    label: "Engine",
    hint: "Models, runtime and inference",
    icon: <Boxes className="size-4" />,
    tint: "border-emerald-300/20 bg-emerald-400/12 text-emerald-200",
    activeTint: "bg-emerald-400/15 text-emerald-100",
  },
  {
    id: "hardware",
    label: "Hardware",
    hint: "GPU, memory and performance",
    icon: <Cpu className="size-4" />,
    tint: "border-amber-300/20 bg-amber-400/12 text-amber-200",
    activeTint: "bg-amber-400/15 text-amber-100",
  },
];

function identityCard(identity: Identity | null): ReactNode {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 rounded-2xl border border-white/[0.06] bg-white/[0.025] p-4 shadow-[0_18px_50px_-32px_rgba(0,0,0,0.95)]">
        <span className="flex size-10 items-center justify-center rounded-xl border border-sky-300/20 bg-sky-400/12 text-sky-200">
          <Sparkles className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[12.5px] font-medium text-white/90">
            {identity?.name ?? "Compa"}
          </p>
          <p className="truncate text-[10.5px] text-white/54">
            {identity?.model ?? "Local model"}
          </p>
        </div>
        <span className="rounded-full border border-emerald-300/20 bg-emerald-300/[0.07] px-2 py-0.5 text-[10px] text-emerald-200/80">
          Local
        </span>
      </div>

      <div className="rounded-2xl border border-white/[0.06] bg-white/[0.018] p-4">
        <p className="text-[10px] font-semibold tracking-[0.12em] text-white/40 uppercase">
          Memory
        </p>
        <p className="mt-1.5 text-[11px] leading-5 text-white/54">
          Facts are learned from chats and stored locally. The knowledge base
          editor is not wired into this pane yet; it remains available through
          the existing backend endpoints.
        </p>
      </div>
    </div>
  );
}

function hardwareNote(): ReactNode {
  return (
    <div className="rounded-2xl border border-white/[0.06] bg-white/[0.018] p-4 text-[11px] leading-5 text-white/54">
      Hardware profiling, VRAM planning and the layer report are shown above
      under the engine section, next to the model catalog.
    </div>
  );
}

type SettingsViewProps = {
  identity: Identity | null;
  onClose: () => void;
  permissions: ReactNode;
  engine: ReactNode;
};

export function SettingsView({
  identity,
  onClose,
  permissions,
  engine,
}: SettingsViewProps) {
  const [active, setActive] = useState<SettingsCategory>("general");
  const current = useMemo(
    () => CATEGORIES.find((item) => item.id === active) ?? CATEGORIES[0],
    [active],
  );

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      <aside className="flex w-14 shrink-0 flex-col border-r border-white/[0.06] bg-black/25 p-2 min-[560px]:w-[132px]">
        <div className="hidden px-2 pt-1 pb-2 min-[560px]:block">
          <p className="text-[10px] font-semibold tracking-[0.14em] text-white/32 uppercase">
            Settings
          </p>
          <p className="mt-0.5 truncate text-[10px] text-white/22">
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
                className={`group flex items-center gap-2 rounded-xl px-2 py-1.5 text-left transition-colors duration-150 ease-out ${
                  selected
                    ? category.activeTint
                    : "text-white/54 hover:bg-white/[0.04] hover:text-white/80"
                }`}
              >
                <span
                  className={`flex size-6 shrink-0 items-center justify-center rounded-lg border ${category.tint}`}
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
          <p className="flex items-center gap-1 text-[9.5px] leading-4 text-white/22">
            <Accessibility className="size-3" />
            Local only
          </p>
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex shrink-0 items-center gap-2 border-b border-white/[0.06] px-4 py-2.5">
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[12.5px] font-medium text-white/90">
              {current.label}
            </span>
            <span className="block truncate text-[10px] text-white/40">
              {current.hint}
            </span>
          </span>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close settings"
            className="flex size-7 items-center justify-center rounded-lg text-white/40 transition-colors hover:bg-white/[0.06] hover:text-white/80"
          >
            <X className="size-4" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
          {active === "general" && identityCard(identity)}
          {active === "permissions" && permissions}
          {active === "engine" && engine}
          {active === "hardware" && hardwareNote()}
        </div>
      </section>
    </div>
  );
}
