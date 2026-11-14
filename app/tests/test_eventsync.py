import hashlib
import json
from datetime import datetime, timezone

import pytest

from lapcounter import cloud, db, eventsync
from lapcounter.cloud import CloudError

from .test_cloud import PAIRED

THING = "lc-dev-93a7"
LINK = {k: PAIRED.get(k) for k in cloud.LINK_FIELDS}


def pack(event_id="ev1", **over):
    out = {
        "schema": 1, "eventId": event_id, "clubId": "c1", "title": "Test Race",
        "description": "", "dateISO": "2026-11-07T09:00:00", "endDateISO": None,
        "location": "Riley's Place", "days": ["2026-11-07"],
        "classes": [{"name": "2WD Buggy", "qualifyingRounds": 2, "mains": 1}],
        "entries": [
            {"registrationId": "r1", "racerUserId": "u1", "racerName": "Riley",
             "racerClass": "2WD Buggy", "status": "paid", "transponder": "8954097",
             "vehicle": "B6 (2WD Buggy)"},
            {"registrationId": "r2", "racerUserId": None, "racerName": "Kid",
             "racerClass": "2WD Buggy", "status": "pending", "transponder": None, "vehicle": None},
            {"registrationId": "r3", "racerUserId": "u3", "racerName": "Sam",
             "racerClass": "2WD Buggy", "status": "paid", "transponder": 7340166, "vehicle": None},
        ],
        "heats": [{"heatId": "h1", "className": "2WD Buggy", "roundType": "qualifier",
                   "roundNumber": 1, "dayISO": "2026-11-07", "order": 1, "entries": []}],
        "generatedAt": "2026-10-31T00:00:00Z", "futureField": True,
    }
    out.update(over)
    return out


def raw(obj):
    return json.dumps(obj).encode()


class FakeS3:
    """Index replies come from a list (the last one repeats); packs from a dict."""

    def __init__(self, indexes, packs):
        self.indexes, self.packs, self.gets = list(indexes), packs, []

    def get(self, key, absent=(404,)):
        self.gets.append(key)
        if key.endswith("/index.json"):
            item = self.indexes.pop(0) if len(self.indexes) > 1 else self.indexes[0]
            return item if item is None else raw(item)
        return self.packs.get(key)


def index_for(request_id, packs, **over):
    out = {"schema": 1, "requestId": request_id, "generatedAt": "x",
           "lapCounter": {"clubName": "Riley's Radical RC Racing", "trackName": "Riley's Place"},
           "events": [{"eventId": json.loads(b)["eventId"], "title": "T", "dateISO": "2026-11-07",
                       "packKey": k, "packSha256": hashlib.sha256(b).hexdigest(),
                       "entryCount": 3, "heatCount": 1} for k, b in packs.items()]}
    out.update(over)
    return out


@pytest.fixture
def conn():
    c = db.open_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def paths(tmp_path):
    p = cloud.Paths(tmp_path)
    p.key.write_text("k")
    p.cert.write_text("c")
    return p


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(conn, paths, s3, *, publish=None, clock=None):
    clock = clock or Clock()
    calls = []

    def pub(host, client_id, topic, body, context):
        calls.append((host, client_id, topic, json.loads(body)))
    result = eventsync.sync_events(conn, LINK, paths, s3=s3, publish=publish or pub,
                                   sleep=clock.sleep, clock=clock, request_id="req1")
    return result, calls


def test_publish_url_and_body(conn, paths):
    packs = {f"packs/{THING}/ev1.json": raw(pack())}
    _, calls = run(conn, paths, FakeS3([index_for("req1", packs)], packs))
    assert calls == [(LINK["iotEndpoint"], THING, f"rchq/dev/lc/{THING}/events/get",
                      {"requestId": "req1"})]


@pytest.fixture(autouse=True)
def no_tls(monkeypatch):
    monkeypatch.setattr(eventsync.ssl.SSLContext, "load_cert_chain", lambda *a, **k: None)


def test_waits_for_our_request_then_stores(conn, paths):
    packs = {f"packs/{THING}/ev1.json": raw(pack())}
    s3 = FakeS3([None, index_for("older", packs), index_for("req1", packs)], packs)
    result, _ = run(conn, paths, s3)
    assert (result.events, result.updated, result.removed) == (1, 1, 0)
    assert [e["title"] for e in db.list_events(conn)] == ["Test Race"]
    entries = db.list_event_entries(conn, "ev1")
    assert {e["registration_id"]: e["transponder"] for e in entries} == {"r1": "8954097", "r2": None, "r3": "7340166"}
    assert eventsync.last_sync(conn)["lapCounter"]["trackName"] == "Riley's Place"

    # Same hash again: nothing downloaded.
    s3 = FakeS3([index_for("req1", packs)], packs)
    result, _ = run(conn, paths, s3)
    assert result.updated == 0 and not any(k.endswith("ev1.json") for k in s3.gets)


def test_timeout(conn, paths):
    s3 = FakeS3([index_for("older", {})], {})
    with pytest.raises(CloudError, match="didn't answer"):
        run(conn, paths, s3)


def test_error_in_index_keeps_local_events(conn, paths):
    packs = {f"packs/{THING}/ev1.json": raw(pack())}
    run(conn, paths, FakeS3([index_for("req1", packs)], packs))
    s3 = FakeS3([index_for("req1", {}, error="This lap counter was unlinked.")], {})
    with pytest.raises(CloudError, match="unlinked"):
        run(conn, paths, s3)
    assert len(db.list_events(conn)) == 1


def test_removed_events_go(conn, paths):
    packs = {f"packs/{THING}/ev1.json": raw(pack()), f"packs/{THING}/ev2.json": raw(pack("ev2"))}
    run(conn, paths, FakeS3([index_for("req1", packs)], packs))
    keep = {f"packs/{THING}/ev2.json": packs[f"packs/{THING}/ev2.json"]}
    result, _ = run(conn, paths, FakeS3([index_for("req1", keep)], keep))
    assert result.removed == 1 and [e["event_id"] for e in db.list_events(conn)] == ["ev2"]
    assert db.list_event_entries(conn, "ev1") == []


def test_sha_mismatch_refused(conn, paths):
    packs = {f"packs/{THING}/ev1.json": raw(pack())}
    index = index_for("req1", packs)
    tampered = {f"packs/{THING}/ev1.json": raw(pack(title="Other"))}
    with pytest.raises(CloudError, match="checksum"):
        run(conn, paths, FakeS3([index], tampered))
    assert db.list_events(conn) == []


def test_schema_refused(conn, paths):
    packs = {f"packs/{THING}/ev1.json": raw(pack(schema=2))}
    with pytest.raises(CloudError, match="schema 2"):
        run(conn, paths, FakeS3([index_for("req1", packs)], packs))
    with pytest.raises(CloudError, match="schema"):
        run(conn, paths, FakeS3([index_for("req1", {}, schema=9)], {}))


def test_pack_key_must_be_own_folder(conn, paths):
    packs = {"packs/lc-other/ev1.json": raw(pack())}
    with pytest.raises(CloudError, match="outside"):
        run(conn, paths, FakeS3([index_for("req1", packs)], packs))


def test_publish_refused_says_relink(conn, paths):
    def refused(*a):
        raise CloudError(eventsync.REFUSED)
    with pytest.raises(CloudError, match="link this Pi again"):
        run(conn, paths, FakeS3([None], {}), publish=refused)


class FakeBroker:
    """One end of a socketpair answering like AWS IoT."""

    def __init__(self, replies):
        self.sent, self.replies = b"", list(replies)

    def sendall(self, data):
        self.sent += data
        self._buf = self.replies.pop(0) if self.replies else b""

    def recv(self, n):
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def test_mqtt_packets():
    assert eventsync.connect_packet("lc-dev-x") == (
        b"\x10\x14\x00\x04MQTT\x04\x02\x00\x1e\x00\x08lc-dev-x")
    pkt = eventsync.publish_packet("a/b", b"{}")
    assert pkt == b"\x32\x09\x00\x03a/b\x00\x01{}"
    assert eventsync._remaining_length(321) == b"\xc1\x02"


def test_mqtt_exchange_ok():
    broker = FakeBroker([b"\x20\x02\x00\x00", b"\x40\x02\x00\x01", b""])
    eventsync.mqtt_exchange(broker, THING, "t/x", b"{}")
    assert broker.sent.startswith(eventsync.connect_packet(THING))
    assert broker.sent.endswith(eventsync.publish_packet("t/x", b"{}") + b"\xe0\x00")


@pytest.mark.parametrize("replies", [
    [b""],                                  # closed on CONNECT
    [b"\x20\x02\x00\x05"],                 # CONNACK not authorized
    [b"\x20\x02\x00\x00", b""],            # closed on PUBLISH (topic not allowed)
])
def test_mqtt_exchange_refused(replies):
    with pytest.raises(CloudError, match="link this Pi again"):
        eventsync.mqtt_exchange(FakeBroker(replies), THING, "t/x", b"{}")


def test_s3_signs_and_caches_creds():
    calls, gets = [], []

    def creds():
        calls.append(1)
        return {"accessKeyId": "A", "secretAccessKey": "S", "sessionToken": "T",
                "expiration": "2026-10-31T01:00:00Z"}

    def get(url, headers):
        gets.append((url, headers))
        return 200, b"{}"
    now = [datetime(2026, 10, 31, 0, 20, tzinfo=timezone.utc).timestamp()]
    s3 = eventsync.S3("bkt", "us-west-2", creds, get, clock=lambda: now[0])
    s3.get(f"packs/{THING}/index.json")
    s3.get(f"packs/{THING}/index.json")
    assert len(calls) == 1
    url, headers = gets[0]
    assert url == f"https://bkt.s3.us-west-2.amazonaws.com/packs/{THING}/index.json"
    assert headers["x-amz-security-token"] == "T" and headers["authorization"].startswith("AWS4-HMAC-SHA256")
    now[0] += 40 * 60  # within 5 min of expiry
    s3.get("k")
    assert len(calls) == 2


def test_s3_404_is_none_and_403_raises():
    creds = lambda: {"accessKeyId": "A", "secretAccessKey": "S", "sessionToken": "T", "expiration": None}
    assert eventsync.S3("b", "r", creds, lambda u, h: (404, b"")).get("k") is None
    with pytest.raises(CloudError, match="403"):
        eventsync.S3("b", "r", creds, lambda u, h: (403, b"")).get("k")


def test_to_learn(conn, paths, tmp_path):
    packs = {f"packs/{THING}/ev1.json": raw(pack())}
    run(conn, paths, FakeS3([index_for("req1", packs)], packs))
    rc4 = tmp_path / "rc4"
    rc4.mkdir()
    (rc4 / "7340166.rc4").write_bytes(b"x")  # learned file, not in the list
    rows = eventsync.to_learn(conn, rc4)
    assert [(r["registration_id"], r["reason"], r["learnable"]) for r in rows] == [
        ("r2", "none", False), ("r1", "not_recorded", True)]
    db.upsert_transponder(conn, number=8954097, kind="rc4", racer_name=None, rc4_sha256=None,
                          source="learned")
    assert [r["registration_id"] for r in eventsync.to_learn(conn, rc4, "ev1")] == ["r2"]


def test_index_403_means_not_yet():
    creds = lambda: {"accessKeyId": "A", "secretAccessKey": "S", "sessionToken": "T", "expiration": None}
    s3 = eventsync.S3("b", "r", creds, lambda u, h: (403, b""))
    assert s3.get("packs/t/index.json", absent=(403, 404)) is None
