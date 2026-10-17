import logging
import os
from pathlib import Path
from datetime import datetime as dt

import pandas as pd
import boto3
import dotenv
from botocore.exceptions import ClientError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage

# Try container-friendly path first (mounted project at /app), then fall back to default search.
# exists() is guarded: when not running as root, stat()ing /root/.env raises
# PermissionError rather than returning False, which would abort the import.
env_path = Path("/root/.env")
try:
    has_env = env_path.is_file()
except OSError:
    has_env = False
if has_env:
    dotenv.load_dotenv(dotenv_path=str(env_path))
else:
    dotenv.load_dotenv()
# Palette and formatting helpers now live in email_fmt so the section templates and
# the sending code share one implementation. Re-exported here because email_utils
# is imported directly by main.py.
from email_fmt import (  # noqa: E402,F401  (imported after .env is loaded)
    BAND,
    BORDER,
    INK,
    MUT,
    NEG,
    POS,
    TONE_STYLES,
    _is_blank,
    colored_money as _colored_money,
    day_label as _day_label,
    esc as _esc,
    is_blank,
    money as _money,
    month_label as _month_label,
    signed_pct as _signed_pct,
)

import lots_db  # noqa: E402  (imported after .env is loaded)
import sections as sections_mod  # noqa: E402  (same)


def _load_store():
    """The user's section templates, or the shipped defaults if unset."""
    try:
        return sections_mod.load_store(lots_db)
    except Exception as exc:  # a settings problem must not stop the email
        import logging

        logging.getLogger(__name__).error(
            "could not load template settings (%s); using defaults", exc
        )
        return sections_mod.default_store()


def render_email_html(ctx, store=None):
    """Render the email HTML from the section templates."""
    return sections_mod.render_html(ctx, store if store is not None else _load_store())


def _build_plain_text(ctx, store=None):
    """Render the plain-text alternative from the section templates."""
    return sections_mod.render_text(ctx, store if store is not None else _load_store())


def _chart_source(ctx, out_path=None):
    """How the chart image should be referenced.

    The email attaches the PNG inline as cid:attachment1. The dashboard and the
    web preview have no attachment, so they point at a data URI instead.
    """
    return ctx.get("chart_src", "cid:attachment1")



def write_dashboard_html(context, out_path):
    """Render the full email HTML with the chart embedded (base64) into a
    standalone, shareable dashboard file for local viewing."""
    body = render_email_html(context)
    chart = context.get("graph_path", "total_net.png")
    if os.path.isfile(chart):
        import base64

        with open(chart, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        body = body.replace('src="cid:attachment1"', f'src="data:image/png;base64,{b64}"')
    out_path = Path(out_path)
    out_path.write_text(body, encoding="utf-8")
    return str(out_path)


def build_message(context, html=None, text=None, subject=None):
    """Assemble the MIME message for the checkup.

    html/text/subject default to what the cron email would send, but the web UI
    passes the editor's current unsaved body so a test send is exactly what is
    on screen.
    """
    SENDER = lots_db.runtime_setting("SES_SENDER", "no-reply@example.com")
    RECIPIENT = lots_db.runtime_setting("SES_RECIPIENT", "you@example.com")
    if subject is None:
        subject = "Portfolio Checkup - " + dt.today().strftime("%A %d. %B %Y")

    CSV_PATH = context.get("csv_path", "FT_GainLoss.csv")
    IMAGE_PATH = context.get("graph_path", "total_net.png")

    BODY_HTML = html if html is not None else render_email_html(context)
    BODY_TEXT = text if text is not None else _build_plain_text(context)

    CHARSET = "utf-8"
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = SENDER
    msg["To"] = RECIPIENT

    msg_body = MIMEMultipart("alternative")
    msg_body.attach(MIMEText(BODY_TEXT.encode(CHARSET), "plain", CHARSET))
    msg_body.attach(MIMEText(BODY_HTML.encode(CHARSET), "html", CHARSET))
    msg.attach(msg_body)

    if os.path.isfile(CSV_PATH):
        with open(CSV_PATH, "rb") as f:
            csv_part = MIMEApplication(f.read())
        csv_part.add_header(
            "Content-Disposition", "attachment", filename=os.path.basename(CSV_PATH)
        )
        msg.attach(csv_part)

    if os.path.isfile(IMAGE_PATH):
        with open(IMAGE_PATH, "rb") as f:
            img_part = MIMEImage(f.read())
        img_part.add_header(
            "Content-Disposition", "inline", filename=os.path.basename(IMAGE_PATH)
        )
        img_part.add_header("Content-ID", "<attachment1>")
        msg.attach(img_part)

    return msg


def deliver(msg, dry_run=False, kind="test"):
    """Hand the message to SES. Returns (ok, detail).

    Every attempt is recorded in the send_log table, including the ones that
    send nothing. Container logs cannot do that job: recreating the container
    discards them, so a question like "how many emails did that button just
    send me" ends up unanswerable. Nothing else is written to disk here, so a
    DRY_RUN still leaves no preview.html behind and the web UI's test send keeps
    its no-side-effects property.
    """
    log = logging.getLogger(__name__)
    AWS_REGION = lots_db.runtime_setting("AWS_REGION", "eu-north-1")
    SENDER = lots_db.runtime_setting("SES_SENDER", "no-reply@example.com")
    RECIPIENT = lots_db.runtime_setting("SES_RECIPIENT", "you@example.com")
    subject = str(msg.get("Subject") or "")

    def record(ok, message_id="", detail=""):
        lots_db.record_send(
            kind=kind,
            subject=subject,
            message_id=message_id,
            ok=ok,
            dry_run=dry_run,
            detail=detail,
        )

    if dry_run:
        detail = "DRY_RUN is set, so nothing was actually sent."
        log.info("send (%s) suppressed by DRY_RUN: %s", kind, subject)
        record(True, detail=detail)
        return True, detail

    body = bytes(str(msg), "utf-8")

    aws_key = os.environ.get("AWS_KEY")
    aws_secret = os.environ.get("AWS_SECRET")

    client_kwargs = {"region_name": AWS_REGION}
    if aws_key and aws_secret:
        client_kwargs.update(
            {"aws_access_key_id": aws_key, "aws_secret_access_key": aws_secret}
        )

    try:
        client = boto3.client("sesv2", **client_kwargs)
    except Exception as e:
        detail = f"Could not create an SES client: {e}"
        log.error("send (%s) failed before reaching SES: %s", kind, e)
        record(False, detail=detail)
        return False, detail

    try:
        response = client.send_email(
            FromEmailAddress=SENDER,
            Destination={"ToAddresses": [RECIPIENT]},
            Content={"Raw": {"Data": body}},
        )
        message_id = str(response.get("MessageId") or "")
        ok = response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
        detail = (
            f"SES accepted the message for {RECIPIENT}."
            if ok
            else f"SES returned an unexpected status: {response.get('ResponseMetadata')}"
        )
        if ok:
            log.info(
                "send (%s) accepted by SES: id=%s subject=%r recipient=%s",
                kind,
                message_id,
                subject,
                RECIPIENT,
            )
        else:
            log.error("send (%s) got an unexpected SES status: %s", kind, detail)
        record(ok, message_id=message_id, detail=detail)
        return ok, detail
    except ClientError as e:
        detail = f"SES rejected the message: {e}"
        log.error("send (%s) rejected by SES: %s", kind, e)
        record(False, detail=detail)
        return False, detail


def email(context, html=None, text=None, subject=None, dry_run_preview=True):
    """Build and send the checkup email, as the cron job always has.

    The history snapshot is written by main(), not here, so callers that only
    want to send (the web UI's test send) cannot accidentally archive.
    """
    dry_run = str(lots_db.runtime_setting("DRY_RUN", "1")).lower() in ("1", "true", "yes")
    if html is None:
        html = render_email_html(context)
    if text is None:
        text = _build_plain_text(context)

    msg = build_message(context, html=html, text=text, subject=subject)
    ok, _detail = deliver(msg, dry_run=dry_run, kind="cron")
    if not ok:
        return False
    if dry_run and dry_run_preview:
        path = Path("preview.html")
        path.write_text(html, encoding="utf-8")
        print("DRY RUN enabled - email not sent. Subject:", msg["Subject"])
        print("Wrote HTML preview to", path.resolve())
        print("Plain text preview:\n", text)
    return True


if __name__ == "__main__":
    email(
        {
            "today": dt.now(),
            "summary": {
                "market_value": 0.0,
                "cost_basis": 0.0,
                "net_gl": 0.0,
                "net_gl_pct": 0.0,
                "positions": 0,
                "symbols": 0,
            },
            "prev": {"date": None},
            "holdings": pd.DataFrame(),
            "history": [],
            "removed": [],
            "csv_path": "FT_GainLoss.csv",
            "graph_path": "total_net.png",
        }
    )
