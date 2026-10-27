//! Phase 23 desktop integration: global shortcut, Explorer menu, window modes.
//!
//! Settings live in `desktop.json` inside the app data folder. Live runtime
//! state, including hook registration results, is mirrored to
//! `desktop-state.json`, which is also the QA interface used by
//! `scripts/qa_phase23.ps1`. A second instance started with
//! `--qa key=value ...` is forwarded by the single-instance plugin and applies
//! the same settings path as the UI, so hook conflicts and notification
//! lifecycle can be verified without clicking through the app.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, LogicalSize, Manager, State};
use tauri_plugin_global_shortcut::{
    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
};

use crate::notifications::NotificationService;

pub const EVENT_SUMMON: &str = "compa://summon";
pub const EVENT_ANALYZE: &str = "compa://analyze";
pub const EVENT_SETTINGS: &str = "compa://settings";
pub const EVENT_NOTIFICATION: &str = "compa://notification";
pub const EVENT_QA_RENDER: &str = "compa://qa-render";
pub const EVENT_UI: &str = "compa://ui";
pub const EVENT_DEMO: &str = "compa://qa-demo";
pub const EVENT_QA_AUDIT: &str = "compa://qa-audit";

pub const MODERN_WIDTH: f64 = 720.0;
pub const MODERN_COLLAPSED_HEIGHT: f64 = 136.0;
pub const MODERN_MAX_HEIGHT: f64 = 780.0;
pub const CLASSIC_WIDTH: f64 = 440.0;
pub const CLASSIC_HEIGHT: f64 = 680.0;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct DesktopSettings {
    pub shortcut: String,
    pub notifications: bool,
    pub mode: String,
    pub backdrop: String,
    pub context_menu: bool,
}

impl Default for DesktopSettings {
    fn default() -> Self {
        Self {
            shortcut: "Ctrl+Shift+Space".to_string(),
            notifications: false,
            mode: "classic".to_string(),
            backdrop: "mica".to_string(),
            context_menu: false,
        }
    }
}

impl DesktopSettings {
    fn normalized(mut self) -> Self {
        self.shortcut = self.shortcut.trim().to_string();
        self.mode = if self.mode == "modern" { "modern" } else { "classic" }.into();
        self.backdrop = if self.backdrop == "solid" { "solid" } else { "mica" }.into();
        self
    }

    pub fn is_modern(&self) -> bool {
        self.mode == "modern"
    }
}

#[derive(Clone, Serialize, Default)]
pub struct FrameStats {
    pub fps: f64,
    pub max_frame_ms: f64,
    pub frames: u64,
    pub long_frames: u64,
    pub mode: String,
    pub at: u64,
}

/// Console error/warning forwarded by the webview (Phase 24 QA).
#[derive(Clone, Serialize, Deserialize, Default)]
pub struct ConsoleEvent {
    pub level: String,
    pub message: String,
    pub at: u64,
}

#[derive(Default)]
pub struct RuntimeState {
    pub shortcut_registered: bool,
    pub shortcut_error: String,
    pub context_menu_error: String,
    pub backdrop_error: String,
    pub notifications_status: String,
    pub notifications_polls: u64,
    pub qa_error: String,
    pub exited: bool,
    pub frame_stats: FrameStats,
    pub console_events: Vec<ConsoleEvent>,
    pub layout_audit: serde_json::Value,
}

pub struct DesktopRuntime {
    pub settings: Mutex<DesktopSettings>,
    pub runtime: Mutex<RuntimeState>,
    pub pending_analyze: Mutex<Option<String>>,
    pub notifications: Arc<NotificationService>,
    pub height_generation: AtomicU64,
}

impl Default for DesktopRuntime {
    fn default() -> Self {
        Self {
            settings: Mutex::new(DesktopSettings::default()),
            runtime: Mutex::new(RuntimeState::default()),
            pending_analyze: Mutex::new(None),
            notifications: Arc::new(NotificationService::new()),
            height_generation: AtomicU64::new(1),
        }
    }
}

// ------------------------------------------------------------------ storage

fn data_dir(app: &AppHandle) -> PathBuf {
    app.path()
        .app_local_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("com.personalagent.desktop"))
}

fn settings_path(app: &AppHandle) -> PathBuf {
    data_dir(app).join("desktop.json")
}

fn state_path(app: &AppHandle) -> PathBuf {
    data_dir(app).join("desktop-state.json")
}

fn atomic_write(path: &PathBuf, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let temp = path.with_extension("tmp");
    std::fs::write(&temp, bytes)?;
    std::fs::rename(temp, path)
}

fn load_settings(app: &AppHandle) -> DesktopSettings {
    std::fs::read_to_string(settings_path(app))
        .ok()
        .and_then(|text| serde_json::from_str::<DesktopSettings>(&text).ok())
        .unwrap_or_default()
        .normalized()
}

fn persist_settings(app: &AppHandle, settings: &DesktopSettings) -> Result<(), String> {
    let json = serde_json::to_vec_pretty(settings).map_err(|e| e.to_string())?;
    atomic_write(&settings_path(app), &json).map_err(|e| e.to_string())
}

fn now_epoch() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

pub fn settings_view(settings: &DesktopSettings) -> serde_json::Value {
    serde_json::to_value(settings).unwrap_or_default()
}

pub fn write_state(app: &AppHandle) {
    let runtime = app.state::<DesktopRuntime>();
    let settings = runtime
        .settings
        .lock()
        .map(|value| value.clone())
        .unwrap_or_default();
    let state = runtime.runtime.lock().map(|value| {
        serde_json::json!({
            "shortcut": settings.shortcut,
            "shortcut_registered": value.shortcut_registered,
            "shortcut_error": value.shortcut_error,
            "notifications_enabled": settings.notifications,
            "notifications_status": value.notifications_status,
            "notification_polls": value.notifications_polls,
            "context_menu_enabled": settings.context_menu,
            "context_menu_error": value.context_menu_error,
            "mode": settings.mode,
            "backdrop": settings.backdrop,
            "backdrop_error": value.backdrop_error,
            "qa_error": value.qa_error,
            "exited": value.exited,
            "frame_stats": value.frame_stats,
            "console_events": value.console_events,
            "layout_audit": value.layout_audit,
            "pid": std::process::id(),
            "updated_at": now_epoch(),
        })
    });
    let Ok(data) = state else { return };
    let _ = atomic_write(
        &state_path(app),
        &serde_json::to_vec_pretty(&data).unwrap_or_default(),
    );
}

// ---------------------------------------------------------------- shortcuts

pub fn format_shortcut(shortcut: &Shortcut) -> String {
    let mut parts: Vec<String> = Vec::new();
    if shortcut.mods.contains(Modifiers::CONTROL) {
        parts.push("Ctrl".into());
    }
    if shortcut.mods.contains(Modifiers::SHIFT) {
        parts.push("Shift".into());
    }
    if shortcut.mods.contains(Modifiers::ALT) {
        parts.push("Alt".into());
    }
    if shortcut.mods.contains(Modifiers::SUPER) {
        parts.push("Win".into());
    }
    parts.push(format!("{:?}", shortcut.key));
    parts.join("+")
}

fn is_modifier_key(key: Code) -> bool {
    let debug = format!("{key:?}");
    debug.contains("Control")
        || debug.contains("Shift")
        || debug.starts_with("Alt")
        || debug.contains("Super")
        || debug.contains("Meta")
}

pub fn validate_shortcut(value: &str) -> Result<Shortcut, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err("Enter a shortcut first".into());
    }
    let shortcut: Shortcut = trimmed
        .parse()
        .map_err(|_| format!("'{trimmed}' is not a valid shortcut"))?;
    if shortcut.mods.is_empty() {
        return Err("Add at least one modifier (Ctrl, Alt, Shift or Win)".into());
    }
    if is_modifier_key(shortcut.key) {
        return Err("The shortcut must end with a letter, number or function key".into());
    }
    Ok(shortcut)
}

pub fn register_shortcut(app: &AppHandle, value: &str) -> Result<Shortcut, String> {
    let shortcut = validate_shortcut(value)?;
    let manager = app.global_shortcut();
    let _ = manager.unregister_all();
    manager
        .register(shortcut)
        .map_err(|error| format!("Windows refused {value}: {error}"))?;
    Ok(shortcut)
}

pub fn unregister_shortcuts(app: &AppHandle) {
    let _ = app.global_shortcut().unregister_all();
}

// ------------------------------------------------------------- window state

pub fn focus_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
        let _ = app.emit(EVENT_SUMMON, ());
    }
}

pub fn toggle_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        match window.is_visible() {
            Ok(true) => {
                let _ = window.hide();
            }
            _ => focus_main(app),
        }
    }
}

pub fn on_shortcut(app: &AppHandle, event_state: ShortcutState) {
    if event_state == ShortcutState::Pressed {
        focus_main(app);
    }
}

#[cfg(target_os = "windows")]
fn set_context_menu(enabled: bool) -> Result<(), String> {
    use winreg::RegKey;
    use winreg::enums::HKEY_CURRENT_USER;

    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let exe = exe.to_string_lossy().to_string();
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    for base in [
        r"Software\Classes\*\shell\Compa",
        r"Software\Classes\Directory\shell\Compa",
    ] {
        if enabled {
            let (key, _) = hkcu
                .create_subkey(base)
                .map_err(|e| format!("Could not write {base}: {e}"))?;
            key.set_value("", &"Analyze with Compa")
                .map_err(|e| e.to_string())?;
            key.set_value("Icon", &exe).map_err(|e| e.to_string())?;
            key.set_value("MultiSelectModel", &"Single")
                .map_err(|e| e.to_string())?;
            let (command, _) = hkcu
                .create_subkey(format!(r"{base}\command"))
                .map_err(|e| e.to_string())?;
            command
                .set_value("", &format!("\"{exe}\" --analyze \"%1\""))
                .map_err(|e| e.to_string())?;
        } else {
            let _ = hkcu.delete_subkey_all(base);
        }
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn set_context_menu(_enabled: bool) -> Result<(), String> {
    Ok(())
}

/// Read back what the Explorer menu currently points at, for the UI and QA.
#[cfg(target_os = "windows")]
pub fn context_menu_command() -> Option<String> {
    use winreg::RegKey;
    use winreg::enums::HKEY_CURRENT_USER;
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let key = hkcu
        .open_subkey(r"Software\Classes\*\shell\Compa\command")
        .ok()?;
    key.get_value::<String, _>("").ok()
}

#[cfg(not(target_os = "windows"))]
pub fn context_menu_command() -> Option<String> {
    None
}

#[cfg(target_os = "windows")]
fn apply_vibrancy(window: &tauri::WebviewWindow, backdrop: &str) -> Result<(), String> {
    use window_vibrancy::{apply_mica, clear_mica};
    // clear_vibrancy() is macOS-only; Windows must reset the DWM backdrop type.
    let _ = clear_mica(window);
    if backdrop == "solid" {
        return Ok(());
    }
    apply_mica(window, Some(true)).map_err(|e| format!("Mica is unavailable: {e}"))
}

#[cfg(not(target_os = "windows"))]
fn apply_vibrancy(_window: &tauri::WebviewWindow, _backdrop: &str) -> Result<(), String> {
    Ok(())
}

fn modern_position(window: &tauri::WebviewWindow, width: f64, height: f64) {
    if let Ok(Some(monitor)) = window.current_monitor() {
        let scale = monitor.scale_factor();
        let size = monitor.size().to_logical::<f64>(scale);
        let x = ((size.width - width) / 2.0).max(0.0);
        let y = (size.height * 0.14).max(24.0);
        let _ = window.set_position(tauri::LogicalPosition::new(x, y));
        let _ = window.set_size(LogicalSize::new(width, height));
    } else {
        let _ = window.set_size(LogicalSize::new(width, height));
        let _ = window.center();
    }
}

/// Ask DWM to round the native window (and with it the Mica backdrop).
///
/// A `SetWindowRgn` window region clipped the backdrop correctly but crashed
/// WebView2 on Windows 11 25H2, so this uses the documented DWM attribute and
/// the CSS card radius is matched to the OS rounding instead.
#[cfg(target_os = "windows")]
fn round_window(window: &tauri::WebviewWindow, _radius_logical: f64) {
    // Temporarily disabled to bisect a startup crash.
    let _ = window;
}

#[cfg(not(target_os = "windows"))]
fn round_window(_window: &tauri::WebviewWindow, _radius_logical: f64) {}

pub fn apply_mode(app: &AppHandle, settings: &DesktopSettings) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "main window is missing".to_string())?;
    let _ = window.set_decorations(false);
    if settings.is_modern() {
        let current_height = window
            .inner_size()
            .map(|size| size.to_logical::<f64>(window.scale_factor().unwrap_or(1.0)).height)
            .unwrap_or(MODERN_COLLAPSED_HEIGHT);
        let height = if current_height > MODERN_COLLAPSED_HEIGHT + 8.0 {
            current_height.min(MODERN_MAX_HEIGHT)
        } else {
            MODERN_COLLAPSED_HEIGHT
        };
        modern_position(&window, MODERN_WIDTH, height);
        round_window(&window, 18.0);
        apply_vibrancy(&window, &settings.backdrop)
    } else {
        #[cfg(target_os = "windows")]
        {
            let _ = window_vibrancy::clear_mica(&window);
        }
        let _ = window.set_size(LogicalSize::new(CLASSIC_WIDTH, CLASSIC_HEIGHT));
        let _ = window.center();
        round_window(&window, 26.0);
        Ok(())
    }
}

pub fn animate_palette_height(app: &AppHandle, target: f64) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let runtime = app.state::<DesktopRuntime>();
    let modern = runtime
        .settings
        .lock()
        .map(|settings| settings.is_modern())
        .unwrap_or(false);
    // Classic mode has a fixed 440x680 window; only the palette animates.
    if !modern {
        return;
    }
    let target = target.clamp(MODERN_COLLAPSED_HEIGHT, MODERN_MAX_HEIGHT);
    let generation = runtime.height_generation.fetch_add(1, Ordering::SeqCst) + 1;
    let handle = app.clone();
    let scale = window.scale_factor().unwrap_or(1.0);
    let current = window
        .inner_size()
        .map(|size| size.to_logical::<f64>(scale).height)
        .unwrap_or(MODERN_COLLAPSED_HEIGHT);
    if (current - target).abs() < 1.0 {
        return;
    }
    std::thread::spawn(move || {
        let steps = 14;
        for step in 1..=steps {
            let state = handle.state::<DesktopRuntime>();
            if state.height_generation.load(Ordering::SeqCst) != generation {
                return;
            }
            let progress = step as f64 / steps as f64;
            // Smoothstep: no large first/last step, so collapse never snaps.
            let eased = progress * progress * (3.0 - 2.0 * progress);
            let height = current + (target - current) * eased;
            if let Some(window) = handle.get_webview_window("main") {
                let _ = window.set_size(LogicalSize::new(MODERN_WIDTH, height));
                round_window(&window, 18.0);
            }
            std::thread::sleep(Duration::from_millis(12));
        }
    });
}

// -------------------------------------------------------------- apply paths

/// Single code path for every settings change: UI save, CLI override, startup.
fn apply_settings(app: &AppHandle, incoming: DesktopSettings) -> Result<DesktopSettings, String> {
    let next = incoming.normalized();
    validate_shortcut(&next.shortcut)?;

    let runtime = app.state::<DesktopRuntime>();
    let previous = runtime
        .settings
        .lock()
        .map(|value| value.clone())
        .unwrap_or_default();
    let was_registered = runtime
        .runtime
        .lock()
        .map(|value| value.shortcut_registered)
        .unwrap_or(false);

    let notifications_changed = next.notifications != previous.notifications;
    if notifications_changed {
        if next.notifications {
            runtime.notifications.start(app.clone());
        } else {
            runtime.notifications.stop();
        }
    }

    if next.context_menu != previous.context_menu {
        if let Err(error) = set_context_menu(next.context_menu) {
            if notifications_changed {
                if previous.notifications {
                    runtime.notifications.start(app.clone());
                } else {
                    runtime.notifications.stop();
                }
            }
            return Err(error);
        }
    }

    if next.shortcut != previous.shortcut || !was_registered {
        if let Err(error) = register_shortcut(app, &next.shortcut) {
            // Put the previous shortcut back so the user keeps their summon key.
            let restored = register_shortcut(app, &previous.shortcut).is_ok();
            if notifications_changed {
                if previous.notifications {
                    runtime.notifications.start(app.clone());
                } else {
                    runtime.notifications.stop();
                }
            }
            if next.context_menu != previous.context_menu {
                let _ = set_context_menu(previous.context_menu);
            }
            if let Ok(mut state) = runtime.runtime.lock() {
                state.shortcut_registered = restored;
                state.shortcut_error = error.clone();
            }
            write_state(app);
            return Err(error);
        }
    }

    // Re-applying the mode re-centres the window, so only do it when the shell
    // really changes; toggling notifications must not move the palette.
    let mode_changed =
        next.mode != previous.mode || next.backdrop != previous.backdrop;
    let backdrop_error = if mode_changed {
        apply_mode(app, &next).err().unwrap_or_default()
    } else {
        runtime
            .runtime
            .lock()
            .map(|state| state.backdrop_error.clone())
            .unwrap_or_default()
    };

    if let Ok(mut state) = runtime.runtime.lock() {
        state.shortcut_registered = true;
        state.shortcut_error = String::new();
        state.context_menu_error = String::new();
        state.backdrop_error = backdrop_error;
        // start()/stop() own the live status; reflect the change immediately so
        // the settings panel never shows "unregistered" with the switch on.
        if notifications_changed {
            state.notifications_status = if next.notifications {
                "starting".into()
            } else {
                "disabled".into()
            };
        }
        if !next.notifications {
            state.notifications_polls = 0;
        }
    }
    if let Ok(mut current) = runtime.settings.lock() {
        *current = next.clone();
    }
    persist_settings(app, &next)?;
    write_state(app);
    let _ = app.emit(EVENT_SETTINGS, settings_view(&next));
    Ok(next)
}

// -------------------------------------------------------------------- argv

fn analyze_payload(path: &str) -> serde_json::Value {
    let candidate = std::path::Path::new(path);
    let exists = candidate.is_file();
    let canonical = std::fs::canonicalize(candidate)
        .ok()
        .map(|value| {
            value
                .to_string_lossy()
                .trim_start_matches(r"\\?\")
                .to_string()
        });
    serde_json::json!({
        "path": path,
        "exists": exists,
        "canonical": canonical,
    })
}

pub fn analyze_arg(args: &[String]) -> Option<String> {
    args.iter()
        .position(|arg| arg == "--analyze")
        .and_then(|index| args.get(index + 1))
        .map(|value| value.trim_matches('"').to_string())
        .filter(|value| !value.is_empty())
}

#[derive(Default, Clone, Debug)]
pub struct QaCommand {
    pub set: Vec<(String, String)>,
    pub render: bool,
    pub quit: bool,
    pub show: bool,
    pub hide: bool,
    pub view: Option<String>,
    pub demo: Option<String>,
    pub reset_logs: bool,
    pub audit: bool,
}

/// Parse `--qa mode=modern notifications=on render`.
pub fn qa_command(args: &[String]) -> Option<QaCommand> {
    let index = args.iter().position(|arg| arg == "--qa")?;
    let mut command = QaCommand::default();
    for token in &args[index + 1..] {
        if token.starts_with("--") {
            break;
        }
        match token.as_str() {
            "render" => command.render = true,
            "quit" => command.quit = true,
            "show" => command.show = true,
            "hide" => command.hide = true,
            "reset-logs" => command.reset_logs = true,
            "audit" => command.audit = true,
            _ => match token.split_once('=') {
                Some((key, value)) if key.trim() == "view" => {
                    command.view = Some(value.trim().to_string())
                }
                Some((key, value)) if key.trim() == "demo" => {
                    command.demo = Some(value.trim().to_string())
                }
                Some((key, value)) => {
                    command.set.push((key.trim().to_string(), value.trim().to_string()))
                }
                None => command.set.push((token.clone(), String::new())),
            },
        }
    }
    Some(command)
}

fn parse_bool(value: &str) -> Result<bool, String> {
    match value.to_lowercase().as_str() {
        "1" | "true" | "on" | "yes" | "enable" | "enabled" => Ok(true),
        "0" | "false" | "off" | "no" | "disable" | "disabled" => Ok(false),
        other => Err(format!("'{other}' is not a boolean")),
    }
}

fn apply_qa_settings(settings: &mut DesktopSettings, pairs: &[(String, String)]) -> Result<(), String> {
    for (key, value) in pairs {
        match key.replace('-', "_").as_str() {
            "shortcut" => settings.shortcut = value.clone(),
            "mode" => settings.mode = value.clone(),
            "backdrop" => settings.backdrop = value.clone(),
            "notifications" => settings.notifications = parse_bool(value)?,
            "context_menu" => settings.context_menu = parse_bool(value)?,
            other => return Err(format!("unknown setting '{other}'")),
        }
    }
    Ok(())
}

fn apply_qa_command(app: &AppHandle, command: &QaCommand) {
    let result = (|| -> Result<(), String> {
        if !command.set.is_empty() {
            let runtime = app.state::<DesktopRuntime>();
            let mut next = runtime
                .settings
                .lock()
                .map(|value| value.clone())
                .unwrap_or_default();
            apply_qa_settings(&mut next, &command.set)?;
            apply_settings(app, next)?;
        }
        Ok(())
    })();

    if let Err(error) = &result {
        let runtime = app.state::<DesktopRuntime>();
        if let Ok(mut state) = runtime.runtime.lock() {
            state.qa_error = error.clone();
        }
        eprintln!("qa command failed: {error}");
    } else {
        let runtime = app.state::<DesktopRuntime>();
        if let Ok(mut state) = runtime.runtime.lock() {
            state.qa_error = String::new();
        }
    }
    if command.reset_logs {
        let runtime = app.state::<DesktopRuntime>();
        if let Ok(mut state) = runtime.runtime.lock() {
            state.console_events.clear();
            state.layout_audit = serde_json::Value::Null;
            state.qa_error = String::new();
        }
    }
    write_state(app);

    if command.show {
        focus_main(app);
    }
    if command.hide {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.hide();
        }
    }
    if command.render {
        // Give the webview a beat to settle before the stress run starts.
        let handle = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(400));
            let _ = handle.emit(EVENT_QA_RENDER, ());
        });
    }
    if let Some(view) = &command.view {
        let handle = app.clone();
        let view = view.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(150));
            let _ = handle.emit(EVENT_UI, view);
        });
    }
    if let Some(demo) = &command.demo {
        let handle = app.clone();
        let demo = demo.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(150));
            let _ = handle.emit(EVENT_DEMO, demo);
        });
    }
    if command.audit {
        let handle = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(250));
            let _ = handle.emit(EVENT_QA_AUDIT, ());
        });
    }
    if command.quit {
        let handle = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(250));
            shutdown(&handle);
            handle.exit(0);
        });
    }
}

pub fn forward_args(app: &AppHandle, argv: &[String]) {
    if let Some(command) = qa_command(argv) {
        apply_qa_command(app, &command);
        return;
    }
    if let Some(path) = analyze_arg(argv) {
        if let Ok(mut pending) = app.state::<DesktopRuntime>().pending_analyze.lock() {
            *pending = Some(path.clone());
        }
        focus_main(app);
        let _ = app.emit(EVENT_ANALYZE, analyze_payload(&path));
    } else {
        focus_main(app);
    }
}

// ------------------------------------------------------------- lifecycle

pub fn shutdown(app: &AppHandle) {
    unregister_shortcuts(app);
    let runtime = app.state::<DesktopRuntime>();
    runtime.notifications.stop();
    if let Ok(mut state) = runtime.runtime.lock() {
        state.exited = true;
        state.shortcut_registered = false;
    }
    write_state(app);
}

pub fn init(app: &AppHandle) -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    let mut settings = load_settings(app);

    // A `--qa` override launched as the primary instance applies before the
    // window is shown, so QA never depends on a second process.
    let qa = qa_command(&args);
    let mut qa_error = String::new();
    if let Some(command) = &qa {
        if let Err(error) = apply_qa_settings(&mut settings, &command.set) {
            qa_error = error;
        }
    }

    let runtime = app.state::<DesktopRuntime>();
    if let Ok(mut current) = runtime.settings.lock() {
        *current = settings.clone();
    }

    if let Some(path) = analyze_arg(&args) {
        if let Ok(mut pending) = runtime.pending_analyze.lock() {
            *pending = Some(path);
        }
    }

    let shortcut_result = register_shortcut(app, &settings.shortcut)
        .map(|shortcut| format_shortcut(&shortcut));
    let (shortcut_registered, shortcut_error, shortcut_label) = match shortcut_result {
        Ok(label) => (true, String::new(), label),
        Err(error) => (false, error, settings.shortcut.clone()),
    };
    if shortcut_registered && shortcut_label != settings.shortcut {
        settings.shortcut = shortcut_label;
    }
    let context_error = match set_context_menu(settings.context_menu) {
        Ok(()) => String::new(),
        Err(error) => error,
    };
    let mode_error = apply_mode(app, &settings).err().unwrap_or_default();

    if let Ok(mut current) = runtime.settings.lock() {
        *current = settings.clone();
    }
    let _ = persist_settings(app, &settings);

    if let Ok(mut state) = runtime.runtime.lock() {
        state.shortcut_registered = shortcut_registered;
        state.shortcut_error = shortcut_error;
        state.context_menu_error = context_error;
        state.backdrop_error = mode_error;
        state.qa_error = qa_error;
        state.notifications_status = if settings.notifications {
            "starting".into()
        } else {
            "disabled".into()
        };
    }

    if settings.notifications {
        runtime.notifications.start(app.clone());
    }

    write_state(app);

    if let Some(command) = qa {
        if command.render
            || command.quit
            || command.show
            || command.hide
            || command.view.is_some()
            || command.demo.is_some()
            || command.audit
        {
            let app = app.clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(1400));
                if command.show {
                    focus_main(&app);
                }
                if command.hide {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.hide();
                    }
                }
                if let Some(view) = &command.view {
                    let _ = app.emit(EVENT_UI, view.clone());
                }
                if let Some(demo) = &command.demo {
                    let _ = app.emit(EVENT_DEMO, demo.clone());
                }
                if command.audit {
                    let _ = app.emit(EVENT_QA_AUDIT, ());
                }
                if command.render {
                    let _ = app.emit(EVENT_QA_RENDER, ());
                }
                if command.quit {
                    std::thread::sleep(Duration::from_millis(6000));
                    shutdown(&app);
                    app.exit(0);
                }
            });
        }
    }

    let handle = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(2));
        let state = handle.state::<DesktopRuntime>();
        if state
            .runtime
            .lock()
            .map(|value| value.exited)
            .unwrap_or(false)
        {
            break;
        }
        if let Ok(mut value) = state.runtime.lock() {
            if let Some((polls, status)) = state.notifications.snapshot() {
                value.notifications_polls = polls;
                value.notifications_status = status;
            }
        }
        write_state(&handle);
    });

    Ok(())
}

// ----------------------------------------------------------------- commands

#[tauri::command]
pub fn get_desktop_settings(runtime: State<'_, DesktopRuntime>) -> DesktopSettings {
    runtime
        .settings
        .lock()
        .map(|value| value.clone())
        .unwrap_or_default()
}

#[tauri::command]
pub fn get_desktop_state(runtime: State<'_, DesktopRuntime>) -> serde_json::Value {
    let settings = runtime
        .settings
        .lock()
        .map(|value| value.clone())
        .unwrap_or_default();
    let state = runtime.runtime.lock().map(|value| {
        serde_json::json!({
            "shortcut_registered": value.shortcut_registered,
            "shortcut_error": value.shortcut_error,
            "notifications_status": value.notifications_status,
            "notification_polls": value.notifications_polls,
            "context_menu_error": value.context_menu_error,
            "backdrop_error": value.backdrop_error,
            "qa_error": value.qa_error,
            "exited": value.exited,
            "frame_stats": value.frame_stats,
        })
    });
    serde_json::json!({
        "settings": settings,
        "state": state.unwrap_or_default(),
        "context_menu_command": context_menu_command(),
        "version": env!("CARGO_PKG_VERSION"),
    })
}

#[tauri::command]
pub fn save_desktop_settings(
    app: AppHandle,
    settings: DesktopSettings,
) -> Result<serde_json::Value, String> {
    let saved = apply_settings(&app, settings)?;
    Ok(settings_view(&saved))
}

#[tauri::command]
pub fn set_palette_height(app: AppHandle, height: f64) {
    animate_palette_height(&app, height);
}

#[tauri::command]
pub fn set_palette_expanded(app: AppHandle, expanded: bool) {
    animate_palette_height(
        &app,
        if expanded {
            560.0
        } else {
            MODERN_COLLAPSED_HEIGHT
        },
    );
}

#[tauri::command]
pub fn hide_main(app: AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
}

#[tauri::command]
pub fn show_main(app: AppHandle) {
    focus_main(&app);
}

#[tauri::command]
pub fn take_pending_analyze(runtime: State<'_, DesktopRuntime>) -> Option<serde_json::Value> {
    runtime
        .pending_analyze
        .lock()
        .ok()
        .and_then(|mut value| value.take())
        .map(|path| analyze_payload(&path))
}

#[tauri::command]
pub fn record_frame_stats(
    runtime: State<'_, DesktopRuntime>,
    fps: f64,
    max_frame_ms: f64,
    frames: u64,
    long_frames: u64,
    mode: String,
) {
    if let Ok(mut state) = runtime.runtime.lock() {
        state.frame_stats = FrameStats {
            fps,
            max_frame_ms,
            frames,
            long_frames,
            mode,
            at: now_epoch(),
        };
    }
}

/// Phase 24: webview console errors/warnings, kept small for the QA report.
#[tauri::command]
pub fn record_console_events(runtime: State<'_, DesktopRuntime>, events: Vec<ConsoleEvent>) {
    if let Ok(mut state) = runtime.runtime.lock() {
        for mut event in events {
            event.at = now_epoch();
            let duplicate = state
                .console_events
                .iter()
                .any(|item| item.level == event.level && item.message == event.message);
            if !duplicate {
                state.console_events.push(event);
            }
        }
        if state.console_events.len() > 40 {
            let drop = state.console_events.len() - 40;
            state.console_events.drain(0..drop);
        }
    }
}

/// Phase 24: layout/contrast audit computed in the webview.
#[tauri::command]
pub fn record_layout_audit(runtime: State<'_, DesktopRuntime>, audit: serde_json::Value) {
    if let Ok(mut state) = runtime.runtime.lock() {
        state.layout_audit = audit;
    }
}

#[tauri::command]
pub fn clear_qa_logs(runtime: State<'_, DesktopRuntime>) {
    if let Ok(mut state) = runtime.runtime.lock() {
        state.console_events.clear();
        state.layout_audit = serde_json::Value::Null;
        state.qa_error = String::new();
    }
}

#[tauri::command]
pub fn quit_app(app: AppHandle) {
    shutdown(&app);
    app.exit(0);
}

// -------------------------------------------------------------------- tests

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_shortcut_is_ctrl_shift_space() {
        let settings = DesktopSettings::default();
        let shortcut = validate_shortcut(&settings.shortcut).expect("default must parse");
        assert_eq!(format_shortcut(&shortcut), "Ctrl+Shift+Space");
    }

    #[test]
    fn rejects_shortcuts_without_modifier_or_key() {
        assert!(validate_shortcut("F9").is_err());
        assert!(validate_shortcut("Ctrl+Shift").is_err());
        assert!(validate_shortcut("").is_err());
        assert!(validate_shortcut("Ctrl+Shift+F9").is_ok());
        assert!(validate_shortcut("Control+Alt+KeyK").is_ok());
    }

    #[test]
    fn accepts_and_canonicalizes_aliases() {
        let shortcut = validate_shortcut("control + shift + space").unwrap();
        assert_eq!(format_shortcut(&shortcut), "Ctrl+Shift+Space");
    }

    #[test]
    fn parses_analyze_argument() {
        let args = vec!["compa.exe".into(), "--analyze".into(), "\"C:\\a b\\c.txt\"".into()];
        assert_eq!(analyze_arg(&args).as_deref(), Some("C:\\a b\\c.txt"));
        assert_eq!(analyze_arg(&["compa.exe".into()]), None);
    }

    #[test]
    fn analyze_payload_checks_the_path() {
        let dir = std::env::temp_dir();
        let file = dir.join(format!("compa-analyze-{}.txt", std::process::id()));
        std::fs::write(&file, "hello").expect("temp file");
        let payload = analyze_payload(&file.to_string_lossy());
        assert_eq!(payload["exists"], true);
        assert!(payload["canonical"].as_str().is_some());
        assert_eq!(
            analyze_payload(r"C:\definitely\missing\compa-file.txt")["exists"],
            false
        );
        let _ = std::fs::remove_file(&file);
    }

    #[test]
    fn parses_qa_command() {
        let args: Vec<String> = ["compa.exe", "--qa", "mode=modern", "notifications=on", "render"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let command = qa_command(&args).expect("qa command");
        assert!(command.render);
        assert!(!command.quit);
        assert_eq!(command.set.len(), 2);

        let mut settings = DesktopSettings::default();
        apply_qa_settings(&mut settings, &command.set).unwrap();
        assert!(settings.is_modern());
        assert!(settings.notifications);

        assert!(qa_command(&["compa.exe".into()]).is_none());
    }

    #[test]
    fn rejects_unknown_qa_setting() {
        let mut settings = DesktopSettings::default();
        let pairs = vec![("nope".to_string(), "1".to_string())];
        assert!(apply_qa_settings(&mut settings, &pairs).is_err());
    }

    #[test]
    fn normalizes_mode_and_backdrop() {
        // Unknown values fall back to the safe classic/solid-free defaults.
        let settings = DesktopSettings {
            mode: "MODERN".into(),
            backdrop: "acrylic".into(),
            ..Default::default()
        }
        .normalized();
        assert_eq!(settings.mode, "classic");
        assert_eq!(settings.backdrop, "mica");
        let settings = DesktopSettings {
            mode: "modern".into(),
            backdrop: "solid".into(),
            ..Default::default()
        }
        .normalized();
        assert!(settings.is_modern());
        assert_eq!(settings.backdrop, "solid");
    }
}
