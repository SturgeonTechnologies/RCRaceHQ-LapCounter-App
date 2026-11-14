import json
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from lapcounter import cloud, db

PAIRED = {
    "status": "paired",
    "lapCounterId": "93a7",
    "thingName": "lc-dev-93a7",
    "certificatePem": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    "club": {"clubId": "c1", "name": "Riley's Radical RC Racing"},
    "track": {"trackId": "t1", "name": "Riley's Place"},
    "stage": "dev",
    "region": "us-west-2",
    "iotEndpoint": "a1-ats.iot.us-west-2.amazonaws.com",
    "credentialsEndpoint": "c1.credentials.iot.us-west-2.amazonaws.com",
    "roleAlias": "rcracehq-dev-lapcounter",
    "topicPrefix": "rchq/dev/lc/lc-dev-93a7",
    "bucket": "rcracehq-dev-lapcounter-1",
}


class FakePost:
    """Scripted RC Race HQ: start returns a code; status replies come from a list."""

    def __init__(self, statuses=None, start=(200, None)):
        self.calls = []
        self.statuses = list(statuses or [])
        self.start = start

    def __call__(self, url, body):
        self.calls.append((url, body))
        if url.endswith("/lapcounters/pair"):
            status, out = self.start
            return status, out if out is not None else {"code": "K7QX-4M2P", "pollToken": "tok", "expiresAt": 2_000}
        return self.statuses.pop(0)


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(str(tmp_path / "lc.db"))
    yield c
    c.close()


@pytest.fixture
def paths(tmp_path):
    return cloud.Paths(tmp_path / "cloud")


def test_key_and_csr_are_p256_and_match():
    key_pem, csr_pem = cloud.new_key_and_csr()
    key = serialization.load_pem_private_key(key_pem, None)
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert csr.is_signature_valid
    assert key.curve.name == "secp256r1"
    assert csr.public_key().public_numbers() == key.public_key().public_numbers()
    assert csr_pem.startswith("-----BEGIN CERTIFICATE REQUEST-----")


def test_start_sends_csr_and_keeps_pending_key(conn, paths):
    post = FakePost()
    pairing = cloud.start_link(conn, paths, cloud.Api("https://api.example/", post), now=1_000)
    url, body = post.calls[0]
    assert url == "https://api.example/lapcounters/pair"
    assert "CERTIFICATE REQUEST" in body["csr"]
    assert pairing["code"] == "K7QX-4M2P"
    assert cloud.current_pairing(conn)["pollToken"] == "tok"
    assert stat.S_IMODE(paths.pending_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.dir.stat().st_mode) == 0o700
    assert not paths.key.exists()


def test_start_refused_cleans_up(conn, paths):
    api = cloud.Api("https://api.example", FakePost(start=(429, {"error": "Too Many Requests"})))
    with pytest.raises(cloud.CloudError, match="HTTP 429: Too Many Requests"):
        cloud.start_link(conn, paths, api, now=1_000)
    assert cloud.current_pairing(conn) is None
    assert not paths.pending_key.exists()


def test_waiting_then_paired_stores_cert_key_and_link(conn, paths):
    post = FakePost(statuses=[(200, {"status": "waiting"}), (200, PAIRED)])
    api = cloud.Api("https://api.example", post)
    cloud.start_link(conn, paths, api, now=1_000)
    pending = paths.pending_key.read_bytes()

    assert cloud.poll_link(conn, paths, api, now=1_003)["state"] == "waiting"
    assert cloud.poll_link(conn, paths, api, now=1_006) == {"state": "linked"}

    assert post.calls[1][1] == {"code": "K7QX-4M2P", "pollToken": "tok"}
    assert paths.key.read_bytes() == pending
    assert paths.cert.read_text() == PAIRED["certificatePem"]
    assert stat.S_IMODE(paths.cert.stat().st_mode) == 0o600
    assert not paths.pending_key.exists()
    link = cloud.current_link(conn)
    assert link["thingName"] == "lc-dev-93a7"
    assert link["track"]["name"] == "Riley's Place"
    assert link["linkedAt"] == 1_006
    assert "certificatePem" not in json.dumps(link)
    assert cloud.current_pairing(conn) is None
    assert cloud.poll_link(conn, paths, api, now=1_010) == {"state": "linked"}


def test_polls_are_throttled(conn, paths):
    post = FakePost(statuses=[(200, {"status": "waiting"})])
    api = cloud.Api("https://api.example", post)
    cloud.start_link(conn, paths, api, now=1_000)
    cloud.poll_link(conn, paths, api, now=1_003)
    assert cloud.poll_link(conn, paths, api, now=1_004)["state"] == "waiting"
    assert len(post.calls) == 2  # start + one status


def test_expired_locally_skips_the_api(conn, paths):
    post = FakePost()
    api = cloud.Api("https://api.example", post)
    cloud.start_link(conn, paths, api, now=1_000)
    assert cloud.poll_link(conn, paths, api, now=2_000)["state"] == "expired"
    assert len(post.calls) == 1
    assert not paths.pending_key.exists()


@pytest.mark.parametrize("reply, state, text", [
    ((404, {"status": "expired"}), "expired", "expired"),
    ((200, {"status": "failed", "error": "IoT said no"}), "failed", "IoT said no"),
])
def test_server_side_end_clears_pairing(conn, paths, reply, state, text):
    api = cloud.Api("https://api.example", FakePost(statuses=[reply]))
    cloud.start_link(conn, paths, api, now=1_000)
    out = cloud.poll_link(conn, paths, api, now=1_005)
    assert out["state"] == state and text in out["message"]
    assert cloud.current_pairing(conn) is None


def test_relink_keeps_old_link_until_new_one_completes(conn, paths):
    api = cloud.Api("https://api.example", FakePost(statuses=[(200, PAIRED)]))
    cloud.start_link(conn, paths, api, now=1_000)
    cloud.poll_link(conn, paths, api, now=1_005)
    old_key = paths.key.read_bytes()

    cloud.start_link(conn, paths, api, now=1_100)
    assert paths.key.read_bytes() == old_key
    assert cloud.current_link(conn)["thingName"] == "lc-dev-93a7"
    cloud.cancel_link(conn, paths)
    assert paths.key.read_bytes() == old_key
    assert not paths.pending_key.exists()


def test_forget_removes_everything(conn, paths):
    api = cloud.Api("https://api.example", FakePost(statuses=[(200, PAIRED)]))
    cloud.start_link(conn, paths, api, now=1_000)
    cloud.poll_link(conn, paths, api, now=1_005)
    cloud.forget_link(conn, paths)
    assert cloud.current_link(conn) is None
    assert not paths.key.exists() and not paths.cert.exists()


def _linked(conn, paths):
    key_pem, _ = cloud.new_key_and_csr()
    cloud._write_private(paths.key, key_pem)
    # Self-signed cert for that key so ssl.load_cert_chain accepts the pair.
    import datetime
    from cryptography.hazmat.primitives import hashes
    key = serialization.load_pem_private_key(key_pem, None)
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "t")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cloud._write_private(paths.cert, cert.public_bytes(serialization.Encoding.PEM))
    link = {k: PAIRED.get(k) for k in cloud.LINK_FIELDS}
    return link


def test_fetch_credentials_sends_thing_name(conn, paths):
    link = _linked(conn, paths)
    seen = {}

    def get(url, headers, context):
        seen.update(url=url, headers=headers)
        return 200, {"credentials": {"accessKeyId": "AK", "expiration": "2026-10-30T19:32:52Z"}}

    creds = cloud.fetch_credentials(link, paths, get)
    assert creds["accessKeyId"] == "AK"
    assert seen["url"] == ("https://c1.credentials.iot.us-west-2.amazonaws.com"
                           "/role-aliases/rcracehq-dev-lapcounter/credentials")
    assert seen["headers"] == {"x-amzn-iot-thingname": "lc-dev-93a7"}


def test_fetch_credentials_revoked(conn, paths):
    link = _linked(conn, paths)
    with pytest.raises(cloud.CloudError, match="unlinked in RC Race HQ"):
        cloud.fetch_credentials(link, paths, lambda *a: (403, {"message": "Forbidden"}))


def test_fetch_credentials_without_files(paths):
    link = {k: PAIRED.get(k) for k in cloud.LINK_FIELDS}
    with pytest.raises(cloud.CloudError, match="missing"):
        cloud.fetch_credentials(link, paths, lambda *a: (200, {}))
