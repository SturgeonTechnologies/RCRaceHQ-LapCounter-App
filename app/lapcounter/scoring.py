"""Qualifying order for a class (docs/club-sync.md, Scoring).

Today's rule, `best_lap`: rank by the best complete lap across all of the class's
finished qualifier heats, fastest first. A tie goes to whoever set the lap first.
Racers with no complete lap go last, by name. Kept as one function so another
rule (laps then time, points) can be added without touching the rest.
"""

from __future__ import annotations


def _who(s: dict) -> str:
    if s.get("registrationId"):
        return f"r:{s['registrationId']}"
    return f"t:{s.get('transponder')}:{s.get('racerName', '')}"


def rank_qualifying(results: list[list[dict]]) -> list[dict]:
    """`results` is one standings list (race.Standing.as_dict) per finished qualifier.

    Returns [{place, registrationId, racerName, transponder, bestLapMs, heats}]."""
    best: dict[str, dict] = {}
    for heat in results:
        for s in heat:
            row = best.setdefault(_who(s), {
                "registrationId": s.get("registrationId"), "racerName": s.get("racerName", ""),
                "transponder": s.get("transponder"), "bestLapMs": None, "bestLapAtMs": None,
                "heats": 0,
            })
            row["heats"] += 1
            lap, at = s.get("bestLapMs"), s.get("bestLapAtMs")
            if lap is None:
                continue
            if (row["bestLapMs"] is None or lap < row["bestLapMs"]
                    or (lap == row["bestLapMs"] and (at or 0) < (row["bestLapAtMs"] or 0))):
                row["bestLapMs"], row["bestLapAtMs"] = lap, at

    def key(r: dict):
        if r["bestLapMs"] is None:
            return (1, 0, 0, r["racerName"].lower())
        return (0, r["bestLapMs"], r["bestLapAtMs"] or 0, r["racerName"].lower())
    ranked = sorted(best.values(), key=key)
    for place, r in enumerate(ranked, start=1):
        r["place"] = place
    return ranked
