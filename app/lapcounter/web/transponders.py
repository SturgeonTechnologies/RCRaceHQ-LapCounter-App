"""Admin → Transponders: list what's known, learn a new one, record it, delete it.

Flow (docs/learning.md): type the label number → Learn page polls
/api/learn/<id> while the decoder learns → Record renames the decoder's
`<auto_id>.rc4` to `<label>.rc4` and adds the transponder to the list.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from .. import db, eventsync, learning, syncd
from .auth import check_csrf, login_required
from .live import decoder_view

bp = Blueprint("transponders", __name__)


def _cfg():
    return current_app.config["LAPCOUNTER"]


def _session_or_404(conn, session_id: int) -> dict:
    session = db.get_learn_session(conn, session_id)
    if session is None:
        abort(404)
    return session


def session_view(conn, session: dict, now_ms: int) -> learning.View | None:
    if session["state"] != "open":
        return None
    since = session["started_at_ms"]
    return learning.describe(
        session,
        db.learn_events_since(conn, since),
        db.passing_seen_since(conn, session["label"], since),
        now_ms,
        db.last_learn_event_before(conn, since),
    )


def session_json(conn, session: dict) -> dict:
    now = db.now_ms()
    view = session_view(conn, session, now)
    decoder = decoder_view(db.decoder_status(conn), _cfg().decoder_stale_s, now)
    out = {
        "id": session["id"],
        "label": session["label"],
        "racer_name": session["racer_name"],
        "closed": view is None,
        "result": session["state"],
        "decoder": {k: decoder.get(k) for k in ("online", "seen", "noise", "received", "processed")},
    }
    if view is not None:
        out.update(state=view.state, message=view.message, elapsed_s=view.elapsed_s,
                   progress=view.progress, auto_id=view.auto_id, payloads=view.payloads,
                   can_record=view.can_record)
    return out


@bp.get("/transponders")
@login_required
def index():
    from . import get_db
    conn = get_db()
    rc4_dir = _cfg().rc4_dir
    listed = db.list_transponders(conn)
    listed_numbers = {t["number"] for t in listed}
    files = learning.learned_numbers(rc4_dir)
    return render_template(
        "transponders.html",
        transponders=listed,
        has_file=files,
        unlisted_files=sorted(files - listed_numbers),
        strays=learning.stray_auto_files(rc4_dir),
        library=syncd.library_status(conn),
        current=db.current_learn_session(conn),
        error=request.args.get("error"),
        deleted=request.args.get("deleted", type=int),
        to_learn=eventsync.to_learn_by_event(conn, rc4_dir),
        has_events=bool(db.list_events(conn)),
        notice=request.args.get("notice"),
        # Pre-filled from an event's "Transponders to learn" list.
        prefill_label=request.args.get("label", ""),
        prefill_name=request.args.get("racer_name", ""),
    )


@bp.post("/transponders/learn")
@login_required
def start():
    from . import get_db
    check_csrf()
    conn = get_db()
    try:
        label = learning.validate_label(request.form.get("label", ""))
        name = learning.validate_name(request.form.get("racer_name", ""))
    except learning.LearnError as exc:
        return redirect(url_for("transponders.index", error=str(exc)))
    if db.active_heat_run(conn) is not None:
        return redirect(url_for(
            "transponders.index",
            error="A heat is on track. Learn between heats, once it has finished."))
    if label in learning.learned_numbers(_cfg().rc4_dir):
        return redirect(url_for(
            "transponders.index",
            error=f"{label} is already learned. Delete it below to learn it again."))
    session_id = db.open_learn_session(conn, label, name)
    return redirect(url_for("transponders.learn", session_id=session_id))


@bp.get("/transponders/learn/<int:session_id>")
@login_required
def learn(session_id: int):
    from . import get_db
    conn = get_db()
    session = _session_or_404(conn, session_id)
    return render_template("learn.html", s=session_json(conn, session),
                           error=request.args.get("error"))


@bp.get("/api/learn/<int:session_id>")
@login_required
def learn_json(session_id: int):
    from . import get_db
    conn = get_db()
    resp = current_app.make_response(session_json(conn, _session_or_404(conn, session_id)))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.post("/transponders/learn/<int:session_id>/record")
@login_required
def record(session_id: int):
    from . import get_db
    check_csrf()
    conn = get_db()
    session = _session_or_404(conn, session_id)
    here = url_for("transponders.learn", session_id=session_id)
    view = session_view(conn, session, db.now_ms())
    if view is None:
        return redirect(here)
    if not view.can_record:
        return redirect(url_for("transponders.learn", session_id=session_id,
                                error="Nothing to record yet."))

    rc4_dir, label = _cfg().rc4_dir, session["label"]
    try:
        if view.state == learning.READY:
            path = learning.rename_learned(rc4_dir, view.auto_id, label)
        else:
            path = learning.rc4_path(rc4_dir, label)
        if path.exists():
            kind, digest = "rc4", learning.sha256_of(path)
        else:
            # RC3 / Hybrid: the transponder sends its own number, there is no file.
            kind, digest = "direct", None
    except (learning.LearnError, OSError) as exc:
        return redirect(url_for("transponders.learn", session_id=session_id, error=str(exc)))

    db.upsert_transponder(conn, number=label, kind=kind, racer_name=session["racer_name"],
                          rc4_sha256=digest, source="learned")
    db.close_learn_session(conn, session_id, "recorded", auto_id=view.auto_id,
                           payloads=view.payloads)
    current_app.logger.info("recorded transponder %s (%s, auto id %s)", label, kind, view.auto_id)
    return redirect(here)


@bp.post("/transponders/learn/<int:session_id>/cancel")
@login_required
def cancel(session_id: int):
    from . import get_db
    check_csrf()
    conn = get_db()
    _session_or_404(conn, session_id)
    db.close_learn_session(conn, session_id, "cancelled")
    return redirect(url_for("transponders.index"))


@bp.post("/transponders/notify/<event_id>")
@login_required
def notify(event_id: str):
    """"Notify racers we are missing their transponder for the race".

    Records a request per racer still on the to-learn list for that event.
    Nothing is sent yet: texting goes through RC Race HQ once results sync
    does (docs/learning.md#missing-transponders).
    """
    from . import get_db
    check_csrf()
    conn = get_db()
    ev = db.get_event_pack(conn, event_id)
    if ev is None:
        abort(404)
    rows = eventsync.to_learn(conn, _cfg().rc4_dir, event_id)
    if not rows:
        return redirect(url_for("transponders.index",
                                notice=f"Every racer in {ev['title']} has a recorded transponder.")
                        + "#to-learn")
    n = db.queue_transponder_notices(conn, event_id, rows)
    current_app.logger.info("queued %d missing-transponder notices for %s", n, event_id)
    racers = "racer" if n == 1 else "racers"
    return redirect(url_for(
        "transponders.index",
        notice=f"Noted {n} {racers} in {ev['title']} to notify. "
               "Texts aren't sent yet; that comes with RC Race HQ sync.") + "#to-learn")


@bp.post("/transponders/<int:number>/delete")
@login_required
def delete(number: int):
    """Forget a transponder: its `.rc4` file (if any) and its row in the list.

    Also takes the decoder's own 1000-9999 numbers, so a stray file nobody
    recorded can be cleared from the page.
    """
    from . import get_db
    check_csrf()
    conn = get_db()
    try:
        had_file = learning.delete_learned(_cfg().rc4_dir, number)
    except learning.LearnError as exc:
        return redirect(url_for("transponders.index", error=str(exc)))
    had_row = db.delete_transponder(conn, number)
    if not (had_file or had_row):
        return redirect(url_for("transponders.index", error=f"{number} isn't known."))
    current_app.logger.info("deleted transponder %s (file=%s, listed=%s)", number, had_file, had_row)
    return redirect(url_for("transponders.index", deleted=number))
