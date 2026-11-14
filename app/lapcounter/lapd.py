"""lapd: subscribe to OpenStint's ZeroMQ feed and record what it hears.

Every passing is stored raw, decoder stats are kept current for the live page,
and every learning line is logged for the Learn page. Assigning passings to
heats and computing laps lands with the heat model.

Runs as its own process so a web crash never loses a passing.

    python -m lapcounter.lapd
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
from collections.abc import Callable, Iterable

from . import db, decoder
from .config import Config

LOG = logging.getLogger("lapd")


def handle(conn: sqlite3.Connection, raw: str, now: Callable[[], int] = db.now_ms) -> str | None:
    """Store one decoder line. Returns the message tag it stored, or None if ignored."""
    msg = decoder.parse(raw)
    at = now()
    if isinstance(msg, decoder.Passing):
        db.insert_passing(conn, transponder=msg.transponder, kind=msg.kind,
                          decoder_ts_ms=msg.ts_ms, rssi=msg.rssi, hits=msg.hits,
                          received_at_ms=at)
        LOG.info("passing %s %s rssi=%.1f", msg.kind, msg.transponder, msg.rssi)
        return "P"
    if isinstance(msg, decoder.Stats):
        db.update_stats(conn, noise=msg.noise, received=msg.received,
                        processed=msg.processed, at_ms=at)
        return "S"
    if isinstance(msg, decoder.Learn):
        db.update_learn(conn, event=msg.event, transponder=msg.transponder, at_ms=at)
        db.insert_learn_event(conn, event=msg.event, transponder=msg.transponder,
                              value=msg.value, received_at_ms=at)
        LOG.info("learn %s %s", msg.event, msg.transponder or "")
        return "L"
    return None


def run(conn: sqlite3.Connection, lines: Iterable[str]) -> int:
    """Feed lines into the database until the iterable ends. Returns lines stored."""
    stored = 0
    for raw in lines:
        try:
            if handle(conn, raw):
                stored += 1
        except sqlite3.Error:
            # A locked or full database must not kill the subscriber; log and carry on.
            LOG.exception("could not store %r", raw[:80])
    return stored


def subscribe(url: str) -> Iterable[str]:
    import zmq  # late import keeps the unit tests free of a live socket

    sock = zmq.Context.instance().socket(zmq.SUB)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVHWM, 10000)
    sock.setsockopt_string(zmq.SUBSCRIBE, "")
    # connect() never fails if the decoder is down; ZeroMQ reconnects on its own.
    sock.connect(url)
    LOG.info("subscribed to %s", url)
    while True:
        yield sock.recv().decode("utf-8", errors="replace")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    conn = db.open_db(cfg.db_path)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run(conn, subscribe(cfg.decoder_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
