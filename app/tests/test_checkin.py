import hashlib
import json

import pytest

from lapcounter import checkin, db, outbox
from lapcounter.config import Config
from lapcounter.web import create_app
from lapcounter.web.auth import reset_throttle
from lapcounter.web.checkin import reset_checkin_throttle

from .conftest import csrf_of, do_setup
from .test_eventsync import pack

PHONE = "+15095551234"
PARENT = "+15095550000"


def tagged_pack(event_id="ev1"):
    p = pack(event_id)
    p["entries"][0].update(firstName="Riley", phoneTags=[checkin.phone_tag(event_id, PHONE)])
    # r2 is a kid registered by a parent: the parent's phone checks them in.
    p["entries"][1].update(firstName="Kid", phoneTags=[checkin.phone_tag(event_id, PARENT)])
    p["entries"][2].update(firstName="Sam", phoneTags=[checkin.phone_tag(event_id, PARENT)])
    return p


def load(conn, p):
    raw = json.dumps(p)
    db.store_event_pack(conn, p, hashlib.sha256(raw.encode()).hexdigest(), raw)


@pytest.fixture
def site(tmp_path):
    reset_throttle()
    reset_checkin_throttle()
    cfg = Config(db_path=str(tmp_path / "lc.db"), decoder_url="tcp://127.0.0.1:1",
                 rc4_dir=str(tmp_path / "rc4"), cloud_dir=str(tmp_path / "cloud"))
    app = create_app(cfg, testing=True)
    conn = db.open_db(cfg.db_path)
    load(conn, tagged_pack())
    admin = app.test_client()
    do_setup(admin)
    racer = app.test_client()
    yield conn, admin, racer
    conn.close()


def post_phone(client, phone, **extra):
    client.get("/checkin")
    return client.post("/checkin", data={"csrf": csrf_of(client), "phone": phone, **extra})


# --- rules -------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("509-555-1234", PHONE), ("(509) 555 1234", PHONE), ("1 509 555 1234", PHONE),
    ("+1 509 555 1234", PHONE), ("+44 20 7946 0958", "+442079460958"),
    ("555-1234", None), ("109 555 1234", None), ("call me", None), ("", None),
])
def test_normalize_phone_matches_rc_race_hq(raw, want):
    assert checkin.normalize_phone(raw) == want


def test_phone_tag_matches_the_backend_vector():
    # backend/test/lapCounterPacks.test.ts uses the same input.
    expect = hashlib.sha256(b"rchq-checkin:evt-123:+15095551234").hexdigest()[:32]
    assert checkin.phone_tag("evt-123", PHONE) == expect
    assert checkin.phone_tag("evt-124", PHONE) != expect


def test_pack_keeps_tags_but_drops_junk(tmp_path):
    conn = db.open_db(str(tmp_path / "x.db"))
    p = tagged_pack()
    p["entries"][0]["phoneTags"].append("not-a-tag")
    load(conn, p)
    row = conn.execute("SELECT first_name, phone_tags FROM event_entry WHERE registration_id = 'r1'").fetchone()
    assert row["first_name"] == "Riley"
    assert row["phone_tags"] == checkin.phone_tag("ev1", PHONE)


def test_throttle_limits_per_client_and_overall():
    t = [0.0]
    th = checkin.Throttle(per_client=2, overall=3, window_s=60, clock=lambda: t[0])
    assert th.allow("a") and th.allow("a") and not th.allow("a")
    assert th.allow("b") and not th.allow("c")
    t[0] = 61
    assert th.allow("a")


# --- page --------------------------------------------------------------------

def test_closed_until_the_event_is_checked_out(site):
    conn, _admin, racer = site
    assert "open yet" in racer.get("/checkin").get_data(as_text=True)
    assert "Check in here" not in racer.get("/").get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM entry_checkin").fetchone()[0] == 0


def test_racer_checks_in_and_sees_only_name_and_todos(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    assert "Check in here" in racer.get("/").get_data(as_text=True)
    resp = post_phone(racer, "509.555.1234")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Thanks, Riley!" in body and "2WD Buggy" in body
    assert "learning table" in body  # 8954097 isn't learned on this Pi
    assert "Kid" not in body and "Sam" not in body and "B6" in body
    rec = [r for r in outbox.pending(conn) if r["type"] == "entry.checkin"]
    assert [r["payload"]["registrationId"] for r in rec] == ["r1"]


def test_parent_phone_checks_in_every_registration_it_made(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    body = post_phone(racer, PARENT).get_data(as_text=True)
    assert "Thanks, Kid!" in body or "Thanks, Sam!" in body
    assert set(checkin.checked_in(conn, "ev1")) == {"r2", "r3"}
    assert "have a transponder number" in body  # r2 has none


def test_second_check_in_queues_nothing_new(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    post_phone(racer, PHONE)
    post_phone(racer, PHONE)
    assert len([r for r in outbox.pending(conn) if r["type"] == "entry.checkin"]) == 1


def test_unknown_and_bad_numbers_reveal_nothing(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    resp = post_phone(racer, "509 555 9999")
    assert resp.status_code == 404 and "find a registration" in resp.get_data(as_text=True)
    assert post_phone(racer, "12").status_code == 400


def test_too_many_tries_are_refused(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    codes = [post_phone(racer, f"509 555 {1000 + i}").status_code for i in range(8)]
    assert codes[-1] == 429


def test_staff_note_shows_after_check_in_and_on_the_event_page(site):
    conn, admin, racer = site
    outbox.checkout(conn, "ev1")
    admin.get("/events/ev1")
    admin.post("/checkin/note", data={"csrf": csrf_of(admin), "note": "Tech inspection at the tower",
                                      "event_id": "ev1"})
    assert "Tech inspection at the tower" in post_phone(racer, PHONE).get_data(as_text=True)
    page = admin.get("/events/ev1").get_data(as_text=True)
    assert "1 of 3 registrations checked in" in page
    assert "Not here yet: Kid" in page


def test_note_is_admin_only(site):
    _conn, _admin, racer = site
    racer.get("/checkin")
    resp = racer.post("/checkin/note", data={"csrf": csrf_of(racer), "note": "x"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


# --- checklist, Ready to race!, kiosk ---------------------------------------

def learn(number, conn):
    db.upsert_transponder(conn, number=number, kind="rc4", racer_name=None, rc4_sha256=None,
                          source="learned")


def ready_post(client, prefix="/checkin"):
    client.get(f"{prefix}/status")
    return client.post(f"{prefix}/ready", data={"csrf": csrf_of(client)})


def test_checklist_shows_paid_and_transponder_state(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    body = post_phone(racer, PHONE).get_data(as_text=True)
    assert "Entry paid" in body and "Transponder learned" in body
    assert "learning table" in body  # 8954097 not learned yet
    assert 'class="ready" disabled' in body


def test_ready_refused_until_everything_is_done(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    post_phone(racer, PHONE)
    assert ready_post(racer).status_code == 409
    assert checkin.ready(conn, "ev1") == set()

    learn(8954097, conn)  # learned at the table, then "Check again"
    again = racer.get("/checkin/status").get_data(as_text=True)
    assert 'class="ready" disabled' not in again
    resp = ready_post(racer)
    assert resp.status_code == 200 and "You're ready, Riley!" in resp.get_data(as_text=True)
    assert checkin.ready(conn, "ev1") == {"r1"}


def test_unpaid_entry_blocks_ready(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    learn(7340166, conn)
    body = post_phone(racer, PARENT).get_data(as_text=True)  # r2 is pending and has no transponder
    assert "Pay at the race desk" in body
    assert ready_post(racer).status_code == 409


def test_staff_note_does_not_block_ready(site):
    conn, admin, racer = site
    outbox.checkout(conn, "ev1")
    admin.get("/events/ev1")
    admin.post("/checkin/note", data={"csrf": csrf_of(admin), "note": "Tech at the tower", "event_id": "ev1"})
    learn(8954097, conn)
    assert "Tech at the tower" in post_phone(racer, PHONE).get_data(as_text=True)
    assert ready_post(racer).status_code == 200
    assert "1 ready to race" in admin.get("/events/ev1").get_data(as_text=True)


def test_ready_without_a_phone_match_goes_back_to_the_start(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    racer.get("/checkin")
    resp = racer.post("/checkin/ready", data={"csrf": csrf_of(racer)})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/checkin")
    assert racer.get("/checkin/status").status_code == 302


def test_kiosk_hides_the_menu_and_forgets_the_racer(site):
    conn, _admin, kiosk = site
    outbox.checkout(conn, "ev1")
    learn(8954097, conn)
    page = kiosk.get("/kiosk").get_data(as_text=True)
    assert 'class="kiosk"' in page and "Admin sign in" not in page
    kiosk.get("/kiosk")
    body = kiosk.post("/kiosk", data={"csrf": csrf_of(kiosk), "phone": PHONE}).get_data(as_text=True)
    assert 'http-equiv="refresh"' in body and "url=/kiosk" in body and "Done, next racer" in body
    done = kiosk.post("/kiosk/ready", data={"csrf": csrf_of(kiosk)})
    assert done.status_code == 200 and "You're ready, Riley!" in done.get_data(as_text=True)
    # The next person at the tablet can't pull up Riley's list.
    assert kiosk.get("/kiosk/status").status_code == 302


def test_kiosk_new_screen_drops_the_last_racer(site):
    conn, _admin, kiosk = site
    outbox.checkout(conn, "ev1")
    kiosk.get("/kiosk")
    kiosk.post("/kiosk", data={"csrf": csrf_of(kiosk), "phone": PHONE})
    kiosk.get("/kiosk")  # timed out back to the start, or "Done, next racer"
    assert kiosk.get("/kiosk/status").status_code == 302


def test_checkin_link_still_points_at_checkin(site):
    _conn, _admin, racer = site
    assert 'href="/checkin"' in racer.get("/").get_data(as_text=True)


# --- "Just show what I still need" -------------------------------------------

def test_look_shows_list_without_checking_in(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    body = post_phone(racer, PHONE, action="look").get_data(as_text=True)
    assert "not checked in yet" in body and "Transponder learned" in body
    assert checkin.checked_in(conn, "ev1") == {}
    assert not [r for r in outbox.pending(conn) if r["type"] == "entry.checkin"]

    racer.get("/checkin/status")
    resp = racer.post("/checkin/arrive", data={"csrf": csrf_of(racer)})
    assert resp.status_code == 200 and "You're checked in" in resp.get_data(as_text=True)
    assert set(checkin.checked_in(conn, "ev1")) == {"r1"}


def test_arrive_needs_a_remembered_match(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    racer.get("/checkin")
    resp = racer.post("/checkin/arrive", data={"csrf": csrf_of(racer)})
    assert resp.status_code == 302 and checkin.checked_in(conn, "ev1") == {}


def test_kiosk_ignores_look_and_always_checks_in(site):
    conn, _admin, racer = site
    outbox.checkout(conn, "ev1")
    page = racer.get("/kiosk").get_data(as_text=True)
    assert "Just show what I still need" not in page
    racer.post("/kiosk", data={"csrf": csrf_of(racer), "phone": PHONE, "action": "look"})
    assert set(checkin.checked_in(conn, "ev1")) == {"r1"}
