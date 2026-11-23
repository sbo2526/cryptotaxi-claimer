#!/usr/bin/env python3
"""Start the protected dashboard and, only if opted in, a read-only monitor.

The monitor is never restarted automatically after failure/suspension. Cross-
project exclusivity is enforced by its shared database lock, not a local PID.
"""

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from runtime_config import initialize_data, monitoring_enabled

HERE = Path(__file__).resolve().parent
log = logging.getLogger('supervisor')


def service_commands():
    commands = {'dashboard': [sys.executable, 'dashboard.py']}
    if monitoring_enabled():
        commands['monitor'] = [sys.executable, 'claimer.py']
    return commands


def main():
    initialize_data()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    processes = {}
    running = True

    def stop(signum=None, frame=None):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    commands = service_commands()
    try:
        for name, command in commands.items():
            processes[name] = subprocess.Popen(command, cwd=HERE, stdin=subprocess.DEVNULL)
        while running:
            for name, process in list(processes.items()):
                if process.poll() is not None:
                    if name == 'dashboard':
                        log.warning('Dashboard exited; restarting')
                        processes[name] = subprocess.Popen(commands[name], cwd=HERE, stdin=subprocess.DEVNULL)
                    else:
                        log.warning('Monitor stopped. It will NOT restart without manual intervention.')
                        del processes[name]
            time.sleep(2)
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
