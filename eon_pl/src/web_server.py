"""Tiny aiohttp web UI exposed via Home Assistant ingress."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web

_LOGGER = logging.getLogger(__name__)


def build_app(
    *,
    coordinator,
    state_store,
    cookie_store,
    trigger_login: Callable[[], Awaitable[None]],
    trigger_fetch: Callable[[], Awaitable[None]],
) -> web.Application:
    app = web.Application()

    async def index(_req: web.Request) -> web.Response:
        last_login = state_store.login_recorded()
        last_fetch = state_store.fetch_recorded()
        cookie, captured = cookie_store.load()

        contracts = coordinator.contracts
        rows = []
        for key, c in contracts.items():
            ku = c.get("ku") or {}
            ppe = c.get("ppe") or {}
            last_hr = coordinator.last_hour.get(key, {})
            rows.append({
                "key": key,
                "ku_name": ku.get("KuDisplayName") or "?",
                "ppe_name": ppe.get("PPEDisplayName") or "?",
                "last_hour_imported": last_hr.get("imported_kwh"),
                "last_hour_timestamp": _fmt(last_hr.get("timestamp")),
            })

        body = _render_html(
            last_login=_fmt(last_login),
            last_fetch=_fmt(last_fetch),
            cookie_present=bool(cookie),
            cookie_captured=_fmt(captured),
            contracts=rows,
        )
        return web.Response(text=body, content_type="text/html")

    async def post_relogin(_req: web.Request) -> web.Response:
        try:
            await trigger_login()
            return web.json_response({"status": "ok"})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def post_refetch(_req: web.Request) -> web.Response:
        try:
            await trigger_fetch()
            return web.json_response({"status": "ok"})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/", index)
    app.router.add_post("/relogin", post_relogin)
    app.router.add_post("/refetch", post_refetch)
    app.router.add_get("/health", health)
    return app


def _fmt(t: Any) -> str:
    if t is None:
        return "—"
    if isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return str(t)


def _render_html(*, last_login, last_fetch, cookie_present, cookie_captured, contracts) -> str:
    rows_html = "".join(
        f"<tr><td>{c['ku_name']}</td><td>{c['ppe_name']}</td>"
        f"<td>{c['last_hour_imported'] or '—'} kWh</td>"
        f"<td>{c['last_hour_timestamp']}</td></tr>"
        for c in contracts
    ) or "<tr><td colspan=4>brak danych — naciśnij <em>Pobierz dane</em></td></tr>"
    cookie_state = "✓ zapisany" if cookie_present else "✗ brak"

    return f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8">
<title>E.ON Polska — addon</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2em auto; padding: 0 1em; }}
  h1 {{ font-weight: 400; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 1em; }}
  th, td {{ text-align: left; padding: .5em; border-bottom: 1px solid #eee; }}
  .meta {{ display: grid; grid-template-columns: 200px 1fr; gap: .5em; margin: 1em 0; }}
  .meta div:nth-child(odd) {{ color: #666; }}
  button {{ padding: .6em 1.2em; margin-right: .5em; cursor: pointer; }}
</style>
</head><body>
<h1>E.ON Polska — Mój E.ON → Home Assistant</h1>

<div class="meta">
  <div>Ostatni login</div><div>{last_login}</div>
  <div>Ostatni fetch</div><div>{last_fetch}</div>
  <div>Cookie</div><div>{cookie_state} (przechwycony: {cookie_captured})</div>
</div>

<button onclick="post('/relogin', this)">Zaloguj ponownie</button>
<button onclick="post('/refetch', this)">Pobierz dane teraz</button>

<h2>Liczniki</h2>
<table>
<thead><tr><th>KU</th><th>PPE</th><th>Pobrana (ostatnia h)</th><th>Timestamp</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>

<script>
async function post(url, btn) {{
  btn.disabled = true; const orig = btn.textContent;
  btn.textContent = 'Pracuję…';
  try {{
    const r = await fetch(url, {{method:'POST'}});
    const j = await r.json();
    btn.textContent = j.status === 'ok' ? 'OK' : ('Błąd: ' + j.message);
    setTimeout(() => location.reload(), 1500);
  }} catch (e) {{ btn.textContent = 'Błąd: ' + e; }}
  finally {{ setTimeout(() => {{ btn.disabled = false; btn.textContent = orig; }}, 3000); }}
}}
</script>
</body></html>"""
