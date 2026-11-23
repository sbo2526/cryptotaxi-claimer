"""Best-effort per-supervisor diagnostics, separate from shared account state.

The supervisor gives its dashboard and worker a fresh private local report path.
Reports never authorize traffic, release locks, change budgets or erase bans.
They are stage evidence, NOT heartbeats. No driver/server messages are stored.
"""

from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from runtime_config import primary_username

TZ = ZoneInfo('Asia/Tehran')
STAGES = frozenset(('starting', 'acquiring_lock', 'logging_in', 'waiting_first_read',
                    'waiting', 'cooldown', 'stopped'))
STOP_CODES = frozenset(('invalid_config', 'database_error', 'already_running',
                        'safety_unavailable', 'worker_exited', 'needs_login',
                        'access_denied', 'rate_limited', 'manual_stop',
                        'manual_review', 'suspended'))


def report_path(path=None):
    value = path or os.environ.get('MONITOR_RUNTIME_FILE')
    return Path(value) if value else None


def write_report(stage, code=None, path=None):
    target = report_path(path)
    if target is None:
        return False  # Direct workers without a supervisor still log normally.
    if stage not in STAGES:
        raise ValueError('Unknown monitor stage')
    report = {'user': primary_username(), 'stage': stage,
              'updated_at': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}
    if code is not None:
        report['code'] = code if isinstance(code, str) and code in STOP_CODES else 'manual_review'
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(report, stream)
        os.replace(temporary, target)
        return True
    except OSError:
        logging.getLogger('monitor').error('Unable to write local monitor diagnostics; inspect Railway logs.')
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_report(user=None, now=None, path=None):
    target = report_path(path)
    if target is None:
        return None
    user = primary_username() if user is None else user
    now = now or datetime.now(TZ)
    try:
        with target.open(encoding='utf-8') as stream:
            report = json.load(stream)
        if (not user or not isinstance(report, dict) or report.get('user') != user
                or report.get('stage') not in tuple(STAGES)):
            return None
        updated = datetime.strptime(report.get('updated_at', ''), '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
        if updated > now + timedelta(seconds=30):
            return None
        code = report.get('code')
        code = code if isinstance(code, str) and code in STOP_CODES else 'manual_review'
        return {'stage': report['stage'], 'updated': updated, 'code': code}
    except (OSError, ValueError, TypeError):
        return None


def record_exit(path=None):
    """Keep a specific worker-reported cause; catch unreported process deaths."""
    report = read_report(path=path)
    if report and report['stage'] == 'stopped':
        return
    write_report('stopped', code='worker_exited', path=path)
