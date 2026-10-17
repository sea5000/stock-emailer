#!/usr/bin/env python3
"""
main.py

1) Builds the 1-year portfolio net P/L chart from FT_GainLoss.csv lots plus historical
   price lookups (yfinance).
2) Derives the email report strictly from the FT_GainLoss.csv snapshot:
   holdings summary, movers and change-vs-previous-checkup by comparing archived
   daily CSV snapshots (HISTORY_DIR).

Usage:
    python main.py --csv FT_GainLoss.csv --out total_net.png --no-show

Options:
    --csv PATH         Path to FT_GainLoss.csv (default: FT_GainLoss.csv)
    --out PATH         Output PNG path (default: total_net.png)
    --use-cost-baseline Use the CSV's `Adj Unit Cost` as lot baseline (instead of market price at acquisition)
    --no-show          Do not attempt to show the interactive plot (useful in headless environments)
    --force            Run even if today is not a weekday
"""

from __future__ import annotations
import argparse
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime as dt, date as date_cls, timedelta

import numpy as np
import pandas as pd
import email_utils
import lots_db
import returns as returns_mod

HISTORY_DIR = os.environ.get("HISTORY_DIR", "/app/data/history")
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

# Staleness guard: flag the report if the latest yfinance close is older than this.
STALE_DAYS = int(os.environ.get("STALE_DAYS", "3"))
# Alert thresholds (percentages).
ALERT_DRAWDOWN = float(os.environ.get("ALERT_DRAWDOWN_PCT", "15"))
ALERT_POSITION = float(os.environ.get("ALERT_POSITION_PCT", "25"))
ALERT_DAILY_MOVE = float(os.environ.get("ALERT_DAILY_MOVE_PCT", "10"))
# Symbols known to be private / not publicly traded: a quoted price here is almost
# certainly a different entity, so the value must never reach totals silently.
# Default empty; add via PRIVATE_TICKERS env var (comma-separated).
PRIVATE_TICKERS = {
    s.strip().upper()
    for s in os.environ.get("PRIVATE_TICKERS", "").split(",")
    if s.strip()
}


def is_trading_day(day):
    """True on US market trading days (weekdays excluding market holidays)."""
    if day.weekday() >= 5:
        return False
    from pandas.tseries.holiday import USFederalHolidayCalendar

    cal = USFederalHolidayCalendar()
    lo, hi = day - timedelta(days=2), day + timedelta(days=2)
    holidays = set(cal.holidays(start=lo, end=hi))
    # Markets stay open on these federal holidays.
    open_holidays = set()
    for rule in getattr(cal, "rules", []):
        if rule.name in ("Columbus Day", "Veterans Day"):
            open_holidays.update(rule.dates(lo, hi))
    # Markets close for Good Friday, which is not a federal holiday.
    good_friday = {
        "2024-03-29",
        "2025-04-18",
        "2026-04-03",
        "2027-03-26",
        "2028-04-14",
    }
    closed = (holidays - open_holidays) or set()
    return day not in closed and day.strftime("%Y-%m-%d") not in good_friday


def load_lots(csv_path):
    """Authoritative lot source: the SQLite lots ledger. Falls back to the FT CSV
    if the ledger is empty, and seeds the ledger from the CSV when it is."""
    df = lots_db.load_lots_df()
    if not df.empty:
        logging.info("Loaded %d lots from lots DB (%s)", len(df), lots_db.DB_PATH)
        return df, True
    if Path(csv_path).is_file():
        logging.info("Lots DB empty - importing from %s", csv_path)
        added = lots_db.import_csv(csv_path)
        if added:
            logging.info("Imported %d lots into the ledger", added)
            df = lots_db.load_lots_df()
            if not df.empty:
                return df, True
    return df, False


def resolve_csv(path_arg):
    """Resolve the snapshot path, preferring an explicit arg, then the mounted
    /app/data directory, and finally the cwd default (the baked copy)."""
    explicit = bool(path_arg) and path_arg != "FT_GainLoss.csv"
    candidates = []
    if explicit:
        candidates.append(path_arg)
    if DATA_DIR:
        candidates.append(str(Path(DATA_DIR) / "FT_GainLoss.csv"))
    if path_arg:
        candidates.append(path_arg)
    for c in candidates:
        if Path(c).is_file():
            return c
    return path_arg or "FT_GainLoss.csv"

NUMERIC_COLS = {
    "Qty": "float",
    "Days Held": "int",
    "Last": "float",
    "Market Value": "float",
    "Adj Unit Cost": "float",
    "Adj Cost": "float",
    "Adj GainLoss": "float",
    "Adj PGainLoss": "float",
}


def fetch_price_df(symbols, start, end):
    """Fetch adjusted-close prices for `symbols` between start/end (inclusive start, exclusive end).
    Returns a DataFrame indexed by date with columns per symbol.
    """
    try:
        import yfinance as yf
    except Exception as e:
        raise RuntimeError("yfinance is required: pip install yfinance") from e

    raw = None
    try:
        raw = yf.download(symbols, start=start, end=end, progress=False)
    except Exception as e:
        logging.warning("Bulk yf.download failed: %s", e)
        raw = None
    if isinstance(raw.columns, pd.MultiIndex):
        if "Adj Close" in raw.columns.levels[0]:
            price_df = raw["Adj Close"]
        elif "Close" in raw.columns.levels[0]:
            price_df = raw["Close"]
        else:
            try:
                price_df = raw.iloc[:, raw.columns.get_level_values(1) == "Close"]
            except Exception:
                price_df = raw
    else:
        if "Adj Close" in raw.columns:
            price_df = raw["Adj Close"]
        elif "Close" in raw.columns:
            price_df = raw["Close"]
        else:
            price_df = raw

    if raw is None or (hasattr(raw, "empty") and raw.empty):
        frames = []
        for s in symbols:
            try:
                h = yf.download(s, start=start, end=end, progress=False)
                if isinstance(h.columns, pd.MultiIndex):
                    if "Adj Close" in h.columns.levels[0]:
                        col = h["Adj Close"]
                    elif "Close" in h.columns.levels[0]:
                        col = h["Close"]
                    else:
                        col = (
                            h.iloc[:, h.columns.get_level_values(1) == "Close"]
                            if "Close" in h.columns.get_level_values(1)
                            else h
                        )
                else:
                    if "Adj Close" in h.columns:
                        col = h["Adj Close"]
                    elif "Close" in h.columns:
                        col = h["Close"]
                    else:
                        col = h.iloc[:, 0]
                col = col.rename(s)
                frames.append(col)
            except Exception as e:
                logging.warning("Failed to fetch %s -> %s", s, e)
        if not frames:
            raise RuntimeError("Failed to fetch any prices via yfinance")
        price_df = pd.concat(frames, axis=1)

    if isinstance(price_df, pd.Series):
        price_df = price_df.to_frame(
            price_df.name if hasattr(price_df, "name") else symbols[0]
        )

    price_df = price_df.sort_index().ffill().bfill()
    return price_df


def build_total_net(lots, price_df, use_cost_baseline=True):
    """Given a lots DataFrame and price DataFrame, return a single Series of total net per day.
    Baseline per lot is the CSV purchase price (Adj Unit Cost); falls back to the first
    available close at/after acquisition if the price is missing."""
    per_lot_series = []
    for idx, lot in lots.iterrows():
        sym = lot.get("Symbol")
        try:
            qty = float(lot.get("Qty") or 0)
        except Exception:
            qty = 0.0
        acq_date = pd.to_datetime(lot.get("Date Acquired"), errors="coerce")
        if pd.isna(acq_date) or qty == 0 or sym not in price_df.columns:
            continue
        acq = acq_date.floor("D")
        prices = price_df[sym]

        baseline = None
        if "Adj Unit Cost" in lots.columns and not pd.isna(lot.get("Adj Unit Cost")):
            try:
                baseline = float(str(lot.get("Adj Unit Cost")).replace(",", ""))
            except Exception:
                baseline = None
        if baseline is None:
            after = (
                prices.loc[prices.index >= acq]
                if (prices.index >= acq).any()
                else prices
            )
            baseline = float(after.iloc[0]) if not after.empty else float(prices.iloc[0])

        net = (prices - baseline) * qty
        net[prices.index < acq] = 0.0
        per_lot_series.append(net.rename(f"{sym}_lot{idx}"))

    if not per_lot_series:
        raise RuntimeError(
            "No valid lot series were created; check CSV and availability of prices"
        )

    lots_df = pd.concat(per_lot_series, axis=1)
    total_net = lots_df.fillna(0).sum(axis=1)
    return total_net, lots_df


def plot_and_save(
    total_net,
    lots_df,
    out_path,
    invested_total=None,
    show=True,
    port_cum=None,
    bench=None,
    bench_label="VOO",
    window_days=545,
):
    """Two-panel chart: top = net P/L in dollars, bottom = cumulative return %
    (portfolio vs benchmark), both over the last ~year."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        end_plot = total_net.index.max()
        start_plot = end_plot - pd.Timedelta(days=window_days)
        total_net_plot = total_net[total_net.index >= start_plot]
        if total_net_plot.empty:
            total_net_plot = total_net
    except Exception:
        total_net_plot = total_net

    fig, (ax1, ax2) = plt.subplots(
        2,
        1,
        figsize=(12, 7),
        sharex=True,
        gridspec_kw={"height_ratios": [1.15, 1]},
    )

    total_net_plot.plot(ax=ax1, lw=2, color="tab:blue")
    ax1.axhline(0, color="k", lw=0.7)
    ax1.axhline(
        total_net_plot.max(),
        color="green",
        lw=0.7,
        ls="--",
        label=f"Max {total_net_plot.max():.2f}",
    )
    ax1.text(
        0.01,
        total_net_plot.max(),
        f"Max: {total_net_plot.max():.2f}",
        transform=ax1.get_yaxis_transform(),
        va="bottom",
        ha="left",
        color="green",
        fontsize="small",
        clip_on=False,
    )
    current_val = float(total_net_plot.iloc[-1])
    ax1.axhline(current_val, color="red", lw=0.7, ls="--")
    ax1.text(
        0.99,
        current_val,
        f"Current {current_val:.2f}",
        transform=ax1.get_yaxis_transform(),
        va="top",
        ha="right",
        color="red",
        fontsize="small",
        clip_on=False,
    )
    ax1.set_title("Total Portfolio Net Gain/Loss Since Purchases")
    ax1.set_ylabel("Net USD")
    ax1.grid(alpha=0.3)

    chart_info = None
    if port_cum is not None and not port_cum.empty and bench is not None:
        port_plot = port_cum[port_cum.index >= start_plot]
        bench_plot = bench[bench.index >= start_plot]
        if len(port_plot) and len(bench_plot):
            p0 = float(port_plot.iloc[0])
            b0 = float(bench_plot.iloc[0])
            port_plot = ((1.0 + port_plot / 100.0) / (1.0 + p0 / 100.0) - 1.0) * 100.0
            bench_plot = ((1.0 + bench_plot / 100.0) / (1.0 + b0 / 100.0) - 1.0) * 100.0
            port_plot.plot(ax=ax2, lw=2, color="tab:blue", label="Portfolio")
            bench_plot.plot(ax=ax2, lw=2, color="tab:orange", label=bench_label)
            ax2.axhline(0, color="k", lw=0.7)
            ax2.set_ylabel("Cumulative return %")
            ax2.legend(loc="upper left", fontsize=9)
            ax2.grid(alpha=0.3)
            p_last = float(port_plot.iloc[-1]) if len(port_plot) else 0.0
            b_last = float(bench_plot.iloc[-1]) if len(bench_plot) else 0.0
            ax2.text(
                0.99,
                0.02,
                f"since {port_plot.index.min():%b %d %Y}: portfolio {p_last:+.1f}%  {bench_label} {b_last:+.1f}%",
                transform=ax2.get_yaxis_transform(),
                va="bottom",
                ha="right",
                fontsize="small",
            )
            chart_info = {
                "p_last": p_last,
                "b_last": b_last,
                "start": port_plot.index.min(),
            }
    if chart_info is None:
        ax2.text(
            0.5,
            0.5,
            "benchmark data unavailable",
            transform=ax2.get_yaxis_transform(),
            va="center",
            ha="center",
            color="gray",
        )
    ax2.set_xlabel("Date")
    plt.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    logging.info("Saved combined net plot to %s", out_path)
    return chart_info
    try:
        csv_out = out_path.with_suffix(".csv")
        total_net.to_csv(csv_out, header=["total_net"], index=True)
        logging.info("Saved combined net series to %s", csv_out)
    except Exception as e:
        logging.warning("Failed to save CSV of total_net: %s", e)

    if invested_total is not None and invested_total != 0:
        pct = float(total_net.iloc[-1]) / float(invested_total) * 100.0
        print(
            f"Net amount as percent of investment: {pct:.2f}% "
            f"(net {float(total_net.iloc[-1]):.2f} / invested {float(invested_total):.2f})"
        )
    print("Most recent total net (last available date):", float(total_net.iloc[-1]))


def compute_invested_total(lots, price_df, use_cost_baseline=True):
    invested_total = 0.0
    for _idx, lot in lots.iterrows():
        try:
            qty = float(lot.get("Qty") or 0)
        except Exception:
            qty = 0.0
        if qty == 0:
            continue
        sym = lot.get("Symbol")
        acq_date = pd.to_datetime(lot.get("Date Acquired"), errors="coerce")
        if pd.isna(acq_date) or sym not in price_df.columns:
            continue
        acq = acq_date.floor("D")
        prices = price_df[sym]

        baseline = None
        if "Adj Unit Cost" in lots.columns and not pd.isna(lot.get("Adj Unit Cost")):
            try:
                baseline = float(str(lot.get("Adj Unit Cost")).replace(",", ""))
            except Exception:
                baseline = None
        if baseline is None:
            after = (
                prices.loc[prices.index >= acq]
                if (prices.index >= acq).any()
                else prices
            )
            baseline = float(after.iloc[0]) if not after.empty else float(prices.iloc[0])
        invested_total += abs(qty) * float(baseline)
    return invested_total


# ---------------------------------------------------------------------------
# CSV-only report data
# ---------------------------------------------------------------------------


def parse_snapshot(csv_path):
    """Read an FT_GainLoss.csv snapshot with robust numeric parsing."""
    df = pd.read_csv(csv_path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "Symbol" not in df.columns:
        raise ValueError("CSV has no Symbol column: %s" % csv_path)
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    if "Description" not in df.columns:
        df["Description"] = ""
    else:
        df["Description"] = df["Description"].fillna("").astype(str).str.strip()
    for col, kind in NUMERIC_COLS.items():
        if col not in df.columns:
            df[col] = 0.0
            continue
        ser = (
            df[col]
            .fillna("")
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.strip()
        )
        ser = ser.replace({"": None, "--": None, "nan": None, "None": None})
        if kind == "int":
            df[col] = pd.to_numeric(ser, errors="coerce").fillna(0).astype("int64")
        else:
            df[col] = pd.to_numeric(ser, errors="coerce").fillna(0.0)
    return df


def aggregate_symbols(df):
    """Aggregate lot-level rows into one row per symbol (all values CSV-derived)."""
    rows = []
    for sym, g in df.groupby("Symbol", sort=False):
        desc = g["Description"].dropna().astype(str)
        desc = desc[desc.str.strip() != ""]
        qty = float(g["Qty"].sum() or 0)
        mv = float(g["Market Value"].sum() or 0)
        cost = float(g["Adj Cost"].sum() or 0)
        gl = float(g["Adj GainLoss"].sum() or 0)
        last_series = g["Last"].dropna()
        last = (mv / qty) if qty else (float(last_series.iloc[0]) if len(last_series) else 0.0)
        pct = (gl / cost * 100.0) if cost else 0.0
        rows.append(
            {
                "Symbol": sym,
                "Description": desc.iloc[0] if len(desc) else "",
                "Qty": qty,
                "Last": last,
                "MarketValue": mv,
                "Cost": cost,
                "GainLoss": gl,
                "GainLossPct": pct,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("GainLoss", ascending=False).reset_index(drop=True)


def compute_market_snapshot(df, price_df):
    """Build the authoritative snapshot: lot data from the FT export (date, purchase
    price, quantity) priced at the latest yfinance close. Returns a DataFrame in the
    same column layout as FT_GainLoss.csv so it can be archived and re-read."""
    out_rows = []
    missing = set()
    for _, r in df.iterrows():
        sym = r["Symbol"]
        prices = price_df[sym].dropna() if sym in price_df.columns else None
        if prices is None or prices.empty:
            missing.add(sym)
            continue
        last = float(prices.iloc[-1])
        qty = float(r.get("Qty") or 0)
        basis = float(r.get("Adj Unit Cost") or 0)
        gl = qty * (last - basis)
        out_rows.append(
            {
                "Symbol": sym,
                "Description": r.get("Description", ""),
                "Qty": qty,
                "Days Held": r.get("Days Held", 0),
                "Date Acquired": r.get("Date Acquired", ""),
                "Last": last,
                "Market Value": qty * last,
                "Adj Unit Cost": basis,
                "Adj Cost": qty * basis,
                "Wash Sale Adj": r.get("Wash Sale Adj", ""),
                "Adj GainLoss": gl,
                "Adj PGainLoss": ((last - basis) / basis * 100.0) if basis else 0.0,
            }
        )
    if missing:
        logging.warning(
            "Symbols with no yfinance price excluded from computed snapshot: %s",
            sorted(missing),
        )
    return pd.DataFrame(out_rows)


def _snapshot_path(history_dir, day):
    return Path(history_dir) / f"FT_GainLoss_{day:%Y-%m-%d}.csv"


def load_previous_snapshot(history_dir, today):
    """Return (prev_date, prev_df) for the most recent snapshot strictly before `today`."""
    d = Path(history_dir)
    if not d.exists():
        return None, None
    best_date, best_path = None, None
    for f in sorted(d.glob("FT_GainLoss_*.csv")):
        try:
            dte = dt.strptime(f.stem.replace("FT_GainLoss_", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if dte < today and (best_date is None or dte > best_date):
            best_date, best_path = dte, f
    if best_path is None:
        return None, None
    return best_date, parse_snapshot(best_path)


def save_snapshot(df, history_dir, day):
    d = Path(history_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(d, day)
    df.to_csv(path, index=False)
    logging.info("Archived snapshot to %s", path)


def reprice_snapshot(df, price_df, at_date):
    """Re-price an archived snapshot's lots at a given date using yfinance closes,
    so historical/change figures stay on one consistent price basis. Falls back to
    the stored values for symbols with no price data at that date."""
    out = df.copy()
    if price_df is None or price_df.empty:
        return out
    ts = pd.Timestamp(at_date).floor("D")
    if ts > price_df.index.max():
        return out
    for i, r in out.iterrows():
        sym = r["Symbol"]
        qty = float(r.get("Qty") or 0)
        cost = float(r.get("Adj Cost") or 0)
        if sym not in price_df.columns:
            continue
        px = price_df[sym].asof(ts)
        if pd.isna(px):
            continue
        mv = qty * float(px)
        out.at[i, "Market Value"] = mv
        out.at[i, "Last"] = float(px)
        out.at[i, "Adj GainLoss"] = mv - cost
        out.at[i, "Adj PGainLoss"] = ((float(px) / float(r.get("Adj Unit Cost") or 1.0)) - 1.0) * 100.0
    return out


def build_history(history_dir, today, price_df=None):
    """List of archived snapshots (oldest first) with totals and day-over-day delta.
    When price data is available the snapshots are re-priced on one consistent basis."""
    d = Path(history_dir)
    rows = []
    if d.exists():
        prev_gl = None
        for f in sorted(d.glob("FT_GainLoss_*.csv")):
            try:
                dte = dt.strptime(f.stem.replace("FT_GainLoss_", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if dte > today:
                continue
            df = parse_snapshot(f)
            if price_df is not None and not price_df.empty:
                df = reprice_snapshot(df, price_df, dte)
            mv = float(df["Market Value"].sum() or 0)
            cost = float(df["Adj Cost"].sum() or 0)
            gl = float(df["Adj GainLoss"].sum() or 0)
            delta = (gl - prev_gl) if prev_gl is not None else None
            rows.append(
                {
                    "Date": dte.strftime("%Y-%m-%d"),
                    "MarketValue": mv,
                    "Cost": cost,
                    "GainLoss": gl,
                    "GainLossPct": (gl / cost * 100.0) if cost else 0.0,
                    "DeltaGL": delta,
                }
            )
            prev_gl = gl
    return rows


def build_report_context(df, today, price_df=None):
    """Build the email context from the current snapshot and the history archive."""
    prev_date, prev_df = load_previous_snapshot(HISTORY_DIR, today)
    if prev_df is not None and price_df is not None and not price_df.empty:
        prev_df = reprice_snapshot(prev_df, price_df, prev_date)

    holdings = aggregate_symbols(df)
    mv = float(df["Market Value"].sum() or 0)
    cost = float(df["Adj Cost"].sum() or 0)
    gl = float(df["Adj GainLoss"].sum() or 0)
    net_pct = (gl / cost * 100.0) if cost else 0.0

    summary = {
        "market_value": mv,
        "cost_basis": cost,
        "net_gl": gl,
        "net_gl_pct": net_pct,
        "positions": int(len(df)),
        "symbols": int(len(holdings)),
    }

    prev = {"date": prev_date.strftime("%Y-%m-%d") if prev_date else None}
    removed = []

    if prev_df is not None:
        prev_sym = aggregate_symbols(prev_df)
        prev_map = prev_sym.set_index("Symbol")
        prev_gl = float(prev_df["Adj GainLoss"].sum() or 0)
        prev_mv = float(prev_df["Market Value"].sum() or 0)
        prev["delta_gl"] = gl - prev_gl
        prev["delta_mv"] = mv - prev_mv

        holdings["PrevLast"] = holdings["Symbol"].map(prev_map["Last"])
        holdings["PrevGL"] = holdings["Symbol"].map(prev_map["GainLoss"])
        holdings["DeltaGL"] = holdings["GainLoss"] - holdings["PrevGL"]
        holdings["DeltaLast"] = holdings["Last"] - holdings["PrevLast"]
        holdings["IsNew"] = holdings["PrevGL"].isna()

        removed_syms = [s for s in prev_map.index if s not in set(holdings["Symbol"])]
        removed = [(s, float(prev_map.loc[s, "GainLoss"] or 0)) for s in removed_syms]
    else:
        holdings["PrevLast"] = np.nan
        holdings["PrevGL"] = np.nan
        holdings["DeltaGL"] = np.nan
        holdings["DeltaLast"] = np.nan
        holdings["IsNew"] = False

    context = {
        "today": today,
        "generated_time": dt.now().strftime("%H:%M"),
        "summary": summary,
        "prev": prev,
        "holdings": holdings,
        "history": build_history(HISTORY_DIR, today, price_df),
        "removed": removed,
        "graph_path": "total_net.png",
        "csv_path": "FT_GainLoss.csv",
    }
    return context


def build_verdict(stale, concentration, risk12, last_ret):
    """One daily imperative synthesized from the decision-relevant signals.
    Returns {"text", "tone"} where tone in {ok, caution, action}."""
    if stale:
        return {
            "text": "Hold. Market data is stale; wait for fresh closes before acting.",
            "tone": "caution",
        }
    over = [c for c in concentration if c["pct"] > ALERT_POSITION]
    if over:
        names = ", ".join(f"{c['Symbol']} ({c['pct']:.0f}%)" for c in over)
        return {
            "text": f"Act: trim {names} - above the {ALERT_POSITION:.0f}% concentration threshold.",
            "tone": "action",
        }
    if risk12 and risk12["max_drawdown"] < -ALERT_DRAWDOWN / 100.0:
        return {
            "text": (
                f"Hold. 12-month drawdown {risk12['max_drawdown']:.1%} is worse than "
                f"-{ALERT_DRAWDOWN:.0f}%; monitor before adding."
            ),
            "tone": "caution",
        }
    if last_ret is not None and abs(last_ret) > ALERT_DAILY_MOVE / 100.0:
        return {
            "text": f"Monitor. Portfolio moved {last_ret:+.2%} today.",
            "tone": "caution",
        }
    return {
        "text": "Do nothing. No concentration or drawdown trigger is breached.",
        "tone": "ok",
    }


# ---------------------------------------------------------------------------


class ReportError(RuntimeError):
    """The report cannot be built.

    `exit_code` is what the CLI returns, preserving the pre-refactor statuses
    (2 = no lots, 1 = no prices). The web UI catches this and renders the
    message instead of a stack trace.
    """

    def __init__(self, message, exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class ReportRun:
    """Everything compute_context() produced, for the caller to send or inspect."""

    ctx: dict
    computed_df: pd.DataFrame
    from_db: bool
    today: object


def compute_context(
    today=None,
    benchmark=None,
    csv_arg="FT_GainLoss.csv",
    out_path="total_net.png",
    show=False,
    attach_csv=True,
    write_dashboard=True,
):
    """Build the full report context from the lot ledger plus live yfinance prices.

    Does no sending and writes no history snapshot, so the web UI can call this
    freely to render a preview. `attach_csv=False` skips exporting the temporary
    lots CSV that only the email attachment needs.

    Returns a ReportRun. Raises ReportError if the report cannot be built.
    """
    if benchmark is None:
        benchmark = lots_db.runtime_setting("BENCHMARK", "VOO")
    if today is None:
        today = date_cls.today()
    out_path = Path(out_path)

    csv_path = Path(resolve_csv(csv_arg))
    df, from_db = load_lots(csv_path)
    if df.empty:
        raise ReportError(
            f"No lots in the ledger, and {csv_path} not found or empty", exit_code=2
        )

    # --- chart / analytics (historical price lookup) ---
    lots = df.copy()
    if "Date Acquired" in lots.columns:
        lots["Date Acquired"] = pd.to_datetime(
            lots["Date Acquired"], format="%m/%d/%Y", errors="coerce"
        )
        mask_na = lots["Date Acquired"].isna()
        if mask_na.any():
            lots.loc[mask_na, "Date Acquired"] = pd.to_datetime(
                lots.loc[mask_na, "Date Acquired"].astype(str), errors="coerce"
            )
    else:
        lots["Date Acquired"] = pd.NaT

    symbols = sorted(lots["Symbol"].dropna().unique())
    if not symbols:
        raise ReportError("No symbols found in the ledger", exit_code=2)

    start = pd.to_datetime(lots["Date Acquired"]).min()
    if pd.isna(start):
        logging.warning("Could not determine earliest acquisition date; falling back to 5 years ago")
        end = pd.Timestamp.today().ceil("D")
        start = (end - pd.Timedelta(days=365 * 5)).floor("D")
    else:
        start = start.floor("D")
        end = pd.Timestamp.today().ceil("D")

    price_df = None
    present = []
    fetch_args = (symbols, start - pd.Timedelta(days=7), end + pd.Timedelta(days=7))
    for attempt in (1, 2):
        try:
            price_df = fetch_price_df(*fetch_args)
            break
        except Exception as e:
            logging.warning("Price fetch failed (attempt %d/2): %s", attempt, e)
            price_df = None
    if price_df is not None:
        price_df.columns = [str(c).strip() for c in price_df.columns]
        present = [s for s in symbols if s in price_df.columns]
        date_index = pd.date_range(start=start, end=price_df.index.max(), freq="D")
        raw_indexed = price_df.reindex(date_index)
        price_df = raw_indexed.ffill()
        price_df = price_df[[c for c in price_df.columns if c in present]]

    # --- benchmark ---
    bench_price = None
    if price_df is not None:
        try:
            b = fetch_price_df([benchmark], *fetch_args[1:])
            b.columns = [str(c).strip() for c in b.columns]
            bench_price = b[benchmark].reindex(date_index).ffill()
        except Exception as e:
            logging.warning("Benchmark fetch failed for %s: %s", benchmark, e)
            bench_price = None

    computed_df = pd.DataFrame()
    risk = None
    risk12 = None
    dd_attr = []
    returns_ctx = None
    monthly = []
    port_cum = None
    bench_pct = None
    chart_info = None
    bench_ctx = {"twr": None, "twr_ann": None, "xirr": None, "yr1": None}
    explainer = None
    if price_df is not None and present:
        computed_df = compute_market_snapshot(df[df["Symbol"].isin(present)], price_df)
        lots_used = lots[lots["Symbol"].isin(present)].copy()
        if len(lots_used):
            try:
                total_net, lots_df = build_total_net(
                    lots_used, price_df, use_cost_baseline=True
                )
                invested_total = compute_invested_total(lots_used, price_df)

                # --- returns / risk analytics (TWR, XIRR, drawdown, vol) ---
                value, deposits = returns_mod.daily_value_and_deposits(
                    lots_used, price_df
                )
                r = returns_mod.deposit_adjusted_returns(value, deposits)
                twr, twr_ann = returns_mod.time_weighted_return(r)
                flows = [
                    (pd.to_datetime(lot["Date Acquired"]), -float(lot["Qty"] or 0) * float(lot["Adj Unit Cost"] or 0))
                    for _, lot in lots_used.iterrows()
                ]
                flows.append(
                    (pd.Timestamp(today), float(computed_df["Market Value"].sum() or 0))
                )
                xirr = returns_mod.xirr(flows)
                risk = returns_mod.risk_metrics(r, value)
                r12 = r[r.index >= (r.index.max() - pd.Timedelta(days=365))]
                risk12 = returns_mod.risk_metrics(r12, value.loc[r12.index])
                dd_attr = returns_mod.drawdown_attribution(
                    lots_used, price_df, risk12.get("peak_date"), risk12.get("trough_date")
                )
                if bench_price is not None and len(bench_price):
                    bench_ctx["twr"] = returns_mod.benchmark_total_return(bench_price, start)
                    bench_ctx["twr_ann"] = returns_mod.benchmark_total_return_annualized(bench_price, start)
                    bench_ctx["xirr"] = returns_mod.benchmark_money_weighted_return(flows, bench_price)

                mv_now = float(computed_df["Market Value"].sum() or 0)
                dd12_peak = dd12_trough = dd12_dollars = None
                if risk12:
                    peak_date = risk12.get("peak_date")
                    trough_date = risk12.get("trough_date")
                    if (
                        peak_date is not None
                        and trough_date is not None
                        and value is not None
                        and peak_date in value.index
                        and trough_date in value.index
                    ):
                        dd12_peak = float(value.loc[peak_date])
                        dd12_trough = float(value.loc[trough_date])
                        dd12_dollars = dd12_trough - dd12_peak
                vol_daily_dollars = None
                if risk12 and risk12.get("volatility", 0.0) > 0:
                    vol_daily_dollars = risk12["volatility"] * mv_now / (252.0 ** 0.5)
                explainer = {
                    "twr_total": twr,
                    "twr_ann": twr_ann,
                    "xirr": xirr,
                    "dd12_pct": risk12.get("max_drawdown") if risk12 else None,
                    "dd12_peak": dd12_peak,
                    "dd12_trough": dd12_trough,
                    "dd12_dollars": dd12_dollars,
                    "incep_dd_pct": risk.get("max_drawdown") if risk else None,
                    "vol12_pct": risk12.get("volatility") if risk12 else None,
                    "vol_daily_dollars": vol_daily_dollars,
                    "deposits_total": float(df["Adj Cost"].sum() or 0),
                    "final_value": mv_now,
                }
                monthly = returns_mod.monthly_returns(r)[-12:]
                yr1_start = r.index.max() - pd.Timedelta(days=365)
                r_yr1 = r[r.index >= yr1_start]
                yr1_twr = float((1.0 + r_yr1).prod() - 1.0) if len(r_yr1) else None
                yr1_bench = (
                    returns_mod.benchmark_total_return(bench_price, yr1_start)
                    if bench_price is not None and len(bench_price)
                    else None
                )
                returns_ctx = {
                    "twr": twr,
                    "twr_annualized": twr_ann,
                    "xirr": xirr,
                    "yr1": yr1_twr,
                }
                bench_ctx["yr1"] = yr1_bench
                port_cum = ((1.0 + r).cumprod() - 1.0) * 100.0
                if bench_price is not None and len(bench_price):
                    bench_pct = (bench_price / bench_price.iloc[0] - 1.0) * 100.0

                print(
                    f"TWR {twr:+.2%} (ann {twr_ann:+.2%})  XIRR {xirr:+.2%}  "
                    f"maxDD {risk['max_drawdown']:+.2%}  vol {risk['volatility']:.2%}  "
                    f"Sharpe {risk['sharpe']:.2f}"
                )

                chart_info = plot_and_save(
                    total_net,
                    lots_df,
                    out_path,
                    invested_total=invested_total,
                    show=show,
                    port_cum=port_cum,
                    bench=bench_pct,
                    bench_label=benchmark,
                )
            except Exception as e:
                logging.warning("Chart/analytics generation failed (continuing): %s", e)
        else:
            logging.warning("No lots usable for chart; skipping chart generation")

    if computed_df.empty:
        raise ReportError(
            "No live prices available; cannot build the report. "
            "Check network access to Yahoo Finance.",
            exit_code=1,
        )

    # --- staleness guard ---
    stale = False
    last_close = None
    stale_symbols = []
    if price_df is not None and len(price_df):
        last_close = price_df.index.max()
        days_since = (pd.Timestamp(today) - last_close).days
        stale = days_since > STALE_DAYS
        for col in price_df.columns:
            lv = raw_indexed[col].last_valid_index()
            if lv is not None and (last_close - lv).days > 5:
                stale_symbols.append(col)

    # --- alerts + concentration (trailing 12 months drives action) ---
    holdings = aggregate_symbols(computed_df)
    tot_mv = float(holdings["MarketValue"].sum() or 0)
    alerts = []
    if risk12 and risk12["max_drawdown"] < -ALERT_DRAWDOWN / 100.0:
        alerts.append(
            f"12-month max drawdown {risk12['max_drawdown']:.1%} is worse than -{ALERT_DRAWDOWN:.0f}%."
        )
    concentration = []
    for _, row in holdings.iterrows():
        pct = (row["MarketValue"] / tot_mv * 100.0) if tot_mv else 0.0
        concentration.append({"Symbol": row["Symbol"], "pct": pct})
        if pct > ALERT_POSITION:
            alerts.append(
                f"{row['Symbol']} is {pct:.0f}% of portfolio value (>{ALERT_POSITION:.0f}%)."
            )
    last_ret = float(r.iloc[-1]) if r is not None and len(r) else None
    if last_ret is not None and not np.isnan(last_ret) and abs(last_ret) * 100.0 > ALERT_DAILY_MOVE:
        alerts.append(
            f"Portfolio moved {last_ret:+.2%} today (>{ALERT_DAILY_MOVE:.0f}%)."
        )

    # --- data integrity gates ---
    integrity = []
    notes = []
    no_price_lots = sorted(set(symbols) - set(present)) if present else sorted(symbols)
    if no_price_lots:
        integrity.append(
            f"No market price for {', '.join(no_price_lots)} - excluded from totals until resolved."
        )
    private_hits = sorted(s for s in PRIVATE_TICKERS if s in present)
    if private_hits:
        integrity.append(
            f"{', '.join(private_hits)} is on the private-company watchlist - the quoted "
            "price may be a different entity; verify the lot."
        )
    if from_db and Path(csv_path).is_file():
        try:
            diffs = lots_db.reconcile_csv(str(csv_path))
            # The ledger is authoritative; the CSV is only the import seed. The
            # two directions are not equally serious, and treating them the same
            # trains the reader to ignore the banner, which also carries the
            # checks that do matter.
            #
            #   db < csv  the ledger has lost shares the broker export still
            #             shows. That silently shrinks every total, so it warns.
            #   db > csv  lots bought through the app since the export. Expected,
            #             reported as a plain note rather than a warning.
            short = [(s, a, b) for s, a, b in diffs if b < a - 0.01]
            extra = [(s, a, b) for s, a, b in diffs if b > a + 0.01]
            if short:
                details = ", ".join(f"{s} (csv {a:g} vs db {b:g})" for s, a, b in short[:5])
                integrity.append(
                    f"Ledger is short against the FT CSV: {details}. The ledger is "
                    "used for the report, so these holdings are not counted until "
                    "the lots are restored."
                )
            if extra:
                adds = ", ".join(f"{s} +{b - a:g}" for s, a, b in extra[:6])
                more = f" and {len(extra) - 6} more" if len(extra) > 6 else ""
                notes.append(
                    f"Ledger updated: {adds}{more} recorded since the last FT CSV "
                    "export. The ledger is authoritative and these are included in "
                    "the totals above; the CSV is only an import seed."
                )
        except Exception as e:
            logging.warning("Reconcile check failed: %s", e)

    context = build_report_context(computed_df, today, price_df)
    context["show_graph"] = out_path.is_file()
    context["returns"] = returns_ctx
    context["risk"] = risk
    context["risk12"] = risk12
    context["drawdown_attribution"] = dd_attr
    context["monthly_returns"] = monthly
    context["benchmark"] = benchmark
    context["bench_ctx"] = bench_ctx
    context["since_date"] = (
        pd.Timestamp(start).strftime("%b %d, %Y") if start is not None else None
    )
    context["explainer"] = explainer
    context["benchmark_return"] = (
        float(bench_pct.iloc[-1]) if bench_pct is not None and len(bench_pct) else None
    )
    if chart_info:
        context["portfolio_window_return"] = chart_info["p_last"]
        context["benchmark_window_return"] = chart_info["b_last"]
        context["chart_window_start"] = chart_info["start"]
    context["stale"] = stale
    context["last_close"] = last_close
    context["stale_symbols"] = stale_symbols
    context["alerts"] = alerts
    context["integrity"] = integrity
    context["integrity_notes"] = notes
    context["concentration"] = concentration
    context["alert_position_pct"] = ALERT_POSITION
    context["csv_path"] = str(csv_path)
    context["verdict"] = build_verdict(stale, concentration, risk12, last_ret)

    # Full-resolution dashboard for local viewing (chart embedded, not emailed).
    if write_dashboard:
        try:
            dash_path = Path(DATA_DIR) / "dashboard.html"
            email_utils.write_dashboard_html(context, dash_path)
            context["dashboard_path"] = str(dash_path)
            logging.info("Wrote full-resolution dashboard to %s", dash_path)
        except Exception as e:
            logging.warning("Dashboard write failed: %s", e)

    # Attach the authoritative lots ledger (from DB) rather than the raw broker export.
    if from_db and attach_csv:
        export_dir = Path(tempfile.mkdtemp(prefix="lots_export_"))
        export_path = export_dir / "FT_GainLoss.csv"
        lots_db.export_csv(str(export_path))
        context["csv_path"] = str(export_path)

    return ReportRun(ctx=context, computed_df=computed_df, from_db=from_db, today=today)


def main(email_send=True, argv=None):
    p = argparse.ArgumentParser(
        description="Build portfolio checkup email from lots ledger + live yfinance prices"
    )
    p.add_argument("--csv", default="FT_GainLoss.csv", help="Fallback/seed CSV for lots")
    p.add_argument("--out", default="total_net.png")
    p.add_argument(
        "--benchmark",
        default=lots_db.runtime_setting("BENCHMARK", "VOO"),
        help="Benchmark ticker for the comparison panel (default VOO)",
    )
    p.add_argument(
        "--use-cost-baseline",
        action="store_true",
        help="(deprecated) purchase price is always the lot baseline",
    )
    p.add_argument("--no-show", action="store_true", help="Do not show interactive plot")
    p.add_argument("--force", action="store_true", help="Run even if not a trading day")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    today = date_cls.today()
    if not args.force and not is_trading_day(today):
        logging.info("Not a US trading day (%s) - skipping scheduled checkup", today)
        return

    try:
        run = compute_context(
            today=today,
            benchmark=args.benchmark,
            csv_arg=args.csv,
            out_path=args.out,
            show=not args.no_show,
        )
    except ReportError as e:
        logging.error("%s", e)
        sys.exit(e.exit_code)

    if email_send:
        if email_utils.email(run.ctx):
            print("Successfully sent portfolio checkup email!")
            # Only archive once the send succeeded, so a failed send does not
            # become tomorrow's "previous checkup" baseline.
            save_snapshot(run.computed_df, HISTORY_DIR, today)
        else:
            print("#Failed to send portfolio checkup email!#")


if __name__ == "__main__":
    main(email_send=True)
