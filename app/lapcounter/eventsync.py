"""Download this club's events from RC Race HQ (docs/event-sync.md).

1. `request_events` publishes `{"requestId"}` to `<topicPrefix>/events/get` over
   MQTT (port 8883, client ID = thing name, the device certificate as client cert).
2. RC Race HQ writes one pack per event to `packs/<thing>/<eventId>.json` in the
   lap counter bucket, then `packs/<thing>/index.json` carrying our requestId.
3. `sync_events` polls the index until it carries our requestId, downloads the
   packs whose sha256 changed, checks each hash and stores them; events that
   are no longer in the index are removed.

S3 is read with short-lived keys from the IoT credentials provider
(cloud.fetch_credentials), signed with sigv4.py. Every network call is
injectable so tests never touch AWS.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import cloud, db, sigv4
from .cloud import CloudError

SCHEMA = 1
LAST_SYNC_KEY = "events.last_sync"
POLL_INTERVAL_S = 1.5
POLL_TIMEOUT_S = 25.0
MAX_OBJECT_BYTES = 5 * 1024 * 1024
CREDS_REFRESH_S = 5 * 60
HTTP_TIMEOUT_S = 10.0
EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --- publish (MQTT over TLS with the device certificate) ---------------------
#
# The IoT policy scopes topics with ${iot:Connection.Thing.ThingName}, which AWS
# only resolves for MQTT connections whose client ID is the thing name. HTTPS
# publishes (port 8443) have no client ID, so AWS refused them with 403. This is
# the smallest MQTT 3.1.1 client that does CONNECT -> PUBLISH QoS 1 -> PUBACK.

MQTT_PORT = 8883

Publish = Callable[[str, str, str, bytes, ssl.SSLContext], None]


def _remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _string(value: str) -> bytes:
    raw = value.encode()
    return len(raw).to_bytes(2, "big") + raw


def _packet(header: int, body: bytes) -> bytes:
    return bytes([header]) + _remaining_length(len(body)) + body


def connect_packet(client_id: str, keepalive: int = 30) -> bytes:
    # Protocol "MQTT" level 4 (3.1.1), clean session, no will/user/password.
    return _packet(0x10, _string("MQTT") + bytes([4, 0x02]) + keepalive.to_bytes(2, "big")
                   + _string(client_id))


def publish_packet(topic: str, payload: bytes, packet_id: int = 1) -> bytes:
    return _packet(0x32, _string(topic) + packet_id.to_bytes(2, "big") + payload)  # QoS 1


def _read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionResetError("closed by AWS IoT")
        buf += chunk
    return buf


REFUSED = ("AWS IoT refused the certificate. It was probably unlinked in "
           "RC Race HQ; link this Pi again under Settings.")


def mqtt_exchange(sock, client_id: str, topic: str, payload: bytes) -> None:
    """CONNECT, PUBLISH (QoS 1), wait for PUBACK, DISCONNECT on an open stream.

    AWS IoT answers an unauthorized CONNECT or PUBLISH by closing the
    connection, so a reset at either step means the certificate was refused.
    """
    try:
        sock.sendall(connect_packet(client_id))
        connack = _read_exact(sock, 4)
        if connack[0] != 0x20 or connack[3] != 0:
            raise CloudError(REFUSED if connack[3] in (4, 5) else
                             f"AWS IoT refused the connection (CONNACK {connack[3]}).")
        sock.sendall(publish_packet(topic, payload))
        puback = _read_exact(sock, 4)
        if puback[0] != 0x40 or puback[2:4] != b"\x00\x01":
            raise CloudError(f"AWS IoT sent an unexpected reply ({puback.hex()}).")
        sock.sendall(b"\xe0\x00")
    except (ConnectionResetError, BrokenPipeError, ssl.SSLEOFError) as exc:
        raise CloudError(REFUSED) from exc


def mqtt_publish(host: str, client_id: str, topic: str, payload: bytes,
                 context: ssl.SSLContext) -> None:
    try:
        with socket.create_connection((host, MQTT_PORT), timeout=HTTP_TIMEOUT_S) as raw:
            with context.wrap_socket(raw, server_hostname=host) as sock:
                mqtt_exchange(sock, client_id, topic, payload)
    except CloudError:
        raise
    except ssl.SSLError as exc:
        raise CloudError(REFUSED) from exc
    except (TimeoutError, OSError) as exc:
        raise CloudError(f"Can't reach AWS IoT ({exc}). Check the Pi's internet.") from exc


def request_events(link: dict, paths: cloud.Paths, request_id: str,
                   publish: Publish = mqtt_publish) -> None:
    if not (paths.key.exists() and paths.cert.exists()):
        raise CloudError("The key or certificate is missing on this Pi. Link it again.")
    context = ssl.create_default_context()
    context.load_cert_chain(certfile=str(paths.cert), keyfile=str(paths.key))
    publish(link["iotEndpoint"], link["thingName"], f"{link['topicPrefix']}/events/get",
            json.dumps({"requestId": request_id}).encode(), context)


# --- S3 (GET only, SigV4 with credentials-provider keys) ----------------------

HttpGet = Callable[[str, dict], tuple[int, bytes]]


def http_get(url: str, headers: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read(MAX_OBJECT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"Can't reach S3 ({getattr(exc, 'reason', exc)}). "
                         "Check the Pi's internet.") from exc


HttpPut = Callable[[str, dict, bytes], int]


def http_put(url: str, headers: dict, body: bytes) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"Can't reach S3 ({getattr(exc, 'reason', exc)}). "
                         "Check the Pi's internet.") from exc


Creds = Callable[[], dict]


class S3:
    """Reads objects from the lap counter bucket with cached short-lived keys."""

    def __init__(self, bucket: str, region: str, creds: Creds, get: HttpGet = http_get,
                 clock: Callable[[], float] = time.time, put: HttpPut = http_put):
        self.bucket, self.region = bucket, region
        self._creds, self._get, self._clock, self._put = creds, get, clock, put
        self._cached: dict | None = None
        self._expires = 0.0

    def _keys(self) -> dict:
        if self._cached is None or self._clock() >= self._expires - CREDS_REFRESH_S:
            creds = self._creds()
            self._cached, self._expires = creds, _epoch(creds.get("expiration"), self._clock())
        return self._cached

    def get(self, key: str, *, absent: tuple[int, ...] = (404,)) -> bytes | None:
        """Object bytes, or None if S3 answered one of `absent` (it doesn't exist yet)."""
        keys = self._keys()
        host = f"{self.bucket}.s3.{self.region}.amazonaws.com"
        path = sigv4.canonical_path(key)
        headers = sigv4.sign(
            method="GET", host=host, path=path, region=self.region, service="s3",
            headers={"x-amz-security-token": keys["sessionToken"]},
            access_key=keys["accessKeyId"], secret_key=keys["secretAccessKey"],
            date=sigv4.amz_date(datetime.fromtimestamp(self._clock(), timezone.utc)),
        )
        headers.pop("host")
        status, body = self._get(f"https://{host}{path}", headers)
        if status == 200:
            if len(body) > MAX_OBJECT_BYTES:
                raise CloudError(f"{key} is larger than {MAX_OBJECT_BYTES // 1024} KB; refusing it.")
            return body
        if status in absent:
            return None
        if status == 403:
            self._cached = None  # keys may have expired early; fetch fresh ones next time
            raise CloudError(f"S3 refused {key} (403). Link the Pi again if this keeps happening.")
        raise CloudError(f"S3 answered HTTP {status} for {key}.")


    def put(self, key: str, body: bytes) -> None:
        """Upload an object (only uploads/<thing>/ is allowed for the Pi)."""
        keys = self._keys()
        host = f"{self.bucket}.s3.{self.region}.amazonaws.com"
        path = sigv4.canonical_path(key)
        headers = sigv4.sign(
            method="PUT", host=host, path=path, region=self.region, service="s3",
            headers={"x-amz-security-token": keys["sessionToken"]},
            access_key=keys["accessKeyId"], secret_key=keys["secretAccessKey"],
            date=sigv4.amz_date(datetime.fromtimestamp(self._clock(), timezone.utc)),
            payload_hash=hashlib.sha256(body).hexdigest(),
        )
        headers.pop("host")
        headers["content-type"] = "application/octet-stream"
        status = self._put(f"https://{host}{path}", headers, body)
        if status == 200:
            return
        if status == 403:
            self._cached = None
            raise CloudError(f"S3 refused the upload of {key} (403). RC Race HQ may not "
                             "be updated for transponder uploads yet.")
        raise CloudError(f"S3 answered HTTP {status} uploading {key}.")


def _epoch(iso: str | None, now: float) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return now + 15 * 60


# --- validation --------------------------------------------------------------

def parse_index(raw: bytes, thing: str) -> dict:
    try:
        index = json.loads(raw)
    except ValueError as exc:
        raise CloudError("RC Race HQ's event index isn't valid JSON.") from exc
    if not isinstance(index, dict) or index.get("schema") != SCHEMA:
        raise CloudError(f"RC Race HQ sent event index schema {index.get('schema') if isinstance(index, dict) else '?'};"
                         f" this lap counter understands {SCHEMA}. Update the lap counter app.")
    for ev in index.get("events") or []:
        event_id = ev.get("eventId") if isinstance(ev, dict) else None
        if not isinstance(event_id, str) or not EVENT_ID.match(event_id):
            raise CloudError(f"RC Race HQ listed an event with a bad id: {event_id!r}.")
        if ev.get("packKey") != f"packs/{thing}/{event_id}.json":
            raise CloudError(f"Event {event_id} points outside this lap counter's folder.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(ev.get("packSha256", ""))):
            raise CloudError(f"Event {event_id} has no valid packSha256.")
    return index


def parse_pack(raw: bytes, event_id: str) -> dict:
    try:
        pack = json.loads(raw)
    except ValueError as exc:
        raise CloudError(f"The pack for event {event_id} isn't valid JSON.") from exc
    if not isinstance(pack, dict) or pack.get("schema") != SCHEMA:
        raise CloudError(f"The pack for event {event_id} is schema "
                         f"{pack.get('schema') if isinstance(pack, dict) else '?'}; this lap counter "
                         f"understands {SCHEMA}. Update the lap counter app.")
    if pack.get("eventId") != event_id:
        raise CloudError(f"The pack for event {event_id} is for {pack.get('eventId')!r}.")
    for key in ("classes", "entries", "heats"):
        if not isinstance(pack.get(key, []), list):
            raise CloudError(f"The pack for event {event_id} has a bad {key} list.")
    for e in pack.get("entries", []):
        if not isinstance(e, dict) or not e.get("registrationId"):
            raise CloudError(f"The pack for event {event_id} has an entry with no registrationId.")
        if e.get("transponder") is not None:
            e["transponder"] = str(e["transponder"]).strip() or None
    return pack


# --- the whole download --------------------------------------------------------

@dataclass(frozen=True)
class Result:
    events: int
    updated: int
    removed: int
    lap_counter: dict

    def message(self) -> str:
        n = "event" if self.events == 1 else "events"
        out = f"Downloaded {self.events} {n} ({self.updated} new or changed"
        return out + (f", {self.removed} removed)." if self.removed else ").")


def sync_events(conn, link: dict, paths: cloud.Paths, *, s3: S3 | None = None,
                publish: Publish = mqtt_publish, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic,
                request_id: str | None = None) -> Result:
    thing = link["thingName"]
    s3 = s3 or S3(link["bucket"], link["region"], lambda: cloud.fetch_credentials(link, paths))
    request_id = request_id or uuid.uuid4().hex
    request_events(link, paths, request_id, publish)

    index_key = f"packs/{thing}/index.json"
    deadline = clock() + POLL_TIMEOUT_S
    while True:
        # Before the first answer index.json doesn't exist, and S3 reports a
        # missing key as 403 (not 404) unless the GET itself passes the
        # ListBucket prefix condition -- so both mean "not yet" here.
        raw = s3.get(index_key, absent=(403, 404))
        if raw is not None:
            index = parse_index(raw, thing)
            if index.get("requestId") == request_id:
                break
        if clock() >= deadline:
            raise CloudError("RC Race HQ didn't answer the download request within "
                             f"{int(POLL_TIMEOUT_S)} s. Try again; if it keeps happening the "
                             "event download isn't deployed on RC Race HQ yet.")
        sleep(POLL_INTERVAL_S)

    if index.get("error"):
        raise CloudError(f"RC Race HQ: {index['error']}")

    have = db.event_hashes(conn)
    listed = index.get("events") or []
    updated = 0
    for ev in listed:
        event_id, want = ev["eventId"], ev["packSha256"]
        if have.get(event_id) == want:
            continue
        body = s3.get(ev["packKey"])
        if body is None:
            raise CloudError(f"The pack for {ev.get('title') or event_id} is missing in S3.")
        got = hashlib.sha256(body).hexdigest()
        if got != want:
            raise CloudError(f"The pack for {ev.get('title') or event_id} didn't match its "
                             "checksum (changed while downloading?). Try again.")
        pack = parse_pack(body, event_id)
        db.store_event_pack(conn, pack, got, body.decode())
        updated += 1
    removed = db.delete_events_except(conn, {ev["eventId"] for ev in listed})

    lap_counter = index.get("lapCounter") if isinstance(index.get("lapCounter"), dict) else {}
    db.set_setting(conn, LAST_SYNC_KEY, json.dumps({
        "at_ms": db.now_ms(), "events": len(listed), "lapCounter": lap_counter,
        "generatedAt": index.get("generatedAt"),
    }))
    return Result(len(listed), updated, removed, lap_counter)


def last_sync(conn) -> dict | None:
    raw = db.get_setting(conn, LAST_SYNC_KEY)
    return json.loads(raw) if raw else None


# --- transponders to learn ----------------------------------------------------

def known_transponders(conn, rc4_dir: str | Path) -> set[str]:
    """Numbers this Pi can time: recorded in the list, or with a learned .rc4 file."""
    from .learning import learned_numbers
    numbers = {t["number"] for t in db.list_transponders(conn)} | learned_numbers(rc4_dir)
    return {str(n) for n in numbers}


def to_learn(conn, rc4_dir: str | Path, event_id: str | None = None) -> list[dict]:
    """Entries whose transponder is missing or not recorded on this Pi.

    Each row gets `reason` ("none" | "not_recorded") and `learnable` (the
    number can be pre-filled into Learn).
    """
    known = known_transponders(conn, rc4_dir)
    out = []
    for e in db.list_event_entries(conn, event_id):
        number = e["transponder"]
        if number and number in known:
            continue
        e["reason"] = "none" if not number else "not_recorded"
        e["learnable"] = bool(number and number.isdigit())
        out.append(e)
    return out


def to_learn_by_event(conn, rc4_dir: str | Path) -> list[dict]:
    """to_learn grouped per event, events in date order:
    [{event_id, event_title, event_date, rows: [...]}]. Each row also gets
    `notified_at_ms` (last notify request, or None)."""
    notices = db.last_transponder_notices(conn)
    groups: dict[str, dict] = {}
    for e in to_learn(conn, rc4_dir):
        e["notified_at_ms"] = notices.get((e["event_id"], e["registration_id"]))
        g = groups.setdefault(e["event_id"], {
            "event_id": e["event_id"], "event_title": e["event_title"],
            "event_date": e["event_date"], "rows": []})
        g["rows"].append(e)
    return list(groups.values())


def racer_counts(conn, rc4_dir: str | Path, event_id: str | None = None) -> dict:
    """Racers and missing transponders, per event and per class:
    {event_id: {"racers": n, "missing": m, "classes": {class: {"racers", "missing"}}}}."""
    known = known_transponders(conn, rc4_dir)
    out: dict[str, dict] = {}
    for e in db.list_event_entries(conn, event_id):
        ev = out.setdefault(e["event_id"], {"racers": 0, "missing": 0, "classes": {}})
        cls = ev["classes"].setdefault(e["racer_class"], {"racers": 0, "missing": 0})
        missing = not (e["transponder"] and e["transponder"] in known)
        for bucket in (ev, cls):
            bucket["racers"] += 1
            bucket["missing"] += missing
    return out
