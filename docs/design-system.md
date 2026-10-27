# Phase 28: design system handover

## What is implemented

- **Two-pane settings shell** (`frontend/src/components/SettingsView.tsx`):
  - left rail with colored category icons (General, System & Permissions,
    Engine, Hardware), a rounded active highlight, and an icon-only layout in
    the 440 px classic window;
  - right pane with a compact header (category title + hint + close) and a
    scrolling content area.
- **Reuse over rewrite**: the engine/model catalog, hardware profile, VRAM plan,
  Windows integration (shortcut, Explorer menu, notifications, backdrop) and the
  identity data are the existing components. `ModelSettings` gained an
  `embedded` mode that hides its own header so the two-pane shell owns the
  chrome. No existing setting was removed.
- **Cards** in the general pane use `rounded-2xl`, soft diffused shadows and the
  OLED surface treatment.

## What is not implemented yet (and why)

The full brief is a multi-step migration, not a one-shot edit:

1. **Light / dark / system tokens.** The application is built with literal
   white-alpha utilities in ~20 components. A real light mode requires a token
   migration (`--surface`, `--text`, `--accent`, …) plus a visual pass over the
   palette, chat, composer, tool cards, diagnosis charts, approval cards and
   dialogs. Half-converted light mode would look broken, so it was not faked.
2. **Settings that have no backend yet** must be implemented end-to-end before
   they get a control, otherwise the UI would lie:
   - startup with Windows (registry Run key, Rust);
   - default language and communication style (prompt + persisted settings);
   - knowledge-base manager (list/edit/delete endpoints + editor UI);
   - autonomy level and GUI fallback (policy fields + prompt/executor wiring);
   - CPU thread limit and process priority (llama-server args / Windows API);
   - theme and animation preferences (persisted desktop settings + token layer).
3. **Primitive migration.** Green iOS-style switches, sliding segmented
   controls, blue primary buttons and floating popup cards are designed but not
   yet applied globally. The existing controls work and are keyboard accessible;
   migrating them is a component-by-component change.

## Suggested order for the next slice

1. Token layer + light theme for the shell/palette/settings (smallest visible
   surface first), then chat and tool cards.
2. Persist `theme`, `animations`, `start_with_windows` in `desktop.json` and add
   the Appearance pane controls.
3. Add `language`, `communication_style`, `autonomy`, `gui_fallback`,
   `cpu_threads`, `process_priority` to the backend settings with validation,
   then expose them in the grouped cards.
4. Knowledge API + editor.
5. Replace the remaining controls with the shared primitives and run the Phase
   24 visual QA matrix in both themes.
