"""Running a heat: countdown, laps, time-up and finish (docs/racing.md).

Pure functions over a heat run and the raw passings recorded by lapd, so the
timing rules are unit-tested without a decoder or a clock. Nothing here writes
to the database; the web layer stores runs and calls `standings` to score them.

Rules (docs/design.md, Lap logic):
- Start begins a COUNTDOWN_MS countdown. The official start ("go") is the Pi
  clock at the end of it, never when a sound was played.
- The first crossing per transponder after go is the start-line crossing (lap 0).
- A crossing closer than `min_lap_ms` to the transponder's previous counted one
  is ignored (a car sitting on the loop, or a double read).
- At go + duration the heat is time-up; each car's next crossing is its finish
  and nothing after it counts.
- The heat finishes once every car that started has finished, FINISH_TIMEOUT_MS
  after time-up at the latest, or when the race director ends it.
- Standings: most laps, then least total time to the last counted crossing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COUNTDOWN_MS = 15_000
DEFAULT_DURATION_S = 300
DEFAULT_MIN_LAP_MS = 4_000
FINISH_TIMEOUT_MS = 60_000
# Lap times come from the decoder's own clock when it agrees with the Pi clock to
# within this much; ZeroMQ delivery jitter then can't skew a lap.
CLOCK_AGREE_MS = 2_000

COUNTDOWN, RUNNING, FINISHING, FINISHED, CANCELLED, ABORTED = (
    "countdown", "running", "finishing", "finished", "cancelled", "aborted")


@dataclass
class Standing:
    registration_id: str | None
    racer_name: str
    transponder: int | None
    grid: int
    laps: int = 0
    total_ms: int | None = None
    best_lap_ms: int | None = None
    best_lap_at_ms: int | None = None
    last_lap_ms: int | None = None
    lap_times: list[int] = field(default_factory=list)
    started: bool = False
    finished: bool = False
    place: int | None = None

    def as_dict(self) -> dict:
        return {
            "registrationId": self.registration_id, "racerName": self.racer_name,
            "transponder": self.transponder, "grid": self.grid, "place": self.place,
            "laps": self.laps, "totalMs": self.total_ms, "bestLapMs": self.best_lap_ms,
            "bestLapAtMs": self.best_lap_at_ms, "lastLapMs": self.last_lap_ms,
            "lapTimes": self.lap_times, "started": self.started, "finished": self.finished,
        }


@dataclass
class Scored:
    standings: list[Standing]
    unknown: list[dict]  # transponders seen in the heat that aren't on the grid


def as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def time_up_ms(run: dict) -> int:
    return run["go_at_ms"] + run["duration_s"] * 1000


def phase(run: dict, now_ms: int) -> str:
    """Where a heat is. Stored states win; a 'started' run is placed by the clock."""
    if run["state"] != "started":
        return run["state"]
    if now_ms < run["go_at_ms"]:
        return COUNTDOWN
    if now_ms < time_up_ms(run):
        return RUNNING
    return FINISHING


def _lap_ms(prev: dict, cur: dict) -> int:
    received = cur["received_at_ms"] - prev["received_at_ms"]
    decoded = cur["decoder_ts_ms"] - prev["decoder_ts_ms"]
    # A decoder restart resets its clock; fall back to the Pi clock then.
    if decoded > 0 and abs(decoded - received) <= CLOCK_AGREE_MS:
        return decoded
    return received


def counted_crossings(run: dict, passings: list[dict]) -> dict[int, list[dict]]:
    """Per transponder, the crossings that count: after go, debounced by min lap,
    up to and including the first one at or after time-up."""
    go, up = run["go_at_ms"], time_up_ms(run)
    end = run.get("ended_at_ms")
    out: dict[int, list[dict]] = {}
    done: set[int] = set()
    for p in sorted(passings, key=lambda p: (p["received_at_ms"], p.get("id", 0))):
        at = p["received_at_ms"]
        if at < go or (end is not None and at > end):
            continue
        t = p["transponder"]
        if t in done:
            continue
        seen = out.setdefault(t, [])
        if seen and at - seen[-1]["received_at_ms"] < run["min_lap_ms"]:
            continue
        seen.append(p)
        if at >= up and len(seen) > 1:
            done.add(t)  # crossed after time-up: that was the finish
        elif at >= up:
            # First crossing is already after time-up: the car never raced.
            out[t] = [p]
            done.add(t)
    return out


def standings(run: dict, passings: list[dict]) -> Scored:
    go, up = run["go_at_ms"], time_up_ms(run)
    crossings = counted_crossings(run, passings)
    rows: list[Standing] = []
    on_grid: set[int] = set()
    for e in sorted(run["entries"], key=lambda e: e.get("grid") or 0):
        t = as_int(e.get("transponder"))
        s = Standing(e.get("registrationId"), e.get("racerName") or "", t, e.get("grid") or 0)
        rows.append(s)
        if t is None:
            continue
        on_grid.add(t)
        seen = crossings.get(t, [])
        if not seen or (len(seen) == 1 and seen[0]["received_at_ms"] >= up):
            continue
        s.started = True
        for prev, cur in zip(seen, seen[1:]):
            lap = _lap_ms(prev, cur)
            s.lap_times.append(lap)
            if s.best_lap_ms is None or lap < s.best_lap_ms:
                s.best_lap_ms, s.best_lap_at_ms = lap, cur["received_at_ms"]
        s.laps = len(s.lap_times)
        s.last_lap_ms = s.lap_times[-1] if s.lap_times else None
        s.total_ms = seen[-1]["received_at_ms"] - go
        s.finished = seen[-1]["received_at_ms"] >= up and s.laps > 0

    def key(s: Standing):
        if not s.started:
            return (1, 0, 0, s.grid)
        return (0, -s.laps, s.total_ms or 0, s.grid)
    rows.sort(key=key)
    for place, s in enumerate(rows, start=1):
        s.place = place if s.started else None

    unknown = [{"transponder": t, "crossings": len(c), "lastAtMs": c[-1]["received_at_ms"]}
               for t, c in sorted(crossings.items()) if t not in on_grid]
    return Scored(rows, unknown)


def grid_crossings(run: dict, passings: list[dict]) -> list[dict]:
    """Every counted crossing of a car on the grid, in time order, numbered the
    way `standings` counts them: lap 0 is the start-line crossing and each
    later one carries the lap it completed. These are what the outbox sends
    to RC Race HQ as heat.lap records."""
    up = time_up_ms(run)
    by_t = {as_int(e.get("transponder")): e for e in run["entries"]}
    out = []
    for t, seen in counted_crossings(run, passings).items():
        e = by_t.get(t)
        if e is None or t is None or (len(seen) == 1 and seen[0]["received_at_ms"] >= up):
            continue
        for n, cur in enumerate(seen):
            out.append({
                "transponder": t, "registrationId": e.get("registrationId"),
                "racerName": e.get("racerName") or "", "lapNo": n,
                "lapMs": _lap_ms(seen[n - 1], cur) if n else None,
                "tsMs": cur["received_at_ms"],
            })
    return sorted(out, key=lambda c: (c["tsMs"], c["transponder"]))


def should_finish(run: dict, scored: Scored, now_ms: int) -> bool:
    """True once a time-up heat has nothing left to wait for."""
    if phase(run, now_ms) != FINISHING:
        return False
    if now_ms >= time_up_ms(run) + FINISH_TIMEOUT_MS:
        return True
    racing = [s for s in scored.standings if s.started]
    return all(s.finished for s in racing)


def view(run: dict, scored: Scored, now_ms: int) -> dict:
    """What the live page and the race director page render."""
    p = phase(run, now_ms)
    out = {
        "id": run["id"], "eventId": run["event_id"], "heatId": run["heat_id"],
        "title": run["title"], "phase": p, "nowMs": now_ms, "goAtMs": run["go_at_ms"],
        "timeUpAtMs": time_up_ms(run), "durationS": run["duration_s"],
        "minLapMs": run["min_lap_ms"], "endedAtMs": run.get("ended_at_ms"),
        "standings": [s.as_dict() for s in scored.standings],
        "unknown": scored.unknown,
    }
    if p == COUNTDOWN:
        out["countdownS"] = max(0, -(-(run["go_at_ms"] - now_ms) // 1000))
    elif p in (RUNNING, FINISHING):
        out["remainingS"] = max(0, (time_up_ms(run) - now_ms) // 1000)
    return out
