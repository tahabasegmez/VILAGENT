# VILAGENT Frontend Migration

VILAGENT is the computer-use-only rebuild of the original VILAGENT app. The
frontend should move toward an Electron-hosted operator surface and away from
general-purpose VILAGENT chat, agent gallery, landing, and deep-research UI.

## Current Defaults

- `/` redirects to `/workspace/operator`.
- `/workspace` redirects to `/workspace/operator`.
- `/workspace/operator` is the primary VILAGENT operator surface.
- The old VILAGENT landing page is temporarily available at `/legacy`.
- Chat, agents, and recent chat navigation are hidden by default.

## Legacy Escape Hatch

Set this only while migrating old flows:

```bash
NEXT_PUBLIC_VILAGENT_SHOW_LEGACY_NAV=true
```

When enabled, the workspace sidebar shows:

- legacy chats;
- legacy agents;
- legacy new chat;
- recent chat history.

## Removal Order

1. Keep legacy routes reachable but hidden while operator/electron workflows
   stabilize.
2. Move any still-useful settings into a VILAGENT operator settings surface.
3. Remove legacy landing, chat, and agents navigation entirely.
4. Remove unused VILAGENT chat/deep-research components, tests, and docs.
5. Add Electron packaging once the operator route is stable enough to be the
   only renderer entrypoint.

Avoid deleting large UI areas until tests and Electron entrypoints no longer
depend on them.
