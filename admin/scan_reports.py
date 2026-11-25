"""Append and read compact scan/autopilot reports for the admin UI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_PATH = PROJECT_ROOT / "leads" / "scan-reports.jsonl"


def append_report(report: dict[str, Any]) -> None:
    REPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"recorded_at": datetime.now(UTC).isoformat(), **report}
    with REPORTS_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_reports(limit: int = 100) -> list[dict[str, Any]]:
    try:
        lines = REPORTS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    reports: list[dict[str, Any]] = []
    for line in lines[-max(1, min(limit, 500)) :]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            reports.append(value)
    return list(reversed(reports))
