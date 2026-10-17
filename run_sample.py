#!/usr/bin/env python3
"""Sample-only, fully offline entrypoint for stock-emailer.

This module lets the repository render end-to-end with zero credentials,
zero network, and zero real portfolio data. It:

1. Generates (or reuses) clearly-fake data under ``seeds/``: an FT-style
   12-column lot ledger and a seeded daily price history. Both are
   deterministic given a given end date, so the rendered sample is stable.
2. Points every data path at ./sample_output and swaps
   ``main.fetch_price_df`` for a stub that reads ``seeds/prices.csv``
   instead of Yahoo Finance. The real entrypoint ``Code/main.py`` is
   never modified - only the module-level function reference is patched,
   at runtime, by this script.
3. Runs the *actual* production path: lots CSV -> SQLite ledger ->
   ``compute_context`` (positions, returns, TWR/XIRR, drawdown, risk,
   concentration, 2-panel chart) -> ``email_utils.email``, which defaults
   to DRY_RUN and therefore records a local send_log row and writes
   ``preview.html`` without touching SES or the network.

Usage (from anywhere):

    python run_sample.py               # reuse seeds/ unless the price
                                        # data is older than today
    python run_sample.py --regenerate  # rebuild seeds/ from scratch
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parent
CODE_DIR = ROOT / "Code"
SEEDS = ROOT / "seeds"
OUT = ROOT / "sample_output"

# Lots of the report reads DATA_DIR / HISTORY_DIR / LOTDB_PATH at import
# time, so these must be set before `main` is imported below.
OUT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("DATA_DIR", str(OUT))
os.environ.setdefault("HISTORY_DIR", str(OUT / "history"))
os.environ.setdefault("LOTDB_PATH", str(OUT / "lots.db"))
os.environ.setdefault("DRY_RUN", "1")
sys.path.insert(0, str(CODE_DIR))

# Ticker, company name, start price, daily drift, daily vol.
# All prices are invented random walks; the names are public companies so
# the holdings table reads like a real portfolio checkup.
TUNES = [
    ("AAPL", "Apple Inc.", 150.0, 0.0005, 0.018),
    ("MSFT", "Microsoft Corp.", 280.0, 0.0006, 0.016),
    ("NVDA", "NVIDIA Corp.", 90.0, 0.0018, 0.030),
    ("TSLA", "Tesla Inc.", 240.0, -0.0006, 0.034),
    ("AMZN", "Amazon.com Inc.", 130.0, 0.0007, 0.022),
    ("GOOGL", "Alphabet Inc.", 85.0, 0.0006, 0.020),
    ("META", "Meta Platforms Inc.", 250.0, 0.0008, 0.026),
    ("JPM", "JPMorgan Chase & Co.", 140.0, 0.0004, 0.013),
    ("XOM", "Exxon Mobil Corp.", 100.0, 0.0002, 0.015),
    ("JNJ", "Johnson & Johnson", 155.0, 0.0003, 0.010),
    ("PG", "Procter & Gamble Co.", 135.0, 0.0003, 0.011),
    ("KO", "Coca-Cola Co.", 58.0, 0.0002, 0.009),
    ("WMT", "Walmart Inc.", 55.0, 0.0003, 0.010),
    ("CRM", "Salesforce.com Inc.", 170.0, 0.0010, 0.024),
    ("ADBE", "Adobe Inc.", 190.0, 0.0004, 0.023),
    ("INTC", "Intel Corp.", 45.0, -0.0004, 0.020),
    ("BA", "The Boeing Co.", 170.0, -0.0010, 0.026),
    ("CAT", "Caterpillar Inc.", 320.0, -0.0002, 0.017),
    ("LMT", "Lockheed Martin Corp.", 350.0, 0.0005, 0.012),
    ("PFE", "Pfizer Inc.", 38.0, -0.0003, 0.014),
    ("UNH", "UnitedHealth Group Inc.", 380.0, 0.0002, 0.012),
]
# Benchmark series only, never held.
BENCH = ("VOO", "Vanguard S&P 500 ETF", 380.0, 0.00045, 0.011)
SEED = 20261017
START = date(2022, 1, 3)
CSV_COLUMNS = [
    "Symbol", "Description", "Qty", "Days Held", "Date Acquired", "Last",
    "Market Value", "Adj Unit Cost", "Adj Cost", "Wash Sale Adj",
    "Adj GainLoss", "Adj PGainLoss",
]


def _generate_prices(end: date):
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(SEED + end.toordinal())
    days = pd.date_range(start=START.isoformat(), end=end.isoformat(), freq="D")
    frames = []
    for sym, _name, p0, drift, vol in list(TUNES) + [BENCH]:
        steps = rng.normal(drift, vol, size=len(days))
        frames.append(
            pd.Series(p0 * np.cumprod(1.0 + steps), index=days, name=sym)
        )
    return pd.concat(frames, axis=1)


def _pick_lots(prices) -> list[dict]:
    """30-50 lots across the tuned tickers, bought on plausible dates at
    exactly the seeded price for that day, so the CSV and prices.csv agree
    like a real broker export would."""
    rng = Random(SEED)
    idx = list(prices.index)
    lots: list[dict] = []
    for sym, company, _p0, _drift, _vol in TUNES:
        for _k in range(rng.choice([1, 1, 2, 2, 3])):
            pos = rng.randrange(30, max(60, len(idx) - 60))
            day = idx[pos].date()
            qty = rng.randrange(4, 80) * 5
            cost = round(float(prices[sym].iloc[pos])
                         * rng.uniform(0.995, 1.005), 4)
            lots.append(
                {"symbol": sym, "company": company,
                 "qty": qty, "acq": day, "cost": cost}
            )
    return lots[:48]


def _write_seeds(prices, lots: list[dict]) -> None:
    import pandas as pd

    SEEDS.mkdir(parents=True, exist_ok=True)
    last_pos = len(prices.index) - 1
    end = prices.index[-1].date()

    p = prices.copy()
    p.index = [d.strftime("%Y-%m-%d") for d in p.index]
    p.to_csv(SEEDS / "prices.csv", float_format="%.4f")

    rows = []
    for lot in lots:
        sym = lot["symbol"]
        last = float(prices[sym].iloc[last_pos])
        qty, cost = lot["qty"], lot["cost"]
        gl = qty * (last - cost)
        rows.append({
            "Symbol": sym,
            "Description": f"Sample: {lot['company']}",
            "Qty": qty,
            "Days Held": (end - lot["acq"]).days,
            "Date Acquired": lot["acq"].strftime("%m/%d/%Y"),
            "Last": round(last, 4),
            "Market Value": round(qty * last, 2),
            "Adj Unit Cost": cost,
            "Adj Cost": round(qty * cost, 2),
            "Wash Sale Adj": "",
            "Adj GainLoss": round(gl, 2),
            "Adj PGainLoss": round(gl / (qty * cost) * 100.0, 4),
        })
    pd.DataFrame(rows, columns=CSV_COLUMNS).to_csv(
        SEEDS / "FT_GainLoss.csv", index=False
    )


def _seed_end_date() -> date | None:
    """Last price date in a previous seed, if one exists."""
    try:
        import pandas as pd

        tail = pd.read_csv(
            SEEDS / "prices.csv", usecols=[0], index_col=0, parse_dates=True
        )
        return tail.index[-1].date()
    except Exception:
        return None


def ensure_seeds(regenerate: bool = False) -> None:
    """Write seeds/ unless the existing price data already runs to today."""
    if not regenerate:
        last = _seed_end_date()
        if last == date.today() and (SEEDS / "FT_GainLoss.csv").is_file():
            return
    prices = _generate_prices(date.today())
    lots = _pick_lots(prices)
    _write_seeds(prices, lots)
    print(f"Seeded {SEEDS}: {len(lots)} lots, prices -> {prices.index[-1].date()}")


def main() -> int:
    import pandas as pd

    import email_utils
    import main as se

    ensure_seeds("--regenerate" in sys.argv[1:])

    prices = pd.read_csv(SEEDS / "prices.csv", index_col=0, parse_dates=True)

    def stub_fetch_price_df(symbols, start, end):
        cols = [s for s in symbols if s in prices.columns]
        return prices[cols]

    se.fetch_price_df = stub_fetch_price_df  # type: ignore[assignment]

    # All artifacts (chart, dashboard, preview, send_log) land in ./sample_output.
    os.chdir(OUT)
    print("Running real compute_context() against the seeded sample data...")
    try:
        run = se.compute_context(
            csv_arg=str(SEEDS / "FT_GainLoss.csv"),
            out_path="total_net.png",
            show=False,
        )
    except se.ReportError as e:
        print(f"Report failed: {e}")
        return int(getattr(e, "exit_code", 1) or 1)

    print("Rendering through the real email path (DRY_RUN, nothing sent)...")
    ok = email_utils.email(run.ctx)
    if not ok:
        print("Dry-run email build failed")
        return 1

    s = run.ctx["summary"]
    print("\n--- sample summary (all figures synthetic) ---")
    print(f"positions={s['positions']}  symbols={s['symbols']}")
    print(f"cost=${s['cost_basis']:,.0f}  market=${s['market_value']:,.0f}  "
          f"net=${s['net_gl']:,.0f}")
    for name in ("total_net.png", "preview.html", "dashboard.html"):
        p = Path(name)
        if p.is_file():
            print(f"  {name}: {p.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
