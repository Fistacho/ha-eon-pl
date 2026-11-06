"""Customer numbers (KU, PPE) must not reach the add-on log at INFO/WARNING.

The add-on log is often pasted into issues. The numbers here are fictitious.
"""
from __future__ import annotations

import logging

import httpx
import pytest
from src.api import EonPolskaClient
from src.config import configure_logging
from src.coordinator import EonCoordinator

KU = "10000000042"
PPE = "590000000000009876"


@pytest.fixture
def restore_lib_loggers():
    names = ("httpx", "httpcore", "mqtt")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def _portal(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/GetPHList"):
        return httpx.Response(200, json={
            "HasOze": True,
            "Partners": [{"ContractAccounts": [
                {"Id": KU, "IsActive": True, "PPEList": [{"Id": PPE}]},
            ]}],
        })
    if path.endswith("/GetBillingData"):
        # Dead session even after re-login -> "auth failed" + "Billing unavailable".
        return httpx.Response(302, headers={"location": "https://eon.pl/mojeon/Logowanie"})
    if path.endswith("/GetOzeAgrData"):
        return httpx.Response(500)
    if path.endswith("/GenerateOzeReport"):
        # Session never warms up -> "hourly: dropped session" + "hourly unavailable".
        return httpx.Response(302, headers={"location": "https://eon.pl/mojeon/Historia-zuzycia"})
    return httpx.Response(200, json={})


async def _fetch(mock_httpx) -> EonCoordinator:
    mock_httpx["handler"] = _portal

    async def relogin() -> str:
        return "fresh-cookie"

    coord = EonCoordinator(EonPolskaClient("cookie"), selected_kus=[], relogin=relogin)
    try:
        await coord.fetch()
    finally:
        await coord.client.aclose()
    return coord


async def test_fetch_logs_mask_ku_and_ppe(mock_httpx, caplog, restore_lib_loggers):
    configure_logging("info")
    caplog.set_level(logging.INFO)

    coord = await _fetch(mock_httpx)

    # The numbers still go to the portal and stay in the contract keys.
    assert any(f"ppe={PPE}" in url for url in mock_httpx["calls"])
    assert list(coord.contracts) == [f"{KU}_{PPE}"]

    messages = [r.getMessage() for r in caplog.records]
    for expected in ("auth failed", "Billing unavailable", "OZE unavailable",
                     "hourly: dropped session", "hourly unavailable"):
        assert any(expected in m for m in messages), expected
    assert KU not in caplog.text
    assert PPE not in caplog.text
    assert PPE[-4:] in caplog.text


@pytest.mark.parametrize("level", ["info", "warning"])
async def test_httpx_request_lines_are_not_logged(mock_httpx, caplog, restore_lib_loggers, level):
    # httpx logs every request URL (with ku= and ppe= query params) at INFO.
    configure_logging(level)
    caplog.set_level(logging.INFO)

    await _fetch(mock_httpx)

    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]


def test_mqtt_debug_lines_are_not_logged(caplog, restore_lib_loggers):
    # aiomqtt hands logging.getLogger("mqtt") to paho, which at DEBUG logs
    # "Sending PUBLISH ... '<topic>'" with topics like eon_pl_{KU}_{PPE}.
    configure_logging("debug")
    caplog.set_level(logging.DEBUG)

    logging.getLogger("mqtt").debug(
        "Sending PUBLISH (d0, q0, r1, m1), 'b'homeassistant/sensor/eon_pl_%s_%s/state''",
        KU, PPE,
    )

    assert not [r for r in caplog.records if r.name == "mqtt"]
    assert KU not in caplog.text
    assert PPE not in caplog.text
