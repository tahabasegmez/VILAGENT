const { contextBridge, ipcRenderer } = require("electron");

// Synchronous so the gateway address and token exist before the UI's first request.
const bridge = ipcRenderer.sendSync("vilagent:get-bridge");

contextBridge.exposeInMainWorld("vilagentDesktop", {
  platform: bridge.platform,
  apiBase: bridge.apiBase,
  authToken: bridge.authToken,
  openExternal: (url) => ipcRenderer.invoke("vilagent:open-external", url),
});
