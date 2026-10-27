# Phase 26: universal background app control

Compa can now drive any Windows application through two routes that never touch
the physical mouse, never steal keyboard focus, and never switch the user's
desktop.

```text
request
  |
  +-- Priority 1: ui_query  ->  ui_action (UIA patterns, out-of-process)
  |
  +-- Priority 2: isolated_workspace attach
  |                 -> analyze_screen_ui(headless=true)  (PrintWindow + SoM)
  |                 -> click_element / keyboard_type_text (window messages)
  |                 -> isolated_workspace release
  |
  +-- Priority 3: visible foreground input (only with explicit consent)
```

## 1. UI Automation driver

`backend/tools/uia_driver.py` + `ui_query` / `ui_action`.

- `ui_query` walks the target window's UIA tree (depth ≤ 6, ≤ 220 nodes, default
  120) and returns each element's short id, control type, name, automation id,
  class, rectangle, enabled/offscreen flags, and the patterns it **really**
  supports. Pattern support is probed with `GetCurrentPattern`; pywinauto's
  lazy `iface_*` properties report every pattern otherwise.
- `ui_action` runs `invoke`, `set_value`, `toggle`, `select`, `expand`,
  `collapse`, `scroll_into_view`, `focus`, `get_text`, `type`, or `click`
  against an element id.
  - Patterns are out-of-process COM calls: no focus, no cursor, works while the
    window is covered, minimised, or on another virtual desktop.
  - `click` is a fallback for controls without a pattern: it posts
    `WM_LBUTTONDOWN/UP` to the deepest child under the element's center in
    client coordinates.
  - `type` uses `ValuePattern.SetValue`, or `WM_SETTEXT` when the element owns a
    window handle. It refuses rather than stealing focus.
- Ids (`u1`, `u2`, …) belong to one tree query. A second query invalidates the
  previous ids, and stale ids raise a clear error instead of clicking the wrong
  control.
- Every action observes the cursor position before and after, reports
  `cursor_moved: false` (the drivers never call a cursor API) and
  `cursor_stable` for transparency. It deliberately does **not** move the
  cursor back: that would hijack a mouse the user is actively using.

Verified on Character Map: 27 nodes, 16 interactable, real patterns
(`Edit: value+text`, `Button: invoke`, `ComboBox: value+expand`), `set_value`
and `invoke` succeed with `cursor_moved: false`.

## 2. Isolated workspace

`backend/tools/workspace.py` + `isolated_workspace`, plus `vd_worker.py`.

Two backends are picked automatically:

| Backend | When | What happens |
| --- | --- | --- |
| `virtual_desktop` | Another virtual desktop exists, or creation succeeds | The window is moved with the documented `IVirtualDesktopManager.MoveWindowToDesktop`; the user's desktop stays exactly as it is |
| `offscreen` | No second desktop and creation is unavailable | The window is moved to `(-4000, 0)` and kept there; it is invisible on every monitor |

- **Why a worker subprocess?** Creating a virtual desktop needs pyvda's
  undocumented COM interface. On Windows 11 25H2 (build 26200) `pyvda` 0.4.2
  raises an access violation in `CreateDesktopW`. That call therefore runs in
  `vd_worker.py`: a crash kills a short-lived process and the manager falls back
  to `offscreen` instead of taking the backend down. `status` reports the
  capability and the worker error verbatim.
- **Move/restore use the documented manager** (`IVirtualDesktopManager`,
  comtypes) and store the original desktop GUID + rectangle in
  `data/automation/workspaces.json`, so `release` restores the window even after
  a backend restart.
- **Capture** is `PrintWindow(PW_RENDERFULLCONTENT)` into a DIB, saved as PNG.
  It returns real pixels for Win32, UWP (Notepad) and Chromium/Electron windows
  even when they are off-screen or on another desktop; the result includes
  `ink_ratio` so a black frame is visible instead of silently empty.
- **Input** is `PostMessage`/`SendMessage`:
  - clicks go to the deepest child window under the point (so Edit and list
    controls receive them), in client coordinates;
  - typing uses `GetGUIThreadInfo(...).hwndFocus` to find the app's *own* focused
    control and sends `WM_CHAR` to it.
- **Release** restores the original desktop (documented move) or rectangle and
  maximised state, removes a desktop it created, and clears the state file.

## 3. Headless Set-of-Mark

`analyze_screen_ui(headless=true, window_title=… | window_handle=…)` captures the
isolated window with `PrintWindow` and runs the exact same SoM pipeline on those
pixels: UIA detection, annotation, grounding, snapshot store.

- `SOMSnapshot.input_mode` is `"background"`; `to_public()` exposes it.
- OCR is allowed on a headless capture because the image is guaranteed to be the
  target window, unlike a background grab of the screen.
- `click_element` on such a snapshot posts the click into the isolated window;
  `keyboard_type_text` sends characters to the app's focused control. Both
  return `mode: "background"` and never move the user's pointer.
- The annotated PNG keeps the numbering, so the model can still reason visually
  about custom-drawn surfaces.

Verified end-to-end on Character Map: attach → capture (ink 0.965) → headless SoM
(12 elements, suggested id 10) → background click (`cursor_moved: false`) →
`keyboard_type_text` (3 chars) → release restores the window.

## 4. Routing and safety

- The system prompt defines the strict hierarchy and asks the agent to report
  which route it used and whether the screen was touched.
- `ui_query` and `isolated_workspace capture/status` are read-only (no approval).
  `ui_action` and `isolated_workspace attach/release` require approval, like the
  existing click tools.
- The GUI tool set is always sent with the base tools, so switching routes does
  not invalidate the cached prompt prefix.
- The agent must call `isolated_workspace release` when the task ends or fails;
  the manager also cleans up on the next `status`/`release` after a restart.

## 5. Verification

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_background_automation.py -q
backend\.venv\Scripts\python.exe scripts\live_phase26.py
```

- 12 unit tests cover tool contracts, the confirmation policy, prompt
  hierarchy, tree ids/filtering/truncation, stale ids, background click/type
  routing (with a pointer that must never be called), workspace state shape, the
  virtual-desktop worker, and the headless-handle guard.
- `scripts/live_phase26.py` is the live acceptance run against Character Map and
  Explorer. It asserts `cursor_moved: false` for every action, that capture is
  non-black, that `set_value` lands in the control, and that release restores
  the window.

## 6. Honest limits

- **Virtual-desktop creation is version-dependent.** On this 25H2 build pyvda's
  internal interface crashes, so the workspace runs off-screen. The documented
  move path works and is used whenever another desktop exists; the worker keeps
  a future pyvda fix usable without a code change.
- **Windows on another desktop are not enumerated** by `EnumWindows`. Attach
  first, keep the returned handle, and use it for later calls; title lookup only
  sees the current desktop.
- **Background typing depends on the app.** `ValuePattern` and `WM_CHAR` cover
  classic Win32 and many Electron apps, but some Chromium text fields ignore
  synthetic characters unless they own focus. The driver reports the mechanism
  it used instead of silently typing into the wrong place.
- **UIA can be slow on hung apps.** Windows has no hard per-call timeout; a
  frozen target can delay a query. The tool is read-only and the agent is
  instructed not to retry in a loop.
- **Not every control is reachable in the background.** Apps that only respond
  to raw input (some games/anti-cheat surfaces) still require the visible
  foreground route, which the agent may only use with explicit user consent.
- Coordinates are physical pixels; the workspace and UIA both run DPI-aware, so
  125–200 % scaling does not shift clicks.
