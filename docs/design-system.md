# Phase 28: design system

Compa now has one token-driven design system with a light, dark and system
theme, a two-pane Settings surface, and shared controls. Every existing feature
and backend setting is preserved; the panels below only change presentation and
wire up settings that previously had no UI.

## Token layer

`frontend/src/index.css` defines the palette once and maps it into Tailwind with
`@theme inline`:

| Token | Dark | Light |
| --- | --- | --- |
| `--compa-fg` | `#ffffff` | `#1d1d1f` |
| `--compa-canvas` | `#000000` (OLED) | `#eef0f4` |
| `--compa-surface` | `#08080a` | `#fdfdfe` |
| `--compa-raised` | `#121216` | `#ffffff` |
| `--compa-sunken` | `#1b1b20` | `#e7e9ef` |
| `--compa-accent` | `#0068e0` | `#0068e0` |

Components use `text-fg/80`, `bg-fg/[0.03]`, `bg-sunken`, `border-fg/10` and
`bg-accent`. Text on accent uses `accent-ink`, which is brighter in dark mode
and deeper in light mode, so 11 px labels still clear WCAG AA. instead of literal white/black. Because the foreground token flips,
one class tree renders correctly in both themes. Semantic colours
(`positive`, `warning`, `danger`, `accent`) and chart colours are tokens too.

`frontend/src/lib/theme.ts` resolves `system` through
`prefers-color-scheme`, mirrors the preference in `localStorage` so the last
theme paints before React mounts, and keeps `<html data-theme>` in sync at
runtime. `<html data-motion="off">` disables CSS animation; `MotionConfig`
reduces Framer Motion at the same time.

## Settings surface

`SettingsView.tsx` is a two-pane layout:

| Category | Contents |
| --- | --- |
| General | Agent name, default language, communication style, launch at login |
| Identity & Memory | Identity card, knowledge-base manager (add / delete / raw edit / reset) |
| Appearance | Theme, animations, window mode, backdrop effect |
| System & Permissions | Summon shortcut, Explorer menu, notifications, autonomy, foreground fallback, UI-tree depth |
| Engine | Context window, model standby, restart-engine action, model catalog |
| Hardware | GPU layers, VRAM override, vision downscaling, CPU thread limit, process priority |

The rail is icon-only in the 440 px classic window and shows labels from 560 px.
Category icons are coloured rounded squares; the active row uses a rounded
foreground tint. Panes cross-fade with an ease-out entrance and an ease-in exit.

`SettingsUi.tsx` is the shared primitive set: rounded cards with diffused
shadows, rows, green iOS switches, sliding segmented controls, text/number
fields, primary/quiet/danger buttons, badges and notes. All primitives are
keyboard accessible (`role="switch"`, `role="radiogroup"`, focus rings).

## Settings added in this phase

| UI control | Backend / shell field | Effect |
| --- | --- | --- |
| Agent name | `agent_name` | identity, window title, prompt |
| Default language | `agent.language` | reply language instruction |
| Communication style | `agent.communication_style` | tone/length instruction |
| Autonomy level | `agent.autonomy` | planning/asking guidance; never bypasses approvals |
| Launch at login | `desktop.json: start_with_windows` | per-user `Run` key with `--tray` |
| Theme | `desktop.json: theme` | light/dark/system; native Mica variant |
| Animations | `desktop.json: animations` | CSS + Framer Motion |
| Backdrop effect | `desktop.json: backdrop` | Mica or OLED black (modern palette) |
| GUI fallback | `automation.allow_foreground_fallback` | blocks cursor/keyboard fallback when off |
| Knowledge manager | `GET/PUT/DELETE /knowledge` | local fact editing with `.bak` backup |
| Context window | `engine.context_window` + `ollama.context_window` | next engine start |
| Model standby | `ollama.keep_alive_seconds` | Ollama fallback residency |
| CPU threads | `engine.cpu_threads` | llama-server `--threads` |
| Process priority | `engine.process_priority` | llama-server process class |
| GPU layers / VRAM | `engine.gpu_layers` / `vram_override_gb` | planner override; empty = auto |
| Vision downscaling | `automation.screenshot_max_width` + `vision.screen_max_width` | capture size |
| UI-tree depth | `automation.max_element_depth` | UIA traversal depth |

`PATCH /settings/editable` validates every value with Pydantic, writes
`settings.json` atomically enough for local use, and returns
`restart_required` for inference changes. `POST /engine/restart` rebuilds the
llama.cpp client without restarting the app.

Autonomy is a prompt/policy hint only: **every mutating tool still produces an
approval card**. That invariant is intentional and documented in the panel.

## Verification

- 437 backend pytest tests, 10 Rust shell tests, TypeScript + Vite build pass.
- Phase 24 visual QA runs per theme:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\visual_qa.ps1 -Theme dark  -OutDir build\phase28-dark
powershell -ExecutionPolicy Bypass -File scripts\visual_qa.ps1 -Theme light -OutDir build\phase28-light
```

The script now accepts `-Theme dark|light`, passes `theme=` to the `--qa`
surface, and restores the previous theme/animations after the run.

## Remaining polish

- Browser-hosted chat recipient detection and per-session UIA caches are
  security work, unrelated to this visual layer.
- The diagnosis sparkline colours use CSS variables now; more chart palettes
  can be added without touching components.
