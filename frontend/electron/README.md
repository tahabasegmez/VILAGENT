# VILAGENT Electron Shell

This is the minimal desktop host for the VILAGENT operator UI.

Current behavior:

- opens the VILAGENT Next.js operator route at `/operator`;
- keeps `contextIsolation` enabled;
- disables Node integration in the renderer;
- routes new-window links to the system browser;
- exposes only a tiny `window.vilagentDesktop` marker from preload.

The Electron dependency and packaging scripts are intentionally not added yet,
so the existing lockfile stays stable while the operator UI is still forming.
When packaging begins, wire `electron/main.cjs` as the app entrypoint and set
`VILAGENT_OPERATOR_URL` when the UI is served from a non-default URL.
