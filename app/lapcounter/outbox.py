"""Outbox to RC Race HQ: what happened on race day, in order (docs/club-sync.md).

Everything RC Race HQ should record is appended here first, in the same
SQLite file, so nothing is lost if the internet or the cloud is down. syncd
(lapcounter/syncd.py) sends pending records in seq order and marks them
acked once RC Race HQ confirms it applied them.

Each record has a `uuid` that is the same however often it's produced (e.g.
`lap:<runId>:<transponder>:<lapNo>`), so building records again from the
same data adds nothing. `outbox_id` is random per database, so a re-imaged
Pi starts a fresh sequence in RC Race HQ.

Record types (the cloud side is RCRaceHQ backend/src/lib/lapCounterOutbox.ts):
  event.checkout / event.finish  {}
  heat.state   {heatId, runId, state, goAtMs, durationS, minLapMs, entries}
  heat.lap     {heatId, runId, transponder, registrationId, racerName, lapNo, lapMs, tsMs}
  heat.result  {heatId, runId, standings}
"""

from __future__ import annotations

import json
import secrets
import sqlite3

from . import db, race

OUTBOX_ID_KEY = "outbox.id"
STATUS_KEY = "outbox.status"
BATCH = 200  # matches RC Race HQ's MAX_RECORDS; ~40 KB of laps, well under IoT's 128 KB


def outbox_id(conn: sqlite3.Connection) -> str:
    return db.setdefault_setting(conn, OUTBOX_ID_KEY, secrets.token_hex(8))


def run_id(conn: sqlite3.Connection, run: dict) -> str:
    """A heat run's id in RC Race HQ: unique across re-imaged Pis."""
    return f"{outbox_id(conn)}.{run['id']}"


def add(conn: sqlite3.Connection, type_: str, event_id: str, payload: dict, uuid: str,
        at_ms: int | None = None) -> bool:
    """Append a record. False if one with this uuid is already there."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox (uuid, type, event_id, payload_json, created_at_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        (uuid, type_, event_id, json.dumps(payload, separators=(",", ":")), at_ms or db.now_ms()),
    )
    return cur.rowcount == 1


def pending(conn: sqlite3.Connection, limit: int = BATCH) -> list[dict]:
    rows = conn.execute(
        "SELECT seq, uuid, type, event_id, payload_json, created_at_ms FROM outbox"
        " WHERE acked_at_ms IS NULL ORDER BY seq LIMIT ?", (limit,)).fetchall()
    return [{"seq": r["seq"], "uuid": r["uuid"], "type": r["type"], "eventId": r["event_id"],
             "at": r["created_at_ms"], "payload": json.loads(r["payload_json"])} for r in rows]


def acknowledge(conn: sqlite3.Connection, upto_seq: int, rejected: list[dict] | None = None,
                at_ms: int | None = None) -> int:
    """Mark every pending record up to `upto_seq` done. Rejected ones keep RC Race
    HQ's reason so the Events page can show it. Returns how many were marked."""
    at = at_ms or db.now_ms()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("UPDATE outbox SET acked_at_ms = ? WHERE acked_at_ms IS NULL AND seq <= ?",
                           (at, upto_seq))
        for r in rejected or []:
            if isinstance(r, dict) and isinstance(r.get("seq"), int) and r["seq"] <= upto_seq:
                conn.execute("UPDATE outbox SET rejected = ? WHERE seq = ?",
                             (str(r.get("reason") or "rejected")[:300], r["seq"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT SUM(acked_at_ms IS NULL) AS waiting, MIN(CASE WHEN acked_at_ms IS NULL"
        " THEN created_at_ms END) AS oldest_ms, COUNT(*) AS total FROM outbox").fetchone()
    rejected = conn.execute(
        "SELECT seq, type, event_id, rejected, created_at_ms FROM outbox WHERE rejected IS NOT NULL"
        " ORDER BY seq DESC LIMIT 10").fetchall()
    return {"waiting": row["waiting"] or 0, "oldestMs": row["oldest_ms"], "total": row["total"],
            "rejected": [dict(r) for r in rejected]}


def set_status(conn: sqlite3.Connection, **status) -> None:
    """syncd's last attempt ({at_ms, ok, error, sent}), shown on the Events page."""
    db.set_setting(conn, STATUS_KEY, json.dumps(status))


def status(conn: sqlite3.Connection) -> dict:
    raw = db.get_setting(conn, STATUS_KEY)
    return {**counts(conn), "last": json.loads(raw) if raw else None}


# --- checkout ----------------------------------------------------------------

def is_checked_out(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM event_checkout WHERE event_id = ?", (event_id,)).fetchone() is not None


def checkout(conn: sqlite3.Connection, event_id: str, at_ms: int | None = None) -> bool:
    """Race day: this Pi owns the event. RC Race HQ locks its heats. False if
    it was already checked out here."""
    at = at_ms or db.now_ms()
    cur = conn.execute("INSERT OR IGNORE INTO event_checkout (event_id, checked_out_at_ms) VALUES (?, ?)",
                       (event_id, at))
    if cur.rowcount != 1:
        return False
    add(conn, "event.checkout", event_id, {}, f"checkout:{event_id}:{at}", at)
    return True


def finish_event(conn: sqlite3.Connection, event_id: str, at_ms: int | None = None) -> bool:
    """Hand the event back: RC Race HQ unlocks it once the outbox has drained
    up to here. False if it wasn't checked out."""
    at = at_ms or db.now_ms()
    cur = conn.execute("DELETE FROM event_checkout WHERE event_id = ?", (event_id,))
    if cur.rowcount != 1:
        return False
    add(conn, "event.finish", event_id, {}, f"finish:{event_id}:{at}", at)
    return True


# --- heat records ------------------------------------------------------------

def _heat(conn: sqlite3.Connection, run: dict) -> dict:
    return {"heatId": run["heat_id"], "runId": run_id(conn, run)}


def heat_state(conn: sqlite3.Connection, run: dict, state: str, at_ms: int | None = None) -> bool:
    """staged (countdown) / running / finished / aborted / cancelled."""
    payload = {**_heat(conn, run), "state": state, "goAtMs": run["go_at_ms"],
               "durationS": run["duration_s"], "minLapMs": run["min_lap_ms"],
               "entries": run["entries"]}
    return add(conn, "heat.state", run["event_id"], payload,
               f"state:{payload['runId']}:{state}", at_ms)


def heat_laps(conn: sqlite3.Connection, run: dict, passings: list[dict]) -> int:
    """Every counted crossing so far that isn't in the outbox yet. Returns how many were added."""
    base = _heat(conn, run)
    added = 0
    for c in race.grid_crossings(run, passings):
        uuid = f"lap:{base['runId']}:{c['transponder']}:{c['lapNo']}"
        added += add(conn, "heat.lap", run["event_id"], {**base, **c}, uuid, c["tsMs"])
    return added


def heat_result(conn: sqlite3.Connection, run: dict, results: dict, at_ms: int | None = None) -> bool:
    payload = {**_heat(conn, run), "standings": results.get("standings", [])}
    return add(conn, "heat.result", run["event_id"], payload, f"result:{payload['runId']}", at_ms)
