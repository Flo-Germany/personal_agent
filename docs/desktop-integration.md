# Phase 23: Windows integration and the modern palette

Compa stopped being a chat window that happens to be an exe. It is now a tray
daemon with a system-wide summon key, an Explorer verb, a strictly opt-in
notification listener, and a second shell mode built for large OLED panels.

Everything here lives in the Tauri layer (`frontend/src-tauri/src/`) plus the
React shell (`frontend/src/`). The Python agent and the llama.cpp engine are
unchanged except for one default (`ollama.keep_alive_seconds`, see below).

| Area | Files |
| --- | --- |
| Shell settings, hooks, window modes, QA surface | `frontend/src-tauri/src/desktop.rs` |
| Toast listener | `frontend/src-tauri/src/notifications.rs` |
| Window/plugin wiring, tray | `frontend/src-tauri/src/lib.rs` |
| Frontend bridge | `frontend/src/lib/desktop.ts`, `frontend/src/hooks/useDesktop.ts` |
| Modern shell | `frontend/src/components/ModernPalette.tsx` |
| Integration settings UI | `frontend/src/components/DesktopSettings.tsx` |
| QA matrix | `scripts/qa_phase23.ps1`, `scripts/screenshot_window.ps1` |

## 1. Global shortcut and tray daemon

- Default: `Ctrl + Shift + Space`, registered through
  `tauri-plugin-global-shortcut` when the app starts.
- Remap: **Settings → Windows integration → Summon shortcut**, click the chord
  chip and press the new combination. Only chords with at least one modifier and
  a non-modifier key are accepted; the stored form is canonical
  (`Ctrl+Shift+KeyK` is shown as `Ctrl + Shift + K`).
- Conflicts: before a remap is persisted, the new chord is registered. If
  Windows refuses it (reserved or already owned by another app), the previous
  chord is re-registered, the settings file is left untouched, and the panel
  shows the Windows error. The error stays visible until a successful change.
- Lifecycle: `RunEvent::Exit` calls `shutdown`, which unregisters every global
  shortcut, joins the notification thread, and writes `exited: true` +
  `shortcut_registered: false` into `desktop-state.json`. Closing the window
  does not quit; it hides to the tray, and the webview plus the loaded model
  stay alive.
- Instant summon: the LLM stays loaded. The native llama.cpp server is resident
  for the life of the app; for the Ollama fallback the shipped
  `ollama.keep_alive_seconds` is now `-1` (stay resident until quit). Summoning
  only shows and focuses the existing window, so there is no engine start, no
  model load, and no backend spawn on that path.

Tray menu and left-click both toggle the window through the same
`desktop::toggle_main`, which also emits `compa://summon` so the palette can
focus its input.

## 2. Explorer context menu

Enabling **Explorer context menu** writes two `HKCU` keys:

```text
HKCU\Software\Classes\*\shell\Compa          (files)
HKCU\Software\Classes\Directory\shell\Compa  (folders)
  (default)       = Analyze with Compa
  Icon            = <path to Personal Agent.exe>
  MultiSelectModel = Single
  command\(default) = "<exe>" --analyze "%1"
```

- The path is quoted, so spaces and non-ASCII folders are safe.
- Disabling deletes both keys; no elevation is needed because the keys are
  per-user.
- `Personal Agent.exe --analyze "<path>"` is handled by the single-instance
  plugin: if Compa is already running, the second process forwards the path to
  the running instance, which focuses itself and starts a chat turn with
  "Analyze the file at &lt;path&gt; …". If it is not running, the same argument is
  consumed during startup before the window is shown.
- `desktop-state.json` exposes `context_menu_enabled` and the command string is
  verified by the QA script, so the registry and the UI cannot drift silently.

## 3. Smart notifications

- Backend: WinRT `UserNotificationListener` (`windows` crate), COM
  multithreaded, toast kind only.
- Flow: `RequestAccessAsync` → if `Allowed`, poll every 10 s, diff by
  notification `Id`, read `AppInfo.DisplayName` plus the `ToastGeneric` text
  elements, and emit `compa://notification` with the batch.
- Status is surfaced in the panel as `active` (with poll count), `starting`,
  `disabled`, or the Windows refusal (`denied by Windows …`). A refusal stops
  the thread instead of retrying in a loop.
- Strict off switch: toggling off sets the stop flag, joins the listener thread,
  releases the WinRT objects, resets the poll counter to `0`, and reports
  `Hooks unregistered · 0 threads · 0 polls`.
- Privacy: toast text is parsed in-process and never leaves the machine; the
  hook is only registered while the setting is on.

## 4. Dual-mode UI

**Classic** is the previous chat window: 440×680, full history, tool task cards,
composer. **Modern** is a Raycast/Spotlight-style palette:

- 720 wide, centred horizontally at 14 % of the screen height, frameless
  (`decorations: false`, transparent webview, no shadow).
- Collapsed height `136 px`: one search field, three quick prompts, and a
  status strip (`model · shortcut to summon`).
- It grows downward once the agent works or has something to show: the body
  measures its rendered content and asks the Rust side for a height between
  `150` and `620 px` (settings: `748 px`). While streaming it only grows and at
  most once every 600 ms, so token rendering never fights the window animation.
  The native resize is a 14-step ease-out in Rust; the content fades in with
  Framer Motion (`AnimatePresence`, spring-free opacity/translate).
- There are no raw tool logs. The status row is three pulsing dots plus a single
  verb (`Reading the screen`, `Running a command`, `Searching your files`, …),
  and the answer streams with a blinking caret.
- During Set-of-Mark / screen analysis a violet-emerald glowing frame pulses
  around the whole window (`compa://` state → `data-analyzing`), so screen
  parsing is legible without any "thinking" text.
- `Esc` clears the input, then closes settings, then hides to the tray.
  `Ctrl+,` opens settings, like Raycast. `Enter` sends.
- Drag: the header and the whole palette background are Tauri drag regions.

### OLED and Mica layering

The primary surfaces use **`#000000`** (`--compa-oled`). The two backdrop modes
decide what sits in front of the OS compositor:

| `backdrop` | Window background | Effect |
| --- | --- | --- |
| `solid` | `#000000` | Strict OLED black, no blur, maximum contrast |
| `mica` | `rgba(0, 0, 0, 0.88)` | Native Windows 11 Mica blur behind a 12 % veil, so the desktop softly bleeds through while black stays black on QD-OLED |

Mica is applied with `window-vibrancy::apply_mica` (falling back to a clear
error message if the compositor refuses) and verified in QA by reading
`DWMWA_SYSTEMBACKDROP_TYPE` (`2` = Mica). Switching to `solid` clears the
backdrop (`DWMWA_SYSTEMBACKDROP_TYPE != 2`).

## 5. State files

`%LOCALAPPDATA%\com.personalagent.desktop\desktop.json` — settings:

```json
{
  "shortcut": "Ctrl+Shift+Space",
  "notifications": false,
  "mode": "classic",
  "backdrop": "mica",
  "context_menu": false
}
```

`desktop-state.json` — live truth, rewritten by the app every 2 s and after
every change. It is the QA interface:

```text
shortcut, shortcut_registered, shortcut_error,
notifications_enabled, notifications_status, notification_polls,
context_menu_enabled, context_menu_error,
mode, backdrop, backdrop_error, qa_error,
exited, frame_stats { fps, max_frame_ms, frames, long_frames, mode, at },
pid, updated_at
```

Writes are atomic (temp file + rename), so a reader never sees a partial file.

## 6. Automation surface (`--qa`)

A second instance launched with `--qa …` is forwarded by the single-instance
plugin and applies changes through the exact same code path as the UI. This is
how the QA matrix drives the shell without synthetic mouse input:

| Flag | Effect |
| --- | --- |
| `shortcut=Ctrl+Alt+F9` | Remap (conflict handled like the UI) |
| `mode=modern`, `backdrop=solid` | Switch shell / backdrop |
| `notifications=on|off` | Start / stop the WinRT listener |
| `context_menu=on|off` | Write / remove the Explorer keys |
| `view=settings|chat` | Open a view |
| `show`, `hide` | Focus or hide the window |
| `render` | Start the 3.4 s synthetic streaming/layout stress run |
| `quit` | Shut down cleanly (unregisters hooks) |

Unknown keys are reported in `qa_error` instead of being ignored.

## 7. QA matrix

```powershell
powershell -ExecutionPolicy Bypass -File scripts\qa_phase23.ps1
# faster loop without the destructive exit test:
powershell -ExecutionPolicy Bypass -File scripts\qa_phase23.ps1 -SkipExitTest -SampleSeconds 4
```

The script restores the original settings at the end and writes
`build/phase23-qa.json`. It exits `1` if any check fails.

| ID | Verifies |
| --- | --- |
| `hook-own` | `RegisterHotKey(Ctrl+Shift+Space)` is refused elsewhere → the app really owns the chord |
| `hook-registered` | State reports registered with no error |
| `hook-hide` / `hook-summon` | Tray hide hides the window, and a real `keybd_event` chord brings it back |
| `hook-conflict` | A chord reserved by another process is rejected, the old chord is restored, error is surfaced |
| `hook-recover` | Re-applying the original chord clears the error |
| `hook-exit` | Quit sets `exited`, unregisters, and frees the chord for other apps (relaunches afterwards) |
| `res-baseline` / `res-listening` | Idle CPU/threads sampled; listener polls while enabled |
| `res-unregistered` / `res-cpu` | Disable zeroes polls, drops the thread, returns CPU to baseline |
| `render-mica` | Mica applied, `DWMWA_SYSTEMBACKDROP_TYPE = 2`, no error |
| `render-oled` | CSS carries `#000000` + solid rule; window is frameless and transparent |
| `render-frames` | Synthetic 3.4 s stream: FPS, worst frame, long frames within budget |
| `render-solid` | OLED mode clears the Mica backdrop |
| `shell-menu` / `shell-menu-off` | Registry command routes `--analyze "%1"`; disabling removes it |
| `restore` | Original mode/backdrop/shortcut/toggles restored |

Measured on the development machine (RX 9070 XT, 240 Hz QD-OLED, Windows 11):
20 checks pass, 0 failures.

```text
render:  240 fps, worst frame 4.3 ms, 0 long frames, 289 frames over 3.4 s
Mica:    DWMWA_SYSTEMBACKDROP_TYPE = 2 (solid = 1)
toasts:  listener active, polls 1 -> 2 in 11 s, threads 30 -> 32
 CPU:    0.0 % enabled, 0.0 % after disable (baseline 0.0 %)
```

`scripts/screenshot_window.ps1 -Out build\shot.png` captures the window with DWM
compositing for visual review.

## 8. Honest limits

- **Mica is Windows 11 only.** On Windows 10, or when the DWM backdrop is
  refused, `backdrop_error` is populated and the palette keeps its black veil.
  The `solid` backdrop always works.
- **Notification access is not guaranteed.** Windows may deny
  `UserNotificationListener` to unpackaged desktop apps; the panel shows the
  exact refusal and the hook stays off. Toast parsing reads text elements only:
  no images, buttons, or inline replies.
- **Shortcut space is shared.** OS-reserved chords are rejected by Windows; the
  app reports the failure and keeps the previous chord. The QA conflict test
  reserves a chord in a helper process to prove the restore path.
- **One conversation surface.** Modern mode shows the current exchange, not a
  browsable history list; switch to Classic for the full transcript.
- **Frame numbers need a visible window.** Windows throttles `requestAnimationFrame`
  for occluded or minimised windows, so the stress result is only meaningful
  with the palette on screen.
- **`--qa` is a test surface**, not a public API. It is intentionally only
  reachable by launching the exe with those arguments.
