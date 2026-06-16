const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("vilagentDesktop", {
  platform: process.platform,
  shell: "electron",
  getDesktopInfo: () => ipcRenderer.invoke("vilagent:get-desktop-info"),
  openExternal: (url) => ipcRenderer.invoke("vilagent:open-external", url),
});
