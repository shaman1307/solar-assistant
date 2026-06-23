#!/usr/bin/env bash
# Graceful smart service reload — Pi Zero safe (no fuser, no nginx, no SA restart).
set -euo pipefail

UNIT=smart
UVICORN_PATTERN='[u]vicorn src.main:app'
HEALTH_URL='http://127.0.0.1:8000/'

echo "[reload-smart] stopping ${UNIT} ..."
sudo systemctl stop "${UNIT}" || true

for _ in $(seq 1 25); do
  if ! pgrep -f "${UVICORN_PATTERN}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if pgrep -f "${UVICORN_PATTERN}" >/dev/null 2>&1; then
  echo "[reload-smart] SIGTERM to leftover uvicorn"
  pkill -TERM -f "${UVICORN_PATTERN}" || true
  sleep 4
fi

touch "${HOME}/project/.smart-deployed"
echo "[reload-smart] starting ${UNIT} ..."
sudo systemctl start "${UNIT}"

for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "${HEALTH_URL}" 2>/dev/null; then
    echo "[reload-smart] OK - ${HEALTH_URL}"
    exit 0
  fi
  sleep 2
done

echo "[reload-smart] FAILED - service did not respond in time"
systemctl status "${UNIT}" --no-pager || true
exit 1
