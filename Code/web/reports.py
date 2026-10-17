"""Building and caching the report context for previews and test sends.

compute_context() is the expensive part: it pulls daily closes from yfinance and
runs the analytics and the matplotlib chart, which takes about six seconds. That
is fine for a page load but not for six taps on a phone, so the context is cached
in memory and shared, with one lock so simultaneous requests do not each start
their own download.

A preview must not disturb the scheduled email. That means: no history snapshot
(compute_context never writes one), no rewritten dashboard.html, no exported lots
CSV, and the chart written to a temporary directory rather than over the PNG the
cron job produces.
"""

import base64
import logging
import os
import tempfile
import threading
import time
from datetime import date as date_cls
from pathlib import Path

import lots_db
import main
import sections

log = logging.getLogger(__name__)

# How long a computed context stays usable. Long enough that flicking between
# the editor and the lots list does not refetch, short enough that an afternoon
# of editing sees new prices.
CACHE_TTL_SECONDS = int(os.environ.get("WEB_PREVIEW_TTL", "300"))


class _ContextCache:
    """One entry, rebuilt at most every CACHE_TTL_SECONDS."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ctx = None
        self._built_at = 0.0
        self._key = None
        self._tmpdir = None
        self._building = False

    def _data_key(self, today, benchmark):
        """What the report depends on: the clock, the benchmark, and the bytes of
        both the ledger and the CSV it can fall back to.

        Editing a lot has to change this, or the next preview serves a portfolio
        the user has already changed. Three signals are combined because no single
        one is reliable on its own:

        - a content hash of the ledger rows, which catches every add, delete and
          edit including a changed description. Deliberately not a file
          timestamp: the database is in WAL mode, so a write goes to `-wal` and
          leaves `lots.db`'s own mtime alone, while `-wal`/`-shm` are disturbed
          by reads. mtime here means either missing an edit that matters or
          rebuilding on every single request;
        - the CSV's mtime and size, for the seed/fallback file.
        """
        parts = [str(today), str(benchmark), "lots:" + lots_db.data_fingerprint()]
        for name in ("FT_GainLoss.csv",):
            path = Path(main.DATA_DIR) / name
            try:
                st = path.stat()
                parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{name}:missing")
        return "|".join(parts)

    def get(self, today=None, benchmark=None, force=False):
        today = today or date_cls.today()
        # Resolve through lots_db, not os.environ: the settings page can override
        # the benchmark, and because it feeds the cache key, changing it rebuilds
        # the report immediately instead of serving a stale one for TTL seconds.
        benchmark = benchmark or lots_db.runtime_setting("BENCHMARK", "VOO")
        key = self._data_key(today, benchmark)

        with self._lock:
            fresh = (
                self._ctx is not None
                and not force
                and self._key == key
                and (time.time() - self._built_at) < CACHE_TTL_SECONDS
            )
            if fresh:
                return self._ctx, False
            if self._building:
                # A build really is in flight. Serving the previous one beats
                # making this request wait several seconds for the same answer.
                log.info("report is being rebuilt; serving the previous one")
                return self._ctx, False
            self._building = True  # claim it, so the next request waits its turn

        try:
            ctx = self._build(today, benchmark)
        except Exception:
            # Leave the previous context alone but release the claim, so the
            # next request tries again instead of being told the build is
            # already running when it is not.
            with self._lock:
                self._building = False
            raise

        with self._lock:
            previous_tmp = self._tmpdir
            self._ctx = ctx
            self._key = key
            self._built_at = time.time()
            self._tmpdir = ctx["_tmpdir"]
            self._building = False
        if previous_tmp:
            _cleanup(previous_tmp)
        return ctx, True

    def _build(self, today, benchmark):
        tmpdir = tempfile.mkdtemp(prefix="stockemailer-preview-")
        chart = Path(tmpdir) / "total_net.png"
        try:
            run = main.compute_context(
                today=today,
                benchmark=benchmark,
                out_path=chart,
                show=False,
                attach_csv=False,
                write_dashboard=False,
            )
        except Exception:
            _cleanup(tmpdir)
            raise

        ctx = run.ctx
        # compute_context leaves graph_path as a bare relative name, which only
        # happens to be right when the chart lands in the cwd. Point it at the
        # file that was actually written.
        ctx["graph_path"] = str(chart)
        ctx["show_graph"] = chart.is_file()

        # The preview is shown in a sandboxed iframe with no network access, so
        # the chart has to travel inside the document rather than be fetched.
        if chart.is_file():
            try:
                b64 = base64.b64encode(chart.read_bytes()).decode("ascii")
                ctx["chart_src"] = f"data:image/png;base64,{b64}"
            except OSError as exc:
                log.warning("could not inline the chart for the preview: %s", exc)
                ctx["chart_src"] = ""
        else:
            ctx["chart_src"] = ""

        ctx["from_db"] = run.from_db
        ctx["_tmpdir"] = tmpdir
        log.info(
            "built a fresh report context for %s (%d lots, from_db=%s)",
            today,
            len(run.computed_df),
            run.from_db,
        )
        return ctx


def _cleanup(tmpdir):
    if not tmpdir:
        return
    try:
        for child in Path(tmpdir).iterdir():
            child.unlink(missing_ok=True)
        Path(tmpdir).rmdir()
    except OSError as exc:
        log.debug("could not clean up %s: %s", tmpdir, exc)


cache = _ContextCache()


def load_store():
    """The user's templates, or the defaults if the settings row is unreadable."""
    import lots_db

    try:
        return sections.load_store(lots_db)
    except Exception as exc:
        log.error("could not read the template settings (%s); using defaults", exc)
        return sections.default_store()


def render(ctx, store):
    """Render both parts of the email from one context and one store.

    Returns (html, text, problems) where problems are compile errors in the
    user's templates. A section that fails at render time is dropped by
    render_html so the cron email still goes out; here the errors are also
    surfaced, because a silently missing section in an editor is just baffling.
    """
    html = sections.render_html(ctx, store)
    text = sections.render_text(ctx, store)
    return html, text, sections.validate(store, only_enabled=True)
