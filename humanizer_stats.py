"""Compact report for humanizer-framework telemetry."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from humanizer_framework.telemetry import telemetry_path


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def _number(value):
    return value if isinstance(value, (int, float)) else 0


def _usage_totals(usage):
    if not isinstance(usage, dict):
        return 0, 0, 0
    input_tokens = _number(usage.get("input_tokens")) or _number(usage.get("prompt_tokens"))
    output_tokens = _number(usage.get("output_tokens")) or _number(usage.get("completion_tokens"))
    total = _number(usage.get("total_tokens")) or input_tokens + output_tokens
    return input_tokens, output_tokens, total


def load_events(path: Path):
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "generation":
            events.append(event)
    return events


def report(events):
    success = [event for event in events if event.get("status") == "success"]
    errors = [event for event in events if event.get("status") == "error"]
    totals = [0, 0, 0]
    for event in events:
        values = _usage_totals(event.get("usage"))
        totals = [left + right for left, right in zip(totals, values)]

    print(f"Генераций: {len(events)}  успешно: {len(success)}  ошибок: {len(errors)}")
    print(f"Токены: input={totals[0]} output={totals[1]} total={totals[2]}")
    print(f"Среднее время: {round(sum(event.get('elapsed_ms', 0) for event in events) / len(events)) if events else 0} ms")
    print(f"Repair: {sum(bool(event.get('repair_attempted')) for event in events)}")

    for title, key in (("По сценариям", "message_type"), ("По моделям", "model"), ("По лидам", "lead")):
        counter = Counter(event.get(key) or "—" for event in events)
        print(f"\n{title}:")
        for name, count in counter.most_common():
            print(f"  {name}: {count}")

    if errors:
        print("\nПоследние ошибки:")
        for event in errors[-10:]:
            print(f"  {event.get('timestamp')} {event.get('lead', '—')}: {event.get('error_type')} {event.get('error', '')}")


def main():
    parser = argparse.ArgumentParser(description="Статистика humanizer-framework")
    parser.add_argument(
        "--log",
        default=str(telemetry_path() or Path("humanizer-telemetry.jsonl")),
        help="путь к telemetry JSONL",
    )
    args = parser.parse_args()
    report(load_events(Path(args.log)))


if __name__ == "__main__":
    main()
