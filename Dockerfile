FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source files
COPY *.py .
COPY *.sh .
COPY *.json .
COPY proxies.txt . 2>/dev/null || true

# Create initial data files if they don't exist
RUN python3 -c "
import json, os
for fname, default in [
    ('accounts.json', []),
    ('earnings.json', {'all_time': {'total_usdt': 0}, 'daily': {}, 'weekly': {}}),
    ('status.json', {'_meta': {'started_at': '', 'proxy': {'total': 0, 'success': 0, 'failure': 0, 'current': 'direct'}}}),
    ('banned_accounts.json', []),
    ('creator_status.json', {'status': 'not_started', 'current_phase': 'idle'}),
    ('referral_faker_status.json', {'status': 'dormant', 'current_referral_count': 0, 'triggered': False}),
]:
    if not os.path.exists(fname):
        json.dump(default, open(fname, 'w'), ensure_ascii=False, indent=2)
"

# Expose dashboard port
EXPOSE 8080

# Start supervisor — it manages claimer, account_creator, dashboard, referral_faker
CMD ["python3", "supervisor.py", "--monitor-interval", "30"]
