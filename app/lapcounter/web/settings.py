"""Admin → Settings: link this lap counter to a track in RC Race HQ.

Link shows a code; the page polls /api/link every 3 s, which advances the
pairing (lapcounter/cloud.py) until RC Race HQ says it's linked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from .. import cloud, wifi
from .auth import check_csrf, login_required

bp = Blueprint("settings", __name__)


def _cfg():
    return current_app.config["LAPCOUNTER"]


def _paths() -> cloud.Paths:
    return cloud.Paths(Path(_cfg().cloud_dir))


def _api() -> cloud.Api | None:
    # Tests put a fake in app.extensions.
    fake = current_app.extensions.get("rcracehq_api")
    if fake is not None:
        return fake
    url = _cfg().rcracehq_api_url
    return cloud.Api(url) if url else None


def _back(**args):
    return redirect(url_for("settings.index", **args))


@bp.get("/settings")
@login_required
def index():
    from . import get_db
    conn = get_db()
    return render_template(
        "settings.html",
        configured=_api() is not None,
        api_url=_cfg().rcracehq_api_url,
        link=cloud.current_link(conn),
        pairing=cloud.current_pairing(conn),
        error=request.args.get("error"),
        notice=request.args.get("notice"),
        wifi=wifi.load(_cfg().wifi_dir),
    )


@bp.post("/settings/wifi")
@login_required
def set_wifi():
    check_csrf()
    form = request.form
    try:
        conf = wifi.validate(form.get("security", ""), form.get("ssid", ""),
                             form.get("passphrase", ""))
        wifi.save(_cfg().wifi_dir, conf)
    except wifi.WifiError as exc:
        return _back(error=str(exc))
    return _back(notice="Wi-Fi saved. The hotspot restarts in a moment; reconnect if you're on it.")


@bp.post("/settings/link")
@login_required
def start():
    from . import get_db
    check_csrf()
    api = _api()
    if api is None:
        return _back(error="RC Race HQ isn't configured on this Pi (RCRACEHQ_API_URL).")
    try:
        cloud.start_link(get_db(), _paths(), api)
    except cloud.CloudError as exc:
        return _back(error=str(exc))
    return _back()


@bp.get("/api/link")
@login_required
def poll():
    from . import get_db
    api = _api()
    try:
        out = cloud.poll_link(get_db(), _paths(), api) if api else {"state": "idle"}
    except cloud.CloudError as exc:
        # Keep the code: a dropped connection shouldn't throw away a pairing.
        out = {"state": "waiting", "message": str(exc)}
    resp = current_app.make_response(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.post("/settings/link/cancel")
@login_required
def cancel():
    from . import get_db
    check_csrf()
    cloud.cancel_link(get_db(), _paths())
    return _back()


@bp.post("/settings/link/check")
@login_required
def check():
    from . import get_db
    check_csrf()
    link = cloud.current_link(get_db())
    if link is None:
        return _back(error="Not linked.")
    try:
        creds = cloud.fetch_credentials(link, _paths())
    except cloud.CloudError as exc:
        return _back(error=str(exc))
    return _back(notice=f"Connection OK. AWS issued keys valid until {_until(creds.get('expiration'))}.")


@bp.post("/settings/link/forget")
@login_required
def forget():
    from . import get_db
    check_csrf()
    conn = get_db()
    link = cloud.current_link(conn)
    cloud.forget_link(conn, _paths())
    if link:
        current_app.logger.info("forgot link %s", link.get("thingName"))
    return _back(notice="Link removed from this Pi. Also Unlink it in RC Race HQ → Manage → Lap counters.")


def _until(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%H:%M UTC")
    except ValueError:
        return iso
