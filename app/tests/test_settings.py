import time

import pytest

from lapcounter import cloud
from lapcounter.config import Config
from lapcounter.web import create_app
from lapcounter.web.auth import reset_throttle

from .conftest import csrf_of, do_setup
from .test_cloud import PAIRED, FakePost


def make(tmp_path, *, api_url="https://api.example", statuses=None, fake=True):
    reset_throttle()
    cfg = Config(db_path=str(tmp_path / "lc.db"), decoder_url="tcp://127.0.0.1:1",
                 rcracehq_api_url=api_url, cloud_dir=str(tmp_path / "cloud"))
    app = create_app(cfg, testing=True)
    post = FakePost(statuses=statuses, start=(200, {"code": "K7QX-4M2P", "pollToken": "tok",
                                                     "expiresAt": int(time.time()) + 600}))
    if fake:
        app.extensions["rcracehq_api"] = cloud.Api(api_url, post)
    client = app.test_client()
    do_setup(client)
    client.post_calls = post.calls
    return client


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(cloud, "MIN_POLL_INTERVAL_S", 0)


def post(client, path):
    return client.post(path, data={"csrf": csrf_of(client)})


def test_needs_admin(tmp_path):
    client = create_app(Config(db_path=str(tmp_path / "lc.db")), testing=True).test_client()
    do_setup(client)
    client.post("/logout", data={"csrf": csrf_of(client)})
    for path in ("/settings", "/api/link"):
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_not_configured(tmp_path):
    client = make(tmp_path, api_url="", fake=False)
    assert b"isn't configured" in client.get("/settings").data
    r = post(client, "/settings/link")
    assert "configured" in r.headers["Location"]


def test_link_flow(tmp_path):
    client = make(tmp_path, statuses=[(200, {"status": "waiting"}), (200, PAIRED)])
    page = client.get("/settings").data
    assert b"Link to RC Race HQ" in page

    post(client, "/settings/link")
    page = client.get("/settings").data
    assert b"K7QX-4M2P" in page and b"link.js" in page

    assert client.get("/api/link").get_json()["state"] == "waiting"
    r = client.get("/api/link")
    assert r.get_json() == {"state": "linked"} and r.headers["Cache-Control"] == "no-store"

    page = client.get("/settings").data.decode()
    assert "Linked to Riley&#39;s Radical RC Racing / Riley&#39;s Place" in page
    assert "lc-dev-93a7" in page and "Link to another track" in page
    assert (tmp_path / "cloud" / "device.crt").exists()


def test_cancel(tmp_path):
    client = make(tmp_path)
    post(client, "/settings/link")
    post(client, "/settings/link/cancel")
    assert b"K7QX" not in client.get("/settings").data
    assert not (tmp_path / "cloud" / "pending.key").exists()


def test_network_error_keeps_waiting(tmp_path):
    client = make(tmp_path)
    post(client, "/settings/link")

    def down(url, body):
        raise cloud.CloudError("Can't reach RC Race HQ (timed out). Check the Pi's internet.")
    client.application.extensions["rcracehq_api"] = cloud.Api("https://api.example", down)
    out = client.get("/api/link").get_json()
    assert out["state"] == "waiting" and "Can't reach" in out["message"]
    assert b"K7QX-4M2P" in client.get("/settings").data


def test_check_and_forget(tmp_path, monkeypatch):
    client = make(tmp_path, statuses=[(200, PAIRED)])
    post(client, "/settings/link")
    client.get("/api/link")

    monkeypatch.setattr(cloud, "fetch_credentials",
                        lambda link, paths: {"expiration": "2026-10-30T19:32:52Z"})
    r = post(client, "/settings/link/check")
    assert "Connection+OK" in r.headers["Location"] and "19:32+UTC" in r.headers["Location"]

    def refused(link, paths):
        raise cloud.CloudError("AWS refused the certificate.")
    monkeypatch.setattr(cloud, "fetch_credentials", refused)
    assert "refused" in post(client, "/settings/link/check").headers["Location"]

    post(client, "/settings/link/forget")
    assert b"Not linked" in client.get("/settings").data
    assert not (tmp_path / "cloud" / "device.key").exists()


def test_csrf_required(tmp_path):
    client = make(tmp_path)
    for path in ("/settings/link", "/settings/link/cancel", "/settings/link/check", "/settings/link/forget"):
        assert client.post(path).status_code == 400, path
    assert client.post("/settings/wifi").status_code == 400


def wifi_client(tmp_path):
    reset_throttle()
    cfg = Config(db_path=str(tmp_path / "lc.db"), decoder_url="tcp://127.0.0.1:1",
                 wifi_dir=str(tmp_path / "wifi"))
    client = create_app(cfg, testing=True).test_client()
    do_setup(client)
    return client


def wifi_post(client, **data):
    return client.post("/settings/wifi", data={"csrf": csrf_of(client), **data})


def test_wifi_defaults_open(tmp_path):
    page = wifi_client(tmp_path).get("/settings").data
    assert b'value="open" selected' in page and b"RC Race Event" in page


def test_wifi_wpa2_saved(tmp_path):
    import json
    client = wifi_client(tmp_path)
    r = wifi_post(client, security="wpa2", ssid="Track", passphrase="hunter2hunter2")
    assert "notice" in r.headers["Location"]
    saved = json.loads((tmp_path / "wifi" / "wifi.json").read_text())
    assert saved == {"security": "wpa2", "ssid": "Track", "passphrase": "hunter2hunter2"}
    assert b'value="wpa2" selected' in client.get("/settings").data


def test_wifi_open_drops_passphrase(tmp_path):
    import json
    client = wifi_client(tmp_path)
    wifi_post(client, security="open", ssid="Track", passphrase="ignored-pass")
    assert json.loads((tmp_path / "wifi" / "wifi.json").read_text())["passphrase"] == ""


@pytest.mark.parametrize("data", [
    dict(security="wpa2", ssid="Track", passphrase="short"),
    dict(security="wep", ssid="Track", passphrase=""),
    dict(security="open", ssid="", passphrase=""),
    dict(security="open", ssid="x" * 33, passphrase=""),
])
def test_wifi_rejects_bad(tmp_path, data):
    client = wifi_client(tmp_path)
    assert "error" in wifi_post(client, **data).headers["Location"]
    assert not (tmp_path / "wifi" / "wifi.json").exists()
