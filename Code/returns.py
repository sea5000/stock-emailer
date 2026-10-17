#!/usr/bin/env python3
"""Performance and risk analytics, all computed from the lot ledger + yfinance prices.

- Time-weighted return (TWR): deposit-adjusted daily returns chained together.
  This is the honest "how is my investing doing" number, independent of when/how
  much new money was added.
- XIRR (money-weighted): what the portfolio actually earned per dollar invested.
- Risk metrics: annualized volatility, Sharpe, Sortino, max drawdown.
"""
import numpy as np
import pandas as pd

ANNUALIZATION = 252


def daily_value_and_deposits(lots, price_df):
    """Per-day portfolio market value and external deposits (new lot purchases).

    lots: DataFrame with Symbol, Qty, Date Acquired, Adj Unit Cost.
    Returns (value, deposits), both Series indexed like price_df.
    """
    idx = price_df.index
    value = pd.Series(0.0, index=idx)
    deposits = pd.Series(0.0, index=idx)
    for _, lot in lots.iterrows():
        sym = lot["Symbol"]
        if sym not in price_df.columns:
            continue
        qty = float(lot.get("Qty") or 0)
        acq = pd.to_datetime(lot.get("Date Acquired"), errors="coerce")
        if pd.isna(acq) or qty == 0:
            continue
        acq = acq.floor("D")
        series = (price_df[sym] * qty).clip(lower=0)
        series[series.index < acq] = 0.0
        value = value + series
        price = float(lot.get("Adj Unit Cost") or 0)
        if price and acq in idx:
            deposits.loc[acq] = deposits.loc[acq] + qty * price
    return value, deposits


def deposit_adjusted_returns(value, deposits):
    """Daily returns with same-day deposits treated as invested at the START of
    the day: r_t = V_t / (V_{t-1} + D_t) - 1.

    This is the standard TWR convention and, unlike (V_t - D_t)/V_{t-1} - 1, it
    stays sane when a deposit is large relative to the existing balance.
    """
    r = pd.Series(np.nan, index=value.index)
    v_prev = value.shift(1)
    base = (v_prev + deposits).replace(0.0, np.nan)
    base = base.where(base > 0)
    r = (value / base - 1.0).fillna(0.0)
    r.iloc[0] = 0.0
    return r


def time_weighted_return(r):
    """Cumulative and annualized time-weighted return from a daily return series."""
    rr = r.dropna()
    if rr.empty:
        return 0.0, 0.0
    twr = float((1.0 + rr).prod() - 1.0)
    years = (rr.index.max() - rr.index.min()).days / 365.25
    ann = (1.0 + twr) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    return twr, ann


def xirr(cashflows, guess=0.10):
    """Money-weighted return. cashflows: list of (date, amount), sign convention:
    deposits negative, final market value positive. Pure-Newton, no scipy."""
    if len(cashflows) < 2:
        return 0.0
    dates = pd.to_datetime([d for d, _ in cashflows])
    amounts = np.array([float(a) for _, a in cashflows])
    d0 = dates[0]
    years = np.array([(d - d0).days / 365.0 for d in dates])
    if not (amounts > 0).any() or not (amounts < 0).any():
        return 0.0
    rate = guess
    for _ in range(200):
        denom = (1.0 + rate) ** years
        f = np.sum(amounts / denom)
        fprime = np.sum(-years * amounts / (1.0 + rate) ** (years + 1.0))
        if fprime == 0:
            break
        step = f / fprime
        rate -= step
        if abs(step) < 1e-9:
            break
        if rate <= -0.999:
            rate = -0.999
    return rate


def risk_metrics(r, value, risk_free=0.0):
    """Annualized return, volatility, Sharpe, Sortino, max drawdown, plus the
    drawdown window's peak/trough dates (for attribution)."""
    rr = r.dropna()
    mean_ann = float(rr.mean() * ANNUALIZATION) if len(rr) else 0.0
    vol = float(rr.std(ddof=1) * np.sqrt(ANNUALIZATION)) if len(rr) > 1 else 0.0
    sharpe = (mean_ann - risk_free) / vol if vol > 0 else 0.0
    neg = rr[rr < 0]
    downside = float(neg.std(ddof=1) * np.sqrt(ANNUALIZATION)) if len(neg) > 1 else 0.0
    sortino = (mean_ann - risk_free) / downside if downside > 0 else 0.0
    peak_date = trough_date = None
    mdd = 0.0
    if (value > 0).any():
        run = value / value.cummax() - 1.0
        trough_date = run.idxmin()
        mdd = float(run.min())
        if isinstance(trough_date, pd.Timestamp) and not pd.isna(trough_date):
            prior = value.loc[:trough_date]
            peak_date = prior.idxmax() if len(prior) else None
        else:
            trough_date = None
    return {
        "annualized_return": mean_ann,
        "volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": mdd,
        "peak_date": peak_date,
        "trough_date": trough_date,
    }


def drawdown_attribution(lots, price_df, peak_date, trough_date, top=3):
    """Per-symbol contribution to a drawdown window (peak -> trough).

    Lots held at the peak are valued at the peak close and marked to the trough
    close. Returns the worst `top` contributors, most-negative first.
    """
    if peak_date is None or trough_date is None:
        return []
    peak_date = pd.Timestamp(peak_date).floor("D")
    trough_date = pd.Timestamp(trough_date).floor("D")
    lots = lots.copy()
    acq = pd.to_datetime(lots["Date Acquired"], errors="coerce")
    held = lots[acq <= peak_date]
    out = []
    for sym, g in held.groupby("Symbol"):
        if sym not in price_df.columns:
            continue
        px = price_df[sym].dropna()
        if px.empty:
            continue
        p_peak = px.asof(peak_date)
        p_trough = px.asof(trough_date)
        if pd.isna(p_peak) or pd.isna(p_trough) or p_peak <= 0:
            continue
        qty = float(g["Qty"].sum() or 0)
        out.append(
            {
                "symbol": sym,
                "pct_change": float(p_trough / p_peak - 1.0),
                "dollar": float(qty * (p_trough - p_peak)),
                "value_at_peak": float(qty * p_peak),
            }
        )
    out.sort(key=lambda x: x["dollar"])
    return out[:top]


def benchmark_total_return(bench_price, start_date):
    """Buy-and-hold total return (decimal) of the benchmark from start_date to the
    latest close."""
    px = bench_price.dropna()
    if px.empty:
        return None
    p0 = px.asof(pd.Timestamp(start_date).floor("D"))
    if pd.isna(p0) or p0 <= 0:
        return None
    return float(px.iloc[-1] / p0 - 1.0)


def benchmark_total_return_annualized(bench_price, start_date):
    """Annualized version of benchmark_total_return."""
    px = bench_price.dropna()
    if px.empty:
        return None
    start_ts = pd.Timestamp(start_date).floor("D")
    p0 = px.asof(start_ts)
    if pd.isna(p0) or p0 <= 0:
        return None
    total = float(px.iloc[-1] / p0 - 1.0)
    years = (px.index.max() - start_ts).days / 365.25
    if years <= 0:
        return None
    return (1.0 + total) ** (1.0 / years) - 1.0


def benchmark_money_weighted_return(flows, bench_price):
    """Money-weighted (XIRR) return of the benchmark given the SAME external
    deposits as the portfolio: each deposit buys benchmark shares at that date's
    price, and the final value is the benchmark position marked to the latest
    close. Returns annualized decimal XIRR."""
    deposits = [(d, a) for d, a in flows if a < 0]
    if not deposits:
        return 0.0
    px = bench_price.dropna()
    if px.empty:
        return 0.0
    shares = 0.0
    for d, a in deposits:
        p = px.asof(pd.Timestamp(d))
        if pd.isna(p) or p <= 0:
            p = px.iloc[0]
        shares += abs(a) / float(p)
    last_date = px.index.max()
    final_value = shares * float(px.iloc[-1])
    return xirr([*deposits, (last_date, final_value)])


def monthly_returns(r):
    """List of {month, pct} for months with any returns, oldest first."""
    rr = r.dropna()
    if rr.empty:
        return []
    m = (1.0 + rr).resample("ME").prod() - 1.0
    return [
        {"month": ts.strftime("%b %Y"), "pct": float(v)}
        for ts, v in m.items()
        if not np.isnan(v)
    ]
