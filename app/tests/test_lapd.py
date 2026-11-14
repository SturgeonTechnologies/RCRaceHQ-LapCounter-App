import sqlite3

from lapcounter import db, lapd


def test_handle_stores_each_kind(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    assert lapd.handle(conn, "P 109613 AMB 8954097 -30.86 4096 0 10.7", now=lambda: 5000) == "P"
    assert lapd.handle(conn, "S 1 -47.12 0.99 755 615 0.04", now=lambda: 6000) == "S"
    assert lapd.handle(conn, "L 2 DONE 1000 40", now=lambda: 7000) == "L"
    assert lapd.handle(conn, "F RC4 TS:1 noise", now=lambda: 8000) is None

    (p,) = db.recent_passings(conn)
    assert (p["transponder"], p["received_at_ms"], p["hits"]) == (8954097, 5000, 4096)
    s = db.decoder_status(conn)
    assert (s["received"], s["learn_event"], s["learn_transponder"]) == (755, "DONE", 1000)
    (e,) = db.learn_events_since(conn, 0)
    assert (e["event"], e["transponder"], e["value"], e["received_at_ms"]) == ("DONE", 1000, 40.0, 7000)


def test_run_survives_database_errors(tmp_path, monkeypatch):
    conn = db.open_db(str(tmp_path / "x.db"))
    calls = {"n": 0}
    real = db.insert_passing

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(db, "insert_passing", flaky)
    stored = lapd.run(conn, ["P 1 AMB 1 -30 2", "P 2 AMB 2 -30 2", "garbage"])
    assert stored == 1
    assert [p["transponder"] for p in db.recent_passings(conn)] == [2]


def test_subscribe_reads_a_real_zmq_feed(tmp_path):
    import threading
    import time

    import zmq

    pub = zmq.Context.instance().socket(zmq.PUB)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    conn = db.open_db(str(tmp_path / "x.db"))
    lines = lapd.subscribe(f"tcp://127.0.0.1:{port}")
    got = []
    t = threading.Thread(target=lambda: got.append(next(lines)), daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while t.is_alive() and time.monotonic() < deadline:  # PUB drops until SUB has joined
        pub.send_string("P 1 AMB 8954097 -30.0 3")
        time.sleep(0.05)
    pub.close()
    assert got == ["P 1 AMB 8954097 -30.0 3"]
    assert lapd.handle(conn, got[0]) == "P"
