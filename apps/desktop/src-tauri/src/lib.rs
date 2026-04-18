use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use tauri::async_runtime;
use tauri::{AppHandle, DragDropEvent, Emitter, Manager, RunEvent, State, Url, WindowEvent};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Child;

struct RunningSidecar {
    child: Child,
    /// Set to ``true`` by kill paths (return_home, replace-on-drop, app
    /// exit). The stdout recv task checks this before emitting a
    /// "sidecar exited" error, so intentional kills stay silent.
    intentional_kill: Arc<AtomicBool>,
}

#[derive(Default)]
struct SidecarState {
    running: Mutex<Option<RunningSidecar>>,
    current_dataset: Mutex<Option<String>>,
    bootstrap_url: Mutex<Option<Url>>,
}

const SUPPORTED_EXTENSIONS: &[&str] = &[
    "parquet", "geoparquet", "csv", "tsv", "json", "jsonl", "arrow", "feather",
];

#[derive(Serialize, Clone)]
struct SidecarReady {
    url: String,
}

#[derive(Serialize, Clone)]
struct SidecarError {
    message: String,
}

#[derive(Serialize, Clone)]
struct SidecarLog {
    line: String,
}

#[derive(Serialize, Clone, serde::Deserialize)]
struct SidecarProgress {
    stage: String,
    percent: f64,
    #[serde(default)]
    detail: String,
}

// ---------- sidecar launch ----------

fn sidecar_binary_path(app: &AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|e| format!("resource dir: {e}"))?;
    let bin_name = if cfg!(windows) {
        "geospatial-atlas-sidecar.exe"
    } else {
        "geospatial-atlas-sidecar"
    };
    let p = resource_dir.join("sidecar").join(bin_name);
    if !p.is_file() {
        return Err(format!(
            "sidecar binary not found at {} — did you run the python-sidecar build?",
            p.display()
        ));
    }
    Ok(p)
}

fn handle_line(app: &AppHandle, line: String) {
    if line.is_empty() {
        return;
    }
    if let Some(rest) = line.strip_prefix("GSA_PROGRESS ") {
        if let Ok(p) = serde_json::from_str::<SidecarProgress>(rest) {
            let _ = app.emit("sidecar-progress", p);
            return;
        }
    }
    let _ = app.emit("sidecar-log", SidecarLog { line });
}

#[tauri::command]
async fn launch_sidecar(
    app: AppHandle,
    state: State<'_, SidecarState>,
    dataset: String,
    limit: u64,
    text: String,
) -> Result<(), String> {
    kill_existing(&state);
    {
        let mut d = state.current_dataset.lock().unwrap();
        *d = Some(dataset.clone());
    }

    let port = portpicker::pick_unused_port().ok_or_else(|| "no free port".to_string())?;
    let host = "127.0.0.1";
    let url = format!("http://{host}:{port}");

    let sidecar_path = sidecar_binary_path(&app)?;
    let limit_str = limit.to_string();
    // argv[3] = text column name ("" means none).
    let text_arg = text.clone();

    let mut cmd = tokio::process::Command::new(&sidecar_path);
    cmd.arg(&dataset)
        .arg(&limit_str)
        .arg(&text_arg)
        .env("GEOSPATIAL_ATLAS_HOST", host)
        .env("GEOSPATIAL_ATLAS_PORT", port.to_string())
        .env("GEOSPATIAL_ATLAS_PARENT_PID", std::process::id().to_string())
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);

    // Windows: hide the console window that would otherwise flash up.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar at {}: {e}", sidecar_path.display()))?;
    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    let intentional_kill = Arc::new(AtomicBool::new(false));
    {
        let mut guard = state.running.lock().unwrap();
        *guard = Some(RunningSidecar {
            child,
            intentional_kill: intentional_kill.clone(),
        });
    }

    let app_for_logs = app.clone();
    let intentional_for_logs = intentional_kill.clone();
    let url_for_poll = url.clone();
    async_runtime::spawn(async move {
        let mut stdout = BufReader::new(stdout).lines();
        let mut stderr = BufReader::new(stderr).lines();
        loop {
            tokio::select! {
                line = stdout.next_line() => match line {
                    Ok(Some(line)) => handle_line(&app_for_logs, line),
                    Ok(None) | Err(_) => break,
                },
                line = stderr.next_line() => match line {
                    Ok(Some(line)) => handle_line(&app_for_logs, line),
                    Ok(None) | Err(_) => break,
                },
            }
        }
        if !intentional_for_logs.load(Ordering::SeqCst) {
            let _ = app_for_logs.emit(
                "sidecar-error",
                SidecarError {
                    message: "sidecar exited unexpectedly".to_string(),
                },
            );
        }
    });

    let app_for_poll = app.clone();
    async_runtime::spawn(async move {
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_millis(800))
            .build()
        {
            Ok(c) => c,
            Err(e) => {
                let _ = app_for_poll.emit(
                    "sidecar-error",
                    SidecarError {
                        message: format!("http client build failed: {e}"),
                    },
                );
                return;
            }
        };

        let deadline = std::time::Instant::now() + Duration::from_secs(120);
        let health = format!("{url_for_poll}/data/metadata.json");
        loop {
            if std::time::Instant::now() > deadline {
                let _ = app_for_poll.emit(
                    "sidecar-error",
                    SidecarError {
                        message: "timed out waiting for sidecar to become ready".into(),
                    },
                );
                return;
            }
            if let Ok(resp) = client.get(&health).send().await {
                if resp.status().is_success() {
                    let _ =
                        app_for_poll.emit("sidecar-ready", SidecarReady { url: url_for_poll });
                    return;
                }
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
    });

    Ok(())
}

fn take_running(state: &SidecarState) -> Option<RunningSidecar> {
    state.running.lock().unwrap().take()
}

/// Mark the sidecar as intentionally stopped and kill it. Safe to call
/// when nothing is running — in that case it's a no-op.
fn stop_running(state: &SidecarState) {
    if let Some(mut rs) = take_running(state) {
        rs.intentional_kill.store(true, Ordering::SeqCst);
        let _ = rs.child.start_kill();
    }
}

fn kill_existing(state: &State<'_, SidecarState>) {
    stop_running(state.inner());
}

fn kill_on_exit(app: &AppHandle) {
    stop_running(&app.state::<SidecarState>());
}

#[tauri::command]
fn return_home(app: AppHandle) -> Result<(), String> {
    let state = app.state::<SidecarState>();
    stop_running(state.inner());
    {
        let mut d = state.current_dataset.lock().unwrap();
        *d = None;
    }
    let bootstrap = state
        .bootstrap_url
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "bootstrap URL not captured".to_string())?;
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    window
        .navigate(bootstrap)
        .map_err(|e| format!("navigate failed: {e}"))?;
    Ok(())
}

fn is_supported_dataset(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_ascii_lowercase())
        .map(|e| SUPPORTED_EXTENSIONS.iter().any(|x| *x == e))
        .unwrap_or(false)
}

fn handle_dropped_files(app: &AppHandle, paths: Vec<PathBuf>) {
    let Some(first) = paths.into_iter().find(|p| p.is_file() && is_supported_dataset(p)) else {
        let _ = app.emit(
            "sidecar-error",
            SidecarError {
                message: "Dropped file is not a supported dataset (.parquet, .csv, .json, .arrow …)"
                    .to_string(),
            },
        );
        return;
    };
    let dataset = first.to_string_lossy().to_string();

    // First navigate back to bootstrap (so the progress UI is visible
    // immediately, regardless of whether we were on the viewer page).
    // Then launch the new sidecar. Ignore navigation errors — the bootstrap
    // may already be the active page.
    let _ = return_home(app.clone());
    let handle = app.clone();
    async_runtime::spawn(async move {
        let state = handle.state::<SidecarState>();
        if let Err(e) = launch_sidecar(handle.clone(), state, dataset, 0, String::new()).await {
            let _ = handle.emit("sidecar-error", SidecarError { message: e });
        }
    });
}

// ---------- persistent per-dataset viewer state ----------

fn state_file(app: &AppHandle) -> Option<PathBuf> {
    let data_dir = app.path().app_data_dir().ok()?;
    if fs::create_dir_all(&data_dir).is_err() {
        return None;
    }
    Some(data_dir.join("viewer-state.json"))
}

fn load_state_map(app: &AppHandle) -> HashMap<String, String> {
    let Some(path) = state_file(app) else {
        return HashMap::new();
    };
    match fs::read_to_string(&path) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => HashMap::new(),
    }
}

fn save_state_map(app: &AppHandle, map: &HashMap<String, String>) {
    if let Some(path) = state_file(app) {
        if let Ok(data) = serde_json::to_string_pretty(map) {
            let _ = fs::write(&path, data);
        }
    }
}

#[tauri::command]
fn get_current_dataset(state: State<'_, SidecarState>) -> Option<String> {
    state.current_dataset.lock().unwrap().clone()
}

#[tauri::command]
fn load_viewer_state(app: AppHandle, state: State<'_, SidecarState>) -> Option<String> {
    let dataset = state.current_dataset.lock().unwrap().clone()?;
    let map = load_state_map(&app);
    map.get(&dataset).cloned()
}

#[tauri::command]
fn save_viewer_state(app: AppHandle, state: State<'_, SidecarState>, hash: String) {
    let dataset = {
        match state.current_dataset.lock().unwrap().clone() {
            Some(d) => d,
            None => return,
        }
    };
    let mut map = load_state_map(&app);
    let cleaned = hash.trim_start_matches('#').to_string();
    if cleaned.is_empty() {
        map.remove(&dataset);
    } else {
        map.insert(dataset, cleaned);
    }
    save_state_map(&app, &map);
}

// Injected into every page load. Serves three purposes:
//
//   1. Mirrors viewer URL-hash changes to the native state file.
//   2. On the viewer origin only, injects a floating "← New dataset"
//      button that invokes the Rust return_home command.
//   3. Both origins: installs lightweight dragover visual feedback.
//      (The actual drag-drop handler lives in Rust at the window level
//      so it fires even when the viewer page isn't wired for it.)
const STATE_SYNC_SCRIPT: &str = r##"
(function () {
  if (window.__gsaBound) return;
  if (!window.__TAURI__ || !window.__TAURI__.core) {
    setTimeout(arguments.callee, 200);
    return;
  }
  window.__gsaBound = true;
  const invoke = window.__TAURI__.core.invoke;

  // --- 1. state persistence ---
  let pending = null;
  const save = () => {
    if (pending) clearTimeout(pending);
    pending = setTimeout(() => {
      try { invoke("save_viewer_state", { hash: location.hash || "" }); }
      catch (_) {}
    }, 400);
  };
  window.addEventListener("hashchange", save);
  window.addEventListener("popstate", save);
  const origPush = history.pushState;
  history.pushState = function () { origPush.apply(this, arguments); save(); };
  const origReplace = history.replaceState;
  history.replaceState = function () { origReplace.apply(this, arguments); save(); };

  // --- 2. home icon button (viewer pages only) ---
  // Placed inline at the start of the viewer's toolbar (left of the
  // search box) so it doesn't overlap any existing UI elements.
  const isViewer = location.hostname === "127.0.0.1";
  if (isViewer) {
    const HOME_SVG =
      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M3 9.5L12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1V9.5z"/></svg>';

    const makeBtn = () => {
      const b = document.createElement("button");
      b.id = "gsa-home-btn";
      b.type = "button";
      b.setAttribute("aria-label", "Back to dataset picker");
      b.title = "Back to dataset picker";
      b.innerHTML = HOME_SVG;
      b.style.cssText = [
        "display:inline-flex", "align-items:center", "justify-content:center",
        "width:32px", "height:32px", "flex:0 0 auto",
        "border-radius:6px",
        "border:1px solid rgba(100,116,139,0.35)",
        "background:transparent",
        "color:inherit",
        "cursor:pointer",
        "padding:0",
        "margin:0",
      ].join(";");
      b.onmouseenter = () => { b.style.background = "rgba(100,116,139,0.18)"; };
      b.onmouseleave = () => { b.style.background = "transparent"; };
      b.onclick = () => { try { invoke("return_home"); } catch (_) {} };
      return b;
    };

    const tryInject = () => {
      // Find the viewer's toolbar via its search input — a stable anchor
      // regardless of Tailwind class order.
      const input = document.querySelector('input[type="search"]');
      if (!input) return false;
      const leftSide = input.closest(".flex-1");
      const toolbar = (leftSide && leftSide.parentElement) || input.parentElement;
      if (!toolbar) return false;
      if (toolbar.querySelector("#gsa-home-btn")) return true;
      toolbar.insertBefore(makeBtn(), toolbar.firstChild);
      return true;
    };

    // Try immediately, then keep watching the DOM — the toolbar renders
    // after the viewer initialises, and Svelte may re-render it.
    const observer = new MutationObserver(() => { tryInject(); });
    const startObserving = () => {
      if (document.body) observer.observe(document.body, { childList: true, subtree: true });
      else document.addEventListener("DOMContentLoaded", startObserving, { once: true });
    };
    startObserving();
    setTimeout(tryInject, 100);
    setTimeout(tryInject, 500);
  }

  // --- 3. drag-drop visual feedback ---
  // Tauri handles the actual drop at the window level; we only need to
  // show the user that a drop is possible.
  const overlay = document.createElement("div");
  overlay.style.cssText = [
    "position:fixed", "inset:0", "z-index:2147483646",
    "background:rgba(37,99,235,0.18)",
    "border:3px dashed rgba(96,165,250,0.9)",
    "box-sizing:border-box",
    "display:none", "align-items:center", "justify-content:center",
    "font:600 18px -apple-system,BlinkMacSystemFont,sans-serif",
    "color:#dbeafe", "pointer-events:none",
  ].join(";");
  overlay.textContent = "Drop a dataset to open";
  const attachOverlay = () => {
    if (document.body) document.body.appendChild(overlay);
    else document.addEventListener("DOMContentLoaded", attachOverlay, { once: true });
  };
  attachOverlay();

  // Native HTML5 events for per-page feedback (won't suppress Tauri's
  // window-level drop handler because the overlay has pointer-events:none).
  window.addEventListener("dragenter", (e) => { e.preventDefault(); overlay.style.display = "flex"; });
  window.addEventListener("dragover",  (e) => { e.preventDefault(); overlay.style.display = "flex"; });
  window.addEventListener("dragleave", (e) => {
    if (e.clientX <= 0 || e.clientY <= 0 || e.clientX >= window.innerWidth || e.clientY >= window.innerHeight) {
      overlay.style.display = "none";
    }
  });
  window.addEventListener("drop",      (e) => { e.preventDefault(); overlay.style.display = "none"; });

  // Listen for Tauri's own drag-drop events to keep the overlay in sync
  // (they fire even when HTML5 events are suppressed).
  if (window.__TAURI__.event) {
    const on = window.__TAURI__.event.listen;
    on("tauri://drag-enter", () => { overlay.style.display = "flex"; });
    on("tauri://drag-leave", () => { overlay.style.display = "none"; });
    on("tauri://drag-drop",  () => { overlay.style.display = "none"; });
  }
})();
"##;

// ---------- CLI / argv bootstrap ----------

fn initial_dataset_from_argv() -> Option<String> {
    let mut args = std::env::args();
    args.next();
    for a in args {
        if a.starts_with('-') {
            continue;
        }
        if std::path::Path::new(&a).is_file() {
            return Some(a);
        }
    }
    std::env::var("GEOSPATIAL_ATLAS_INITIAL_DATASET").ok()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(SidecarState::default())
        .invoke_handler(tauri::generate_handler![
            launch_sidecar,
            get_current_dataset,
            load_viewer_state,
            save_viewer_state,
            return_home,
        ])
        .on_page_load(|webview, _payload| {
            let _ = webview.eval(STATE_SYNC_SCRIPT);
        })
        .on_window_event(|window, event| {
            let app = window.app_handle().clone();
            match event {
                WindowEvent::CloseRequested { .. } | WindowEvent::Destroyed => {
                    kill_on_exit(&app);
                }
                WindowEvent::DragDrop(DragDropEvent::Drop { paths, .. }) => {
                    let paths = paths.clone();
                    async_runtime::spawn(async move {
                        handle_dropped_files(&app, paths);
                    });
                }
                _ => {}
            }
        })
        .setup(|app| {
            // Capture the initial (bootstrap) webview URL so we can
            // return to it from return_home / drag-drop.
            if let Some(win) = app.get_webview_window("main") {
                if let Ok(url) = win.url() {
                    let state = app.state::<SidecarState>();
                    *state.bootstrap_url.lock().unwrap() = Some(url);
                }
            }
            if let Some(dataset) = initial_dataset_from_argv() {
                let handle = app.handle().clone();
                async_runtime::spawn(async move {
                    let state = handle.state::<SidecarState>();
                    if let Err(e) = launch_sidecar(handle.clone(), state, dataset, 0, String::new()).await {
                        let _ = handle.emit("sidecar-error", SidecarError { message: e });
                    }
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| match event {
            RunEvent::ExitRequested { .. } | RunEvent::Exit => kill_on_exit(app),
            _ => {}
        });
}
