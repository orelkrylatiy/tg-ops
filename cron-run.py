"""Крон-автопилот tg-ops: прописка → догонки → холодные, одним прогоном.

Скрипт выполняет автономный цикл сбора и отправки. Планировщик запускает
cron-run.py; журнал сохраняется в leads/cron-run.log.

Гарантии одного прогона:
  • лок leads/.cron.lock — один писатель базы (урок инцидента с порчей 21.10);
    протухший лок (3 ч+) снимается сам;
  • суточный лимит отправок (--daily-cap, дефолт 10: спам-лимиты ТГ 12/16/21.10);
  • при ошибке спам-лимита/флуда — исходящие останавливаются лишь в текущем прогоне
    (следующий запуск снова пытается отправку, без самоназначенной паузы);
  • ручные треды (leads/manual-threads.json) не трогаются: фильтр внутри
    followups, preflight перед humanize и финальный guard внутри outreach;
  • догонка максимум одна на лида (followup_count), дубль холодки невозможен
    (статус new + guard треда).

  py cron-run.py                 # полный прогон: скан → догонки → холодные
  py cron-run.py --dry-run       # всё то же, но без фактических отправок
  py cron-run.py --prescribe-cap 20 --cold 3 --followup 3 --daily-cap 6
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from admin.scan_reports import append_report

HERE = os.path.dirname(os.path.abspath(__file__))
LEADS = os.path.join(HERE, "leads")
STATE = os.path.join(LEADS, "cron-state.json")
LOCK = os.path.join(LEADS, ".cron.lock")
REPORT_FU = os.path.join(LEADS, "cron-report-followups.json")
REPORT_WARM = os.path.join(LEADS, "cron-report-warm.json")
REPORT_COLD = os.path.join(LEADS, "cron-report-outreach.json")
MAP = os.path.join(LEADS, "cron-outreach-map.json")

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def stamp():
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)


def acquire_lock():
    os.makedirs(LEADS, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(LOCK)
            except OSError:
                continue
            if age > 3 * 3600:
                print(f"[{stamp()}] снимаю протухший лок ({age / 3600:.1f} ч)")
                try:
                    os.remove(LOCK)
                except OSError:
                    return False
                continue
            return False
    return False


def run_step(name, cmd):
    print(f"\n=== {name}: py {' '.join(cmd)}")
    p = subprocess.run(
        [sys.executable] + cmd,
        cwd=HERE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if (p.stdout or "").strip():
        print(p.stdout.rstrip())
    if (p.stderr or "").strip():
        print(f"[stderr] {p.stderr.strip()[:1000]}")
    if p.returncode:
        FAILED_STEPS.append(name)
    return p


FAILED_STEPS = []


def read_report(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def stop_outbound_for_run(st, reason):
    """Record an outbound error without suppressing future scheduled runs."""
    st["last_outbound_error"] = reason
    st["last_outbound_error_at"] = f"{datetime.now():%Y-%m-%d %H:%M}"
    print(
        f"!! спам-лимит/флуд ({reason}) — исходящие остановлены только "
        "для текущего прогона; следующий запуск попробует снова"
    )


def main():
    ap = argparse.ArgumentParser(description="Крон-автопилот tg-ops (прописка+догонки+холодные)")
    ap.add_argument("--profile", default="react", help="основной профиль каналов config.json")
    ap.add_argument("--also-profile", action="append", default=[], help="дополнительный профиль; можно повторять")
    ap.add_argument("--scan-limit", type=int, default=500, help="глубина скана на канал")
    ap.add_argument(
        "--prescribe-cap",
        type=int,
        default=20,
        help="суточный лимит новых Telegram HR в активной очереди",
    )
    ap.add_argument("--followup", type=int, default=5, help="макс. догонок за прогон")
    ap.add_argument("--warm", type=int, default=2, help="макс. тёплых возвратов за прогон")
    ap.add_argument("--cold", type=int, default=5, help="макс. холодных за прогон")
    ap.add_argument("--daily-cap", type=int, default=10, help="суточный лимит отправок (все типы)")
    ap.add_argument("--dry-run", action="store_true", help="без фактических отправок")
    args = ap.parse_args()
    profiles = list(dict.fromkeys([args.profile, *args.also_profile]))

    print(
        f"[{stamp()}] cron-run start: dry_run={args.dry_run}, "
        f"new_hr<={args.prescribe_cap}/day, followup<={args.followup}, "
        f"cold<={args.cold}, send_cap={args.daily_cap}"
    )
    if not acquire_lock():
        print(f"[{stamp()}] !! другой прогон держит лок {LOCK} — выходим")
        return 1

    st = load_state()
    if st.get("date") != f"{datetime.now():%Y-%m-%d}":
        st = {
            "date": f"{datetime.now():%Y-%m-%d}",
            "sent_cold": 0,
            "sent_followup": 0,
            "sent_warm": 0,
            "last_outbound_error": "",
            "last_outbound_error_at": "",
        }
    run_completed = False
    try:
        started_at = datetime.now().astimezone().isoformat()
        sent_before = {
            "followup": st.get("sent_followup", 0),
            "warm": st.get("sent_warm", 0),
            "cold": st.get("sent_cold", 0),
        }
        FAILED_STEPS.clear()
        # Каждый профиль получает отдельный скан вакансий и HR-прописку.
        for profile_name in profiles:
            run_step(
                f"скан вакансий ({profile_name})",
                ["scan_jobs.py", "scan", "--profile", profile_name, "--limit", str(args.scan_limit)],
            )
            run_step(
                f"прописка ({profile_name})",
                [
                    "leads.py", "prescribe", "--profile", profile_name,
                    "--limit", str(args.scan_limit),
                    "--daily-new-limit", str(args.prescribe_cap),
                ],
            )

        # Свежие вакансии могут содержать email-контакты. Рассылка проходит
        # через humanizer, Gmail SMTP и отдельный лимит с recipient-ledger.
        sent_emails = 0
        for email_role in profiles:
            if email_role not in ("react", "ml", "ai", "analyst"):
                continue
            email_cmd = [
                "email_outreach.py", "--max-age-days", "30",
                "--role", email_role, "--quiet",
            ]
            if not args.dry_run:
                email_cmd.append("--send")
            email_run = run_step(f"email-лиды ({email_role})", email_cmd)
            for line in (email_run.stdout or "").splitlines():
                match = re.fullmatch(r"EMAIL_SENT=(\d+)", line.strip())
                if match:
                    sent_emails += int(match.group(1))
                    break

        # Живые ЛС — источник истины: свежий ответ сразу переводит холодок в
        # диалог, а не ждёт наступления окна догонки.
        reconcile_cmd = ["leads.py", "reconcile"]
        if args.dry_run:
            reconcile_cmd.append("--dry-run")
        run_step("сверка ЛС", reconcile_cmd)

        budget = (
            args.daily_cap
            - st.get("sent_cold", 0)
            - st.get("sent_followup", 0)
            - st.get("sent_warm", 0)
        )
        if budget <= 0:
            print(
                f"[{stamp()}] суточный лимит отправок {args.daily_cap} исчерпан; "
                "скан, сверка ЛС и папки всё равно выполняются"
            )

        # 2) догонки: окно 2–3 дня горит, им приоритет перед холодными
        fu_budget = min(args.followup, budget)
        if fu_budget > 0:
            cmd = ["leads.py", "followups", "--limit", str(fu_budget), "--report-json", REPORT_FU]
            if not args.dry_run:
                cmd.append("--send")
            run_step("догонки", cmd)
            fu = read_report(REPORT_FU)
            st["sent_followup"] += int(fu.get("sent", 0))
            budget -= int(fu.get("sent", 0))
            if fu.get("stopped_reason") in ("spam", "flood"):
                stop_outbound_for_run(st, fu["stopped_reason"])
                budget = 0
        else:
            print("\n=== догонки: бюджет исчерпан")

        # 2.5) тёплые возвраты: «вакансий нет» → касание через 20 дней (спека §3)
        warm_budget = min(args.warm, budget)
        if warm_budget > 0:
            cmd = [
                "leads.py",
                "warm-due",
                "--limit",
                str(warm_budget),
                "--report-json",
                REPORT_WARM,
            ]
            if not args.dry_run:
                cmd.append("--send")
            run_step("тёплые возвраты", cmd)
            wm = read_report(REPORT_WARM)
            st["sent_warm"] = st.get("sent_warm", 0) + int(wm.get("sent", 0))
            budget -= int(wm.get("sent", 0))
            if wm.get("stopped_reason") in ("spam", "flood"):
                stop_outbound_for_run(st, wm["stopped_reason"])
                budget = 0
        else:
            print("\n=== тёплые возвраты: бюджет исчерпан")

        # 3) холодные: генерация под каждого + отправка на остаток лимита
        cold_budget = min(args.cold, budget)
        if cold_budget > 0:
            if os.path.exists(MAP):  # карта прошлой гонки могла остаться
                os.remove(MAP)
            run_step(
                "генерация холодков",
                ["humanize.py", "-n", str(cold_budget), "--max-age-days", "30", "--out", MAP],
            )
            if os.path.exists(MAP) and os.path.getsize(MAP) > 4:
                cmd = [
                    "leads.py",
                    "outreach",
                    "--map",
                    MAP,
                    "--limit",
                    str(cold_budget),
                    "--report-json",
                    REPORT_COLD,
                ]
                if args.dry_run:
                    cmd.append("--dry-run")
                run_step("холодные", cmd)
                rep = read_report(REPORT_COLD)
                st["sent_cold"] += int(rep.get("sent", 0))
                if rep.get("stopped_reason") in ("spam", "flood"):
                    stop_outbound_for_run(st, rep["stopped_reason"])
            else:
                print("карта текстов пуста — холодных нет (нет новых лидов)")
        else:
            print("\n=== холодные: бюджет исчерпан")

        # 4) папки: раскладка чатов по статусам + HR-папки → manual-threads
        #    (канон 22.10: HR* и «Люди» липкие, бот их не трогает)
        folder_cmd = ["folders.py", "sync"]
        if args.dry_run:
            folder_cmd.append("--dry-run")
        run_step("папки", folder_cmd)

        print(
            f"\n[{stamp()}] итог за {st['date']}: догонок {st['sent_followup']}, "
            f"тёплых {st.get('sent_warm', 0)}, холодных {st['sent_cold']} из лимита {args.daily_cap}; "
            f"email в этом прогоне {sent_emails}"
        )
        run_completed = True
        return 0
    finally:
        try:
            append_report(
                {
                    "kind": "autopilot_run",
                    "source": "systemd_timer",
                    "profile": ",".join(profiles),
                    "started_at": started_at,
                    "status": (
                        "failed" if not run_completed else
                        "completed_with_errors" if FAILED_STEPS else "completed"
                    ),
                    "failed_steps": FAILED_STEPS,
                    "dry_run": args.dry_run,
                    "sent_followup": st.get("sent_followup", 0) - sent_before["followup"],
                    "sent_warm": st.get("sent_warm", 0) - sent_before["warm"],
                    "sent_cold": st.get("sent_cold", 0) - sent_before["cold"],
                    "sent_email": sent_emails,
                }
            )
        except (OSError, UnboundLocalError, NameError):
            print("!! не удалось записать отчёт автопилота")
        save_state(st)
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
