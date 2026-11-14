"""Link this lap counter to a track in RC Race HQ (docs/club-sync.md, "Pairing").

1. `start_link` makes a fresh P-256 key + CSR, sends the CSR to
   `POST /lapcounters/pair` and keeps the returned code + poll token.
2. A club manager types the code in RC Race HQ → Manage → club → Lap counters.
3. `poll_link` calls `POST /lapcounters/pair/status` until it says `paired`,
   then stores the signed certificate and the connection settings.

The private key is made here and never leaves the Pi. While a pairing is open
the new key is `pending.key`; an existing link keeps working until the new one
completes, so re-linking to another track is safe to abandon halfway.

`fetch_credentials` trades the certificate for short-lived AWS keys at the IoT
credentials provider. Settings → Link uses it as the "is the link alive" check;
syncd will use it for event packs.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import db

PAIRING_KEY = "cloud.pairing"
LINK_KEY = "cloud.link"
# pair/status is throttled to 5 req/s for everyone; several open tabs shouldn't add up.
MIN_POLL_INTERVAL_S = 2.5
HTTP_TIMEOUT_S = 10.0

# What we keep from the paired response (the certificate goes to a file instead).
LINK_FIELDS = (
    "lapCounterId", "thingName", "club", "track", "stage", "region", "iotEndpoint",
    "credentialsEndpoint", "roleAlias", "topicPrefix", "bucket",
)


class CloudError(Exception):
    """Something the admin should see, already worded for them."""


@dataclass(frozen=True)
class Paths:
    dir: Path

    @property
    def key(self) -> Path:
        return self.dir / "device.key"

    @property
    def cert(self) -> Path:
        return self.dir / "device.crt"

    @property
    def pending_key(self) -> Path:
        return self.dir / "pending.key"


# --- HTTP --------------------------------------------------------------------

Post = Callable[[str, dict], tuple[int, dict]]


def http_post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON, return (status, json). Network failures become CloudError."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, _json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"Can't reach RC Race HQ ({_reason(exc)}). Check the Pi's internet.") from exc


def _json(raw: bytes) -> dict:
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _reason(exc: Exception) -> str:
    return str(getattr(exc, "reason", exc))


class Api:
    """The two public pairing routes on the RC Race HQ HTTP API."""

    def __init__(self, base_url: str, post: Post = http_post):
        self.base_url = base_url.rstrip("/")
        self._post = post

    def start(self, csr_pem: str) -> dict:
        status, body = self._post(f"{self.base_url}/lapcounters/pair", {"csr": csr_pem})
        if status != 200 or not body.get("code") or not body.get("pollToken"):
            raise CloudError(f"RC Race HQ refused the link request (HTTP {status}"
                             f"{': ' + str(body['error']) if body.get('error') else ''}).")
        return body

    def status(self, code: str, poll_token: str) -> dict:
        status, body = self._post(f"{self.base_url}/lapcounters/pair/status",
                                  {"code": code, "pollToken": poll_token})
        if status == 404:
            return {"status": "expired"}
        if status != 200:
            raise CloudError(f"RC Race HQ answered HTTP {status} while waiting for the code.")
        return body


# --- key + CSR -----------------------------------------------------------------

def new_key_and_csr() -> tuple[bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rcracehq-lapcounter")]))
           .sign(key, hashes.SHA256()))
    return key_pem, csr.public_bytes(serialization.Encoding.PEM).decode()


def _write_private(path: Path, data: bytes) -> None:
    """Write a 0600 file atomically (temp file + rename in the same folder)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# --- state -------------------------------------------------------------------

def _load(conn, key: str) -> dict | None:
    raw = db.get_setting(conn, key)
    return json.loads(raw) if raw else None


def current_link(conn) -> dict | None:
    return _load(conn, LINK_KEY)


def current_pairing(conn) -> dict | None:
    return _load(conn, PAIRING_KEY)


def _clear_pairing(conn, paths: Paths) -> None:
    db.delete_setting(conn, PAIRING_KEY)
    paths.pending_key.unlink(missing_ok=True)


def start_link(conn, paths: Paths, api: Api, *, now: float | None = None) -> dict:
    """New key + CSR → pairing code. Replaces any pairing still open."""
    now = time.time() if now is None else now
    _clear_pairing(conn, paths)
    key_pem, csr_pem = new_key_and_csr()
    _write_private(paths.pending_key, key_pem)
    try:
        started = api.start(csr_pem)
    except CloudError:
        paths.pending_key.unlink(missing_ok=True)
        raise
    pairing = {
        "code": started["code"],
        "pollToken": started["pollToken"],
        "expiresAt": int(started.get("expiresAt") or now + 600),
        "lastPollAt": 0,
    }
    db.set_setting(conn, PAIRING_KEY, json.dumps(pairing))
    return pairing


def cancel_link(conn, paths: Paths) -> None:
    _clear_pairing(conn, paths)


def poll_link(conn, paths: Paths, api: Api, *, now: float | None = None) -> dict:
    """Advance an open pairing one step. Returns what the page should show:

    {"state": "idle" | "waiting" | "linked" | "expired" | "failed", ...}
    """
    now = time.time() if now is None else now
    pairing = current_pairing(conn)
    if pairing is None:
        return {"state": "linked" if current_link(conn) else "idle"}
    if pairing["expiresAt"] <= now:
        _clear_pairing(conn, paths)
        return {"state": "expired", "message": "The code expired before it was entered. Start again."}
    waiting = {"state": "waiting", "code": pairing["code"], "expiresAt": pairing["expiresAt"]}
    if now - pairing.get("lastPollAt", 0) < MIN_POLL_INTERVAL_S:
        return waiting
    pairing["lastPollAt"] = now
    db.set_setting(conn, PAIRING_KEY, json.dumps(pairing))

    res = api.status(pairing["code"], pairing["pollToken"])
    status = res.get("status")
    if status == "waiting":
        return waiting
    if status == "paired":
        return _finish(conn, paths, res, now)
    _clear_pairing(conn, paths)
    if status == "failed":
        return {"state": "failed", "message": f"RC Race HQ couldn't link it: {res.get('error') or 'unknown error'}"}
    return {"state": "expired", "message": "The code expired or was already used. Start again."}


def _finish(conn, paths: Paths, res: dict, now: float) -> dict:
    cert = res.get("certificatePem")
    if not cert or not res.get("thingName"):
        raise CloudError("RC Race HQ said linked but sent no certificate.")
    if not paths.pending_key.exists():
        # Another request finished this pairing a moment ago.
        return {"state": "linked"}
    _write_private(paths.cert, cert.encode())
    os.replace(paths.pending_key, paths.key)
    link = {k: res.get(k) for k in LINK_FIELDS}
    link["linkedAt"] = int(now)
    db.set_setting(conn, LINK_KEY, json.dumps(link))
    db.delete_setting(conn, PAIRING_KEY)
    return {"state": "linked"}


def forget_link(conn, paths: Paths) -> None:
    """Drop the link on this Pi. RC Race HQ still lists it until it's unlinked there."""
    _clear_pairing(conn, paths)
    db.delete_setting(conn, LINK_KEY)
    paths.key.unlink(missing_ok=True)
    paths.cert.unlink(missing_ok=True)


# --- credentials provider ----------------------------------------------------

Get = Callable[[str, dict, ssl.SSLContext], tuple[int, dict]]


def https_get(url: str, headers: dict, context: ssl.SSLContext) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=context) as resp:
            return resp.status, _json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"Can't reach AWS IoT ({_reason(exc)}). Check the Pi's internet.") from exc


def fetch_credentials(link: dict, paths: Paths, get: Get = https_get) -> dict:
    """Certificate → {accessKeyId, secretAccessKey, sessionToken, expiration}."""
    if not (paths.key.exists() and paths.cert.exists()):
        raise CloudError("The key or certificate is missing on this Pi. Link it again.")
    context = ssl.create_default_context()
    context.load_cert_chain(certfile=str(paths.cert), keyfile=str(paths.key))
    url = f"https://{link['credentialsEndpoint']}/role-aliases/{link['roleAlias']}/credentials"
    status, body = get(url, {"x-amzn-iot-thingname": link["thingName"]}, context)
    if status == 200 and isinstance(body.get("credentials"), dict):
        return body["credentials"]
    if status in (401, 403):
        raise CloudError("AWS refused the certificate. It was probably unlinked in RC Race HQ; link this Pi again.")
    raise CloudError(f"AWS IoT answered HTTP {status}: {body.get('message', 'no detail')}")
