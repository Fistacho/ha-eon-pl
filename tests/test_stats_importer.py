"""StatsImporter: kWh + optional PLN cost statistics, anchors, no network."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from src.config import AddonOptions
from src.state_store import StateStore
from src.stats_importer import StatsImporter

PPE = "590000000000000001"
WARSAW = ZoneInfo("Europe/Warsaw")

IMPORTED = f"eon_pl:imported_{PPE}"
EXPORTED = f"eon_pl:exported_{PPE}"
IMPORTED_COST = f"eon_pl:imported_cost_{PPE}"
EXPORTED_COMP = f"eon_pl:exported_compensation_{PPE}"


def _naive_local(hour: int) -> datetime:
    # Naive Polish local time, exactly as the eon.pl CSV parser yields it.
    return datetime(2026, 1, 10, hour, tzinfo=WARSAW).replace(tzinfo=None)


def _rows() -> list[dict[str, Any]]:
    return [
        {"timestamp": _naive_local(0), "imported_kwh": 1.0, "exported_kwh": 0.0},
        {"timestamp": _naive_local(1), "imported_kwh": 0.5, "exported_kwh": 2.0},
        {"timestamp": _naive_local(2), "imported_kwh": 2.0, "exported_kwh": 1.0},
    ]


def _utc(hour: int) -> datetime:
    return datetime(2026, 1, 10, hour, tzinfo=WARSAW).astimezone(timezone.utc)


class FakeWs:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    async def __call__(self, metadata, stats):
        self.calls.append((metadata, stats))
        return True, ""

    def by_id(self) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
        return {meta["statistic_id"]: (meta, stats) for meta, stats in self.calls}


@pytest.fixture
def state(tmp_path) -> StateStore:
    return StateStore(str(tmp_path))


def _importer(state: StateStore, monkeypatch, **prices) -> tuple[StatsImporter, FakeWs]:
    imp = StatsImporter("http://ha:8123", "token", state, **prices)
    ws = FakeWs()
    monkeypatch.setattr(imp, "_ws_import", ws)
    return imp, ws


async def test_cost_is_kwh_times_price_with_running_sum(state, monkeypatch):
    imp, ws = _importer(
        state, monkeypatch, price_import_pln_kwh=1.1, value_export_pln_kwh=0.4
    )

    await imp.import_hourly({f"ku_{PPE}": _rows()})

    calls = ws.by_id()
    assert set(calls) == {IMPORTED, EXPORTED, IMPORTED_COST, EXPORTED_COMP}

    meta, stats = calls[IMPORTED_COST]
    assert meta == {
        "has_mean": False,
        "has_sum": True,
        "name": f"E.ON Polska — koszt poboru {PPE}",
        "source": "eon_pl",
        "statistic_id": IMPORTED_COST,
        "unit_of_measurement": "PLN",
    }
    assert [s["start"] for s in stats] == [_utc(h).isoformat() for h in (0, 1, 2)]
    assert [s["state"] for s in stats] == pytest.approx([1.1, 0.55, 2.2])
    assert [s["sum"] for s in stats] == pytest.approx([1.1, 1.65, 3.85])

    meta, stats = calls[EXPORTED_COMP]
    assert meta["name"] == f"E.ON Polska — wartość oddanej {PPE}"
    assert meta["unit_of_measurement"] == "PLN"
    assert meta["has_sum"] is True and meta["has_mean"] is False
    assert [s["state"] for s in stats] == pytest.approx([0.0, 0.8, 0.4])
    assert [s["sum"] for s in stats] == pytest.approx([0.0, 0.8, 1.2])

    # kWh statistics stay untouched by the price (multiplier 1, unit kWh).
    meta, stats = calls[IMPORTED]
    assert meta["unit_of_measurement"] == "kWh"
    assert [s["sum"] for s in stats] == pytest.approx([1.0, 1.5, 3.5])

    assert state.stats_anchor(IMPORTED_COST) == (pytest.approx(3.85), _utc(2))
    assert state.stats_anchor(EXPORTED_COMP) == (pytest.approx(1.2), _utc(2))


async def test_no_cost_import_when_prices_zero(state, monkeypatch):
    imp, ws = _importer(state, monkeypatch)  # defaults: 0 = disabled

    await imp.import_hourly({f"ku_{PPE}": _rows()})

    assert [meta["statistic_id"] for meta, _ in ws.calls] == [IMPORTED, EXPORTED]
    assert state.stats_anchor(IMPORTED_COST) == (0.0, None)
    assert state.stats_anchor(EXPORTED_COMP) == (0.0, None)


async def test_only_enabled_cost_statistic_is_imported(state, monkeypatch):
    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=1.0)

    await imp.import_hourly({f"ku_{PPE}": _rows()})

    assert set(ws.by_id()) == {IMPORTED, EXPORTED, IMPORTED_COST}


async def test_kwh_and_pln_anchors_are_independent(state, monkeypatch):
    # kWh stat already imported up to 01:00; cost stat never imported
    # (price just switched on) — cost must start from 0 at the first new row
    # and must not inherit the kWh sum.
    state.update_stats_anchor(IMPORTED, 100.0, _utc(1))
    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=2.0)

    await imp.import_hourly({f"ku_{PPE}": _rows()})

    calls = ws.by_id()
    _, kwh = calls[IMPORTED]
    assert [s["start"] for s in kwh] == [_utc(2).isoformat()]
    assert [s["sum"] for s in kwh] == pytest.approx([102.0])

    _, cost = calls[IMPORTED_COST]
    assert [s["start"] for s in cost] == [_utc(h).isoformat() for h in (0, 1, 2)]
    assert [s["sum"] for s in cost] == pytest.approx([2.0, 3.0, 7.0])

    assert state.stats_anchor(IMPORTED) == (pytest.approx(102.0), _utc(2))
    assert state.stats_anchor(IMPORTED_COST) == (pytest.approx(7.0), _utc(2))


async def test_cost_rows_at_or_before_anchor_are_skipped(state, monkeypatch):
    # A price change applies only to new hours: earlier sums are kept as-is.
    state.update_stats_anchor(IMPORTED_COST, 10.0, _utc(1))
    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=3.0)

    await imp.import_hourly({f"ku_{PPE}": _rows()})

    _, cost = ws.by_id()[IMPORTED_COST]
    assert [s["start"] for s in cost] == [_utc(2).isoformat()]
    assert [s["state"] for s in cost] == pytest.approx([6.0])
    assert [s["sum"] for s in cost] == pytest.approx([16.0])


async def test_nothing_new_means_no_cost_call(state, monkeypatch):
    state.update_stats_anchor(IMPORTED_COST, 10.0, _utc(2))
    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=3.0)

    await imp.import_hourly({f"ku_{PPE}": _rows()})

    assert IMPORTED_COST not in ws.by_id()
    assert state.stats_anchor(IMPORTED_COST) == (pytest.approx(10.0), _utc(2))


def _write_options(tmp_path, **extra) -> str:
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"email": "a@b.pl", "password": "x", **extra}))
    return str(path)


def test_price_options_default_to_zero(tmp_path):
    opts = AddonOptions.load(_write_options(tmp_path))
    assert opts.price_import_pln_kwh == 0.0
    assert opts.value_export_pln_kwh == 0.0


def test_price_options_are_loaded(tmp_path):
    opts = AddonOptions.load(
        _write_options(tmp_path, price_import_pln_kwh=1.07, value_export_pln_kwh=0.52)
    )
    assert opts.price_import_pln_kwh == pytest.approx(1.07)
    assert opts.value_export_pln_kwh == pytest.approx(0.52)


# ---- revised (provisional) days + one-time history re-import ----
#
# eon.pl serves provisional values for the newest days (sometimes a copy of the
# same weekday 4 weeks earlier) and replaces them with real readings later.
# A day imported once must therefore be re-imported when the portal revises it.


def _day_rows(day: int, values: list[float], month: int = 1) -> list[dict[str, Any]]:
    """Rows for hours 0..len(values)-1 of one local day (naive, like the CSV)."""
    return [
        {
            "timestamp": datetime(2026, month, day, h, tzinfo=WARSAW).replace(tzinfo=None),
            "imported_kwh": v,
            "exported_kwh": 0.0,
        }
        for h, v in enumerate(values)
    ]


def _utc_at(day: int, hour: int, month: int = 1) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=WARSAW).astimezone(timezone.utc)


async def test_revised_day_is_reimported_with_consistent_sums(state, monkeypatch):
    imp, ws = _importer(state, monkeypatch)
    # Fetch 1: day 11 still provisional (portal copy of another day).
    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(10, [1.0, 0.5, 2.0]) + _day_rows(11, [5.0, 5.0, 5.0])}
    )

    # Fetch 2 (window overlaps previous days): day 11 revised, day 12 new.
    ws.calls.clear()
    await imp.import_hourly(
        {
            f"ku_{PPE}": _day_rows(10, [1.0, 0.5, 2.0])
            + _day_rows(11, [1.0, 1.0, 1.0])
            + _day_rows(12, [2.0, 2.0, 2.0])
        }
    )

    _, stats = ws.by_id()[IMPORTED]
    by_start = {s["start"]: (s["state"], s["sum"]) for s in stats}
    assert by_start[_utc_at(11, 0).isoformat()] == pytest.approx((1.0, 4.5))
    assert by_start[_utc_at(11, 2).isoformat()] == pytest.approx((1.0, 6.5))
    assert by_start[_utc_at(12, 2).isoformat()] == pytest.approx((2.0, 12.5))
    assert state.stats_anchor(IMPORTED) == (pytest.approx(12.5), _utc_at(12, 2))


async def test_revised_cost_keeps_price_of_first_import(state, monkeypatch):
    imp, _ = _importer(state, monkeypatch, price_import_pln_kwh=2.0)
    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(10, [1.0, 1.0]) + _day_rows(11, [5.0, 5.0])}
    )

    # Price changed in options (addon restart = new importer, same state file).
    imp2, ws2 = _importer(state, monkeypatch, price_import_pln_kwh=3.0)
    await imp2.import_hourly(
        {
            f"ku_{PPE}": _day_rows(10, [1.0, 1.0])
            + _day_rows(11, [1.0, 1.0])
            + _day_rows(12, [1.0, 1.0])
        }
    )

    _, cost = ws2.by_id()[IMPORTED_COST]
    by_start = {s["start"]: (s["state"], s["sum"]) for s in cost}
    # Revised day 11: real kWh x the price it was first imported with (2.0).
    assert by_start[_utc_at(11, 1).isoformat()] == pytest.approx((2.0, 8.0))
    # New day 12: current price (3.0).
    assert by_start[_utc_at(12, 1).isoformat()] == pytest.approx((3.0, 14.0))


async def test_revision_window_matches_fetch_window(state, monkeypatch):
    from src.api import EonPolskaClient
    from src.const import STATS_REVISION_DAYS
    from src.coordinator import EonCoordinator

    imp, _ = _importer(state, monkeypatch)
    rows = [r for day in range(1, 32) for r in _day_rows(day, [1.0], month=1)]
    rows += [r for day in range(1, 11) for r in _day_rows(day, [1.0], month=2)]
    await imp.import_hourly({f"ku_{PPE}": rows})

    days = state.stats_checkpoints(IMPORTED)
    last_day = rows[-1]["timestamp"].date()
    assert min(days) == last_day - timedelta(days=STATS_REVISION_DAYS)
    assert max(days) == last_day

    # The coordinator re-fetches exactly the days the importer can still revise.
    coord = EonCoordinator(client=EonPolskaClient(""), selected_kus=[])
    coord._last_anchor[f"ku_{PPE}"] = rows[-1]["timestamp"]
    date_from, _ = coord._stats_window(f"ku_{PPE}")
    assert date_from == min(days)


def _legacy_state(tmp_path, anchors: dict[str, tuple[float, datetime]]) -> StateStore:
    """state.json as written by eon_pl <= 1.3.0 (no checkpoints, no version)."""
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "stats": {
                    sid: {"last_sum": s, "last_start": ts.isoformat()}
                    for sid, (s, ts) in anchors.items()
                }
            }
        )
    )
    return StateStore(str(tmp_path))


class FakeHourStat:
    """Answers the 'what does HA hold for this hour' query."""

    def __init__(self, rows: dict[str, dict[str, float]] | None, ok: bool = True):
        self.rows = rows or {}
        self.ok = ok
        self.asked: list[tuple[str, datetime]] = []

    async def __call__(self, statistic_id: str, start: datetime):
        self.asked.append((statistic_id, start))
        if not self.ok:
            return False, None
        return True, self.rows.get(statistic_id)


async def test_legacy_kwh_history_is_reimported_once_from_year_start(
    tmp_path, monkeypatch
):
    state = _legacy_state(
        tmp_path,
        {
            IMPORTED: (999.0, _utc_at(2, 2)),
            IMPORTED_COST: (555.0, _utc_at(2, 2)),
        },
    )
    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=2.0)
    # HA's first hour of the year: sum 10.7 incl. state 0.7, so 10.0 came
    # before it (older history); the rewrite must continue from it, not from 0.
    hour_stat = FakeHourStat({IMPORTED: {"state": 0.7, "sum": 10.7}})
    monkeypatch.setattr(imp, "_ws_hour_stat", hour_stat, raising=False)

    rows = _day_rows(1, [1.0, 1.0, 1.0]) + _day_rows(2, [2.0, 2.0, 2.0])
    await imp.import_hourly({f"ku_{PPE}": rows})

    calls = ws.by_id()
    _, kwh = calls[IMPORTED]
    assert [s["start"] for s in kwh] == [
        _utc_at(d, h).isoformat() for d in (1, 2) for h in (0, 1, 2)
    ]
    assert [s["sum"] for s in kwh] == pytest.approx([11.0, 12.0, 13.0, 15.0, 17.0, 19.0])
    assert state.stats_anchor(IMPORTED) == (pytest.approx(19.0), _utc_at(2, 2))
    # Cost statistics are not rewritten (price semantics: only new hours).
    assert IMPORTED_COST not in calls
    assert state.stats_anchor(IMPORTED_COST) == (pytest.approx(555.0), _utc_at(2, 2))

    # One-time: survives a restart and does not repeat.
    restarted = StateStore(str(tmp_path))
    imp2, _ = _importer(restarted, monkeypatch)
    again = FakeHourStat({IMPORTED: {"state": 1.0, "sum": 11.0}})
    monkeypatch.setattr(imp2, "_ws_hour_stat", again, raising=False)
    await imp2.import_hourly({f"ku_{PPE}": rows})
    assert again.asked == []
    assert restarted.stats_anchor(IMPORTED) == (pytest.approx(19.0), _utc_at(2, 2))


async def test_legacy_reimport_waits_for_a_year_start_fetch(tmp_path, monkeypatch):
    state = _legacy_state(tmp_path, {IMPORTED: (999.0, _utc_at(11, 2))})
    imp, ws = _importer(state, monkeypatch)
    hour_stat = FakeHourStat({IMPORTED: {"state": 1.0, "sum": 1.0}})
    monkeypatch.setattr(imp, "_ws_hour_stat", hour_stat, raising=False)

    # Incremental window (does not start on 1 January): history stays as is.
    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(10, [1.0, 1.0, 1.0]) + _day_rows(11, [1.0, 1.0, 1.0])}
    )
    assert hour_stat.asked == []
    assert IMPORTED not in ws.by_id()
    assert state.full_reimport_pending(IMPORTED)


async def test_legacy_reimport_is_retried_when_ha_cannot_answer(tmp_path, monkeypatch):
    state = _legacy_state(tmp_path, {IMPORTED: (999.0, _utc_at(2, 2))})
    imp, ws = _importer(state, monkeypatch)
    monkeypatch.setattr(imp, "_ws_hour_stat", FakeHourStat(None, ok=False), raising=False)

    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(1, [1.0, 1.0, 1.0]) + _day_rows(2, [2.0, 2.0, 2.0])}
    )

    # Unknown base sum: never guess (a wrong base = a jump in Energy Dashboard).
    assert IMPORTED not in ws.by_id()
    assert state.stats_anchor(IMPORTED) == (pytest.approx(999.0), _utc_at(2, 2))
    assert state.full_reimport_pending(IMPORTED)
