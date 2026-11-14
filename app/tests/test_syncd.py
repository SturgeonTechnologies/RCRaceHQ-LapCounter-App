import hashlib
import json

import pytest

from lapcounter import cloud, db, outbox, syncd
from lapcounter.cloud import CloudError
from lapcounter.config import Config

THING = "lc-dev-93a7"


class FakeCloud:
    """AWS as syncd sees it: a topic, an ack object and an upload prefix."""

    def __init__(self):
        self.sent: list[dict] = []
        self.ack: dict | None = None
        self.uploads: dict[str, bytes] = {}
        self.down = False

    def publish(self, payload: bytes) -> None:
        if self.down:
            raise CloudError("Can't reach AWS IoT")
        self.sent.append(json.loads(payload))

    def get_ack(self) -> bytes | None:
        return json.dumps(self.ack).encode() if self.ack else None

    def put(self, key: str, body: bytes) -> None:
        if self.down:
            raise CloudError("Can't reach S3")
        self.uploads[key] = body

    def as_cloud(self) -> syncd.Cloud:
        return syncd.Cloud(publish=self.publish, get_ack=self.get_ack, put=self.put, thing=THING)


@pytest.fixture
def conn(tmp_path):
    return db.open_db(str(tmp_path / "lc.db"))


@pytest.fixture
def fake():
    return FakeCloud()


def add(conn, n):
    for i in range(n):
        outbox.add(conn, "heat.lap", "ev1", {"i": i}, f"u{i}")


def test_sends_one_batch_and_waits_for_the_ack(conn, fake):
    add(conn, 3)
    c = fake.as_cloud()
    syncd.step(conn, c, now=100)
    assert len(fake.sent) == 1 and [r["seq"] for r in fake.sent[0]["records"]] == [1, 2, 3]
    syncd.step(conn, c, now=101)  # no ack yet, not time to resend
    assert len(fake.sent) == 1
    fake.ack = {"outboxId": outbox.outbox_id(conn), "seq": 3, "rejected": []}
    syncd.step(conn, c, now=102)
    assert outbox.counts(conn)["waiting"] == 0
    assert outbox.status(conn)["last"]["ok"] is True


def test_resends_when_no_ack_arrives(conn, fake):
    add(conn, 1)
    c = fake.as_cloud()
    syncd.step(conn, c, now=100)
    syncd.step(conn, c, now=100 + syncd.RESEND_S + 1)
    assert len(fake.sent) == 2
    assert fake.sent[0]["records"] == fake.sent[1]["records"]


def test_partial_ack_keeps_the_rest_pending(conn, fake):
    add(conn, 3)
    c = fake.as_cloud()
    syncd.step(conn, c, now=100)
    fake.ack = {"outboxId": outbox.outbox_id(conn), "seq": 2, "rejected": [],
                "error": "RC Race HQ couldn't save everything just now"}
    syncd.step(conn, c, now=101)
    assert outbox.counts(conn)["waiting"] == 1
    assert "couldn't save" in outbox.status(conn)["last"]["error"]
    syncd.step(conn, c, now=100 + syncd.RESEND_S + 1)
    assert [r["seq"] for r in fake.sent[-1]["records"]] == [3]


def test_ignores_an_ack_for_another_outbox(conn, fake):
    add(conn, 1)
    c = fake.as_cloud()
    syncd.step(conn, c, now=100)
    fake.ack = {"outboxId": "someone-else", "seq": 99, "rejected": []}
    syncd.step(conn, c, now=101)
    assert outbox.counts(conn)["waiting"] == 1


def write_rc4(folder, number, body):
    folder.mkdir(exist_ok=True)
    (folder / f"{number}.rc4").write_bytes(body)


def test_uploads_each_learned_file_once(conn, fake, tmp_path):
    rc4 = tmp_path / "rc4"
    write_rc4(rc4, 8954097, b"learned A")
    write_rc4(rc4, 1234, b"auto numbered, not recorded yet")
    c = fake.as_cloud()
    assert syncd.queue_learned(conn, rc4, c) == 1
    assert syncd.queue_learned(conn, rc4, c) == 0
    sha = hashlib.sha256(b"learned A").hexdigest()
    assert fake.uploads == {f"uploads/{THING}/rc4/8954097-{sha}.rc4": b"learned A"}
    rec = outbox.pending(conn)[0]
    assert rec["type"] == "transponder.learned" and rec["eventId"] == "library"
    assert rec["payload"] == {"transponder": 8954097, "sha256": sha, "size": 9}
    # Relearned: different bytes go up again.
    write_rc4(rc4, 8954097, b"learned B")
    assert syncd.queue_learned(conn, rc4, c) == 1


def test_a_failed_upload_queues_nothing(conn, fake, tmp_path):
    rc4 = tmp_path / "rc4"
    write_rc4(rc4, 8954097, b"learned A")
    fake.down = True
    with pytest.raises(CloudError):
        syncd.queue_learned(conn, rc4, fake.as_cloud())
    assert outbox.pending(conn) == []


def test_library_status_shows_a_locked_refusal(conn, fake, tmp_path):
    rc4 = tmp_path / "rc4"
    write_rc4(rc4, 7340166, b"x")
    c = fake.as_cloud()
    syncd.queue_learned(conn, rc4, c)
    assert syncd.library_status(conn)[7340166]["state"] == "waiting"
    syncd.step(conn, c, now=100)
    fake.ack = {"outboxId": outbox.outbox_id(conn), "seq": 1,
                "rejected": [{"seq": 1, "reason": "Transponder 7340166 is already registered in RC Race HQ and locked"}]}
    syncd.step(conn, c, now=101)
    st = syncd.library_status(conn)[7340166]
    assert st["state"] == "refused" and "already registered" in st["reason"]


def test_run_once_does_nothing_in_the_cloud_when_unlinked(conn, tmp_path):
    called = []
    syncd.run_once(conn, Config(rc4_dir=str(tmp_path)), 100.0, lambda link: called.append(link))
    assert called == []


def test_run_once_reports_a_network_failure(conn, fake, tmp_path):
    db.set_setting(conn, cloud.LINK_KEY, json.dumps({"thingName": THING}))
    add(conn, 1)
    fake.down = True
    syncd.run_once(conn, Config(rc4_dir=str(tmp_path)), 100.0, lambda link: fake.as_cloud())
    assert outbox.status(conn)["last"]["ok"] is False
    assert outbox.counts(conn)["waiting"] == 1
