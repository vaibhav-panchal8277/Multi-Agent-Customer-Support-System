# Security Notes — MESH Support

## ⚠️ Action required: rotate the exposed API keys (issue C2)

During code review, **real credentials were found in plaintext** in `backend/.env`:

- `GROQ_API_KEY` (Groq API key)
- `LANGFUSE_SECRET_KEY` and `LANGFUSE_PUBLIC_KEY`

Verified good news: `backend/.env` was **never committed** — it is absent from the
entire git history, the key values appear nowhere in any commit, and `.env` is
correctly listed in `.gitignore`. The exposure is limited to the plaintext file
on disk. Because the README encourages zipping/sharing the project folder, treat
these keys as **potentially compromised** and rotate them:

1. **Groq API key** — Groq console (https://console.groq.com) → *API Keys* →
   revoke/regenerate. Update `GROQ_API_KEY` in your local `backend/.env` only.
2. **Langfuse keys** — Langfuse project → *Settings → API Keys* → delete the exposed
   pair and create a new one. Update `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY`.
3. After rotating, confirm the app still starts and `GET /api/langfuse/status`
   reports `auth_ok: true`.

## Secret-hygiene rules for this repo

- **Never commit a real `.env`.** Only `env.example` (placeholders) is tracked.
  `.gitignore` already excludes `.env`, `*.env`, `.env.*`, `*.key`, `credentials.json`.
- **Verify before sharing/zipping:** `git check-ignore backend/.env frontend/.env`
  should print both paths. To scan a folder before zipping:
  `grep -rIl "gsk_\|sk-lf-" . --exclude-dir=.git` should return nothing but
  `env.example`-style templates you intentionally sanitized.
- **In deployment**, inject secrets via the platform's secret manager
  (Render/Railway/Fly/ECS env vars), not a committed file.

## Access model

| Layer | Header | Protects | Notes |
|-------|--------|----------|-------|
| `API_TOKEN` (optional) | `Authorization: Bearer …` | casual traffic | Built into the public JS bundle via `REACT_APP_API_TOKEN`, so it is **not a secret**. |
| Session key | `X-Session-Key` | one conversation: read, chat, verify, confirm refunds, resolve, delete | Returned once by `POST /api/sessions`; the server stores only its SHA-256 hash. The browser keeps it in `localStorage`. |
| Customer identity | (bound to the session) | invoices, refunds | Set only by `POST /api/sessions/{id}/verify` (email + PIN, 5 attempts per session) or by an admin. Without it, invoice/refund tools refuse to run (**fail closed**). |
| `ADMIN_TOKEN` | `X-Admin-Token` | list all sessions, traces, `/langfuse/status`, `/guardrails/status` | Server-side only; typed into `/admin` at runtime and kept in `sessionStorage`. **Never** put it in a `REACT_APP_*` variable. Unset ⇒ admin endpoints return 403. |

**Refunds** are two-step: the Refunds agent can only call `request_refund`, which
proposes a refund. Money moves only when the customer clicks *Confirm refund*
(`POST /api/sessions/{id}/refunds/{confirmation_id}/confirm`), which re-checks
ownership, eligibility and idempotency server-side. Proposals expire after 15 minutes.

The email + PIN directory (`MOCK_CUSTOMERS` in `backend/tools.py`) is a demo
stand-in. Replace it with your real identity provider before using real data.

## Guardrails

- **Fail closed by default.** If a rail can't run after retrying transient Groq
  errors (429/5xx/timeouts), the message or reply is treated as blocked and the
  customer gets a "temporarily unavailable" reply. `GUARDRAILS_FAIL_MODE=open`
  restores allow-on-error for local development only.
- **Blocked turns never reach the agents.** Messages rejected by the input or
  output rail are stored with `blocked: true`, still shown in the UI, but excluded
  from the history the agents see. Without this, a rejected prompt injection would
  be replayed to the agents on the next harmless turn.

## Other hardening

- **CORS (L2):** wildcard origins are no longer combined with credentials; set
  `CORS_ORIGINS` to your exact frontend origin(s) in production.
- Rail errors and retries are counted in `GET /api/guardrails/status`
  (`errors`, `retries`, `last_error`, `fail_mode`).

## Tickets

Tickets are owned by the verified customer, or — for an unverified session —
by the session that opened them. `get_ticket` only returns tickets the current
session or customer owns (a foreign ticket looks exactly like a missing one),
and the ticket owner is set server-side, never by the LLM.

## Rate limiting

Per-conversation message limits and per-IP session-creation limits return
`429` + `Retry-After` (see README → API surface). They are in-process: with
several workers or replicas, move the counters to a shared store. Behind a
reverse proxy, run uvicorn with `--forwarded-allow-ips` so the client IP is
the real one.

## Known gaps

- The email + PIN directory is a demo; replace it with a real identity provider.
- Rate limits are per process (see above).
