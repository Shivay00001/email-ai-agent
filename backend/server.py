# email-ai-agent backend (FastAPI)
#
# Inbound-email AI agent: SendGrid Inbound Parse webhook -> LLM-drafted reply ->
# human-in-the-loop approval dashboard -> outbound send via SMTP.
#
# All secrets and config come from environment variables (see .env.example).
# Non-public API endpoints require the shared-secret API key header:
#   X-API-Key: <API_KEY>

import logging
import os
import re
import secrets
import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional

import aiosmtplib
import litellm
from dotenv import load_dotenv
from email.message import EmailMessage
from email_reply_parser import EmailReplyParser
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sendgrid.helpers.eventwebhook import EventWebhook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import Base, SessionLocal, engine, get_db
from models import EmailLog, Setting

load_dotenv()

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("email-ai-agent")

# ----------------------------------------------------------------------------
# Config (all from env; see .env.example)
# ----------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError(
        "API_KEY env var is required. Copy .env.example to .env and set a "
        "long random value (e.g. `openssl rand -hex 32`)."
    )

CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
]
SMTP_PORT = os.getenv("SMTP_PORT", "587")
try:
    SMTP_PORT_INT = int(SMTP_PORT)
except ValueError:
    raise RuntimeError(f"SMTP_PORT must be an integer, got {SMTP_PORT!r}")

SENDGRID_WEBHOOK_KEY = os.getenv("SENDGRID_WEBHOOK_KEY", "").strip() or None
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "agent@localhost")
DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "gpt-4o") or "gpt-4o"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FROM_HEADER_RE = re.compile(r"<(.+?)>")
MAX_SUBJECT_LEN = 500
MAX_BODY_LEN = 200_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as db:
        result = await db.execute(
            select(Setting).where(Setting.key == "system_prompt")
        )
        if not result.scalar_one_or_none():
            db.add(
                Setting(
                    key="system_prompt",
                    value="You are a professional AI executive assistant. Read the incoming email and reply politely and concisely.",
                )
            )
            await db.commit()
    logger.info("email-ai-agent backend ready")
    yield


app = FastAPI(title="Email AI Agent", lifespan=lifespan)

# Sane CORS: allow only explicitly configured origins (never "*").
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
    max_age=600,
)


# ----------------------------------------------------------------------------
# Structured JSON errors (never leak stack traces to clients)
# ----------------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": _status_phrase(exc.status_code), "detail": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal_error"})


def _status_phrase(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        422: "validation_error",
        500: "internal_error",
    }.get(status_code, "error")


# ----------------------------------------------------------------------------
# Auth: shared-secret API key on all /api/* endpoints
# ----------------------------------------------------------------------------
async def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    """Require the shared-secret API key header on dashboard endpoints."""
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ----------------------------------------------------------------------------
# Public endpoints
# ----------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Public health check: 200 when the app and DB are reachable."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:  # DB unreachable; app itself is up
        logger.warning("health check: database unreachable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "error": "database_unreachable"},
        )


# ----------------------------------------------------------------------------
# LLM / email plumbing
# ----------------------------------------------------------------------------
async def get_api_key(db: AsyncSession, key_name: str, env_fallback: str) -> Optional[str]:
    """DB-stored key first, env var fallback. Returns None when unset."""
    result = await db.execute(select(Setting).where(Setting.key == key_name))
    setting = result.scalar_one_or_none()
    if setting and setting.value:
        return setting.value.strip()
    value = os.getenv(env_fallback, "").strip()
    return value or None


async def send_email_async(
    to_email: str, subject: str, text_content: str, db: AsyncSession
) -> bool:
    """Send outbound email using SMTP (e.g. SendGrid SMTP)."""
    msg = EmailMessage()
    msg.set_content(text_content)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = to_email

    try:
        smtp_password = await get_api_key(db, "sendgrid_api_key", "SMTP_PASSWORD")
        if not smtp_password:
            raise RuntimeError("SMTP password not configured (set SMTP_PASSWORD or store sendgrid_api_key)")

        await aiosmtplib.send(
            msg,
            hostname=os.getenv("SMTP_HOST", "smtp.sendgrid.net") or "smtp.sendgrid.net",
            port=SMTP_PORT_INT,
            username=os.getenv("SMTP_USERNAME", "apikey") or "apikey",
            password=smtp_password,
            use_tls=False,
            start_tls=True,
        )
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


async def process_email_task(from_email: str, subject: str, text_body: str):
    """Background pipeline: log inbound email, draft an LLM reply (draft only)."""
    async with SessionLocal() as db:
        try:
            # 0. Clean incoming email (remove quoted history)
            cleaned_body = EmailReplyParser.parse_reply(text_body)

            # 1. Log incoming email
            db.add(
                EmailLog(
                    sender_email=from_email,
                    subject=subject,
                    content=cleaned_body,
                    is_outbound=False,
                    status="received",
                )
            )
            await db.commit()

            # 2. Get instructions and keys
            res = await db.execute(
                select(Setting).where(Setting.key == "system_prompt")
            )
            setting = res.scalar_one_or_none()
            sys_prompt = setting.value if setting else "You are an assistant."

            openai_key = await get_api_key(db, "openai_api_key", "OPENAI_API_KEY")
            anthropic_key = await get_api_key(db, "anthropic_api_key", "ANTHROPIC_API_KEY")
            gemini_key = await get_api_key(db, "gemini_api_key", "GEMINI_API_KEY")
            glm_key = await get_api_key(db, "glm_api_key", "ZHIPUAI_API_KEY")
            provider = (
                await get_api_key(db, "llm_provider", "LLM_PROVIDER")
            ) or DEFAULT_PROVIDER

            def get_active_key(prov: str) -> Optional[str]:
                prov = (prov or "").lower()
                if prov.startswith("gpt"):
                    return openai_key
                if prov.startswith("claude"):
                    return anthropic_key
                if prov.startswith("gemini"):
                    return gemini_key
                if prov.startswith("zhipu"):
                    return glm_key
                return openai_key

            # 3. Generate reply (real LLM call only -- no placeholder fallbacks).
            #    If the LLM fails, the email is recorded as "failed" so a human
            #    sees it; it never becomes an approvable draft.
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Subject: {subject}\n\n{cleaned_body}"},
            ]

            ai_reply: Optional[str] = None
            last_error = "unknown error"
            for attempt in range(3):
                try:
                    active_key = get_active_key(provider)
                    if not active_key:
                        raise RuntimeError(
                            f"No API key configured for provider {provider!r}"
                        )
                    response = await litellm.acompletion(
                        model=provider,
                        messages=messages,
                        api_key=active_key,
                        max_tokens=800,
                    )
                    content = response.choices[0].message.content
                    if content and content.strip():
                        ai_reply = content
                        break
                    last_error = "LLM returned an empty reply"
                except Exception as exc:
                    last_error = str(exc)[:300]
                    logger.warning(
                        "LLM attempt %d/3 failed: %s", attempt + 1, last_error
                    )
                    await asyncio.sleep(1)

            reply_subject = (
                f"Re: {subject}" if not subject.startswith("Re:") else subject
            )
            if ai_reply:
                # 4. Log outbound email as DRAFT (Human-in-the-Loop)
                db.add(
                    EmailLog(
                        sender_email=from_email,
                        subject=reply_subject,
                        content=ai_reply,
                        is_outbound=True,
                        status="draft",  # Do not send yet!
                    )
                )
            else:
                db.add(
                    EmailLog(
                        sender_email=from_email,
                        subject=reply_subject,
                        content=(
                            "[LLM ERROR] Reply generation failed after 3 attempts. "
                            f"Provider: {provider}. Last error: {last_error}. "
                            "No draft was created."
                        ),
                        is_outbound=True,
                        status="failed",
                    )
                )
            await db.commit()

        except Exception as exc:
            logger.exception("Email pipeline error")
            # Never let a stack trace reach a client; it's logged server-side.


# ----------------------------------------------------------------------------
# SendGrid Inbound Parse webhook (public; SendGrid signature-checked when key set)
# ----------------------------------------------------------------------------
@app.post("/webhook/email")
async def email_webhook(
    request: Request,
    x_twilio_email_event_webhook_signature: Optional[str] = Header(default=None),
    x_twilio_email_event_webhook_timestamp: Optional[str] = Header(default=None),
):
    """
    SendGrid Inbound Parse Webhook. Receives multipart/form-data and returns
    200 immediately; the reply is drafted in the background.
    """
    if SENDGRID_WEBHOOK_KEY:
        # When the public key is configured, the signature is REQUIRED.
        ew = EventWebhook()
        key = ew.convert_public_key_to_ecdsa(SENDGRID_WEBHOOK_KEY)
        payload = await request.body()
        is_valid = ew.verify_signature(
            payload,
            x_twilio_email_event_webhook_signature or "",
            x_twilio_email_event_webhook_timestamp or "",
            key,
        )
        if not is_valid:
            raise HTTPException(status_code=403, detail="Invalid SendGrid signature")
    elif not x_twilio_email_event_webhook_signature:
        logger.warning(
            "Unsigned inbound webhook accepted (SENDGRID_WEBHOOK_KEY not set). "
            "Set SENDGRID_WEBHOOK_KEY in production."
        )

    form_data = await request.form()

    from_raw = str(form_data.get("from", "")).strip()
    subject = str(form_data.get("subject", "")).strip()
    text_body = str(form_data.get("text", "") or "")

    match = FROM_HEADER_RE.search(from_raw)
    from_email = (match.group(1) if match else from_raw).strip().lower()

    # Input validation on all external inputs
    if len(from_email) > 254 or not EMAIL_RE.fullmatch(from_email):
        raise HTTPException(status_code=422, detail="Invalid sender email address")
    if len(subject) > MAX_SUBJECT_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Subject too long (max {MAX_SUBJECT_LEN} chars)",
        )
    if len(text_body) > MAX_BODY_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Body too long (max {MAX_BODY_LEN} chars)",
        )

    # Draft the reply in the background so the webhook stays fast.
    asyncio.create_task(process_email_task(from_email, subject, text_body))
    return Response(content="ok", status_code=200)


# ----------------------------------------------------------------------------
# Dashboard API (all require X-API-Key)
# ----------------------------------------------------------------------------
class PromptUpdate(BaseModel):
    system_prompt: str = Field(min_length=1, max_length=5000)


class DraftApproval(BaseModel):
    edited_content: str = Field(min_length=1, max_length=MAX_BODY_LEN)


class ApiKeysUpdate(BaseModel):
    openai_api_key: Optional[str] = Field(default=None, max_length=500)
    anthropic_api_key: Optional[str] = Field(default=None, max_length=500)
    gemini_api_key: Optional[str] = Field(default=None, max_length=500)
    glm_api_key: Optional[str] = Field(default=None, max_length=500)
    sendgrid_api_key: Optional[str] = Field(default=None, max_length=500)
    llm_provider: Optional[str] = Field(default=None, max_length=100)


@app.get("/api/settings/prompt", dependencies=[Depends(require_api_key)])
async def get_prompt(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Setting).where(Setting.key == "system_prompt")
    )
    setting = result.scalar_one_or_none()
    return {"system_prompt": setting.value if setting else ""}


@app.post("/api/settings/prompt", dependencies=[Depends(require_api_key)])
async def update_prompt(req: PromptUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Setting).where(Setting.key == "system_prompt")
    )
    setting = result.scalar_one_or_none()

    if setting:
        setting.value = req.system_prompt.strip()
    else:
        setting = Setting(key="system_prompt", value=req.system_prompt.strip())
        db.add(setting)

    await db.commit()
    return {"status": "success", "system_prompt": setting.value}


@app.post("/api/settings/keys", dependencies=[Depends(require_api_key)])
async def update_keys(req: ApiKeysUpdate, db: AsyncSession = Depends(get_db)):
    """Store provider keys in the DB (partial updates allowed)."""
    updated: List[str] = []
    for k, v in [
        ("openai_api_key", req.openai_api_key),
        ("anthropic_api_key", req.anthropic_api_key),
        ("gemini_api_key", req.gemini_api_key),
        ("glm_api_key", req.glm_api_key),
        ("llm_provider", req.llm_provider),
        ("sendgrid_api_key", req.sendgrid_api_key),
    ]:
        v = (v or "").strip()
        if v:
            res = await db.execute(select(Setting).where(Setting.key == k))
            setting = res.scalar_one_or_none()
            if setting:
                setting.value = v
            else:
                db.add(Setting(key=k, value=v))
            updated.append(k)
    await db.commit()
    return {"status": "success", "updated": updated}


@app.get("/api/emails/drafts", dependencies=[Depends(require_api_key)])
async def get_drafts(db: AsyncSession = Depends(get_db)):
    """Fetch all pending draft emails for user approval."""
    result = await db.execute(
        select(EmailLog)
        .where(EmailLog.status == "draft")
        .order_by(EmailLog.timestamp.desc())
    )
    drafts = result.scalars().all()
    return [
        {
            "id": d.id,
            "sender_email": d.sender_email,
            "subject": d.subject,
            "content": d.content,
            "timestamp": d.timestamp,
        }
        for d in drafts
    ]


@app.post(
    "/api/emails/drafts/{draft_id}/approve",
    dependencies=[Depends(require_api_key)],
)
async def approve_draft(
    draft_id: int, req: DraftApproval, db: AsyncSession = Depends(get_db)
):
    """Approve a draft, update its content if edited, and send it."""
    if draft_id < 1:
        raise HTTPException(status_code=422, detail="Invalid draft id")

    result = await db.execute(
        select(EmailLog).where(
            EmailLog.id == draft_id, EmailLog.status == "draft"
        )
    )
    draft = result.scalar_one_or_none()

    if not draft:
        raise HTTPException(
            status_code=404, detail="Draft not found or already processed"
        )

    draft.content = req.edited_content.strip()
    draft.status = "approved"
    await db.commit()

    success = await send_email_async(
        draft.sender_email, draft.subject, draft.content, db
    )

    if success:
        draft.status = "sent"
        await db.commit()
        return {"status": "success", "message": "Email sent"}
    else:
        draft.status = "failed"
        await db.commit()
        raise HTTPException(status_code=500, detail="Failed to send email")

