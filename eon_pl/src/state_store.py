"""Persist running statistic sums + last login timestamp between restarts."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# 1 (implicit, eon_pl <= 1.3.0): anchors only. Statistics written by it may
# hold provisional portal values that were never revised.
# 2 (eon_pl 1.3.1): anchors + per-day checkpoints for re-importing revised
# days. Statistics written by <= 1.3.1 hold misplaced hours on DST days.
# 3: DST days imported one row per hour.
STATS_STATE_VERSION = 3


class StateStore:
    """Single JSON file under /data with addon runtime state."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "state.json"
        self._data: dict[str, Any] = self._load()
        self._migrate()

    def _migrate(self) -> None:
        """Mark statistics written by an older version for one full re-import.

        Persisted with the next save; repeating it before then is harmless.
        """
        if int(self._data.get("stats_version") or 1) >= STATS_STATE_VERSION:
            return
        self._data["stats_full_reimport"] = sorted((self._data.get("stats") or {}).keys())
        self._data["stats_version"] = STATS_STATE_VERSION

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

    def stats_checkpoints(self, stat_id: str) -> dict[date, tuple[float, float]]:
        """Per local day: (sum before the day's first hour, factor it was imported with)."""
        a = (self._data.get("stats") or {}).get(stat_id) or {}
        return {
            date.fromisoformat(d): (float(v[0]), float(v[1]))
            for d, v in (a.get("days") or {}).items()
        }

    def update_stats_anchor(
        self,
        stat_id: str,
        last_sum: float,
        last_start: datetime,
        checkpoints: dict[date, tuple[float, float]] | None = None,
    ) -> None:
        self._data.setdefault("stats", {})[stat_id] = {
            "last_sum": last_sum,
            "last_start": last_start.isoformat(),
            "days": {
                d.isoformat(): [s, f] for d, (s, f) in sorted((checkpoints or {}).items())
            },
        }
        self._save()

    def full_reimport_pending(self, stat_id: str) -> bool:
        return stat_id in (self._data.get("stats_full_reimport") or [])

    def clear_full_reimport(self, stat_id: str) -> None:
        pending = self._data.get("stats_full_reimport") or []
        if stat_id in pending:
            self._data["stats_full_reimport"] = [s for s in pending if s != stat_id]
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
