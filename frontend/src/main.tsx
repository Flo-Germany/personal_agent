import "@fontsource-variable/inter";
import { isTauri } from "@tauri-apps/api/core";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./index.css";
import { applyBootAppearance } from "./lib/theme";

// The Tauri window is transparent; keep a solid backdrop for browser previews.
if (!isTauri()) {
  document.documentElement.dataset.environment = "browser";
}

// Apply the cached theme before the first paint, then let the shell confirm it
// from desktop.json once settings are loaded.
applyBootAppearance();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
