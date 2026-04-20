// Electron main process — Tauri-shell replacement.
//
// Feature parity with the original apps/desktop/src-tauri/src/lib.rs:
// spawns the PyInstaller sidecar, polls for readiness, owns persistent
// per-dataset viewer state, handles drag-drop of datasets anywhere on
// the window, injects the "back to picker" home button on viewer
// pages, and persists the MCP on/off toggle.

import { app, BrowserWindow, dialog, ipcMain, shell, type IpcMainInvokeEvent } from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createServer } from "node:net";
import { pathToFileURL } from "node:url";

// ---------- constants ----------

const SUPPORTED_EXTENSIONS = [
  "parquet", "geoparquet", "csv", "tsv", "json", "jsonl", "arrow", "feather",
];

const isDev = !app.isPackaged;
const SIDECAR_READY_TIMEOUT_MS = 120_000;
const SIDECAR_HEALTH_POLL_MS = 300;

// ---------- sidecar state (module-level, single window) ----------

interface RunningSidecar {
  child: ChildProcess;
  intentionalKill: { value: boolean };
}

const state = {
  running: null as RunningSidecar | null,
  currentDataset: null as string | null,
  bootstrapUrl: null as string | null,
  mcpEnabled: true,
  mcpUrl: "",
};

let mainWindow: BrowserWindow | null = null;

// ---------- helpers ----------

function pickFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      if (typeof addr === "object" && addr) {
        const port = addr.port;
        srv.close(() => resolve(port));
      } else {
        reject(new Error("no address from listener"));
      }
    });
  });
}

function sidecarBinaryPath(): string {
  // Packaged: <resources>/sidecar/<bin>. Dev: apps/desktop/resources/sidecar/<bin>.
  const binName =
    process.platform === "win32"
      ? "geospatial-atlas-sidecar.exe"
      : "geospatial-atlas-sidecar";
  if (app.isPackaged) {
    return join(process.resourcesPath, "sidecar", binName);
  }
  return join(__dirname, "..", "..", "resources", "sidecar", binName);
}

function bootstrapUrl(): string {
  if (isDev) {
    return "http://127.0.0.1:1420";
  }
  // Packaged layout (inside app.asar):
  //   /electron/dist/main.js  ← __dirname
  //   /dist/index.html
  // So go up two levels: electron/dist → electron → app.asar, then dist/.
  return pathToFileURL(join(__dirname, "..", "..", "dist", "index.html")).toString();
}

function emit(channel: string, payload: unknown) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(`evt:${channel}`, payload);
  }
}

function isSupportedDataset(path: string): boolean {
  const lower = path.toLowerCase();
  return SUPPORTED_EXTENSIONS.some((ext) => lower.endsWith("." + ext));
}

// ---------- state file (per-dataset viewer state) ----------

function stateFilePath(): string {
  const dir = app.getPath("userData");
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  return join(dir, "viewer-state.json");
}

function loadStateMap(): Record<string, string> {
  try {
    const data = readFileSync(stateFilePath(), "utf8");
    return JSON.parse(data) as Record<string, string>;
  } catch {
    return {};
  }
}

function saveStateMap(map: Record<string, string>) {
  try {
    writeFileSync(stateFilePath(), JSON.stringify(map, null, 2), "utf8");
  } catch {
    // best-effort
  }
}

// ---------- sidecar lifecycle ----------

function killRunning() {
  const rs = state.running;
  state.running = null;
  if (rs) {
    rs.intentionalKill.value = true;
    try {
      rs.child.kill();
    } catch {
      // process may already be dead
    }
  }
  state.mcpUrl = "";
}

function handleSidecarLine(line: string) {
  if (!line) return;
  if (line.startsWith("GSA_PROGRESS ")) {
    try {
      const progress = JSON.parse(line.slice("GSA_PROGRESS ".length));
      emit("sidecar-progress", progress);
      return;
    } catch {
      // fall through to log
    }
  }
  emit("sidecar-log", { line });
}

function attachStreamReader(stream: NodeJS.ReadableStream) {
  let buf = "";
  stream.on("data", (chunk: Buffer | string) => {
    buf += chunk.toString("utf8");
    let nl = buf.indexOf("\n");
    while (nl >= 0) {
      const line = buf.slice(0, nl).replace(/\r$/, "");
      buf = buf.slice(nl + 1);
      handleSidecarLine(line);
      nl = buf.indexOf("\n");
    }
  });
  stream.on("end", () => {
    if (buf) handleSidecarLine(buf);
  });
}

async function waitForSidecarReady(url: string): Promise<void> {
  const deadline = Date.now() + SIDECAR_READY_TIMEOUT_MS;
  const health = `${url}/data/metadata.json`;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(health, { signal: AbortSignal.timeout(800) });
      if (res.ok) return;
    } catch {
      // keep polling
    }
    await new Promise((r) => setTimeout(r, SIDECAR_HEALTH_POLL_MS));
  }
  throw new Error("timed out waiting for sidecar to become ready");
}

async function launchSidecar(dataset: string, limit: number, text: string): Promise<void> {
  killRunning();
  state.currentDataset = dataset;

  const port = await pickFreePort();
  const host = "127.0.0.1";
  const url = `http://${host}:${port}`;
  const binPath = sidecarBinaryPath();

  if (!existsSync(binPath)) {
    throw new Error(
      `sidecar binary not found at ${binPath} — did you run the python-sidecar build?`
    );
  }

  const mcpOn = state.mcpEnabled;
  const child = spawn(
    binPath,
    [dataset, String(limit), text],
    {
      env: {
        ...process.env,
        GEOSPATIAL_ATLAS_HOST: host,
        GEOSPATIAL_ATLAS_PORT: String(port),
        GEOSPATIAL_ATLAS_PARENT_PID: String(process.pid),
        GEOSPATIAL_ATLAS_MCP: mcpOn ? "1" : "0",
      },
      stdio: ["pipe", "pipe", "pipe"],
      // Windows: detach so the console window doesn't flash.
      windowsHide: true,
    }
  );

  const intentionalKill = { value: false };
  state.running = { child, intentionalKill };

  if (child.stdout) attachStreamReader(child.stdout);
  if (child.stderr) attachStreamReader(child.stderr);

  child.on("exit", () => {
    if (!intentionalKill.value) {
      emit("sidecar-error", { message: "sidecar exited unexpectedly" });
    }
  });
  child.on("error", (err) => {
    emit("sidecar-error", { message: `failed to spawn sidecar: ${err.message}` });
  });

  // Poll for readiness off the main thread.
  void waitForSidecarReady(url).then(
    () => {
      const mcpUrl = mcpOn ? `${url}/mcp` : "";
      state.mcpUrl = mcpUrl;
      emit("sidecar-ready", { url, mcp_url: mcpUrl });
    },
    (err) => {
      emit("sidecar-error", { message: String(err.message ?? err) });
    }
  );
}

function returnHome() {
  killRunning();
  state.currentDataset = null;
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const target = state.bootstrapUrl ?? bootstrapUrl();
  void mainWindow.loadURL(target);
}

// ---------- dropped file handling ----------

async function handleDroppedFile(path: string) {
  if (!isSupportedDataset(path)) {
    emit("sidecar-error", {
      message:
        "Dropped file is not a supported dataset (.parquet, .csv, .json, .arrow …)",
    });
    return;
  }
  // Match the Tauri flow: navigate back to bootstrap so the progress UI is
  // visible immediately, then launch the new sidecar.
  returnHome();
  try {
    await launchSidecar(path, 0, "");
  } catch (e) {
    emit("sidecar-error", { message: String((e as Error).message ?? e) });
  }
}

// ---------- IPC handlers (Tauri-command equivalents) ----------

function registerIpcHandlers() {
  ipcMain.handle(
    "cmd:launch_sidecar",
    async (
      _e: IpcMainInvokeEvent,
      args: { dataset: string; limit: number; text: string }
    ) => {
      await launchSidecar(args.dataset, args.limit, args.text);
    }
  );

  ipcMain.handle("cmd:return_home", () => returnHome());

  ipcMain.handle("cmd:get_current_dataset", () => state.currentDataset);

  ipcMain.handle("cmd:load_viewer_state", () => {
    if (!state.currentDataset) return null;
    const map = loadStateMap();
    return map[state.currentDataset] ?? null;
  });

  ipcMain.handle(
    "cmd:save_viewer_state",
    (_e: IpcMainInvokeEvent, args: { hash: string }) => {
      if (!state.currentDataset) return;
      const map = loadStateMap();
      const cleaned = (args.hash ?? "").replace(/^#/, "");
      if (cleaned === "") {
        delete map[state.currentDataset];
      } else {
        map[state.currentDataset] = cleaned;
      }
      saveStateMap(map);
    }
  );

  ipcMain.handle("cmd:get_mcp_enabled", () => state.mcpEnabled);
  ipcMain.handle(
    "cmd:set_mcp_enabled",
    (_e: IpcMainInvokeEvent, args: { enabled: boolean }) => {
      state.mcpEnabled = !!args.enabled;
      return state.mcpEnabled;
    }
  );
  ipcMain.handle("cmd:get_mcp_url", () => state.mcpUrl);

  // File-picker dialog (Tauri plugin-dialog equivalent).
  ipcMain.handle(
    "dialog:open",
    async (
      _e: IpcMainInvokeEvent,
      options: {
        multiple?: boolean;
        directory?: boolean;
        filters?: Array<{ name: string; extensions: string[] }>;
      }
    ) => {
      if (!mainWindow) return null;
      const result = await dialog.showOpenDialog(mainWindow, {
        properties: [
          options.directory ? "openDirectory" : "openFile",
          ...(options.multiple ? (["multiSelections"] as const) : []),
        ],
        filters: options.filters,
      });
      if (result.canceled || result.filePaths.length === 0) return null;
      return options.multiple ? result.filePaths : result.filePaths[0];
    }
  );

  // Called from the renderer when a file is dropped on the window.
  ipcMain.handle(
    "cmd:dropped_file",
    async (_e: IpcMainInvokeEvent, args: { path: string }) => {
      await handleDroppedFile(args.path);
    }
  );
}

// ---------- CLI / argv bootstrap ----------

function initialDatasetFromArgv(): string | null {
  // Packaged apps get the dataset path as a positional arg (if any).
  // argv[0] is the app binary; argv[1..] may include flags or a file.
  const args = app.isPackaged ? process.argv.slice(1) : process.argv.slice(2);
  for (const a of args) {
    if (a.startsWith("-")) continue;
    if (existsSync(a)) return a;
  }
  return process.env["GEOSPATIAL_ATLAS_INITIAL_DATASET"] ?? null;
}

// ---------- window + app lifecycle ----------

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: "Geospatial Atlas",
    backgroundColor: "#111418",
    webPreferences: {
      preload: join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // WebGPU is on by default in current Electron/Chromium — no flag needed.
    },
  });

  state.bootstrapUrl = bootstrapUrl();
  void mainWindow.loadURL(state.bootstrapUrl);

  // Open external http(s) links in the user's default browser, not in-app.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://127.0.0.1") || url.startsWith("file://")) {
      return { action: "allow" };
    }
    void shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
    killRunning();
  });
}

// Pin the userData directory to the bundle identifier so per-dataset
// state written by the old Tauri build ({appData}/io.github.do-me.geospatial-atlas/
// viewer-state.json) carries over transparently. Without this override
// Electron would default to the package.json `name` ("geospatial-atlas-desktop")
// and the migration from Tauri would silently drop every saved viewport.
const BUNDLE_ID = "io.github.do-me.geospatial-atlas";
app.setPath("userData", join(app.getPath("appData"), BUNDLE_ID));

// Hardening + perf flags applied before app.whenReady.
app.commandLine.appendSwitch("enable-features", "UseOzonePlatform");
// Force the discrete GPU on multi-GPU macOS systems (big perf win for
// WebGPU/WebGL scatter rendering on MBPs with integrated + discrete).
app.commandLine.appendSwitch("force-high-performance-gpu");

// Single-instance: subsequent launches focus the existing window and
// forward the dataset arg.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
      const dataset = argv.slice(1).find((a) => !a.startsWith("-") && existsSync(a));
      if (dataset) void handleDroppedFile(dataset);
    }
  });

  app.whenReady().then(() => {
    registerIpcHandlers();
    createWindow();

    const initial = initialDatasetFromArgv();
    if (initial) {
      // Defer so the window is ready to receive events.
      setTimeout(() => {
        void launchSidecar(initial, 0, "").catch((e) => {
          emit("sidecar-error", { message: String((e as Error).message ?? e) });
        });
      }, 300);
    }

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    killRunning();
    if (process.platform !== "darwin") app.quit();
  });

  app.on("before-quit", () => {
    killRunning();
  });
}
