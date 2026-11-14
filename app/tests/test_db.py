import threading

from lapcounter import db


def test_migrate_is_idempotent(tmp_path):
    path = str(tmp_path / "x.db")
    conn = db.open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert db.migrate(conn) == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_only_one_admin(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    assert not db.admin_exists(conn)
    assert db.create_admin(conn, "a", "h1")
    assert not db.create_admin(conn, "b", "h2")
    assert db.get_admin(conn)["username"] == "a"


def test_concurrent_setup_creates_one_admin(tmp_path):
    path = str(tmp_path / "x.db")
    db.open_db(path).close()
    wins = []

    def attempt(name):
        conn = db.connect(path)
        if db.create_admin(conn, name, "h"):
            wins.append(name)
        conn.close()

    threads = [threading.Thread(target=attempt, args=(f"u{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_setdefault_setting_keeps_first_value(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    assert db.setdefault_setting(conn, "k", "one") == "one"
    assert db.setdefault_setting(conn, "k", "two") == "one"


def test_stats_and_learn_share_one_row(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    db.update_stats(conn, noise=-47.0, received=700, processed=600, at_ms=1000)
    db.update_learn(conn, event="DONE", transponder=1000, at_ms=2000)
    s = db.decoder_status(conn)
    assert (s["noise"], s["received"], s["learn_event"], s["learn_transponder"]) == (-47.0, 700, "DONE", 1000)
    assert s["updated_at_ms"] == 2000


def test_recent_passings_order_and_cursor(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    ids = [db.insert_passing(conn, transponder=t, kind="AMB", decoder_ts_ms=t, rssi=-30, hits=1)
           for t in (1, 2, 3)]
    assert [p["transponder"] for p in db.recent_passings(conn)] == [1, 2, 3]
    assert [p["transponder"] for p in db.recent_passings(conn, after_id=ids[0])] == [2, 3]
    assert [p["transponder"] for p in db.recent_passings(conn, limit=2)] == [2, 3]


def test_last_learn_event_before(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    assert db.last_learn_event_before(conn, 5_000) is None
    db.insert_learn_event(conn, event="START", transponder=None, value=-10.0, received_at_ms=1_000)
    db.insert_learn_event(conn, event="DONE", transponder=1000, value=40.0, received_at_ms=2_000)
    db.insert_learn_event(conn, event="RESET", transponder=None, value=None, received_at_ms=6_000)
    assert db.last_learn_event_before(conn, 5_000)["event"] == "DONE"
    assert db.last_learn_event_before(conn, 7_000)["event"] == "RESET"
