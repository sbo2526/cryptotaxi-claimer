"""Fail-closed configuration. Secrets belong in Railway Variables, never Git."""

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('DATA_DIR', str(HERE / 'data'))).resolve()
ACCOUNT_CREATION_ENABLED = False  # Removed, not configurable.
AUTOMATIC_ACTIONS_ENABLED = False  # Legacy broad automation switch remains disabled.
# Owner-selected public identity only. The password stays in Railway Variables.
# An old PRIMARY_ACCOUNT value must not silently select a different account.
ALLOWED_PRIMARY_ACCOUNT = 'bavafasobhan@gmail.com'


def primary_username():
    configured = os.environ.get('PRIMARY_ACCOUNT', '').strip()
    return configured if configured == ALLOWED_PRIMARY_ACCOUNT else ''


def monitoring_enabled():
    # Enable only after obtaining permission to use the site's API.
    return os.environ.get('SITE_MONITORING_ENABLED', '').lower() == 'true'


def auto_start_enabled():
    # Narrow opt-in: no renting, claiming, registration or arbitrary mutations.
    return os.environ.get('AUTO_START_TRIPS_ENABLED', '').lower() == 'true'


def initialize_data():
    import json
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, value in {
        'status.json': {'_meta': {}},
        'earnings.json': {},
        'banned_accounts.json': {'banned': []},
    }.items():
        path = DATA_DIR / name
        try:
            with path.open('x', encoding='utf-8') as stream:
                json.dump(value, stream)
            path.chmod(0o600)
        except FileExistsError:
            pass
