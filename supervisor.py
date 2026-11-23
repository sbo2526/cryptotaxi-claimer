#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Taxi — Supervisor / Watchdog
===================================
Kicks off and monitors all four background services and auto-restarts them
when they crash.  Also rotates logs periodically so they never grow
unbounded on a long-running VPS.

Services managed:
  1. claimer.py          — the multi-account claimer
  2. account_creator.py  — background account creator (auto-shuts at 100)
  3. dashboard.py        — web dashboard on port 8080
  4. referral_faker.py   — dormant referral faker (activates at 100 referrals)

Usage:
  python3 supervisor.py           # start all services
  python3 supervisor.py --status   # show current status
  python3 supervisor.py --stop     # stop everything
  python3 supervisor.py --restart  # restart all services
"""

import os
import sys
import time
import json
import shutil
import signal
import argparse
import subprocess
import threading
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from runtime_config import ACCOUNT_CREATION_ENABLED

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Asia/Tehran")

# ── Logging ─────────────────────────────────────────────────────────────────
SUPERVISOR_LOG = os.path.join(HERE, "supervisor.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(SUPERVISOR_LOG, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("supervisor")

# ── Service definitions ──────────────────────────────────────────────────────
SERVICES = {
    "claimer": {
        "name": "Claimer",
        "cmd": ["python3", "claimer.py", "--poll", "60"],
        "log": "claimer.log",
        "critical": True,   # must stay alive
        "auto_restart": True,
    },
    "account_creator": {
        "name": "Account Creator",
        "enabled": ACCOUNT_CREATION_ENABLED,
        "cmd": ["python3", "account_creator.py", "--target", "100", "--interval", "300"],
        "log": "creator.log",
        "critical": False,  # self-shuts at 100; not restarted if exited cleanly
        "auto_restart": True,
    },
    "dashboard": {
        "name": "Dashboard",
        "cmd": ["python3", "dashboard.py", "8080"],
        "log": "dashboard.log",
        "critical": True,
        "auto_restart": True,
        "port": 8080,
    },
    "referral_faker": {
        "name": "Referral Faker",
        "enabled": ACCOUNT_CREATION_ENABLED,
        "cmd": ["python3", "referral_faker.py"],
        "log": "referral_faker.log",
        "critical": False,
        "auto_restart": True,
    },
}

# ── Log rotation ────────────────────────────────────────────────────────────
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5 MB per log file


def rotate_logs():
    """Rotate any log file that exceeds the max size."""
    for svc in SERVICES.values():
        log_path = os.path.join(HERE, svc["log"])
        if os.path.exists(log_path) and os.path.getsize(log_path) > LOG_MAX_SIZE:
            rotated = log_path + ".1"
            shutil.move(log_path, rotated)
            log.info("Rotated %s -> %s (was > %d MB)", svc["log"], svc["log"] + ".1", LOG_MAX_SIZE // 1024 // 1024)


# ── Process management ──────────────────────────────────────────────────────

class Service:
    """Wraps a single background process with restart logic."""

    def __init__(self, key, config):
        self.key = key
        self.config = config
        self.process = None
        self.pid = None
        self.restart_count = 0
        self.last_start = None
        self.last_exit = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if not self.config.get("enabled", True):
                log.info("[%s] disabled — not starting", self.config["name"])
                return
            if self.process and self.process.poll() is None:
                log.debug("[%s] already running (pid %d)", self.key, self.pid)
                return

            cmd = self.config["cmd"]
            cwd = HERE
            # Append stdout/stderr to the service log
            log_path = os.path.join(HERE, self.config["log"])
            log_file = open(log_path, "a", encoding="utf-8")

            try:
                self.process = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,  # detach — survives supervisor restart
                )
                self.pid = self.process.pid
                self.last_start = datetime.now(TZ)
                self.restart_count += 1
                log.info("[%s] started (pid %d)", self.config["name"], self.pid)
            except Exception as e:
                log.error("[%s] failed to start: %s", self.config["name"], e)
            finally:
                log_file.close()

    def stop(self):
        with self._lock:
            if self.process and self.process.poll() is None:
                log.info("[%s] stopping (pid %d)...", self.config["name"], self.pid)
                try:
                    os.killpg(os.getpgid(self.pid), signal.SIGTERM)
                    self.process.wait(timeout=10)
                except Exception:
                    try:
                        os.killpg(os.getpgid(self.pid), signal.SIGKILL)
                    except Exception:
                        pass
                log.info("[%s] stopped", self.config["name"])
            self.process = None
            self.pid = None

    def is_running(self):
        if self.process is None:
            return False
        return self.process.poll() is None

    def get_exit_code(self):
        if self.process:
            return self.process.poll()
        return None

    def status(self):
        running = self.is_running()
        exit_code = self.get_exit_code()
        return {
            "key": self.key,
            "name": self.config["name"],
            "running": running,
            "pid": self.pid if running else None,
            "exit_code": exit_code,
            "restart_count": self.restart_count,
            "last_start": self.last_start.strftime("%Y-%m-%d %H:%M:%S") if self.last_start else None,
            "last_exit": self.last_exit.strftime("%Y-%m-%d %H:%M:%S") if self.last_exit else None,
            "enabled": self.config.get("enabled", True),
            "auto_restart": self.config["auto_restart"] and self.config.get("enabled", True),
            "critical": self.config["critical"],
        }


# ── Supervisor ─────────────────────────────────────────────────────────────

class Supervisor:
    def __init__(self):
        self.services = {}
        self.running = False
        self.monitor_thread = None
        self.rotate_thread = None
        self.monitor_interval = 30  # seconds between health checks

        for key, config in SERVICES.items():
            self.services[key] = Service(key, config)

    def start_all(self):
        log.info("=" * 60)
        log.info("Crypto Taxi Supervisor — starting all services")
        log.info("Timezone: Asia/Tehran (%s)", datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        log.info("=" * 60)

        self.running = True
        for svc in self.services.values():
            svc.start()

        # Start monitor thread
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="supervisor-monitor")
        self.monitor_thread.start()

        # Start log rotation thread
        self.rotate_thread = threading.Thread(target=self._rotate_loop, daemon=True, name="supervisor-rotate")
        self.rotate_thread.start()

        log.info("Supervisor running. Monitoring %d services every %ds.", len(self.services), self.monitor_interval)
        log.info("Press Ctrl+C to stop.")

    def stop_all(self):
        log.info("Stopping all services...")
        self.running = False
        for svc in self.services.values():
            svc.stop()
        log.info("All services stopped.")

    def _monitor_loop(self):
        while self.running:
            time.sleep(self.monitor_interval)
            for svc in self.services.values():
                try:
                    self._check_service(svc)
                except Exception as e:
                    log.error("Monitor error for %s: %s", svc.key, e)

    def _check_service(self, svc):
        if not svc.config.get("enabled", True):
            return
        was_running = svc.is_running()

        if not was_running:
            exit_code = svc.get_exit_code()
            svc.last_exit = datetime.now(TZ)

            # Check if the account creator exited cleanly (target reached)
            if svc.key == "account_creator" and exit_code == 0:
                # Check if we've hit 100 accounts
                accounts_file = os.path.join(HERE, "accounts.json")
                if os.path.exists(accounts_file):
                    try:
                        accounts = json.load(open(accounts_file))
                        count = len(accounts) if isinstance(accounts, list) else 0
                        if count >= 100:
                            log.info("[%s] exited cleanly — target of 100 accounts reached. Not restarting.", svc.config["name"])
                            return
                    except Exception:
                        pass
                # If exit code 0 but not at 100, restart anyway
                log.warning("[%s] exited (code %s) — restarting...", svc.config["name"], exit_code)
            else:
                log.warning("[%s] is NOT running (exit code: %s) — restarting...", svc.config["name"], exit_code)

            if svc.config["auto_restart"]:
                log.info("[%s] restarting...", svc.config["name"])
                svc.start()
                # Brief sleep to let it settle
                time.sleep(2)

    def _rotate_loop(self):
        while self.running:
            time.sleep(3600)  # every hour
            try:
                rotate_logs()
            except Exception as e:
                log.error("Log rotation error: %s", e)

    def print_status(self):
        """Print a formatted status table by scanning the real process list."""
        import subprocess as sp
        print()
        print("=" * 80)
        print(f"  Crypto Taxi Supervisor — Status  ({datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')})")
        print("=" * 80)

        # Account summary
        accounts_file = os.path.join(HERE, "accounts.json")
        earnings_file = os.path.join(HERE, "earnings.json")

        account_count = 0
        total_earned = 0.0
        if os.path.exists(accounts_file):
            try:
                accounts = json.load(open(accounts_file))
                account_count = len(accounts) if isinstance(accounts, list) else 0
            except Exception:
                pass
        if os.path.exists(earnings_file):
            try:
                earnings = json.load(open(earnings_file))
                total_earned = earnings.get("all_time", {}).get("total_usdt", 0)
            except Exception:
                pass

        print(f"  Accounts: {account_count} / 100  |  All-time earnings: {total_earned:.4f} USDT")
        print()

        # Scan real processes by command-line pattern
        ps_out = sp.run(["ps", "aux"], capture_output=True, text=True).stdout
        for svc in self.services.values():
            if not svc.config.get("enabled", True):
                print(f"  {svc.config['name']:<20} DISABLED                            auto-restart: ❌")
                continue
            cmd_pattern = svc.config["cmd"][1]  # e.g. "claimer.py"
            matching_lines = [l for l in ps_out.split("\n") if cmd_pattern in l and "supervisor.py" not in l and "grep" not in l]
            if matching_lines:
                pid = matching_lines[0].split()[1]
                status_str = f"✅ RUNNING (pid {pid})"
            else:
                status_str = "❌ STOPPED"

            print(f"  {svc.config['name']:<20} {status_str:<35} auto-restart: {'✅' if svc.config['auto_restart'] else '❌'}")

        # Also check supervisor itself
        sup_running = any("supervisor.py" in l for l in ps_out.split("\n") if "grep" not in l)
        if sup_running:
            sup_pid = [l.split()[1] for l in ps_out.split("\n") if "supervisor.py" in l and "grep" not in l][0]
            print(f"  {'Supervisor':<20} ✅ RUNNING (pid {sup_pid})")
        else:
            print(f"  {'Supervisor':<20} ❌ NOT RUNNING")

        print("=" * 80)
        print()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crypto Taxi Supervisor")
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    parser.add_argument("--stop", action="store_true", help="Stop all services and exit")
    parser.add_argument("--restart", action="store_true", help="Restart all services")
    parser.add_argument("--monitor-interval", type=int, default=30, help="Health check interval (seconds)")
    args = parser.parse_args()

    sup = Supervisor()
    sup.monitor_interval = args.monitor_interval

    if args.status:
        sup.print_status()
        return

    if args.stop:
        sup.stop_all()
        return

    if args.restart:
        sup.stop_all()
        time.sleep(3)
        sup.start_all()

    try:
        sup.start_all()
        # Keep running
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Supervisor interrupted — stopping all services...")
        sup.stop_all()


if __name__ == "__main__":
    main()
