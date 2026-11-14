"""Transponder learning: turn OpenStint's L lines into a step-by-step session.

OpenStint learns on its own whenever an unknown RC4 transponder sits still on
the loop (or the pad): `L <ts> START <rssi>`, maybe `INTERRUPTED`, then
`L <ts> DONE <auto_id> <payloads>` and a new `<auto_id>.rc4` (1000-9999) in its
folder. Learning here means: the admin types the number on the label, the
decoder does its thing, and **Record** renames `<auto_id>.rc4` to
`<label>.rc4`. The decoder rescans the folder about once a second, so the new
number is live without a restart.

The session state is worked out from the logged events each time it is read
(`describe`), so nothing has to run in the background. Same rules as
`openstint-learn` in RCRaceHQ-LapCounter-Install.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

AUTO_MIN, AUTO_MAX = 1000, 9999   # the decoder's auto-numbering range (openstint src/rc4.cpp)
LABEL_MAX = 2**31 - 1             # OpenStint transponder ids are int32
NAME_MAX = 64
EXPECTED_LEARN_S = 12.0           # RC4; an RC4 Hybrid takes ~25 s
SESSION_TIMEOUT_S = 600

WAITING = "waiting"
LIFT = "lift"                 # decoder still holds the last learn; needs a 1 s gap
LEARNING = "learning"
INTERRUPTED = "interrupted"
READY = "ready"               # DONE with an auto id: Record renames the file
KNOWN = "known"               # decoder already reports the label number
CONFLICT = "conflict"         # this transponder is already filed under another number
EXPIRED = "expired"

MESSAGES = {
    WAITING: "Turn on only this transponder and set it still on the pad or loop. Hands off.",
    LIFT: "The decoder is still holding its last learn. Lift the car off the loop "
          "(or switch the transponder off) for 2 seconds, then set it back.",
    LEARNING: "Learning. Keep it still; this takes about 12 s (25 s for a Hybrid).",
    INTERRUPTED: "It moved or the signal dropped. Keep it still; the decoder retries by itself.",
    READY: "Learned. Press Record to save it under the label number.",
    KNOWN: "The decoder already sees this transponder as {label}. Press Record to add it to the list.",
    CONFLICT: "This transponder is already known as {auto_id}, not {label}. "
              "Check the label, or delete {auto_id} and learn it again.",
    EXPIRED: "Nothing heard for 10 minutes. Cancel and start again.",
}


class LearnError(Exception):
    """Something the admin has to fix; the message is shown as is."""


def validate_label(raw: str) -> int:
    raw = (raw or "").strip()
    if not raw.isdigit():
        raise LearnError("Type the number printed on the transponder (digits only).")
    label = int(raw)
    if not 0 < label <= LABEL_MAX:
        raise LearnError("That number is out of range.")
    if AUTO_MIN <= label <= AUTO_MAX:
        raise LearnError(f"{AUTO_MIN}-{AUTO_MAX} are reserved for the decoder's own "
                         "numbering. Check the label.")
    return label


def validate_name(raw: str) -> str | None:
    name = " ".join((raw or "").split())
    if not name:
        return None
    if len(name) > NAME_MAX or not name.isprintable():
        raise LearnError(f"Racer name: up to {NAME_MAX} characters.")
    return name


def is_auto(number: int | None) -> bool:
    return number is not None and AUTO_MIN <= number <= AUTO_MAX


@dataclass(frozen=True)
class View:
    state: str
    message: str
    elapsed_s: float = 0.0
    progress: int = 0          # 0-100, an estimate from time since START
    auto_id: int | None = None
    payloads: int | None = None

    @property
    def can_record(self) -> bool:
        return self.state in (READY, KNOWN)


def describe(session: dict, events: list[dict], label_passing: dict | None,
             now_ms: int, before: dict | None = None) -> View:
    """State of an open session from the L lines (and passings) since it started.

    `before` is the last L line from before the session. After a DONE the
    decoder won't START again until it hears nothing for more than 1 s (RESET,
    openstint src/rc4.cpp), so a transponder left sitting on the loop since its
    last learn just waits forever. That case gets the LIFT hint.
    """
    label = session["label"]
    state, started, auto_id, payloads = WAITING, None, None, None
    for ev in events:
        kind = ev["event"]
        if kind == "START":
            state, started = LEARNING, ev["received_at_ms"]
        elif kind == "INTERRUPTED":
            state = INTERRUPTED
        elif kind == "RESET":
            state, started = WAITING, None
        elif kind == "DONE":
            auto_id = ev["transponder"]
            payloads = None if ev["value"] is None else int(ev["value"])
            if auto_id == label:
                state = KNOWN
            elif is_auto(auto_id):
                state = READY
            else:
                state = CONFLICT
            break  # first DONE wins; later ones are another transponder or a re-learn

    # RC3 / RC4 Hybrid transponders (or one learned earlier) send the label number
    # straight away and never produce a DONE.
    if state in (WAITING, INTERRUPTED) and label_passing is not None:
        state = KNOWN

    if state == WAITING and now_ms - session["started_at_ms"] > SESSION_TIMEOUT_S * 1000:
        state = EXPIRED

    if state == WAITING and not events and before is not None and before["event"] == "DONE":
        state = LIFT

    elapsed = 0.0
    progress = 0
    if state == LEARNING and started is not None:
        elapsed = max(0.0, (now_ms - started) / 1000)
        progress = min(95, int(elapsed / EXPECTED_LEARN_S * 100))
    elif state in (READY, KNOWN):
        progress = 100

    message = MESSAGES[state].format(label=label, auto_id=auto_id)
    return View(state, message, round(elapsed, 1), progress, auto_id, payloads)


# --- the .rc4 folder -----------------------------------------------------------

def rc4_path(rc4_dir: str | Path, number: int) -> Path:
    return Path(rc4_dir) / f"{int(number)}.rc4"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stray_auto_files(rc4_dir: str | Path) -> list[int]:
    """Auto-numbered files nobody recorded yet; they make it unclear who is who."""
    folder = Path(rc4_dir)
    if not folder.is_dir():
        return []
    return sorted(int(p.stem) for p in folder.glob("*.rc4")
                  if p.stem.isdigit() and is_auto(int(p.stem)))


def learned_numbers(rc4_dir: str | Path) -> set[int]:
    folder = Path(rc4_dir)
    if not folder.is_dir():
        return set()
    return {int(p.stem) for p in folder.glob("*.rc4")
            if p.stem.isdigit() and not is_auto(int(p.stem))}


def rename_learned(rc4_dir: str | Path, auto_id: int, label: int,
                   wait_s: float = 3.0) -> Path:
    """`<auto_id>.rc4` -> `<label>.rc4`. Never overwrites an existing label file."""
    if not is_auto(auto_id):
        raise LearnError(f"{auto_id} is not a decoder auto number; not renaming it.")
    source, target = rc4_path(rc4_dir, auto_id), rc4_path(rc4_dir, label)
    deadline = time.monotonic() + wait_s
    while not source.exists():
        if time.monotonic() > deadline:
            raise LearnError(f"{source.name} isn't in {rc4_dir}. Is the decoder's folder "
                             "mounted into the app (OPENSTINT_RC4_DIR)?")
        time.sleep(0.05)
    # Not os.link: fs.protected_hardlinks blocks linking the decoder's files.
    # One admin, one session at a time, so check-then-rename is enough.
    if target.exists():
        raise LearnError(f"{target.name} already exists; not overwriting it.")
    try:
        os.rename(source, target)
    except PermissionError:
        raise LearnError(f"No permission to write in {rc4_dir}. The app needs the "
                         "folder's group (see the lapcounter_app role).") from None
    return target


def delete_learned(rc4_dir: str | Path, number: int) -> bool:
    """Remove `<number>.rc4`. The decoder drops it on its next folder scan
    (openstint src/rc4.cpp), so the transponder can be learned again."""
    path = rc4_path(rc4_dir, number)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except PermissionError:
        raise LearnError(f"No permission to write in {rc4_dir}. The app needs the "
                         "folder's group (see the lapcounter_app role).") from None
    return True
