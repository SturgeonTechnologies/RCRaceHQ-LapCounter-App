import pytest

from lapcounter import db, race

from .conftest import csrf_of
from .test_events_web import make, store
from .test_eventsync import pack

T0 = 2_000_000_000_000


@pytest.fixture
def clock(monkeypatch):
    now = {"ms": T0}
    monkeypatch.setattr(db, "now_ms", lambda: now["ms"])
    return now


def grid_pack():
    return pack(heats=[
        {"heatId": "h1", "className": "2WD Buggy", "roundType": "qualifier", "roundNumber": 1,
         "dayISO": "2026-11-07", "order": 1,
         "entries": [{"registrationId": "r3", "grid": 1}, {"registrationId": "r1", "grid": 2}]},
        {"heatId": "h2", "className": "2WD Buggy", "roundType": "qualifier", "roundNumber": 2,
         "dayISO": "2026-11-07", "order": 2, "entries": []},
    ])


@pytest.fixture
def client(tmp_path, clock):
    c = make(tmp_path)
    conn = db.open_db(c.cfg.db_path)
    store(conn, grid_pack())
    conn.close()
    c.conn = db.open_db(c.cfg.db_path)
    yield c
    c.conn.close()


def start(client, heat="h1", **form):
    data = {"csrf": csrf_of(client), "duration_min": "1", "min_lap_s": "4", **form}
    return client.post(f"/events/ev1/heats/{heat}/start", data=data)


def cross(client, transponder, at_ms):
    db.insert_passing(client.conn, transponder=transponder, kind="AMB",
                      decoder_ts_ms=at_ms - T0, rssi=-20.0, hits=10, received_at_ms=at_ms)


def test_start_copies_the_grid_and_counts_down(client, clock):
    r = start(client)
    assert r.status_code == 302 and "/race/" in r.headers["Location"]
    run = db.active_heat_run(client.conn)
    assert run["go_at_ms"] == T0 + race.COUNTDOWN_MS and run["duration_s"] == 60
    assert [(e["racerName"], e["transponder"], e["grid"]) for e in run["entries"]] == [
        ("Sam", 7340166, 1), ("Riley", 8954097, 2)]
    v = client.get(f"/api/race/{run['id']}").get_json()
    assert v["phase"] == "countdown" and v["countdownS"] == 15
    assert "Cancel start" in client.get(r.headers["Location"]).data.decode()


def test_heat_with_no_racers_picked_runs_the_class(client):
    start(client, heat="h2")
    names = [e["racerName"] for e in db.active_heat_run(client.conn)["entries"]]
    assert names == ["Kid", "Riley", "Sam"]


def test_bad_settings_and_second_heat_are_refused(client):
    r = start(client, duration_min="0")
    assert "between" in r.headers["Location"] and db.active_heat_run(client.conn) is None
    start(client)
    r = start(client, heat="h2")
    assert "already" in r.headers["Location"]


def test_cancel_only_during_countdown(client, clock):
    start(client)
    run_id = db.active_heat_run(client.conn)["id"]
    client.post(f"/race/{run_id}/cancel", data={"csrf": csrf_of(client)})
    assert db.get_heat_run(client.conn, run_id)["state"] == "cancelled"
    start(client)
    run_id = db.active_heat_run(client.conn)["id"]
    clock["ms"] = T0 + race.COUNTDOWN_MS + 1
    r = client.post(f"/race/{run_id}/cancel", data={"csrf": csrf_of(client)})
    assert "Too+late" in r.headers["Location"] or "Too%20late" in r.headers["Location"]
    assert db.get_heat_run(client.conn, run_id)["state"] == "started"


def test_full_heat_auto_finishes_with_results(client, clock):
    start(client)
    run_id = db.active_heat_run(client.conn)["id"]
    go = T0 + race.COUNTDOWN_MS
    cross(client, 7340166, go - 2000)          # sitting on the loop before go: ignored
    for t, offs in ((7340166, (1000, 20000, 39000, 56000, 61000)),
                    (8954097, (1500, 22000, 43000, 64000))):
        for o in offs:
            cross(client, t, go + o)
    clock["ms"] = go + 30_000
    v = client.get("/api/race/current").get_json()
    assert v["phase"] == "running" and v["remainingS"] == 30
    clock["ms"] = go + 65_000
    v = client.get("/api/race/current").get_json()
    assert v["phase"] == "finished"
    assert [(s["racerName"], s["laps"], s["finished"]) for s in v["standings"]] == [
        ("Sam", 4, True), ("Riley", 3, True)]
    run = db.get_heat_run(client.conn, run_id)
    assert run["state"] == "finished" and run["results"]["standings"][0]["place"] == 1
    tagged = client.conn.execute("SELECT COUNT(*) FROM passing WHERE heat_id = ?", (run_id,)).fetchone()[0]
    assert tagged == 9
    page = client.get("/events/ev1").data.decode()
    assert "finished · results" in page and "Qualifying (best lap)" in page and "Re-run" in page


def test_end_early_and_abort(client, clock):
    start(client)
    run_id = db.active_heat_run(client.conn)["id"]
    clock["ms"] = T0 + race.COUNTDOWN_MS + 5000
    client.post(f"/race/{run_id}/end", data={"csrf": csrf_of(client)})
    assert db.get_heat_run(client.conn, run_id)["state"] == "finished"
    start(client, heat="h2")
    run_id = db.active_heat_run(client.conn)["id"]
    client.post(f"/race/{run_id}/abort", data={"csrf": csrf_of(client)})
    run = db.get_heat_run(client.conn, run_id)
    assert run["state"] == "aborted" and run["results"] is None


def test_learning_is_blocked_while_a_heat_runs(client):
    start(client)
    r = client.post("/transponders/learn", data={"csrf": csrf_of(client), "label": "7340166"})
    assert "heat" in r.headers["Location"]


def test_starting_a_heat_cancels_an_open_learn(client):
    sid = db.open_learn_session(client.conn, 7340166, None)
    start(client)
    assert db.get_learn_session(client.conn, sid)["state"] == "cancelled"


def test_admin_only_controls_public_current(client):
    start(client)
    run_id = db.active_heat_run(client.conn)["id"]
    client.post("/logout", data={"csrf": csrf_of(client)})
    for path in (f"/race/{run_id}", f"/api/race/{run_id}"):
        assert client.get(path).status_code == 302
    pub = client.get("/api/race/current").get_json()
    assert pub["phase"] == "countdown" and "unknown" not in pub
    assert client.post("/events/ev1/heats/h2/start", data={}).status_code == 302


def test_a_finished_heat_lands_in_the_outbox_in_order(client, clock):
    from lapcounter import outbox, runs
    start(client)
    run_id = db.active_heat_run(client.conn)["id"]
    go = T0 + race.COUNTDOWN_MS
    for o in (1000, 20000, 39000):
        cross(client, 7340166, go + o)
    clock["ms"] = go + 45_000
    client.post(f"/race/{run_id}/end", data={"csrf": csrf_of(client)})
    recs = outbox.pending(client.conn)
    types = [r["type"] for r in recs]
    assert types[:2] == ["event.checkout", "heat.state"]
    assert types[-2:] == ["heat.state", "heat.result"]
    assert [r["payload"]["state"] for r in recs if r["type"] == "heat.state"] == ["staged", "running", "finished"]
    laps = [r["payload"] for r in recs if r["type"] == "heat.lap"]
    assert [(lap["transponder"], lap["lapNo"]) for lap in laps] == [(7340166, 0), (7340166, 1), (7340166, 2)]
    assert laps[1]["lapMs"] == 19000
    # Building the records again adds nothing (stable uuids).
    runs.record_progress(client.conn, db.get_heat_run(client.conn, run_id), clock["ms"])
    assert len(outbox.pending(client.conn)) == len(recs)
