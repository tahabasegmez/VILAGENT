# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

VILAGENT Frontend is a Next.js 16 web interface for an AI agent system. It communicates with a LangGraph-based backend to provide thread-based AI conversations with streaming responses, artifacts, and a skills/tools system.

**Stack**: Next.js 16, React 19, TypeScript 5.8, Tailwind CSS 4, pnpm 10.26.2

## Commands

| Command          | Purpose                                           |
| ---------------- | ------------------------------------------------- |
| `pnpm dev`       | Dev server with Turbopack (http://localhost:3000) |
| `pnpm build`     | Production build                                  |
| `pnpm check`     | Lint + type check (run before committing)         |
| `pnpm lint`      | ESLint only                                       |
| `pnpm lint:fix`  | ESLint with auto-fix                              |
| `pnpm test`      | Run unit tests with Vitest                        |
| `pnpm test:e2e`  | Run E2E tests with Playwright (Chromium)          |
| `pnpm typecheck` | TypeScript type check (`tsc --noEmit`)            |
| `pnpm start`     | Start production server                           |

Unit tests live under `tests/unit/` and mirror the `src/` layout (e.g., `tests/unit/core/api/stream-mode.test.ts` tests `src/core/api/stream-mode.ts`). Powered by Vitest; import source modules via the `@/` path alias.

E2E tests live under `tests/e2e/` and use Playwright with Chromium. They mock all backend APIs via `page.route()` network interception and test real page interactions (navigation, chat input, streaming responses). Config: `playwright.config.ts`.

## Architecture

```
Frontend (Next.js) ──▶ LangGraph SDK ──▶ LangGraph Backend (lead_agent)
                                              ├── Sub-Agents
                                              └── Tools & Skills
```

The frontend is a stateful chat application. Users create **threads** (conversations), send messages, and receive streamed AI responses. The backend orchestrates agents that can produce **artifacts** (files/code) and **todos**.

### Source Layout (`src/`)

- **`app/`** — Next.js App Router. Routes: `/` (landing), `/workspace/chats/[thread_id]` (chat).
- **`components/`** — React components split into:
  - `ui/` — Shadcn UI primitives (auto-generated, ESLint-ignored)
  - `ai-elements/` — Vercel AI SDK elements (auto-generated, ESLint-ignored)
  - `workspace/` — Chat page components (messages, artifacts, settings)
  - `landing/` — Landing page sections
- **`core/`** — Business logic, the heart of the app:
  - `threads/` — Thread creation, streaming, state management (hooks + types)
  - `api/` — LangGraph client singleton
  - `artifacts/` — Artifact loading and caching
  - `i18n/` — Internationalization (en-US, zh-CN)
  - `settings/` — User preferences in localStorage
  - `memory/` — Persistent user memory system
  - `skills/` — Skills installation and management
  - `messages/` — Message processing and transformation
  - `mcp/` — Model Context Protocol integration
  - `models/` — TypeScript types and data models
- **`hooks/`** — Shared React hooks
- **`lib/`** — Utilities (`cn()` from clsx + tailwind-merge)
- **`server/`** — Server-side code (better-auth, not yet active)
- **`styles/`** — Global CSS with Tailwind v4 `@import` syntax and CSS variables for theming

### Data Flow

1. User input → thread hooks (`core/threads/hooks.ts`) → LangGraph SDK streaming
2. Stream events update thread state (messages, artifacts, todos)
3. TanStack Query manages server state; localStorage stores user settings
4. Components subscribe to thread state and render updates

### Key Patterns

- **Server Components by default**, `"use client"` only for interactive components
- **Thread hooks** (`useThreadStream`, `useSubmitThread`, `useThreads`) are the primary API interface
- **LangGraph client** is a singleton obtained via `getAPIClient()` in `core/api/`
- **Environment validation** uses `@t3-oss/env-nextjs` with Zod schemas (`src/env.js`). Skip with `SKIP_ENV_VALIDATION=1`

### VILAGENT Computer Use Frontend

- VILAGENT is now the default frontend direction. `/` and `/workspace` redirect
  to `/workspace/operator`; the old VILAGENT landing page is temporarily kept at
  `/legacy`.
- Legacy chat/agents/recent-chat navigation is hidden by default. It can be
  temporarily restored with `NEXT_PUBLIC_VILAGENT_SHOW_LEGACY_NAV=true` while
  old flows are being migrated.
- `src/core/computer-use/` contains typed operator API helpers for browser
  health, browser session lifecycle, browser-enriched observe, and browser
  target resolution, safe browser action submission, approval decisions,
  action execute/cancel calls, and lifecycle event polling.
- `src/app/workspace/operator/page.tsx` is the first VILAGENT operator console.
  It is intentionally thin and Electron-ready: it consumes `src/core/computer-use`
  for health, browser session creation, observation, target resolution, safe
  action submission, approvals, execute/cancel, and lifecycle events.
- `src/core/computer-use/operator-runtime.ts` owns the console runtime hook and
  persisted draft context. Reuse this hook for future floating panels or
  Electron-specific renderer entries instead of duplicating owner/session state.
- `src/core/computer-use/operator-timeline.ts` is the frontend adapter for
  planner/source evidence. It turns current runtime state, approvals, actions,
  and lifecycle events into a timeline plus evidence sources; extend this file
  when backend planner, memory, tool, or source events become available.
- `electron/` contains a minimal future desktop shell that opens
  `/workspace/operator` with context isolation enabled. Do not add Electron
  packaging churn until the operator UI stabilizes.
- Use `VILAGENT_MIGRATION.md` when deciding whether to hide, migrate, or remove
  old VILAGENT UI. Prefer hiding/preparing first, then delete once replacement
  coverage exists.
- Use the shared CSRF-aware `@/core/api/fetcher` wrapper for all
  `/api/computer-use/*` calls. Do not call these endpoints with raw
  `globalThis.fetch` from components.

## Code Style

- **Imports**: Enforced ordering (builtin → external → internal → parent → sibling), alphabetized, newlines between groups. Use inline type imports: `import { type Foo }`.
- **Unused variables**: Prefix with `_`.
- **Class names**: Use `cn()` from `@/lib/utils` for conditional Tailwind classes.
- **Path alias**: `@/*` maps to `src/*`.
- **Components**: `ui/` and `ai-elements/` are generated from registries (Shadcn, MagicUI, React Bits, Vercel AI SDK) — don't manually edit these.

## Environment

Backend API URLs are optional; an nginx proxy is used by default:

```
NEXT_PUBLIC_BACKEND_BASE_URL=http://localhost:8001
NEXT_PUBLIC_LANGGRAPH_BASE_URL=http://localhost:8001/api
```

Leave these unset for the standard `make dev` / Docker flow, where nginx serves
the public `/api/langgraph/*` prefix and rewrites it to Gateway's native `/api/*`
routes.

Requires Node.js 22+ and pnpm 10.26.2+.
