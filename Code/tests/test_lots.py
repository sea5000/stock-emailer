"""Lots CRUD and CSV import, exercised through the web routes where possible.

The point of testing the routes rather than lots_db directly is that the parsing
and validation that matters lives in the form-handling layer, and a lot silently
dropped because a date was typed wrong is exactly the bug worth catching.
"""

import io
import sqlite3

import pytest

import lots_db
from conftest import csrf_for


def test_add_edit_delete_a_lot(sandbox, logged_in):
    resp = logged_in.post(
        "/lots/add",
        data={
            "_csrf": csrf_for(logged_in),
            "symbol": "xyz",
            "qty": "3",
            "date_acquired": "2026-01-02",
            "unit_price": "10.5",
            "description": "Test Co",
        },
    )
    assert resp.status_code == 302

    lots = [lot for lot in lots_db.list_purchases() if lot["symbol"] == "XYZ"]
    assert len(lots) == 1, "symbol should be upper-cased"
    assert lots[0]["qty"] == 3.0
    assert lots[0]["unit_price"] == 10.5
    assert lots[0]["description"] == "Test Co"

    lot_id = lots[0]["id"]
    resp = logged_in.post(
        f"/lots/{lot_id}/edit",
        data={
            "_csrf": csrf_for(logged_in),
            "symbol": "XYZ",
            "qty": "4",
            "date_acquired": "01/02/2026",
            "unit_price": "11",
            "description": "Renamed",
        },
    )
    assert resp.status_code == 302
    updated = lots_db.get_purchase(lot_id)
    assert updated["qty"] == 4.0
    assert updated["unit_price"] == 11.0
    assert updated["description"] == "Renamed"

    resp = logged_in.post(f"/lots/{lot_id}/delete", data={"_csrf": csrf_for(logged_in)})
    assert resp.status_code == 302
    assert lots_db.get_purchase(lot_id) is None


def test_an_identical_lot_is_not_added_twice(sandbox, logged_in):
    form = {
        "_csrf": csrf_for(logged_in),
        "symbol": "DUP",
        "qty": "1",
        "date_acquired": "2026-01-02",
        "unit_price": "5",
        "description": "",
    }
    logged_in.post("/lots/add", data=form)
    form["_csrf"] = csrf_for(logged_in)
    resp = logged_in.post("/lots/add", data=form, follow_redirects=True)
    assert "already exists" in resp.get_data(as_text=True)
    assert len([lot for lot in lots_db.list_purchases() if lot["symbol"] == "DUP"]) == 1


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("symbol", "", "Symbol is required"),
        ("qty", "abc", "Quantity must be a number"),
        ("qty", "-1", "greater than zero"),
        ("qty", "0", "greater than zero"),
        ("date_acquired", "not-a-date", "Could not read"),
        ("date_acquired", "", "date is required"),
        ("unit_price", "abc", "Price must be a number"),
        ("unit_price", "-5", "cannot be negative"),
    ],
)
def test_bad_input_is_rejected_with_a_readable_reason(sandbox, logged_in, field, value, message):
    form = {
        "_csrf": csrf_for(logged_in),
        "symbol": "AAA",
        "qty": "1",
        "date_acquired": "2026-01-02",
        "unit_price": "1",
        "description": "",
    }
    form[field] = value
    resp = logged_in.post("/lots/add", data=form, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert message in body, f"expected {message!r} in the page"
    assert not [lot for lot in lots_db.list_purchases() if lot["description"] == "SHOULD-NOT-EXIST"]


def test_both_date_formats_are_accepted(sandbox, logged_in):
    for i, raw in enumerate(["2026-03-04", "03/04/2026"]):
        logged_in.post(
            "/lots/add",
            data={
                "_csrf": csrf_for(logged_in),
                "symbol": f"D{i}",
                "qty": "1",
                "date_acquired": raw,
                "unit_price": "1",
                "description": "",
            },
        )
    dates = {lot["date_acquired"] for lot in lots_db.list_purchases() if lot["symbol"] in ("D0", "D1")}
    assert dates == {"03/04/2026"}


def test_edit_form_is_prefilled(sandbox, logged_in):
    # add_purchase returns a rowcount, not an id, so look the row up by symbol
    # rather than feeding the return value to get_purchase - that would silently
    # return whichever lot happens to have that id.
    lots_db.add_purchase("PREF", 2, "2026-05-06", 7.5, "Prefilled Co")
    lot = next(x for x in lots_db.list_purchases() if x["symbol"] == "PREF")
    body = logged_in.get(f"/lots?lot={lot['id']}").get_data(as_text=True)
    assert 'value="PREF"' in body
    assert 'value="Prefilled Co"' in body
    assert 'value="2026-05-06"' in body, "date input needs ISO, the ledger stores MM/DD/YYYY"
    assert 'value="7.5"' in body


def test_positions_page_and_api(sandbox, logged_in):
    body = logged_in.get("/positions").get_data(as_text=True)
    assert "Positions" in body

    payload = logged_in.get("/api/positions?sort=cost&dir=desc").get_json()
    assert payload["positions"]
    costs = [row["cost"] for row in payload["positions"]]
    assert costs == sorted(costs, reverse=True)

    filtered = logged_in.get("/api/positions?q=vanguard").get_json()
    assert len(filtered["positions"]) <= len(payload["positions"])


def test_api_positions_ignores_a_bogus_sort_column(sandbox, logged_in):
    resp = logged_in.get("/api/positions?sort=;drop%20table&dir=desc")
    assert resp.status_code == 200
    assert resp.get_json()["positions"]


def test_export_downloads_the_ledger(sandbox, logged_in):
    resp = logged_in.get("/export/lots.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.mimetype
    body = resp.get_data(as_text=True)
    assert "Symbol" in body
    assert "Adj Unit Cost" in body


CSV = (
    "Symbol,Description,Qty,Date Acquired,Adj Unit Cost\n"
    "NEW,New Co,5,01/15/2026,12.34\n"
    "NEW,New Co,5,01/15/2026,12.34\n"
    "BAD,Bad Co,notanumber,01/15/2026,1.00\n"
)


def test_import_previews_then_confirms(sandbox, logged_in):
    before = len(lots_db.list_purchases())
    resp = logged_in.post(
        "/import",
        data={"_csrf": csrf_for(logged_in, "/import"), "csv": (io.BytesIO(CSV.encode()), "ft.csv")},
        content_type="multipart/form-data",
    )
    body = resp.get_data(as_text=True)
    assert "1 to add" in body
    assert "1 already in the ledger" in body
    assert "unreadable" in body
    assert len(lots_db.list_purchases()) == before, "preview must not write"

    resp = logged_in.post("/import/confirm", data={"_csrf": csrf_for(logged_in, "/import")})
    assert resp.status_code == 302
    after = lots_db.list_purchases()
    assert len(after) == before + 1
    assert any(lot["symbol"] == "NEW" and lot["qty"] == 5.0 for lot in after)
    assert not any(lot["symbol"] == "BAD" for lot in after), "unreadable row must not import"


def test_import_cannot_be_confirmed_twice(sandbox, logged_in):
    logged_in.post(
        "/import",
        data={"_csrf": csrf_for(logged_in, "/import"), "csv": (io.BytesIO(CSV.encode()), "ft.csv")},
        content_type="multipart/form-data",
    )
    logged_in.post("/import/confirm", data={"_csrf": csrf_for(logged_in, "/import")})
    before = len(lots_db.list_purchases())
    resp = logged_in.post("/import/confirm", data={"_csrf": csrf_for(logged_in, "/import")}, follow_redirects=True)
    assert "expired" in resp.get_data(as_text=True)
    assert len(lots_db.list_purchases()) == before


def test_import_can_be_cancelled(sandbox, logged_in):
    logged_in.post(
        "/import",
        data={"_csrf": csrf_for(logged_in, "/import"), "csv": (io.BytesIO(CSV.encode()), "ft.csv")},
        content_type="multipart/form-data",
    )
    before = len(lots_db.list_purchases())
    resp = logged_in.post("/import/cancel", data={"_csrf": csrf_for(logged_in, "/import")}, follow_redirects=True)
    assert "cancelled" in resp.get_data(as_text=True)
    assert len(lots_db.list_purchases()) == before


def test_a_non_csv_upload_is_refused(sandbox, logged_in):
    resp = logged_in.post(
        "/import",
        data={"_csrf": csrf_for(logged_in, "/import"), "csv": (io.BytesIO(b"x"), "evil.sh")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Only .csv" in resp.get_data(as_text=True)


def test_upload_filename_is_never_used_as_a_path(sandbox, logged_in):
    """A traversal attempt in the filename must not escape the temp directory."""
    resp = logged_in.post(
        "/import",
        data={
            "_csrf": csrf_for(logged_in, "/import"),
            "csv": (io.BytesIO(CSV.encode()), "../../../../tmp/evil.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    import os

    assert not os.path.exists("/tmp/evil.csv")


# ---------------------------------------------------------------------------
# runtime_setting: the precedence that makes the settings page real
# ---------------------------------------------------------------------------


def test_a_database_row_beats_the_environment(sandbox, monkeypatch):
    """The settings page writes a row; the runtime has to actually use it.

    Reading os.environ here would mean the UI stores an override and then
    silently ignores it, which is what used to happen.
    """
    monkeypatch.setenv("BENCHMARK", "VOO")
    lots_db.set_setting("BENCHMARK", "QQQ")
    assert lots_db.runtime_setting("BENCHMARK", "VOO") == "QQQ"


def test_the_environment_still_works_with_no_row(sandbox, monkeypatch):
    """A fresh install that never opens the settings page relies on .env."""
    lots_db.delete_setting("BENCHMARK")
    monkeypatch.setenv("BENCHMARK", "VOO")
    assert lots_db.runtime_setting("BENCHMARK", "SPY") == "VOO"
    lots_db.delete_setting("BENCHMARK")


def test_the_default_is_the_last_resort(sandbox, monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert lots_db.runtime_setting("AWS_REGION", "eu-north-1") == "eu-north-1"


def test_a_blank_row_falls_through_to_the_environment(sandbox, monkeypatch):
    monkeypatch.setenv("BENCHMARK", "VOO")
    lots_db.set_setting("BENCHMARK", "   ")
    assert lots_db.runtime_setting("BENCHMARK", "SPY") == "VOO"
    lots_db.delete_setting("BENCHMARK")


def test_a_broken_database_cannot_stop_the_email(sandbox, monkeypatch):
    """A locked or corrupt DB must fall back, not raise into the cron job."""
    monkeypatch.setenv("BENCHMARK", "VOO")

    def explode(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(lots_db, "get_setting", explode)
    assert lots_db.runtime_setting("BENCHMARK", "SPY") == "VOO"


# ---------------------------------------------------------------------------
# the ledger fingerprint and a safe backup
# ---------------------------------------------------------------------------


def test_the_fingerprint_moves_for_every_kind_of_edit(sandbox):
    """It is the cache key, so a missed edit means a stale preview."""
    before = lots_db.data_fingerprint()
    lots_db.add_purchase("FP", 2, "2026-05-06", 7.5, "Fingerprint Co")
    # add_purchase returns a rowcount, not the new id, so look it up.
    lot_id = next(x["id"] for x in lots_db.list_purchases() if x["symbol"] == "FP")
    after_add = lots_db.data_fingerprint()
    assert after_add != before, "adding a lot did not change the fingerprint"

    lots_db.update_purchase(lot_id, "FP", 3, "05/06/2026", 7.5, "Fingerprint Co")
    after_qty = lots_db.data_fingerprint()
    assert after_qty != after_add, "changing a quantity did not change the fingerprint"

    lots_db.update_purchase(lot_id, "FP", 3, "05/06/2026", 7.5, "Renamed Co")
    after_desc = lots_db.data_fingerprint()
    assert after_desc != after_qty, "renaming a lot did not change the fingerprint"

    lots_db.remove_purchase(lot_id)
    assert lots_db.data_fingerprint() == before, "the ledger did not return to its old state"


def test_reading_the_ledger_does_not_change_the_fingerprint(sandbox):
    """Otherwise every request would invalidate the cache and nothing is cached."""
    first = lots_db.data_fingerprint()
    lots_db.list_purchases()
    lots_db.get_setting("email_template")
    assert lots_db.data_fingerprint() == first


def test_a_backup_captures_commits_that_are_still_in_the_wal(sandbox, tmp_path):
    """`cp lots.db backup.db` silently loses whatever is still in the WAL.

    The web app and the cron job both hold the database open, so this is the
    normal state, not a rare one. The backup has to use SQLite's own snapshot.
    """
    import sqlite3

    lots_db.add_purchase("WALONLY", 4, "2026-06-01", 11.0, "Wal Co")
    dest = tmp_path / "snapshot.db"
    assert lots_db.backup(str(dest))
    rows = sqlite3.connect(str(dest)).execute("SELECT symbol FROM lots").fetchall()
    assert ("WALONLY",) in rows, "the newest lot was missing from the backup"
