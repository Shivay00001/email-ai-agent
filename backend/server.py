import os
import asyncio
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Form, Request, Response, BackgroundTasks, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from dotenv import load_dotenv
import litellm
import aiosmtplib
from email.message import EmailMessage
from email_reply_parser import EmailReplyParser
from sendgrid.helpers.eventwebhook import EventWebhook
from pydantic import BaseModel
from typing import List

from database import engine, Base, SessionLocal, get_db
from models import Setting, EmailLog

load_dotenv()

load_dotenv()
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with SessionLocal() as db:
        result = await db.execute(select(Setting).where(Setting.key == "system_prompt"))
        if not result.scalar_one_or_none():
            db.add(Setting(key="system_prompt", value="You are a professional AI executive assistant. Read the incoming email and reply politely and concisely."))
            await db.commit()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def get_api_key(db: AsyncSession, key_name: str, env_fallback: str) -> str:
    result = await db.execute(select(Setting).where(Setting.key == key_name))
    setting = result.scalar_one_or_none()
    if setting and setting.value:
        return setting.value
    return os.getenv(env_fallback)

async def send_email_async(to_email: str, subject: str, text_content: str, db: AsyncSession):
    """Sends outbound email using SMTP (e.g. SendGrid SMTP)"""
    msg = EmailMessage()
    msg.set_content(text_content)
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM_EMAIL", "agent@localhost")
    msg["To"] = to_email

    try:
        smtp_password = await get_api_key(db, "sendgrid_api_key", "SMTP_PASSWORD")
        if not smtp_password:
            raise Exception("SendGrid API Key not configured")

        await aiosmtplib.send(
            msg,
            hostname=os.getenv("SMTP_HOST", "smtp.sendgrid.net"),
            port=int(os.getenv("SMTP_PORT", 587)),
            username=os.getenv("SMTP_USERNAME", "apikey"),
            password=smtp_password,
            use_tls=False,
            start_tls=True,
        )
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False

async def process_email_task(from_email: str, subject: str, text_body: str):
    async with SessionLocal() as db:
        try:
            # 0. Clean incoming email (remove quoted history)
            cleaned_body = EmailReplyParser.parse_reply(text_body)

            # 1. Log incoming email
            incoming_log = EmailLog(
                sender_email=from_email,
                subject=subject,
                content=cleaned_body,
                is_outbound=False,
                status="received"
            )
            db.add(incoming_log)
            await db.commit()

            # 2. Get Instructions and Keys
            res = await db.execute(select(Setting).where(Setting.key == "system_prompt"))
            setting = res.scalar_one_or_none()
            sys_prompt = setting.value if setting else "You are an assistant."

            openai_key = await get_api_key(db, "openai_api_key", "OPENAI_API_KEY")
            anthropic_key = await get_api_key(db, "anthropic_api_key", "ANTHROPIC_API_KEY")
            gemini_key = await get_api_key(db, "gemini_api_key", "GEMINI_API_KEY")
            glm_key = await get_api_key(db, "glm_api_key", "ZHIPUAI_API_KEY")
            provider = await get_api_key(db, "llm_provider", "LLM_PROVIDER") or "gpt-4o"

            def get_active_key(prov: str):
                if prov.startswith("gpt"): return openai_key
                if prov.startswith("claude"): return anthropic_key
                if prov.startswith("gemini"): return gemini_key
                if prov.startswith("zhipu"): return glm_key
                return openai_key

            # 3. Generate Reply
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Subject: {subject}\n\n{cleaned_body}"}
            ]

            ai_reply = "Thank you for your email. I am currently unavailable."
            for attempt in range(3):
                try:
                    response = await litellm.acompletion(
                        model=provider,
                        messages=messages,
                        api_key=get_active_key(provider),
                        max_tokens=800
                    )
                    ai_reply = response.choices[0].message.content
                    break
                except Exception as e:
                    print(f"OpenAI error: {e}")
                    await asyncio.sleep(1)

            # 4. Log outbound email as DRAFT (Human-in-the-Loop)
            reply_subject = f"Re: {subject}" if not subject.startswith("Re:") else subject
            outbound_log = EmailLog(
                sender_email=from_email,
                subject=reply_subject,
                content=ai_reply,
                is_outbound=True,
                status="draft" # Do not send yet!
            )
            db.add(outbound_log)
            await db.commit()

        except Exception as e:
            print(f"Pipeline error: {e}")

@app.post("/webhook/email")
async def email_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_twilio_email_event_webhook_signature: str = Header(None),
    x_twilio_email_event_webhook_timestamp: str = Header(None)
):
    """
    SendGrid Inbound Parse Webhook Endpoint.
    Receives multipart/form-data.
    """
    # Security: Verify SendGrid Signature if key is provided
    public_key = os.getenv("SENDGRID_WEBHOOK_KEY")
    if public_key and x_twilio_email_event_webhook_signature:
        ew = EventWebhook()
        key = ew.convert_public_key_to_ecdsa(public_key)
        payload = await request.body()
        is_valid = ew.verify_signature(
            payload,
            x_twilio_email_event_webhook_signature,
            x_twilio_email_event_webhook_timestamp,
            key
        )
        if not is_valid:
            raise HTTPException(status_code=403, detail="Invalid SendGrid Signature")

    form_data = await request.form()
    
    from_raw = form_data.get("from", "")
    subject = form_data.get("subject", "")
    text_body = form_data.get("text", "")
    
    match = re.search(r'<(.+?)>', from_raw)
    from_email = match.group(1) if match else from_raw

    # Dispatch to background task to avoid blocking webhook
    background_tasks.add_task(process_email_task, from_email, subject, text_body)
    
    return Response(content="ok", status_code=200)

# ==========================================
# DASHBOARD SETTINGS & DRAFTS ENDPOINTS
# ==========================================
class PromptUpdate(BaseModel):
    system_prompt: str

class DraftApproval(BaseModel):
    edited_content: str

@app.get("/api/settings/prompt")
async def get_prompt(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting).where(Setting.key == "system_prompt"))
    setting = result.scalar_one_or_none()
    return {"system_prompt": setting.value if setting else ""}

@app.post("/api/settings/prompt")
async def update_prompt(req: PromptUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting).where(Setting.key == "system_prompt"))
    setting = result.scalar_one_or_none()
    
    if setting:
        setting.value = req.system_prompt
    else:
        setting = Setting(key="system_prompt", value=req.system_prompt)
        db.add(setting)
        
    await db.commit()
    return {"status": "success", "system_prompt": setting.value}

class ApiKeysUpdate(BaseModel):
    openai_api_key: str
    anthropic_api_key: str
    gemini_api_key: str
    glm_api_key: str
    sendgrid_api_key: str
    llm_provider: str

@app.post("/api/settings/keys")
async def update_keys(req: ApiKeysUpdate, db: AsyncSession = Depends(get_db)):
    for k, v in [
        ("openai_api_key", req.openai_api_key), 
        ("anthropic_api_key", req.anthropic_api_key),
        ("gemini_api_key", req.gemini_api_key),
        ("glm_api_key", req.glm_api_key),
        ("llm_provider", req.llm_provider),
        ("sendgrid_api_key", req.sendgrid_api_key)
    ]:
        if v:
            res = await db.execute(select(Setting).where(Setting.key == k))
            setting = res.scalar_one_or_none()
            if setting:
                setting.value = v
            else:
                db.add(Setting(key=k, value=v))
    await db.commit()
    return {"status": "success"}

@app.get("/api/emails/drafts")
async def get_drafts(db: AsyncSession = Depends(get_db)):
    """Fetch all pending draft emails for user approval."""
    result = await db.execute(
        select(EmailLog)
        .where(EmailLog.status == "draft")
        .order_by(EmailLog.timestamp.desc())
    )
    drafts = result.scalars().all()
    return [{
        "id": d.id,
        "sender_email": d.sender_email,
        "subject": d.subject,
        "content": d.content,
        "timestamp": d.timestamp
    } for d in drafts]

@app.post("/api/emails/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, req: DraftApproval, db: AsyncSession = Depends(get_db)):
    """Approve a draft, update its content if edited, and send it."""
    result = await db.execute(select(EmailLog).where(EmailLog.id == draft_id, EmailLog.status == "draft"))
    draft = result.scalar_one_or_none()
    
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found or already processed")
        
    draft.content = req.edited_content
    draft.status = "approved"
    await db.commit()
    
    success = await send_email_async(draft.sender_email, draft.subject, draft.content, db)
    
    if success:
        draft.status = "sent"
        await db.commit()
        return {"status": "success", "message": "Email sent"}
    else:
        draft.status = "failed"
        await db.commit()
        raise HTTPException(status_code=500, detail="Failed to send email")
