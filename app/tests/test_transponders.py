import pytest

from lapcounter import db, lapd, learning
from lapcounter.config import Config
from lapcounter.web import create_app
from lapcounter.web.auth import reset_throttle

from .conftest import csrf_of, do_setup


@pytest.fixture
def rc4(tmp_path):
    folder = tmp_path / "openstint"
    folder.mkdir()
    return folder


@pytest.fixture
def admin(tmp_path, rc4):
    reset_throttle()
    cfg = Config(db_path=str(tmp_path / "lc.db"), decoder_url="tcp://127.0.0.1:1",
                 rc4_dir=str(rc4))
    client = create_app(cfg, testing=True).test_client()
    do_setup(client)
    client.conn = db.connect(cfg.db_path)
    return client


def start(client, label="8954097", name="Paul Smith"):
    return client.post("/transponders/learn",
                       data={"csrf": csrf_of(client), "label": label, "racer_name": name})


def feed(client, *lines):
    for line in lines:
        lapd.handle(client.conn, line)


def post(client, path):
    return client.post(path, data={"csrf": csrf_of(client)})


def test_pages_need_admin(tmp_path):
    reset_throttle()
    client = create_app(Config(db_path=str(tmp_path / "x.db")), testing=True).test_client()
    do_setup(client)
    post(client, "/logout")
    for path in ("/transponders", "/transponders/learn/1", "/api/learn/1"):
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_full_rc4_learn_and_record(admin, rc4):
    r = start(admin)
    assert r.status_code == 302
    learn_url = r.headers["Location"]
    sid = int(learn_url.rsplit("/", 1)[1])
    assert b"Learn 8954097" in admin.get(learn_url).data

    s = admin.get(f"/api/learn/{sid}").get_json()
    assert (s["state"], s["can_record"]) == ("waiting", False)
    assert admin.get(f"/api/learn/{sid}").headers["Cache-Control"] == "no-store"

    feed(admin, "S 1 -47.0 0.99 755 615 0.04", "L 2 START -30.5")
    assert admin.get(f"/api/learn/{sid}").get_json()["state"] == "learning"

    (rc4 / "1000.rc4").write_bytes(b"rc4-code")
    feed(admin, "L 3 DONE 1000 40")
    s = admin.get(f"/api/learn/{sid}").get_json()
    assert (s["state"], s["auto_id"], s["can_record"]) == ("ready", 1000, True)
    assert s["decoder"]["received"] == 755

    r = post(admin, f"/transponders/learn/{sid}/record")
    assert r.status_code == 302 and r.headers["Location"] == learn_url
    assert (rc4 / "8954097.rc4").read_bytes() == b"rc4-code"
    assert not (rc4 / "1000.rc4").exists()
    (t,) = db.list_transponders(admin.conn)
    assert (t["number"], t["kind"], t["racer_name"]) == (8954097, "rc4", "Paul Smith")
    assert len(t["rc4_sha256"]) == 64
    assert admin.get(f"/api/learn/{sid}").get_json()["result"] == "recorded"
    assert b"Recorded" in admin.get(learn_url).data

    page = admin.get("/transponders").data
    assert b"8954097" in page and b"Paul Smith" in page

    # second Record (double click / other tab) changes nothing
    assert post(admin, f"/transponders/learn/{sid}/record").status_code == 302
    assert len(db.list_transponders(admin.conn)) == 1


def test_rc3_records_without_a_file(admin, rc4):
    sid = int(start(admin, "4321234", "").headers["Location"].rsplit("/", 1)[1])
    feed(admin, "P 5 AMB 4321234 -31.0 12")
    assert admin.get(f"/api/learn/{sid}").get_json()["state"] == "known"
    post(admin, f"/transponders/learn/{sid}/record")
    (t,) = db.list_transponders(admin.conn)
    assert (t["kind"], t["rc4_sha256"], t["racer_name"]) == ("direct", None, None)


def test_record_before_done_is_refused(admin):
    sid = int(start(admin).headers["Location"].rsplit("/", 1)[1])
    r = post(admin, f"/transponders/learn/{sid}/record")
    assert "error=" in r.headers["Location"]
    assert db.list_transponders(admin.conn) == []


def test_record_shows_file_errors(admin, rc4, monkeypatch):
    sid = int(start(admin).headers["Location"].rsplit("/", 1)[1])
    feed(admin, "L 3 DONE 1000 40")  # but no 1000.rc4 in the folder
    real = learning.rename_learned
    monkeypatch.setattr(learning, "rename_learned",
                        lambda d, a, lbl: real(d, a, lbl, wait_s=0.1))
    r = post(admin, f"/transponders/learn/{sid}/record")
    assert "error=" in r.headers["Location"]
    assert db.get_learn_session(admin.conn, sid)["state"] == "open"  # can retry


def test_start_validation_and_already_learned(admin, rc4):
    r = start(admin, "1000")
    assert r.headers["Location"].startswith("/transponders?error=")
    (rc4 / "8954097.rc4").write_text("x")
    r = start(admin)
    assert "already+learned" in r.headers["Location"]


def test_new_session_cancels_the_open_one_and_cancel(admin):
    first = int(start(admin).headers["Location"].rsplit("/", 1)[1])
    second = int(start(admin, "7654321").headers["Location"].rsplit("/", 1)[1])
    assert db.get_learn_session(admin.conn, first)["state"] == "cancelled"
    assert b"7654321 is still open" in admin.get("/transponders").data
    r = post(admin, f"/transponders/learn/{second}/cancel")
    assert r.headers["Location"] == "/transponders"
    assert db.current_learn_session(admin.conn) is None


def test_done_before_the_session_asks_to_lift_until_reset(admin, rc4):
    feed(admin, "L 1 DONE 1000 40")
    sid = int(start(admin).headers["Location"].rsplit("/", 1)[1])
    assert admin.get(f"/api/learn/{sid}").get_json()["state"] == "lift"
    feed(admin, "L 3 RESET")
    assert admin.get(f"/api/learn/{sid}").get_json()["state"] == "waiting"


def test_csrf_required(admin):
    r = admin.post("/transponders/learn", data={"label": "8954097"})
    assert r.status_code == 400


def test_list_flags_strays_and_unlisted_files(admin, rc4):
    (rc4 / "1001.rc4").write_text("x")
    (rc4 / "5550001.rc4").write_text("x")
    page = admin.get("/transponders").data
    assert b"1001.rc4" in page and b"5550001" in page and b"file only" in page


def test_unknown_session_404(admin):
    assert admin.get("/transponders/learn/999").status_code == 404
    assert admin.get("/api/learn/999").status_code == 404


def test_delete_recorded_rc4_then_learn_again(admin, rc4):
    (rc4 / "8954097.rc4").write_bytes(b"rc4-code")
    db.upsert_transponder(admin.conn, number=8954097, kind="rc4", racer_name="Paul",
                          rc4_sha256="x" * 64, source="learned")
    admin.conn.commit()
    assert b"already learned" in admin.get(start(admin).headers["Location"]).data

    r = post(admin, "/transponders/8954097/delete")
    assert r.status_code == 302 and "deleted=8954097" in r.headers["Location"]
    assert not (rc4 / "8954097.rc4").exists()
    assert db.list_transponders(admin.conn) == []
    assert b"Deleted 8954097" in admin.get(r.headers["Location"]).data
    assert "/transponders/learn/" in start(admin).headers["Location"]


def test_delete_direct_transponder_has_no_file(admin, rc4):
    db.upsert_transponder(admin.conn, number=4321000, kind="direct", racer_name=None,
                          rc4_sha256=None, source="learned")
    admin.conn.commit()
    assert "deleted=4321000" in post(admin, "/transponders/4321000/delete").headers["Location"]
    assert db.list_transponders(admin.conn) == []


def test_delete_stray_auto_file(admin, rc4):
    (rc4 / "1000.rc4").write_bytes(b"x")
    page = admin.get("/transponders").data
    assert b"1000.rc4" in page and b'action="/transponders/1000/delete"' in page
    post(admin, "/transponders/1000/delete")
    assert not (rc4 / "1000.rc4").exists()
    assert b"nobody recorded" not in admin.get("/transponders").data


def test_delete_unknown_number(admin):
    r = post(admin, "/transponders/5550001/delete")
    assert "error=" in r.headers["Location"] and "known" in r.headers["Location"]


def test_delete_needs_csrf_and_login(admin, rc4):
    (rc4 / "8954097.rc4").write_bytes(b"x")
    assert admin.post("/transponders/8954097/delete", data={}).status_code in (400, 403)
    assert (rc4 / "8954097.rc4").exists()
    post(admin, "/logout")
    r = admin.post("/transponders/8954097/delete", data={})
    assert r.status_code in (302, 400, 403)
    assert (rc4 / "8954097.rc4").exists()


def test_delete_without_write_permission(admin, rc4, monkeypatch):
    (rc4 / "8954097.rc4").write_bytes(b"x")

    def deny(self):
        raise PermissionError
    monkeypatch.setattr(type(rc4), "unlink", deny)
    r = post(admin, "/transponders/8954097/delete")
    assert "No+permission" in r.headers["Location"]


def test_list_shows_the_library_upload_and_a_locked_refusal(admin, rc4):
    from lapcounter import outbox
    (rc4 / "7340166.rc4").write_bytes(b"x")
    (rc4 / "8954097.rc4").write_bytes(b"y")
    outbox.add(admin.conn, "transponder.learned", "library", {"transponder": 8954097}, "rc4:a")
    outbox.add(admin.conn, "transponder.learned", "library", {"transponder": 7340166}, "rc4:b")
    outbox.acknowledge(admin.conn, 2, [{"seq": 2, "reason": "Transponder 7340166 is already registered in RC Race HQ and locked"}])
    page = admin.get("/transponders").data.decode()
    assert "In library" in page
    assert "Refused" in page and "already registered" in page
