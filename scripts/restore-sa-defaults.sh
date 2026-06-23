#!/usr/bin/env bash
# Restore SolarAssistant default web port (80). Stop Smart from occupying nginx/PORT.
# Run on the Pi after deploy: bash scripts/restore-sa-defaults.sh
set -euo pipefail

SA_ENV="/usr/lib/influx-bridge/influx-bridge.env"
FRPC="/usr/lib/frp/proxy.toml"
NGINX_SITE="/etc/nginx/sites-enabled/solar-smart"

echo "=== Restore SolarAssistant defaults (port 80) ==="

if [ -f "$SA_ENV" ] && grep -q '^PORT=' "$SA_ENV" 2>/dev/null; then
  echo "Removing PORT= from $SA_ENV"
  sudo sed -i '/^PORT=/d' "$SA_ENV"
else
  echo "No PORT= override in $SA_ENV — OK"
fi

if [ -L "$NGINX_SITE" ] || [ -f "$NGINX_SITE" ]; then
  echo "Disabling nginx solar-smart site (frees port 80 for SA)"
  sudo rm -f "$NGINX_SITE"
  if sudo nginx -t 2>/dev/null; then
    sudo systemctl stop nginx 2>/dev/null || true
    sudo systemctl disable nginx 2>/dev/null || true
  fi
else
  echo "nginx solar-smart site not enabled — OK"
fi

if [ -f "$FRPC" ] && grep -q 'localPort = 8080' "$FRPC" 2>/dev/null; then
  echo "Reverting frpc localPort 8080 → 80"
  sudo sed -i 's/localPort = 8080/localPort = 80/' "$FRPC"
  sudo systemctl restart frpc.service 2>/dev/null || true
fi

echo "Restarting SolarAssistant …"
sudo systemctl reset-failed influx-bridge.service 2>/dev/null || true
sudo systemctl restart influx-bridge.service

echo "Done. SolarAssistant: http://<pi-ip>/  |  Solar Smart: http://<pi-ip>:8000/"
