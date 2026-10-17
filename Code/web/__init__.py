"""Flask app for configuring the stock-emailer from a phone on the LAN.

Run with `gunicorn web:app` (see web_main.py). The web container shares the
/app/data bind mount with the cron container, so editing a lot here is picked up
by tomorrow's scheduled email.
"""

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template

log = logging.getLogger(__name__)

# Serve the templates and static files from the package rather than the cwd, so
# the app works whether it is started from /app or from the project checkout.
_HERE = Path(__file__).resolve().parent


def _secret_key(data_dir):
    """A stable session key, so logins survive a restart of the container.

    WEB_SECRET_KEY wins if set. Otherwise a key is generated once and kept in
    .web_secret next to lots.db, which also lets both containers share sessions.
    Failing that, an ephemeral key is better than refusing to start - it just
    means everyone is logged out after a restart.
    """
    env = os.environ.get("WEB_SECRET_KEY")
    if env and env.strip():
        return env.strip(), "WEB_SECRET_KEY"

    path = Path(data_dir) / ".web_secret"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing, str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        # Create with 0600 from the start; the file is as sensitive as a password.
        path.write_text(generated, encoding="utf-8")
        os.chmod(path, 0o600)
        log.info("wrote a new session key to %s", path)
        return generated, str(path)
    except OSError as exc:
        log.warning(
            "could not persist a session key (%s); sessions end on restart", exc
        )
        return secrets.token_hex(32), "ephemeral"


def create_app():
    app = Flask(
        __name__,
        template_folder=str(_HERE / "templates"),
        static_folder=str(_HERE / "static"),
    )

    data_dir = os.environ.get("DATA_DIR", "/app/data")
    key, key_source = _secret_key(data_dir)
    app.config.update(
        SECRET_KEY=key,
        SESSION_COOKIE_HTTPONLY=True,
        # Plain HTTP on the LAN, so the Secure flag would make the cookie
        # silently unusable. Set WEB_HTTPS=1 if this is ever put behind TLS.
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("WEB_HTTPS") == "1",
        # The preview and the editor are the only places templates reach the DOM,
        # and they do so through a sandboxed iframe / a textContent textarea.
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        DATA_DIR=data_dir,
        TEMPLATES_AUTO_RELOAD=False,
    )

    password = os.environ.get("WEB_PASSWORD", "")
    app.config["WEB_PASSWORD"] = password
    if not password:
        log.error(
            "WEB_PASSWORD is not set. The UI will refuse every login rather than "
            "run unauthenticated."
        )

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    @app.errorhandler(404)
    def _not_found(_e):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.template_filter("money")
    def _money(value, dp=2):
        """$1,234.56, or empty for a missing value.

        Deliberately not email_fmt.money: that one emits HTML (- for a
        blank) because it is building an email body, and this one is not.
        """
        if value is None or value == "":
            return ""
        try:
            return f"${float(value):,.{dp}f}"
        except (TypeError, ValueError):
            return ""

    @app.template_filter("num")
    def _num(value, _dp=4):
        """Trim trailing zeros: 12.5000 -> 12.5, but keep 12.0 as 12."""
        if value is None:
            return ""
        text = f"{float(value):.{_dp}f}".rstrip("0").rstrip(".")
        return text or "0"

    @app.template_filter("pct")
    def _pct(value, dp=2):
        if value is None:
            return ""
        return f"{float(value) * 100:,.{dp}f}%"

    @app.template_filter("iso_date")
    def _iso_date(value):
        """MM/DD/YYYY (what the ledger stores) -> YYYY-MM-DD, for <input type=date>.

        Anything unparseable is passed through empty rather than shown, so a
        malformed stored date cannot make the browser reject the whole form.
        """
        if not value:
            return ""
        text = str(value).strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""


    @app.errorhandler(500)

    def _server_error(e):
        # A preview that blows up should say so, not show a stack trace.
        log.exception("unhandled error: %s", e)
        return (
            render_template(
                "error.html",
                code=500,
                message="Something went wrong building that page. The log has the details.",
            ),
            500,
        )

    from web import auth, views

    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)

    log.info("session key from %s; data dir %s", key_source, data_dir)
    return app
