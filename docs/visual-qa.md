# Phase 24: autonomous visual QA loop

The UI is no longer checked by eye alone. Every core state is captured from the
compiled Tauri window, measured in the live DOM, timed during its animations,
and judged by the local vision model. The loop repeats until all four gates are
clean.

```text
compile -> launch -> capture 12 states -> DOM audit + console + motion probe
        -> vision model verdict -> rewrite -> repeat
```

## Tooling

| File | Role |
| --- | --- |
| `scripts/visual_qa.ps1` | Drives the shell through every state, captures PNGs, reads the in-app audit, checks the console, times expand/collapse, runs the vision pass |
| `scripts/visual_qa.py` | Starts the active llama.cpp vision model, asks for a strict JSON design verdict per screenshot, writes `vision-report.{json,md}` |
| `scripts/screenshot_window.ps1` | Single-window capture including DWM/Mica compositing |
| `frontend/src/lib/designAudit.ts` | Renders-time audit: WCAG contrast, overflow, unlabeled controls, sub-24 px targets, truncation, console capture |
| `frontend/src/lib/qaFixtures.ts` | Deterministic states for idle, reply, search, tools, approval |
| `frontend/src-tauri/src/desktop.rs` | `--qa` surface, `console_events`, `layout_audit`, motion samples in `desktop-state.json` |

Run the whole matrix against the packaged app:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\visual_qa.ps1
```

Outputs:

```text
build/phase24/shots/*.png          12 high-DPI screenshots
build/phase24/qa-report.json       audits, console, animation, summary
build/phase24/vision-report.json   per-shot scores and issues
build/phase24/vision-report.md     readable review
```

Useful switches: `-SkipVision`, `-SkipAnimation`, `-Threshold 9`.

## The 12 captured states

| State | What it exercises |
| --- | --- |
| `classic-idle` | empty chat, header, quick prompts, composer |
| `classic-search` | search summary header, filter chips, file tree |
| `classic-tools` | live task card with progress, tool names, spinner |
| `classic-approval` | approval timeline, Now/Then, buttons |
| `classic-settings` | Windows integration, engine, model cards |
| `modern-idle` | collapsed palette, input, quick prompts, status strip |
| `modern-reply` | streaming markdown, caret, working row |
| `modern-search` | status verb and long answer |
| `modern-tools` | tool state without log spam |
| `modern-approval` | content-sized window, scrolled to the card |
| `modern-settings` | 748 px settings sheet inside the palette |
| `modern-notifications` | live WinRT listener (`Listening · N polls`) |

States are produced by `--qa demo=<kind>` fixtures, so no typing or real model
turn is needed. The fixtures never touch the conversation store.

## Gates

1. **Layout audit** (in the webview, read from `desktop-state.json`)
   - `scrollWidth/scrollHeight` may not exceed the viewport (zero overflow).
   - Text contrast is computed from the *rendered* colors: every visible text
     node is composited over its real background stack, then compared with
     WCAG AA (4.5:1, or 3:1 for large/bold text). The audit reports color,
     background, ratio and selector.
   - Gradient/image backgrounds are excluded from the automatic ratio (a known
     limit); those are covered by the vision review.
   - Buttons must have an accessible name; interactive targets must be at least
     24×24 CSS px.
2. **Console** – `console.error`/`console.warn` plus `window.onerror` and
   unhandled rejections are forwarded to the shell. Any error fails the run;
   React key/prop warnings show up here in dev builds.
3. **Motion** – the palette is collapsed, expanded, and collapsed again while
   the window height is sampled every ~2 ms (1 ms timer resolution). The probe
   requires monotonic movement, at most a 90 px step, at least 4 distinct
   heights, and under 700 ms.
4. **Vision** – the active vision model scores typography, spacing, contrast and
   layout (1–10) and lists concrete defects. A shot passes only with `verdict:
   pass`, every score ≥ 8, and no critical/major issue.

## Fixes the loop produced

Iteration 1 exposed real defects rather than taste differences:

- **Contrast**: secondary text sat at 1.66–2.72:1 on black (`text-white/20…/40`).
  The whole text scale was lifted to `/48…/58`, coloured status labels to `/70+`,
  so every visible label now clears AA. This is the largest visual change in the
  phase and it makes the OLED surface usable in daylight.
- **Overflow**: the pulsing glow was inset by −1 px, adding a 1 px horizontal and
  vertical overflow. It is now clipped by the palette root.
- **Palette height**: the scroll container's 32 px padding was missing from the
  height calculation, so the last row (e.g. the working status) was cut off. The
  chrome constant now includes it, the palette may shrink again, and the
  scrollbar is hidden in modern mode.
- **Approval focus**: expanding into an approval scrolled to the bottom and
  clipped the card header. The card is now scrolled into view.
- **Status verb**: `move_file` was announced as “Searching your files” because
  the label contained “folder”. Tool names now win, with a “Tidying your files”
  verb for file operations.
- **Search header**: at 440 px the query title clipped into “0 …”. The header is
  now two rows (query + path/duration, then filter chips) with 24 px targets.
- **Notification state**: the toggle could read ON while the chip still said
  “Hooks unregistered”. The shell now switches to “starting” immediately.
- **Motion**: the ease-out resize snapped up to 88 px in one sample on collapse;
  a smoothstep curve keeps both directions within 79 px, ~150 ms.

## Final result (packaged exe, RX 9070 XT, 240 Hz QD-OLED)

```text
12 / 12 screenshots pass the vision gate (scores 9-10, no issues)
12 / 12 state audits: 0 contrast violations, 0 small targets, 0 overflow
console: 0 errors, 0 warnings
expand:   155 ms, max step 72 px, 13 distinct heights
collapse: 143 ms, max step 79 px, 12 distinct heights
```

## Honest limits

- The local vision model (Qwen3-VL 8B, switched from 2B for this phase) is a
  useful but lenient reviewer: its structured scores cluster at 9–10 and its
  free-text “second look” tends to invent generic nits. The free-text pass is
  therefore **off by default** and never gates a run; the JSON verdict and the
  deterministic audit are the gate. Taste still needs a human.
- Contrast on gradient/photo backgrounds is not measured automatically.
- Frame pacing is judged by the window-height sampler and the rAF stats from
  Phase 23, not by a GPU capture tool.
- 24 px targets are enforced; 44 px (touch guidance) is not, because this is a
  mouse/keyboard desktop app.
- The `--qa` flags and demo fixtures are a test surface compiled into the app,
  reachable only by launching the exe with those arguments.
