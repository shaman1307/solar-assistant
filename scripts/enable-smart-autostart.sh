#!/usr/bin/env bash
# Enable Solar Smart autostart on boot (systemd).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT=smart
LEGACY=solar-smart.service
LEGACY_PATH="/etc/systemd/system/${LEGACY}"

echo "[enable-smart-autostart] installing ${UNIT}.service ..."
sudo cp "${PROJECT_DIR}/smart.service" "/etc/systemd/system/${UNIT}.service"

if [ -f "${LEGACY_PATH}" ]; then
  echo "[enable-smart-autostart] removing legacy ${LEGACY} ..."
  sudo systemctl disable --now "${LEGACY}" 2>/dev/null || true
  sudo rm -f "${LEGACY_PATH}"
fi

sudo systemctl daemon-reload
sudo systemctl enable "${UNIT}.service"
sudo systemctl reset-failed "${UNIT}.service" 2>/dev/null || true

ENABLED="$(systemctl is-enabled "${UNIT}.service" 2>/dev/null || echo unknown)"
if [ "${ENABLED}" != "enabled" ]; then
  echo "[enable-smart-autostart] ERROR: ${UNIT}.service is not enabled (got ${ENABLED})" >&2
  exit 1
fi

if systemctl is-active --quiet "${UNIT}.service"; then
  echo "[enable-smart-autostart] ${UNIT} already running"
else
  echo "[enable-smart-autostart] starting ${UNIT} ..."
  sudo systemctl start "${UNIT}.service"
fi

echo "[enable-smart-autostart] enabled=$(systemctl is-enabled ${UNIT}.service) active=$(systemctl is-active ${UNIT}.service)"
