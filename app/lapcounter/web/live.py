"""Live view: decoder health and the latest passings, pushed with SSE."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from flask import Blueprint, Response, current_app, render_template, request

from .. import db
from .auth import login_required

bp = Blueprint("live", __name__)

STREAM_SECONDS = 55   # browsers' EventSource reconnects on its own after this
POLL_SECONDS = 0.5


def decoder_view(status: dict | None, stale_s: float, now_ms: int) -> dict:
    if status is None:
        return {"online": False, "seen": False}
    age_s = (now_ms - status["updated_at_ms"]) / 1000
    online = status.get("noise") is not None and age_s <= stale_s
    return {**status, "online": online, "seen": True, "age_s": round(age_s, 1)}


def snapshot(conn, stale_s: float, after_id: int = 0) -> dict:
    return {
        "decoder": decoder_view(db.decoder_status(conn), stale_s, db.now_ms()),
        "passings": db.recent_passings(conn, after_id=after_id, limit=50),
    }


def _after_id() -> int:
    raw = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


@bp.get("/")
def index():
    from . import get_db
    from .. import checkin
    # The hotspot's captive portal lands phones here; point racers at check-in.
    return render_template("live.html", checkin_open=bool(checkin.open_events(get_db())))


@bp.get("/api/live")
def live_json():
    from . import get_db
    cfg = current_app.config["LAPCOUNTER"]
    return snapshot(get_db(), cfg.decoder_stale_s, _after_id())


def sse(event: str, data: dict, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def stream_events(db_path: str, stale_s: float, after_id: int,
                  seconds: float = STREAM_SECONDS, poll: float = POLL_SECONDS) -> Iterator[str]:
    conn = db.connect(db_path)
    try:
        yield "retry: 2000\n\n"
        deadline = time.monotonic() + seconds
        last_status = None
        while True:
            for p in db.recent_passings(conn, after_id=after_id, limit=200):
                after_id = p["id"]
                yield sse("passing", p, p["id"])
            status = decoder_view(db.decoder_status(conn), stale_s, db.now_ms())
            status.pop("age_s", None)  # changes every tick; only send real changes
            if status != last_status:
                last_status = status
                yield sse("decoder", status)
            if time.monotonic() >= deadline:
                return
            time.sleep(poll)
    finally:
        conn.close()


@bp.get("/api/live/stream")
def live_stream():
    cfg = current_app.config["LAPCOUNTER"]
    body = stream_events(cfg.db_path, cfg.decoder_stale_s, _after_id())
    return Response(body, mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@bp.get("/admin")
@login_required
def admin():
    from . import get_db
    cfg = current_app.config["LAPCOUNTER"]
    return render_template("admin.html", data=snapshot(get_db(), cfg.decoder_stale_s),
                           decoder_url=cfg.decoder_url)
