from lapcounter import race

GO = 1_000_000


def run(**over):
    out = {"id": 1, "event_id": "ev1", "heat_id": "h1", "title": "2WD Buggy · Qualifier 1",
           "state": "started", "go_at_ms": GO, "duration_s": 60, "min_lap_ms": 4000,
           "ended_at_ms": None,
           "entries": [{"registrationId": "r1", "racerName": "Riley", "transponder": 8954097, "grid": 1},
                       {"registrationId": "r3", "racerName": "Sam", "transponder": "7340166", "grid": 2},
                       {"registrationId": "r2", "racerName": "Kid", "transponder": None, "grid": 3}]}
    out.update(over)
    return out


_ids = iter(range(1, 10_000))


def p(t, at, ts=None):
    """A passing at GO+at ms; the decoder clock runs 5 s behind the Pi's."""
    return {"id": next(_ids), "transponder": t, "received_at_ms": GO + at,
            "decoder_ts_ms": (GO + at - 5000) if ts is None else ts}


def test_phase_follows_the_clock():
    r = run()
    assert race.phase(r, GO - 1) == race.COUNTDOWN
    assert race.phase(r, GO) == race.RUNNING
    assert race.phase(r, GO + 60_000) == race.FINISHING
    assert race.phase(run(state="finished"), GO + 1) == race.FINISHED
    assert race.phase(run(state="cancelled"), GO - 1) == race.CANCELLED


def test_laps_best_last_and_order():
    passings = [p(8954097, 2000), p(7340166, 2500),
                p(8954097, 22000), p(7340166, 21000),   # Riley 20.0, Sam 18.5
                p(8954097, 41000), p(7340166, 40000),   # Riley 19.0, Sam 19.0
                p(7340166, 61000)]                      # Sam finishes 21.0 after time-up
    s = race.standings(run(), passings)
    by = {x.racer_name: x for x in s.standings}
    assert by["Sam"].laps == 3 and by["Sam"].finished
    assert by["Sam"].best_lap_ms == 18500 and by["Sam"].last_lap_ms == 21000
    assert by["Riley"].laps == 2 and not by["Riley"].finished
    assert by["Riley"].best_lap_ms == 19000 and by["Riley"].total_ms == 41000
    assert [x.racer_name for x in s.standings] == ["Sam", "Riley", "Kid"]
    assert [x.place for x in s.standings] == [1, 2, None]
    assert not by["Kid"].started and s.unknown == []


def test_same_laps_less_time_wins():
    passings = [p(8954097, 1000), p(7340166, 1000),
                p(8954097, 20000), p(7340166, 19000)]
    s = race.standings(run(), passings)
    assert [x.racer_name for x in s.standings][:2] == ["Sam", "Riley"]


def test_passings_before_go_and_within_min_lap_are_ignored():
    passings = [{"id": 0, "transponder": 8954097, "received_at_ms": GO - 500, "decoder_ts_ms": 0},
                p(8954097, 1000), p(8954097, 3000),      # double read, 2 s < 4 s min lap
                p(8954097, 21000)]
    s = race.standings(run(), passings)
    riley = s.standings[0]
    assert riley.laps == 1 and riley.lap_times == [20000]


def test_nothing_counts_after_the_finish_crossing():
    passings = [p(8954097, 1000), p(8954097, 59000), p(8954097, 65000), p(8954097, 80000)]
    riley = race.standings(run(), passings).standings[0]
    assert riley.laps == 2 and riley.finished and riley.total_ms == 65000


def test_first_crossing_after_time_up_means_did_not_race():
    s = race.standings(run(), [p(8954097, 61000)])
    assert not s.standings[0].started and s.standings[0].place is None


def test_decoder_clock_used_when_it_agrees_else_pi_clock():
    agree = [p(8954097, 1000, ts=100), p(8954097, 21000, ts=20_150)]   # 20050 vs 20000
    assert race.standings(run(), agree).standings[0].lap_times == [20050]
    restart = [p(8954097, 1000, ts=90_000), p(8954097, 21000, ts=500)]
    assert race.standings(run(), restart).standings[0].lap_times == [20000]


def test_unknown_transponders_are_listed():
    s = race.standings(run(), [p(1234567, 1000), p(1234567, 20000)])
    assert s.unknown == [{"transponder": 1234567, "crossings": 2, "lastAtMs": GO + 20000}]


def test_ended_early_stops_counting():
    passings = [p(8954097, 1000), p(8954097, 20000), p(8954097, 40000)]
    riley = race.standings(run(ended_at_ms=GO + 30000), passings).standings[0]
    assert riley.laps == 1


def test_should_finish_when_every_starter_is_in_or_after_timeout():
    r = run()
    passings = [p(8954097, 1000), p(7340166, 1000), p(8954097, 61000)]
    s = race.standings(r, passings)
    assert not race.should_finish(r, s, GO + 59_000)          # still running
    assert not race.should_finish(r, s, GO + 62_000)          # Sam still out
    assert race.should_finish(r, s, GO + 60_000 + race.FINISH_TIMEOUT_MS)
    passings.append(p(7340166, 63000))
    assert race.should_finish(r, race.standings(r, passings), GO + 63_500)


def test_view_countdown_and_remaining():
    r = run()
    s = race.standings(r, [])
    assert race.view(r, s, GO - 14_200)["countdownS"] == 15
    assert race.view(r, s, GO + 10_000)["remainingS"] == 50
