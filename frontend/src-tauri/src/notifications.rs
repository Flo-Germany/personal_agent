//! Smart Notifications: native Windows toast listener with a strict off switch.
//!
//! The listener polls the WinRT `UserNotificationListener` every 10 seconds
//! while enabled. Disabling it joins the thread and releases the WinRT
//! objects, so CPU and memory return to baseline. Unpackaged apps can be
//! refused access by Windows; the status is surfaced instead of retrying in a
//! loop.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use tauri::{AppHandle, Emitter};

use crate::desktop::EVENT_NOTIFICATION;

pub struct NotificationService {
    running: AtomicBool,
    stop: AtomicBool,
    polls: AtomicU64,
    status: Mutex<String>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl Default for NotificationService {
    fn default() -> Self {
        Self::new()
    }
}

impl NotificationService {
    pub fn new() -> Self {
        Self {
            running: AtomicBool::new(false),
            stop: AtomicBool::new(false),
            polls: AtomicU64::new(0),
            status: Mutex::new("disabled".into()),
            thread: Mutex::new(None),
        }
    }

    pub fn snapshot(&self) -> Option<(u64, String)> {
        let status = self.status.lock().ok()?.clone();
        Some((self.polls.load(Ordering::Relaxed), status))
    }

    pub fn start(self: &Arc<Self>, app: AppHandle) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }
        self.stop.store(false, Ordering::SeqCst);
        if let Ok(mut status) = self.status.lock() {
            *status = "starting".into();
        }
        let service = Arc::clone(self);
        let handle = std::thread::spawn(move || {
            service.run(app);
            service.running.store(false, Ordering::SeqCst);
        });
        if let Ok(mut slot) = self.thread.lock() {
            *slot = Some(handle);
        }
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        let handle = self.thread.lock().ok().and_then(|mut slot| slot.take());
        if let Some(handle) = handle {
            let _ = handle.join();
        }
        self.running.store(false, Ordering::SeqCst);
        // A disabled listener must not keep reporting old poll counts.
        self.polls.store(0, Ordering::Relaxed);
        if let Ok(mut status) = self.status.lock() {
            *status = "disabled".into();
        }
    }

    fn set_status(&self, value: &str) {
        if let Ok(mut status) = self.status.lock() {
            *status = value.to_string();
        }
    }

    fn run(&self, app: AppHandle) {
        #[cfg(target_os = "windows")]
        {
            use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};
            use windows::UI::Notifications::Management::{
                UserNotificationListener, UserNotificationListenerAccessStatus,
            };
            use windows::UI::Notifications::{KnownNotificationBindings, NotificationKinds};

            unsafe {
                let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
            }

            let listener = match UserNotificationListener::Current() {
                Ok(listener) => listener,
                Err(error) => {
                    self.set_status(&format!("unavailable: {error}"));
                    return;
                }
            };

            let access = match listener.RequestAccessAsync().and_then(|op| op.get()) {
                Ok(status) => status,
                Err(error) => {
                    self.set_status(&format!("access error: {error}"));
                    return;
                }
            };
            if access != UserNotificationListenerAccessStatus::Allowed {
                self.set_status("denied by Windows (Settings > Privacy > Notifications)");
                return;
            }
            self.set_status("active");

            let mut last_id: u32 = 0;
            let mut primed = false;
            while !self.stop.load(Ordering::SeqCst) {
                match listener
                    .GetNotificationsAsync(NotificationKinds::Toast)
                    .and_then(|op| op.get())
                {
                    Ok(notifications) => {
                        let count = notifications.Size().unwrap_or(0);
                        let mut max_id = last_id;
                        let mut batch = Vec::new();
                        for index in 0..count {
                            let Ok(notification) = notifications.GetAt(index) else {
                                continue;
                            };
                            let Ok(id) = notification.Id() else { continue };
                            max_id = max_id.max(id);
                            if !primed || id <= last_id {
                                continue;
                            }
                            let app_name = notification
                                .AppInfo()
                                .and_then(|info| info.DisplayInfo())
                                .and_then(|display| display.DisplayName())
                                .map(|name| name.to_string_lossy())
                                .unwrap_or_else(|_| "Windows".into());
                            let title = KnownNotificationBindings::ToastGeneric()
                                .ok()
                                .and_then(|binding| {
                                    notification
                                        .Notification()
                                        .and_then(|content| content.Visual())
                                        .and_then(|visual| visual.GetBinding(&binding))
                                        .ok()
                                })
                                .and_then(|binding| binding.GetTextElements().ok())
                                .map(|texts| {
                                    let size = texts.Size().unwrap_or(0);
                                    let mut lines = Vec::new();
                                    for line in 0..size {
                                        if let Ok(item) = texts.GetAt(line) {
                                            if let Ok(text) = item.Text() {
                                                lines.push(text.to_string_lossy());
                                            }
                                        }
                                    }
                                    lines
                                })
                                .unwrap_or_default();
                            batch.push(serde_json::json!({
                                "id": id,
                                "app": app_name,
                                "title": title.first().cloned().unwrap_or_default(),
                                "body": title.iter().skip(1).cloned().collect::<Vec<_>>().join(" "),
                            }));
                        }
                        last_id = max_id;
                        primed = true;
                        if !batch.is_empty() {
                            let _ = app.emit(EVENT_NOTIFICATION, batch);
                        }
                        self.polls.fetch_add(1, Ordering::Relaxed);
                        self.set_status("active");
                    }
                    Err(error) => {
                        self.set_status(&format!("poll error: {error}"));
                        break;
                    }
                }

                for _ in 0..20 {
                    if self.stop.load(Ordering::SeqCst) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(500));
                }
            }
            self.set_status("disabled");
        }

        #[cfg(not(target_os = "windows"))]
        {
            let _ = app;
            self.set_status("unsupported on this platform");
        }
    }
}
