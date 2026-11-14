"""Admin → Events: download this club's events from RC Race HQ and look at them.

The download itself is lapcounter/eventsync.py (docs/event-sync.md). Heats are
started from the event page; running them is lapcounter/web/race.py.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from .. import checkin, cloud, db, eventsync, outbox, race, scoring
from .auth import check_csrf, login_required

bp = Blueprint("events", __name__)

ROUND_ORDER = {"qualifier": 0, "main": 1}


def _cfg():
    return current_app.config["LAPCOUNTER"]


def _sync(conn, link: dict) -> eventsync.Result:
    # Tests put a fake in app.extensions.
    fake = current_app.extensions.get("event_sync")
    if fake is not None:
        return fake(conn, link)
    return eventsync.sync_events(conn, link, cloud.Paths(Path(_cfg().cloud_dir)))


@bp.get("/events")
@login_required
def index():
    from . import get_db
    conn = get_db()
    return render_template(
        "events.html",
        link=cloud.current_link(conn),
        last=eventsync.last_sync(conn),
        events=db.list_events(conn),
        counts=eventsync.racer_counts(conn, _cfg().rc4_dir),
        error=request.args.get("error"),
        notice=request.args.get("notice"),
    )


@bp.post("/events/download")
@login_required
def download():
    from . import get_db
    check_csrf()
    conn = get_db()
    link = cloud.current_link(conn)
    if link is None:
        return redirect(url_for("events.index", error="This Pi isn't linked to RC Race HQ yet."))
    try:
        result = _sync(conn, link)
    except cloud.CloudError as exc:
        current_app.logger.warning("event download failed: %s", exc)
        return redirect(url_for("events.index", error=str(exc)))
    current_app.logger.info("event download: %s", result.message())
    return redirect(url_for("events.index", notice=result.message()))


@bp.get("/events/<event_id>")
@login_required
def show(event_id: str):
    from . import get_db
    conn = get_db()
    row = db.get_event_pack(conn, event_id)
    if row is None:
        abort(404)
    pack = json.loads(row["pack_json"])
    entries = db.list_event_entries(conn, event_id)
    known = eventsync.known_transponders(conn, _cfg().rc4_dir)
    by_class: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_class[e["racer_class"]].append(e)
    by_reg = {e["registration_id"]: e for e in entries}
    return render_template(
        "event.html",
        ev=row,
        pack=pack,
        days=heats_by_day(pack),
        by_class=by_class,
        by_reg=by_reg,
        known=known,
        counts=eventsync.racer_counts(conn, _cfg().rc4_dir, event_id).get(
            event_id, {"racers": 0, "missing": 0, "classes": {}}),
        runs=last_runs(conn, event_id),
        active=db.active_heat_run(conn),
        qualifying=qualifying(conn, event_id, pack),
        defaults={"duration_min": race.DEFAULT_DURATION_S // 60,
                  "min_lap_s": race.DEFAULT_MIN_LAP_MS // 1000},
        error=request.args.get("error"),
        checkout=outbox.is_checked_out(conn, event_id),
        checked_in=checkin.checked_in(conn, event_id),
        ready_ids=checkin.ready(conn, event_id),
        checkin_note=checkin.note(conn),
    )


def last_runs(conn, event_id: str) -> dict[str, dict]:
    """heatId -> its latest run that wasn't cancelled (finished, aborted or live)."""
    out: dict[str, dict] = {}
    for r in db.heat_runs_for_event(conn, event_id):
        if r["state"] != race.CANCELLED:
            out[r["heat_id"]] = r
    return out


def qualifying(conn, event_id: str, pack: dict) -> list[tuple[str, list[dict]]]:
    """[(class, ranking)] from finished qualifier heats, classes in pack order."""
    latest: dict[str, dict] = {}  # a re-run replaces the heat's earlier results
    for r in db.heat_runs_for_event(conn, event_id):
        if r["state"] == race.FINISHED and r["round_type"] == "qualifier" and r["results"]:
            latest[r["heat_id"]] = r
    per_class: dict[str, list[list[dict]]] = defaultdict(list)
    for r in latest.values():
        per_class[r["class_name"] or ""].append(r["results"]["standings"])
    order = [c.get("name") for c in pack.get("classes", [])]
    names = [c for c in order if c in per_class] + sorted(set(per_class) - set(order))
    return [(c, scoring.rank_qualifying(per_class[c])) for c in names]


def heats_by_day(pack: dict) -> list[tuple[str, list[dict]]]:
    """[(day, heats in run order)]. Heats without a day go on the event's first day;
    heats without an order go after the ordered ones (same as RC Race HQ)."""
    first = (pack.get("days") or [str(pack.get("dateISO", ""))[:10]])[0]
    days: dict[str, list[dict]] = defaultdict(list)
    for h in pack.get("heats", []):
        days[h.get("dayISO") or first].append(h)
    def key(h: dict):
        order = h.get("order")
        return (order is None, order or 0, ROUND_ORDER.get(h.get("roundType"), 2),
                h.get("roundNumber") or 0, h.get("className") or "")
    return [(day, sorted(heats, key=key)) for day, heats in sorted(days.items())]
