"""Persist .AspNet.Cookies between addon restarts (Supervisor wipes /tmp,
keeps /data)."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


class CookieStore:
    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "cookie.json"

    def load(self) -> tuple[str | None, datetime | None]:
        if not self._path.exists():
            return None, None
        try:
            raw = json.loads(self._path.read_text())
            cookie = raw.get("cookie") or None
            ts_str = raw.get("captured_at")
            ts = datetime.fromisoformat(ts_str) if ts_str else None
            return cookie, ts
        except Exception as exc:
            _LOGGER.warning("Failed to read cookie store: %s", exc)
            return None, None

    def save(self, cookie: str) -> None:
        payload = {
            "cookie": cookie,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self._path)
