# Email AI Agent

Inbound-email AI agent for SMBs: **SendGrid Inbound Parse webhook → LLM-drafted reply → human-in-the-loop approval dashboard → outbound send via SMTP.** Drafts are never auto-sent; a human reviews and approves each reply.

- **Backend:** FastAPI (Python), LiteLLM (OpenAI / Anthropic / Gemini / ZhipuAI), SQLite / PostgreSQL
- **Frontend:** Next.js dashboard (drafts queue + agent settings)

## Quick start (local)

```bash
# 1. Configure
cp .env.example .env          # then set a long random API_KEY

# 2. Backend
python -m venv ~/workspace/venvs/email-agent
~/workspace/venvs/email-agent/bin/pip install -r requirements.txt
cd backend
~/workspace/venvs/email-agent/bin/uvicorn server:app --host 0.0.0.0 --port 8000

# 3. Frontend
cp frontend/.env.example frontend/.env.local   # set NEXT_PUBLIC_API_KEY = same API_KEY
cd frontend
npm install
npm run dev     # dashboard at http://localhost:3000
```

Verify: `curl http://localhost:8000/health` → `{"status":"ok"}`.

Run the smoke test: `python3 tests/smoke_test.py` (boots a throwaway server + SQLite DB, asserts health/auth/validation/webhook behavior).

### Wiring SendGrid inbound mail (production)

1. SendGrid → Settings → Inbound Parse: point your domain/MX at SendGrid, set the webhook URL to `https://<your-api>/webhook/email` with POST.
2. Inbound Parse posts `from`, `subject`, `text` (and attachments) as multipart form — handled by `/webhook/email`.
3. For signature verification, create an ECDSA key pair in SendGrid → Mail Settings → Event Webhook and set `SENDGRID_WEBHOOK_KEY` to the public key.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `API_KEY` | **yes** | — | Shared secret; every `/api/*` call must send `X-API-Key: <API_KEY>`. Generate with `openssl rand -hex 32`. |
| `DATABASE_URL` | no | `sqlite+aiosqlite:///./email.db` | SQLAlchemy URL. Production: `postgresql+asyncpg://USER:PASS@HOST:5432/email_ai_agent` |
| `CORS_ORIGINS` | no | _(empty = none)_ | Comma-separated origins allowed to call the API (e.g. `https://dash.example.com`). Never `*`. |
| `OPENAI_API_KEY` | no* | — | LLM provider keys — at least one required for draft generation |
| `ANTHROPIC_API_KEY` | no* | — | |
| `GEMINI_API_KEY` | no* | — | |
| `ZHIPUAI_API_KEY` | no* | — | |
| `LLM_PROVIDER` | no | `gpt-4o` | Model litellm calls, e.g. `gpt-4o`, `claude-3-5-sonnet-20240620`, `gemini/gemini-1.5-pro`, `zhipu/glm-4` |
| `SENDGRID_WEBHOOK_KEY` | no | — | SendGrid ECDSA public key; when set, `/webhook/email` rejects unsigned requests (403) |
| `SMTP_HOST` | no | `smtp.sendgrid.net` | Outbound SMTP host |
| `SMTP_PORT` | no | `587` | Outbound SMTP port |
| `SMTP_USERNAME` | no | `apikey` | Outbound SMTP username |
| `SMTP_PASSWORD` | no | — | Outbound SMTP password (or store `sendgrid_api_key` via `/api/settings/keys`) |
| `SMTP_FROM_EMAIL` | no | `agent@localhost` | From-address on outbound mail |

Provider keys can also be stored per-install via `POST /api/settings/keys` (DB wins over env). The frontend `frontend/.env.local` needs `NEXT_PUBLIC_API_BASE` (backend URL) and `NEXT_PUBLIC_API_KEY` (same `API_KEY`).

## API

Public: `GET /health` → 200 `{"status":"ok"}` (503 `{"status":"degraded"}` if DB unreachable).
SendGrid webhook: `POST /webhook/email` (multipart form `from`/`subject`/`text`) → 200 `ok`.

All `/api/*` endpoints require header `X-API-Key: <API_KEY>` (401 otherwise):

- `GET /api/settings/prompt` — current system prompt
- `POST /api/settings/prompt` — `{system_prompt}` (1–5000 chars)
- `POST /api/settings/keys` — partial update of provider keys / `llm_provider`
- `GET /api/emails/drafts` — pending drafts awaiting approval
- `POST /api/emails/drafts/{id}/approve` — `{edited_content}` → sends the email

Errors are JSON (`{"error": "...", "detail": "..."}`); stack traces are never returned.

## Deploy on Render (free tier)

1. Push this repo to GitHub. Render → New → **Web Service** → select the repo → **Dockerfile** runtime.
2. Set environment variables from the table above (Render dashboard → Environment). `API_KEY` = random hex.
3. Add a Render **PostgreSQL (free)** instance and set `DATABASE_URL` to its async URL (`postgresql+asyncpg://…`).
4. Set `CORS_ORIGINS` to your dashboard URL. Deploy the `frontend/` on Render Static Site or Cloudflare Pages with `NEXT_PUBLIC_API_BASE` = backend URL and `NEXT_PUBLIC_API_KEY` = same `API_KEY`.

Notes: SQLite works for a quick demo but Render disks are ephemeral — use Postgres for anything real. The agent only *drafts* replies; nothing sends without a human tapping **Approve & Send** in the dashboard.
