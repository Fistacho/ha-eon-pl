"""Offline parser tests — no network, no credentials.

Locks two invariants:
  1. parse_oze_csv keeps the exact positional 7-column OZE mapping.
  2. The new non-OZE parsers handle the shapes captured from a live
     consumption-only account (2026-07): chart JSON, meter <select> HTML,
     GetMeterReadingsForKU JSON.

Usage:
    python test_parsers.py
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eon_pl"))

from src.api import (  # noqa: E402
    parse_chart_rows,
    parse_history_meters,
    parse_meter_readings,
    parse_oze_csv,
)


def test_oze_csv_positional_mapping() -> None:
    """The documented OZE layout: date;hour;imported;?;exported;?;balance."""
    csv_bytes = (
        "Data;Godzina;Pobrana;X;Wprowadzona;Y;Bilans\n"
        "01.07.2026;01:00;0,123;-;0,456;-;-0,333\n"
        "01.07.2026;02:00;1 234,5;-;0,0;-;1 234,5\n"
        "01.07.2026;;-;-;-;-;-\n"
    ).encode("utf-8")
    rows = parse_oze_csv(csv_bytes)
    assert len(rows) == 2, rows
    assert rows[0]["timestamp"] == datetime(2026, 7, 1, 0, 0)
    assert rows[0]["imported_kwh"] == 0.123
    assert rows[0]["exported_kwh"] == 0.456
    assert rows[0]["balance_kwh"] == -0.333
    assert rows[1]["imported_kwh"] == 1234.5
    assert rows[1]["balance_kwh"] == 1234.5


def test_chart_rows() -> None:
    data = {
        "ChartType": "compareYear",
        "Categories": ["01.12.2024", "01.05.2026"],
        "Results": [
            {"X": 1, "Y": 3507.38, "CategoryId": "01.05.2026", "LegendId": 0},
            {"X": 0, "Y": 7239.36, "CategoryId": "01.12.2024", "LegendId": 0},
            {"X": 2, "Y": None, "CategoryId": "01.06.2026", "LegendId": 0},
            {"X": 3, "Y": 1.0, "CategoryId": "not-a-date", "LegendId": 0},
        ],
    }
    rows = parse_chart_rows(data)
    assert [(r["date"], r["kwh"]) for r in rows] == [
        (date(2024, 12, 1), 7239.36),
        (date(2026, 5, 1), 3507.38),
    ], rows
    assert parse_chart_rows(None) == []
    assert parse_chart_rows({"Faulted": True}) == []


def test_history_meters() -> None:
    html = """
    <div class="custom-select-info">Nr licznika</div>
    <select name="meterId" class="consumption-meter form-control">
        <option value="306611600004" data-type="active">30870098</option>
        <option value="305991359004" data-type="inactive">4220978</option>
    </select>
    """
    meters = parse_history_meters(html)
    assert meters == [
        {"meter_id": "306611600004", "active": True, "serial": "30870098"},
        {"meter_id": "305991359004", "active": False, "serial": "4220978"},
    ], meters
    assert parse_history_meters("<html>no select</html>") == []


def test_meter_readings() -> None:
    data = {
        "Result": [
            {
                "DateValue": "/Date(1748642400000)/",
                "Type": "Odczyt zdalny",
                "MeterSerial": "30870098",
                "Readings": [{"read_type": "Całodobowa", "read_value": "5 715,30 kWh"}],
                "EnergyType": "Pobrana",
            },
            {
                "DateValue": "/Date(1780178400000)/",
                "Type": "Odczyt zdalny",
                "MeterSerial": "30870098",
                "Readings": [{"read_type": "Całodobowa", "read_value": "14529,52 kWh"}],
                "EnergyType": "Pobrana",
            },
        ]
    }
    rows = parse_meter_readings(data)
    assert rows[0]["value_kwh"] == 14529.52, rows  # newest first
    assert rows[0]["date"].year == 2026
    assert rows[1]["value_kwh"] == 5715.30
    assert parse_meter_readings(None) == []


if __name__ == "__main__":
    for fn in (
        test_oze_csv_positional_mapping,
        test_chart_rows,
        test_history_meters,
        test_meter_readings,
    ):
        fn()
        print(f"ok: {fn.__name__}")
    print("all parser tests passed")
