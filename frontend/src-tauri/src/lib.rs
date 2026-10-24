use std::fs::OpenOptions;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{
    AppHandle, Manager,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};

const BACKEND_ADDR: &str = "127.0.0.1:8000";

/// Owns the backend process if this app instance started it.
struct BackendGuard(Mutex<Option<Child>>);

impl BackendGuard {
    fn stop(&self) {
        if let Ok(mut slot) = self.0.lock() {
            if let Some(mut child) = slot.take() {
                stop_child(&mut child);
            }
        }
    }
}

/// Stop the backend and every child process it created.

fn stop_child(child: &mut Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        // python.exe in the venv can start a python3.13.exe worker, so kill
        // the whole tree rather than only the launcher process.
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }

    let _ = child.kill();
    let _ = child.wait();
}

impl Drop for BackendGuard {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Show the chat window when hidden, hide it when visible.
fn toggle_main_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };

    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn log_launcher(app: &AppHandle, message: &str) {
    let dir = app
        .path()
        .app_log_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("personal-agent"));
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("launcher.log"))
    {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "{stamp} {message}");
    }
}

fn backend_healthy() -> bool {
    let addr: SocketAddr = match BACKEND_ADDR.parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(400))
    else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    if stream
        .write_all(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }

    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);
    response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")
}

fn dev_backend_dir() -> Option<PathBuf> {
    // In development the Tauri manifest lives at <repo>/frontend/src-tauri,
    // so <repo>/backend is two levels up.
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let candidate = manifest.join("..").join("..").join("backend");
    if candidate
        .join(".venv")
        .join("Scripts")
        .join("python.exe")
        .is_file()
    {
        return Some(candidate);
    }
    None
}

fn bundled_backend(app: &AppHandle) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join("compa-backend.exe"));
        candidates.push(dir.join("backend").join("compa-backend.exe"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            candidates.push(parent.join("compa-backend.exe"));
        }
    }
    candidates.into_iter().find(|path| path.is_file())
}

fn spawn_backend(app: &AppHandle) -> Option<Child> {
    let log_dir = app
        .path()
        .app_log_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("personal-agent"));
    let _ = std::fs::create_dir_all(&log_dir);
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("backend.out.log"))
        .ok()?;
    let stderr = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("backend.err.log"))
        .ok()?;

    let mut command = if let Some(path) = std::env::var_os("PERSONAL_AGENT_BACKEND") {
        let path = PathBuf::from(path);
        let mut cmd = Command::new(&path);
        if let Some(parent) = path.parent() {
            cmd.current_dir(parent);
        }
        cmd
    } else if let Some(sidecar) = bundled_backend(app) {
        let mut cmd = Command::new(&sidecar);
        if let Some(parent) = sidecar.parent() {
            cmd.current_dir(parent);
        }
        cmd
    } else if let Some(backend_dir) = dev_backend_dir() {
        let mut cmd = Command::new(
            backend_dir
                .join(".venv")
                .join("Scripts")
                .join("python.exe"),
        );
        cmd.args([
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ]);
        cmd.current_dir(&backend_dir);
        cmd
    } else {
        return None;
    };

    command
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // Do not create a console window for the backend.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    command.spawn().ok()
}

fn ensure_backend(app: &AppHandle) -> bool {
    if backend_healthy() {
        log_launcher(app, "Backend is already running; reusing it.");
        return false;
    }

    log_launcher(app, "Starting the backend...");
    let Some(child) = spawn_backend(app) else {
        log_launcher(app, "Could not find or start the backend.");
        return false;
    };

    if let Some(guard) = app.try_state::<BackendGuard>() {
        if let Ok(mut slot) = guard.0.lock() {
            *slot = Some(child);
        }
    }
    log_launcher(app, "Backend process started.");
    true
}

fn create_tray(app: &tauri::App) -> tauri::Result<()> {
    let toggle_item = MenuItem::with_id(app, "toggle", "Show / Hide", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&toggle_item, &quit_item])?;

    let icon = app
        .default_window_icon()
        .expect("default window icon is missing")
        .clone();

    TrayIconBuilder::new()
        .icon(icon)
        .tooltip("Personal Agent")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "toggle" => toggle_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            create_tray(app)?;
            app.manage(BackendGuard(Mutex::new(None)));

            // Start the backend without a terminal window. The frontend waits
            // for /health before loading, so the window can appear immediately.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                ensure_backend(&handle);
            });

            // The window is created hidden to avoid a white flash.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the chat window keeps the app available in the tray.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(guard) = app_handle.try_state::<BackendGuard>() {
                guard.stop();
            }
        }
    });
}
