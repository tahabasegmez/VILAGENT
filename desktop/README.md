# VILAGENT desktop app

The Electron shell and the operator UI.

- `electron/main.cjs` starts the Python gateway on a free port, waits for `/health`, then
  loads the UI; it also opens the floating always-on-top panel and stops every child
  process when the window closes.
- `electron/preload.cjs` hands the UI the gateway address and the per-launch auth token.
- `src/` is the UI: a static Next.js export that the gateway itself serves in the
  packaged app (`next dev` serves it while developing).

```bash
corepack pnpm dev      # gateway + next dev + the desktop window
corepack pnpm check    # eslint + tsc
corepack pnpm test     # vitest
corepack pnpm dist     # static UI + frozen gateway + installer in dist-electron/
```

The UI is in `src/components/hud/`: `use-operator` holds the state and
the run/stop actions, `hud.tsx` lays out the floating panels, `live-graph` draws the run's
`trace` events with React Flow (`trace-node`, `flow-edge`), and `mini-hud` is the small
always-on-top window shown while FARA acts. `src/core/computer-use/` is the API client and
`trace.ts` the pure trace reducer and layout (tested in `tests/trace.test.ts`).
