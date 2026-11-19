"""Local read-only operations dashboard for TG Ops.

Run: python admin.py [--host 127.0.0.1] [--port 8765]
The dashboard reads local JSONL state and periodically checks configured Telegram sessions.
It never sends messages, changes folders, or mutates lead records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telethon.tl.types import User

from ops_telemetry import read_events
from tgcommon import HERE, SERVICE_CHAT_IDS, configured_sessions, connect_any

LEADS_PATH = Path(HERE) / "leads" / "leads.jsonl"
VACANCIES_PATH = Path(HERE) / "vacancies" / "vacancies.jsonl"
INDEX_PATH = Path(HERE) / "admin" / "index.html"
PERIODS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


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
        with path.open("r", encoding="utf-8") as handle:
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
        else:
            statuses[record.get("status") or "unknown"] += 1
    return {
        "leads_total": len(leads),
        "leads_by_status": dict(statuses),
        "leads_by_type": dict(types),
        "leads_queued": queued,
        "leads_review": review,
        "vacancies_total": len(vacancies),
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
        elif kind == "operation_failed":
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
    events = _filter_events(read_events(), period, session_id)
    sessions = _selected_sessions(snapshot, session_id)
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
        "available_sessions": sum(item.get("status") == "online" for item in sessions),
        "total_chats": sum(_as_int(item.get("total_chats")) for item in live_sessions),
        "active_chats_30d": sum(
            _as_int(item.get("active_chats_30d")) for item in live_sessions
        ),
        "unread_chats": sum(_as_int(item.get("unread_chats")) for item in live_sessions),
    }
    return {
        "generated_at": _utc_now().isoformat(timespec="seconds"),
        "period": period,
        "session_filter": session_id,
        "metrics": _event_metrics(events),
        "live": live,
        "pipeline": _pipeline_snapshot(),
        "sessions": sessions,
        "sessions_updated_at": snapshot.get("updated_at"),
        "refreshing": bool(snapshot.get("refreshing")),
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
    return list(buckets.values())


def _activity_item(event, session_names):
    kind = event.get("event")
    category = event.get("category")
    if kind == "message_sent":
        labels = {
            "manual": "Сообщение отправлено",
            "manual_reply": "Ответ отправлен",
            "cold_outreach": "Холодное касание",
            "followup": "Догонка отправлена",
            "warm_return": "Тёплый возврат",
            "auto_reply": "Автоответ отправлен",
            "manual_bridge": "Ответ через бота",
        }
        title = labels.get(category, "Исходящее сообщение")
        detail = event.get("chat_name") or str(event.get("chat_id") or "Чат")
    elif kind == "resume_sent":
        title = "Резюме отправлено"
        detail = event.get("chat_name") or str(event.get("chat_id") or "Чат")
    elif kind == "inbound_message":
        title = "Входящее сообщение"
        detail = event.get("chat_name") or str(event.get("chat_id") or "Чат")
    elif kind == "file_sent":
        title = "Файл отправлен"
        detail = event.get("chat_name") or str(event.get("chat_id") or "Чат")
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
    elif kind == "cron_step":
        title = "Шаг автопилота"
        detail = str(event.get("step") or "операция")
    elif kind == "operation_failed":
        title = "Ошибка операции"
        detail = str(event.get("operation") or event.get("error_type") or "ошибка")
    else:
        title = str(kind or "Событие")
        detail = str(event.get("detail") or "")
    timestamp = _event_timestamp(event)
    return {
        "ts": timestamp.isoformat() if timestamp else event.get("ts"),
        "title": title,
        "detail": detail,
        "session_id": event.get("session_id", "userbot"),
        "session_name": session_names.get(event.get("session_id", "userbot"), "Сессия"),
        "category": category or kind,
        "status": event.get("status") or ("success" if kind != "operation_failed" else "error"),
    }


def build_activity(limit, session_id, snapshot):
    events = _filter_events(read_events(), "all", session_id)
    names = {item.get("id"): item.get("name") for item in snapshot.get("sessions") or []}
    events.sort(
        key=lambda item: _event_timestamp(item) or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    return [_activity_item(event, names) for event in events[:limit]]


async def collect_session_snapshot():
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
            snapshot = asyncio.run(collect_session_snapshot())
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


class DashboardHandler(BaseHTTPRequestHandler):
    dashboard_state = None

    def __init__(self, *args, dashboard_state=None, **kwargs):
        self.dashboard_state = dashboard_state
        super().__init__(*args, **kwargs)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        period = query.get("range", ["30d"])[0]
        if period not in {*PERIODS, "all"}:
            period = "30d"
        session_id = query.get("session", ["all"])[0]
        snapshot = self.dashboard_state.snapshot()

        if parsed.path in {"/", "/index.html"}:
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                self.send_error(404, "admin/index.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
        if parsed.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "telemetry_available": bool(read_events()),
                    "sessions_updated_at": snapshot.get("updated_at"),
                    "refreshing": snapshot.get("refreshing", False),
                }
            )
            return
        self.send_error(404, "Not found")

    def do_POST(self):
        if urlparse(self.path).path != "/api/refresh":
            self.send_error(404, "Not found")
            return
        started = self.dashboard_state.refresh_async()
        self._send_json({"started": started, "refreshing": True}, status=202)

    def log_message(self, fmt, *args):
        print(f"[admin] {self.address_string()} - {fmt % args}")


def _refresh_loop(state, interval):
    while True:
        time.sleep(interval)
        state.refresh_async()


def main():
    parser = argparse.ArgumentParser(description="TG Ops — локальная операционная админка")
    parser.add_argument("--host", default="127.0.0.1", help="по умолчанию доступна только локально")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-seconds", type=int, default=300)
    args = parser.parse_args()

    state = DashboardState()
    state.refresh_async()
    threading.Thread(
        target=_refresh_loop,
        args=(state, max(60, args.refresh_seconds)),
        daemon=True,
    ).start()
    handler = partial(DashboardHandler, dashboard_state=state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"TG Ops Admin: http://{args.host}:{args.port}")
    print("Режим: read-only · статистика не содержит текстов сообщений")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "ВНИМАНИЕ: внешний bind без встроенной авторизации. Используй только за защищённым VPN/proxy."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка TG Ops Admin…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
