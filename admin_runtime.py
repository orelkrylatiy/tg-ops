"""Safe process controls for the local TG Ops Control Center.

Only predefined project commands can be started. No shell command or arbitrary
argument is accepted from the HTTP API.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ops_telemetry import record_event
from tgcommon import configured_sessions

HERE = Path(__file__).resolve().parent
OPS_DIR = HERE / "ops-data"
LOG_DIR = OPS_DIR / "logs"
CRON_LOCK = HERE / "leads" / ".cron.lock"

_TASKS = {
    "responder": {
        "label": "Автоответчик",
        "description": "Постоянно проверяет входящие и отвечает по базе знаний.",
        "kind": "daemon",
        "sends": True,
        "global": True,
    },
    "responder_once": {
        "label": "Проверить ответы один раз",
        "description": "Один цикл responder без запуска демона.",
        "kind": "once",
        "sends": True,
        "global": True,
    },
    "cron_dry_run": {
        "label": "Проверка автопилота",
        "description": "Полный план сканирования и касаний без отправки сообщений.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
    "cron_run": {
        "label": "Запустить автопилот",
        "description": "Полный цикл: новые HR, сверка, догонки, тёплые и холодные касания.",
        "kind": "once",
        "sends": True,
        "global": True,
    },
    "lead_scan": {
        "label": "Сканировать HR-контакты",
        "description": "Собрать контакты из каналов в локальную базу, без сообщений.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
    "lead_prescribe": {
        "label": "Проверить и прописать HR",
        "description": "Проверить свежие вакансии и добавить подходящих HR в очередь. Не отправляет.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
    "vacancy_scan": {
        "label": "Сканировать вакансии",
        "description": "Сканировать источники вакансий по активному профилю.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
    "reconcile": {
        "label": "Сверить диалоги",
        "description": "Сверить статусы contacted с фактическими ответами в Telegram.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
    "folders_sync": {
        "label": "Синхронизировать папки",
        "description": "Обновить статусные папки и реестр ручных переписок.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
    "followups_preview": {
        "label": "Посмотреть очередь догонок",
        "description": "Показать готовые догонки. По умолчанию ничего не отправляет.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
    "warm_preview": {
        "label": "Посмотреть тёплые возвраты",
        "description": "Показать лиды, которым пора вернуться. По умолчанию ничего не отправляет.",
        "kind": "once",
        "sends": False,
        "global": True,
    },
}

_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _session_lock(session_id):
    key = str(session_id or "userbot")
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def session_io(session_id, timeout=0):
    """Serialize admin-initiated Telegram access for one session."""
    lock = _session_lock(session_id)
    acquired = lock.acquire(timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def _session_spec(session_id=None):
    specs = configured_sessions()
    enabled = [item for item in specs if item.get("enabled", True)]
    if not enabled:
        raise RuntimeError("Нет включённых Telegram-сессий в sessions.json")
    if session_id in (None, "", "all"):
        spec = enabled[0]
    else:
        spec = next((item for item in specs if item["id"] == session_id), None)
        if spec is None:
            raise ValueError("Неизвестная Telegram-сессия")
    if not spec.get("enabled", True):
        raise ValueError("Telegram-сессия отключена")
    if not spec.get("exists"):
        raise FileNotFoundError(f"Файл сессии не найден: {spec['name']}")
    return spec


def _profiles():
    try:
        data = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        return sorted((data.get("profiles") or {}).keys())
    except (OSError, json.JSONDecodeError, AttributeError):
        return ["react"]


def build_command(task_id, params=None):
    """Build an argv list from an allow-listed task and validated scalar options."""
    params = params or {}
    if task_id not in _TASKS:
        raise ValueError("Неизвестная операция")

    profile = str(params.get("profile") or "react")
    if profile not in _profiles():
        raise ValueError("Неизвестный профиль сканирования")

    try:
        limit = int(params.get("limit", 500))
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректная глубина сканирования") from exc
    if not 1 <= limit <= 2000:
        raise ValueError("Глубина сканирования должна быть от 1 до 2000")

    try:
        daily_cap = int(params.get("daily_cap", 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный суточный лимит") from exc
    if not 0 <= daily_cap <= 30:
        raise ValueError("Суточный лимит должен быть от 0 до 30")

    base = [sys.executable]
    if task_id == "responder":
        return base + ["responder.py"]
    if task_id == "responder_once":
        return base + ["responder.py", "--once"]
    if task_id == "cron_dry_run":
        return base + ["cron-run.py", "--dry-run", "--profile", profile]
    if task_id == "cron_run":
        return base + ["cron-run.py", "--profile", profile, "--daily-cap", str(daily_cap)]
    if task_id == "lead_scan":
        return base + ["leads.py", "scan", "--profile", profile, "--limit", str(limit)]
    if task_id == "lead_prescribe":
        return base + [
            "leads.py",
            "prescribe",
            "--profile",
            profile,
            "--limit",
            str(limit),
            "--daily-new-limit",
            "20",
        ]
    if task_id == "vacancy_scan":
        return base + ["scan_jobs.py", "scan", "--profile", profile, "-n", str(limit)]
    if task_id == "reconcile":
        return base + ["leads.py", "reconcile"]
    if task_id == "folders_sync":
        return base + ["folders.py", "sync"]
    if task_id == "followups_preview":
        return base + ["leads.py", "followups"]
    if task_id == "warm_preview":
        return base + ["leads.py", "warm-due"]
    raise ValueError("Операция не реализована")


class ProcessManager:
    """Tracks child processes, logs, and safe stop/start operations."""

    def __init__(self):
        self._lock = threading.RLock()
        self._active = {}
        self._history = deque(maxlen=60)

    def _refresh_locked(self):
        for record in list(self._active.values()):
            process = record["process"]
            code = process.poll()
            if code is None:
                continue
            record["status"] = "completed" if code == 0 else "failed"
            record["exit_code"] = code
            record["finished_at"] = _now()
            record["process"] = None
            self._history.appendleft(self._public_record(record))
            record_event(
                "process_exit",
                task_id=record["task_id"],
                process_id=record["id"],
                exit_code=code,
                session_id=record["session_id"],
            )
            self._active.pop(record["id"], None)

    @staticmethod
    def _public_record(record):
        return {
            "id": record["id"],
            "task_id": record["task_id"],
            "label": record["label"],
            "session_id": record["session_id"],
            "pid": record["pid"],
            "status": record["status"],
            "started_at": record["started_at"],
            "finished_at": record.get("finished_at"),
            "exit_code": record.get("exit_code"),
            "log_name": record["log_path"].name,
            "sends": record["sends"],
            "kind": record["kind"],
        }

    def is_session_busy(self, session_id):
        with self._lock:
            self._refresh_locked()
            return any(item["session_id"] == session_id for item in self._active.values())

    def is_global_busy(self):
        with self._lock:
            self._refresh_locked()
            return any(_TASKS[item["task_id"]].get("global") for item in self._active.values())

    def list_processes(self):
        with self._lock:
            self._refresh_locked()
            active = [self._public_record(item) for item in self._active.values()]
            external_lock = self._external_cron_lock()
            if external_lock and any(item["pid"] == external_lock.get("pid") for item in active):
                external_lock = None
            return {
                "active": active,
                "history": list(self._history),
                "external_cron_lock": external_lock,
            }

    def task_catalog(self):
        return [
            {
                "id": task_id,
                "label": spec["label"],
                "description": spec["description"],
                "kind": spec["kind"],
                "sends": bool(spec.get("sends")),
            }
            for task_id, spec in _TASKS.items()
        ]

    @staticmethod
    def _external_cron_lock():
        if not CRON_LOCK.exists():
            return None
        try:
            raw = CRON_LOCK.read_text(encoding="utf-8").strip()
            pid = int(raw) if raw.isdigit() else None
            if pid:
                try:
                    os.kill(pid, 0)
                    return {"pid": pid, "status": "running"}
                except PermissionError:
                    return {"pid": pid, "status": "running"}
                except ProcessLookupError:
                    pass
            age = max(0, int(time.time() - CRON_LOCK.stat().st_mtime))
            return {"pid": pid, "status": "stale", "age_seconds": age}
        except OSError:
            return {"status": "unknown"}

    def start(self, task_id, session_id=None, params=None):
        params = params or {}
        command = build_command(task_id, params)
        spec = _session_spec(session_id)
        selected_id = spec["id"]
        with self._lock:
            self._refresh_locked()
            external_lock = self._external_cron_lock()
            if external_lock and external_lock["status"] == "running":
                raise RuntimeError(
                    f"Автопилот уже работает вне админки (PID {external_lock['pid']})."
                )
            if external_lock and external_lock["status"] == "stale":
                age = int(external_lock.get("age_seconds", 0))
                if age <= 3 * 3600:
                    raise RuntimeError(
                        "Найден устаревший leads/.cron.lock. Дождись 3 часов с момента "
                        "создания lock; до этого операции заблокированы."
                    )
                CRON_LOCK.unlink(missing_ok=True)
            if task_id == "cron_run" and self._active:
                raise RuntimeError("Перед автопилотом останови остальные процессы TG Ops.")
            if task_id != "cron_run" and self.is_global_busy():
                raise RuntimeError("Сейчас работает глобальная операция. Дождись её завершения.")
            if any(item["session_id"] == selected_id for item in self._active.values()):
                raise RuntimeError("Для этой Telegram-сессии уже запущен процесс.")
            if task_id in {"responder", "responder_once"} and any(
                item["task_id"] in {"responder", "responder_once"} for item in self._active.values()
            ):
                raise RuntimeError("Responder уже запущен. Второй экземпляр использовать нельзя.")

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            process_id = f"{task_id}-{uuid.uuid4().hex[:8]}"
            log_path = LOG_DIR / f"{process_id}.log"
            env = os.environ.copy()
            env["TG_OPS_SESSION"] = spec["session_path"]
            env["TG_OPS_SESSION_ID"] = selected_id
            env["PYTHONUNBUFFERED"] = "1"
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=HERE,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=(os.name != "nt"),
                    creationflags=creationflags,
                )

            record = {
                "id": process_id,
                "task_id": task_id,
                "label": _TASKS[task_id]["label"],
                "session_id": selected_id,
                "pid": process.pid,
                "process": process,
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "exit_code": None,
                "log_path": log_path,
                "sends": bool(_TASKS[task_id].get("sends")),
                "kind": _TASKS[task_id]["kind"],
            }
            self._active[process_id] = record
            record_event(
                "process_started",
                task_id=task_id,
                process_id=process_id,
                pid=process.pid,
                session_id=selected_id,
            )
            return self._public_record(record)

    def stop(self, process_id):
        with self._lock:
            self._refresh_locked()
            record = self._active.get(process_id)
            if record is None:
                raise ValueError("Активный процесс не найден")
            process = record["process"]
            if process.poll() is not None:
                self._refresh_locked()
                raise ValueError("Процесс уже завершился")
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                    process.wait(timeout=5)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
            except ProcessLookupError:
                pass
            record["status"] = "stopped"
            record["finished_at"] = _now()
            record["exit_code"] = process.poll()
            record["process"] = None
            self._history.appendleft(self._public_record(record))
            self._active.pop(process_id, None)
            record_event(
                "process_stopped",
                task_id=record["task_id"],
                process_id=process_id,
                session_id=record["session_id"],
            )
            return self._public_record(record)

    def log_sources(self):
        sources = [
            {
                "id": "cron",
                "label": "Автопилот",
                "path": HERE / "leads" / "cron-run.log",
            },
            {
                "id": "events",
                "label": "Журнал событий",
                "path": OPS_DIR / "events.jsonl",
            },
        ]
        with self._lock:
            self._refresh_locked()
            for record in self._active.values():
                sources.append(
                    {
                        "id": f"process:{record['id']}",
                        "label": record["label"],
                        "path": record["log_path"],
                    }
                )
            for record in self._history:
                sources.append(
                    {
                        "id": f"process:{record['id']}",
                        "label": f"{record['label']} · завершено",
                        "path": LOG_DIR / record["log_name"],
                    }
                )
        return [
            {"id": item["id"], "label": item["label"], "available": item["path"].is_file()}
            for item in sources
        ]

    def read_log(self, source_id, lines=200):
        try:
            limit = max(10, min(1000, int(lines)))
        except (TypeError, ValueError):
            limit = 200
        known = {
            "cron": HERE / "leads" / "cron-run.log",
            "events": OPS_DIR / "events.jsonl",
        }
        if source_id.startswith("process:"):
            process_id = source_id.split(":", 1)[1]
            if not process_id or any(part in process_id for part in ("/", "\\", "..")):
                raise ValueError("Некорректный идентификатор лога")
            known[source_id] = LOG_DIR / f"{process_id}.log"
        path = known.get(source_id)
        if path is None:
            raise ValueError("Неизвестный источник логов")
        if not path.is_file():
            return {"source": source_id, "lines": [], "available": False}
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                tail = list(deque(handle, maxlen=limit))
        except OSError as exc:
            raise RuntimeError(f"Не удалось прочитать лог: {type(exc).__name__}") from exc
        return {
            "source": source_id,
            "lines": [line.rstrip("\r\n") for line in tail],
            "available": True,
            "path_name": path.name,
        }
