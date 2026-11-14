"""Parse OpenStint's line protocol (openstint docs/decoder-protocol.md).

Only the three message kinds the lap counter uses are parsed:

    P <ts_ms> <kind> <transponder> <rssi> <hits> <...>   a passing
    S <ts_ms> <noise> <dc> <received> <processed> <jitter>  stats, once a second
    L <ts_ms> START <rssi> | DONE <id> <n> | INTERRUPTED | RESET   RC4 learning

`ts_ms` is the decoder's own clock (ms since it started), not wall time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Passing:
    ts_ms: int
    kind: str
    transponder: int
    rssi: float
    hits: int | None


@dataclass(frozen=True)
class Stats:
    ts_ms: int
    noise: float
    received: int
    processed: int


@dataclass(frozen=True)
class Learn:
    ts_ms: int
    event: str  # START | DONE | INTERRUPTED | RESET
    transponder: int | None = None
    value: float | None = None


Message = Passing | Stats | Learn


def parse(raw: str) -> Message | None:
    """One decoder line -> message, or None for anything malformed or unused."""
    parts = raw.split()
    if len(parts) < 3:
        return None
    try:
        ts = int(parts[1])
        tag = parts[0]
        if tag == "P" and len(parts) >= 5:
            hits = int(parts[5]) if len(parts) >= 6 else None
            return Passing(ts, parts[2], int(parts[3]), float(parts[4]), hits)
        if tag == "S" and len(parts) >= 6:
            return Stats(ts, float(parts[2]), int(parts[4]), int(parts[5]))
        if tag == "L":
            event = parts[2]
            if event == "DONE" and len(parts) >= 5:
                return Learn(ts, event, int(parts[3]), float(parts[4]))
            if event == "START" and len(parts) >= 4:
                return Learn(ts, event, value=float(parts[3]))
            if event in ("INTERRUPTED", "RESET"):
                return Learn(ts, event)
    except ValueError:
        return None
    return None
