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

cd /opt/eon_pl
exec python3 -m src
