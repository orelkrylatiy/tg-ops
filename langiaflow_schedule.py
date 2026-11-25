from __future__ import annotations

import json
import random
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STATE = Path("/home/ubuntu/tg-ops/leads/langiaflow-schedule.json")
POSTS = Path("/home/ubuntu/tg-ops/leads/langiaflow-posts.jsonl")
POSTER = Path("/home/ubuntu/tg-ops/langiaflow_post.py")
MOSCOW = ZoneInfo("Europe/Moscow")


def main() -> None:
    now = datetime.now(MOSCOW)
    today = now.strftime("%Y-%m-%d")
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if state.get("date") != today:
        state = {"date": today, "target_minute": random.randint(9 * 60, 21 * 60 + 5)}
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state) + "\n", encoding="utf-8")
    if now.hour * 60 + now.minute < state["target_minute"]:
        print(f"waiting target={state['target_minute']} now={now.hour * 60 + now.minute}")
        return
    if POSTS.exists() and any(json.loads(line).get("ts", "").startswith(today) for line in POSTS.read_text(encoding="utf-8").splitlines() if line.strip()):
        print("already published today")
        return
    if state.get("attempted_date") == today:
        print("already attempted today; skipping repeated provider calls")
        return
    state["attempted_date"] = today
    STATE.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
    subprocess.run(["/home/ubuntu/tg-ops/.venv/bin/python", str(POSTER)], check=True)


if __name__ == "__main__":
    main()
