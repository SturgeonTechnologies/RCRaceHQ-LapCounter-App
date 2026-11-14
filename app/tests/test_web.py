import json

import pytest

from lapcounter import db
from lapcounter.web import live

from .conftest import csrf_of, do_setup, get_csrf

PW = "correct horse battery"


def test_everything_redirects_to_setup_first(client):
    for path in ("/", "/admin", "/login", "/api/live", "/hotspot-detect.html"):
        r = client.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/setup"), path


def test_health_and_static_open_before_setup(client):
    assert client.get("/healthz").get_json()["ok"] is True
    assert client.get("/static/live.js").status_code == 200


def test_setup_creates_admin_and_signs_in(client, cfg):
    r = do_setup(client)
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert db.admin_exists(db.connect(cfg.db_path))
    assert client.get("/admin").status_code == 200


def test_setup_closed_once_done(client):
    do_setup(client)
    other = client.application.test_client()
    assert other.get("/setup").headers["Location"].endswith("/login")
    token = get_csrf(other, "/login")
    r = other.post("/setup", data={"csrf": token, "username": "evil", "password": PW, "confirm": PW})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


@pytest.mark.parametrize("form,msg", [
    ({"username": "", "password": PW, "confirm": PW}, b"Pick a username"),
    ({"username": "r", "password": "short", "confirm": "short"}, b"at least 10"),
    ({"username": "r", "password": PW, "confirm": PW + "x"}, b"don&#39;t match"),
])
def test_setup_validation(client, form, msg):
    token = get_csrf(client, "/setup")
    r = client.post("/setup", data={"csrf": token, **form})
    assert r.status_code == 400 and msg in r.data


def test_posts_need_csrf(client):
    client.get("/setup")
    r = client.post("/setup", data={"csrf": "nope", "username": "r", "password": PW, "confirm": PW})
    assert r.status_code == 400


def test_login_logout_flow(client):
    do_setup(client)
    client.post("/logout", data={"csrf": csrf_of(client)})
    assert client.get("/admin").status_code == 302
    token = get_csrf(client, "/login")
    bad = client.post("/login", data={"csrf": token, "username": "riley", "password": "wrong password!"})
    assert bad.status_code == 401
    ok = client.post("/login?next=/admin", data={"csrf": token, "username": "riley", "password": PW})
    assert ok.status_code == 302 and ok.headers["Location"] == "/admin"


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example/", "/\\evil"])
def test_login_never_redirects_off_site(client, target):
    do_setup(client)
    client.post("/logout", data={"csrf": csrf_of(client)})
    token = get_csrf(client, "/login")
    r = client.post(f"/login?next={target}", data={"csrf": token, "username": "riley", "password": PW})
    assert r.headers["Location"] == "/"


def test_login_lockout(client):
    do_setup(client)
    client.post("/logout", data={"csrf": csrf_of(client)})
    token = get_csrf(client, "/login")
    for _ in range(5):
        client.post("/login", data={"csrf": token, "username": "riley", "password": "nope nope nope"})
    r = client.post("/login", data={"csrf": token, "username": "riley", "password": PW})
    assert r.status_code == 429


def test_live_page_is_public_after_setup(client):
    do_setup(client)
    anon = client.application.test_client()
    r = anon.get("/")
    assert r.status_code == 200 and b"Latest passings" in r.data
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert anon.get("/admin").status_code == 302


def test_live_json(client, cfg):
    do_setup(client)
    conn = db.connect(cfg.db_path)
    db.update_stats(conn, noise=-47.0, received=700, processed=600)
    first = db.insert_passing(conn, transponder=1, kind="AMB", decoder_ts_ms=1, rssi=-30, hits=2)
    db.insert_passing(conn, transponder=8954097, kind="AMB", decoder_ts_ms=2, rssi=-31, hits=3)
    data = client.get(f"/api/live?after={first}").get_json()
    assert data["decoder"]["online"] is True
    assert [p["transponder"] for p in data["passings"]] == [8954097]


def test_decoder_view_staleness():
    status = {"updated_at_ms": 0, "noise": -47.0, "received": 1, "processed": 1}
    assert live.decoder_view(status, 5.0, 4000)["online"] is True
    assert live.decoder_view(status, 5.0, 6000)["online"] is False
    assert live.decoder_view(None, 5.0, 0) == {"online": False, "seen": False}
    # Only a learn event seen, never stats: not online.
    assert live.decoder_view({"updated_at_ms": 0, "noise": None}, 5.0, 0)["online"] is False


def test_stream_events_sends_passings_and_status(cfg):
    conn = db.open_db(cfg.db_path)
    first = db.insert_passing(conn, transponder=1, kind="AMB", decoder_ts_ms=1, rssi=-30, hits=2)
    db.insert_passing(conn, transponder=2, kind="AMB", decoder_ts_ms=2, rssi=-30, hits=2)
    chunks = list(live.stream_events(cfg.db_path, 5.0, after_id=first, seconds=0, poll=0))
    assert chunks[0].startswith("retry:")
    events = chunks[1:]
    assert events[0].startswith(f"id: {first + 1}\nevent: passing\n")
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload["transponder"] == 2
    assert events[1].startswith("event: decoder\n")


def test_stream_endpoint_headers(client):
    do_setup(client)
    r = client.get("/api/live/stream", buffered=False)
    assert r.mimetype == "text/event-stream"
    assert r.headers["Cache-Control"] == "no-store"
    first = next(iter(r.response))
    assert (first if isinstance(first, bytes) else first.encode()).startswith(b"retry:")
    r.close()


def test_secret_key_persists_across_app_instances(cfg):
    from lapcounter.web import create_app
    assert create_app(cfg).secret_key == create_app(cfg).secret_key



# ── behind race-portal (http://race/ on port 80) ─────────────────────────

def _addr_through_proxy(remote, forwarded=None):
    from lapcounter.web import TrustLocalProxy
    seen = {}

    def inner(environ, start_response):
        seen["addr"] = environ["REMOTE_ADDR"]
        start_response("200 OK", [])
        return [b""]

    environ = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        environ["HTTP_X_FORWARDED_FOR"] = forwarded
    TrustLocalProxy(inner)(environ, lambda *a: None)
    return seen["addr"]


@pytest.mark.parametrize("remote,forwarded,expected", [
    ("127.0.0.1", "10.77.0.61", "10.77.0.61"),          # phone via race-portal
    ("::1", "10.77.0.61", "10.77.0.61"),
    ("127.0.0.1", "6.6.6.6, 10.77.0.61", "10.77.0.61"),  # only the portal's own entry counts
    ("192.168.0.40", "10.77.0.61", "192.168.0.40"),      # wired client can't spoof
    ("10.77.0.61", "127.0.0.1", "10.77.0.61"),
    ("127.0.0.1", None, "127.0.0.1"),
    ("127.0.0.1", " ", "127.0.0.1"),
])
def test_forwarded_for_only_trusted_from_this_pi(remote, forwarded, expected):
    assert _addr_through_proxy(remote, forwarded) == expected


def test_app_is_wrapped(tmp_path):
    from lapcounter.config import Config
    from lapcounter.web import TrustLocalProxy, create_app
    app = create_app(Config(db_path=str(tmp_path / "p.db")), testing=True)
    assert isinstance(app.wsgi_app, TrustLocalProxy)
