"""Load addon options from /data/options.json."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass
class AddonOptions:
    email: str
    password: str
    scan_interval_hours: int = 6
    cookie_refresh_hours: int = 12
    selected_kus: list[str] = field(default_factory=list)
    log_level: str = "info"
    mqtt_discovery: bool = True
    # Manual overrides — used when Supervisor doesn't inject SUPERVISOR_TOKEN
    # (Protection mode kept ON, etc.). Fields are optional; empty string means
    # "fall back to Supervisor".
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    ha_token: str = ""
    capsolver_api_key: str = ""
    manual_cookie_only: bool = False

    @classmethod
    def load(cls, path: str | None = None) -> "AddonOptions":
        path = path or os.environ.get("EON_OPTIONS_FILE", "/data/options.json")
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return cls(
            email=raw.get("email", ""),
            password=raw.get("password", ""),
            scan_interval_hours=int(raw.get("scan_interval_hours", 6)),
            cookie_refresh_hours=int(raw.get("cookie_refresh_hours", 12)),
            selected_kus=[str(x) for x in raw.get("selected_kus", []) or []],
            log_level=str(raw.get("log_level", "info")).lower(),
            mqtt_discovery=bool(raw.get("mqtt_discovery", True)),
            mqtt_host=str(raw.get("mqtt_host", "")).strip(),
            mqtt_port=int(raw.get("mqtt_port", 1883) or 1883),
            mqtt_user=str(raw.get("mqtt_user", "")).strip(),
            mqtt_password=str(raw.get("mqtt_password", "")),
            ha_token=str(raw.get("ha_token", "")).strip(),
            capsolver_api_key=str(raw.get("capsolver_api_key", "")).strip(),
            manual_cookie_only=_as_bool(raw.get("manual_cookie_only"), False),
        )


@dataclass
class Runtime:
    options: AddonOptions
    data_dir: str
    ha_url: str
    ha_token: str

    @classmethod
    def from_env(cls) -> "Runtime":
        options = AddonOptions.load()
        return cls(
            options=options,
            data_dir=os.environ.get("EON_DATA_DIR", "/data"),
            ha_url=os.environ.get("EON_HA_URL", "http://supervisor/core"),
            ha_token=os.environ.get("EON_HA_TOKEN", ""),
        )


def configure_logging(level: str) -> None:
    levels = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    logging.basicConfig(
        level=levels.get(level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
