#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$EUID" -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Missing TG Ops virtual environment: $ROOT/.venv/bin/python" >&2
  exit 1
fi
if [[ ! -f "$ROOT/admin.py" ]]; then
  echo "Missing TG Ops admin.py in $ROOT" >&2
  exit 1
fi

install -d -m 0755 /etc/systemd/system
install -m 0644 "$ROOT/systemd/tg-ops-control-center.service" \
  /etc/systemd/system/tg-ops-control-center.service

systemctl daemon-reload
systemctl enable --now tg-ops-control-center.service
systemctl restart tg-ops-control-center.service
systemctl is-active --quiet tg-ops-control-center.service

echo "TG Ops Control Center is running on 127.0.0.1:8765."
echo "Open http://127.0.0.1:8765/content through an SSH tunnel."
