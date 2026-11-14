"""Flask web app: first-run admin setup, local login, live view, transponders,
events downloaded from RC Race HQ, running heats, settings (link to RC Race HQ).

    gunicorn -k gthread --threads 8 -b 0.0.0.0:8080 'lapcounter.web:create_app()'

The live stream is Server-Sent Events, so gunicorn must use threaded workers
(gthread); a sync worker would be tied up by one browser tab.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from flask import Flask, g, redirect, request, url_for

from .. import __version__, db
from ..config import Config

LOCAL_PROXIES = frozenset({"127.0.0.1", "::1"})


class TrustLocalProxy:
    """Use X-Forwarded-For only when the request came from this Pi.

    On the event Wi-Fi, phones reach the app through race-portal on port 80
    (http://race/), so every request arrives from 127.0.0.1. race-portal drops
    any X-Forwarded-For the client sent and sets exactly one, the real client
    address. Without this, the check-in throttle and the login log would see
    every phone as 127.0.0.1. A client that reaches :8080 directly (wired side)
    can't spoof its address: its REMOTE_ADDR isn't local, so the header is ignored.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded and environ.get("REMOTE_ADDR") in LOCAL_PROXIES:
            client = forwarded.split(",")[-1].strip()
            if client:
                environ["lapcounter.proxy_addr"] = environ["REMOTE_ADDR"]
                environ["REMOTE_ADDR"] = client
        return self.wsgi_app(environ, start_response)


# Endpoints reachable before an admin exists. Everything else redirects to setup,
# which is what the captive portal lands phones on during first boot.
OPEN_BEFORE_SETUP = frozenset({"auth.setup", "static", "health"})


def create_app(cfg: Config | None = None, *, testing: bool = False) -> Flask:
    cfg = cfg or Config.from_env()
    app = Flask(__name__)
    app.wsgi_app = TrustLocalProxy(app.wsgi_app)
    app.config["LAPCOUNTER"] = cfg

    boot = db.open_db(cfg.db_path)
    try:
        # Shared by every gunicorn worker and kept across restarts, so sessions survive both.
        secret = db.setdefault_setting(boot, "secret_key", secrets.token_hex(32))
    finally:
        boot.close()

    app.config.update(
        SECRET_KEY=secret,
        TESTING=testing,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=64 * 1024,
    )

    from . import auth, checkin, events, live, race, settings, transponders
    app.register_blueprint(auth.bp)
    app.register_blueprint(live.bp)
    app.register_blueprint(transponders.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(events.bp)
    app.register_blueprint(race.bp)
    app.register_blueprint(checkin.bp)

    @app.get("/healthz", endpoint="health")
    def health():
        get_db().execute("SELECT 1")
        return {"ok": True, "version": __version__}

    @app.before_request
    def require_setup():
        if request.endpoint in OPEN_BEFORE_SETUP:
            return None
        if not db.admin_exists(get_db()):
            return redirect(url_for("auth.setup"))
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'",
        )
        return resp

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def inject():
        from .auth import csrf_token, is_admin
        return {"csrf_token": csrf_token, "is_admin": is_admin(), "version": __version__}

    return app


def get_db():
    from flask import current_app
    if "db" not in g:
        g.db = db.connect(current_app.config["LAPCOUNTER"].db_path)
    return g.db
