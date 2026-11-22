import json, os

defaults = [
    ('accounts.json', []),
    ('earnings.json', {'all_time': {'total_usdt': 0}, 'daily': {}, 'weekly': {}}),
    ('status.json', {'_meta': {'started_at': '', 'proxy': {'total': 0, 'success': 0, 'failure': 0, 'current': 'direct'}}}),
    ('banned_accounts.json', []),
    ('creator_status.json', {'status': 'not_started', 'current_phase': 'idle'}),
    ('referral_faker_status.json', {'status': 'dormant', 'current_referral_count': 0, 'triggered': False}),
]
for fname, default in defaults:
    if not os.path.exists(fname):
        with open(fname, 'w') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
