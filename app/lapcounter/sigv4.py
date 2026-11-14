"""AWS Signature Version 4 for the one call the Pi makes to S3: GET an object.

Hand-rolled so the image doesn't need boto3. Only what GetObject needs: no
query string, signed headers = host + every x-amz-* header + range.
https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def amz_date(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def canonical_path(key: str) -> str:
    """S3 encodes each path segment once (no double encoding like other services)."""
    return "/" + quote(key.lstrip("/"), safe="/-_.~")


def sign(*, method: str, host: str, path: str, headers: dict[str, str], region: str,
         service: str, access_key: str, secret_key: str, date: str,
         payload_hash: str = UNSIGNED_PAYLOAD) -> dict[str, str]:
    """Return the request headers (lower-case names) including Authorization.

    `headers` are extra headers to sign (e.g. range, x-amz-security-token);
    host, x-amz-date and x-amz-content-sha256 are added here.
    """
    signed = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    signed.update({"host": host, "x-amz-date": date, "x-amz-content-sha256": payload_hash})
    names = sorted(signed)
    canonical = "\n".join([
        method,
        path,
        "",  # no query string
        "".join(f"{n}:{signed[n]}\n" for n in names),
        ";".join(names),
        payload_hash,
    ])
    scope = f"{date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", date, scope, hashlib.sha256(canonical.encode()).hexdigest(),
    ])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date[:8]), region), service),
                "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    signed["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                               f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return signed
