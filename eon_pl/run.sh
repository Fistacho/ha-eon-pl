#!/usr/bin/env bash
set -euo pipefail

# Read addon options (Supervisor writes them to /data/options.json on start)
OPTIONS_FILE="/data/options.json"
if [[ ! -f "$OPTIONS_FILE" ]]; then
    echo "FATAL: $OPTIONS_FILE not found" >&2
    exit 1
fi

# Inject Supervisor token + HA URL so the Python process can call REST APIs.
# These env vars are set by HA Supervisor when hassio_api / homeassistant_api
# are enabled in config.yaml.
export EON_OPTIONS_FILE="$OPTIONS_FILE"
export EON_DATA_DIR="/data"
export EON_HA_URL="${SUPERVISOR_TOKEN:+http://supervisor/core}"
export EON_HA_TOKEN="${SUPERVISOR_TOKEN:-}"

# One-line env diagnostic for the addon log so future "no token" issues are
# easy to spot without rebuilding the image. Lists everything Supervisor
# auto-injects that we might consume.
echo "[run.sh] SUPERVISOR_TOKEN=${SUPERVISOR_TOKEN:+<set>}${SUPERVISOR_TOKEN:-<empty>}" \
     "HASSIO_TOKEN=${HASSIO_TOKEN:+<set>}${HASSIO_TOKEN:-<empty>}" \
     "MQTT_HOST=${MQTT_HOST:-<empty>}" \
     "MQTT_PORT=${MQTT_PORT:-<empty>}" \
     "MQTT_USERNAME=${MQTT_USERNAME:-<empty>}" \
     "MQTT_PASSWORD=${MQTT_PASSWORD:+<set>}${MQTT_PASSWORD:-<empty>}"

cd /opt/eon_pl

# s6-overlay restarts this script within the same container on crash, so /tmp
# is NOT cleaned between restarts. Remove stale X lock/socket files left by
# the previous run; without this, Xvfb fails to bind :99 and Chromium hangs.
pkill -f "Xvfb :99" 2>/dev/null || true
sleep 0.3
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
mkdir -p /tmp/.X11-unix 2>/dev/null || true

# Start Xvfb in the background instead of using xvfb-run, because xvfb-run
# wipes some env vars before exec — SUPERVISOR_TOKEN was being lost, which
# broke MQTT discovery and statistics import. Running Xvfb manually keeps
# the addon's full env intact.
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
export DISPLAY=:99
echo "[run.sh] Xvfb PID=$XVFB_PID"

# Wait up to 10s for the X11 socket to appear.
for i in $(seq 1 10); do
    if [ -S /tmp/.X11-unix/X99 ]; then
        echo "[run.sh] Xvfb :99 ready (${i}s)"
        break
    fi
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "[run.sh] ERROR: Xvfb died immediately:" >&2
        cat /tmp/xvfb.log >&2
        break
    fi
    sleep 1
done
[ -S /tmp/.X11-unix/X99 ] || echo "[run.sh] WARNING: X99 socket not found — Xvfb may have failed"

exec python3 -m src
