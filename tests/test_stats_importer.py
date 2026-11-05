"""StatsImporter: kWh + optional PLN cost statistics, anchors, no network."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
