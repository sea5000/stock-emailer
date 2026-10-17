"""Routes: positions, lots CRUD, CSV import, the template editor, preview, test send."""

import io
import logging
import os
import shutil
import tempfile
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

import email_utils
import lots_db
import sections
from web import auth, reports

log = logging.getLogger(__name__)

bp = Blueprint("views", __name__)

# Two shapes are accepted for a purchase date, and both are unambiguous.
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y")


@bp.before_request
def _guard():
    """Every route on this blueprint needs a session, and every POST a token.

    Checked here rather than with a per-view decorator so a route added later
    cannot forget it.
    """
    if not session.get("logged_in"):
        return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
    if request.method == "POST" and not auth.check_csrf():
        flash("Your session expired. Please try again.", "error")
        return redirect(request.referrer or url_for("views.positions"))
    return None


@bp.app_context_processor
def _globals():
    """Values every template needs: the CSRF token and the nav list."""
    return {
        "csrf": auth.csrf_token(),
        "nav": (
            ("positions", url_for("views.positions"), "Positions"),
            ("lots", url_for("views.lots"), "Lots"),
            ("editor", url_for("views.editor"), "Email"),
            ("import_csv", url_for("views.import_csv"), "Import"),
            ("settings", url_for("views.settings"), "Settings"),
        ),
        # The editor labels each block from the section registry, so adding a
        # section to sections.py is all it takes to show up in the UI.
        "section_labels": sections.SECTION_LABELS,
        "section_descriptions": sections.SECTION_DESCRIPTIONS,
    }


def _flash_problems(problems):
    for problem in problems:
        flash(problem, "error")


def _parse_date(raw):
    """Accept YYYY-MM-DD or MM/DD/YYYY, return the MM/DD/YYYY the DB stores.

    lots_db._norm_date is deliberately lenient - it passes an unparseable string
    straight through - which is right for the import path but wrong for a form,
    where a typo should be an error rather than a silently un-sortable lot.
    """
    text = (raw or "").strip()
    if not text:
        return None, "Purchase date is required."
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y"), None
        except ValueError:
            continue
    return None, f"Could not read '{text}' as a date. Use YYYY-MM-DD or MM/DD/YYYY."


def _lot_from_form(form):
    """Pull one lot out of a submitted form, or explain what is wrong."""
    symbol = (form.get("symbol") or "").strip().upper()
    if not symbol:
        return None, "Symbol is required."

    try:
        qty = float(form.get("qty") or "")
    except ValueError:
        return None, "Quantity must be a number."
    if qty <= 0:
        return None, "Quantity must be greater than zero."

    date_str, date_error = _parse_date(form.get("date_acquired"))
    if date_error:
        return None, date_error

    try:
        price = float(form.get("unit_price") or "")
    except ValueError:
        return None, "Price must be a number."
    if price < 0:
        return None, "Price cannot be negative."

    return {
        "symbol": symbol,
        "description": (form.get("description") or "").strip(),
        "qty": qty,
        "date": date_str,
        "price": price,
    }, None


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------


@bp.route("/")
@bp.route("/positions")
def positions():
    rows = lots_db.list_positions()
    return render_template(
        "positions.html",
        positions=rows,
        total_cost=sum(r["cost"] for r in rows),
        total_qty=sum(r["qty"] for r in rows),
        lots=lots_db.list_purchases(),
    )


@bp.route("/api/positions")
def api_positions():
    """JSON for the interactive table, so sorting and filtering need no reload."""
    rows = lots_db.list_positions()
    q = (request.args.get("q") or "").strip().lower()
    if q:
        rows = [
            r
            for r in rows
            if q in r["symbol"].lower() or q in (r["description"] or "").lower()
        ]

    allowed = ("symbol", "description", "qty", "cost", "avg_cost", "lots", "first_acquired")
    sort = request.args.get("sort") or "cost"
    if sort not in allowed:
        sort = "cost"
    reverse = (request.args.get("dir") or "desc") == "desc"
    rows.sort(key=lambda r: (r.get(sort) is None, _sort_key(r.get(sort), sort)), reverse=reverse)
    return {
        "positions": rows,
        "total_cost": round(sum(r["cost"] for r in rows), 2),
        "total_qty": round(sum(r["qty"] for r in rows), 4),
    }


def _sort_key(value, field):
    if field in ("qty", "cost", "avg_cost", "lots"):
        return float(value or 0)
    return str(value or "").lower()


# ---------------------------------------------------------------------------
# lots CRUD
# ---------------------------------------------------------------------------


@bp.route("/lots")
def lots():
    return render_template(
        "lots.html",
        lots=lots_db.list_purchases(),
        positions=lots_db.list_positions(),
        editing=lot_for_edit(),
    )


def lot_for_edit():
    """The lot named by ?lot=, so an edit form can be pre-filled."""
    raw = request.args.get("lot")
    if not raw or not raw.isdigit():
        return None
    return lots_db.get_purchase(int(raw))


@bp.route("/lots/add", methods=["POST"])
def add_lot():
    lot, error = _lot_from_form(request.form)
    if error:
        flash(error, "error")
        return redirect(url_for("views.lots"))
    added = lots_db.add_purchase(
        lot["symbol"], lot["qty"], lot["date"], lot["price"], lot["description"]
    )
    if added:
        flash(f"Added {lot['qty']:g} {lot['symbol']} bought {lot['date']}.", "ok")
    else:
        flash(
            f"An identical {lot['symbol']} lot already exists, so nothing was added.",
            "error",
        )
    return redirect(url_for("views.lots"))


@bp.route("/lots/<int:lot_id>/edit", methods=["POST"])
def edit_lot(lot_id):
    lot, error = _lot_from_form(request.form)
    if error:
        flash(error, "error")
        return redirect(url_for("views.lots"))
    updated, message = lots_db.update_purchase(
        lot_id,
        lot["symbol"],
        lot["qty"],
        lot["date"],
        lot["price"],
        lot["description"],
    )
    if not updated:
        flash(message or "Could not save that change.", "error")
    else:
        flash(f"Saved {lot['symbol']}.", "ok")
    return redirect(url_for("views.lots"))


@bp.route("/lots/<int:lot_id>/delete", methods=["POST"])
def delete_lot(lot_id):
    lot = lots_db.get_purchase(lot_id)
    if not lot:
        flash("That lot is already gone.", "error")
        return redirect(url_for("views.lots"))
    removed = lots_db.remove_purchase(lot_id)
    if removed:
        flash(f"Deleted {lot['symbol']} ({lot['qty']:g} bought {lot['date_acquired']}).", "ok")
    else:
        flash("Could not delete that lot.", "error")
    return redirect(url_for("views.lots"))


# ---------------------------------------------------------------------------
# CSV import: preview, then confirm
# ---------------------------------------------------------------------------


@bp.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "GET":
        return render_template("import.html", preview=None, staged=None)

    upload = request.files.get("csv")
    if not upload or not upload.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("views.import_csv"))

    # The uploaded name is user-supplied and the parser reads the whole file, so
    # it is never used as a path - only the suffix is, and only to reject things.
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in (".csv", ".txt"):
        flash("Only .csv files can be imported.", "error")
        return redirect(url_for("views.import_csv"))

    tmpdir = tempfile.mkdtemp(prefix="stockemailer-import-")
    tmp = Path(tmpdir) / f"upload{suffix}"
    upload.save(tmp)
    try:
        preview = lots_db.preview_import_csv(tmp)
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        log.exception("import preview failed")
        flash(f"Could not read that CSV: {exc}", "error")
        return redirect(url_for("views.import_csv"))

    # The staged copy has to outlive this request, because confirming is a second
    # POST that cannot re-upload the file.
    session["import_staged"] = str(tmp)
    return render_template("import.html", preview=preview, staged=tmp.name)


@bp.route("/import/confirm", methods=["POST"])
def import_confirm():
    staged = session.pop("import_staged", None)
    if not staged or not Path(staged).is_file():
        flash("That upload expired. Please choose the file again.", "error")
        return redirect(url_for("views.import_csv"))
    try:
        added = lots_db.import_csv(staged)
    except Exception as exc:
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("views.import_csv"))
    finally:
        shutil.rmtree(Path(staged).parent, ignore_errors=True)
    flash(f"Imported {added} new lot(s).", "ok")
    return redirect(url_for("views.lots"))


@bp.route("/import/cancel", methods=["POST"])
def import_cancel():
    staged = session.pop("import_staged", None)
    if staged:
        shutil.rmtree(Path(staged).parent, ignore_errors=True)
    flash("Import cancelled.", "ok")
    return redirect(url_for("views.import_csv"))


# ---------------------------------------------------------------------------
# template editor
# ---------------------------------------------------------------------------


def _setting(key, default=None):
    """Resolve an overridable setting the way the running system will.

    Goes through lots_db.runtime_setting so the settings page is not
    decorative: database first, then the environment, then `default`. Never
    raises, so a locked or corrupt settings table cannot break a preview.
    """
    return lots_db.runtime_setting(key, default)


def _dry_run():
    """True when a send should be reported rather than performed."""
    return str(_setting("DRY_RUN", "1")).lower() in ("1", "true", "yes")


def _store_from_form(base):
    """Overlay a posted editor form onto a store.

    A checkbox only submits when ticked, so presence in the form means enabled.
    The textarea is always submitted, whether or not the section is on, so
    unticking a section keeps the text - otherwise switching something off to
    try it out would throw the edit away.

    A request that carries none of the `html__` fields is not an editor
    submission at all, so nothing is overlaid. Without this, any POST that
    arrives without the textarea contents - a bare curl, a stale form, a
    half-finished page - reads as "every template is now an empty string" and
    renders a blank email, which is exactly the wrong thing to do to a real
    send. A template the user has genuinely emptied still submits as an empty
    string and is honoured.
    """
    if not any(f"html__{key}" in request.form for key in sections.SECTION_KEYS):
        return {
            "sections": {k: dict(c) for k, c in base["sections"].items()},
            "text": base.get("text", sections.DEFAULT_TEXT),
            "order": list(base["order"]),
        }

    store = {
        "sections": {k: dict(c) for k, c in base["sections"].items()},
        "text": base.get("text", sections.DEFAULT_TEXT),
        "order": list(base["order"]),
    }
    enabled = set(request.form.getlist("sections"))
    for key, cfg in store["sections"].items():
        if key not in sections.SECTION_KEYS:
            continue
        cfg["enabled"] = key in enabled
        posted = request.form.get(f"html__{key}")
        if posted is not None:
            cfg["html"] = posted
    if "text" in request.form:
        store["text"] = request.form["text"]
    order = [k for k in request.form.getlist("order") if k in sections.SECTION_KEYS]
    store["order"] = order + [k for k in sections.SECTION_KEYS if k not in order]
    return store


@bp.route("/email", methods=["GET", "POST"])
def editor():
    """Edit the section templates. POST saves.

    The whole store is posted as one form so the write is atomic: a half-saved
    email is worse than a rejected one.
    """
    saved = sections.load_store(lots_db)
    if request.method == "POST":
        store = _store_from_form(saved)
        problems = sections.validate(store)
        if problems and request.form.get("force") != "1":
            _flash_problems(problems)
            flash("Nothing was saved. Fix the template, or use Save anyway.", "error")
            return render_template("editor.html", store=store, problems=problems)
        sections.save_store(lots_db, store)
        if problems:
            _flash_problems(problems)
            flash(
                "Saved, but some templates still do not compile. Those sections "
                "will be left out of the email until they are fixed.",
                "error",
            )
        else:
            flash("Saved.", "ok")
        return redirect(url_for("views.editor"))

    return render_template(
        "editor.html",
        store=saved,
        problems=[],
        recipient=_setting("SES_RECIPIENT", "the configured recipient"),
    )


@bp.route("/email/reset/<key>", methods=["POST"])
def reset_template(key):
    if key == "text":
        sections.reset_text(lots_db)
        flash("Plain-text template reset to the original.", "ok")
    elif key == "all":
        sections.reset_all(lots_db)
        flash("Every template reset to the original.", "ok")
    elif key in sections.SECTION_KEYS:
        sections.reset_section(lots_db, key)
        flash(f"'{sections.SECTION_LABELS[key]}' reset to the original.", "ok")
    else:
        abort(404)
    return redirect(url_for("views.editor"))


# ---------------------------------------------------------------------------
# preview and test send
# ---------------------------------------------------------------------------


def _draft_store():
    """The stored store, or the posted one when previewing unsaved edits."""
    saved = sections.load_store(lots_db)
    if request.method == "POST":
        return _store_from_form(saved)
    return saved


@bp.route("/preview", methods=["GET", "POST"])
def preview():
    """Render the email in a sandboxed iframe.

    POST renders the editor's unsaved body, so what is previewed is what is on
    screen whether or not it has been saved.
    """
    ctx, _rebuilt = reports.cache.get()
    store = _draft_store()
    html, text, problems = reports.render(ctx, store)
    return render_template(
        "preview.html",
        html=html,
        text=text,
        problems=problems,
        recipient=_setting("SES_RECIPIENT", ""),
        dry_run=_dry_run(),
        rebuilt=_rebuilt,
    )


@bp.route("/preview/email.html")
def preview_html():
    """The raw rendered HTML, for opening outside the iframe."""
    ctx, _rebuilt = reports.cache.get()
    html, _text, _problems = reports.render(ctx, sections.load_store(lots_db))
    return current_app.response_class(html, mimetype="text/html")


@bp.route("/send-test", methods=["POST"])
def send_test():
    """Send the email that is on screen to the configured recipient.

    Honours DRY_RUN and deliberately does not write a history snapshot, so
    testing a template cannot overwrite tomorrow's 'previous checkup' baseline.
    """
    store = _store_from_form(sections.load_store(lots_db))
    # only_enabled: a section that is switched off is not going out, so a broken
    # template in it must not block the send.
    problems = sections.validate(store, only_enabled=True)
    if problems:
        _flash_problems(problems)
        flash("Not sent: the templates do not compile.", "error")
        # The flash is the only feedback a bare request gets, and a request with
        # no browser attached has none at all. Log it so "why did nothing
        # arrive" is answerable from the log rather than by guessing.
        log.info("test send refused: %d template problem(s): %s", len(problems), problems)
        return redirect(url_for("views.editor"))

    # force=True: a test send should reflect the lots as they are now, not a
    # context cached before the edit that prompted it.
    ctx, _rebuilt = reports.cache.get(force=True)
    parts = sections.render_parts(ctx, store)
    if not any(part.strip() for part in parts.values()):
        # Every section is either switched off or rendered nothing. Sending an
        # empty email looks like a broken report rather than a broken template,
        # so say what happened instead of mailing a blank page.
        log.info(
            "test send refused: no section rendered; enabled=%s",
            [k for k, c in store["sections"].items() if c.get("enabled", True)],
        )
        flash(
            "Not sent: no section produced any content. That usually means the "
            "editor form arrived without its templates, or every section is "
            "switched off. The saved templates were left untouched.",
            "error",
        )
        return redirect(url_for("views.editor"))
    html = sections.assemble(ctx, parts)
    text = sections.render_text(ctx, store)
    subject = (
        request.form.get("subject")
        or "TEST - Portfolio Checkup - " + date_cls.today().strftime("%A %d. %B %Y")
    )
    msg = email_utils.build_message(ctx, html=html, text=text, subject=subject)
    ok, detail = email_utils.deliver(msg, dry_run=_dry_run(), kind="test")
    flash(detail, "ok" if ok else "error")
    return redirect(url_for("views.editor"))


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

# Overridable per-install, stored in the settings table so the UI can change them
# without editing .env. AWS_KEY and AWS_SECRET are deliberately absent: a
# credential has no business round-tripping through a web form.
SETTING_KEYS = (
    "BENCHMARK",
    "SES_SENDER",
    "SES_RECIPIENT",
    "AWS_REGION",
    "CRON_TIME",
    "DRY_RUN",
)

_ENV_FOR_KEY = {
    "BENCHMARK": "BENCHMARK",
    "SES_SENDER": "SES_SENDER",
    "SES_RECIPIENT": "SES_RECIPIENT",
    "AWS_REGION": "AWS_REGION",
    "CRON_TIME": "CRON_TIME",
    "DRY_RUN": "DRY_RUN",
}


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        for key in SETTING_KEYS:
            value = (request.form.get(key) or "").strip()
            if value == "":
                lots_db.delete_setting(key)
            else:
                lots_db.set_setting(key, value)
        flash(
            "Settings saved. Benchmark, sender, recipient, region and DRY_RUN "
            "are read fresh each time a report is built, so they apply to the "
            "next preview or send. The cron schedule is only read at startup, "
            "so restart the services for that one.",
            "ok",
        )
        return redirect(url_for("views.settings"))

    return render_template(
        "settings.html",
        keys=SETTING_KEYS,
        current={k: lots_db.get_setting(k) for k in SETTING_KEYS},
        from_env={k: os.environ.get(_ENV_FOR_KEY[k], "") for k in SETTING_KEYS},
        db_path=lots_db.DB_PATH,
        data_dir=current_app.config["DATA_DIR"],
    )


@bp.route("/export/lots.csv")
def export_lots():
    """Download the ledger, so there is a way out that does not involve SQL."""
    tmpdir = tempfile.mkdtemp(prefix="stockemailer-export-")
    try:
        path = Path(tmpdir) / "lots.csv"
        lots_db.export_csv(path)
        data = path.read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name="lots.csv",
    )
