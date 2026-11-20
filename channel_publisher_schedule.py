"""Run configured channel posts when their per-channel daily time arrives."""
from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "channel_publisher.json"
STATE_DIR = ROOT / "leads" / "channel-posts"


def minute(value: str) -> int:
    parsed = time.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    for channel in config.get("channels", []):
        if not channel.get("enabled", False):
            continue
        zone = ZoneInfo(channel.get("timezone", "UTC"))
        now = datetime.now(zone)
        today = now.date().isoformat()
        key = channel["id"]
        state_path = STATE_DIR / f"{key}-schedule.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        start, end = minute(channel.get("window_start", "09:00")), minute(channel.get("window_end", "21:00"))
        if end < start:
            raise ValueError(f"channel {key}: window_end must be after window_start")
        if state.get("date") != today:
            state = {"date": today, "targets": sorted(random.sample(range(start, end + 1),
                k=min(max(1, int(channel.get("daily_limit", 1))), end - start + 1)))}
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        current = now.hour * 60 + now.minute
        attempted = state.setdefault("attempted_indexes", [])
        due = next((index for index, target in enumerate(state["targets"])
                    if target <= current and index not in attempted), None)
        if due is None:
            print(f"{key}: waiting/already attempted today")
            continue
        attempted.append(due)
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "channel_publisher.py"), key], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
