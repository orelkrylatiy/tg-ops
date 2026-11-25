"""Email outreach for scanned vacancy contacts.

Generates each letter through the existing tg-ops humanizer and sends only
after explicit ``--send``.  The default is a dry-run.  Recipients are stored
in a SQLite ledger to prevent repeat contact across runs.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sqlite3
import ssl
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import humanize

try:
    import fcntl
except ImportError:  # Windows local dry-runs do not need flock.
    fcntl = None

ROOT = Path(__file__).resolve().parent
LEADS = ROOT / "leads" / "leads.jsonl"
LEDGER = ROOT / "leads" / "email-outreach.sqlite"
URL_RE = re.compile(r"https?://[^\s)]+")


def artifact_link(row):
    """Prefer an external vacancy URL; fall back to the source Telegram post."""
    for url in URL_RE.findall(row.get("snippet") or ""):
        clean = url.rstrip(".,;]")
        if "t.me/" not in clean and "telegram.me/" not in clean:
            return clean
    return row.get("link") or ""


def email_paragraphs(text: str) -> str:
    """Restore readable email paragraphs after the humanizer's compact output."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if part.strip()]
    if len(sentences) <= 2:
        return "\n\n".join(sentences)
    groups = [" ".join(sentences[:2])]
    groups.extend(sentences[2:])
    return "\n\n".join(groups)


def rows(max_age_days: int, role: str = "react"):
    cutoff = datetime.now() - timedelta(days=max_age_days) if max_age_days else None
    selected = []
    for line in LEADS.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "email" or row.get("status") != "new" or row.get("post_kind") != "job":
            continue
        if role == "analyst":
            context = (row.get("role_contexts") or {}).get("analyst")
            if not context:
                continue
            row = {**row, **context}
        elif role not in row.get("roles", [row.get("role", "react")]):
            continue
        try:
            posted = datetime.fromisoformat(str(row.get("source_date") or "").replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        if cutoff is not None and posted < cutoff:
            continue
        selected.append(row)
    return sorted(selected, key=lambda row: row.get("source_date") or "", reverse=True)


def db():
    conn = sqlite3.connect(LEDGER)
    conn.execute("""CREATE TABLE IF NOT EXISTS email_sends (
        recipient TEXT PRIMARY KEY COLLATE NOCASE,
        subject TEXT NOT NULL,
        status TEXT NOT NULL,
        source TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        sent_at TEXT
    )""")
    conn.commit()
    return conn


def smtp_config():
    configured = os.environ.get("TG_GMAIL_DIR")
    path = Path(configured) / "config.json" if configured else Path.home() / ".mail" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    smtp = dict(data.get("smtp") or {})
    smtp.setdefault("user", data.get("email"))
    smtp.setdefault("from", data.get("email"))
    smtp.setdefault("password", data.get("app_password"))
    if not smtp.get("host") or not smtp.get("port") or not smtp.get("user") or not smtp.get("password"):
        raise RuntimeError("shared Gmail SMTP configuration is incomplete")
    return smtp


def send_mail(cfg, recipient, subject, body, resume_path=None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["from"], recipient, subject
    msg.set_content(body)
    if resume_path:
        path = Path(resume_path)
        msg.add_attachment(
            path.read_bytes(), maintype="application", subtype="pdf", filename=path.name
        )
    if cfg.get("ssl", False) or int(cfg["port"]) == 465:
        with smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), context=ssl.create_default_context(), timeout=30) as server:
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)


def main():
    ap = argparse.ArgumentParser(description="Humanizer email outreach for scanned vacancy leads")
    ap.add_argument("-n", "--limit", type=int, default=None)
    ap.add_argument("--max-age-days", type=int, default=30, help="не брать вакансии старше N дней; 0 = без ограничения")
    ap.add_argument("--send", action="store_true", help="actually send; default is dry-run")
    ap.add_argument("--quiet", action="store_true", help="не печатать адреса и текст писем в лог")
    ap.add_argument("--subject", default="Отклик на вакансию")
    ap.add_argument("--resume", default=None, help="PDF резюме to attach")
    ap.add_argument(
        "--role",
        choices=["react", "ml", "ai", "analyst"],
        default="react",
        help="роль лидов и позиция писем (react|ml|ai|analyst)",
    )
    args = ap.parse_args()
    lock_handle = None
    if fcntl is not None:
        lock_handle = open(ROOT / "leads" / ".email-outreach.lock", "w", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Email outreach уже выполняется — выходим")
            return 0
    conn = db()
    candidates = rows(args.max_age_days, args.role)
    selected = candidates[: args.limit] if args.limit is not None else candidates
    if not selected:
        print("Нет новых email-лидов, подходящих под фильтры")
        print("EMAIL_SENT=0")
        return 0
    cfg = smtp_config() if args.send else None
    default_resumes = {
        "react": "resume/resume-react.pdf",
        "ml": "resume/resume-ml.pdf",
        "ai": "resume/resume-ai.pdf",
        "analyst": None,
    }
    resume_value = args.resume or default_resumes[args.role]
    resume_path = Path(resume_value) if resume_value else None
    if resume_path and not resume_path.is_file():
        raise SystemExit(f"Резюме не найдено: {resume_path}")
    sent = failed = 0
    for row in selected:
        recipient = row["value"].strip().lower()
        if conn.execute("SELECT 1 FROM email_sends WHERE recipient=? AND status IN ('pending','sent')", (recipient,)).fetchone():
            print("SKIP email lead: already in send ledger")
            continue
        vacancy = row.get("snippet") or ""
        try:
            body = email_paragraphs(humanize.generate_outreach(
                vacancy, lead=recipient, channel_name=row.get("source_channel") or ""
            ))
            artifact = artifact_link(row)
            if artifact:
                body = f"{body.strip()}\n\nВакансия: {artifact}"
            if resume_path:
                body = f"{body}\n\nРезюме прикрепил к письму."
            body = f"{body.rstrip()}\n\nTelegram: @maxxffly"
        except Exception as exc:
            print(f"FAIL email lead: humanizer: {exc}")
            failed += 1
            continue
        if not args.quiet:
            print(f"--- email lead\n{body}\n")
        if args.send:
            conn.execute("INSERT OR REPLACE INTO email_sends(recipient,subject,status,source) VALUES(?,?,?,?)", (recipient, args.subject, "pending", row.get("link")))
            try:
                send_mail(cfg, recipient, args.subject, body, resume_path)
                conn.execute("UPDATE email_sends SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE recipient=?", (recipient,))
                sent += 1
                print("SENT email lead")
            except Exception as exc:
                conn.execute("UPDATE email_sends SET status='failed' WHERE recipient=?", (recipient,))
                failed += 1
                print(f"FAIL email lead: smtp: {type(exc).__name__}")
    conn.commit()
    print(f"Итог: подготовлено {len(selected)}, отправлено {sent}, ошибок {failed}, режим={'send' if args.send else 'dry-run'}")
    print(f"EMAIL_SENT={sent}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
