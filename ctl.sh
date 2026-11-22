#!/bin/bash
# ── Crypto Taxi — control script ──────────────────────────────────────────────
# Start / stop / status all services via the Python supervisor.
#
# Usage:
#   ./ctl.sh start      — start all services (supervisor in background)
#   ./ctl.sh stop       — stop everything
#   ./ctl.sh restart    — restart everything
#   ./ctl.sh status     — show current status
#   ./ctl.sh log        — tail supervisor log

HERE="$(cd "$(dirname "$0")" && pwd)"
SUP="$HERE/supervisor.py"

case "${1:-start}" in
  start)
    echo "🚀 Starting Crypto Taxi services..."
    # Check if supervisor already running
    if pgrep -f "supervisor.py" > /dev/null 2>&1; then
      echo "⚠️  Supervisor is already running."
      exit 0
    fi
    nohup python3 "$SUP" --monitor-interval 30 >> "$HERE/supervisor.log" 2>&1 &
    echo "✅ Supervisor started (pid $!)"
    sleep 5
    echo
    python3 "$SUP" --status
    ;;
  stop)
    echo "🛑 Stopping all services..."
    if pgrep -f "supervisor.py" > /dev/null 2>&1; then
      pkill -f "supervisor.py"
      sleep 2
    fi
    # Also kill any stray service processes
    for name in "claimer.py" "account_creator.py" "dashboard.py" "referral_faker.py"; do
      pids=$(pgrep -f "$name" 2>/dev/null)
      if [ -n "$pids" ]; then
        kill $pids 2>/dev/null
        echo "  Killed $name (pid: $pids)"
      fi
    done
    echo "✅ All services stopped."
    ;;
  restart)
    echo "🔄 Restarting all services..."
    pkill -f "supervisor.py" 2>/dev/null
    sleep 3
    # Kill strays
    for name in "claimer.py" "account_creator.py" "dashboard.py" "referral_faker.py"; do
      pids=$(pgrep -f "$name" 2>/dev/null)
      if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null
      fi
    done
    sleep 1
    nohup python3 "$SUP" --monitor-interval 30 >> "$HERE/supervisor.log" 2>&1 &
    echo "✅ Supervisor restarted (pid $!)"
    sleep 5
    echo
    python3 "$SUP" --status
    ;;
  status)
    python3 "$SUP" --status
    ;;
  log)
    tail -50 "$HERE/supervisor.log"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log}"
    exit 1
    ;;
esac
