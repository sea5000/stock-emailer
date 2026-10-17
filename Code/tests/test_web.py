"""Auth, CSRF, and the routes.

The preview and test-send paths are the ones with real risk - a preview that
writes a history snapshot would quietly corrupt tomorrow's "previous checkup"
baseline - so those are asserted explicitly rather than assumed.
"""

import re
import time

import pytest

import email_utils
import lots_db
import sections
from conftest import csrf_for
from web import reports

PROTECTED_GETS = [
    "/",
    "/positions",
    "/lots",
    "/email",
    "/import",
    "/settings",
    "/api/positions",
    "/preview",
    "/preview/email.html",
    "/export/lots.csv",
]

PROTECTED_POSTS = [
    ("/lots/add", {}),
    ("/import/confirm", {}),
    ("/import/cancel", {}),
    ("/send-test", {}),
    ("/settings", {}),
    ("/email", {}),
]


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_anonymous_visitors_are_redirected(client, path):
    resp = client.get(path)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_anonymous_visitors_see_no_data(client, path):
    body = client.get(path).get_data(as_text=True)
    assert "Portfolio Checkup" not in body
    assert "cost basis" not in body.lower()


@pytest.mark.parametrize("path,data", PROTECTED_POSTS)
def test_anonymous_posts_are_redirected(client, path, data):
    resp = client.post(path, data=data)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_logout_without_a_token_is_rejected(client):
    """Logout is on the auth blueprint, so it checks the token itself."""
    assert client.post("/logout").status_code == 400


def test_login_flow(client):
    assert "Password" in client.get("/login").get_data(as_text=True)

    # no token
    assert client.post("/login", data={"password": "test-password"}).status_code == 400

    token = re.search(r'name="_csrf" value="([^"]+)"', client.get("/login").get_data(as_text=True)).group(1)
    assert client.post("/login", data={"password": "wrong", "_csrf": token}).status_code == 401
    assert client.post(
        "/login", data={"password": "test-password", "_csrf": token, "next": "/lots"}
    ).status_code == 302
    assert client.get("/lots").status_code == 200


def test_login_redirects_only_within_the_app(client):
    token = re.search(r'name="_csrf" value="([^"]+)"', client.get("/login").get_data(as_text=True)).group(1)
    resp = client.post(
        "/login",
        data={"password": "test-password", "_csrf": token, "next": "//evil.example.com/"},
    )
    assert resp.status_code == 302
    assert not resp.headers["Location"].startswith("//")


def test_failed_logins_are_throttled(client, monkeypatch):
    monkeypatch.setattr("web.auth.LOGIN_DELAY_SECONDS", 0)
    token = re.search(r'name="_csrf" value="([^"]+)"', client.get("/login").get_data(as_text=True)).group(1)
    codes = [
        client.post("/login", data={"password": "nope", "_csrf": token}).status_code
        for _ in range(10)
    ]
    assert 429 in codes, "repeated wrong passwords should lock the client out"
    # and the correct password is refused while locked out
    assert client.post(
        "/login", data={"password": "test-password", "_csrf": token}
    ).status_code == 429


def test_a_missing_password_locks_the_ui_out(sandbox, monkeypatch):
    from web import create_app

    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    app = create_app()
    resp = app.test_client().get("/login")
    assert resp.status_code == 500
    assert "WEB_PASSWORD" in resp.get_data(as_text=True)


def test_logout_ends_the_session(logged_in):
    assert logged_in.get("/lots").status_code == 200
    logged_in.post("/logout", data={"_csrf": csrf_for(logged_in)})
    resp = logged_in.get("/lots")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_post_without_csrf_writes_nothing(logged_in):
    before = len(lots_db.list_purchases())
    logged_in.post(
        "/lots/add",
        data={"symbol": "NOCSRF", "qty": "1", "date_acquired": "2026-01-01", "unit_price": "1"},
    )
    assert len(lots_db.list_purchases()) == before


def test_pages_render(logged_in):
    for path, marker in [
        ("/positions", "Positions"),
        ("/lots", "Add a lot"),
        ("/email", "Email"),
        ("/import", "Choose a CSV"),
        ("/settings", "BENCHMARK"),
    ]:
        body = logged_in.get(path).get_data(as_text=True)
        assert body, path
        assert marker in body, f"{path} should mention {marker!r}"


def test_404(logged_in):
    resp = logged_in.get("/no-such-page")
    assert resp.status_code == 404
    assert "not found" in resp.get_data(as_text=True).lower()


# ---------------------------------------------------------------- the editor


def _editor_form(client, **overrides):
    store = sections.load_store(lots_db)
    form = {"_csrf": csrf_for(client, "/email"), "text": store["text"]}
    for key in store["order"]:
        form.setdefault("sections", []).append(key)
        form[f"html__{key}"] = store["sections"][key]["html"]
    form.update(overrides)
    return form


def test_editor_saves_templates(sandbox, logged_in):
    form = _editor_form(logged_in, **{"html__footer": "<p>EDITED</p>"})
    assert logged_in.post("/email", data=form).status_code == 302
    assert "<p>EDITED</p>" in sections.load_store(lots_db)["sections"]["footer"]["html"]


def test_unticking_a_section_keeps_its_html(sandbox, logged_in):
    """Switching a section off to try something out must not throw the edit away."""
    store = sections.load_store(lots_db)
    form = _editor_form(logged_in, **{"html__footer": "<p>KEEP ME</p>"})
    form["sections"] = [k for k in store["order"] if k != "footer"]
    logged_in.post("/email", data=form)
    saved = sections.load_store(lots_db)
    assert saved["sections"]["footer"]["enabled"] is False
    assert saved["sections"]["footer"]["html"] == "<p>KEEP ME</p>"


def test_a_broken_template_is_not_saved(sandbox, logged_in):
    form = _editor_form(logged_in, **{"html__footer": "{% for x in %}"})
    resp = logged_in.post("/email", data=form)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, "the editor should come back with the error"
    assert "Nothing was saved" in body
    assert "Expected an expression" in body, "the actual Jinja error should be shown"
    assert "{% for x in %}" not in sections.load_store(lots_db)["sections"]["footer"]["html"]


def test_save_anyway_stores_a_broken_template(sandbox, logged_in):
    form = _editor_form(logged_in, force="1", **{"html__footer": "{% for x in %}"})
    assert logged_in.post("/email", data=form).status_code == 302
    assert "{% for x in %}" in sections.load_store(lots_db)["sections"]["footer"]["html"]


def test_resetting_templates(sandbox, logged_in):
    store = sections.load_store(lots_db)
    store["sections"]["footer"]["html"] = "<p>X</p>"
    store["text"] = "custom"
    sections.save_store(lots_db, store)

    logged_in.post("/email/reset/text", data={"_csrf": csrf_for(logged_in, "/email")})
    assert sections.load_store(lots_db)["text"] == sections.DEFAULT_TEXT

    logged_in.post("/email/reset/all", data={"_csrf": csrf_for(logged_in, "/email")})
    assert sections.load_store(lots_db) == sections.default_store()


def test_resetting_an_unknown_template_is_a_404(logged_in):
    resp = logged_in.post("/email/reset/nope", data={"_csrf": csrf_for(logged_in, "/email")})
    assert resp.status_code == 404


def test_reordering_sections(sandbox, logged_in):
    store = sections.load_store(lots_db)
    flipped = list(reversed(store["order"]))
    form = _editor_form(logged_in)
    form["order"] = flipped
    logged_in.post("/email", data=form)
    assert sections.load_store(lots_db)["order"] == flipped


# --------------------------------------------------- preview and test send


@pytest.fixture
def offline_preview(app, fake_context, monkeypatch):
    """Serve a synthetic context so these tests never touch the network."""
    monkeypatch.setattr(reports.cache, "get", lambda **kw: (fake_context, True))
    return fake_context


def test_preview_renders_the_email(logged_in, offline_preview):
    resp = logged_in.get("/preview")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Portfolio Value" in body
    assert 'sandbox=""' in body, "the email must be rendered in a locked-down frame"
    assert "allow-scripts" not in body and "allow-same-origin" not in body, (
        "the frame holding the email must grant the browser nothing"
    )


def test_preview_embeds_the_chart_inline(logged_in, offline_preview, sandbox, fake_context):
    """The sandboxed frame cannot fetch, so the chart has to travel in the HTML."""
    import base64

    png = sandbox / "chart.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    fake_context["chart_src"] = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    body = logged_in.get("/preview").get_data(as_text=True)
    assert "data:image/png;base64," in body


def test_raw_html_route(logged_in, offline_preview):
    resp = logged_in.get("/preview/email.html")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert "Portfolio Checkup" in resp.get_data(as_text=True)


def test_test_send_honours_dry_run(logged_in, offline_preview, monkeypatch):
    lots_db.delete_setting("DRY_RUN")
    monkeypatch.setenv("DRY_RUN", "1")
    sent = []

    def fake_deliver(msg, dry_run=False, kind="test"):
        sent.append((msg, dry_run))
        return True, "DRY_RUN is set, so nothing was actually sent."

    monkeypatch.setattr("email_utils.deliver", fake_deliver)
    form = _editor_form(logged_in, subject="TEST SUBJECT")
    resp = logged_in.post("/send-test", data=form, follow_redirects=True)
    assert resp.status_code == 200
    assert "nothing was actually sent" in resp.get_data(as_text=True)
    assert len(sent) == 1
    assert sent[0][0]["Subject"] == "TEST SUBJECT"
    assert sent[0][1] is True, "DRY_RUN must be passed through to deliver()"


def test_a_settings_page_dry_run_stops_a_real_send(logged_in, offline_preview, monkeypatch):
    """The whole point of the override: DRY_RUN=1 in the UI must not send.

    This reads the database via runtime_setting, so it fails if the view goes
    back to os.environ and quietly ignores what the user just typed.
    """
    monkeypatch.setenv("DRY_RUN", "0")  # the environment says "really send"
    lots_db.set_setting("DRY_RUN", "1")  # the settings page says "do not"
    sent = []
    monkeypatch.setattr(
        "email_utils.deliver",
        lambda msg, dry_run=False, kind="test": (sent.append(dry_run), (True, "ok"))[1],
    )
    form = _editor_form(logged_in, subject="TEST SUBJECT")
    resp = logged_in.post("/send-test", data=form, follow_redirects=True)
    assert resp.status_code == 200
    assert sent == [True], "the settings page DRY_RUN override was ignored"


def test_a_bare_post_cannot_blank_the_email(logged_in, offline_preview, monkeypatch):
    """A request with no editor fields must not be read as "empty templates".

    A bare curl or a half-loaded form used to render a blank email and still
    send it. The stored templates are the fallback.
    """
    bodies = []
    real_build = email_utils.build_message

    def spy(context, html=None, text=None, subject=None):
        bodies.append(html)
        return real_build(context, html=html, text=text, subject=subject)

    monkeypatch.setattr("email_utils.build_message", spy)
    monkeypatch.setattr("email_utils.deliver", lambda msg, dry_run=False, kind="test": (True, "sent"))
    logged_in.post("/send-test", data={"_csrf": csrf_for(logged_in, "/email")})
    assert len(bodies) == 1, "the request should have gone through with the saved store"
    assert "Portfolio Value" in bodies[0], "the saved templates were not used"
    assert bodies[0].count("<tr") > 5, "the email looks empty"


def test_an_all_empty_editor_is_refused_rather_than_sent(logged_in, offline_preview, monkeypatch):
    """Sending a blank email looks like a broken report, so refuse and explain."""
    sent = []
    monkeypatch.setattr(
        "email_utils.deliver",
        lambda msg, dry_run=False, kind="test": (sent.append(msg), (True, "sent"))[1],
    )
    form = {f"html__{k}": "" for k in sections.SECTION_KEYS}
    form["_csrf"] = csrf_for(logged_in, "/email")
    resp = logged_in.post("/send-test", data=form, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Not sent" in body
    assert "no section produced any content" in body
    assert not sent, "an empty email must not be sent"


def test_test_send_refuses_a_broken_template(logged_in, offline_preview, monkeypatch):
    called = []
    monkeypatch.setattr(
        "email_utils.deliver", lambda *a, **k: (called.append(1), (True, "sent"))[1]
    )
    form = _editor_form(logged_in, **{"html__footer": "{% for x in %}"})
    resp = logged_in.post("/send-test", data=form, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Not sent" in body
    assert "Expected an expression" in body
    assert not called, "nothing should be handed to SES"


def test_test_send_ignores_a_broken_section_that_is_switched_off(logged_in, offline_preview):

    store = sections.load_store(lots_db)
    form = _editor_form(logged_in, **{"html__footer": "{% for x in %}"})
    form["sections"] = [k for k in store["order"] if k != "footer"]
    resp = logged_in.post("/send-test", data=form, follow_redirects=True)
    assert "Not sent" not in resp.get_data(as_text=True)


def test_a_preview_writes_nothing(logged_in, offline_preview, sandbox):
    """The whole point: previewing must not disturb the scheduled email."""
    import os

    history = set(os.listdir(sandbox / "history"))
    before = {p.name for p in sandbox.iterdir()}

    logged_in.get("/preview")
    logged_in.post("/send-test", data=_editor_form(logged_in))

    assert set(os.listdir(sandbox / "history")) == history, "no history snapshot"
    after = {p.name for p in sandbox.iterdir()} - before
    assert not [f for f in after if not f.endswith(("-wal", "-shm"))], after
    assert not (sandbox / "preview.html").exists(), "no stray DRY_RUN preview file"
    assert not (sandbox / "dashboard.html").exists(), "the dashboard was not rewritten"


# -------------------------------------------------------------- settings


def test_settings_save_and_clear(sandbox, logged_in):
    resp = logged_in.post(
        "/settings",
        data={"_csrf": csrf_for(logged_in, "/settings"), "BENCHMARK": "QQQ", "DRY_RUN": ""},
    )
    assert resp.status_code == 302
    assert lots_db.get_setting("BENCHMARK") == "QQQ"
    assert lots_db.get_setting("DRY_RUN") is None, "a blank value clears the override"

    logged_in.post(
        "/settings",
        data={"_csrf": csrf_for(logged_in, "/settings"), "BENCHMARK": ""},
    )
    assert lots_db.get_setting("BENCHMARK") is None


def test_settings_never_expose_aws_credentials(sandbox, logged_in, monkeypatch):
    monkeypatch.setenv("AWS_KEY", "AKIA-SECRET")
    monkeypatch.setenv("AWS_SECRET", "super-secret")
    body = logged_in.get("/settings").get_data(as_text=True)
    assert "super-secret" not in body
    assert "AKIA-SECRET" not in body
    assert "AWS_KEY" not in body


# ---------------------------------------------------------------------------
# the report cache has to actually invalidate
# ---------------------------------------------------------------------------


@pytest.fixture
def counting_build(app, fake_context, monkeypatch):
    """Run the real cache logic, but build synthetically.

    `offline_preview` stubs out cache.get entirely, so it cannot catch a cache
    that never invalidates - the bug is in the code it replaces. This patches
    only the expensive part, _build, and counts how often it is called.
    """
    calls = []

    # The sandbox copies the real lots.db, so a BENCHMARK override the user
    # typed into their own settings page would leak in and make these tests
    # depend on whatever the last person did in the UI.
    lots_db.delete_setting("BENCHMARK")

    def fake_build(today, benchmark):
        calls.append((today, benchmark))
        ctx = dict(fake_context)
        ctx["today"] = today
        ctx["benchmark"] = benchmark
        ctx["_tmpdir"] = None
        return ctx

    cache = reports.cache
    monkeypatch.setattr(cache, "_build", fake_build)
    monkeypatch.setattr(cache, "_ctx", None, raising=False)
    monkeypatch.setattr(cache, "_key", None, raising=False)
    monkeypatch.setattr(cache, "_built_at", 0.0, raising=False)
    monkeypatch.setattr(cache, "_building", False, raising=False)
    return calls


def settle(logged_in, calls, limit=6):
    """Request until the cache key stops moving.

    The first request after the database is copied can touch lots.db - closing
    the first connection checkpoints the WAL back into the main file, which
    bumps its mtime - and that legitimately changes the key and forces one extra
    build. It is a one-time cost per fresh data directory rather than a cache
    failure, so the tests wait it out instead of asserting on it.
    """
    previous = -1
    for _ in range(limit):
        logged_in.get("/preview/email.html")
        if len(calls) == previous:
            return
        previous = len(calls)
    raise AssertionError("the cache never settled; the key is still moving")


def test_a_repeat_request_reuses_the_build(sandbox, logged_in, counting_build):
    settle(logged_in, counting_build)
    before = len(counting_build)
    logged_in.get("/preview/email.html")
    logged_in.get("/preview/email.html")
    logged_in.get("/preview/email.html")
    assert len(counting_build) == before, (
        f"three more requests cost {len(counting_build) - before} extra yfinance calls"
    )


def test_changing_the_benchmark_rebuilds_the_report(sandbox, logged_in, counting_build):
    """The settings page can change the benchmark, so it has to invalidate."""
    settle(logged_in, counting_build)
    before = len(counting_build)
    lots_db.set_setting("BENCHMARK", "QQQ")
    logged_in.get("/preview/email.html")
    assert len(counting_build) > before, "a stale report was served after a settings change"
    assert counting_build[-1][1] == "QQQ", "the override never reached the builder"
    lots_db.delete_setting("BENCHMARK")


def test_editing_a_lot_rebuilds_the_report(sandbox, logged_in, counting_build):
    """The ledger is part of the key precisely so an edit shows up at once."""
    settle(logged_in, counting_build)
    before = len(counting_build)
    lots_db.add_purchase("CACHETEST", 10, "2026-05-06", 5.0, "Cache Co")
    logged_in.get("/preview/email.html")
    assert len(counting_build) > before, "a stale report was served after a lot was added"


def test_the_cache_expires_instead_of_being_frozen(
    sandbox, logged_in, counting_build, monkeypatch
):
    """After the TTL the prices should be refetched, not pinned until restart."""
    settle(logged_in, counting_build)
    before = len(counting_build)
    monkeypatch.setattr(reports, "CACHE_TTL_SECONDS", -1)
    logged_in.get("/preview/email.html")
    assert len(counting_build) > before, "an expired cache kept serving the first build"


def test_a_failed_build_does_not_wedge_the_cache(sandbox, logged_in, counting_build, monkeypatch):
    """One bad request must not leave every later request thinking a build is running."""
    logged_in.get("/preview/email.html")
    assert len(counting_build) == 1

    def explode(today, benchmark):
        raise RuntimeError("yfinance is down")

    settle(logged_in, counting_build)
    before = len(counting_build)
    working_build = reports.cache._build
    # Expire the TTL too, otherwise the fresh cache answers without ever
    # calling _build and the failure never happens.
    monkeypatch.setattr(reports, "CACHE_TTL_SECONDS", -1)
    monkeypatch.setattr(reports.cache, "_build", explode)
    with pytest.raises(RuntimeError):
        logged_in.get("/preview/email.html")
    monkeypatch.setattr(reports.cache, "_build", working_build)
    logged_in.get("/preview/email.html")
    assert len(counting_build) > before, "the cache stayed wedged after a failed build"


def test_a_request_during_a_build_gets_the_previous_report(
    sandbox, logged_in, counting_build, monkeypatch
):
    """While one request rebuilds, the next gets the old answer immediately
    rather than blocking on a second yfinance round trip."""
    settle(logged_in, counting_build)
    before = len(counting_build)
    monkeypatch.setattr(reports.cache, "_building", True, raising=False)
    started = time.monotonic()
    logged_in.get("/preview/email.html")
    assert time.monotonic() - started < 0.5, "it waited for a build it could have skipped"
    assert len(counting_build) == before, "it started a competing build"
