// VILAGENT desktop app: starts the Python gateway (and `next dev` in development),
// waits until it is healthy, then shows the operator UI. Closing the app stops
// every child process.
const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

const isDev = !app.isPackaged;
const repoRoot = path.resolve(__dirname, "..", "..");
const desktopRoot = path.resolve(__dirname, "..");
const dataDir = isDev ? repoRoot : path.join(app.getPath("appData"), "VILAGENT");
const logsDir = path.join(dataDir, "logs");
const authToken = crypto.randomBytes(24).toString("base64url");
const FLOATING_FRAME = "vilagent-floating";

const children = [];
let mainWindow = null;
let apiBase = "";
let quitting = false;

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
  app.whenReady().then(start);
}

async function start() {
  fs.mkdirSync(logsDir, { recursive: true });
  mainWindow = createMainWindow();
  void mainWindow.loadURL(splashUrl("Starting VILAGENT…"));
  try {
    const backendPort = await freePort();
    apiBase = `http://127.0.0.1:${backendPort}`;
    let uiUrl = `${apiBase}/`;
    if (isDev) {
      const uiPort = await freePort();
      uiUrl = `http://localhost:${uiPort}/`;
      startNextDev(uiPort);
      startBackend(backendPort, { devOrigin: `http://localhost:${uiPort}` });
      await waitForHttp(`${apiBase}/health`, 120_000);
      await waitForHttp(uiUrl, 120_000);
    } else {
      startBackend(backendPort, {});
      await waitForHttp(`${apiBase}/health`, 120_000);
    }
    if (!quitting && mainWindow) await mainWindow.loadURL(uiUrl);
  } catch (error) {
    fail(`VILAGENT could not start: ${error.message}`);
  }
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 980,
    minWidth: 1100,
    minHeight: 720,
    title: "VILAGENT",
    backgroundColor: "#0a0610",
    autoHideMenuBar: true,
    webPreferences: webPreferences(),
  });

  win.webContents.setWindowOpenHandler(({ url, frameName }) => {
    // The console's floating panel opens itself via window.open("", "vilagent-floating")
    // as a real frameless always-on-top window over the desktop.
    if (frameName === FLOATING_FRAME) {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          width: 360,
          height: 300,
          frame: false,
          alwaysOnTop: true,
          resizable: true,
          maximizable: false,
          fullscreenable: false,
          backgroundColor: "#0a0612",
          title: "VILAGENT",
          webPreferences: webPreferences(),
        },
      };
    }
    // Web links open in the default browser; anything else is dropped (non-http
    // targets make Windows offer the Microsoft Store).
    if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("did-create-window", (child, details) => {
    // Semi-transparent so the desktop shows through the floating panel.
    if (details.frameName === FLOATING_FRAME) child.setOpacity(0.75);
  });
  win.on("closed", () => {
    mainWindow = null;
  });
  return win;
}

function webPreferences() {
  return {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    preload: path.join(__dirname, "preload.cjs"),
  };
}

// ── child processes ──────────────────────────────────────────────────────────

function startBackend(port, { devOrigin }) {
  const args = ["--host", "127.0.0.1", "--port", String(port), "--data-dir", dataDir];
  const env = { ...process.env, VILAGENT_INTERNAL_AUTH_TOKEN: authToken, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" };
  let command;
  let cwd;
  if (isDev) {
    command = pythonExecutable();
    cwd = path.join(repoRoot, "agent");
    args.unshift("-m", "vilagent");
    args.push("--reload", "--dev-origin", devOrigin);
    // Conda environments need their Scripts/ and Library/bin on PATH for native DLLs.
    const pythonDir = path.dirname(command);
    if (path.isAbsolute(command)) {
      env.PATH = [pythonDir, path.join(pythonDir, "Scripts"), path.join(pythonDir, "Library", "bin"), env.PATH].join(path.delimiter);
    }
    env.PYTHONNOUSERSITE = "1";
    delete env.PYTHONHOME;
  } else {
    command = path.join(process.resourcesPath, "server", "vilagent-server.exe");
    cwd = path.dirname(command);
    args.push("--static-dir", path.join(process.resourcesPath, "ui"));
  }
  track("gateway", spawn(command, args, { cwd, env, windowsHide: true }), "gateway.log");
}

function startNextDev(port) {
  const nextBin = require.resolve("next/dist/bin/next", { paths: [desktopRoot] });
  // Electron's own binary runs Node scripts when ELECTRON_RUN_AS_NODE is set.
  const env = { ...process.env, ELECTRON_RUN_AS_NODE: "1" };
  track("next dev", spawn(process.execPath, [nextBin, "dev", "--turbo", "-p", String(port)], { cwd: desktopRoot, env, windowsHide: true }), "ui.log");
}

function track(name, child, logName) {
  const log = fs.createWriteStream(path.join(logsDir, logName), { flags: "a" });
  log.write(`\n--- ${new Date().toISOString()} starting ${name} (pid ${child.pid}) ---\n`);
  child.stdout?.pipe(log, { end: false });
  child.stderr?.pipe(log, { end: false });
  children.push(child);
  child.on("error", (error) => fail(`Could not start ${name}: ${error.message}`));
  child.on("exit", (code) => {
    log.write(`--- ${name} exited with code ${code} ---\n`);
    if (!quitting) fail(`${name} stopped unexpectedly (exit code ${code}).`);
  });
}

function pythonExecutable() {
  return process.env.VILAGENT_PYTHON || readDotEnv(path.join(repoRoot, ".env")).VILAGENT_PYTHON || "python";
}

function readDotEnv(file) {
  const values = {};
  if (!fs.existsSync(file)) return values;
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const match = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (match) values[match[1]] = match[2].replace(/\s+#.*$/, "").replace(/^(['"])(.*)\1$/, "$2");
  }
  return values;
}

function stopChildren() {
  for (const child of children) {
    if (child.exitCode === null && child.pid) {
      // /T also ends grandchildren (uvicorn reloader worker, the Windows host process).
      spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
    }
  }
}

// ── helpers ──────────────────────────────────────────────────────────────────

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (quitting) return reject(new Error("quitting"));
      const req = http.get(url, (res) => {
        res.resume();
        if (res.statusCode && res.statusCode < 500) resolve();
        else retry();
      });
      req.on("error", retry);
      req.setTimeout(2000, () => req.destroy());
    };
    const retry = () => {
      if (Date.now() > deadline) reject(new Error(`timed out waiting for ${url}`));
      else setTimeout(attempt, 300);
    };
    attempt();
  });
}

function fail(message) {
  if (quitting) return;
  quitting = true;
  stopChildren();
  dialog.showErrorBox("VILAGENT", `${message}\n\nLogs: ${logsDir}`);
  app.quit();
}

function splashUrl(text) {
  const html = `<body style="margin:0;height:100vh;display:grid;place-items:center;background:#0a0610;color:#e9d5ff;font:600 15px system-ui">${text}</body>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

// ── IPC for the preload bridge ───────────────────────────────────────────────

ipcMain.on("vilagent:get-bridge", (event) => {
  event.returnValue = { apiBase, authToken, platform: process.platform };
});

ipcMain.handle("vilagent:open-external", (_event, url) => {
  if (typeof url !== "string" || !/^https?:\/\//.test(url)) return { opened: false };
  void shell.openExternal(url);
  return { opened: true };
});

app.on("before-quit", () => {
  quitting = true;
  stopChildren();
});

app.on("window-all-closed", () => {
  app.quit();
});
