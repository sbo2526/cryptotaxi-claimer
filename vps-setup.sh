#!/usr/bin/env bash
# ============================================================
#  Install & run the Crypto Taxi claimer + dashboard on a VPS.
#  Run on the VPS (as root) after uploading this folder there.
#  Usage:
#    scp -r cryptotaxi_claimer user@vps-ip:/tmp/
#    ssh user@vps-ip 'sudo bash /tmp/cryptotaxi_claimer/vps-setup.sh'
# ============================================================
set -e

DIR=/opt/cryptotaxi

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Please run as root: sudo bash vps-setup.sh"
  exit 1
fi

echo "==> 1) Installing Python + requests"
apt-get update -qq
apt-get install -y -qq python3 python3-pip > /dev/null
pip3 install --quiet requests 2>/dev/null || apt-get install -y -qq python3-requests > /dev/null
python3 -c "import requests" && echo "   requests OK"

echo "==> 2) Copying app files to $DIR"
mkdir -p "$DIR/reports"
# copy from the folder where the script lives, or from /tmp if uploaded there
SRC="$(cd "$(dirname "$0")" && pwd)"
cp -f "$SRC"/claimer.py "$SRC"/dashboard.py "$SRC"/accounts.json "$DIR/" 2>/dev/null || true
cp -f cryptotaxi-claimer.service cryptotaxi-dashboard.service /etc/systemd/system/
# if a status.json exists locally, carry it over (optional)
cp -f "$SRC"/status.json "$DIR/" 2>/dev/null || true

echo "==> 3) Starting services (auto-start on boot, auto-restart on crash)"
systemctl daemon-reload
systemctl enable --now cryptotaxi-claimer cryptotaxi-dashboard

echo
echo "✅ Done!"
echo "   claimer : journalctl -u cryptotaxi-claimer -f"
echo "   dashboard: http://<vps-ip>:8080  (use Tailscale or a tunnel for secure access)"
