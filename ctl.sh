#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-start}" != start ]; then
  echo "Stop/restart through Railway or your process manager. No detached secondary workers are started here."
  exit 1
fi
exec python3 supervisor.py
