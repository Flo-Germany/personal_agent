use std::fs::OpenOptions;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{
    AppHandle, Manager,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};

const BACKEND_ADDR: &str = "127.0.0.1:8000";
const OLLAMA_ADDR: &str = "127.0.0.1:11434";
const OLLAMA_START_TIMEOUT: Duration = Duration::from_secs(20);
const BACKEND_START_TIMEOUT: Duration = Duration::from_secs(90);

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
        // python.exe can start a worker process, so kill the whole tree
        // rather than only the launcher process.
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

fn log_dir(app: &AppHandle) -> PathBuf {
    app.path()
        .app_log_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("personal-agent"))
}

fn log_launcher(app: &AppHandle, message: &str) {
    let dir = log_dir(app);
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

fn port_open(addr: &str) -> bool {
    let Ok(addr) = addr.parse::<SocketAddr>() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(400)).is_ok()
}

fn backend_healthy() -> bool {
    let Ok(addr) = BACKEND_ADDR.parse::<SocketAddr>() else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(400)) else {
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

/// Folders that may contain the packaged backend and its Python runtime.
///
/// Installed bundles put resources in the install folder; a portable build
/// ships them next to the executable. The dev build has neither.
fn resource_roots(app: &AppHandle) -> Vec<PathBuf> {
    let mut roots = Vec::new();

    if let Ok(dir) = app.path().resource_dir() {
        roots.push(dir);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let parent = parent.to_path_buf();
            if !roots.iter().any(|root| root == &parent) {
                roots.push(parent);
            }
        }
    }

    roots
}

/// The embedded CPython runtime used by the portable/installed backend.
fn bundled_runtime(app: &AppHandle) -> Option<PathBuf> {
    resource_roots(app)
        .into_iter()
        .map(|root| root.join("backend-runtime").join("python.exe"))
        .find(|path| path.is_file())
}

/// The backend sources that accompany [`bundled_runtime`].
fn bundled_backend_source(app: &AppHandle) -> Option<PathBuf> {
    resource_roots(app)
        .into_iter()
        .map(|root| root.join("backend"))
        .find(|path| path.join("main.py").is_file())
}

fn bundled_backend_exe(app: &AppHandle) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    for root in resource_roots(app) {
        candidates.push(root.join("compa-backend.exe"));
        candidates.push(root.join("backend").join("compa-backend.exe"));
    }
    candidates.into_iter().find(|path| path.is_file())
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

/// Per-user folder for config, memory, logs produced by the backend.
fn data_dir(app: &AppHandle) -> PathBuf {
    let base = app
        .path()
        .app_local_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("com.personalagent.desktop"));
    base.join("data")
}

/// Copy the bundled default config into the writable data folder on first run.
///
/// The installed program files are usually read-only, so settings and the
/// personality live in `%LOCALAPPDATA%` where the user can edit them.
fn seed_config(app: &AppHandle, target: &Path) {
    let config_dir = target.join("config");
    if let Err(error) = std::fs::create_dir_all(&config_dir) {
        log_launcher(
            app,
            &format!("Could not create {}: {error}", config_dir.display()),
        );
        return;
    }

    let mut copied = Vec::new();
    for name in ["settings.json", "personality.md"] {
        let destination = config_dir.join(name);
        if destination.exists() {
            continue;
        }
        let source = resource_roots(app)
            .into_iter()
            .map(|root| root.join("config").join(name))
            .find(|path| path.is_file());
        if let Some(source) = source {
            if std::fs::copy(&source, &destination).is_ok() {
                copied.push(name);
            }
        }
    }

    if copied.is_empty() {
        log_launcher(
            app,
            &format!("Config folder ready at {}", config_dir.display()),
        );
    } else {
        log_launcher(
            app,
            &format!("Seeded {} into {}", copied.join(", "), config_dir.display()),
        );
    }
}

/// Start Ollama when it is installed but not running.
///
/// The app is still usable without it: the backend reports a readable error in
/// the chat. Starting it here means a double-click is enough on a normal
/// install.
fn ensure_ollama(app: &AppHandle) {
    if port_open(OLLAMA_ADDR) {
        return;
    }

    let mut candidates = Vec::new();
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        candidates.push(
            PathBuf::from(local)
                .join("Programs")
                .join("Ollama")
                .join("ollama app.exe"),
        );
    }
    if let Some(program_files) = std::env::var_os("ProgramFiles") {
        candidates.push(
            PathBuf::from(program_files)
                .join("Ollama")
                .join("ollama app.exe"),
        );
    }

    for candidate in candidates {
        if !candidate.is_file() {
            continue;
        }
        let mut command = Command::new(&candidate);
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000);
        }
        match command.spawn() {
            Ok(_) => {
                log_launcher(app, &format!("Started Ollama from {}", candidate.display()));
                let deadline = Instant::now() + OLLAMA_START_TIMEOUT;
                while Instant::now() < deadline {
                    if port_open(OLLAMA_ADDR) {
                        log_launcher(app, "Ollama is listening on 127.0.0.1:11434.");
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(300));
                }
                log_launcher(app, "Ollama was started but is not listening yet.");
                return;
            }
            Err(error) => {
                log_launcher(
                    app,
                    &format!("Could not start {}: {error}", candidate.display()),
                );
            }
        }
    }

    log_launcher(
        app,
        "Ollama is not running and was not found; the backend will report it.",
    );
}

fn spawn_backend(app: &AppHandle) -> Option<Child> {
    let logs = log_dir(app);
    let _ = std::fs::create_dir_all(&logs);
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("backend.out.log"))
        .ok()?;
    let stderr = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("backend.err.log"))
        .ok()?;

    let runtime = bundled_runtime(app);
    let source = bundled_backend_source(app);

    let mut command = if let Some(path) = std::env::var_os("PERSONAL_AGENT_BACKEND") {
        let path = PathBuf::from(path);
        let mut cmd = Command::new(&path);
        if let Some(parent) = path.parent() {
            cmd.current_dir(parent);
        }
        cmd
    } else if let (Some(python), Some(backend_dir)) = (runtime, source) {
        log_launcher(
            app,
            &format!(
                "Starting bundled backend: {} in {}",
                python.display(),
                backend_dir.display()
            ),
        );
        let mut cmd = Command::new(python);
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
    } else if let Some(sidecar) = bundled_backend_exe(app) {
        let mut cmd = Command::new(&sidecar);
        if let Some(parent) = sidecar.parent() {
            cmd.current_dir(parent);
        }
        cmd
    } else if let Some(backend_dir) = dev_backend_dir() {
        log_launcher(app, "Starting backend from the development virtualenv.");
        let mut cmd = Command::new(backend_dir.join(".venv").join("Scripts").join("python.exe"));
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

    let data = data_dir(app);
    let _ = std::fs::create_dir_all(&data);
    seed_config(app, &data);

    command
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .env("PERSONAL_AGENT_DATA_DIR", &data)
        // Keep the bundled runtime isolated from a user's global Python setup.
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // Do not create a console window for the backend.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    match command.spawn() {
        Ok(child) => Some(child),
        Err(error) => {
            log_launcher(app, &format!("Could not start the backend: {error}"));
            None
        }
    }
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
    log_launcher(app, "Backend process started; waiting for /health...");

    let deadline = Instant::now() + BACKEND_START_TIMEOUT;
    while Instant::now() < deadline {
        if backend_healthy() {
            log_launcher(app, "Backend is ready.");
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }

    log_launcher(
        app,
        "Backend did not become healthy in time; see backend.err.log.",
    );
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

            // Start Ollama and the backend without a terminal window. The
            // frontend retries /health, so the window can appear immediately.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                ensure_ollama(&handle);
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
