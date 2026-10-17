"""Section registry for the portfolio checkup email.

The email used to be one 500-line f-string in email_utils.render_email_html.
It is now thirteen named sections, each with:

  * a stable key and a human label
  * an HTML template and a plain-text template, both user-editable
  * an enabled flag and a sort order
  * a builder that turns the report context into the values the template uses

The split is deliberate. The template owns the *markup* - headings, captions,
table shells, colours, wording. The builder owns the *numbers*. You can restyle
or rewrite a whole section, reorder it, or switch it off, but you cannot
accidentally change a computed figure by editing a template.

Templates render through a Jinja2 SandboxedEnvironment with autoescape OFF.
Off because the values handed to them are pre-built HTML (entities, coloured
spans) and escaping would double-encode them - the same reason the original
escaper only stringified. The sandbox is what stops a template reaching into
Python internals or looping forever.
"""
import json
import logging

from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup

import email_fmt as fmt

SETTING_KEY = "email_template"

# Order here is the default document order.
SECTION_DEFS = [
    ("header", "Header", "Title bar with the report name and date."),
    ("banners", "Warning banners", "Data-quality, stale-price and alert banners."),
    ("money_cards", "Money cards", "Portfolio value, cost basis, net G/L, change."),
    ("analytics_cards", "Analytics cards", "TWR, 1-year return, XIRR, drawdown, volatility."),
    ("explainer", "Metric explainer", "Footnote defining each return metric."),
    ("verdict", "Verdict", "The one-line do-something verdict strip."),
    ("chart", "Chart", "Net P/L and benchmark chart image."),
    ("holdings", "Holdings table", "Per-symbol table with live prices."),
    ("closed_positions", "Closed positions", "Note listing positions closed since last run."),
    ("movers", "Biggest movers", "Top gainers and losers since the last checkup."),
    ("monthly", "Monthly returns", "Trailing 12 months of deposit-adjusted returns."),
    ("history", "Checkup history", "Archived record of previous checkups."),
    ("footer", "Footer", "Methodology note and generation time."),
]

SECTION_KEYS = [k for k, _label, _desc in SECTION_DEFS]
SECTION_LABELS = {k: label for k, label, _desc in SECTION_DEFS}
SECTION_DESCRIPTIONS = {k: desc for k, _label, desc in SECTION_DEFS}
DEFAULT_ORDER = {k: i for i, k in enumerate(SECTION_KEYS)}


# ---------------------------------------------------------------------------
# template store
# ---------------------------------------------------------------------------


def default_store():
    """A fresh store: every section enabled, in default order, at defaults."""
    return {
        "sections": {
            key: {"enabled": True, "html": DEFAULT_HTML[key]} for key in SECTION_KEYS
        },
        "order": list(SECTION_KEYS),
        "text": DEFAULT_TEXT,
    }


def load_store(lots_db):
    """The stored store, merged over the defaults.

    A section added in a later release gets its default rather than being
    missing; a section the user has customised keeps their version.
    """
    stored = lots_db.get_setting(SETTING_KEY) or {}
    store = default_store()
    if not isinstance(stored, dict):
        return store
    saved_sections = stored.get("sections") or {}
    for key, cfg in saved_sections.items():
        if key in store["sections"] and isinstance(cfg, dict):
            if "enabled" in cfg:
                store["sections"][key]["enabled"] = cfg["enabled"]
            if "html" in cfg:
                store["sections"][key]["html"] = cfg["html"]
    if isinstance(stored.get("text"), str):
        store["text"] = stored["text"]
    order = [k for k in (stored.get("order") or []) if k in store["sections"]]
    # Any section missing from a saved order (new in this release) keeps its
    # default position at the end, so nothing silently disappears.
    store["order"] = order + [k for k in SECTION_KEYS if k not in order]
    return store


def save_store(lots_db, store):
    lots_db.set_setting(SETTING_KEY, store)
    return store


def reset_section(lots_db, key):
    """Put one section back to its shipped default."""
    store = load_store(lots_db)
    if key not in store["sections"]:
        raise KeyError(key)
    store["sections"][key] = {"enabled": True, "html": DEFAULT_HTML[key]}
    return save_store(lots_db, store)


def reset_text(lots_db):
    """Put the plain-text template back to its shipped default."""
    store = load_store(lots_db)
    store["text"] = DEFAULT_TEXT
    return save_store(lots_db, store)


def reset_all(lots_db):
    return save_store(lots_db, default_store())


# ---------------------------------------------------------------------------
# environments
# ---------------------------------------------------------------------------


def _make_env():
    """HTML env.

    trim_blocks/lstrip_blocks are OFF. Templates are written as adjacent string
    literals with newlines only where one is wanted, so the tags introduce none
    of their own - but with trimming ON, Jinja also swallows a newline the
    template *deliberately* placed after a loop tag, which silently removes the
    row separators between table rows.
    """
    env = SandboxedEnvironment(
        autoescape=False,  # see module docstring: values are pre-built HTML
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=False,
    )
    env.filters["money"] = lambda v: fmt.money(v)
    env.filters["pct"] = lambda v, nd=1: fmt.signed_pct(v, nd)
    return env


def _make_text_env():
    """Text env. Block tags are NOT trimmed here: in the text part a newline is
    meaningful, and swallowing the one after {% endif %} runs lines together."""
    return SandboxedEnvironment(
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=False,
    )


_ENV = _make_env()
_TEXT_ENV = _make_text_env()


def render_template(source, data):
    """Render one section template. Raises jinja2.TemplateError on bad syntax."""
    return _ENV.from_string(source).render(**data)


def render_text_template(source, data):
    return _TEXT_ENV.from_string(source).render(**data)


# ---------------------------------------------------------------------------
# context builders
# ---------------------------------------------------------------------------


def _card(label, value, sub, color, width="25%"):
    return {
        "label": label,
        "value": value,
        "sub": sub,
        "color": color,
        "width": width,
        "cell": Markup(
            f'<td width="{width}" style="vertical-align:top;padding:8px 10px;'
            f"border:1px solid {fmt.BORDER};border-left:5px solid {color};"
            f'border-radius:8px;background:#ffffff">'
            f'<div style="font-size:12px;color:{fmt.MUT};letter-spacing:.03em;'
            f'text-transform:uppercase">{fmt.esc(label)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{fmt.INK};'
            f'margin-top:4px;white-space:nowrap">{value}</div>'
            f'<div style="font-size:12px;color:{fmt.MUT};margin-top:3px">{sub}</div></td>'
        ),
    }


def _build_header(ctx):
    return {
        "title": ctx.get("report_title", "Portfolio Checkup"),
        "date_label": fmt.day_label(ctx.get("today")),
    }


def _build_banners(ctx):
    last_close = ctx.get("last_close")
    return {
        "integrity": list(ctx.get("integrity") or []),
        "integrity_notes": list(ctx.get("integrity_notes") or []),
        "stale": bool(ctx.get("stale")),
        "last_close": last_close.strftime("%Y-%m-%d") if last_close is not None else "unknown",
        "stale_symbols": list(ctx.get("stale_symbols") or []),
        "alerts": list(ctx.get("alerts") or []),
    }


def _build_money_cards(ctx):
    s = ctx.get("summary") or {}
    prev = ctx.get("prev") or {}
    mv, cost = s.get("market_value"), s.get("cost_basis")
    net_gl, net_pct = s.get("net_gl"), s.get("net_gl_pct")
    delta_gl, delta_mv = prev.get("delta_gl"), prev.get("delta_mv")
    prev_date = prev.get("date")

    cards = [
        _card(
            "Portfolio Value",
            fmt.money(mv),
            f"{s.get('positions')} lots &middot; {s.get('symbols')} symbols",
            "#3b82f6",
        ),
        _card("Cost Basis", fmt.money(cost), "Total purchase cost", "#6366f1"),
        _card(
            "Net Gain / Loss",
            fmt.colored_money(net_gl),
            fmt.colored_money(net_pct, pct=True),
            fmt.POS if (net_gl or 0) >= 0 else fmt.NEG,
        ),
    ]
    if prev_date:
        cards.append(
            _card(
                f"Change vs {prev_date}",
                fmt.colored_money(delta_gl),
                "Mkt value " + fmt.colored_money(delta_mv, blank="-"),
                fmt.POS if (delta_gl or 0) >= 0 else fmt.NEG,
            )
        )
    else:
        cards.append(
            _card("Change since last checkup", "-", "Baseline set this run", fmt.MUT)
        )
    return {"cards": cards}


def _build_analytics_cards(ctx):
    returns_ctx = ctx.get("returns") or {}
    risk = ctx.get("risk") or {}
    risk12 = ctx.get("risk12") or {}
    bench = ctx.get("bench_ctx") or {}
    benchmark = ctx.get("benchmark", "VOO")
    since_date = ctx.get("since_date")

    twr = returns_ctx.get("twr")
    twr_ann = returns_ctx.get("twr_annualized")
    xirr = returns_ctx.get("xirr")
    mdd_incep = risk.get("max_drawdown")
    vol_incep = risk.get("volatility")
    mdd12 = risk12.get("max_drawdown")
    vol12 = risk12.get("volatility")
    sharpe12 = risk12.get("sharpe")
    bench_twr = bench.get("twr")
    bench_twr_ann = bench.get("twr_ann")
    bench_xirr = bench.get("xirr")
    yr1 = returns_ctx.get("yr1")
    yr1_bench = bench.get("yr1")

    def p100(v):
        return v * 100.0 if not fmt.is_blank(v) else None

    # The TWR card is a two-row sub-table, so it gets its own shape rather than
    # being forced through _card().
    twr_color = fmt.POS if (twr or 0) >= 0 else fmt.NEG
    twr_rows = [
        {
            "label": "Portfolio",
            "total": fmt.signed_pct(twr, nd=1) if not fmt.is_blank(twr) else "-",
            "ann": fmt.signed_pct(twr_ann) if not fmt.is_blank(twr_ann) else "n/a",
            "color": twr_color,
        }
    ]
    if bench_twr is not None:
        twr_rows.append(
            {
                "label": benchmark,
                "total": fmt.signed_pct(bench_twr),
                "ann": fmt.signed_pct(bench_twr_ann),
                "color": fmt.MUT,
            }
        )
    else:
        twr_rows.append(
            {
                "label": f"{benchmark} unavailable",
                "total": None,
                "ann": None,
                "color": fmt.MUT,
            }
        )
    twr_card = {
        "label": "Return (time-weighted)",
        "rows": twr_rows,
        "caption": "same window" + (f" &middot; since {fmt.esc(since_date)}" if since_date else ""),
        "color": twr_color,
        "width": "20%",
        "cell": Markup(
            f'<td width="20%" style="vertical-align:top;padding:8px 10px;'
            f"border:1px solid {fmt.BORDER};border-left:5px solid {twr_color};"
            f'border-radius:8px;background:#ffffff">'
            f'<div style="font-size:12px;color:{fmt.MUT};letter-spacing:.03em;'
            f'text-transform:uppercase">Return (time-weighted)</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:4px">'
            f"{''.join(_twr_row_html(r, i == 0) for i, r in enumerate(twr_rows))}</table>"
            f'<div style="font-size:11px;color:{fmt.MUT};margin-top:2px">'
            f"same window{(' &middot; since ' + fmt.esc(since_date)) if since_date else ''}</div></td>"
        ),
    }

    cards = [twr_card]
    cards.append(
        _card(
            "1-Year Return",
            fmt.colored_money(p100(yr1), pct=True) if not fmt.is_blank(yr1) else "-",
            (f"vs {fmt.esc(benchmark)} {fmt.signed_pct(yr1_bench)} (12m)")
            if yr1_bench is not None
            else "trailing 12 months",
            fmt.POS if (yr1 or 0) >= 0 else fmt.NEG,
            "20%",
        )
    )
    cards.append(
        _card(
            "XIRR (money-weighted)",
            fmt.colored_money(p100(xirr), pct=True) if not fmt.is_blank(xirr) else "-",
            (f"vs {fmt.esc(benchmark)} {fmt.signed_pct(bench_xirr)} same deposits")
            if bench_xirr is not None
            else "return per dollar invested",
            fmt.POS if (xirr or 0) >= 0 else fmt.NEG,
            "20%",
        )
    )
    cards.append(
        _card(
            "Max Drawdown (12m)",
            fmt.colored_money(p100(mdd12), pct=True) if not fmt.is_blank(mdd12) else "-",
            f"since {fmt.signed_pct(mdd_incep)} incl."
            if not fmt.is_blank(mdd_incep)
            else "peak to trough",
            fmt.NEG,
            "20%",
        )
    )
    cards.append(
        _card(
            "Volatility (12m)",
            fmt.money(p100(vol12), pct=True) if not fmt.is_blank(vol12) else "-",
            (
                "Sharpe(12m) "
                + (f"{sharpe12:.2f}" if not fmt.is_blank(sharpe12) else "n/a")
                + " &middot; vol "
                + fmt.signed_pct(vol_incep, nd=0)
            )
            if not fmt.is_blank(vol12)
            else "n/a",
            "#6366f1",
            "20%",
        )
    )
    return {"cards": cards}


def _twr_row_html(r, first):
    """One row of the TWR card's sub-table, or its 'unavailable' variant.

    The first row carries slightly tighter bottom padding, matching the original.
    """
    if r["total"] is None:
        return (
            f'<tr><td style="padding:3px 0;font-size:12px;color:{fmt.MUT}">'
            f'{fmt.esc(r["label"])}</td></tr>'
        )
    pad = "padding:3px 0 2px" if first else "padding:3px 0"
    return (
        "<tr>"
        f'<td style="{pad};font-size:12px;color:{fmt.MUT}">{fmt.esc(r["label"])}</td>'
        f'<td style="text-align:right;font-size:14px;font-weight:700;color:{r["color"]};'
        f'white-space:nowrap">{r["total"]}</td>'
        f'<td style="text-align:right;font-size:11px;color:{fmt.MUT};white-space:nowrap">'
        f"ann {r['ann']}</td>"
        "</tr>"
    )


def _build_explainer(ctx):
    exp = ctx.get("explainer") or {}
    lines = []
    if exp.get("twr_total") is not None:
        twr_t = exp["twr_total"]
        ann = f" (ann {fmt.signed_pct(exp.get('twr_ann'))})" if exp.get("twr_ann") is not None else ""
        lines.append(
            f"<b>Return (time-weighted) {fmt.signed_pct(twr_t)}</b>: strategy growth per dollar "
            f"that rode the full period: $1 invested at the start became "
            f"${1.0 + twr_t:,.2f}{ann}; timing-neutral, not the account's actual gain."
        )
    if (
        exp.get("xirr") is not None
        and exp.get("deposits_total") is not None
        and exp.get("final_value") is not None
    ):
        lines.append(
            f"<b>XIRR (money-weighted) {fmt.signed_pct(exp['xirr'])}</b>: your actual dollars "
            f"{fmt.money(exp['deposits_total'])} deposited grew to "
            f"{fmt.money(exp['final_value'])}, each dollar compounding "
            f"~{exp['xirr'] * 100:.1f}%/yr while invested."
        )
    if exp.get("dd12_pct") is not None:
        if exp.get("dd12_dollars") is not None:
            dd_text = (
                f"at the worst point the account was {fmt.money(abs(exp['dd12_dollars']))} "
                f"below its 12-month high ({fmt.money(exp['dd12_peak'])} peak &rarr; "
                f"{fmt.money(exp['dd12_trough'])} trough)"
            )
        else:
            dd_text = "the account fell off its 12-month high"
        lines.append(
            f"<b>Max Drawdown (12m) {fmt.signed_pct(exp['dd12_pct'])}</b>: {dd_text}, since recovered."
        )
    if exp.get("vol12_pct") is not None:
        vol_d = exp.get("vol_daily_dollars")
        daily_pct = exp["vol12_pct"] * 100.0 / (252.0 ** 0.5)
        tail = f"{fmt.money(vol_d)} (~{daily_pct:.1f}% of portfolio)" if vol_d else "n/a"
        lines.append(
            f"<b>Volatility (12m) {exp['vol12_pct'] * 100:.1f}%</b>: typical &plusmn;1&sigma; "
            f"daily move &asymp; {tail}."
        )
    return {"lines": lines}


def _build_verdict(ctx):
    v = ctx.get("verdict") or {}
    tone = v.get("tone", "ok")
    bg, bd, fg = fmt.TONE_STYLES.get(tone, fmt.TONE_STYLES["ok"])
    return {"text": v.get("text") or "", "tone": tone, "bg": bg, "border": bd, "fg": fg}


def _build_chart(ctx):
    benchmark = ctx.get("benchmark", "VOO")
    start = ctx.get("chart_window_start")
    port_ret = ctx.get("portfolio_window_return")
    bench_ret = ctx.get("benchmark_window_return")
    bench_total = ctx.get("benchmark_return")
    lead = "Top: net P/L vs lot purchase price. Bottom: portfolio vs " + fmt.esc(benchmark)
    if start is not None and port_ret is not None and bench_ret is not None:
        sub = (
            f"{lead} cumulative return since {start.strftime('%b %d %Y')} (rebased to 0): "
            f"portfolio {port_ret:+.1f}% vs {fmt.esc(benchmark)} {bench_ret:+.1f}%."
        )
    elif bench_total is not None:
        sub = f"{lead} cumulative return ({bench_total:+.1f}%)."
    else:
        sub = f"{lead} cumulative return."
    return {
        "src": ctx.get("chart_src", 'cid:attachment1'),
        "alt": "Portfolio net P/L chart",
        "sub_text": sub,
    }


def _build_holdings(ctx):
    holdings = ctx.get("holdings")
    alert_pos = float(ctx.get("alert_position_pct") or 25)
    prev_date = (ctx.get("prev") or {}).get("date")
    tot = float(holdings["MarketValue"].sum()) if holdings is not None and len(holdings) else 0.0

    rows = []
    if holdings is not None:
        for _, r in holdings.iterrows():
            sym = fmt.esc(r["Symbol"])
            desc = fmt.esc(r.get("Description", ""))
            last, dl = r.get("Last"), r.get("DeltaLast")
            is_new = bool(r.get("IsNew"))
            mv_v, c_v = r.get("MarketValue"), r.get("Cost")
            gl, pct, dgl = r.get("GainLoss"), r.get("GainLossPct"), r.get("DeltaGL")

            mv_pct = (float(mv_v) / tot * 100.0) if tot else 0.0
            over = mv_pct > alert_pos

            last_cell = fmt.money(last) if not fmt.is_blank(last) else "-"
            if is_new:
                last_cell += '<div style="font-size:11px;color:#b45309">new position</div>'
            elif not fmt.is_blank(dl) and abs(dl) >= 0.005:
                last_cell += f'<div style="font-size:11px">{fmt.colored_money(dl, blank="")}</div>'

            dgl_cell = "" if (fmt.is_blank(dgl) or abs(dgl) < 0.005) else fmt.colored_money(dgl, blank="")

            rows.append(
                {
                    "symbol": sym,
                    "description": desc,
                    "qty": r.get("Qty"),
                    "last": last_cell,
                    "market_value": fmt.money(mv_v),
                    "pct_value": f"{mv_pct:.1f}%",
                    "pct_value_style": ";color:#b45309;font-weight:700" if over else "",
                    "over_concentration": over,
                    "cost": fmt.money(c_v),
                    "gain_loss": fmt.colored_money(gl),
                    "gain_loss_pct": fmt.colored_money(pct, pct=True),
                    "delta_gl": dgl_cell,
                }
            )
    return {
        "rows": rows,
        "delta_column": f"&Delta; vs {prev_date}" if prev_date else "&Delta; since last",
        "empty_message": "No holdings.",
    }


def _build_closed(ctx):
    removed = ctx.get("removed") or []
    return {"names": ", ".join(str(s) for s, _ in removed)}


def _build_movers(ctx):
    holdings = ctx.get("holdings")
    prev_date = (ctx.get("prev") or {}).get("date")

    def top(n, positive):
        if holdings is None or not prev_date or not len(holdings):
            return []
        h = holdings.copy()
        h["Abs"] = h["DeltaGL"].abs()
        h = h.dropna(subset=["DeltaGL"]).sort_values("Abs", ascending=False)
        sub = h[h["DeltaGL"] > 0] if positive else h[h["DeltaGL"] < 0]
        return [
            {"symbol": fmt.esc(r["Symbol"]), "delta": fmt.colored_money(r["DeltaGL"])}
            for _, r in sub.head(n).iterrows()
        ]

    return {
        "gainers": top(4, True),
        "losers": top(4, False),
        "prev_date": prev_date,
        "gainers_title": f"&uarr; Biggest gainers since {prev_date}" if prev_date else "&uarr; Biggest gainers",
        "losers_title": f"&darr; Biggest losers since {prev_date}" if prev_date else "&darr; Biggest losers",
    }


def _build_monthly(ctx):
    monthly = ctx.get("monthly_returns") or []
    return {
        "rows": [
            {"month": fmt.esc(r["month"]), "pct": fmt.colored_money(r["pct"] * 100.0, pct=True)}
            for r in monthly
        ]
    }


def _build_history(ctx):
    history = ctx.get("history") or []
    return {
        "rows": [
            {
                "date": fmt.esc(r.get("Date")),
                "market_value": fmt.money(r.get("MarketValue")),
                "cost": fmt.money(r.get("Cost")),
                "gain_loss": fmt.colored_money(r.get("GainLoss")),
                "gain_loss_pct": fmt.colored_money(r.get("GainLossPct"), pct=True),
                "delta_gl": fmt.colored_money(r.get("DeltaGL"), blank="-"),
            }
            for r in history
        ],
        "empty_message": "History starts with this checkup.",
    }


def _build_footer(ctx):
    return {
        "generated": ctx.get("generated_time", ""),
        "dashboard_path": ctx.get("dashboard_path"),
    }


BUILDERS = {
    "header": _build_header,
    "banners": _build_banners,
    "money_cards": _build_money_cards,
    "analytics_cards": _build_analytics_cards,
    "explainer": _build_explainer,
    "verdict": _build_verdict,
    "chart": _build_chart,
    "holdings": _build_holdings,
    "closed_positions": _build_closed,
    "movers": _build_movers,
    "monthly": _build_monthly,
    "history": _build_history,
    "footer": _build_footer,
}


# Palette values available to every template, so a section can be restyled
# without hard-coding a hex code. Injected into each section's context by
# build_contexts, and overridable per section by a variable of the same name.
PALETTE = {
    "ink": fmt.INK,
    "mut": fmt.MUT,
    "border": fmt.BORDER,
    "band": fmt.BAND,
    "pos": fmt.POS,
    "neg": fmt.NEG,
}


def build_contexts(ctx):
    """Run every builder. The palette is merged in first so a builder can
    override any of it."""
    log = logging.getLogger(__name__)
    out = {}
    for key, fn in BUILDERS.items():
        try:
            out[key] = {**PALETTE, **fn(ctx)}
        except Exception as exc:  # a broken section must not kill the email
            log.warning("section %s failed to build its context: %s", key, exc)
            out[key] = dict(PALETTE)
    return out


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

# Fixed outer shell. Not user-editable: the width, the presentation role and the
# border/radius are what make the report render in Outlook, and a broken shell
# means an unreadable email rather than a slightly different one.
DOCUMENT_HEAD = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>"
    "<title>{{ report_title }}</title></head>"
    '<body style="margin:0;padding:0;background-color:#eef1f5">'
    '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
    'style="background-color:#eef1f5">'
    '<tr><td align="center" style="padding:16px 4px">'
    '<table width="780" cellpadding="0" cellspacing="0" role="presentation" '
    'style="max-width:780px;width:100%;background:#ffffff;border:1px solid {{ border }};'
    'border-radius:10px;font-family:Segoe UI,Arial,sans-serif;color:{{ ink }}">'
)
DOCUMENT_TAIL = "</table></td></tr></table></body></html>"


def section_visible(key, data):
    """Whether a section has anything to show.

    A section that is enabled but has no data renders as nothing rather than as
    an empty heading, which is how movers, monthly and the chart behaved before.
    """
    if key in ("movers",):
        return bool(data.get("prev_date")) and bool(data.get("gainers") or data.get("losers"))
    if key == "monthly":
        return bool(data.get("rows"))
    if key == "chart":
        return bool(data.get("src"))
    if key == "explainer":
        return bool(data.get("lines"))
    if key == "verdict":
        return bool(data.get("text"))
    if key == "banners":
        return bool(
            data.get("integrity")
            or data.get("integrity_notes")
            or data.get("alerts")
            or data.get("stale")
        )
    if key == "closed_positions":
        return bool(data.get("names"))
    if key == "holdings":
        return True
    return True


def validate(store, only_enabled=False):
    """Compile the templates without rendering.

    Returns a list of "label: error" strings; empty means everything parses.
    Parsing is the check that matters for an editor - it catches the mistake
    actually made while typing (a missing %} or a bad filter) without needing a
    report context, and without rendering a template thirteen times just to see
    whether it compiles.

    only_enabled=False checks every section, which is what the editor wants: a
    broken template is still shown in the form, so the error is real even while
    the section is switched off. only_enabled=True checks just what would
    actually be sent, which is what a test send should care about.
    """
    problems = []
    for key in store.get("order") or SECTION_KEYS:
        cfg = (store.get("sections") or {}).get(key) or {}
        enabled = cfg.get("enabled", True)
        if only_enabled and not enabled:
            continue
        source = cfg.get("html") or ""
        if not source.strip():
            continue
        try:
            _ENV.from_string(source)
        except Exception as exc:
            problems.append(f"{SECTION_LABELS.get(key, key)}: {exc}")
    try:
        _TEXT_ENV.from_string(store.get("text") or "")
    except Exception as exc:
        problems.append(f"Plain text: {exc}")
    return problems


def render_parts(ctx, store):
    """Render each enabled section on its own and return {key: html}.

    Split out from render_html so a caller can check whether anything actually
    rendered. The web UI refuses to send a test email whose sections all came
    out empty, which is the failure you get from a half-submitted editor form.
    """
    contexts = build_contexts(ctx)
    log = logging.getLogger(__name__)
    out = {}
    for key in [k for k in store["order"] if k in store["sections"]]:
        cfg = store["sections"][key]
        if not cfg.get("enabled", True):
            continue
        data = contexts.get(key) or {}
        if not section_visible(key, data):
            continue
        try:
            out[key] = render_template(cfg["html"], data)
        except Exception as exc:
            # One bad template must not take the whole report down, and the
            # comment makes the omission obvious when reading the received mail.
            log.error("section %s failed to render: %s", key, exc)
            out[key] = f"<!-- section {key} failed to render: {exc} -->"
    return out


def assemble(ctx, parts):
    """Wrap rendered section HTML in the document scaffolding.

    Takes the {key: html} mapping from render_parts so a caller can inspect what
    rendered and then assemble it, rather than rendering the expensive report
    contexts twice.
    """
    head = render_template(
        DOCUMENT_HEAD,
        {
            "report_title": ctx.get("report_title", "Portfolio Checkup"),
            "border": fmt.BORDER,
            "ink": fmt.INK,
        },
    )
    return "".join([head, *parts.values(), DOCUMENT_TAIL])


def render_html(ctx, store):
    """Render the whole email from the section store."""
    return assemble(ctx, render_parts(ctx, store))


def render_text(ctx, store):
    """Render the plain-text alternative from the single text template.

    Section enable/disable is honoured through the `on.<key>` flags rather than
    by splitting the text into per-section pieces - see DEFAULT_TEXT.
    """
    log = logging.getLogger(__name__)
    try:
        out = render_text_template(
            store.get("text") or DEFAULT_TEXT, build_text_context(ctx, store)
        )
    except Exception as exc:
        log.error("plain-text template failed to render: %s", exc)
        return ""
    # Collapse the blank lines the {% if %} guards leave behind, and normalise
    # line endings to CRLF as before.
    lines = [ln.rstrip() for ln in out.replace("\r\n", "\n").split("\n")]
    collapsed = [ln for i, ln in enumerate(lines) if ln or (i + 1 < len(lines) and lines[i + 1])]
    return "\r\n".join(collapsed).strip()


def store_as_json(store):
    return json.dumps(store, indent=2)


# ---------------------------------------------------------------------------
# Default templates
#
# These reproduce the pre-refactor email exactly. They double as the reset
# target, so a user can always get back to a known-good report. Available
# variables are listed per section in the web editor.
# ---------------------------------------------------------------------------

DEFAULT_HTML = {
    "header": (
        '<tr><td style="background:{{ ink }};padding:16px 24px;border-radius:10px 10px 0 0">'
        '<div style="color:#ffffff;font-size:24px;font-weight:700">{{ title }}</div>'
        '<div style="color:#9ca3af;font-size:13px;margin-top:2px">{{ date_label }}</div>'
        "</td></tr>"
    ),
    "banners": (
        "{% if integrity %}"
        '<tr><td style="background:#fffbeb;border-bottom:1px solid #fcd34d;padding:10px 24px;'
        'color:#92400e;font-size:13px;font-weight:600">&#9888; Data quality</td></tr>'
        '<tr><td style="background:#fffbeb;border-bottom:1px solid #fcd34d;padding:2px 24px 10px;'
        'color:#92400e;font-size:12px">'
        "{% for item in integrity %}<div>&bull; {{ item }}</div>{% endfor %}"
        "</td></tr>"
        "{% endif %}"
        "{% if stale %}"
        '<tr><td style="background:#fffbeb;border-bottom:1px solid #fcd34d;padding:10px 24px;'
        'color:#92400e;font-size:13px;font-weight:600">&#9888; Market data is stale - '
        "latest close was {{ last_close }}"
        "{% if stale_symbols %} (symbols: {{ stale_symbols|join(', ') }}){% endif %}"
        "</td></tr>"
        "{% endif %}"
        "{% if alerts %}"
        '<tr><td style="background:#fef2f2;border-bottom:1px solid #fecaca;padding:10px 24px;'
        'color:#991b1b;font-size:13px;font-weight:600">&#9888; Attention</td></tr>'
        '<tr><td style="background:#fef2f2;border-bottom:1px solid #fecaca;padding:2px 24px 10px;'
        'color:#991b1b;font-size:12px">'
        "{% for item in alerts %}<div>&bull; {{ item }}</div>{% endfor %}"
        "</td></tr>"
        "{% endif %}"
        # Expected drift, so deliberately not styled as a warning. Emits nothing
        # at all when empty, which is what keeps the byte-identity gate honest.
        "{% if integrity_notes %}"
        '<tr><td style="background:#f8fafc;border-bottom:1px solid #e2e8f0;padding:10px 24px;'
        'color:#475569;font-size:13px;font-weight:600">Ledger vs FT CSV</td></tr>'
        '<tr><td style="background:#f8fafc;border-bottom:1px solid #e2e8f0;padding:2px 24px 10px;'
        'color:#475569;font-size:12px">'
        "{% for item in integrity_notes %}<div>&bull; {{ item }}</div>{% endfor %}"
        "</td></tr>"
        "{% endif %}"
    ),
    "money_cards": (
        '<tr><td style="background:#ffffff;padding:12px 10px 6px">'
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
        "<tr>{% for card in cards %}{{ card.cell }}{% endfor %}</tr>"
        "</table></td></tr>"
    ),
    "analytics_cards": (
        '<tr><td style="background:#ffffff;padding:0 10px 6px">'
        '<table width="100%" cellpadding="0" cellspacing="0" role="presentation">'
        "<tr>{% for card in cards %}{{ card.cell }}{% endfor %}</tr>"
        "</table></td></tr>"
    ),
    "explainer": (
        "<tr><td style=\"background:#f9fafb;border-top:1px solid {{ border }};"
        "padding:8px 24px;color:{{ mut }};font-size:11px;line-height:1.65\">"
        "{% for line in lines %}{{ line }}{% if not loop.last %}<br/>{% endif %}{% endfor %}"
        "</td></tr>"
    ),
    "verdict": (
        "<tr><td style=\"background:{{ bg }};border-top:1px solid {{ border }};"
        "padding:12px 24px;color:{{ fg }};font-size:15px;font-weight:700\">"
        "&#9656; {{ text }}</td></tr>"
    ),
    "chart": (
        '<tr><td style="background:#ffffff;padding:14px 24px;border-top:1px solid {{ border }}">'
        '<div style="font-size:16px;font-weight:700;color:{{ ink }}">'
        "Portfolio Net P/L: Past Year</div>"
        '<div style="font-size:12px;color:{{ mut }};margin:2px 0 10px">{{ sub_text }}</div>'
        '<img src="{{ src }}" alt="{{ alt }}" width="100%" '
        'style="display:block;max-width:724px;height:auto;border:1px solid {{ border }};'
        'border-radius:6px"/>'
        "</td></tr>"
    ),
    "holdings": (
        '<tr><td style="background:#ffffff;padding:14px 24px;border-top:1px solid {{ border }}">'
        '<div style="font-size:16px;font-weight:700;color:{{ ink }}">Holdings by Symbol</div>'
        '<div style="font-size:12px;color:{{ mut }};margin:2px 0 8px">'
        "Live prices from yfinance; sorted by net gain.</div>"
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:13px">'
        "<thead><tr>"
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600">Symbol</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Qty</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Last</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Mkt Value</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">% Value</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Cost</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">G/L $</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">G/L %</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">{{ delta_column }}</td>'
        "</tr></thead><tbody>"
        "{% for row in rows %}\n"
        "<tr>"
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }}">'
        '<div style="font-weight:700;color:{{ ink }};white-space:nowrap">{{ row.symbol }}</div>'
        '<div style="font-size:11px;color:{{ mut }}">{{ row.description }}</div></td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.qty }}</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.last }}</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.market_value }}</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap{{ row.pct_value_style }}">{{ row.pct_value }}</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.cost }}</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.gain_loss }}</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.gain_loss_pct }}</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.delta_gl }}</td>'
        "</tr>"
        "{% else %}"
        '<tr><td colspan="9" style="padding:10px;color:{{ mut }}">{{ empty_message }}</td></tr>'
        "{% endfor %}"
        "</tbody></table></td></tr>"
    ),
    "closed_positions": (
        '<tr><td style="background:#ffffff;padding:4px 24px 14px;color:{{ neg }};font-size:12px">'
        "Positions closed since last checkup: {{ names }}</td></tr>"
    ),
    "movers": (
        '<tr><td style="background:#ffffff;padding:14px 24px;border-top:1px solid {{ border }}">'
        '<div style="font-size:16px;font-weight:700;color:{{ ink }}">Biggest Movers</div>'
        '<div style="font-size:12px;color:{{ mut }};margin:2px 0 8px">'
        "Change in gain/loss since last checkup.</div>"
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        '<td width="50%" style="vertical-align:top;padding-right:6px">'
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:13px;border:1px solid {{ border }};'
        'border-radius:8px">'
        '<tr><td colspan="2" style="padding:7px 10px;background:{{ pos }};color:#fff;'
        'font-weight:700;font-size:13px">{{ gainers_title }}</td></tr>'
        "{% for row in gainers %}\n"
        "<tr>"
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};white-space:nowrap;'
        'font-weight:600">{{ row.symbol }}</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.delta }}</td>'
        "</tr>"
        "{% else %}"
        '<tr><td colspan="2" style="padding:7px 10px;color:{{ mut }}">None</td></tr>'
        "{% endfor %}"
        "</table></td>"
        '<td width="50%" style="vertical-align:top;padding-left:6px">'
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:13px;border:1px solid {{ border }};'
        'border-radius:8px">'
        '<tr><td colspan="2" style="padding:7px 10px;background:{{ neg }};color:#fff;'
        'font-weight:700;font-size:13px">{{ losers_title }}</td></tr>'
        "{% for row in losers %}\n"
        "<tr>"
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};white-space:nowrap;'
        'font-weight:600">{{ row.symbol }}</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.delta }}</td>'
        "</tr>"
        "{% else %}"
        '<tr><td colspan="2" style="padding:7px 10px;color:{{ mut }}">None</td></tr>'
        "{% endfor %}"
        "</table></td>"
        "</tr></table></td></tr>"
    ),
    "monthly": (
        '<tr><td style="background:#ffffff;padding:14px 24px;border-top:1px solid {{ border }}">'
        '<div style="font-size:16px;font-weight:700;color:{{ ink }}">Monthly Returns</div>'
        '<div style="font-size:12px;color:{{ mut }};margin:2px 0 8px">'
        "Deposit-adjusted (time-weighted) monthly performance.</div>"
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:13px">'
        "<thead><tr>"
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600">Month</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Return</td>'
        "</tr></thead><tbody>"
        "{% for row in rows %}\n"
        "<tr>"
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};white-space:nowrap">'
        "{{ row.month }}</td>"
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.pct }}</td>'
        "</tr>"
        "{% endfor %}"
        "</tbody></table></td></tr>"
    ),
    "history": (
        '<tr><td style="background:#ffffff;padding:14px 24px;border-top:1px solid {{ border }}">'
        '<div style="font-size:16px;font-weight:700;color:{{ ink }}">Checkup History</div>'
        '<div style="font-size:12px;color:{{ mut }};margin:2px 0 8px">'
        "Documented record of each checkup run.</div>"
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:13px">'
        "<thead><tr>"
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600">Checkup</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Mkt Value</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Cost</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">Net G/L</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        'text-align:right">G/L %</td>'
        '<td style="padding:7px 10px;background:{{ ink }};color:#fff;font-weight:600;'
        "text-align:right\">&Delta; G/L</td>"
        "</tr></thead><tbody>"
        "{% for row in rows %}\n"
        "<tr>"
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};white-space:nowrap">'
        "{{ row.date }}</td>"
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.market_value }}</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.cost }}</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.gain_loss }}</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.gain_loss_pct }}</td>'
        '<td style="padding:6px 10px;border-bottom:1px solid {{ border }};text-align:right;'
        'white-space:nowrap">{{ row.delta_gl }}</td>'
        "</tr>"
        "{% else %}"
        '<tr><td colspan="6" style="padding:8px;color:{{ mut }}">{{ empty_message }}</td></tr>'
        "{% endfor %}"
        "</tbody></table></td></tr>"
    ),
    "footer": (
        '<tr><td style="background:{{ band }};color:{{ mut }};font-size:12px;padding:12px 24px;'
        'border-top:1px solid {{ border }};border-radius:0 0 10px 10px">'
        "Market values and gains are computed from lot data (purchase date, price, quantity) "
        "priced at the latest yfinance close. Returns are time-weighted; XIRR is money-weighted. "
        "Risk figures are trailing 12 months unless marked since inception. "
        "Change columns compare against the previous checkup's archived record, "
        "re-priced on the same basis. Generated {{ generated }}."
        "{% if dashboard_path %} Full-resolution dashboard: <code>{{ dashboard_path }}</code>.{% endif %}"
        "</td></tr>"
    ),
}


# ---------------------------------------------------------------------------
# Plain-text alternative
#
# Deliberately ONE template rather than thirteen. The text part is a fallback
# for text-only mail clients; it is a concise summary, not a mirror of the HTML.
# Thirteen separate text templates meant maintaining a second set of plain-value
# card contexts and produced markedly worse output. One seeded with the
# long-standing wording keeps it good, and `on.<section>` guards mean switching a
# section off in the HTML switches its line off here too.
# ---------------------------------------------------------------------------

DEFAULT_TEXT = """Portfolio Checkup - {{ date_label }}

Portfolio Value : {{ market_value }}
Cost Basis      : {{ cost_basis }}
Net Gain/Loss   : {{ net_gl }} ({{ net_gl_pct }})
{% if prev_date %}Change vs {{ prev_date }}: {{ delta_gl }}
{% endif %}
{% if on.verdict and verdict_text %}VERDICT: {{ verdict_text }}

{% endif %}
{% if on.analytics_cards and twr is not none %}Return (time-weighted): {{ twr }} (ann {{ twr_ann }})
{% if bench_twr is not none %}  vs {{ benchmark }}: {{ bench_twr }}{% if bench_twr_ann %} (ann {{ bench_twr_ann }}){% endif %} (same window)
{% endif %}XIRR (money-weighted) : {{ xirr }}{% if bench_xirr is not none %}
  vs {{ benchmark }}: {{ bench_xirr }} (same deposits){% endif %}
Max Drawdown (12m)    : {{ mdd12 }}{% if mdd_incep is not none %} (since: {{ mdd_incep }}){% endif %}
Volatility (12m)      : {{ vol12 }}
Sharpe (12m)          : {{ sharpe12 }}
{% if yr1 is not none %}1-Year Return (12m)   : {{ yr1 }}{% if yr1_bench is not none %} vs {{ benchmark }} {{ yr1_bench }}{% endif %}
{% endif %}{% endif %}
{% if on.explainer and explainer_lines %}{{ explainer_lines }}
{% endif %}{% if drawdown_attribution %}Drawdown culprits     : {{ drawdown_attribution }}
{% endif %}{% if on.banners %}{% for item in integrity %}DATA CHECK            : {{ item }}
{% endfor %}{% if stale %}NOTE: market data is stale - latest close was {{ last_close }}.
{% endif %}{% for item in alerts %}ATTENTION            : {{ item }}
{% endfor %}{% endif %}
See the HTML part of this message for the full holdings table.
Returns are time-weighted from lot data; XIRR is money-weighted. Risk figures are trailing 12 months. Priced at the latest yfinance close.
{% if dashboard_path %}Full-resolution dashboard: {{ dashboard_path }}{% endif %}"""


class _SectionFlags(dict):
    """`on.holdings` in a template - True when that section is enabled."""

    def __init__(self, store):
        super().__init__(
            {k: bool(cfg.get("enabled", True)) for k, cfg in store["sections"].items()}
        )

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def build_text_context(ctx, store):
    """Plain values for the text template. Deliberately mirrors the wording the
    text part has always used, so the default template reads as before."""
    s = ctx.get("summary") or {}
    prev = ctx.get("prev") or {}
    returns_ctx = ctx.get("returns") or {}
    risk = ctx.get("risk") or {}
    risk12 = ctx.get("risk12") or {}
    bench = ctx.get("bench_ctx") or {}
    exp = ctx.get("explainer") or {}
    last_close = ctx.get("last_close")

    def p100(v):
        return v * 100.0 if not fmt.is_blank(v) else None

    def pct(v, signed=True):
        if fmt.is_blank(v):
            return None
        return f"{v * 100.0:+,.2f}%" if signed else f"{v * 100.0:,.2f}%"

    lines = []
    if exp.get("twr_total") is not None:
        ann = (
            f" (ann {exp['twr_ann'] * 100:+.1f}%/yr)"
            if exp.get("twr_ann") is not None
            else ""
        )
        lines.append(
            f"TWR: $1 riding the strategy for the whole period became "
            f"${1.0 + exp['twr_total']:,.2f}{ann} (timing-neutral)."
        )
    if (
        exp.get("xirr") is not None
        and exp.get("deposits_total") is not None
        and exp.get("final_value") is not None
    ):
        lines.append(
            f"XIRR: your {fmt.money(exp['deposits_total'])} deposited grew to "
            f"{fmt.money(exp['final_value'])}, each dollar compounding "
            f"~{exp['xirr'] * 100:.1f}%/yr while invested."
        )
    if exp.get("dd12_pct") is not None:
        if exp.get("dd12_dollars") is not None:
            dd = (
                f"at the worst point the account was {fmt.money(abs(exp['dd12_dollars']))} "
                f"below its 12-month high ({fmt.money(exp['dd12_peak'])} peak -> "
                f"{fmt.money(exp['dd12_trough'])} trough)"
            )
        else:
            dd = "the account fell off its 12-month high"
        lines.append(f"Max Drawdown (12m): {dd}. since recovered.")
    if exp.get("vol12_pct") is not None:
        vol_d = exp.get("vol_daily_dollars")
        daily_pct = exp["vol12_pct"] * 100.0 / (252.0 ** 0.5)
        tail = f"{fmt.money(vol_d)} (~{daily_pct:.1f}% of portfolio)" if vol_d else "n/a"
        lines.append(
            f"Volatility (12m): typical +/-1 sigma daily move ~= {tail}."
        )

    dd_attr = ctx.get("drawdown_attribution") or []
    return {
        "on": _SectionFlags(store),
        "date_label": fmt.day_label(ctx.get("today")),
        "market_value": fmt.money(s.get("market_value")),
        "cost_basis": fmt.money(s.get("cost_basis")),
        "net_gl": fmt.money(s.get("net_gl")),
        # net_gl_pct is already a percentage in the context - do not rescale it.
        "net_gl_pct": fmt.money(s.get("net_gl_pct"), pct=True),
        "prev_date": prev.get("date"),
        "delta_gl": fmt.money(prev.get("delta_gl")),
        "verdict_text": (ctx.get("verdict") or {}).get("text"),
        "benchmark": ctx.get("benchmark", "VOO"),
        "twr": pct(returns_ctx.get("twr"), signed=False),
        "twr_ann": pct(returns_ctx.get("twr_annualized"), signed=False),
        "bench_twr": pct(bench.get("twr")),
        "bench_twr_ann": pct(bench.get("twr_ann")),
        "xirr": pct(returns_ctx.get("xirr"), signed=False),
        "bench_xirr": pct(bench.get("xirr")),
        "yr1": pct(returns_ctx.get("yr1"), signed=False),
        "yr1_bench": pct(bench.get("yr1")),
        "mdd12": pct(risk12.get("max_drawdown")),
        "mdd_incep": pct(risk.get("max_drawdown")),
        "vol12": pct(risk12.get("volatility"), signed=False),
        "sharpe12": f"{risk12.get('sharpe', 0):.2f}" if risk12.get("sharpe") is not None else "n/a",
        "explainer_lines": "\n".join(lines),
        "drawdown_attribution": "; ".join(
            f"{d['symbol']} {d['pct_change']:+.1%} (${d['dollar']:,.0f})" for d in dd_attr
        ),
        "integrity": list(ctx.get("integrity") or []),
        "stale": bool(ctx.get("stale")),
        "last_close": last_close.strftime("%Y-%m-%d") if last_close is not None else "unknown",
        "alerts": list(ctx.get("alerts") or []),
        "dashboard_path": ctx.get("dashboard_path"),
    }
