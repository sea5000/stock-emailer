"""Shared fixtures.

The web tests must not touch the real ledger and must not need the network, so
every test gets a throwaway copy of the data directory and the report context is
synthesised rather than downloaded.
"""

import shutil
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).resolve().parent.parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

PROJECT_DIR = CODE_DIR.parent


@pytest.fixture
def data_dir(tmp_path):
    """An empty data directory with just the two input files the app reads.

    Both files come from ``seeds/`` - synthetic data checked into the repo -
    so the suite runs offline and needs nothing from the real deployment.
    """
    import os

    (tmp_path / "history").mkdir()
    shutil.copy(PROJECT_DIR / "seeds" / "FT_GainLoss.csv", tmp_path / "FT_GainLoss.csv")
    os.environ["LOTDB_PATH"] = str(tmp_path / "lots.db")
    import lots_db

    # lots_db may already be imported with a stale DB_PATH; rebind explicitly.
    lots_db.DB_PATH = str(tmp_path / "lots.db")
    lots_db.init_db()
    lots_db.import_csv(str(tmp_path / "FT_GainLoss.csv"))
    return tmp_path


@pytest.fixture
def sandbox(monkeypatch, data_dir):
    """Point every path-reading module at the throwaway directory.

    The modules are patched in place rather than re-imported: DB_PATH and
    friends are read at import time, but test modules that did `import lots_db`
    at the top would keep a reference to the old module object and go on using
    /app/data no matter what sys.modules says.
    """
    monkeypatch.setenv("LOTDB_PATH", str(data_dir / "lots.db"))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("HISTORY_DIR", str(data_dir / "history"))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("WEB_PASSWORD", "test-password")
    monkeypatch.setenv("WEB_SECRET_KEY", "test-secret-key")

    import lots_db
    import main

    monkeypatch.setattr(lots_db, "DB_PATH", str(data_dir / "lots.db"))
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "HISTORY_DIR", str(data_dir / "history"))
    lots_db.init_db()
    return data_dir


@pytest.fixture(autouse=True)
def _clear_login_throttle():
    """The failed-login counter is a module-level singleton keyed by client IP,
    and every test client looks like the same IP, so a test that deliberately
    trips the lockout would otherwise lock out the rest of the run."""
    from web import auth

    auth._attempts._data.clear()
    yield
    auth._attempts._data.clear()


@pytest.fixture
def app(sandbox):
    from web import create_app

    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in(client):
    """A client with a valid session, having gone through the real login form."""
    import re

    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    resp = client.post(
        "/login",
        data={"password": "test-password", "_csrf": token, "next": "/lots"},
    )
    assert resp.status_code == 302
    return client


def csrf_for(client, path="/lots"):
    import re

    page = client.get(path).get_data(as_text=True)
    match = re.search(r'name="_csrf" value="([^"]+)"', page)
    assert match, f"no CSRF token in {path}"
    return match.group(1)


@pytest.fixture
def fake_context():
    """A minimal but complete report context, so rendering can be tested offline.

    Shaped like the real compute_context() output for the fields the templates
    read, with no DataFrames where the defaults are empty.
    """
    from datetime import date

    return {
        "today": date(2026, 9, 25),
        "summary": {
            "market_value": 41234.56,
            "cost_basis": 24800.26,
            "net_gl": 16434.30,
            "net_gl_pct": 66.27,
            "positions": 3,
            "symbols": 3,
        },
        "prev": {"date": "2026-09-24", "delta_gl": 120.0, "market_value": 41114.56},
        "holdings": [],
        "history": [],
        "removed": [],
        "csv_path": "FT_GainLoss.csv",
        "graph_path": "total_net.png",
        "chart_src": "",
        "dashboard_path": "/app/data/dashboard.html",
        "benchmark": "VOO",
        "verdict": {"text": "Do nothing. No concentrated bet stands out.", "tone": "calm"},
        "returns": {},
        "risk": {},
        "risk12": {},
        "bench_ctx": {},
        "explainer": {},
        "integrity": [],
        "alerts": [],
        "stale": False,
        "drawdown_attribution": [],
    }
