"""Shared formatting helpers and the email palette.

Extracted from email_utils so the section templates (sections.py) and the
sending code (email_utils.py) format money and colours identically. Keeping one
copy is what guarantees a hand-edited section and a generated one look alike.
"""
import math

POS = "#15803d"  # green
NEG = "#b91c1c"  # red
INK = "#1f2937"  # dark slate
MUT = "#6b7280"  # gray
BORDER = "#e5e7eb"
BAND = "#f9fafb"

# Verdict strip tones: (background, border, foreground)
TONE_STYLES = {
    "action": ("#fef2f2", "#fecaca", "#991b1b"),
    "caution": ("#fffbeb", "#fcd34d", "#92400e"),
    "ok": ("#f0fdf4", "#bbf7d0", "#166534"),
}


def is_blank(v):
    if v is None:
        return True
    if isinstance(v, float):
        return math.isnan(v)
    return False


# Backwards-compatible private alias used inside email_utils.
_is_blank = is_blank


def money(v, signed=False, pct=False):
    if is_blank(v):
        return "-"
    if pct:
        s = f"{v:+,.2f}%" if signed else f"{v:,.2f}%"
    else:
        s = (
            ("+$" if v >= 0 else "-$") + f"{abs(v):,.2f}"
            if signed
            else "$" + f"{v:,.2f}"
        )
    return s


def colored_money(v, pct=False, blank="-"):
    if is_blank(v):
        return blank
    color = POS if v >= 0 else NEG
    s = money(v, signed=True, pct=pct)
    return f'<span style="color:{color};font-weight:600;white-space:nowrap">{s}</span>'


def esc(v):
    """Stringify for interpolation.

    Deliberately does NOT html-escape, matching the email's long-standing
    behaviour: values passed in are already-built HTML fragments (entities like
    &Delta;, coloured <span>s, money strings). Escaping here would double-encode
    them. Section templates are rendered with autoescape off for the same reason.
    """
    return str(v) if v is not None else ""


def day_label(day):
    return day.strftime("%A, %d. %B %Y") if hasattr(day, "strftime") else str(day)


def month_label(day):
    return day.strftime("%b %d") if hasattr(day, "strftime") else str(day)


def signed_pct(v, nd=1):
    return f"{v * 100.0:+,.{nd}f}%" if v is not None else "n/a"
