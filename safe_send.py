"""safe_send.py — рассылка first-touch только тем, с кем «чисто».

Канон 26.11: перед любой рассылкой читаем РЕАЛЬНЫЕ диалоги сессии и шлём
только в пустые чаты. Живой диалог, наше последнее сообщение, старое
касание и несуществующий юзернейм — скип с причиной. Повод: 25.11 шаблоны
2R ушли в живые диалоги, потому что guard tg.py проверяет только последнее
сообщение, а журналов отправок акк2 не существует.

Проверка без отправки (read-only):
    py safe_send.py --outbox-dir .outbox/2r-first-touch-20261120 --check-only
    py safe_send.py --users @a @b --check-only

Отправка «чистым»:
    py safe_send.py --outbox-dir .outbox/2r-first-touch-20261120 --send
    py safe_send.py --users @a @b --text-file draft.txt --send

Отчёт: leads/safe-send-<timestamp>.json (или --report ПУТЬ).
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime

import tgcommon

OUTBOX_RE = re.compile(r"^(\d+)-([a-z]+)-(.+)-validated\.txt$")

LABELS = {
    "clean": "ЧИСТО",
    "live": "ЖИВОЙ ДИАЛОГ",
    "warm": "ПОСЛЕДНЕЕ НАШЕ",
    "cold": "СТАРОЕ КАСАНИЕ",
    "not_found": "НЕ НАЙДЕН",
}


def parse_args():
    p = argparse.ArgumentParser(
        description="Рассылка только тем, с кем «чисто»: пустой чат без единого сообщения"
    )
    p.add_argument("--users", nargs="*", default=[], help="юзернеймы (с @ или без)")
    p.add_argument("--outbox-dir", default=None, help="папка драфтов NN-kind-user-validated.txt")
    p.add_argument("--text", default=None, help="текст для --users")
    p.add_argument("--text-file", default=None, help="файл с текстом для --users")
    p.add_argument("--send", action="store_true", help="отправить «чистым»")
    p.add_argument("--check-only", action="store_true", help="только классификация, без отправок")
    p.add_argument("--stale-days", type=int, default=3, help="их сообщение свежее N дней = живой диалог")
    p.add_argument("--limit-msgs", type=int, default=5, help="сколько сообщений читать на чат")
    p.add_argument("--pause", type=float, default=8.0, help="пауза между отправками, сек")
    p.add_argument("--session", default=None, help="имя сессии (иначе TG_SESSION или userbot)")
    p.add_argument("--report", default=None, help="путь JSON-отчёта")
    return p.parse_args()


def load_targets(args):
    """[(user, text, source)] из --users или --outbox-dir."""
    targets = []
    if args.outbox_dir:
        for fname in sorted(os.listdir(args.outbox_dir)):
            m = OUTBOX_RE.match(fname)
            if not m:
                continue
            path = os.path.join(args.outbox_dir, fname)
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            targets.append((m.group(3), text, fname))
    for user in args.users:
        text = args.text
        if text is None and args.text_file:
            with open(args.text_file, encoding="utf-8") as fh:
                text = fh.read().strip()
        targets.append((user.lstrip("@"), text, "--users"))
    if not targets:
        sys.exit("Нет получателей: передай --users или --outbox-dir")
    if args.send and not args.check_only:
        missing = [t[0] for t in targets if not t[1]]
        if missing:
            joined = ", ".join(missing)
            sys.exit(f"Нет текста для: {joined} — добавь --text/--text-file")
    return targets


def classify(msgs, stale_days, now):
    """(status, last_date, last_out, age_days) по последним сообщениям чата."""
    if not msgs:
        return "clean", None, None, None
    last = msgs[0]
    age_days = (now - last.date).total_seconds() / 86400
    if last.out:
        return "warm", last.date, True, age_days
    if age_days <= stale_days:
        return "live", last.date, False, age_days
    return "cold", last.date, False, age_days


async def main():
    args = parse_args()
    session_name = args.session or os.environ.get("TG_SESSION", "userbot")
    if os.path.basename(session_name) != session_name:
        sys.exit("--session/TG_SESSION — только имя сессии, без пути")
    session_path = os.path.join(tgcommon.HERE, session_name)
    targets = load_targets(args)
    client = await tgcommon.connect_any(session_path)
    now = datetime.now(UTC)
    not_found_label = LABELS["not_found"]
    rows = []
    sent = 0
    try:
        for user, text, source in targets:
            row = {"user": f"@{user}", "source": source, "status": None}
            try:
                ent = await client.get_entity(user)
            except Exception as exc:
                row["status"] = "not_found"
                row["error"] = type(exc).__name__
                rows.append(row)
                print(f"@{user}: {not_found_label} ({type(exc).__name__})")
                continue
            msgs = await client.get_messages(ent, limit=args.limit_msgs)
            status, last_date, last_out, age = classify(msgs, args.stale_days, now)
            chat_name = getattr(ent, "first_name", None) or getattr(ent, "title", None)
            row.update(
                {
                    "status": status,
                    "chat_name": chat_name,
                    "last_date": f"{last_date:%Y-%m-%d %H:%M}" if last_date else None,
                    "last_out": last_out,
                    "age_days": round(age, 1) if age is not None else None,
                    "messages_seen": len(msgs),
                }
            )
            note = ""
            if last_date:
                who = "мы" if last_out else "они"
                note = f" (последнее {last_date:%d.%m %H:%M}, {who}, {age:.0f} дн.)"
            print(f"@{user}: {LABELS[status]}{note}")
            if status == "clean" and args.send and not args.check_only:
                msg = await client.send_message(ent, text)
                row["sent_msg_id"] = msg.id
                sent += 1
                print(f"    → отправлено (msg_id={msg.id})")
                if args.pause > 0:
                    time.sleep(args.pause)
            rows.append(row)
    finally:
        await client.disconnect()

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = ", ".join(f"{LABELS[k]}: {v}" for k, v in sorted(counts.items()))
    print(f"\nИтог: {summary}; отправлено: {sent}")
    report_path = args.report or os.path.join(
        tgcommon.HERE, "leads", f"safe-send-{datetime.now():%Y%m%d-%H%M}.json"
    )
    report = {
        "ts": datetime.now(UTC).isoformat(),
        "session": session_name,
        "stale_days": args.stale_days,
        "send_mode": bool(args.send and not args.check_only),
        "sent": sent,
        "rows": rows,
    }
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"Отчёт: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
