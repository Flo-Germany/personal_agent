import { motion } from "framer-motion";
import { useId, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { SPRING } from "../lib/motion";

/* ------------------------------------------------------------------ layout */

export function SettingsCard({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[18px] border border-fg/[0.08] bg-fg/[0.028] p-4 shadow-[var(--compa-shadow)]">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-[12.5px] leading-5 font-semibold text-fg/90">
            {title}
          </h3>
          {description && (
            <p className="mt-0.5 text-[10.5px] leading-4 text-fg/68">
              {description}
            </p>
          )}
        </div>
        {action}
      </div>
      <div className="mt-3.5 space-y-3.5 border-t border-fg/[0.06] pt-3.5">
        {children}
      </div>
    </section>
  );
}

export function SettingsRow({
  label,
  detail,
  children,
  stacked,
}: {
  label: string;
  detail?: ReactNode;
  children: ReactNode;
  /** Put the control on its own line (used by text and number inputs). */
  stacked?: boolean;
}) {
  return (
    <div
      className={
        stacked
          ? "space-y-1.5"
          : "flex flex-wrap items-center gap-x-3 gap-y-2"
      }
    >
      <div className={stacked ? "min-w-0" : "min-w-0 flex-1 basis-[min(100%,190px)]"}>
        <p className="break-words text-[12px] leading-4 text-fg/80">{label}</p>
        {detail && (
          <p className="mt-0.5 break-words text-[10.5px] leading-4 text-fg/68">
            {detail}
          </p>
        )}
      </div>
      <div
        className={
          stacked
            ? "flex w-full items-center gap-2"
            : "flex shrink-0 items-center gap-2"
        }
      >
        {children}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ toggles */

export function Switch({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-[26px] w-[44px] shrink-0 rounded-full transition-colors duration-200 ease-out disabled:opacity-40 ${
        checked ? "bg-positive" : "bg-fg/[0.16]"
      }`}
    >
      <motion.span
        layout
        transition={SPRING}
        className={`absolute top-[2px] size-[22px] rounded-full bg-white shadow-[0_2px_5px_var(--compa-shadow-tight)] ${
          checked ? "left-[20px]" : "left-[2px]"
        }`}
      />
    </button>
  );
}

/* --------------------------------------------------------------- segmented */

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  disabled,
}: {
  value: T;
  options: { value: T; label: string; title?: string }[];
  onChange: (next: T) => void;
  label: string;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="relative flex shrink-0 rounded-xl bg-sunken p-[3px]"
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            title={option.title ?? option.label}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className={`relative rounded-[9px] px-2.5 py-1 text-[11px] transition-colors duration-150 ease-out disabled:cursor-not-allowed ${
              selected
                ? disabled
                  ? "font-medium text-fg/85"
                  : "font-medium text-white"
                : disabled
                  ? "text-fg/45"
                  : "text-fg/68 hover:text-fg/85"
            }`}
          >
            {selected && (
              <motion.span
                layoutId={`segment-${id}`}
                transition={SPRING}
                className={`absolute inset-0 rounded-[9px] shadow-[0_6px_16px_-7px_rgba(10,132,255,0.9)] ${
                  disabled ? "bg-fg/[0.16]" : "bg-accent"
                }`}
              />
            )}
            <span className="relative">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ inputs */

const inputClass =
  "field-focus w-full rounded-xl border border-fg/[0.1] bg-fg/[0.04] px-2.5 py-1.5 text-[12px] text-fg/90 transition-colors duration-150 ease-out placeholder:text-fg/75 focus:bg-fg/[0.06] focus:outline-none disabled:opacity-40";

export function TextField({
  value,
  onChange,
  onCommit,
  placeholder,
  disabled,
  label,
  maxLength,
}: {
  value: string;
  onChange: (next: string) => void;
  onCommit?: () => void;
  placeholder?: string;
  disabled?: boolean;
  label: string;
  maxLength?: number;
}) {
  return (
    <input
      type="text"
      aria-label={label}
      value={value}
      maxLength={maxLength}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      onBlur={onCommit}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.currentTarget.blur();
        }
      }}
      className={inputClass}
    />
  );
}

export function NumberField({
  value,
  onCommit,
  min,
  max,
  step,
  unit,
  placeholder,
  disabled,
  label,
  width = "w-[84px]",
}: {
  value: number | null;
  onCommit: (next: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  placeholder?: string;
  disabled?: boolean;
  label: string;
  width?: string;
}) {
  const [draft, setDraft] = useState(value === null ? "" : String(value));

  useEffect(() => {
    setDraft(value === null ? "" : String(value));
  }, [value]);

  const commit = () => {
    const raw = draft.trim();
    if (raw === "") {
      onCommit(null);
      return;
    }
    let parsed = Number(raw);
    if (!Number.isFinite(parsed)) {
      setDraft(value === null ? "" : String(value));
      return;
    }
    if (min !== undefined) parsed = Math.max(min, parsed);
    if (max !== undefined) parsed = Math.min(max, parsed);
    setDraft(String(parsed));
    onCommit(parsed);
  };

  return (
    <span className="flex items-center gap-1.5">
      <input
        type="number"
        inputMode="numeric"
        aria-label={label}
        value={draft}
        min={min}
        max={max}
        step={step}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.currentTarget.blur();
          }
        }}
        className={`${inputClass} ${width} tabular-nums`}
      />
      {unit && <span className="text-[10.5px] text-fg/68">{unit}</span>}
    </span>
  );
}

/* ----------------------------------------------------------------- buttons */

export function PrimaryButton({
  children,
  onClick,
  disabled,
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className="rounded-xl bg-accent px-3 py-1.5 text-[11.5px] font-medium text-white shadow-[0_10px_24px_-12px_rgba(10,132,255,0.9)] transition-all duration-200 ease-out hover:bg-accent-strong active:scale-[0.985] disabled:cursor-not-allowed disabled:bg-fg/10 disabled:text-fg/68 disabled:shadow-none"
    >
      {children}
    </button>
  );
}

export function QuietButton({
  children,
  onClick,
  disabled,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-xl border border-fg/[0.1] bg-fg/[0.035] px-3 py-1.5 text-[11.5px] text-fg/74 transition-colors duration-150 ease-out hover:bg-fg/[0.07] hover:text-fg/90 disabled:opacity-40"
    >
      {children}
    </button>
  );
}

export function DangerButton({
  children,
  onClick,
  disabled,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-xl border border-danger/30 bg-danger/[0.08] px-3 py-1.5 text-[11.5px] text-danger-ink transition-colors duration-150 ease-out hover:bg-danger/[0.14] disabled:opacity-40"
    >
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------- notes */

export function SettingsNote({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "warning" | "positive" | "danger";
  children: ReactNode;
}) {
  const tones = {
    neutral: "border-fg/[0.08] bg-fg/[0.03] text-fg/68",
    warning: "border-warning/25 bg-warning/[0.07] text-warning-ink",
    positive: "border-positive/25 bg-positive/[0.07] text-positive-ink",
    danger: "border-danger/25 bg-danger/[0.07] text-danger-ink",
  } as const;
  return (
    <p
      className={`rounded-xl border px-2.5 py-1.5 text-[10.5px] leading-4 ${tones[tone]}`}
    >
      {children}
    </p>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "positive" | "warning";
}) {
  const tones = {
    neutral: "border-fg/[0.09] bg-fg/[0.04] text-fg/74",
    accent: "border-accent/30 bg-accent/10 text-accent-ink",
    positive: "border-positive/30 bg-positive/10 text-positive-ink",
    warning: "border-warning/30 bg-warning/10 text-warning-ink",
  } as const;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9.5px] leading-4 ${tones[tone]}`}
    >
      {children}
    </span>
  );
}
