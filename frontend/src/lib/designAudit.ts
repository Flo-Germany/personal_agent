import { invoke, isTauri } from "@tauri-apps/api/core";

/**
 * Phase 24 instrumentation: forward webview console errors/warnings and a
 * layout/contrast audit to the Tauri shell, where the visual QA script reads
 * them from `desktop-state.json`.
 *
 * The capture is diagnostic only; it never changes rendering and is a no-op in
 * a plain browser preview.
 */

export type ConsoleEvent = {
  level: "error" | "warning";
  message: string;
};

export type ContrastFinding = {
  text: string;
  selector: string;
  color: string;
  background: string;
  ratio: number;
  required: number;
  fontSize: number;
};

export type LayoutAudit = {
  kind: string;
  at: number;
  viewport: { width: number; height: number };
  overflow: { x: number; y: number };
  contrast: ContrastFinding[];
  smallTargets: { label: string; width: number; height: number }[];
  unlabeledButtons: string[];
  truncated: { label: string; text: string }[];
};

const pending: ConsoleEvent[] = [];
let installed = false;
let flushTimer: number | null = null;

function describe(value: unknown): string {
  if (typeof value === "string") return value;
  if (value instanceof Error) return `${value.name}: ${value.message}`;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function scheduleFlush() {
  if (flushTimer !== null) return;
  flushTimer = window.setTimeout(() => {
    flushTimer = null;
    const batch = pending.splice(0, pending.length);
    if (!batch.length || !isTauri()) return;
    void invoke("record_console_events", { events: batch }).catch(() => {});
  }, 700);
}

function push(level: ConsoleEvent["level"], values: unknown[]) {
  const message = values.map(describe).join(" ").replace(/\s+/g, " ").trim();
  if (!message) return;
  pending.push({ level, message: message.slice(0, 600) });
  scheduleFlush();
}

/** Patch once; React dev warnings (missing keys, invalid props) land here. */
export function installConsoleCapture(): void {
  if (installed) return;
  installed = true;

  const originalError = console.error.bind(console);
  console.error = (...args: unknown[]) => {
    push("error", args);
    originalError(...args);
  };
  const originalWarn = console.warn.bind(console);
  console.warn = (...args: unknown[]) => {
    push("warning", args);
    originalWarn(...args);
  };
  window.addEventListener("error", (event) => {
    push("error", [event.message || "window error"]);
  });
  window.addEventListener("unhandledrejection", (event) => {
    push("error", [`unhandled rejection: ${describe(event.reason)}`]);
  });
}

const canvas = document.createElement("canvas");
canvas.width = 1;
canvas.height = 1;
const ctx = canvas.getContext("2d", { willReadFrequently: true });

type Rgba = [number, number, number, number];

function resolveColor(value: string): Rgba {
  if (!ctx) return [255, 255, 255, 1];
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillStyle = "#000000";
  ctx.fillStyle = value;
  ctx.fillRect(0, 0, 1, 1);
  const data = ctx.getImageData(0, 0, 1, 1).data;
  return [data[0], data[1], data[2], data[3] / 255];
}

function composite(top: Rgba, bottom: Rgba): Rgba {
  const alpha = top[3];
  return [
    top[0] * alpha + bottom[0] * (1 - alpha),
    top[1] * alpha + bottom[1] * (1 - alpha),
    top[2] * alpha + bottom[2] * (1 - alpha),
    1,
  ];
}

function luminance([r, g, b]: Rgba): number {
  const channel = (value: number) => {
    const scaled = value / 255;
    return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrastRatio(foreground: Rgba, background: Rgba): number {
  const a = luminance(foreground);
  const b = luminance(background);
  const light = Math.max(a, b);
  const dark = Math.min(a, b);
  return (light + 0.05) / (dark + 0.05);
}

function selectorFor(element: Element): string {
  const tag = element.tagName.toLowerCase();
  const classes = Array.from(element.classList)
    .filter((name) => !name.startsWith("_") && name.length < 40)
    .slice(0, 3)
    .join(".");
  return classes ? `${tag}.${classes}` : tag;
}

function isVisible(element: Element): boolean {
  const style = getComputedStyle(element);
  if (style.display === "none" || style.visibility === "hidden") return false;
  if (Number.parseFloat(style.opacity) < 0.05) return false;
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function textOf(element: Element): string {
  let value = "";
  for (const node of Array.from(element.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE && node.textContent) {
      value += node.textContent;
    }
  }
  return value.replace(/\s+/g, " ").trim();
}

function backgroundOf(element: Element, base: Rgba): Rgba {
  const layers: Rgba[] = [];
  let current: Element | null = element;
  while (current) {
    const color = resolveColor(getComputedStyle(current).backgroundColor);
    if (color[3] > 0.01) layers.push(color);
    current = current.parentElement;
  }
  return layers.reduceRight((backdrop, layer) => composite(layer, backdrop), base);
}

/**
 * Measures what the user actually sees: text contrast, horizontal overflow,
 * unlabeled buttons, sub-24px targets and silent truncation.
 */
export function runDesignAudit(kind: string, baseHex: string): LayoutAudit {
  const base = resolveColor(baseHex);
  const contrast: ContrastFinding[] = [];
  const smallTargets: { label: string; width: number; height: number }[] = [];
  const unlabeledButtons: string[] = [];
  const truncated: { label: string; text: string }[] = [];
  const root = document.getElementById("root") ?? document.body;

  const candidates: Element[] = Array.from(root.querySelectorAll("*"));
  for (const element of candidates) {
    if (!isVisible(element)) continue;
    const style = getComputedStyle(element);
    const fontSize = Number.parseFloat(style.fontSize) || 12;
    const weight = Number.parseInt(style.fontWeight, 10) || 400;

    const label = textOf(element);
    if (label && element.childElementCount === 0) {
      const alpha = Number.parseFloat(style.opacity);
      if (!Number.isFinite(alpha) || alpha > 0.9) {
        const color = resolveColor(style.color);
        const background = backgroundOf(element, base);
        const foreground = composite(color, background);
        const ratio = contrastRatio(foreground, background);
        const required = fontSize >= 18.66 || (fontSize >= 14 && weight >= 700) ? 3 : 4.5;
        if (ratio + 0.05 < required) {
          contrast.push({
            text: label.slice(0, 48),
            selector: selectorFor(element),
            color: style.color,
            background: `rgb(${Math.round(background[0])} ${Math.round(background[1])} ${Math.round(background[2])})`,
            ratio: Math.round(ratio * 100) / 100,
            required,
            fontSize,
          });
        }
      }
    }

    const placeholder = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement
      ? getComputedStyle(element, "::placeholder").color
      : "";
    if (placeholder) {
      const color = resolveColor(placeholder);
      const background = backgroundOf(element, base);
      const ratio = contrastRatio(composite(color, background), background);
      if (ratio < 4.5) {
        contrast.push({
          text: element.getAttribute("placeholder")?.slice(0, 48) ?? "placeholder",
          selector: `${selectorFor(element)}::placeholder`,
          color: placeholder,
          background: `rgb(${Math.round(background[0])} ${Math.round(background[1])} ${Math.round(background[2])})`,
          ratio: Math.round(ratio * 100) / 100,
          required: 4.5,
          fontSize: Number.parseFloat(style.fontSize) || 12,
        });
      }
    }

    const interactive = element.matches("button, a, [role='switch'], [role='button']");
    if (interactive) {
      const rect = element.getBoundingClientRect();
      if (rect.height < 24 || rect.width < 24) {
        smallTargets.push({
          label: (label || element.getAttribute("aria-label") || element.tagName).slice(0, 40),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        });
      }
      const accessible =
        (element.textContent ?? "").trim() ||
        element.getAttribute("aria-label") ||
        element.getAttribute("title") ||
        (element as HTMLButtonElement).title;
      if (!accessible) {
        unlabeledButtons.push(selectorFor(element));
      }
    }

    if (
      (style.overflow === "hidden" || style.overflowX === "hidden") &&
      element.scrollWidth > element.clientWidth + 2 &&
      label
    ) {
      truncated.push({ label: selectorFor(element), text: label.slice(0, 60) });
    }
  }

  return {
    kind,
    at: Date.now(),
    viewport: { width: window.innerWidth, height: window.innerHeight },
    overflow: {
      x: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
      y: Math.max(0, document.documentElement.scrollHeight - window.innerHeight),
    },
    contrast: contrast.sort((a, b) => a.ratio - b.ratio).slice(0, 16),
    smallTargets: smallTargets.slice(0, 10),
    unlabeledButtons,
    truncated: truncated.slice(0, 8),
  };
}

export async function publishLayoutAudit(kind: string, baseHex: string): Promise<void> {
  if (!isTauri()) return;
  try {
    const audit = runDesignAudit(kind, baseHex);
    await invoke("record_layout_audit", { audit });
  } catch {
    // Diagnostics only.
  }
}

export async function clearQaLogs(): Promise<void> {
  if (!isTauri()) return;
  try {
    await invoke("clear_qa_logs");
  } catch {
    // Diagnostics only.
  }
}
