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

## Related hardening applied in this pass

- **AuthN/AuthZ (C3/C4):** API now requires a bearer token (`API_TOKEN`) and each
  session is bound to a customer identity that scopes all reads and tool lookups.
  See `README`/code comments. Set a strong `API_TOKEN` in production.
- **CORS (L2):** wildcard origins are no longer combined with credentials; set
  `CORS_ORIGINS` to your exact frontend origin(s) in production.
- **Guardrails (L3):** the safety layer still fails open by design, but rail errors
  are now surfaced in `GET /api/guardrails/status` (`last_error`) so a silently
  disabled guard is visible.
