"""StatsImporter: kWh + optional PLN cost statistics, anchors, no network."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
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
    # HA >= 2025.11 metadata: mean_type (0 = no mean) replaces has_mean;
    # unit_class None = PLN has no unit converter.
    assert meta == {
        "mean_type": 0,
        "has_sum": True,
        "name": f"E.ON Polska — koszt poboru {PPE}",
        "source": "eon_pl",
        "statistic_id": IMPORTED_COST,
        "unit_class": None,
        "unit_of_measurement": "PLN",
    }
    assert [s["start"] for s in stats] == [_utc(h).isoformat() for h in (0, 1, 2)]
    assert [s["state"] for s in stats] == pytest.approx([1.1, 0.55, 2.2])
    assert [s["sum"] for s in stats] == pytest.approx([1.1, 1.65, 3.85])

    meta, stats = calls[EXPORTED_COMP]
    assert meta["name"] == f"E.ON Polska — wartość oddanej {PPE}"
    assert meta["unit_of_measurement"] == "PLN"
    assert meta["has_sum"] is True and meta["mean_type"] == 0
    assert meta["unit_class"] is None
    assert [s["state"] for s in stats] == pytest.approx([0.0, 0.8, 0.4])
    assert [s["sum"] for s in stats] == pytest.approx([0.0, 0.8, 1.2])

    # kWh statistics stay untouched by the price (multiplier 1, unit kWh).
    meta, stats = calls[IMPORTED]
    assert meta["unit_of_measurement"] == "kWh"
    assert meta["unit_class"] == "energy" and meta["mean_type"] == 0
    assert "has_mean" not in meta  # deprecated in HA, replaced by mean_type
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
    hour_stat = FakeHourStat({
        IMPORTED: {"state": 0.7, "sum": 10.7},
        IMPORTED_COST: {"state": 1.4, "sum": 6.4},
    })
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
    # Cost statistics are rewritten too (eon_pl <= 1.3.1 misplaced their DST
    # hours as well), at the current price, from HA's own base (5.0).
    _, cost = calls[IMPORTED_COST]
    assert [s["sum"] for s in cost] == pytest.approx([7.0, 9.0, 11.0, 15.0, 19.0, 23.0])
    assert state.stats_anchor(IMPORTED_COST) == (pytest.approx(23.0), _utc_at(2, 2))

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

    # Incremental window (does not start on 1 January, HA holds older hours):
    # history stays as is; HA is only read, to see that older hours exist.
    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(10, [1.0, 1.0, 1.0]) + _day_rows(11, [1.0, 1.0, 1.0])}
    )
    assert hour_stat.asked == [
        (IMPORTED, _utc_at(10, 0)), (IMPORTED, _utc_at(10, 0) - timedelta(hours=1))
    ]
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


# ---- DST days ----
#
# The OZE report always has 24 rows per day, each labelled by the local hour
# it ENDS at (1..24). Checked against live data (sensor.total_yield vs
# exported, 2026): summer hours are CEST, winter hours CET. On the spring day
# the row ending at the non-existent 02:00 is a zero placeholder; on the
# autumn day the repeated hour has no row.


def _oze_csv(day: str, values: list[float]) -> bytes:
    """OZE report CSV for one day: rows labelled 1..24 (local hour end)."""
    lines = ["Data;Godzina;Pobrana;Jedn.;Wprowadzona;Jedn.;Bilans;Jedn."]
    for label, v in enumerate(values, start=1):
        num = f"{v:.3f}".replace(".", ",")
        lines.append(f"{day};{label}:00;{num};kWh;0,000;kWh;{num};kWh")
    return "\n".join(lines).encode("utf-8")


def _utc_hours(first: datetime, n: int) -> list[str]:
    return [(first + timedelta(hours=i)).isoformat() for i in range(n)]


def _assert_consistent(stats: list[dict[str, Any]]) -> None:
    for prev, cur in pairwise(stats):
        assert cur["sum"] - prev["sum"] == pytest.approx(cur["state"])


async def test_spring_dst_day_one_row_per_hour(state, monkeypatch):
    from src.api import parse_oze_csv

    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=2.0)
    # Label 2 (ends at the non-existent 02:00) is the portal's zero placeholder.
    values = [1.0, 0.0, 2.0, 3.0] + [1.0] * 20
    await imp.import_hourly({f"ku_{PPE}": parse_oze_csv(_oze_csv("29.03.2026", values))})

    for sid, factor in ((IMPORTED, 1.0), (IMPORTED_COST, 2.0)):
        _, stats = ws.by_id()[sid]
        # 23-hour day: 28.03 23:00Z .. 29.03 21:00Z, each hour exactly once.
        assert [s["start"] for s in stats] == _utc_hours(
            datetime(2026, 3, 28, 23, tzinfo=timezone.utc), 23
        )
        assert [s["state"] for s in stats] == pytest.approx(
            [factor * v for v in [1.0, 2.0, 3.0] + [1.0] * 20]
        )
        _assert_consistent(stats)
        assert stats[-1]["sum"] == pytest.approx(factor * sum(values))


async def test_autumn_dst_day_has_no_gap(state, monkeypatch):
    from src.api import parse_oze_csv

    imp, ws = _importer(state, monkeypatch)
    values = [1.0, 2.0, 3.0, 4.0] + [1.0] * 20
    await imp.import_hourly({f"ku_{PPE}": parse_oze_csv(_oze_csv("25.10.2026", values))})

    _, stats = ws.by_id()[IMPORTED]
    # 25-hour day: 24.10 22:00Z .. 25.10 22:00Z, each hour exactly once.
    assert [s["start"] for s in stats] == _utc_hours(
        datetime(2026, 10, 24, 22, tzinfo=timezone.utc), 25
    )
    # Label 4 (03:00-04:00 CET = 02:00Z) matches PV production, so label 3
    # is 02:00-03:00 CET (01:00Z); the repeated CEST hour (00:00Z) has no row.
    assert [s["state"] for s in stats] == pytest.approx(
        [1.0, 2.0, 0.0, 3.0, 4.0] + [1.0] * 20
    )
    _assert_consistent(stats)
    assert state.stats_anchor(IMPORTED) == (
        pytest.approx(sum(values)), datetime(2026, 10, 25, 22, tzinfo=timezone.utc)
    )


async def test_state_from_1_3_1_rewrites_dst_hours_once_incl_cost(tmp_path, monkeypatch):
    from src.api import parse_oze_csv

    # state.json as written by eon_pl 1.3.1 (version 2, nothing pending).
    anchor = datetime(2026, 3, 29, 21, tzinfo=timezone.utc)
    (tmp_path / "state.json").write_text(json.dumps({
        "stats_version": 2,
        "stats_full_reimport": [],
        "stats": {
            sid: {"last_sum": 99.0, "last_start": anchor.isoformat(), "days": {}}
            for sid in (IMPORTED, IMPORTED_COST)
        },
    }))
    state = StateStore(str(tmp_path))
    assert state.full_reimport_pending(IMPORTED)
    assert state.full_reimport_pending(IMPORTED_COST)

    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=2.0)
    hour_stat = FakeHourStat({
        IMPORTED: {"state": 1.0, "sum": 11.0},       # 10.0 before 1 January
        IMPORTED_COST: {"state": 2.0, "sum": 7.0},   # 5.0 before 1 January
    })
    monkeypatch.setattr(imp, "_ws_hour_stat", hour_stat, raising=False)
    values = [1.0, 0.0, 2.0, 3.0] + [1.0] * 20
    rows = parse_oze_csv(_oze_csv("01.01.2026", [1.0] * 24)) + parse_oze_csv(
        _oze_csv("29.03.2026", values)
    )
    await imp.import_hourly({f"ku_{PPE}": rows})

    for sid, base, factor in ((IMPORTED, 10.0, 1.0), (IMPORTED_COST, 5.0, 2.0)):
        _, stats = ws.by_id()[sid]
        starts = [s["start"] for s in stats]
        assert len(starts) == len(set(starts)) == 24 + 23
        assert stats[0]["sum"] == pytest.approx(base + factor * 1.0)
        assert stats[-1]["sum"] == pytest.approx(base + factor * (24 + sum(values)))
        _assert_consistent(stats[24:])
        assert not state.full_reimport_pending(sid)

    # One-time: a restart does not repeat it.
    assert not StateStore(str(tmp_path)).full_reimport_pending(IMPORTED_COST)


async def test_dst_repair_keeps_checkpoint_price_of_revisable_days(tmp_path, monkeypatch):
    # Day 2 was imported by 1.3.1 at 2.0 PLN/kWh; the price is 3.0 now.
    (tmp_path / "state.json").write_text(json.dumps({
        "stats_version": 2,
        "stats": {IMPORTED_COST: {
            "last_sum": 99.0, "last_start": _utc_at(2, 1).isoformat(),
            "days": {"2026-01-02": [50.0, 2.0]},
        }},
    }))
    state = StateStore(str(tmp_path))
    imp, ws = _importer(state, monkeypatch, price_import_pln_kwh=3.0)
    hour_stat = FakeHourStat({IMPORTED_COST: {"state": 3.0, "sum": 3.0}})
    monkeypatch.setattr(imp, "_ws_hour_stat", hour_stat, raising=False)

    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(1, [1.0, 1.0]) + _day_rows(2, [1.0, 1.0])}
    )

    _, cost = ws.by_id()[IMPORTED_COST]
    assert [s["state"] for s in cost] == pytest.approx([3.0, 3.0, 2.0, 2.0])
    assert [s["sum"] for s in cost] == pytest.approx([3.0, 6.0, 8.0, 10.0])
    assert state.stats_checkpoints(IMPORTED_COST)[date(2026, 1, 2)] == pytest.approx((6.0, 2.0))


async def test_january_update_never_persists_an_uncomputed_checkpoint(tmp_path, monkeypatch):
    # 1.3.1 state on 10.01.2027: checkpoints reach back into December 2026.
    sum_before = {date(2026, 12, 9) + timedelta(days=i): 1000.0 + 24 * i for i in range(31)}
    last_start = datetime(2027, 1, 8, 23, tzinfo=WARSAW).astimezone(timezone.utc)
    (tmp_path / "state.json").write_text(json.dumps({
        "stats_version": 2,
        "stats": {IMPORTED: {
            "last_sum": 1000.0 + 24 * 31, "last_start": last_start.isoformat(),
            "days": {d.isoformat(): [s, 1.0] for d, s in sum_before.items()},
        }},
    }))
    state = StateStore(str(tmp_path))
    imp, ws = _importer(state, monkeypatch)
    jan1 = sum_before[date(2027, 1, 1)]
    monkeypatch.setattr(imp, "_ws_hour_stat",
                        FakeHourStat({IMPORTED: {"state": 1.0, "sum": jan1 + 1.0}}), raising=False)

    def days(first: date, n: int) -> list[dict[str, Any]]:
        return [
            {"timestamp": datetime.combine(first + timedelta(days=i), datetime.min.time())
             + timedelta(hours=h), "imported_kwh": 1.0, "exported_kwh": 0.0}
            for i in range(n) for h in range(24)
        ]

    # First fetch after the update: 1..8 January (one-time re-import).
    await imp.import_hourly({f"ku_{PPE}": days(date(2027, 1, 1), 8)})
    assert not state.full_reimport_pending(IMPORTED)
    # Only days rewritten by this import keep a checkpoint, each with its real sum.
    assert state.stats_checkpoints(IMPORTED) == {
        date(2027, 1, 1) + timedelta(days=i): (pytest.approx(jan1 + 24 * i), 1.0)
        for i in range(8)
    }

    # Next fetch (window reaches back into December): sums must follow HA's.
    ws.calls.clear()
    await imp.import_hourly({f"ku_{PPE}": days(date(2026, 12, 9), 31)})
    _, stats = ws.by_id()[IMPORTED]
    assert stats[0]["start"] == datetime(2027, 1, 1, tzinfo=WARSAW).astimezone(timezone.utc).isoformat()
    assert stats[0]["sum"] == pytest.approx(jan1 + 1.0)
    assert stats[-1]["sum"] == pytest.approx(jan1 + 24 * 8)


class FakeHourStatByStart:
    def __init__(self, rows: dict[datetime, dict[str, float]]):
        self.rows = rows

    async def __call__(self, statistic_id: str, start: datetime):
        return True, self.rows.get(start)


async def test_dst_repair_of_a_contract_starting_mid_year(tmp_path, monkeypatch):
    # Portal (and HA) data begin on 1 March: a complete fetch that cannot start
    # on 1 January must still run the one-time re-import, not wait forever.
    state = _legacy_state(tmp_path, {IMPORTED: (999.0, _utc_at(2, 1, month=3))})
    imp, ws = _importer(state, monkeypatch)
    monkeypatch.setattr(imp, "_ws_hour_stat", FakeHourStatByStart(
        {_utc_at(1, 0, month=3): {"state": 4.0, "sum": 4.0}}), raising=False)

    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(1, [1.0, 1.0], month=3) + _day_rows(2, [2.0, 2.0], month=3)}
    )

    _, stats = ws.by_id()[IMPORTED]
    assert [s["sum"] for s in stats] == pytest.approx([1.0, 2.0, 4.0, 6.0])
    assert not state.full_reimport_pending(IMPORTED)


async def test_legacy_reimport_waits_when_year_start_fetch_misses_anchor(
    tmp_path, monkeypatch
):
    # Fetch starts on 1 January but ends before the anchor (5 January): the
    # rewritten sums would not reach later hours, so nothing is imported yet.
    state = _legacy_state(tmp_path, {IMPORTED: (999.0, _utc_at(5, 2))})
    imp, ws = _importer(state, monkeypatch)
    hour_stat = FakeHourStat({IMPORTED: {"state": 1.0, "sum": 11.0}})
    monkeypatch.setattr(imp, "_ws_hour_stat", hour_stat, raising=False)

    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(1, [1.0, 1.0, 1.0]) + _day_rows(2, [2.0, 2.0, 2.0])}
    )

    assert hour_stat.asked == []
    assert IMPORTED not in ws.by_id()
    assert state.stats_anchor(IMPORTED) == (pytest.approx(999.0), _utc_at(5, 2))
    assert state.full_reimport_pending(IMPORTED)


async def test_missing_hour_on_an_ordinary_day_is_not_filled(state, monkeypatch):
    # Only the repeated autumn DST hour is padded with 0; a gap on any other
    # day (here July, hour 05:00-06:00 local missing) stays a gap.
    imp, ws = _importer(state, monkeypatch)
    rows = [r for r in _day_rows(15, [1.0] * 24, month=7) if r["timestamp"].hour != 5]

    await imp.import_hourly({f"ku_{PPE}": rows})

    _, stats = ws.by_id()[IMPORTED]
    starts = [s["start"] for s in stats]
    assert len(starts) == 23
    assert _utc_at(15, 5, month=7).isoformat() not in starts
    assert stats[-1]["sum"] == pytest.approx(23.0)


# ---- log privacy ----
#
# PPE (and KU) are personal data; the add-on log is often pasted into issues.
# Logs show only their last 4 digits; statistic_id itself is unchanged.


def _assert_ppe_masked(caplog) -> None:
    assert caplog.records
    assert PPE not in caplog.text
    assert PPE[-4:] in caplog.text


async def test_import_logs_mask_ppe(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    # IMPORTED takes the one-time re-import path, EXPORTED the regular one.
    state = _legacy_state(tmp_path, {IMPORTED: (999.0, _utc_at(2, 2))})
    imp, ws = _importer(state, monkeypatch)
    hour_stat = FakeHourStat({IMPORTED: {"state": 1.0, "sum": 1.0}})
    monkeypatch.setattr(imp, "_ws_hour_stat", hour_stat, raising=False)

    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(1, [1.0, 1.0, 1.0]) + _day_rows(2, [2.0, 2.0, 2.0])}
    )

    assert set(ws.by_id()) == {IMPORTED, EXPORTED}  # ids keep the full PPE
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("One-time re-import of") for m in messages)
    assert any(m.startswith("Imported 6 rows for") for m in messages)
    _assert_ppe_masked(caplog)


async def test_import_failure_logs_mask_ppe(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    state = _legacy_state(tmp_path, {IMPORTED: (999.0, _utc_at(2, 2))})
    imp = StatsImporter("http://ha:8123", "token", state)

    async def failing_ws_call(command):
        # HA error texts may echo the statistic_id back.
        sid = (command.get("statistic_ids") or [command.get("metadata", {}).get("statistic_id")])[0]
        return False, json.dumps({"code": "invalid_format", "message": f"bad {sid}"})

    monkeypatch.setattr(imp, "_ws_call", failing_ws_call)

    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(1, [1.0, 1.0, 1.0]) + _day_rows(2, [2.0, 2.0, 2.0])}
    )

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("statistics_during_period") for m in messages)
    assert any("HA statistics unavailable" in m for m in messages)
    assert any(m.startswith("import_statistics") for m in messages)
    _assert_ppe_masked(caplog)


async def test_skipped_reimport_log_masks_ppe(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    state = _legacy_state(tmp_path, {IMPORTED: (999.0, _utc_at(2, 2))})
    imp, _ = _importer(state, monkeypatch)
    monkeypatch.setattr(imp, "_ws_hour_stat", FakeHourStat({}), raising=False)

    await imp.import_hourly(
        {f"ku_{PPE}": _day_rows(1, [1.0, 1.0, 1.0]) + _day_rows(2, [2.0, 2.0, 2.0])}
    )

    assert any("one-time re-import skipped" in r.getMessage() for r in caplog.records)
    _assert_ppe_masked(caplog)
