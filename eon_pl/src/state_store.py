"""Persist running statistic sums + last login timestamp between restarts."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class StateStore:
    """Single JSON file under /data with addon runtime state."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "state.json"
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except Exception as exc:
            _LOGGER.warning("State load failed: %s", exc)
            return {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, default=str))
        os.replace(tmp, self._path)

    # ---- statistics anchor ----

    def stats_anchor(self, stat_id: str) -> tuple[float, datetime | None]:
        a = (self._data.get("stats") or {}).get(stat_id) or {}
        ts_str = a.get("last_start")
        ts = datetime.fromisoformat(ts_str) if ts_str else None
        return float(a.get("last_sum") or 0.0), ts

    def update_stats_anchor(self, stat_id: str, last_sum: float, last_start: datetime) -> None:
        self._data.setdefault("stats", {})[stat_id] = {
            "last_sum": last_sum,
            "last_start": last_start.isoformat(),
        }
        self._save()

    # ---- session tracking ----

    def login_recorded(self) -> datetime | None:
        s = self._data.get("last_login")
        return datetime.fromisoformat(s) if s else None

    def record_login(self, when: datetime) -> None:
        self._data["last_login"] = when.isoformat()
        self._save()

    def fetch_recorded(self) -> datetime | None:
        s = self._data.get("last_fetch")
        return datetime.fromisoformat(s) if s else None

    def record_fetch(self, when: datetime, ok: bool) -> None:
        self._data["last_fetch"] = when.isoformat()
        self._data["last_fetch_ok"] = ok
        self._save()
