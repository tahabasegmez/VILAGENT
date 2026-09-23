/** What the Electron preload exposes to the operator UI (see electron/preload.cjs). */
export type VilagentDesktop = {
  platform: string;
  /** Gateway origin, e.g. http://127.0.0.1:52100 ("" when the UI is served by the gateway). */
  apiBase: string;
  /** Per-launch token the gateway requires on every API call. */
  authToken: string;
};

declare global {
  interface Window {
    vilagentDesktop?: VilagentDesktop;
  }
}

export function getDesktop(): VilagentDesktop | undefined {
  return typeof window === "undefined" ? undefined : window.vilagentDesktop;
}
