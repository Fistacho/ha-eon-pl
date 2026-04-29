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

# Start Xvfb in the background instead of using xvfb-run, because xvfb-run
# wipes some env vars before exec — SUPERVISOR_TOKEN was being lost, which
# broke MQTT discovery and statistics import. Running Xvfb manually keeps
# the addon's full env intact.
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:99
# Tiny delay so Xvfb is ready before chromium tries to connect.
sleep 1

exec python3 -m src
