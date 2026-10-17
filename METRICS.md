# Portfolio Checkup: Metrics Explained

This document explains every number that appears in the daily portfolio checkup
email: what it means, how it is calculated, and the conventions (and caveats)
behind it. All logic lives in `Code/returns.py`, `Code/main.py`, and
`Code/email_utils.py`.

---

## 1. Where the numbers come from

Everything is computed from two inputs:

1. **The lot ledger** (`/app/data/lots.db`). The source of truth. Each lot is a
   purchase with a symbol, quantity, acquisition date, and adjusted unit cost.
2. **yfinance price series** (`main.py:fetch_price_df`). Daily closes for every
   held symbol, from ~7 days before the first lot to ~7 days after today, then
   forward-filled to a business-day index.

The engine builds a **per-day picture** (`returns.daily_value_and_deposits`):

- **Market value** `V_t` = for each lot, `qty × price_t` once the lot is held
  (0 before its acquisition date), summed across lots.
- **Deposits** `D_t` = the `qty × adjusted unit cost` of any lot acquired that
  day. "Deposits" here means *new external money going in*; a purchase, not a
  gain or loss.

Because the portfolio is a ledger of discrete buys, "deposits" and "market
value" are cleanly separable, which is what makes the return math honest.

> **Nuance: prices are adjusted closes.** The price series yfinance returns is
> split/dividend-adjusted, and the lot's *Adj Unit Cost* is likewise adjusted.
> So a return series behaves like a **total return** (splits and dividends
> folded in) rather than a raw price return. `Net Gain/Loss` uses the same
> adjusted basis, so it is consistent with the return metrics.

---

## 2. Row 1 cards (the simple money cards)

| Card | Meaning |
|---|---|
| **Portfolio Value** | Sum of `qty × latest adjusted close` across all lots. Shows `N lots · M symbols`. |
| **Cost Basis** | Sum of `qty × adjusted unit cost`: total money originally invested. |
| **Net Gain / Loss** | `Portfolio Value - Cost Basis`, both in dollars and as a percent of cost. |
| **Change vs last checkup** | Dollar change in unrealized gain vs the previous day's archived snapshot. The previous snapshot is **re-priced on the current yfinance basis** before comparing, so a "change" is a real price move, not an artifact of a different price source (`main.py:reprice_snapshot`). Shows `Mkt value Δ` as well. Until a second archive exists, this reads "Baseline set this run." |

`Net Gain/Loss %` is a *simple* return (`market value / cost - 1`). It mixes in
the effect of when money was added (see **TWR** for the cashflow-independent
view).

---

## 3. Row 2 cards (the analytical cards)

These are the four cards that matter for judging performance and risk.

---

### 3.1 Return (time-weighted, TWR)

**What it answers:** *"How good is my investing, ignoring the timing and size of
my deposits?"*

**Why it is needed:** if you add $10k on the best day and $0 on the worst day,
your simple return looks great even though your actual stock-picking was
average. TWR strips that effect out by chaining **per-day** returns that are
corrected for the same-day deposit.

**Calculation** (`returns.deposit_adjusted_returns` + `time_weighted_return`):

For each day `t`:

```
r_t = V_t / (V_{t-1} + D_t) - 1
```

- `V_t` = market value at close of day `t`
- `V_{t-1}` = market value at close of the prior day
- `D_t` = deposits made on day `t`

The deposit is treated as **invested at the start of the day**, so the
denominator is `V_{t-1} + D_t`, not `V_{t-1}`. That is the standard TWR
convention, and it stays well-behaved when a deposit is large relative to the
existing balance (the naive `(V_t - D_t)/V_{t-1} - 1` blows up in that case).
The first day is defined as `r = 0`.

The cumulative (since first lot) TWR is the **chained product** of daily
returns:

```
TWR = ∏ (1 + r_t) - 1
```

Annualized:

```
TWR_ann = (1 + TWR)^(1/years) - 1,   years = calendar days / 365.25
```

The card shows a **two-row comparison** so portfolio and benchmark use the
identical convention (cumulative + annualized):

```
Portfolio  +18.40%   ann +4.02%
VOO        +24.60%   ann +5.35%   (same window · since Mar 15, 2022)
```

The VOO line's annualized figure comes from
`benchmark_total_return_annualized` (buy-and-hold VOO return, same first-lot
to-today window, annualized).

> **Example (illustrative):** TWR **+18.40%** (annualized **+4.02%**) vs VOO
> **+24.60%** (annualized **+5.35%**) over the same ~4.25-year window.
> The account's first lot was in Mar 2022, so the cumulative number spans
> ~4.25 years and includes one bear-year drawdown.

### 3.2a 1-Year Return (quick market comparison)

**What it answers:** *"Am I doing well vs the market over the last year?"*

This card pairs the trailing 12 months of both series on one line:

- **Portfolio 1-year return** = trailing-365-day TWR (chained daily returns of
  the last 365 days, deposit-adjusted).
- **VOO 1-year return** = buy-and-hold VOO return over the same trailing 365
  days.

From a sample run: **Portfolio +24.60% vs VOO +19.40% (12m)**. It's the
decision-relevant "recent performance" check, distinct from the since-inception
TWR which is dominated by the early years.

---

### 3.2 XIRR (money-weighted return)

**What it answers:** *"What did the portfolio actually earn per dollar
invested?"*

This is the **internal rate of return**: the annualized rate `x` that makes the
net present value of every cashflow equal zero.

**Cashflow list** (`main.py:828`):

```
each lot  →  (acquisition date, -qty × adjusted unit cost)      # money out
today     →  (today, + current market value)                     # money in (final)
```

Sign convention: deposits are **negative**, the final value is **positive**.

**Calculation** (`returns.xirr`): solve for `x` such that

```
Σ  amount_i / (1 + x)^(years_i)  =  0
```

where `years_i = (date_i - first_date).days / 365`, using Newton-Raphson
iteration (no external library). Returns a single annualized decimal rate.

The card subtitle compares it to VOO under the **same deposits** (see §4).

> **Why TWR and XIRR differ:** TWR neutralizes deposit timing; XIRR rewards it.
> If you tend to add money at good times, XIRR > TWR (your dollars did better
> than your picks). If you add money at bad times, XIRR < TWR. In the last run
> XIRR **+4.90%** vs TWR +18.40%: most deposits landed after the strong
> rallies, so the money-weighted number trails.

---

### 3.3 Max Drawdown (12m)

**What it answers:** *"How bad was the worst peak-to-trough decline in the last
12 months?"*

**Calculation** (`returns.risk_metrics`):

```
running_dd = V / running_max(V) - 1
max_drawdown = min(running_dd)          # most negative, incl. today
```

- `running_max(V)` is the running all-time high of market value up to each day.
- The card is computed over the **trailing 12 months** (the last 365 days of the
  return series, `main.py:837`). The subtitle shows the **since-inception**
  drawdown as context: e.g. *"since -22.60% incl."*.
- Peak/trough **dates** are recorded (the value peak just before the trough)
  and feed the drawdown attribution below.

**Drawdown attribution** (`returns.drawdown_attribution`, shown in the plain
text and dashboard as "Drawdown culprits"): for the peak→trough window, each
symbol held at the peak is marked `qty × (price_trough - price_peak)`. The
largest negative contributors are listed, e.g.

```
TSLA -26.4% ($-890); INTC -13.6% ($-310); BA -13.8% ($-247)
```

> **Why 12m and not since inception:** since-inception drawdowns never reset:
> once you survive a bear market, the number stays at its worst forever and
> stops being an early-warning signal. The 12-month window is the
> decision-relevant measure; inception is shown only for historical context.

---

### 3.4 Volatility (12m)

**What it answers:** *"How jumpy is the portfolio?"*

**Calculation** (`returns.risk_metrics`):

```
vol = std(daily returns, ddof=1) × √252
```

- Daily return standard deviation, sample formula (`ddof=1`), **annualized** by
  scaling with `√252` (252 ≈ trading days per year). So "volatility 18%" means
  a ±1σ daily move ≈ 18%/√252 ≈ 1.13%.
- Computed over the trailing 12 months.

**Sharpe ratio** (shown in the subtitle): excess return per unit of risk.

```
Sharpe = (annualized mean return - risk_free) / volatility
```

with `risk_free = 0` (see nuance below). Sortino is computed the same way but
uses **downside deviation**, the std of only the *negative* daily returns,
so it only punishes the losing direction.

> **Nuances:**
> - `risk_free = 0` is hard-coded, so Sharpe here is "pure return per unit of
>   vol", slightly *higher* than a textbook Sharpe that subtracts ~4-5% risk-free.
> - The mean return used for Sharpe is the simple annualized mean of daily
>   returns, **not** the compounding TWR, so it can differ from the TWR card.
> - Volatility is symmetric: big up-days hurt it as much as big down-days.
>   Sortino exists precisely because most people care more about the down side.

---

## 4. Benchmark comparisons (VOO, default)

Two separate comparisons are computed, and the email labels them precisely:

- **`vs VOO (same window)`** (TWR card): `benchmark_total_return` = buy-and-hold
  VOO return from the *first lot date* to today (`price_now / price_then - 1`).
  It covers the same time span as the portfolio's TWR but is **not**
  deposit-adjusted; it's what $1 invested in VOO on day one would be worth
  today.

- **`vs VOO (same deposits)`** (XIRR card): `benchmark_money_weighted_return`
  replays your *actual deposit schedule* into VOO: each deposit buys VOO
  shares at that day's price, the shares are marked to today's close, and an
  XIRR is run on that cashflow list. This gives VOO a money-weighted return
  under *exactly the same cashflow timing* as your portfolio, so the XIRR
  comparison is apples-to-apples. (Illustrative: XIRR +4.90% vs VOO +6.30%.)

> **Why two different benchmarks?** TWR is a pure "return" number, so the fair
> benchmark is a pure buy-and-hold return over the same period. XIRR is a
> dollar-weighted number, so the fair benchmark must replay your deposits;
> otherwise a big early deposit would let VOO's XIRR look artificially great.

---

## 5. Verdict strip

A one-line daily imperative synthesized from the decision-relevant signals
(`main.py:build_verdict`), with a tone color:

| Condition (checked in order) | Verdict | Tone |
|---|---|---|
| Market data stale (last close > `STALE_DAYS` = 3 days old) | "Hold. Market data is stale…" | amber |
| Any position > `ALERT_POSITION` = 25% of portfolio | "Act: trim … above the 25% concentration threshold." | red |
| 12m max drawdown worse than -`ALERT_DRAWDOWN` = -15% | "Hold. 12-month drawdown … monitor before adding." | amber |
| Single-day move exceeds `ALERT_DAILY_MOVE` = 10% | "Monitor. Portfolio moved +X% today." | amber |
| None breached | "Do nothing. No concentration or drawdown trigger is breached." | green |

All four thresholds come from environment variables (`ALERT_POSITION_PCT`,
`ALERT_DRAWDOWN_PCT`, `ALERT_DAILY_MOVE_PCT`, `STALE_DAYS`) with the defaults
above, so the strictness is tunable without code changes.

---

## 6. Data-quality checks

An amber **"⚠ Data quality"** banner appears above the verdict when any of
these fail (`main.py:912`+):

- **No-price symbols**: holdings with no yfinance quote are excluded from
  totals and analytics; a warning notes them.
- **Private/unsupported tickers**: the `PRIVATE_TICKERS` watchlist (env
  `PRIVATE_TICKERS`, default empty) flags symbols whose quoted price is almost
  certainly a different entity. When one is quoted, it is flagged and excluded
  from totals rather than silently counted. (SPCX used to be on this list;
  it was removed when it became publicly traded.)
- **Ledger vs FT CSV reconcile**: `lots_db.reconcile_csv` compares DB quantity
  per symbol against the broker CSV (tolerance 0.01) and reports mismatches.
- **Stale symbols**: any symbol whose last price is more than 5 trading days
  older than the market's last close.

The email is still sent if checks fail, but the banner makes the data
trustworthiness explicit so you don't act on bad numbers.

---

## 7. Monthly returns table

Lists the trailing **12 calendar months**, oldest first
(`returns.monthly_returns` then `[-12:]`):

```
monthly return = ∏ (1 + r_t) over the month's trading days - 1
```

Same daily-return series as TWR (deposit-adjusted), compounded within each
month. The table header says "Deposit-adjusted (time-weighted) monthly
performance." In the last run it runs Sep 2025 → Aug 2026.

---

## 8. The chart (two panels)

- **Top panel, Portfolio Net P/L:** daily `Net Gain/Loss` (market value vs lot
  cost baseline) over the past year.
- **Bottom panel, Portfolio vs VOO cumulative return:** the TWR cumulative
  line and the VOO line, shown over a **long window** and **rebased so both
  start at 0% at the window's start**. The subtitle states the result
  explicitly, e.g. *"since Feb 04 2025 (rebased to 0): portfolio +36.7% vs VOO
  +26.4%."*

> **Why the rebase matters:** the portfolio's cumulative TWR is anchored to its
> first lot, while VOO is anchored to the same date. Before the rebase, the
> portfolio line entered the window at -19% (the leftover early drag) while
> VOO entered at +53%, producing a constant-looking ~70-80 point gap that
> made the portfolio *look* ~100% below VOO even when its recent performance
> was fine. Rebasing to the window start shows the honest period
> comparison. The since-inception values are still available in the TWR
> card and plain-text "same window" line.

---

## 9. Other terms and nuances

- **Time-weighted vs money-weighted**: the single most important distinction:
  TWR removes the effect of when you added money; XIRR measures the actual
  dollars earned. Use TWR to judge *stock-picking*, XIRR to judge *whether your
  dollars were productive*.
- **Annualization constants**: TWR annualizes by calendar days/365.25; XIRR by
  calendar days/365; volatility by 252 trading days. Small differences are by
  design (compounding vs per-dollar vs per-trading-day).
- **Deposit-at-start convention**: a purchase made on day `t` is treated as if
  invested for the whole day. This is standard and keeps the math stable with
  big deposits.
- **Adjusted closes**: splits/dividends are baked into the price series and
  cost basis, so returns behave like total returns. It also means the charted
  "market value" uses adjusted prices, which may differ slightly from quoted
  closes.
- **Symbols without prices**: excluded everywhere (snapshot, analytics,
  chart). The email's totals are therefore "priced holdings only."
- **No dividends as cash**: dividends are reflected through price adjustment,
  but there is no cash-account modeling; the ledger is a single account.
- **Change vs last checkup**: requires a prior archive in
  `/app/data/history/`; the first run only sets the baseline.
- **Wash-sale adjustments**: carried through from the FT export columns
  (`Wash Sale Adj`) but not applied to the return math, which is purely
  lot-based.
- **Full-resolution dashboard**: a copy of the HTML with the chart embedded at
  full size is written to `/app/data/dashboard.html` each run (the emailed copy
  is the same content, width-constrained).
- **Monetized footnote**: under the metric cards, a four-line explainer turns
  each percentage into dollars using live figures: what $1 would have become
  (TWR), what your deposits became (XIRR), how much the worst 12-month dip cost
  in dollars (Max Drawdown), and the typical ±1σ daily move in dollars
  (Volatility).

---

## 10. Worked example (illustrative run, synthetic data)

```
Portfolio Value       $18,400.00       42 lots · 21 symbols
Cost Basis            $12,850.00
Net Gain/Loss         $5,550.00 (43.19%)
TWR (time-weighted)   +18.40%  (ann +4.02%)   vs VOO +24.60% (ann +5.35%) same window
1-Year Return (12m)   +24.60%                vs VOO +19.40% (12m)
XIRR (money-weighted) +4.90%                  vs VOO +6.30% same deposits
Max Drawdown (12m)    -14.20%   (since inception -22.60%)
Volatility (12m)      16.40%    Sharpe(12m) 0.86
Drawdown culprits     TSLA -26.4% ($-890); INTC -13.6% ($-310); BA -13.8% ($-247)
Verdict               "Do nothing." (green)
```

---

## 11. The web UI

A second container, `stock-emailer-web`, serves a small Flask app for managing
the ledger and the email templates from a phone on the LAN. It shares the
`/app/data` volume with the cron container, so a change made in the UI is in the
next scheduled email.

```sh
docker compose up -d --build      # start both
docker compose logs -f stock-emailer-web
```

Then open `http://<your-lan-ip>:8099` on the LAN.

### Required configuration

| Variable | Why |
|---|---|
| `WEB_PASSWORD` | **Required.** Compose refuses to start without it. With no password set the UI locks itself rather than serving the ledger in cleartext. |
| `WEB_SECRET_KEY` | Optional. Session cookie key. If unset, one is generated and kept in `.web_secret` (mode 0600) beside `lots.db`, so logins survive a restart. |
| `WEB_HTTPS` | Set to `1` only if you put this behind a reverse proxy terminating TLS. It adds the `Secure` cookie flag, which plain HTTP cannot use. |
| `WEB_PREVIEW_TTL` | Seconds a computed report is reused before prices are refetched. Default 300. |
| `LOG_LEVEL` | Default `INFO`. |

`AWS_KEY` / `AWS_SECRET` are read from `.env` exactly as the cron job reads
them. They are deliberately not shown or editable on the settings page.
A credential has no business round-tripping through a browser form.

### The pages

| Page | What it does |
|---|---|
| **Positions** | One row per symbol: shares, average cost, cost basis, lot count, first purchase. Reads only the ledger, so it is instant and works with no internet. Sortable and filterable. |
| **Lots** | Add, edit and delete individual purchase lots. This is the authoritative record; `FT_GainLoss.csv` is only an import format and a seed. |
| **Email** | Edit the templates described in §12. |
| **Import** | Upload an FT Gain/Loss CSV. Shows what would be added, what already exists, and what could not be read, and writes nothing until you confirm. |
| **Settings** | Override `BENCHMARK`, `SES_SENDER`, `SES_RECIPIENT`, `AWS_REGION`, `CRON_TIME` and `DRY_RUN` without editing `.env`. A blank box falls back to `.env`. |

### Which value actually wins

Three places can hold the same setting, and the order is fixed:

```
database (the settings page)  >  .env / container environment  >  built-in default
```

A blank box on the settings page *deletes* the row rather than storing an empty
string, which is what makes "blank means use `.env`" true. Anything the runtime
reads for one of these six keys must go through
`lots_db.runtime_setting(key, default)` rather than `os.environ.get`; reading
the environment directly means the page stores an override and then silently
ignores it. `runtime_setting` also never raises: a locked or corrupt database
falls through to the environment, because a broken settings table must not stop
tomorrow's scheduled email.

The five Python-visible keys (`BENCHMARK`, `SES_SENDER`, `SES_RECIPIENT`,
`AWS_REGION`, `DRY_RUN`) are resolved each time a report is built, so they apply
to the next preview or send. `CRON_TIME` is read once by `entrypoint.sh` when
the container starts, so changing that one needs a restart.

Changing `BENCHMARK` also invalidates the cached report, because it is part of
the cache key. See §13.

---

## 13. The preview cache, and why the ledger is hashed

Building a report costs about five seconds: a yfinance round trip plus the
chart. So the web app keeps one report in memory per gunicorn worker and reuses
it for `WEB_PREVIEW_TTL` seconds (default 300). Two workers means two caches, so
the first two previews after a restart each pay the full price; after that both
are warm and a preview is about 0.08 s.

The cache is keyed on everything the report depends on:

- the date,
- the resolved benchmark,
- a SHA-256 hash of the ledger rows the report reads,
- the size and mtime of `FT_GainLoss.csv`.

**The ledger is hashed rather than timestamped, and that is not a detail.** The
database runs in SQLite's WAL mode, so a write appends to `lots.db-wal` and
leaves `lots.db`'s own mtime untouched until some later connection happens to
checkpoint. Keying off `lots.db`'s mtime means an added lot invalidates
nothing, and the preview cheerfully shows the portfolio as it was before you
added the position. Worse, the `-wal` and `-shm` files *are* touched by ordinary
reads, so including those timestamps instead means a rebuild on every single
request: a cache that never caches. Hashing the rows that the report actually
reads is exact and quiet in both directions, and costs well under a
millisecond against a five-second build. A changed description is caught just
as reliably as a changed quantity.

### Backing up the ledger

The same WAL behaviour makes a plain `cp` of the database unsafe:

```sh
cp lots.db backup.db        # WRONG - silently stale
```

Anything still sitting in `lots.db-wal` is missing, and because both containers
hold the database open, "still sitting in the WAL" is the normal state rather
than a rare race. Use the online-backup helper instead, which takes a
consistent snapshot while the app is running and then folds the WAL back in:

```sh
python Code/lots_db.py backup --path /home/you/backups/lots-$(date +%F).db
```

This is not hypothetical: an automated regression gate that copied only
`lots.db` reported stale data once and nearly hid a real settings change.


### About dates

Lots are stored as `MM/DD/YYYY` text, which is the FT convention and what the
CSV uses. That format does **not** sort chronologically as a string
(`01/05/2026` sorts before `12/13/2024`), so anything that orders by date
normalises to ISO first. The UI accepts either `YYYY-MM-DD` or `MM/DD/YYYY` and
rejects anything else rather than storing a value that cannot be sorted.

### What a preview will not do

Preview and Send test are careful not to disturb the scheduled email:

- No history snapshot is written. The snapshot is what tomorrow's email calls
  "change since the last checkup", so a preview must never create one.
- `dashboard.html` is not rewritten.
- The temporary lots CSV is not exported.
- The chart is written to a temp directory, not over the `total_net.png` the
  cron job produces.
- `DRY_RUN=1` (the default) means Send test reports what it *would* have sent
  and sends nothing.

If `DRY_RUN=0`, Send test really does email you. The subject is prefixed
`TEST - ` so it is obvious in an inbox.

---

## 12. Editing the email templates

The email is built from 13 named sections. Each has a default template, and
yours live in the `settings` table under the key `email_sections`, so you can
edit them from the **Email** page without touching the code.

| Key | In the email |
|---|---|
| `header` | Title and date |
| `banners` | Data-quality notices, stale-price warning, alerts |
| `money_cards` | Row 1: portfolio value, cost basis, net gain/loss |
| `analytics_cards` | Row 2: TWR, XIRR, drawdown, volatility, Sharpe, 1-year |
| `explainer` | The plain-language paragraphs under row 2 |
| `verdict` | The recommendation strip |
| `chart` | The two-panel chart |
| `holdings` | Open positions table |
| `closed_positions` | Sold positions, if any |
| `movers` | Biggest gainers and losers |
| `monthly` | Trailing 12 months of returns |
| `history` | Archived previous checkups |
| `footer` | Methodology note and generation time |

### How to edit one

The blocks are Jinja2 templates, but you do not need to know that to use them.
The rule that matters:

> **Edit the markup, not the arithmetic.** Every number is computed for you.

The Python side still owns all the maths (`Code/sections.py` builds the values,
`Code/returns.py` and `Code/main.py` compute them). A template only decides how
they are arranged. If a template computes something itself, it will disagree
with the rest of the email the moment the calculation is corrected.

Each section is rendered in its own Jinja sandbox, so:

- A block that does not compile is **left out of the email** rather than
  breaking the whole message. The Email page tells you which one and why.
- Templates cannot reach Python internals: `{{ ''.__class__.__mro__ }}`
  renders as nothing rather than leaking the object model.
- A section with no data (no closed positions, no movers) renders empty and is
  skipped, so you never have to guard with `{% if %}`.

`on.<key>` is available in every template and is `True` when that section is
switched on; useful for the shared text template below.

### The plain-text part

There is one plain-text template, not thirteen. It is the alternative part of
the email, for clients that will not render HTML, and it is meant to be a
concise summary rather than a mirror of the HTML. It uses the same `on.<key>`
flags, so switching a section off in the HTML switches its line off in the text
too. It has no `{{ money() }}`-style filters and no HTML; values are inserted
as plain text.

### Email-client constraints

Most clients strip `<style>` blocks and ignore external CSS, so:

- Put styling in `style="..."` attributes, as the defaults do.
- Tables are the only reliable layout. The defaults use nested tables with an
  explicit `width` and `cellpadding`.
- Keep it to a single column. There is no width above about 600px that a phone
  will use.
- The `<!--[if mso]>` style conditional comments are still worth keeping if you
  care about Outlook.

### Resetting

Every block has its own **Reset this block**, and **Reset all** puts everything
back to the shipped defaults. Resetting is the fastest way out of a template you
have broken.
