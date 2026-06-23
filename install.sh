#!/usr/bin/env bash
# install.sh — deploy Solar Smart on the Pi (standalone, does NOT touch SolarAssistant port).
# Run as solar-assistant user (sudo is used for system steps).
# Usage: bash install.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$PROJECT_DIR/.venv"

echo "=== Solar Smart — installer ==="
echo "Project: $PROJECT_DIR"
echo ""
echo "SolarAssistant keeps its default port 80."
echo "Solar Smart listens on port 8000 — http://<pi-ip>:8000/"
echo ""

# ---------------------------------------------------------------------------
# 1. Python virtual environment + dependencies
# ---------------------------------------------------------------------------
echo "--- [1/2] Setting up Python virtual environment ---"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install --ignore-requires-python -r "$PROJECT_DIR/requirements.txt" --quiet
echo "    Done."

# ---------------------------------------------------------------------------
# 2. Install systemd service for Solar Smart
# ---------------------------------------------------------------------------
echo "--- [2/2] Installing Solar Smart systemd service ---"
bash "$PROJECT_DIR/scripts/enable-smart-autostart.sh"
if [ -f "$PROJECT_DIR/scripts/restore-sa-defaults.sh" ]; then
    bash "$PROJECT_DIR/scripts/restore-sa-defaults.sh"
fi

echo ""
echo "=== Service status ==="
sudo systemctl is-active influx-bridge.service && echo "  SolarAssistant: OK (default port 80)" || echo "  SolarAssistant: check manually"
sudo systemctl is-active smart.service && echo "  Solar Smart:     OK (port 8000)" || echo "  Solar Smart: FAILED"

echo ""
IP=$(hostname -I | awk '{print $1}')
echo "=== Solar Smart URL: http://$IP:8000/ ==="
echo "=== SolarAssistant:  http://$IP/ ==="
