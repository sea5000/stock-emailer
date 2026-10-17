#!/usr/bin/env python3
"""Lots ledger (SQLite) - the authoritative source of what you own.

Each row is one purchase lot: symbol, description, qty, date acquired, unit price.
No broker-computed fields are stored; all market values are computed from yfinance
at report time. New purchases are recorded here, not by editing FT_GainLoss.csv.

The same module also stores UI/template settings (see the `settings` table) so the
LAN web app and the cron email job agree on one configuration source of truth.

Usage:
    python lots_db.py add    --symbol AMD --qty 5 --date 2026-08-01 --price 250.48 [--description "Advanced Micro Devices"]
    python lots_db.py list
    python lots_db.py update --id 3 --qty 7
    python lots_db.py remove --id 3
    python lots_db.py import-csv --path FT_GainLoss.csv
    python lots_db.py export-csv --path lots.csv
    python lots_db.py backup --path /path/to/lots-snapshot.db
    python lots_db.py get    --key email_options
    python lots_db.py set    --key email_options --value '{"benchmark": "VOO"}'
"""
import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = os.environ.get("LOTDB_PATH", "/app/data/lots.db")


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    # WAL lets the cron container read the ledger while the web container writes
    # to it. Without this, a write during the daily run raises "database is locked".
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def init_db():
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                description TEXT DEFAULT '',
                qty REAL NOT NULL,
                date_acquired TEXT NOT NULL,
                unit_price REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(symbol, date_acquired, qty, unit_price)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TEXT NOT NULL DEFAULT (datetime('now')),
                kind TEXT NOT NULL,
                subject TEXT DEFAULT '',
                message_id TEXT DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 0,
                dry_run INTEGER NOT NULL DEFAULT 0,
                detail TEXT DEFAULT ''
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS send_log_sent_at ON send_log (sent_at DESC)"
        )


# ---------------------------------------------------------------------------
# settings (used by the web UI to persist template / options)
# ---------------------------------------------------------------------------


def get_setting(key, default=None):
    """Read a settings value, stored as JSON text. Returns `default` if unset."""
    init_db()
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (str(key),)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def data_fingerprint():
    """A content hash of the ledger, for cache keys.

    The web app caches the computed report and rebuilds it when this changes.

    File timestamps are not usable for that. The database runs in WAL mode, so
    a write appends to `lots.db-wal` and leaves `lots.db`'s own mtime untouched
    until some later connection happens to checkpoint, while `-wal` and `-shm`
    are touched by ordinary reads. Keying off mtime therefore either misses the
    edit that matters or rebuilds on every request.

    Hashing the rows the report actually reads is exact and quiet: no false
    rebuilds, and a changed description is caught as reliably as a changed
    quantity. This is a few dozen rows, so it costs well under a millisecond
    against a yfinance round trip of seconds. Returns "" if the table is not
    there yet, so a caller can treat that as "no data" rather than an error.
    """
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT symbol, description, qty, date_acquired, unit_price "
                "FROM lots ORDER BY id"
            ).fetchall()
    except sqlite3.Error:
        return ""
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# send log
#
# Container logs are not a record of what was sent: `docker compose up -d`
# recreates the container and the old log goes with it, which is exactly how a
# "why did I get fourteen emails from one click" question becomes unanswerable.
# This table survives that, so the answer is always in the database.
# ---------------------------------------------------------------------------


def record_send(kind, subject="", message_id="", ok=False, dry_run=False, detail=""):
    """Note one delivery attempt. Never raises: a logging failure must not turn
    a successful send into a failed one, or a refusal into a crash."""
    try:
        init_db()
        with _conn() as c:
            c.execute(
                "INSERT INTO send_log "
                "(kind, subject, message_id, ok, dry_run, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(kind),
                    str(subject or "")[:200],
                    str(message_id or "")[:120],
                    1 if ok else 0,
                    1 if dry_run else 0,
                    str(detail or "")[:400],
                ),
            )
    except Exception:  # noqa: BLE001 - see docstring
        pass


def recent_sends(limit=10):
    """Most recent delivery attempts, newest first."""
    try:
        init_db()
        with _conn() as c:
            rows = c.execute(
                "SELECT sent_at, kind, subject, message_id, ok, dry_run, detail "
                "FROM send_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def backup(dest):
    """Write a consistent copy of the ledger to `dest`.

    `cp lots.db somebackup.db` is wrong here. The database runs in WAL mode, so
    recent commits live in `lots.db-wal` until some connection checkpoints them,
    and a copy of the main file alone is a silently stale snapshot that can be
    missing the last few edits. This uses SQLite's online backup, which takes a
    consistent snapshot while the app is running, then folds the WAL back into
    the main file so a later plain `cp` is also fine.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as src:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    with _conn() as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return dest


def runtime_setting(key, default=None):
    """Resolve a setting the way the running system should actually see it.

    Precedence is database, then environment, then `default`. The settings page
    writes the database row, so an override typed into the UI wins over `.env`;
    but the cron container never opens that page, so `.env` still has to work on
    its own for a fresh install with no rows written yet.

    Call this instead of `os.environ.get` for anything the settings page offers
    to override. Reading `os.environ` directly means the row is stored and then
    silently ignored, which is worse than having no settings page at all.

    Deliberately never raises. A missing, locked, or corrupt database must not
    stop tomorrow's scheduled email, so any failure falls back to the
    environment and then to `default`.
    """
    value = None
    try:
        value = get_setting(key)
    except Exception:
        value = None
    if value is None or (isinstance(value, str) and not value.strip()):
        value = os.environ.get(key)
        if value is None or not str(value).strip():
            return default
    return value


def set_setting(key, value):
    """Write a settings value as JSON text."""
    init_db()
    with _conn() as c:
        c.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (str(key), json.dumps(value)),
        )
    return value


def delete_setting(key):
    init_db()
    with _conn() as c:
        cur = c.execute("DELETE FROM settings WHERE key=?", (str(key),))
    return cur.rowcount


def all_settings():
    """Every settings key mapped to its decoded value."""
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (TypeError, ValueError):
            out[r["key"]] = r["value"]
    return out


# ---------------------------------------------------------------------------
# lots
# ---------------------------------------------------------------------------


def _norm_date(value):
    """Normalize a lot date to the FT convention MM/DD/YYYY."""
    d = pd.to_datetime(str(value).strip(), errors="coerce")
    if pd.isna(d):
        return str(value).strip()
    return d.strftime("%m/%d/%Y")


def add_purchase(symbol, qty, date, price, description=""):
    """Record a purchase lot. Idempotent on the exact (symbol, date, qty, price).
    Returns 1 if inserted, 0 if it already existed."""
    init_db()
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT OR IGNORE INTO lots (symbol, description, qty, date_acquired, unit_price) "
                "VALUES (?,?,?,?,?)",
                (
                    str(symbol).upper().strip(),
                    str(description or "").strip(),
                    float(qty),
                    _norm_date(date),
                    float(price),
                ),
            )
            # A rowcount, not the new id: callers that need the id have to look
            # it up, which is easy to get wrong and silently edits the wrong row.
            return cur.rowcount
        except sqlite3.IntegrityError:
            return 0


def remove_purchase(lot_id):
    """Delete a lot by its id. Returns number of rows deleted."""
    init_db()
    with _conn() as c:
        cur = c.execute("DELETE FROM lots WHERE id=?", (int(lot_id),))
        return cur.rowcount


def get_purchase(lot_id):
    """One lot as a dict, or None if the id is unknown."""
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT id, symbol, description, qty, date_acquired, unit_price, created_at "
            "FROM lots WHERE id=?",
            (int(lot_id),),
        ).fetchone()
    return dict(row) if row else None


def update_purchase(lot_id, symbol, qty, date, price, description=""):
    """Update an existing lot in place.

    The table has UNIQUE(symbol, date_acquired, qty, unit_price), so an edit can
    collide with a different lot that already holds those values. Rather than
    raising, that is reported back to the caller so the UI can show a real error.

    Returns (rows_updated, error_message). Exactly one of the two is meaningful.
    """
    init_db()
    with _conn() as c:
        try:
            cur = c.execute(
                "UPDATE lots SET symbol=?, description=?, qty=?, date_acquired=?, unit_price=? "
                "WHERE id=?",
                (
                    str(symbol).upper().strip(),
                    str(description or "").strip(),
                    float(qty),
                    _norm_date(date),
                    float(price),
                    int(lot_id),
                ),
            )
        except sqlite3.IntegrityError:
            return 0, (
                "Another lot already exists with this symbol, date, quantity and price. "
                "Change one of the fields."
            )
        if cur.rowcount == 0:
            return 0, "No lot with that id."
        return cur.rowcount, None


def list_positions():
    """Lots rolled up one row per symbol: totals, weighted average cost, date range.

    Pure ledger arithmetic - no network. The web UI uses this for the positions
    page and the lot editor's symbol picker.
    """
    rows = list_purchases()
    if not rows:
        return []
    by_symbol = {}
    for r in rows:
        sym = str(r["symbol"]).upper().strip()
        qty = float(r["qty"] or 0)
        price = float(r["unit_price"] or 0)
        entry = by_symbol.setdefault(
            sym,
            {
                "symbol": sym,
                "description": "",
                "qty": 0.0,
                "cost": 0.0,
                "lots": 0,
                "first_acquired": None,
                "last_acquired": None,
            },
        )
        entry["description"] = entry["description"] or str(r["description"] or "").strip()
        entry["qty"] += qty
        entry["cost"] += qty * price
        entry["lots"] += 1
        d = _iso(r["date_acquired"])
        if d:
            # _iso returns YYYY-MM-DD, which does sort chronologically as a string.
            if entry["first_acquired"] is None or d < entry["first_acquired"]:
                entry["first_acquired"] = d
            if entry["last_acquired"] is None or d > entry["last_acquired"]:
                entry["last_acquired"] = d
    out = []
    for entry in by_symbol.values():
        entry["avg_cost"] = entry["cost"] / entry["qty"] if entry["qty"] else 0.0
        out.append(entry)
    return sorted(out, key=lambda e: (-abs(e["qty"] * e["avg_cost"]), e["symbol"]))


def _iso(mmddyyyy):
    """MM/DD/YYYY -> YYYY-MM-DD, or None if unparseable."""
    if not mmddyyyy:
        return None
    d = pd.to_datetime(str(mmddyyyy).strip(), format="%m/%d/%Y", errors="coerce")
    return None if pd.isna(d) else d.strftime("%Y-%m-%d")


def list_purchases():
    """All lots, oldest acquisition first.

    Sorted in Python rather than SQL: date_acquired is stored as MM/DD/YYYY text,
    which does not sort chronologically as a string ("01/05/2026" sorts before
    "12/13/2024"). Rows are normalized through _iso so mixed MM/DD/YYYY and
    ISO-8601 values still order correctly.
    """
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, symbol, description, qty, date_acquired, unit_price, created_at "
            "FROM lots"
        ).fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda r: (_iso(r["date_acquired"]) or "9999-12-31", r["id"]))
    return out


def _parse_import_rows(path):
    """Read an FT-style CSV into normalized candidate lots.

    Returns (candidates, invalid) where each candidate is
    {symbol, description, qty, date, price} and `invalid` carries the rows we
    could not read, with the reason, so the UI can show them rather than
    silently dropping them the way the old importer did.
    """
    df = pd.read_csv(path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    candidates = []
    invalid = []
    for _, r in df.iterrows():
        symbol = str(r.get("Symbol", "") or "").strip().upper()
        raw_date = str(r.get("Date Acquired", "") or "").strip()
        try:
            qty = float(str(r.get("Qty", "")).replace(",", ""))
            price = float(str(r.get("Adj Unit Cost", "")).replace(",", ""))
        except (TypeError, ValueError):
            invalid.append({"row": str(symbol or raw_date), "reason": "qty or price is not a number"})
            continue
        if not symbol:
            invalid.append({"row": raw_date, "reason": "missing symbol"})
            continue
        if not raw_date or pd.isna(pd.to_datetime(raw_date, errors="coerce")):
            invalid.append({"row": symbol, "reason": f"unreadable date {raw_date!r}"})
            continue
        if not qty or not price:
            invalid.append({"row": symbol, "reason": "zero quantity or price"})
            continue
        candidates.append(
            {
                "symbol": symbol,
                "description": str(r.get("Description", "") or "").strip(),
                "qty": qty,
                "date": raw_date,
                "price": price,
            }
        )
    return candidates, invalid


def preview_import_csv(path):
    """Diff an FT-style CSV against the ledger without writing anything.

    Returns {"new": [...], "duplicate": [...], "invalid": [...], "summary": str}
    so the web UI can show a confirm-before-commit screen.
    """
    candidates, invalid = _parse_import_rows(path)
    init_db()
    with _conn() as c:
        existing = {
            (r["symbol"], r["date_acquired"], float(r["qty"]), float(r["unit_price"]))
            for r in c.execute("SELECT symbol, date_acquired, qty, unit_price FROM lots")
        }
    new_rows = []
    dupes = []
    # Track within-file repeats too, so a CSV listing the same lot twice is not
    # reported as two separate insertions.
    pending = set()
    for cand in candidates:
        key = (
            cand["symbol"],
            _norm_date(cand["date"]),
            float(cand["qty"]),
            float(cand["price"]),
        )
        if key in existing or key in pending:
            dupes.append(cand)
        else:
            pending.add(key)
            new_rows.append(cand)
    summary = (
        f"{len(new_rows)} new, {len(dupes)} already in the ledger, {len(invalid)} unreadable"
    )
    return {"new": new_rows, "duplicate": dupes, "invalid": invalid, "summary": summary}


def import_csv(path):
    """Import an FT-style CSV (uses Symbol, Qty, Date Acquired, Adj Unit Cost).
    Returns the number of lots newly inserted."""
    candidates, _invalid = _parse_import_rows(path)
    n = 0
    for cand in candidates:
        n += add_purchase(
            cand["symbol"], cand["qty"], cand["date"], cand["price"], cand["description"]
        )
    return n


def load_lots_df():
    """Lots as a DataFrame in the same layout main.py expects:
    Symbol, Description, Qty, Date Acquired (MM/DD/YYYY), Adj Unit Cost, plus
    the derived FT columns main.py and email_utils use downstream."""
    rows = list_purchases()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.rename(
        columns={
            "symbol": "Symbol",
            "description": "Description",
            "qty": "Qty",
            "unit_price": "Adj Unit Cost",
        }
    )
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df["Description"] = df["Description"].fillna("").astype(str).str.strip()
    df["Qty"] = pd.to_numeric(df["Qty"], errors="coerce").fillna(0.0)
    df["Adj Unit Cost"] = pd.to_numeric(df["Adj Unit Cost"], errors="coerce").fillna(0.0)
    raw = df["date_acquired"].astype(str).str.strip()
    iso_mask = raw.str.match(r"^\d{4}-\d{2}-\d{2}$")
    raw = raw.mask(
        iso_mask,
        pd.to_datetime(raw, format="%Y-%m-%d", errors="coerce").dt.strftime("%m/%d/%Y"),
    )
    dates = pd.to_datetime(raw, format="%m/%d/%Y", errors="coerce")
    df["Date Acquired"] = dates.dt.strftime("%m/%d/%Y")
    df["Days Held"] = 0
    df["Last"] = 0.0
    df["Market Value"] = 0.0
    df["Adj Cost"] = df["Qty"] * df["Adj Unit Cost"]
    df["Wash Sale Adj"] = ""
    df["Adj GainLoss"] = 0.0
    df["Adj PGainLoss"] = 0.0
    return df[
        [
            "Symbol",
            "Description",
            "Qty",
            "Days Held",
            "Date Acquired",
            "Last",
            "Market Value",
            "Adj Unit Cost",
            "Adj Cost",
            "Wash Sale Adj",
            "Adj GainLoss",
            "Adj PGainLoss",
        ]
    ]


def export_csv(path):
    """Write the lots ledger to an FT-style CSV."""
    df = load_lots_df()
    df.to_csv(path, index=False)
    return len(df)


def reconcile_csv(path):
    """Compare an FT-style CSV against the lots ledger at symbol level.

    Returns a list of (symbol, csv_qty, db_qty) where total quantities disagree
    by more than 0.01 shares - a signal the ledger and the broker export drifted.
    """
    df = pd.read_csv(path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "Symbol" not in df.columns or "Qty" not in df.columns:
        return []
    sym = df["Symbol"].astype(str).str.strip().str.upper()
    qty = pd.to_numeric(
        df["Qty"].astype(str).str.replace(",", "", regex=False), errors="coerce"
    ).fillna(0.0)
    csv_qty = qty.groupby(sym).sum()
    db_qty = {}
    for r in list_purchases():
        s = str(r["symbol"]).upper().strip()
        db_qty[s] = db_qty.get(s, 0.0) + float(r["qty"] or 0)
    diffs = []
    for s in sorted(set(csv_qty.index) | set(db_qty)):
        a = float(csv_qty.get(s, 0.0) or 0)
        b = float(db_qty.get(s, 0.0) or 0)
        if abs(a - b) > 0.01:
            diffs.append((s, a, b))
    return diffs


def _cli():
    ap = argparse.ArgumentParser(
        description="Lots ledger - record purchases. Data source for the checkup email."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Add a purchase lot")
    a.add_argument("--symbol", required=True)
    a.add_argument("--qty", required=True, type=float)
    a.add_argument("--date", required=True, help="YYYY-MM-DD or MM/DD/YYYY")
    a.add_argument("--price", required=True, type=float)
    a.add_argument("--description", default="")
    a.set_defaults(func=lambda a: print(f"inserted={add_purchase(a.symbol, a.qty, a.date, a.price, a.description)}"))

    sub.add_parser("list", help="List all lots").set_defaults(
        func=lambda a: [print(
            f"{r['id']:>3} {r['symbol']:<6} {r['date_acquired']:<10} "
            f"qty={r['qty']:>10.4f} price={r['unit_price']:>10.2f} {r['description']}"
        ) for r in list_purchases()]
    )

    r = sub.add_parser("remove", help="Remove a lot by id")
    r.add_argument("--id", required=True, type=int)
    r.set_defaults(func=lambda a: print(f"deleted={remove_purchase(a.id)}"))

    u = sub.add_parser("update", help="Edit a lot in place by id")
    u.add_argument("--id", required=True, type=int)
    u.add_argument("--symbol")
    u.add_argument("--qty", type=float)
    u.add_argument("--date", help="YYYY-MM-DD or MM/DD/YYYY")
    u.add_argument("--price", type=float)
    u.add_argument("--description")
    u.set_defaults(func=_cli_update)

    i = sub.add_parser("import-csv", help="Import lots from an FT-style CSV")
    i.add_argument("--path", required=True)
    i.set_defaults(func=lambda a: print(f"imported={import_csv(a.path)}"))

    p = sub.add_parser("preview-csv", help="Diff a CSV against the ledger, write nothing")
    p.add_argument("--path", required=True)
    p.set_defaults(func=_cli_preview)

    e = sub.add_parser("export-csv", help="Export lots to an FT-style CSV")
    e.add_argument("--path", required=True)
    e.set_defaults(func=lambda a: print(f"exported={export_csv(a.path)} rows"))

    g = sub.add_parser("get", help="Read a settings key as JSON")
    g.add_argument("--key", required=True)
    g.set_defaults(
        func=lambda a: print(json.dumps(get_setting(a.key), indent=2, default=str))
    )

    st = sub.add_parser("set", help="Write a settings key from a JSON string")
    st.add_argument("--key", required=True)
    st.add_argument("--value", required=True)
    st.set_defaults(
        func=lambda a: print(
            f"set {a.key} = {json.dumps(set_setting(a.key, json.loads(a.value)), default=str)}"
        )
    )

    b = sub.add_parser(
        "backup",
        help="Write a consistent copy of the ledger, then checkpoint the WAL",
    )
    b.add_argument("--path", required=True, help="destination .db file")
    b.set_defaults(func=_cli_backup)

    args = ap.parse_args()
    args.func(args)


def _cli_backup(a):
    """Copy the ledger safely.

    See backup() for why a plain copy is not good enough.
    """
    dest = backup(a.path)
    print(f"backed up {DB_PATH} -> {dest} ({dest.stat().st_size} bytes), WAL checkpointed")


def _cli_update(a):
    """Apply only the flags the caller actually passed."""
    cur = get_purchase(a.id)
    if not cur:
        print(f"no lot with id {a.id}")
        return
    updated, err = update_purchase(
        a.id,
        a.symbol if a.symbol is not None else cur["symbol"],
        a.qty if a.qty is not None else cur["qty"],
        a.date if a.date is not None else cur["date_acquired"],
        a.price if a.price is not None else cur["unit_price"],
        a.description if a.description is not None else cur["description"],
    )
    if err:
        print(f"error: {err}")
    else:
        print(f"updated {updated} row(s)")


def _cli_preview(a):
    res = preview_import_csv(a.path)
    print(res["summary"])
    for row in res["new"]:
        print(f"  NEW  {row['symbol']:<6} {row['date']:<12} qty={row['qty']:<10} @ {row['price']}")
    for row in res["duplicate"]:
        print(f"  DUP  {row['symbol']:<6} {row['date']:<12} qty={row['qty']:<10} @ {row['price']}")
    for row in res["invalid"]:
        print(f"  BAD  {row['row']}: {row['reason']}")


if __name__ == "__main__":
    _cli()
