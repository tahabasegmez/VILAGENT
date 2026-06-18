const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("node:path");

const DEFAULT_OPERATOR_URL = "http://localhost:3000/operator";

let mainWindow = null;

function operatorUrl() {
  return process.env.VILAGENT_OPERATOR_URL ?? DEFAULT_OPERATOR_URL;
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 980,
    minWidth: 1100,
    minHeight: 720,
    title: "VILAGENT Operator",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url, frameName }) => {
    // The operator console's floating panel opens itself via
    // window.open("", "vilagent-floating", …). Allow it through as a real, separate,
    // frameless always-on-top window that floats over the Windows desktop.
    if (frameName === "vilagent-floating") {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          width: 360,
          height: 520,
          frame: false,
          alwaysOnTop: true,
          resizable: true,
          maximizable: false,
          fullscreenable: false,
          backgroundColor: "#0c0712",
          title: "VILAGENT",
          webPreferences: {
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
            preload: path.join(__dirname, "preload.cjs"),
          },
        },
      };
    }
    // Real web links open in the user's default browser; anything else is dropped
    // (never hand non-http targets to the shell — that pops the Microsoft Store).
    if (/^https?:\/\//.test(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });

  // The floating panel renders semi-transparent so the desktop shows through it.
  mainWindow.webContents.on("did-create-window", (childWindow, details) => {
    if (details.frameName === "vilagent-floating") {
      childWindow.setOpacity(0.75);
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  void mainWindow.loadURL(operatorUrl());
}

ipcMain.handle("vilagent:get-desktop-info", () => ({
  platform: process.platform,
  appVersion: app.getVersion(),
}));

ipcMain.handle("vilagent:open-external", (_event, url) => {
  if (typeof url !== "string" || !/^https?:\/\//.test(url)) {
    return { opened: false };
  }
  void shell.openExternal(url);
  return { opened: true };
});

function bootstrap() {
  createMainWindow();
}

app.whenReady().then(() => {
  bootstrap();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      bootstrap();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
