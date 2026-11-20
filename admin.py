"""TG Ops Control Center.

Run: python admin.py [--host 127.0.0.1] [--port 8765]
The server is intentionally loopback-only. Telegram actions are exposed through
a strict allow-list and outgoing human messages use humanizer-framework.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telethon.tl.types import User

from admin_agent import (
    AgentService,
    get_conversation,
    list_chats,
    prepare_message,
    send_prepared_message,
)
from admin_runtime import ProcessManager, session_io
from ops_telemetry import read_events, record_event
from tgcommon import HERE, SERVICE_CHAT_IDS, configured_sessions, connect_any

LEADS_PATH = Path(HERE) / "leads" / "leads.jsonl"
VACANCIES_PATH = Path(HERE) / "vacancies" / "vacancies.jsonl"
INDEX_PATH = Path(HERE) / "admin" / "index.html"
CRON_LOCK_PATH = Path(HERE) / "leads" / ".cron.lock"
PERIODS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}
VALID_LEAD_STATUSES = {"new", "contacted", "replied", "warm", "declined"}

PROCESS_MANAGER = ProcessManager()
AGENT_SERVICE = AgentService(PROCESS_MANAGER)


def _utc_now():
    return datetime.now(UTC)


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_timestamp(event):
    return _parse_timestamp(event.get("ts") or event.get("timestamp"))


def _load_jsonl(path):
    rows = []
    if not path.is_file():
        return rows
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def _filter_events(events, period, session_id):
    cutoff = _utc_now() - PERIODS[period] if period in PERIODS else None
    filtered = []
    for event in events:
        if session_id != "all" and event.get("session_id", "userbot") != session_id:
            continue
        timestamp = _event_timestamp(event)
        if cutoff and (timestamp is None or timestamp < cutoff):
            continue
        filtered.append(event)
    return filtered


def _day_prefix(value):
    return str(value or "")[:10]


def _lead_statistics(records=None):
    records = records if records is not None else _load_jsonl(LEADS_PATH)
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    statuses = Counter()
    hr_records = []
    emails = 0
    new_today = new_yesterday = 0
    ready_today = ready_yesterday = 0
    first_contacts_today = first_contacts_yesterday = 0

    for record in records:
        kind = record.get("post_kind", "job")
        if record.get("type") == "email":
            emails += 1
        if record.get("type") != "username":
            continue
        if kind == "review":
            continue
        if kind not in {"job", "queued"}:
            continue
        hr_records.append(record)
        status = record.get("status") or "unknown"
        statuses[status] += 1
        first_seen = _day_prefix(record.get("first_seen"))
        ready_date = _day_prefix(
            record.get("prescribed_ready_date") or record.get("prescribed_ready_at")
        )
        contacted_date = _day_prefix(record.get("contacted_at"))
        if first_seen == today.isoformat():
            new_today += 1
        elif first_seen == yesterday.isoformat():
            new_yesterday += 1
        if ready_date == today.isoformat():
            ready_today += 1
        elif ready_date == yesterday.isoformat():
            ready_yesterday += 1
        if contacted_date == today.isoformat():
            first_contacts_today += 1
        elif contacted_date == yesterday.isoformat():
            first_contacts_yesterday += 1

    active = sum(statuses[key] for key in ("new", "contacted", "replied", "warm"))
    return {
        "hr_total": len(hr_records),
        "hr_active": active,
        "hr_new": statuses["new"],
        "hr_contacted": statuses["contacted"],
        "hr_replied": statuses["replied"],
        "hr_warm": statuses["warm"],
        "hr_declined": statuses["declined"],
        "hr_review": sum(
            1
            for record in records
            if record.get("type") == "username" and record.get("post_kind") == "review"
        ),
        "email_total": emails,
        "new_leads_today": new_today,
        "new_leads_yesterday": new_yesterday,
        "ready_today": ready_today,
        "ready_yesterday": ready_yesterday,
        "first_contacts_today": first_contacts_today,
        "first_contacts_yesterday": first_contacts_yesterday,
    }


def _pipeline_snapshot():
    leads = _load_jsonl(LEADS_PATH)
    vacancies = _load_jsonl(VACANCIES_PATH)
    statuses = Counter()
    types = Counter()
    queued = 0
    review = 0
    for record in leads:
        post_kind = record.get("post_kind")
        types[record.get("type") or "unknown"] += 1
        if post_kind == "queued":
            queued += 1
        elif post_kind == "review":
            review += 1
        elif record.get("type") == "username" and post_kind in {"job", None}:
            statuses[record.get("status") or "unknown"] += 1
    return {
        "leads_total": len(leads),
        "leads_by_status": dict(statuses),
        "leads_by_type": dict(types),
        "leads_queued": queued,
        "leads_review": review,
        "vacancies_total": len(vacancies),
        **_lead_statistics(leads),
    }


def _event_metrics(events):
    metrics = {
        "outbound_messages": 0,
        "replies_sent": 0,
        "cold_outreach": 0,
        "followups": 0,
        "warm_returns": 0,
        "auto_replies": 0,
        "resumes_sent": 0,
        "inbound_messages": 0,
        "scan_runs": 0,
        "vacancy_scans": 0,
        "lead_scans": 0,
        "posts_scanned": 0,
        "vacancies_found": 0,
        "new_vacancies": 0,
        "leads_found": 0,
        "operation_errors": 0,
        "cron_steps": 0,
    }
    for event in events:
        kind = event.get("event")
        category = event.get("category")
        if kind == "message_sent":
            metrics["outbound_messages"] += 1
            if event.get("message_type") == "reply":
                metrics["replies_sent"] += 1
            if category == "cold_outreach":
                metrics["cold_outreach"] += 1
            elif category == "followup":
                metrics["followups"] += 1
            elif category == "warm_return":
                metrics["warm_returns"] += 1
            elif category == "auto_reply":
                metrics["auto_replies"] += 1
        elif kind == "resume_sent":
            metrics["resumes_sent"] += 1
        elif kind == "inbound_message":
            metrics["inbound_messages"] += 1
        elif kind == "scan_completed":
            metrics["scan_runs"] += 1
            scan_type = event.get("scan_type")
            if scan_type == "vacancies":
                metrics["vacancy_scans"] += 1
            elif scan_type in {"leads", "prescribe"}:
                metrics["lead_scans"] += 1
            metrics["posts_scanned"] += _as_int(event.get("posts_scanned"))
            metrics["vacancies_found"] += _as_int(event.get("vacancies_found"))
            metrics["new_vacancies"] += _as_int(event.get("new_vacancies"))
            metrics["leads_found"] += _as_int(event.get("leads_found"))
        elif kind in {"operation_failed", "process_exit"}:
            if kind == "operation_failed" or _as_int(event.get("exit_code")) != 0:
                metrics["operation_errors"] += 1
        elif kind == "cron_step":
            metrics["cron_steps"] += 1
            if event.get("success") is False:
                metrics["operation_errors"] += 1
    return metrics


def _as_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _selected_sessions(snapshot, session_id):
    sessions = snapshot.get("sessions") or []
    if session_id == "all":
        return sessions
    return [item for item in sessions if item.get("id") == session_id]


def build_summary(period, session_id, snapshot):
    all_events = read_events()
    events = _filter_events(all_events, period, session_id)
    sessions = [dict(item) for item in _selected_sessions(snapshot, session_id)]
    unique_accounts = {}
    for item in sessions:
        account_key = item.get("user_id") or item.get("id")
        current = unique_accounts.get(account_key)
        if current is None or (
            current.get("status") != "online" and item.get("status") == "online"
        ):
            unique_accounts[account_key] = item
    live_sessions = list(unique_accounts.values())
    live = {
        "configured_sessions": len(sessions),
        "available_sessions": sum(item.get("status") == "online" for item in live_sessions),
        "unique_accounts": len(unique_accounts),
        "total_chats": sum(_as_int(item.get("total_chats")) for item in live_sessions),
        "active_chats_30d": sum(_as_int(item.get("active_chats_30d")) for item in live_sessions),
        "unread_chats": sum(_as_int(item.get("unread_chats")) for item in live_sessions),
    }
    for item in sessions:
        item["metrics"] = _event_metrics(_filter_events(all_events, period, item.get("id", "")))
    metrics = _event_metrics(events)
    pipeline = _pipeline_snapshot()
    pipeline["operation_errors"] = metrics["operation_errors"]
    return {
        "generated_at": _utc_now().isoformat(timespec="seconds"),
        "period": period,
        "session_filter": session_id,
        "metrics": metrics,
        "live": live,
        "pipeline": pipeline,
        "sessions": sessions,
        "sessions_updated_at": snapshot.get("updated_at"),
        "refreshing": bool(snapshot.get("refreshing")),
        "processes": PROCESS_MANAGER.list_processes(),
    }


def build_timeline(period, session_id):
    days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(period, 30)
    start = _utc_now().date() - timedelta(days=days - 1)
    buckets = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        buckets[day.isoformat()] = {
            "date": day.isoformat(),
            "messages": 0,
            "replies": 0,
            "resumes": 0,
            "scans": 0,
            "vacancies": 0,
            "new_leads": 0,
        }
    events = _filter_events(read_events(), period, session_id)
    for event in events:
        timestamp = _event_timestamp(event)
        if timestamp is None:
            continue
        bucket = buckets.get(timestamp.date().isoformat())
        if bucket is None:
            continue
        kind = event.get("event")
        if kind == "message_sent":
            bucket["messages"] += 1
            if event.get("message_type") == "reply":
                bucket["replies"] += 1
        elif kind == "resume_sent":
            bucket["resumes"] += 1
        elif kind == "scan_completed":
            bucket["scans"] += 1
            bucket["vacancies"] += _as_int(event.get("new_vacancies"))
            bucket["new_leads"] += _as_int(event.get("leads_found"))
    return list(buckets.values())


def _activity_item(event, session_names):
    kind = event.get("event")
    category = event.get("category")
    chat_name = event.get("chat_name") or event.get("chat") or str(event.get("chat_id") or "Чат")
    username = event.get("username")
    if not username and str(chat_name).startswith("@"):
        username = str(chat_name).lstrip("@")
    telegram_url = f"https://t.me/{username}" if username else ""
    if kind == "message_sent":
        labels = {
            "manual": "Сообщение отправлено",
            "manual_reply": "Ответ отправлен",
            "cold_outreach": "Первое касание",
            "followup": "Догонка отправлена",
            "warm_return": "Тёплый возврат",
            "auto_reply": "Автоответ отправлен",
            "manual_bridge": "Ответ через бота",
        }
        title = labels.get(category, "Исходящее сообщение")
        detail = chat_name
    elif kind == "resume_sent":
        title = "Резюме отправлено"
        detail = chat_name
    elif kind == "inbound_message":
        title = "Входящее сообщение"
        detail = chat_name
    elif kind == "file_sent":
        title = "Файл отправлен"
        detail = chat_name
    elif kind == "cron_run":
        title = "Автопилот завершён" if event.get("status") == "completed" else "Автопилот"
        detail = (
            f"Холодные: {_as_int(event.get('sent_cold'))} · "
            f"догонки: {_as_int(event.get('sent_followup'))} · "
            f"тёплые: {_as_int(event.get('sent_warm'))}"
        )
    elif kind == "scan_completed":
        title = "Сканирование завершено"
        detail = (
            f"{_as_int(event.get('posts_scanned'))} постов · "
            f"{_as_int(event.get('vacancies_found'))} вакансий · "
            f"{_as_int(event.get('leads_found'))} контактов"
        )
    elif kind in {"process_started", "process_stopped", "process_exit"}:
        title = {
            "process_started": "Процесс запущен",
            "process_stopped": "Процесс остановлен",
            "process_exit": "Процесс завершился",
        }[kind]
        detail = str(event.get("task_id") or event.get("process_id") or "операция")
    elif kind == "cron_step":
        title = "Шаг автопилота"
        detail = str(event.get("step") or "операция")
    elif kind == "lead_status_changed":
        title = "Статус HR изменён"
        detail = str(event.get("status") or "обновлён")
    elif kind == "operation_failed":
        title = "Ошибка операции"
        detail = str(event.get("operation") or event.get("error_type") or "ошибка")
    else:
        title = str(kind or "Событие")
        detail = str(event.get("detail") or "")
    timestamp = _event_timestamp(event)
    session_id = event.get("session_id", "userbot")
    return {
        "ts": timestamp.isoformat() if timestamp else event.get("ts"),
        "title": title,
        "detail": detail,
        "session_id": session_id,
        "session_name": session_names.get(session_id, "Сессия"),
        "category": category or kind,
        "status": event.get("status") or ("error" if kind == "operation_failed" else "success"),
        "telegram_url": telegram_url,
    }


def build_activity(limit, session_id, snapshot):
    events = _filter_events(read_events(), "all", session_id)
    names = {item.get("id"): item.get("name") for item in snapshot.get("sessions") or []}
    events.sort(
        key=lambda item: _event_timestamp(item) or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    return [_activity_item(event, names) for event in events[:limit]]


async def collect_session_snapshot(previous=None):
    previous = previous or {}
    previous_by_id = {item.get("id"): item for item in previous.get("sessions", [])}
    try:
        specs = configured_sessions()
    except Exception as exc:
        return {
            "updated_at": _utc_now().isoformat(timespec="seconds"),
            "sessions": [
                {
                    "id": "sessions-config",
                    "name": "Реестр сессий",
                    "status": "error",
                    "error": type(exc).__name__,
                }
            ],
        }

    sessions = []
    cutoff = _utc_now() - timedelta(days=30)
    for spec in specs:
        item = {
            "id": spec["id"],
            "name": spec["name"],
            "status": "missing",
            "account_name": "",
            "username": "",
            "user_id": None,
            "total_chats": 0,
            "active_chats_30d": 0,
            "unread_chats": 0,
            "checked_at": _utc_now().isoformat(timespec="seconds"),
        }
        if not spec.get("enabled", True):
            item["status"] = "disabled"
            sessions.append(item)
            continue
        if not spec.get("exists"):
            sessions.append(item)
            continue
        if (
            CRON_LOCK_PATH.exists()
            or PROCESS_MANAGER.is_session_busy(spec["id"])
            or PROCESS_MANAGER.is_global_busy()
        ):
            item.update(previous_by_id.get(spec["id"], {}))
            item["status"] = "busy"
            sessions.append(item)
            continue

        with session_io(spec["id"], timeout=0) as acquired:
            if not acquired:
                item.update(previous_by_id.get(spec["id"], {}))
                item["status"] = "busy"
                sessions.append(item)
                continue
            client = None
            try:
                client = await connect_any(session_path=spec["session_path"], attempts=1)
                if not await client.is_user_authorized():
                    item["status"] = "unauthorized"
                    sessions.append(item)
                    continue
                me = await client.get_me()
                item["account_name"] = " ".join(
                    part
                    for part in (getattr(me, "first_name", ""), getattr(me, "last_name", ""))
                    if part
                )
                item["username"] = getattr(me, "username", None) or ""
                item["user_id"] = getattr(me, "id", None)
                async for dialog in client.iter_dialogs(limit=None):
                    entity = dialog.entity
                    if not isinstance(entity, User) or getattr(entity, "bot", False):
                        continue
                    if dialog.id in SERVICE_CHAT_IDS:
                        continue
                    item["total_chats"] += 1
                    if dialog.unread_count:
                        item["unread_chats"] += 1
                    dialog_date = getattr(dialog, "date", None)
                    if dialog_date:
                        if dialog_date.tzinfo is None:
                            dialog_date = dialog_date.replace(tzinfo=UTC)
                        if dialog_date.astimezone(UTC) >= cutoff:
                            item["active_chats_30d"] += 1
                item["status"] = "online"
            except Exception as exc:
                item.update(
                    {
                        key: value
                        for key, value in previous_by_id.get(spec["id"], {}).items()
                        if key
                        in {
                            "account_name",
                            "username",
                            "user_id",
                            "total_chats",
                            "active_chats_30d",
                            "unread_chats",
                        }
                    }
                )
                item["status"] = "busy" if "database is locked" in str(exc).lower() else "error"
                item["error"] = type(exc).__name__
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
        sessions.append(item)

    return {
        "updated_at": _utc_now().isoformat(timespec="seconds"),
        "sessions": sessions,
    }


class DashboardState:
    def __init__(self):
        self._lock = threading.RLock()
        self._refreshing = False
        self._snapshot = {"updated_at": None, "sessions": []}
        self._drafts = {}
        self.process_manager = PROCESS_MANAGER
        self.agent_service = AGENT_SERVICE

    def snapshot(self):
        with self._lock:
            result = dict(self._snapshot)
            result["refreshing"] = self._refreshing
            return result

    def refresh_async(self):
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()
        return True

    def _refresh_worker(self):
        try:
            previous = self.snapshot()
            snapshot = asyncio.run(collect_session_snapshot(previous=previous))
            with self._lock:
                self._snapshot = snapshot
        except Exception as exc:
            with self._lock:
                self._snapshot = {
                    "updated_at": _utc_now().isoformat(timespec="seconds"),
                    "sessions": [],
                    "error": type(exc).__name__,
                }
        finally:
            with self._lock:
                self._refreshing = False

    def store_draft(self, draft):
        draft_id = uuid.uuid4().hex
        with self._lock:
            now = time.time()
            self._drafts = {
                key: item for key, item in self._drafts.items() if item["expires_at"] > now
            }
            self._drafts[draft_id] = {**draft, "expires_at": now + 600}
        return draft_id

    def take_draft(self, draft_id):
        with self._lock:
            draft = self._drafts.pop(str(draft_id), None)
        if not draft or draft["expires_at"] <= time.time():
            raise ValueError("Черновик истёк. Подготовь сообщение заново.")
        return draft


def _lead_api_rows(query="", status=None, limit=100):
    records = _load_jsonl(LEADS_PATH)
    needle = str(query or "").lower().strip()
    rows = []
    for record in records:
        if record.get("type") != "username" or record.get("post_kind", "job") not in {
            "job",
            "queued",
        }:
            continue
        if status and record.get("status") != status:
            continue
        value = str(record.get("value") or "")
        haystack = " ".join(
            str(record.get(key) or "") for key in ("value", "source_channel", "note", "snippet")
        ).lower()
        if needle and needle not in haystack:
            continue
        username = value.lstrip("@") if record.get("type") == "username" else ""
        rows.append(
            {
                "type": record.get("type"),
                "value": value,
                "username": username,
                "telegram_url": f"https://t.me/{username}" if username else "",
                "status": record.get("status"),
                "post_kind": record.get("post_kind", "job"),
                "source_channel": record.get("source_channel"),
                "source_date": record.get("source_date"),
                "first_seen": record.get("first_seen"),
                "contacted_at": record.get("contacted_at"),
                "link": record.get("link"),
                "note": record.get("note"),
                "snippet": str(record.get("snippet") or "")[:500],
            }
        )
    rows.sort(
        key=lambda item: str(item.get("last_seen") or item.get("first_seen") or ""), reverse=True
    )
    return rows[: max(1, min(300, _as_int(limit) or 100))]


class DashboardHandler(BaseHTTPRequestHandler):
    dashboard_state = None

    def __init__(self, *args, dashboard_state=None, **kwargs):
        self.dashboard_state = dashboard_state
        super().__init__(*args, **kwargs)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Некорректный Content-Length") from None
        if size < 0 or size > 65536:
            raise ValueError("Размер запроса превышает 64 КБ")
        raw = self.rfile.read(size) if size else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Ожидается JSON в UTF-8") from exc
        if not isinstance(value, dict):
            raise ValueError("Тело запроса должно быть JSON-объектом")
        return value

    def _check_local_request(self):
        host_header = (self.headers.get("Host") or "").lower()
        if host_header.startswith("["):
            host = host_header[1:].split("]", 1)[0]
        else:
            host = host_header.split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            self._send_json({"error": "Доступ разрешён только с localhost."}, status=403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                self._send_json({"error": "Cross-origin запрос запрещён."}, status=403)
                return False
        return True

    def do_GET(self):
        if not self._check_local_request():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        period = query.get("range", ["30d"])[0]
        if period not in {*PERIODS, "all"}:
            period = "30d"
        session_id = query.get("session", ["all"])[0]
        snapshot = self.dashboard_state.snapshot()
        try:
            if parsed.path in {"/", "/index.html"}:
                body = INDEX_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/summary":
                self._send_json(build_summary(period, session_id, snapshot))
                return
            if parsed.path == "/api/timeline":
                self._send_json({"days": build_timeline(period, session_id)})
                return
            if parsed.path == "/api/activity":
                limit = min(200, max(1, _as_int(query.get("limit", ["30"])[0])))
                self._send_json({"items": build_activity(limit, session_id, snapshot)})
                return
            if parsed.path == "/api/sessions":
                self._send_json(snapshot)
                return
            if parsed.path == "/api/leads":
                self._send_json(
                    {
                        "items": _lead_api_rows(
                            query.get("q", [""])[0],
                            query.get("status", [None])[0],
                            query.get("limit", [100])[0],
                        )
                    }
                )
                return
            if parsed.path == "/api/chats":
                result = asyncio.run(
                    list_chats(
                        query.get("session", [session_id])[0],
                        self.dashboard_state.process_manager,
                        query.get("q", [""])[0],
                        query.get("unread", ["0"])[0] == "1",
                        query.get("limit", [100])[0],
                    )
                )
                self._send_json(result)
                return
            if parsed.path == "/api/chat":
                identifier = query.get("id", [""])[0]
                if not identifier:
                    raise ValueError("Не указан id чата")
                result = asyncio.run(
                    get_conversation(
                        identifier,
                        query.get("session", [session_id])[0],
                        self.dashboard_state.process_manager,
                        query.get("limit", [30])[0],
                    )
                )
                self._send_json(result)
                return
            if parsed.path == "/api/processes":
                self._send_json(
                    {
                        **self.dashboard_state.process_manager.list_processes(),
                        "tasks": self.dashboard_state.process_manager.task_catalog(),
                    }
                )
                return
            if parsed.path == "/api/log-sources":
                self._send_json({"items": self.dashboard_state.process_manager.log_sources()})
                return
            if parsed.path == "/api/logs":
                result = self.dashboard_state.process_manager.read_log(
                    query.get("source", ["cron"])[0],
                    query.get("lines", [200])[0],
                )
                self._send_json(result)
                return
            if parsed.path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "telemetry_available": bool(read_events()),
                        "sessions_updated_at": snapshot.get("updated_at"),
                        "refreshing": snapshot.get("refreshing", False),
                        "active_processes": len(
                            self.dashboard_state.process_manager.list_processes()["active"]
                        ),
                    }
                )
                return
            self.send_error(404, "Not found")
        except (ValueError, LookupError, PermissionError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=404)
        except Exception as exc:
            self._send_json(
                {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                status=500,
            )

    def do_POST(self):
        if not self._check_local_request():
            return
        try:
            body = self._read_json()
            path = urlparse(self.path).path
            if path == "/api/refresh":
                started = self.dashboard_state.refresh_async()
                self._send_json({"started": started, "refreshing": True}, status=202)
                return
            if path == "/api/processes/start":
                task_id = str(body.get("task_id") or "")
                task = next(
                    (
                        item
                        for item in self.dashboard_state.process_manager.task_catalog()
                        if item["id"] == task_id
                    ),
                    None,
                )
                if task is None:
                    raise ValueError("Неизвестная операция")
                if task["sends"] and body.get("confirm") is not True:
                    raise PermissionError(
                        "Для запуска отправляющей операции нужно явное подтверждение."
                    )
                result = self.dashboard_state.process_manager.start(
                    task_id,
                    session_id=body.get("session_id"),
                    params=body.get("params") or {},
                )
                self._send_json({"process": result}, status=202)
                return
            if path == "/api/processes/stop":
                result = self.dashboard_state.process_manager.stop(
                    str(body.get("process_id") or "")
                )
                self._send_json({"process": result})
                return
            if path == "/api/chat/draft":
                draft = asyncio.run(
                    prepare_message(
                        body.get("contact"),
                        body.get("goal") or "",
                        body.get("session_id") or "all",
                        self.dashboard_state.process_manager,
                    )
                )
                draft_id = self.dashboard_state.store_draft(draft)
                self._send_json(
                    {
                        "draft_id": draft_id,
                        "mode": draft["mode"],
                        "chat_name": draft["chat_name"],
                        "username": draft.get("username"),
                        "telegram_url": draft.get("telegram_url"),
                        "text": draft["text"],
                    }
                )
                return
            if path == "/api/chat/send":
                draft = self.dashboard_state.take_draft(body.get("draft_id"))
                result = asyncio.run(
                    send_prepared_message(draft, self.dashboard_state.process_manager)
                )
                self._send_json(result)
                return
            if path == "/api/agent/chat":
                snapshot = self.dashboard_state.snapshot()
                stats = build_summary("30d", "all", snapshot)
                result = asyncio.run(
                    self.dashboard_state.agent_service.chat(
                        body.get("message"),
                        conversation_id=body.get("conversation_id"),
                        session_id=body.get("session_id") or "all",
                        stats=stats,
                        selected_contact=body.get("selected_contact"),
                    )
                )
                self._send_json(result)
                return
            if path == "/api/leads/status":
                if CRON_LOCK_PATH.exists() or self.dashboard_state.process_manager.is_global_busy():
                    raise RuntimeError(
                        "База лидов занята автопилотом. Изменение статуса заблокировано."
                    )
                value = str(body.get("value") or "").strip().lower()
                status = str(body.get("status") or "")
                note = str(body.get("note") or "")[:500]
                if status not in VALID_LEAD_STATUSES:
                    raise ValueError("Недопустимый статус лида")
                import leads

                store = leads.load_store()
                key = next(
                    (
                        item_key
                        for item_key, record in store.items()
                        if str(record.get("value") or "").lower() == value
                    ),
                    None,
                )
                if key is None:
                    raise LookupError("Лид не найден")
                record = store[key]
                record["status"] = status
                if status == "contacted" and not record.get("contacted_at"):
                    record["contacted_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                if status == "warm":
                    record["warm_since"] = datetime.now(UTC).isoformat(timespec="seconds")
                    record["warm_recontact_at"] = (datetime.now(UTC) + timedelta(days=20)).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    record["warm_recontact_count"] = 0
                if note:
                    record["note"] = note
                leads.save_store(store)
                record_event("lead_status_changed", status=status, source="admin")
                self._send_json({"ok": True, "value": record["value"], "status": status})
                return
            self.send_error(404, "Not found")
        except (ValueError, LookupError, PermissionError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=404)
        except Exception as exc:
            self._send_json(
                {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                status=500,
            )

    def log_message(self, fmt, *args):
        print(f"[admin] {self.address_string()} - {fmt % args}")


def _refresh_loop(state, interval):
    while True:
        time.sleep(interval)
        state.refresh_async()


def main():
    parser = argparse.ArgumentParser(description="TG Ops — Control Center")
    parser.add_argument("--host", default="127.0.0.1", help="только loopback для безопасности")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error(
            "Админка принимает соединения только с localhost. Для удалённого доступа используй SSH tunnel."
        )

    state = DashboardState()
    state.refresh_async()
    threading.Thread(
        target=_refresh_loop,
        args=(state, max(60, args.refresh_seconds)),
        daemon=True,
    ).start()
    handler = partial(DashboardHandler, dashboard_state=state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"TG Ops Control Center: http://{args.host}:{args.port}")
    print(
        "Безопасность: loopback-only · отправки через humanizer-framework · команды по allow-list"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка TG Ops Control Center…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
