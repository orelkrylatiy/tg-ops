"""Append-only, privacy-conscious operational telemetry for TG Ops."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "ops-data"
EVENTS_PATH = DATA_DIR / "events.jsonl"
_LOCK = threading.Lock()
_SENSITIVE_FIELDS = {
    "text",
    "message",
    "body",
    "content",
    "caption",
    "phone",
    "token",
    "password",
    "api_hash",
    "api_id",
}


def current_session_id() -> str:
    """Return a stable, non-secret identifier for the account used by this process."""
    raw = os.environ.get("TG_OPS_SESSION_ID") or os.environ.get("TG_OPS_SESSION") or "userbot"
    name = str(raw).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if name.lower().endswith(".session"):
        name = name[:-8]
    return name or "userbot"


def _safe_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in list(value.items())[:50]
            if str(key).lower() not in _SENSITIVE_FIELDS
        }
    return str(value)[:200]


def record_event(event: str, **fields) -> bool:
    """Append one event. Telemetry failures must never interrupt Telegram operations.

    Message bodies, captions, credentials and phone numbers are intentionally omitted.
    """
    payload = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": str(event),
        "session_id": fields.pop("session_id", None) or current_session_id(),
    }
    for key, value in fields.items():
        if str(key).lower() in _SENSITIVE_FIELDS or key in {"ts", "event"}:
            continue
        payload[str(key)] = _safe_value(value)

    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        with _LOCK:
            descriptor = os.open(str(EVENTS_PATH), flags, 0o600)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
        return True
    except OSError:
        return False


def read_events(path=None):
    """Read valid JSONL events; ignore a truncated or malformed final line."""
    source = Path(path) if path is not None else EVENTS_PATH
    if not source.is_file():
        return []
    events = []
    try:
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(item, dict) and item.get("event"):
                    events.append(item)
    except OSError:
        return []
    return events


def is_resume_file(path) -> bool:
    """Best-effort classifier for resume/CV attachments without reading file contents."""
    value = str(path).lower().replace("\\", "/")
    filename = value.rsplit("/", 1)[-1]
    return "resume" in value or filename.startswith("cv") or "/cv/" in value
