FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Explicit source allowlist: no Git history, secrets, legacy JSON or local backups.
COPY claimer.py dashboard.py supervisor.py runtime_config.py ban_detection.py monitor_safety.py monitor_runtime.py trip_rules.py ./
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown app:app /app/data
USER app
ENV DATA_DIR=/app/data
EXPOSE 8080
CMD ["python3", "supervisor.py"]
