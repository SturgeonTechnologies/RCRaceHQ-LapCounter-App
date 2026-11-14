"""Runtime settings, all from environment variables (set by compose / the role)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    db_path: str = "/var/lib/lapcounter/lapcounter.db"
    decoder_url: str = "tcp://127.0.0.1:5556"
    # How long the decoder can be silent before the UI calls it offline. OpenStint
    # sends an S line every second, so a few missed ones means it's gone.
    decoder_stale_s: float = 5.0
    # OpenStint's .rc4 folder (its -s directory), bind-mounted into the web
    # container. Recording a learned transponder renames a file in here.
    rc4_dir: str = "/var/lib/openstint"
    # RC Race HQ HTTP API (the stack's ApiEndpoint output). Empty = Link is off.
    rcracehq_api_url: str = ""
    # Link key + certificate (0700 folder, 0600 files) on the app's data volume.
    cloud_dir: str = "/var/lib/lapcounter/cloud"

    # Host folder (bind-mounted) holding wifi.json for the hotspot; the deploy applies it.
    wifi_dir: str = "/etc/lapcounter"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            db_path=env.get("LAPCOUNTER_DB", cls.db_path),
            decoder_url=env.get("OPENSTINT_ZMQ", cls.decoder_url),
            decoder_stale_s=float(env.get("LAPCOUNTER_DECODER_STALE_S", cls.decoder_stale_s)),
            rc4_dir=env.get("OPENSTINT_RC4_DIR", cls.rc4_dir),
            rcracehq_api_url=env.get("RCRACEHQ_API_URL", cls.rcracehq_api_url),
            cloud_dir=env.get("LAPCOUNTER_CLOUD_DIR", cls.cloud_dir),
            wifi_dir=env.get("LAPCOUNTER_WIFI_DIR", cls.wifi_dir),
        )
