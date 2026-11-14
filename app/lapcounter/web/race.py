"""Race director: start a heat from an event, watch it, end it (docs/racing.md).

Timing rules live in lapcounter/race.py and stored runs in lapcounter/runs.py;
this module serves them. A run's standings are worked out from the raw passings
each time they are asked for, and frozen into results_json when the heat
finishes. Auto-finish happens on the next request or syncd tick after its
condition is met. Every start and close also goes to the outbox for RC Race HQ.
"""

from __future__ import annotations

import json

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from .. import db, race, runs
from .auth import check_csrf, is_admin, login_required

bp = Blueprint("race", __name__)

MAX_DURATION_MIN = 60
MAX_MIN_LAP_S = 120


class RaceError(ValueError):
    pass


def heat_title(h: dict) -> str:
    kind = "Qualifier" if h.get("roundType") == "qualifier" else "Main"
    return f"{h.get('className') or 'Heat'} · {kind} {h.get('roundNumber') or ''}".strip()


def heat_grid(h: dict, entries: list[dict]) -> list[dict]:
    """The heat's racers in grid order, as stored on a run. Same fallback as the
    event page: a heat with no racers picked runs the whole class."""
    by_reg = {e["registration_id"]: e for e in entries}
    picked = sorted(h.get("entries") or [], key=lambda g: g.get("grid") or 0)
    if picked:
        rows = [(g.get("grid") or i, by_reg.get(g.get("registrationId")))
                for i, g in enumerate(picked, start=1)]
    else:
        same = sorted((e for e in entries if e["racer_class"] == h.get("className")),
                      key=lambda e: e["racer_name"].lower())
        rows = list(enumerate(same, start=1))
    return [{"registrationId": e["registration_id"], "racerName": e["racer_name"],
             "transponder": race.as_int(e["transponder"]), "grid": grid}
            for grid, e in rows if e is not None]


def parse_settings(form) -> tuple[int, int]:
    try:
        minutes = float(form.get("duration_min") or race.DEFAULT_DURATION_S / 60)
        min_lap_s = float(form.get("min_lap_s") or race.DEFAULT_MIN_LAP_MS / 1000)
    except ValueError:
        raise RaceError("Heat length and minimum lap must be numbers.") from None
    if not 0.5 <= minutes <= MAX_DURATION_MIN:
        raise RaceError(f"Heat length must be between 0.5 and {MAX_DURATION_MIN} minutes.")
    if not 1 <= min_lap_s <= MAX_MIN_LAP_S:
        raise RaceError(f"Minimum lap must be between 1 and {MAX_MIN_LAP_S} seconds.")
    return round(minutes * 60), round(min_lap_s * 1000)


refresh = runs.refresh


def _run_or_404(conn, run_id: int) -> dict:
    run = db.get_heat_run(conn, run_id)
    if run is None:
        abort(404)
    return run


def _json(payload: dict):
    resp = current_app.make_response(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.post("/events/<event_id>/heats/<heat_id>/start")
@login_required
def start(event_id: str, heat_id: str):
    from . import get_db
    check_csrf()
    conn = get_db()
    row = db.get_event_pack(conn, event_id)
    if row is None:
        abort(404)
    heat = next((h for h in json.loads(row["pack_json"]).get("heats", [])
                 if h.get("heatId") == heat_id), None)
    if heat is None:
        abort(404)
    try:
        duration_s, min_lap_ms = parse_settings(request.form)
    except RaceError as exc:
        return redirect(url_for("events.show", event_id=event_id, error=str(exc)) + "#heats")
    grid = heat_grid(heat, db.list_event_entries(conn, event_id))
    now = db.now_ms()
    try:
        run_id = runs.start(
            conn, event_id=event_id, heat_id=heat_id, title=heat_title(heat),
            class_name=heat.get("className"), round_type=heat.get("roundType"),
            round_number=heat.get("roundNumber"), entries=grid, duration_s=duration_s,
            min_lap_ms=min_lap_ms, go_at_ms=now + race.COUNTDOWN_MS, at_ms=now)
    except db.HeatBusy as exc:
        return redirect(url_for("events.show", event_id=event_id, error=str(exc)) + "#heats")
    return redirect(url_for("race.show", run_id=run_id))


@bp.get("/race/<int:run_id>")
@login_required
def show(run_id: int):
    from . import get_db
    conn = get_db()
    run = _run_or_404(conn, run_id)
    return render_template("race.html", run=run, v=refresh(conn, run, db.now_ms()),
                           error=request.args.get("error"))


@bp.get("/api/race/<int:run_id>")
@login_required
def show_json(run_id: int):
    from . import get_db
    conn = get_db()
    return _json(refresh(conn, _run_or_404(conn, run_id), db.now_ms()))


@bp.get("/api/race/current")
def current_json():
    """Public: the heat on track now, else the last finished one."""
    from . import get_db
    conn = get_db()
    run = db.latest_heat_run(conn)
    if run is None:
        return _json({"phase": None, "nowMs": db.now_ms()})
    out = refresh(conn, run, db.now_ms())
    if not is_admin():
        out.pop("unknown", None)
    return _json(out)


def _close(run_id: int, allowed: set[str], state: str, refused: str):
    from . import get_db
    check_csrf()
    conn = get_db()
    run = _run_or_404(conn, run_id)
    here = url_for("race.show", run_id=run_id)
    now = db.now_ms()
    if race.phase(run, now) not in allowed:
        return redirect(url_for("race.show", run_id=run_id, error=refused))
    if state == "finished":
        runs.finish(conn, run, now)
    else:
        runs.close(conn, run, state, now)
    return redirect(here)


@bp.post("/race/<int:run_id>/cancel")
@login_required
def cancel(run_id: int):
    return _close(run_id, {race.COUNTDOWN}, race.CANCELLED,
                  "Too late to cancel: the heat has started. Use Abort instead.")


@bp.post("/race/<int:run_id>/end")
@login_required
def end(run_id: int):
    return _close(run_id, {race.RUNNING, race.FINISHING}, race.FINISHED,
                  "Only a running heat can be ended.")


@bp.post("/race/<int:run_id>/abort")
@login_required
def abort_run(run_id: int):
    return _close(run_id, {race.COUNTDOWN, race.RUNNING, race.FINISHING}, race.ABORTED,
                  "This heat isn't running.")
