"""syncd: keeps race day moving and sends it to RC Race HQ (docs/club-sync.md).

Once a second it:
  * ticks the heat on track (runs.tick), so a heat finishes on time and its
    laps reach the outbox even with nobody watching the race page;
  * if this Pi is linked, queues any newly learned transponder for the shared
    library (`queue_learned`) and moves the outbox along (`step`).

Outbox delivery is one batch at a time: publish up to outbox.BATCH records to
`<topicPrefix>/outbox`, then poll `packs/<thing>/outbox-ack.json` until it
carries our outboxId and a seq at or past the batch's last record. Anything
not confirmed is sent again after RESEND_S; RC Race HQ skips what it already
applied, so resending is harmless.

Every network call is injectable (`Cloud`) so tests never touch AWS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import signal
import sqlite3
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import cloud, db, eventsync, learning, outbox, runs
from .cloud import CloudError
from .config import Config

LOG = logging.getLogger("lapcounter.syncd")

INFLIGHT_KEY = "outbox.inflight"
TICK_S = 1.0
RESEND_S = 30.0
LIBRARY_EVENT_ID = "library"  # RC Race HQ lib/transponderLibrary.ts
MAX_RC4_BYTES = 64 * 1024


@dataclass
class Cloud:
    """What syncd needs from AWS, bound to one link."""
    publish: Callable[[bytes], None]
    get_ack: Callable[[], bytes | None]
    put: Callable[[str, bytes], None]
    thing: str


def connect_cloud(link: dict, paths: cloud.Paths) -> Cloud:
    s3 = eventsync.S3(link["bucket"], link["region"], lambda: cloud.fetch_credentials(link, paths))
    thing = link["thingName"]

    def publish(payload: bytes) -> None:
        if not (paths.key.exists() and paths.cert.exists()):
            raise CloudError("The key or certificate is missing on this Pi. Link it again.")
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=str(paths.cert), keyfile=str(paths.key))
        eventsync.mqtt_publish(link["iotEndpoint"], thing, f"{link['topicPrefix']}/outbox", payload, context)

    # Before the first ack the key doesn't exist; S3 says 403 or 404 (see sync_events).
    return Cloud(publish=publish, get_ack=lambda: s3.get(f"packs/{thing}/outbox-ack.json", absent=(403, 404)),
                 put=s3.put, thing=thing)


# --- transponder library -----------------------------------------------------

def upload_key(thing: str, number: int, sha256: str) -> str:
    return f"uploads/{thing}/rc4/{number}-{sha256}.rc4"


def queue_learned(conn: sqlite3.Connection, rc4_dir: str | Path, c: Cloud) -> int:
    """Upload each learned .rc4 not sent yet (by number + content) and queue a
    transponder.learned record for it. Returns how many were queued."""
    queued = 0
    for number in sorted(learning.learned_numbers(rc4_dir)):
        path = learning.rc4_path(rc4_dir, number)
        try:
            body = path.read_bytes()
        except OSError:
            continue  # renamed or deleted under us; next pass
        if not body or len(body) > MAX_RC4_BYTES:
            continue
        sha = hashlib.sha256(body).hexdigest()
        uuid = f"rc4:{number}:{sha}"
        if conn.execute("SELECT 1 FROM outbox WHERE uuid = ?", (uuid,)).fetchone():
            continue
        c.put(upload_key(c.thing, number, sha), body)
        queued += outbox.add(conn, "transponder.learned", LIBRARY_EVENT_ID,
                             {"transponder": number, "sha256": sha, "size": len(body)}, uuid)
    return queued


def library_status(conn: sqlite3.Connection) -> dict[int, dict]:
    """The latest upload per transponder: {number: {state, reason}} where state
    is waiting / added / refused. For the Transponders page."""
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT payload_json, acked_at_ms, rejected FROM outbox"
                          " WHERE type = 'transponder.learned' ORDER BY seq"):
        n = json.loads(r["payload_json"]).get("transponder")
        if r["rejected"]:
            out[n] = {"state": "refused", "reason": r["rejected"]}
        elif r["acked_at_ms"]:
            out[n] = {"state": "added", "reason": None}
        else:
            out[n] = {"state": "waiting", "reason": None}
    return out


# --- outbox delivery -----------------------------------------------------------

def _inflight(conn) -> dict | None:
    raw = db.get_setting(conn, INFLIGHT_KEY)
    return json.loads(raw) if raw else None


def step(conn: sqlite3.Connection, c: Cloud, now: float) -> None:
    """One delivery step: take in an ack if there is one, else (re)send."""
    oid = outbox.outbox_id(conn)
    flight = _inflight(conn)
    if flight:
        raw = c.get_ack()
        ack = json.loads(raw) if raw else None
        if isinstance(ack, dict) and ack.get("outboxId") == oid and isinstance(ack.get("seq"), int):
            if ack["seq"] > 0:
                outbox.acknowledge(conn, ack["seq"], ack.get("rejected") or [])
            if ack["seq"] >= flight["upto"]:
                db.delete_setting(conn, INFLIGHT_KEY)
                outbox.set_status(conn, at_ms=db.now_ms(), ok=not ack.get("error"),
                                  error=ack.get("error"), sent=flight["count"])
                flight = None
            elif ack.get("error"):
                outbox.set_status(conn, at_ms=db.now_ms(), ok=False, error=ack["error"], sent=0)
        elif isinstance(ack, dict) and ack.get("error") and ack.get("outboxId") in (None, oid):
            outbox.set_status(conn, at_ms=db.now_ms(), ok=False, error=ack["error"], sent=0)
        if flight and now - flight["sentAt"] < RESEND_S:
            return

    batch = outbox.pending(conn)
    if not batch:
        return
    payload = json.dumps({"schema": 1, "outboxId": oid, "records": batch},
                         separators=(",", ":")).encode()
    c.publish(payload)
    db.set_setting(conn, INFLIGHT_KEY, json.dumps(
        {"upto": batch[-1]["seq"], "count": len(batch), "sentAt": now}))


# --- loop ----------------------------------------------------------------------

def run_once(conn: sqlite3.Connection, cfg: Config, now: float,
             cloud_for: Callable[[dict], Cloud]) -> None:
    runs.tick(conn, int(now * 1000))
    link = cloud.current_link(conn)
    if not link:
        return
    c = cloud_for(link)
    try:
        queue_learned(conn, cfg.rc4_dir, c)
    except CloudError as exc:
        LOG.warning("transponder upload: %s", exc)
    try:
        step(conn, c, now)
    except CloudError as exc:
        outbox.set_status(conn, at_ms=db.now_ms(), ok=False, error=str(exc), sent=0)
        LOG.warning("outbox: %s", exc)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    conn = db.open_db(cfg.db_path)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    paths = cloud.Paths(Path(cfg.cloud_dir))
    cache: dict[str, Cloud] = {}

    def cloud_for(link: dict) -> Cloud:
        key = json.dumps(link, sort_keys=True)
        if key not in cache:
            cache.clear()
            cache[key] = connect_cloud(link, paths)
        return cache[key]

    LOG.info("syncd started")
    while True:
        try:
            run_once(conn, cfg, time.time(), cloud_for)
        except Exception:  # keep race day going; the next tick tries again
            LOG.exception("syncd tick failed")
        time.sleep(TICK_S)


if __name__ == "__main__":
    sys.exit(main())
