#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Crypto Taxi — Deployment Script for DirectAdmin / cPanel / any Linux host
# ═══════════════════════════════════════════════════════════════════════════════
# Usage:
#   1. Upload cryptotaxi_claimer files to your host (any path)
#   2. Run:  bash setup-deploy.sh /path/to/cryptotaxi_claimer
#   3. Follow the on-screen prompts
#
# What this script does:
#   ✓ Checks Python 3 and installs missing dependencies
#   ✓ Starts all services via the supervisor
#   ✓ Creates a systemd service so everything auto-starts on boot
#   ✓ Sets up Apache reverse proxy so your domain points to the dashboard
# ═══════════════════════════════════════════════════════════════════════════════

set -e

# ── Detect script location & project path ─────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${1:-$SCRIPT_DIR}"

if [ ! -f "$PROJECT_DIR/supervisor.py" ]; then
    echo "❌ Error: supervisor.py not found in $PROJECT_DIR"
    echo "   Usage: bash setup-deploy.sh /path/to/cryptotaxi_claimer"
    exit 1
fi

echo "════════════════════════════════════════════════════════════════"
echo "  Crypto Taxi — Deployment Setup"
echo "  Project path: $PROJECT_DIR"
echo "════════════════════════════════════════════════════════════════"
echo

# ── 1. Check Python3 ─────────────────────────────────────────────────────────
echo "▶ [1/5] Checking Python 3..."
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 is not installed."
    echo "   Install it with:"
    echo "     sudo yum install -y python3 python3-pip   (CentOS/RHEL)"
    echo "     sudo apt install -y python3 python3-pip   (Ubuntu/Debian)"
    exit 1
fi

PY_VER=$(python3 --version 2>&1)
echo "   ✅ Found: $PY_VER"

# ── 2. Install Python dependencies ───────────────────────────────────────────
echo
echo "▶ [2/5] Checking Python dependencies (requests)..."
if ! python3 -c "import requests" &>/dev/null; then
    echo "   Installing requests..."
    pip3 install requests 2>/dev/null || pip3 install --user requests 2>/dev/null || {
        echo "   Trying pip from apt..."
        sudo apt install -y python3-requests 2>/dev/null || sudo yum install -y python3-requests 2>/dev/null || true
    }
fi
python3 -c "import requests" 2>/dev/null && echo "   ✅ requests is installed" || echo "   ⚠️  requests may still be missing — try: pip3 install requests"

# ── 3. Create systemd service ────────────────────────────────────────────────
echo
echo "▶ [3/5] Creating systemd service..."
SERVICE_NAME="cryptotaxi-supervisor"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER=$(whoami)

if [ "$CURRENT_USER" = "root" ]; then
    SERVICE_USER="root"
else
    SERVICE_USER="$CURRENT_USER"
fi

cat > "$SERVICE_FILE" << SERVICEEOF
[Unit]
Description=Crypto Taxi Supervisor (claimer + dashboard + creator + referral faker)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/python3 $PROJECT_DIR/supervisor.py --monitor-interval 30
Restart=always
RestartSec=10
StandardOutput=append:$PROJECT_DIR/supervisor.log
StandardError=append:$PROJECT_DIR/supervisor.log

[Install]
WantedBy=multi-user.target
SERVICEEOF

echo "   Service file: $SERVICE_FILE"
echo "   Service user: $SERVICE_USER"

if command -v systemctl &>/dev/null; then
    sudo systemctl daemon-reload 2>/dev/null || systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME" 2>/dev/null || systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME" 2>/dev/null || systemctl start "$SERVICE_NAME"
    echo "   ✅ systemd service enabled and started"
else
    echo "   ⚠️  systemd not found. Services will not auto-start on boot."
    echo "     Start manually with: cd $PROJECT_DIR && ./ctl.sh start"
fi

# ── 4. Check if dashboard is running ─────────────────────────────────────────
echo
echo "▶ [4/5] Checking dashboard..."
sleep 3
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/ 2>/dev/null | grep -q "200"; then
    echo "   ✅ Dashboard is running on http://localhost:8080"
else
    echo "   ⚠️  Dashboard may not be ready yet. Wait a few seconds and check."
    echo "      Manual check: curl http://localhost:8080/"
fi

# ── 5. Apache Reverse Proxy setup ────────────────────────────────────────────
echo
echo "▶ [5/5] Apache Reverse Proxy setup"

# Check if Apache is present
APACHE_BIN=""
if command -v apache2 &>/dev/null; then
    APACHE_BIN="apache2"
elif command -v httpd &>/dev/null; then
    APACHE_BIN="httpd"
fi

if [ -n "$APACHE_BIN" ]; then
    APACHE_CONF_DIR=""
    if [ "$APACHE_BIN" = "apache2" ]; then
        APACHE_CONF_DIR="/etc/apache2"
    else
        APACHE_CONF_DIR="/etc/httpd"
    fi

    # Try to find a conf.d or sites-available directory
    PROXY_CONF=""
    for dir in "$APACHE_CONF_DIR/conf.d" "$APACHE_CONF_DIR/sites-available" "$APACHE_CONF_DIR/conf"; do
        if [ -d "$dir" ]; then
            PROXY_CONF="$dir/cryptotaxi-proxy.conf"
            break
        fi
    done

    if [ -n "$PROXY_CONF" ]; then
        cat > "$PROXY_CONF" << PROXYEOF
# Crypto Taxi — Dashboard Proxy
# Points your domain to the Python dashboard on port 8080
# Replace 'cryptotaxi.example.com' with your actual domain

<VirtualHost *:80>
    ServerName cryptotaxi.example.com
    ProxyPreserveHost On
    ProxyRequests Off
    ProxyPass / http://127.0.0.1:8080/
    ProxyPassReverse / http://127.0.0.1:8080/

    <Proxy "*">
        Require all granted
    </Proxy>
</VirtualHost>
PROXYEOF

        echo "   ✅ Proxy config written: $PROXY_CONF"
        echo
        echo "   ========================================="
        echo "   ⚠️  ACTION REQUIRED:"
        echo "   1. Edit $PROXY_CONF"
        echo "   2. Change 'cryptotaxi.example.com' to your actual domain"
        echo "   3. Restart Apache:"
        if [ "$APACHE_BIN" = "apache2" ]; then
            echo "      sudo systemctl restart apache2"
        else
            echo "      sudo systemctl restart httpd"
        fi
        echo "   4. Make sure the DNS A record points to this server IP"
        echo "   ========================================="
    else
        echo "   ⚠️  Could not find Apache conf.d directory."
        echo "     Add this to your Apache vhost config manually:"
        echo
        echo '     <VirtualHost *:80>'
        echo '         ServerName YOUR_DOMAIN.com'
        echo '         ProxyPreserveHost On'
        echo '         ProxyRequests Off'
        echo '         ProxyPass / http://127.0.0.1:8080/'
        echo '         ProxyPassReverse / http://127.0.0.1:8080/'
        echo '         <Proxy "*"> Require all granted </Proxy>'
        echo '     </VirtualHost>'
    fi
else
    echo "   ⚠️  Apache/httpd not found."
    echo "     If using nginx or cPanel, configure the proxy manually:"
    echo
    echo "     nginx:"
    echo '     location / {'
    echo '         proxy_pass http://127.0.0.1:8080/;'
    echo '         proxy_set_header Host \$host;'
    echo '         proxy_set_header X-Real-IP \$remote_addr;'
    echo '     }'
    echo
    echo "     cPanel: Use 'Reverse Proxy' in the cPanel interface"
    echo "     (Apache → Proxy Accounts → set domain to 127.0.0.1:8080)"
fi

# ── Final status ────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════"
echo "  Deployment complete!"
echo "════════════════════════════════════════════════════════════════"
echo

if command -v systemctl &>/dev/null && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "  ✅ All services running under $SERVICE_NAME"
else
    echo "  ⚠️  Services may need manual start:"
    echo "     cd $PROJECT_DIR && ./ctl.sh start"
fi

echo
echo "  Dashboard:  http://localhost:8080"
echo "  Status:     python3 $PROJECT_DIR/supervisor.py --status"
echo "  Logs:       tail -f $PROJECT_DIR/supervisor.log"
echo "  Stop:       ./ctl.sh stop"
echo "  Restart:    ./ctl.sh restart"
echo
echo "  📋 Important:"
echo "     - If Apache is used, edit the proxy conf to set your domain"
echo "     - Ensure your DNS A record points to this server's IP"
echo "     - The system will auto-start on server reboot"
echo "════════════════════════════════════════════════════════════════"
echo
