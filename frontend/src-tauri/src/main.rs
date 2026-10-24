// Keep the app window-only in debug and release builds: no terminal window.
#![cfg_attr(windows, windows_subsystem = "windows")]

fn main() {
    personal_agent_lib::run()
}
