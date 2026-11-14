"""Local admin: first-run setup, login/logout, CSRF.

The local admin is independent of RC Race HQ so the counter works with no
internet and no account (docs/design.md, First boot).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from functools import wraps

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from .. import db

bp = Blueprint("auth", __name__)
_hasher = PasswordHasher()

MIN_PASSWORD = 10
MAX_FIELD = 128
LOCKOUT_FAILURES = 5
LOCKOUT_WINDOW_S = 300

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


# --- CSRF --------------------------------------------------------------------

def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


def check_csrf() -> None:
    sent = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, "Form expired. Reload the page and try again.")


# --- session -----------------------------------------------------------------

def is_admin() -> bool:
    return session.get("admin") is True


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def _start_session() -> None:
    session.clear()  # new session id contents on privilege change
    session.permanent = True
    session["admin"] = True
    csrf_token()


def _safe_next(target: str | None) -> str:
    # Only same-site paths; never //host or scheme-relative redirects.
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("live.index")


# --- login throttling --------------------------------------------------------

def _locked_out(client: str, now: float) -> bool:
    with _failures_lock:
        recent = [t for t in _failures.get(client, []) if now - t < LOCKOUT_WINDOW_S]
        _failures[client] = recent
        return len(recent) >= LOCKOUT_FAILURES


def _record_failure(client: str, now: float) -> None:
    with _failures_lock:
        _failures.setdefault(client, []).append(now)


def reset_throttle() -> None:
    with _failures_lock:
        _failures.clear()


# --- views -------------------------------------------------------------------

def validate_new_admin(username: str, password: str, confirm: str) -> str | None:
    if not username or len(username) > MAX_FIELD or not username.isprintable():
        return "Pick a username (up to 128 characters)."
    if len(password) < MIN_PASSWORD:
        return f"Password needs at least {MIN_PASSWORD} characters."
    if len(password) > MAX_FIELD:
        return "Password is too long."
    if password != confirm:
        return "The two passwords don't match."
    return None


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    from . import get_db
    conn = get_db()
    if db.admin_exists(conn):
        return redirect(url_for("auth.login"))
    error = None
    username = ""
    if request.method == "POST":
        check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        error = validate_new_admin(username, password, request.form.get("confirm", ""))
        if error is None:
            if db.create_admin(conn, username, _hasher.hash(password)):
                _start_session()
                return redirect(url_for("live.index"))
            # Someone else finished setup first.
            return redirect(url_for("auth.login"))
    return render_template("setup.html", error=error, username=username), (400 if error else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    from . import get_db
    error = None
    status = 200
    if request.method == "POST":
        check_csrf()
        client = request.remote_addr or "?"
        now = time.monotonic()
        if _locked_out(client, now):
            return render_template("login.html", error="Too many tries. Wait a few minutes."), 429
        conn = get_db()
        admin = db.get_admin(conn)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")[:MAX_FIELD]
        ok = False
        if admin is not None and hmac.compare_digest(username, admin["username"]):
            try:
                ok = _hasher.verify(admin["pw_hash"], password)
                if ok and _hasher.check_needs_rehash(admin["pw_hash"]):
                    db.set_admin_hash(conn, _hasher.hash(password))
            except (VerificationError, InvalidHashError):
                ok = False
        if ok:
            _start_session()
            return redirect(_safe_next(request.args.get("next")))
        _record_failure(client, now)
        error, status = "Wrong username or password.", 401
    return render_template("login.html", error=error), status


@bp.post("/logout")
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("live.index"))
