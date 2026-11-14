"""Stored heat runs: scoring, auto-finish and their outbox records.

The timing rules are pure functions in lapcounter/race.py; this module applies
them to runs in the database. Both the web app (lapcounter/web/race.py) and
syncd (lapcounter/syncd.py) call it, so a heat finishes on time and its laps
reach the outbox even when nobody has the race page open.
"""

from __future__ import annotations

import logging
import sqlite3

from . import db, outbox, race

LOG = logging.getLogger("lapcounter.runs")


def score(conn: sqlite3.Connection, run: dict, now_ms: int) -> race.Scored:
    end = run["ended_at_ms"] or now_ms
    return race.standings(run, db.passings_between(conn, run["go_at_ms"], end))


def start(conn: sqlite3.Connection, **kwargs) -> int:
    """db.start_heat_run, plus checkout of its event and the heat.state record.
    Raises db.HeatBusy."""
    run_id = db.start_heat_run(conn, **kwargs)
    run = db.get_heat_run(conn, run_id)
    outbox.checkout(conn, run["event_id"], run["created_at_ms"])
    outbox.heat_state(conn, run, "staged", run["created_at_ms"])
    LOG.info("heat run %s (%s) countdown started", run_id, run["title"])
    return run_id


def record_progress(conn: sqlite3.Connection, run: dict, now_ms: int) -> None:
    """Outbox records for a live run: 'running' once it's past go, and every
    counted crossing so far. Safe to call as often as you like."""
    if race.phase(run, now_ms) in (race.RUNNING, race.FINISHING):
        outbox.heat_state(conn, run, "running", run["go_at_ms"])
        end = run["ended_at_ms"] or now_ms
        outbox.heat_laps(conn, run, db.passings_between(conn, run["go_at_ms"], end))


def finish(conn: sqlite3.Connection, run: dict, now_ms: int) -> bool:
    ended = dict(run, ended_at_ms=now_ms)
    scored = score(conn, ended, now_ms)
    results = {"standings": [s.as_dict() for s in scored.standings], "unknown": scored.unknown}
    closed = db.end_heat_run(conn, run["id"], "finished", results=results, at_ms=now_ms)
    if closed:
        record_progress(conn, ended, now_ms)
        outbox.heat_state(conn, ended, race.FINISHED, now_ms)
        outbox.heat_result(conn, ended, results, now_ms)
        LOG.info("heat run %s (%s) finished", run["id"], run["title"])
    return closed


def close(conn: sqlite3.Connection, run: dict, state: str, now_ms: int) -> bool:
    """Cancel (countdown) or abort a run. Laps already sent stay in RC Race HQ
    under that run; the heat's state says it was thrown away."""
    closed = db.end_heat_run(conn, run["id"], state, at_ms=now_ms)
    if closed:
        outbox.heat_state(conn, dict(run, ended_at_ms=now_ms), state, now_ms)
        LOG.info("heat run %s (%s) %s", run["id"], run["title"], state)
    return closed


def refresh(conn: sqlite3.Connection, run: dict, now_ms: int) -> dict:
    """The run's view, finishing it first if time-up and every car is in."""
    if run["state"] == "finished" and run["results"] is not None:
        out = race.view(run, race.Scored([], []), now_ms)
        out["standings"] = run["results"]["standings"]
        out["unknown"] = run["results"]["unknown"]
        return out
    scored = score(conn, run, now_ms)
    if run["state"] == "started" and race.should_finish(run, scored, now_ms):
        finish(conn, run, now_ms)
        return refresh(conn, db.get_heat_run(conn, run["id"]), now_ms)
    return race.view(run, scored, now_ms)


def tick(conn: sqlite3.Connection, now_ms: int) -> None:
    """syncd's once-a-second step for the heat on track (if any)."""
    run = db.active_heat_run(conn)
    if run is None:
        return
    refresh(conn, run, now_ms)
    run = db.get_heat_run(conn, run["id"])
    if run["state"] == "started":
        record_progress(conn, run, now_ms)
