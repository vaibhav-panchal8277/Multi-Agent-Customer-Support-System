# MESH Support — frontend

React 19 + Tailwind single-page app for the MESH multi-agent support platform:
the customer chat (`/`, `/c/:sid`) and the admin trace dashboard (`/admin`).
See the [project README](../README.md) for the full setup and the backend.

## Configuration

Create `frontend/.env` (git-ignored):

```bash
REACT_APP_BACKEND_URL=http://localhost:8001
# Optional, only if the backend sets API_TOKEN. It ends up in the public bundle,
# so it is not a secret:
# REACT_APP_API_TOKEN=...
```

Never put `ADMIN_TOKEN` here. The admin dashboard asks for it at runtime and keeps
it in `sessionStorage` for that tab only.

## Scripts (use Yarn, not npm)

| Command | What it does |
|---------|--------------|
| `yarn install` | Install dependencies from `yarn.lock` |
| `yarn start` | Dev server on http://localhost:3000 |
| `CI=true yarn test --watchAll=false` | Jest tests (`src/**/*.test.js(x)`) |
| `yarn build` | Production build in `build/`; set `REACT_APP_BACKEND_URL` at build time |

## Layout

```
src/
├── App.js                      routes
├── lib/api.js                  API client: per-session keys (localStorage), admin token
├── pages/ChatPage.jsx          chat, verify account, refund confirmation, resolve/reopen
├── pages/AdminDashboard.jsx    admin login + sessions / trace timeline
├── components/                 AgentBadge, MessageCard, TraceTimeline
├── constants/testIds/          data-testid registry used by end-to-end UI tests
└── lib/utils.js                `cn()` helper for shadcn/ui (components.json)
```

`craco.config.js` adds the `@/` → `src/` alias (webpack and Jest) and an optional
dev-server health check (`ENABLE_HEALTH_CHECK=true`).
