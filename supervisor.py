#!/usr/bin/env python3
"""Start the local-file dashboard and, only if opted in, a primary-account worker.

The monitor is never restarted automatically after failure/suspension. Cross-
project exclusivity is enforced by its shared database lock, not a local PID.
"""

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from monitor_runtime import write_report, record_exit
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
    # Fresh local path per supervisor: replicas/deployments never overwrite
    # each other's pre-lock diagnostics on a shared DATA_DIR volume.
    runtime_directory = tempfile.TemporaryDirectory(prefix='cryptotaxi-runtime-')
    runtime_path = Path(runtime_directory.name) / 'monitor.json'
    child_env = dict(os.environ, MONITOR_RUNTIME_FILE=str(runtime_path))
    try:
        if 'monitor' in commands:
            write_report('starting', path=runtime_path)
        for name, command in commands.items():
            processes[name] = subprocess.Popen(command, cwd=HERE, stdin=subprocess.DEVNULL, env=child_env)
        while running:
            for name, process in list(processes.items()):
                if process.poll() is not None:
                    if name == 'dashboard':
                        log.warning('Dashboard exited; restarting')
                        processes[name] = subprocess.Popen(commands[name], cwd=HERE, stdin=subprocess.DEVNULL, env=child_env)
                    else:
                        log.warning('Monitor stopped. It will NOT restart without manual intervention.')
                        record_exit(path=runtime_path)
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
        runtime_directory.cleanup()


if __name__ == '__main__':
    main()
