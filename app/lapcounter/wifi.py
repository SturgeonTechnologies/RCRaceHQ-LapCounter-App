"""Hotspot Wi-Fi settings (Settings → Advanced).

The web container can't touch NetworkManager, so it writes the wanted setup to
a small JSON file in a directory bind-mounted from the host
(``LAPCOUNTER_WIFI_DIR``, default /etc/lapcounter). The host side (the
lapcounter_app role in RCRaceHQ-LapCounter-Install) watches ``wifi.json`` and
applies it to the hotspot's NetworkManager profile.

wifi.json: {"security": "open" | "wpa2", "ssid": str, "passphrase": str}

Default is open so racers can check in and see race status.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

FILE = "wifi.json"
DEFAULT = {"security": "open", "ssid": "RC Race Event", "passphrase": ""}


class WifiError(ValueError):
    pass


def load(directory: str | Path) -> dict:
    try:
        data = json.loads((Path(directory) / FILE).read_text())
    except (OSError, ValueError):
        return dict(DEFAULT)
    if not isinstance(data, dict):
        return dict(DEFAULT)
    out = dict(DEFAULT)
    for key in out:
        if isinstance(data.get(key), str):
            out[key] = data[key]
    if out["security"] not in ("open", "wpa2"):
        out["security"] = "open"
    return out


def validate(security: str, ssid: str, passphrase: str) -> dict:
    ssid = ssid.strip()
    if security not in ("open", "wpa2"):
        raise WifiError("Pick open or WPA2.")
    if not 1 <= len(ssid.encode()) <= 32:
        raise WifiError("Network name must be 1-32 characters.")
    if security == "open":
        return {"security": "open", "ssid": ssid, "passphrase": ""}
    if not 8 <= len(passphrase) <= 63 or not passphrase.isascii():
        raise WifiError("WPA2 passphrase must be 8-63 plain characters.")
    return {"security": "wpa2", "ssid": ssid, "passphrase": passphrase}


def save(directory: str | Path, conf: dict) -> None:
    d = Path(directory)
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".wifi-")
        with os.fdopen(fd, "w") as f:
            json.dump(conf, f)
        os.chmod(tmp, 0o640)
        os.replace(tmp, d / FILE)
    except OSError as exc:
        raise WifiError(f"Couldn't write {d / FILE}: {exc.strerror or exc}") from exc
