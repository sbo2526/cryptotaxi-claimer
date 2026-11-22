FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source files (including any optional proxies.txt / *.sh / *.json present)
COPY . .

# Create initial data files if they don't exist
RUN python3 init_data.py

# Expose dashboard port
EXPOSE 8080

# Start supervisor — it manages claimer, account_creator, dashboard, referral_faker
CMD ["python3", "supervisor.py", "--monitor-interval", "30"]
