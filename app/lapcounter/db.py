"""SQLite storage shared by lapd and the web app.

One file, WAL mode, short transactions. lapd is the main writer (a few rows a
second at most); the web app reads, plus writes the admin row and settings.
Schema changes go in MIGRATIONS and are applied in order by `migrate`, tracked
with PRAGMA user_version.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

MIGRATIONS: tuple[str, ...] = (
    # 1: first-run admin, settings, raw passings, decoder health
    """
    CREATE TABLE admin (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT NOT NULL,
        pw_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE passing (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        heat_id INTEGER,
        transponder INTEGER NOT NULL,
        kind TEXT NOT NULL,
        decoder_ts_ms INTEGER NOT NULL,
        received_at_ms INTEGER NOT NULL,
        rssi REAL NOT NULL,
        hits INTEGER
    );
    CREATE INDEX passing_received ON passing (received_at_ms);
    CREATE TABLE decoder_status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        updated_at_ms INTEGER NOT NULL,
        noise REAL,
        received INTEGER,
        processed INTEGER,
        learn_event TEXT,
        learn_transponder INTEGER,
        learn_at_ms INTEGER
    );
    """,
    # 2: transponder learning (learn sessions, every L line, learned transponders)
    """
    CREATE TABLE learn_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL,
        transponder INTEGER,
        value REAL,
        received_at_ms INTEGER NOT NULL
    );
    CREATE INDEX learn_event_received ON learn_event (received_at_ms);
    CREATE TABLE learn_session (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label INTEGER NOT NULL,
        racer_name TEXT,
        state TEXT NOT NULL,
        started_at_ms INTEGER NOT NULL,
        finished_at_ms INTEGER,
        auto_id INTEGER,
        payloads INTEGER,
        error TEXT
    );
    CREATE UNIQUE INDEX learn_session_one_open ON learn_session (state) WHERE state = 'open';
    CREATE TABLE transponder (
        number INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        racer_name TEXT,
        rc4_sha256 TEXT,
        learned_at_ms INTEGER NOT NULL,
        source TEXT NOT NULL
    );
    """,
    # 3: events downloaded from RC Race HQ (docs/event-sync.md). The pack is kept
    # whole; entries are split out so "transponders to learn" is one query.
    """
    CREATE TABLE event_pack (
        event_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        date_iso TEXT NOT NULL,
        end_date_iso TEXT,
        location TEXT,
        sha256 TEXT NOT NULL,
        pack_json TEXT NOT NULL,
        downloaded_at_ms INTEGER NOT NULL
    );
    CREATE TABLE event_entry (
        event_id TEXT NOT NULL REFERENCES event_pack (event_id) ON DELETE CASCADE,
        registration_id TEXT NOT NULL,
        racer_name TEXT NOT NULL,
        racer_class TEXT NOT NULL,
        status TEXT,
        transponder TEXT,
        vehicle TEXT,
        PRIMARY KEY (event_id, registration_id)
    );
    """,
    # 4: running heats (docs/racing.md). No foreign key to event_pack: a
    # re-download replaces the pack and must never take results with it. The
    # grid is copied in at start so later downloads can't change a run.
    """
    CREATE TABLE heat_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        heat_id TEXT NOT NULL,
        title TEXT NOT NULL,
        class_name TEXT,
        round_type TEXT,
        round_number INTEGER,
        entries_json TEXT NOT NULL,
        duration_s INTEGER NOT NULL,
        min_lap_ms INTEGER NOT NULL,
        state TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL,
        go_at_ms INTEGER NOT NULL,
        ended_at_ms INTEGER,
        results_json TEXT
    );
    CREATE UNIQUE INDEX heat_run_one_active ON heat_run (state) WHERE state = 'started';
    CREATE INDEX heat_run_heat ON heat_run (event_id, heat_id);
    CREATE INDEX passing_heat ON passing (heat_id);
    """,
    # 5: "notify racers we're missing their transponder" requests. Texting comes
    # later (RC Race HQ sends it); until then sent_at_ms stays NULL. No foreign
    # key: a re-download replaces the event's entries but keeps the history.
    """
    CREATE TABLE transponder_notice (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        registration_id TEXT NOT NULL,
        racer_name TEXT NOT NULL,
        reason TEXT NOT NULL,
        transponder TEXT,
        requested_at_ms INTEGER NOT NULL,
        sent_at_ms INTEGER
    );
    CREATE INDEX transponder_notice_entry ON transponder_notice (event_id, registration_id);
    """,
    # 6: outbox to RC Race HQ (docs/club-sync.md, lapcounter/outbox.py) and
    # which events this Pi has checked out for race day. IF NOT EXISTS because
    # the first outbox build ran as migration 5 on a test Pi; that Pi
    # also gets transponder_notice here, since its 5 was the outbox.
    """
    CREATE TABLE IF NOT EXISTS outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL,
        event_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL,
        acked_at_ms INTEGER,
        rejected TEXT
    );
    CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (seq) WHERE acked_at_ms IS NULL;
    CREATE TABLE IF NOT EXISTS event_checkout (
        event_id TEXT PRIMARY KEY,
        checked_out_at_ms INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS transponder_notice (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        registration_id TEXT NOT NULL,
        racer_name TEXT NOT NULL,
        reason TEXT NOT NULL,
        transponder TEXT,
        requested_at_ms INTEGER NOT NULL,
        sent_at_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS transponder_notice_entry ON transponder_notice (event_id, registration_id);
    """,
    # 7: walk-up check-in (lapcounter/checkin.py). The pack carries a first
    # name and hashed phone tags per entry; check-ins are kept apart from
    # event_entry so a re-download doesn't wipe them.
    """
    ALTER TABLE event_entry ADD COLUMN first_name TEXT;
    ALTER TABLE event_entry ADD COLUMN phone_tags TEXT;
    CREATE TABLE entry_checkin (
        event_id TEXT NOT NULL,
        registration_id TEXT NOT NULL,
        checked_in_at_ms INTEGER NOT NULL,
        PRIMARY KEY (event_id, registration_id)
    );
    """,
    # 8: "Ready to race!" from the check-in checklist (docs/checkin.md).
    """
    ALTER TABLE entry_checkin ADD COLUMN ready_at_ms INTEGER;
    """,
)


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the schema version afterwards."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would COMMIT our transaction, so split by statement.
            for statement in (s.strip() for s in sql.split(";")):
                if statement:
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return conn.execute("PRAGMA user_version").fetchone()[0]


def open_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


# --- admin -------------------------------------------------------------------

def admin_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM admin WHERE id = 1").fetchone() is not None


def create_admin(conn: sqlite3.Connection, username: str, pw_hash: str) -> bool:
    """Insert the one admin. False if one already exists (setup is first-run only)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO admin (id, username, pw_hash, created_at) VALUES (1, ?, ?, ?)",
        (username, pw_hash, now_ms()),
    )
    return cur.rowcount == 1


def get_admin(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT username, pw_hash FROM admin WHERE id = 1").fetchone()


def set_admin_hash(conn: sqlite3.Connection, pw_hash: str) -> None:
    conn.execute("UPDATE admin SET pw_hash = ? WHERE id = 1", (pw_hash,))


# --- settings ----------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def setdefault_setting(conn: sqlite3.Connection, key: str, value: str) -> str:
    """Store value only if key is unset; return whichever value is stored."""
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    return get_setting(conn, key)  # type: ignore[return-value]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --- decoder feed ------------------------------------------------------------

def insert_passing(conn: sqlite3.Connection, *, transponder: int, kind: str,
                   decoder_ts_ms: int, rssi: float, hits: int | None,
                   received_at_ms: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO passing (transponder, kind, decoder_ts_ms, received_at_ms, rssi, hits)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (transponder, kind, decoder_ts_ms, received_at_ms or now_ms(), rssi, hits),
    )
    return int(cur.lastrowid)


def update_stats(conn: sqlite3.Connection, *, noise: float, received: int, processed: int,
                 at_ms: int | None = None) -> None:
    conn.execute(
        "INSERT INTO decoder_status (id, updated_at_ms, noise, received, processed)"
        " VALUES (1, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET updated_at_ms = excluded.updated_at_ms,"
        " noise = excluded.noise, received = excluded.received, processed = excluded.processed",
        (at_ms or now_ms(), noise, received, processed),
    )


def update_learn(conn: sqlite3.Connection, *, event: str, transponder: int | None,
                 at_ms: int | None = None) -> None:
    at = at_ms or now_ms()
    conn.execute(
        "INSERT INTO decoder_status (id, updated_at_ms, learn_event, learn_transponder, learn_at_ms)"
        " VALUES (1, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET updated_at_ms = excluded.updated_at_ms,"
        " learn_event = excluded.learn_event, learn_transponder = excluded.learn_transponder,"
        " learn_at_ms = excluded.learn_at_ms",
        (at, event, transponder, at),
    )


def insert_learn_event(conn: sqlite3.Connection, *, event: str, transponder: int | None,
                       value: float | None, received_at_ms: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO learn_event (event, transponder, value, received_at_ms) VALUES (?, ?, ?, ?)",
        (event, transponder, value, received_at_ms or now_ms()),
    )
    return int(cur.lastrowid)


def learn_events_since(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    rows = conn.execute(
        "SELECT event, transponder, value, received_at_ms FROM learn_event"
        " WHERE received_at_ms >= ? ORDER BY id",
        (since_ms,),
    ).fetchall()
    return [dict(r) for r in rows]


def last_learn_event_before(conn: sqlite3.Connection, before_ms: int) -> dict | None:
    row = conn.execute(
        "SELECT event, transponder, value, received_at_ms FROM learn_event"
        " WHERE received_at_ms < ? ORDER BY id DESC LIMIT 1",
        (before_ms,),
    ).fetchone()
    return None if row is None else dict(row)


def passing_seen_since(conn: sqlite3.Connection, transponder: int, since_ms: int) -> dict | None:
    row = conn.execute(
        "SELECT transponder, rssi, received_at_ms FROM passing"
        " WHERE transponder = ? AND received_at_ms >= ? ORDER BY id LIMIT 1",
        (transponder, since_ms),
    ).fetchone()
    return None if row is None else dict(row)


# --- learning sessions and transponders --------------------------------------

def open_learn_session(conn: sqlite3.Connection, label: int, racer_name: str | None,
                       at_ms: int | None = None) -> int:
    """Start a learn session; any session still open is cancelled (one pad, one at a time)."""
    at = at_ms or now_ms()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE learn_session SET state = 'cancelled', finished_at_ms = ? WHERE state = 'open'",
            (at,),
        )
        cur = conn.execute(
            "INSERT INTO learn_session (label, racer_name, state, started_at_ms)"
            " VALUES (?, ?, 'open', ?)",
            (label, racer_name, at),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return int(cur.lastrowid)


def get_learn_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM learn_session WHERE id = ?", (session_id,)).fetchone()
    return None if row is None else dict(row)


def current_learn_session(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM learn_session WHERE state = 'open'").fetchone()
    return None if row is None else dict(row)


def close_learn_session(conn: sqlite3.Connection, session_id: int, state: str, *,
                        auto_id: int | None = None, payloads: int | None = None,
                        error: str | None = None, at_ms: int | None = None) -> bool:
    """Close an open session. False if it was already closed (double submit, other tab)."""
    cur = conn.execute(
        "UPDATE learn_session SET state = ?, finished_at_ms = ?, auto_id = ?, payloads = ?,"
        " error = ? WHERE id = ? AND state = 'open'",
        (state, at_ms or now_ms(), auto_id, payloads, error, session_id),
    )
    return cur.rowcount == 1


def upsert_transponder(conn: sqlite3.Connection, *, number: int, kind: str,
                       racer_name: str | None, rc4_sha256: str | None, source: str,
                       at_ms: int | None = None) -> None:
    conn.execute(
        "INSERT INTO transponder (number, kind, racer_name, rc4_sha256, learned_at_ms, source)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(number) DO UPDATE SET kind = excluded.kind,"
        " racer_name = COALESCE(excluded.racer_name, transponder.racer_name),"
        " rc4_sha256 = excluded.rc4_sha256, learned_at_ms = excluded.learned_at_ms,"
        " source = excluded.source",
        (number, kind, racer_name, rc4_sha256, at_ms or now_ms(), source),
    )


def delete_transponder(conn: sqlite3.Connection, number: int) -> bool:
    cur = conn.execute("DELETE FROM transponder WHERE number = ?", (number,))
    return cur.rowcount == 1


def list_transponders(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM transponder ORDER BY learned_at_ms DESC").fetchall()
    return [dict(r) for r in rows]


def recent_passings(conn: sqlite3.Connection, *, after_id: int = 0, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, transponder, kind, rssi, hits, received_at_ms FROM passing"
        " WHERE id > ? ORDER BY id DESC LIMIT ?",
        (after_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def decoder_status(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM decoder_status WHERE id = 1").fetchone()
    return None if row is None else {k: row[k] for k in row.keys() if k != "id"}


# --- downloaded events -------------------------------------------------------

def event_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["event_id"]: r["sha256"]
            for r in conn.execute("SELECT event_id, sha256 FROM event_pack")}


def _phone_tags(raw) -> str | None:
    """Pack phoneTags (hex strings) as one space-separated column."""
    if not isinstance(raw, list):
        return None
    tags = [t for t in raw if isinstance(t, str) and len(t) == 32
            and all(c in "0123456789abcdef" for c in t)]
    return " ".join(tags[:4]) or None


def store_event_pack(conn: sqlite3.Connection, pack: dict, sha256: str, raw: str,
                     at_ms: int | None = None) -> None:
    """Replace one event (row + entries) with a freshly downloaded pack."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM event_pack WHERE event_id = ?", (pack["eventId"],))
        conn.execute(
            "INSERT INTO event_pack (event_id, title, date_iso, end_date_iso, location, sha256,"
            " pack_json, downloaded_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pack["eventId"], pack.get("title") or "(untitled)", pack.get("dateISO") or "",
             pack.get("endDateISO"), pack.get("location"), sha256, raw, at_ms or now_ms()),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO event_entry (event_id, registration_id, racer_name, racer_class,"
            " status, transponder, vehicle, first_name, phone_tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(pack["eventId"], e["registrationId"], e.get("racerName") or "",
              e.get("racerClass") or "", e.get("status"), e.get("transponder"), e.get("vehicle"),
              e.get("firstName"), _phone_tags(e.get("phoneTags")))
             for e in pack.get("entries", [])],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete_events_except(conn: sqlite3.Connection, keep: set[str]) -> int:
    gone = [r["event_id"] for r in conn.execute("SELECT event_id FROM event_pack")
            if r["event_id"] not in keep]
    conn.executemany("DELETE FROM event_pack WHERE event_id = ?", [(e,) for e in gone])
    return len(gone)


def list_events(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.event_id, p.title, p.date_iso, p.end_date_iso, p.location, p.downloaded_at_ms,"
        " (SELECT COUNT(*) FROM event_entry e WHERE e.event_id = p.event_id) AS entry_count"
        " FROM event_pack p ORDER BY p.date_iso, p.title"
    ).fetchall()
    return [dict(r) for r in rows]


def get_event_pack(conn: sqlite3.Connection, event_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM event_pack WHERE event_id = ?", (event_id,)).fetchone()
    return None if row is None else dict(row)


def list_event_entries(conn: sqlite3.Connection, event_id: str | None = None) -> list[dict]:
    sql = ("SELECT e.*, p.title AS event_title, p.date_iso AS event_date FROM event_entry e"
           " JOIN event_pack p ON p.event_id = e.event_id")
    args: tuple = ()
    if event_id is not None:
        sql += " WHERE e.event_id = ?"
        args = (event_id,)
    sql += " ORDER BY p.date_iso, e.racer_class, e.racer_name"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# --- missing-transponder notices ---------------------------------------------

def queue_transponder_notices(conn: sqlite3.Connection, event_id: str, rows: list[dict],
                              at_ms: int | None = None) -> int:
    """Record a notify request for each to-learn row; returns how many."""
    at = at_ms or now_ms()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT INTO transponder_notice (event_id, registration_id, racer_name, reason,"
            " transponder, requested_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
            [(event_id, r["registration_id"], r["racer_name"], r["reason"],
              r["transponder"], at) for r in rows],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def last_transponder_notices(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """(event_id, registration_id) -> when a notice was last requested."""
    rows = conn.execute(
        "SELECT event_id, registration_id, MAX(requested_at_ms) AS at_ms"
        " FROM transponder_notice GROUP BY event_id, registration_id"
    ).fetchall()
    return {(r["event_id"], r["registration_id"]): r["at_ms"] for r in rows}


# --- heat runs ---------------------------------------------------------------

class HeatBusy(Exception):
    """Another heat is already counting down or running."""


def _run_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["entries"] = json.loads(out.pop("entries_json"))
    raw = out.pop("results_json")
    out["results"] = None if raw is None else json.loads(raw)
    return out


def start_heat_run(conn: sqlite3.Connection, *, event_id: str, heat_id: str, title: str,
                   class_name: str | None, round_type: str | None, round_number: int | None,
                   entries: list[dict], duration_s: int, min_lap_ms: int, go_at_ms: int,
                   at_ms: int | None = None) -> int:
    """Create a run in its countdown. Cancels any open learn session (the loop is
    the race's now). Raises HeatBusy if another heat is active."""
    at = at_ms or now_ms()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM heat_run WHERE state = 'started'").fetchone():
            raise HeatBusy("Another heat is already running. End it first.")
        conn.execute(
            "UPDATE learn_session SET state = 'cancelled', finished_at_ms = ? WHERE state = 'open'",
            (at,),
        )
        cur = conn.execute(
            "INSERT INTO heat_run (event_id, heat_id, title, class_name, round_type, round_number,"
            " entries_json, duration_s, min_lap_ms, state, created_at_ms, go_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?)",
            (event_id, heat_id, title, class_name, round_type, round_number,
             json.dumps(entries), duration_s, min_lap_ms, at, go_at_ms),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return int(cur.lastrowid)


def get_heat_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    return _run_row(conn.execute("SELECT * FROM heat_run WHERE id = ?", (run_id,)).fetchone())


def active_heat_run(conn: sqlite3.Connection) -> dict | None:
    return _run_row(conn.execute("SELECT * FROM heat_run WHERE state = 'started'").fetchone())


def latest_heat_run(conn: sqlite3.Connection) -> dict | None:
    """The active run, else the most recently ended finished one."""
    run = active_heat_run(conn)
    if run is not None:
        return run
    return _run_row(conn.execute(
        "SELECT * FROM heat_run WHERE state = 'finished' ORDER BY ended_at_ms DESC LIMIT 1"
    ).fetchone())


def heat_runs_for_event(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM heat_run WHERE event_id = ? ORDER BY id",
                        (event_id,)).fetchall()
    return [_run_row(r) for r in rows]  # type: ignore[misc]


def end_heat_run(conn: sqlite3.Connection, run_id: int, state: str, *,
                 results: list[dict] | None = None, at_ms: int | None = None) -> bool:
    """Close an active run as finished / cancelled / aborted. False if it was
    already closed (double click, other tab, auto-finish got there first).
    A finished run tags its passings with the run id."""
    at = at_ms or now_ms()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE heat_run SET state = ?, ended_at_ms = ?, results_json = ?"
            " WHERE id = ? AND state = 'started'",
            (state, at, None if results is None else json.dumps(results), run_id),
        )
        closed = cur.rowcount == 1
        if closed and state == "finished":
            conn.execute(
                "UPDATE passing SET heat_id = ? WHERE heat_id IS NULL AND received_at_ms"
                " BETWEEN (SELECT go_at_ms FROM heat_run WHERE id = ?) AND ?",
                (run_id, run_id, at),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return closed


def passings_between(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, transponder, decoder_ts_ms, received_at_ms, rssi FROM passing"
        " WHERE received_at_ms BETWEEN ? AND ? ORDER BY id",
        (start_ms, end_ms),
    ).fetchall()
    return [dict(r) for r in rows]
