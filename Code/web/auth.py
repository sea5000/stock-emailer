"""Password login, CSRF and brute-force throttling.

One shared password from the WEB_PASSWORD environment variable, compared with
hmac.compare_digest so the check does not leak length information through
timing. That is the whole auth model: a single-user tool on a home LAN, not a
multi-tenant service.
"""

import hmac
import logging
import secrets
import threading
import time

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

# A wrong password costs a sleep; a right one does not.
LOGIN_DELAY_SECONDS = 0.4
MAX_FAILURES = 8
LOCKOUT_SECONDS = 300


class _Attempts:
    """Failed logins per client IP.

    In-process on purpose: it only has to slow down someone hammering the
    login form on the LAN, and it must not need a write to the shared volume.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    def _prune(self, now):
        for ip in [k for k, (_, until) in self._data.items() if until <= now]:
            del self._data[ip]

    def locked_for(self, ip):
        with self._lock:
            now = time.time()
            self._prune(now)
            count, until = self._data.get(ip, (0, 0.0))
            if count >= MAX_FAILURES and until > now:
                return int(until - now)
            return 0

    def fail(self, ip):
        with self._lock:
            now = time.time()
            self._prune(now)
            count, _until = self._data.get(ip, (0, 0.0))
            self._data[ip] = (count + 1, now + LOCKOUT_SECONDS)

    def clear(self, ip):
        with self._lock:
            self._data.pop(ip, None)


_attempts = _Attempts()


def client_ip():
    # request.remote_addr is the real peer. X-Forwarded-For is deliberately not
    # trusted: anything out here could set it and defeat the throttle.
    return request.remote_addr or "unknown"


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def check_csrf():
    """Validate the token on a state-changing request."""
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf") or ""
    return bool(expected and sent and hmac.compare_digest(str(sent), str(expected)))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.config.get("WEB_PASSWORD"):
        # Fail closed rather than serving the ledger to the LAN unprotected.
        return (
            render_template(
                "error.html",
                code=500,
                message=(
                    "WEB_PASSWORD is not set, so the UI is locked. Set it in .env "
                    "and restart the web service."
                ),
            ),
            500,
        )

    nxt = request.args.get("next") or request.form.get("next") or url_for("views.positions")
    # Only ever redirect within this app - an open redirect would hand an
    # attacker a convincing phishing link.
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("views.positions")

    if request.method == "POST":
        ip = client_ip()
        locked = _attempts.locked_for(ip)
        if locked:
            flash(f"Too many failed attempts. Try again in {locked}s.", "error")
            return render_template("login.html", next=nxt, locked=locked), 429

        if not check_csrf():
            flash("Your session expired. Please try again.", "error")
            return render_template("login.html", next=nxt, locked=0), 400

        supplied = request.form.get("password", "")
        expected = current_app.config["WEB_PASSWORD"]
        ok = hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        if not ok:
            _attempts.fail(ip)
            time.sleep(LOGIN_DELAY_SECONDS)
            flash("Incorrect password.", "error")
            return render_template("login.html", next=nxt, locked=0), 401

        _attempts.clear(ip)
        session.clear()
        session["logged_in"] = True
        # New session id material, so a fixated pre-login cookie is worthless.
        session["_csrf"] = secrets.token_urlsafe(32)
        return redirect(nxt)

    return render_template("login.html", next=nxt, locked=_attempts.locked_for(client_ip()))


@bp.route("/logout", methods=["POST"])
def logout():
    if not check_csrf():
        return ("Bad CSRF token.", 400)
    session.clear()
    return redirect(url_for("auth.login"))
