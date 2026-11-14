"""Walk-up check-in (lapcounter/checkin.py, docs/checkin.md).

/checkin is for a racer's own phone on the hotspot. /kiosk is the same flow
for a shared tablet at the race desk: bigger, and it clears itself after
each racer. Either way, after the phone number matches, the racer sees a
checklist (checked in, paid, transponder learned, staff note) and a
"Ready to race!" button that works once everything is done.

The match is kept in the Flask session (signed cookie) for SESSION_MS so
"Check again" and "Ready to race!" don't ask for the number again. The
kiosk drops it as soon as a racer is done or the screen times out."""

from __future__ import annotations

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from .. import checkin, db
from .auth import check_csrf, login_required

bp = Blueprint("checkin", __name__)
THROTTLE = checkin.Throttle()
SESSION_KEY = "checkin"
SESSION_MS = 15 * 60 * 1000
KIOSK_IDLE_S = 45


def reset_checkin_throttle() -> None:
    THROTTLE.reset()


def _kiosk() -> bool:
    return request.path.startswith("/kiosk")


def _form(events, *, error=None, phone="", status=200):
    return render_template("checkin.html", events=events, error=error, phone=phone,
                           kiosk=_kiosk(), idle_s=KIOSK_IDLE_S, ep=ep), status


def _show(conn, event_id: str, entries: list[dict], *, message=None, status=200):
    cfg = current_app.config["LAPCOUNTER"]
    items = checkin.checklist(conn, cfg.rc4_dir, entries)
    here = checkin.checked_in(conn, event_id)
    return render_template(
        "checkin_done.html",
        arrived=bool(entries) and all(e["registration_id"] in here for e in entries),
        name=checkin.first_name(entries),
        classes=sorted({e["racer_class"] for e in entries}),
        items=items,
        ready_ok=checkin.all_done(items),
        is_ready=bool(entries) and {e["registration_id"] for e in entries} <= checkin.ready(conn, event_id),
        message=message,
        kiosk=_kiosk(),
        idle_s=KIOSK_IDLE_S,
        ep=ep,
    ), status


def _remembered(conn):
    """(event_id, entries) from this browser's last match, or None."""
    saved = session.get(SESSION_KEY)
    if not saved or db.now_ms() - saved.get("at", 0) > SESSION_MS:
        session.pop(SESSION_KEY, None)
        return None
    if saved["event"] not in {e["event_id"] for e in checkin.open_events(conn)}:
        session.pop(SESSION_KEY, None)
        return None
    entries = checkin.entries_by_id(conn, saved["event"], saved["regs"])
    return (saved["event"], entries) if entries else None


def ep(name: str) -> str:
    """URL for this flow's page: /kiosk/... on the kiosk, /checkin/... otherwise."""
    return url_for(f"checkin.kiosk_{name}" if _kiosk() else f"checkin.{name}")


def _home():
    return ep("form")


@bp.get("/checkin")
@bp.get("/kiosk", endpoint="kiosk_form")
def form():
    from . import get_db
    if _kiosk():
        session.pop(SESSION_KEY, None)  # a fresh screen for the next racer
    return _form(checkin.open_events(get_db()))


@bp.post("/checkin")
@bp.post("/kiosk", endpoint="kiosk_submit")
def submit():
    from . import get_db
    check_csrf()
    conn = get_db()
    events = checkin.open_events(conn)
    if not events:
        return _form(events)
    raw = request.form.get("phone", "")[:40]
    if not THROTTLE.allow(request.remote_addr or "?"):
        return _form(events, phone=raw, status=429,
                     error="Too many tries. Wait a minute, or see the race desk.")
    e164 = checkin.normalize_phone(raw)
    if e164 is None:
        return _form(events, phone=raw, status=400,
                     error="That doesn't look like a phone number. Use the one on your RC Race HQ account.")
    ids = {e["event_id"] for e in events}
    event_id = request.form.get("event_id") or events[0]["event_id"]
    if event_id not in ids:
        event_id = events[0]["event_id"]
    entries = checkin.find_entries(conn, event_id, e164)
    if not entries:
        return _form(events, phone=raw, status=404,
                     error="We couldn't find a registration with that number. "
                           "Check it's the phone on your RC Race HQ account, or see the race desk.")
    # "Just show my list" (racer's own phone only) looks without checking in,
    # e.g. from the parking lot. The desk kiosk always checks in.
    if _kiosk() or request.form.get("action") != "look":
        checkin.check_in(conn, event_id, entries)
    session[SESSION_KEY] = {"event": event_id, "regs": [e["registration_id"] for e in entries],
                            "at": db.now_ms()}
    return _show(conn, event_id, checkin.entries_by_id(conn, event_id, [e["registration_id"] for e in entries]))


@bp.get("/checkin/status")
@bp.get("/kiosk/status", endpoint="kiosk_status")
def status():
    """"Check again" after learning a transponder or paying at the desk."""
    from . import get_db
    conn = get_db()
    found = _remembered(conn)
    if not found:
        return redirect(_home())
    return _show(conn, *found)


@bp.post("/checkin/arrive")
def arrive():
    """"I'm here" after looking at the list without checking in."""
    from . import get_db
    check_csrf()
    conn = get_db()
    found = _remembered(conn)
    if not found:
        return redirect(_home())
    event_id, entries = found
    checkin.check_in(conn, event_id, entries)
    return _show(conn, event_id, checkin.entries_by_id(conn, event_id, [e["registration_id"] for e in entries]))


@bp.post("/checkin/ready")
@bp.post("/kiosk/ready", endpoint="kiosk_ready")
def ready():
    from . import get_db
    check_csrf()
    conn = get_db()
    found = _remembered(conn)
    if not found:
        return redirect(_home())
    event_id, entries = found
    items = checkin.checklist(conn, current_app.config["LAPCOUNTER"].rc4_dir, entries)
    if not checkin.all_done(items):
        return _show(conn, event_id, entries, status=409,
                     message="Not quite yet. Finish the items without a check mark, then press Check again.")
    checkin.mark_ready(conn, event_id, entries)
    if _kiosk():
        session.pop(SESSION_KEY, None)
    return render_template("checkin_ready.html", name=checkin.first_name(entries), kiosk=_kiosk(),
                           home=_home())


@bp.post("/checkin/note")
@login_required
def save_note():
    from . import get_db
    check_csrf()
    checkin.set_note(get_db(), request.form.get("note", ""))
    back = request.form.get("event_id")
    return redirect(url_for("events.show", event_id=back) + "#checkin" if back else url_for("checkin.form"))
