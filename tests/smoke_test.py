#!/usr/bin/env python3
"""
Smoke test for the email-ai-agent backend.

Boots the server in a subprocess (SQLite test DB, dummy API_KEY), hits the
main endpoints and asserts 200s / expected auth behaviour. Stdlib only.

Usage:
    python3 tests/smoke_test.py
    python3 tests/smoke_test.py --keep-db   # leave the sqlite file for inspection
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "backend")
API_KEY = "smoke-test-secret-key"
PORT = 18001
BASE = f"http://127.0.0.1:{PORT}"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def req(method, path, data=None, headers=None, content_type="application/json"):
    body = None
    if data is not None:
        if isinstance(data, dict) and content_type == "application/x-www-form-urlencoded":
            body = urllib.parse.urlencode(data).encode()
        else:
            body = json.dumps(data).encode()
    r = urllib.request.Request(BASE + path, data=body, method=method)
    r.add_header("Content-Type", content_type)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # connection refused etc.
        return None, str(e)


def main():
    tmpdir = tempfile.mkdtemp(prefix="email-agent-smoke-")
    db_path = os.path.join(tmpdir, "smoke.db")
    env = dict(os.environ)
    # Sandbox quirk: NO_PROXY/no_proxy contain patterns httpx rejects
    # (see repo AGENTS.md); outbound goes through the *_proxy vars.
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)
    env.update(
        {
            "API_KEY": API_KEY,
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "CORS_ORIGINS": "http://localhost:3000",
            "SMTP_PASSWORD": "dummy",
            "PYTHONUNBUFFERED": "1",
        }
    )
    print(f"Booting server on {BASE} (test DB: {db_path}) ...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for /health
        healthy = False
        for _ in range(60):
            time.sleep(1)
            status, _ = req("GET", "/health")
            if status == 200:
                healthy = True
                break
        check("/health returns 200", healthy)

        # Auth: dashboard endpoints require X-API-Key
        status, _ = req("GET", "/api/settings/prompt")
        check("GET /api/settings/prompt without key -> 401", status == 401, f"got {status}")
        status, _ = req("GET", "/api/settings/prompt", headers={"X-API-Key": "wrong"})
        check("GET /api/settings/prompt with wrong key -> 401", status == 401, f"got {status}")
        H = {"X-API-Key": API_KEY}

        # Prompt read/write round-trip
        status, body = req("GET", "/api/settings/prompt", headers=H)
        check("GET /api/settings/prompt with key -> 200", status == 200, f"got {status}")
        status, body = req("POST", "/api/settings/prompt",
                           {"system_prompt": "You are a smoke-test assistant."}, H)
        check("POST /api/settings/prompt -> 200", status == 200, f"got {status}")
        status, body = req("GET", "/api/settings/prompt", headers=H)
        check("prompt round-trips", status == 200 and "smoke-test assistant" in body, body[:120])

        # Validation: empty prompt rejected
        status, _ = req("POST", "/api/settings/prompt", {"system_prompt": ""}, H)
        check("empty prompt -> 422", status == 422, f"got {status}")

        # Partial key update
        status, body = req("POST", "/api/settings/keys",
                           {"openai_api_key": "dummy-key", "llm_provider": "gpt-4o"}, H)
        check("POST /api/settings/keys (partial) -> 200", status == 200, f"got {status}")

        # Drafts list (empty is fine)
        status, body = req("GET", "/api/emails/drafts", headers=H)
        check("GET /api/emails/drafts -> 200", status == 200 and body.strip() == "[]", f"got {status} {body[:80]}")

        # Approve a nonexistent draft -> 404 JSON
        status, body = req("POST", "/api/emails/drafts/99999/approve",
                           {"edited_content": "hi"}, H)
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        check("approve missing draft -> 404 JSON error",
              status == 404 and parsed.get("error") == "not_found", f"got {status} {body[:120]}")

        # Webhook: valid inbound email -> 200 (background LLM call fails on dummy key; response stays 200)
        form = {"from": "Lead <lead@example.com>", "subject": "Pricing?",
                "text": "Hi, what are your charges for a website?"}
        status, body = req("POST", "/webhook/email", form, content_type="application/x-www-form-urlencoded")
        check("POST /webhook/email (valid) -> 200", status == 200 and body == "ok", f"got {status} {body[:80]}")

        # Webhook: invalid sender email rejected
        form_bad = {"from": "not-an-email", "subject": "x", "text": "y"}
        status, _ = req("POST", "/webhook/email", form_bad, content_type="application/x-www-form-urlencoded")
        check("POST /webhook/email (bad from) -> 422", status == 422, f"got {status}")

        # CORS: unlisted origin gets no ACAO header
        r = urllib.request.Request(BASE + "/api/settings/prompt", method="OPTIONS")
        r.add_header("Origin", "https://evil.example")
        r.add_header("Access-Control-Request-Method", "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                acao = resp.headers.get("Access-Control-Allow-Origin")
        except urllib.error.HTTPError as e:
            acao = e.headers.get("Access-Control-Allow-Origin")
        check("unlisted Origin gets no Access-Control-Allow-Origin", acao is None, f"got {acao}")

        # Unknown route -> structured JSON 404 (no stack trace)
        status, body = req("GET", "/nope")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        check("unknown route -> 404 JSON, no traceback",
              status == 404 and parsed.get("error") == "not_found" and "Traceback" not in body,
              f"got {status}")

        print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        return 1 if FAIL else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if "--keep-db" not in sys.argv:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
