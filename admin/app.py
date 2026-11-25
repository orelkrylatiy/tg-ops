"""Read-only HTTP API and UI for Telegram scan reports and LinkedIn leads."""

from __future__ import annotations

import base64
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from admin.scan_reports import read_reports


ROOT = Path(__file__).resolve().parents[1]
CONTACTS_PATH = ROOT / "leads" / "linkedin-contacts.json"
INDEX_PATH = Path(__file__).with_name("index.html")
app = FastAPI(title="TG Ops Admin", docs_url=None, redoc_url=None)


def _configured_credentials() -> tuple[str, str] | None:
    username = os.getenv("ADMIN_USERNAME", "")
    password = os.getenv("ADMIN_PASSWORD", "")
    if bool(username) != bool(password):
        raise RuntimeError("Both admin credentials must be configured.")
    if not username or not password:
        raise RuntimeError("Admin credentials are required.")
    return username, password


def _authorized(request: Request, credentials: tuple[str, str]) -> bool:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    return bool(separator) and hmac.compare_digest(username, credentials[0]) and hmac.compare_digest(
        password, credentials[1]
    )


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    try:
        credentials = _configured_credentials()
    except RuntimeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)
    if not _authorized(request, credentials):
        return JSONResponse(
            {"detail": "Admin authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="TG Ops"'},
        )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(INDEX_PATH, media_type="text/html; charset=utf-8")


@app.get("/api/reports")
def reports():
    return {"reports": read_reports()}


@app.get("/api/contacts")
def contacts():
    try:
        data = json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"updated_at": None, "contacts": []}
    except (OSError, json.JSONDecodeError):
        return JSONResponse({"detail": "LinkedIn contacts data is unavailable."}, status_code=500)
    if not isinstance(data, dict) or not isinstance(data.get("contacts"), list):
        return JSONResponse({"detail": "LinkedIn contacts data is invalid."}, status_code=500)
    return data
