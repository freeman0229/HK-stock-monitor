"""
ccass_sdw_library.py — CCASS Per-Stock Participant Holdings Library
====================================================================
Fetches weekly CCASS participant-level shareholding for ALL stocks
in the ccass_universe (single authoritative source).

Source (holdings):  https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx
Stock universe:     ccass_universe.get_universe_codes()

Schedule: every Friday; Thursday fallback if Friday is a HK holiday.
Start:    2025-04-05

Storage: SQLite database (ccass_sdw.db) with two tables:
  metadata   — per (date, code): total_sh, issued_sh, fetched_at
  holdings   — per (date, code, pid): name, shares, pct

Fetch strategy: Playwright Chromium (headless) — fills the search form
like a real user, bypassing Akamai BotManager detection entirely.
Proxy is optional (SDW_PROXY env var); falls back to direct after 3
consecutive proxy failures.

Parallel fetch: ThreadPoolExecutor with MAX_WORKERS=3 browsers running
concurrently — one browser per worker, each handling its own code chunk.
Each worker has its own per-worker circuit breaker; if a worker trips it
the other workers keep running and the failed chunk is retried once.

Adaptive sleep: inter-request delay backs off automatically when the
error rate within a worker rises above ERROR_RATE_THRESHOLD.

Usage:
  python ccass_sdw_library.py                    # full backfill
  python ccass_sdw_library.py --update           # only new dates
  python ccass_sdw_library.py --date 2026-03-21  # one specific date
  python ccass_sdw_library.py --query 00700      # show holdings for a stock
  python ccass_sdw_library.py --stats            # DB summary statistics
  python ccass_sdw_library.py --export-csv 00700 # export history to CSV
  python ccass_sdw_library.py --verify           # check DB integrity
  python ccass_sdw_library.py --migrate-json     # import old JSON range files
  python ccass_sdw_library.py --vacuum           # reclaim DB disk space
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Generator
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from ccass_universe import get_universe_codes, normalize_code

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ccass_sdw")

# ── Constants ─────────────────────────────────────────────────────────────────

SDW_URL    : str  = "https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx"
DB_PATH    : str  = os.getenv("SDW_DB_PATH", "ccass_sdw.db")
MAX_WORKERS: int  = int(os.getenv("SDW_WORKERS", "3"))  # tune: 2–4 safest

# Timing
SLEEP_MIN            : float = 2.0   # base inter-request delay within each worker
SLEEP_MAX            : float = 5.0
PRE_SLEEP_MIN        : float = 0.5   # think-time before each search
PRE_SLEEP_MAX        : float = 1.5
BACKOFF_MULTIPLIER   : float = 2.0   # adaptive sleep multiplier on high error rate
ERROR_RATE_THRESHOLD : float = 0.20  # >20% errors in a worker triggers back-off
INTER_DATE_SLEEP_SEC : float = 5.0   # cooldown between date iterations

# Resilience
CIRCUIT_BREAKER_LIMIT: int   = 5     # consecutive errors per worker before giving up
BLOCKED_COOLDOWN_SEC : int   = 1800  # 30 min global cooldown after circuit trip
MAX_FETCH_ATTEMPTS   : int   = 3     # retries per individual stock fetch
PROXY_FAIL_THRESHOLD : int   = 3     # consecutive proxy errors before direct fallback

# Data
START_DATE         : date = date(2025, 4, 5)
HKEX_WINDOW_MONTHS : int  = 12     # rolling availability window on HKEX SDW
SCHEMA_VERSION     : int  = 3

_PROXY: str | None = os.getenv("SDW_PROXY", "").strip() or None

_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

BLOCK_PATTERNS: list[str] = [
    "Access Denied",
    "Too many requests",
    "Request blocked",
    "Service unavailable",
    "403 Forbidden",
    "429 Too Many",
]

RANGES: list[tuple[str, int, int]] = [
    ("0001_3999",    1,  3999),
    ("6000_6999", 6000,  6999),
    ("7489_7618", 7489,  7618),
    ("9600_9999", 9600,  9999),
]


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Participant:
    """A single CCASS participant's shareholding."""
    pid  : str
    name : str
    sh   : int
    pct  : float


@dataclass
class HoldingEntry:
    """Parsed result for one (stock, date) fetch."""
    participants: list[Participant]
    total_sh    : int
    issued_sh   : int


@dataclass
class WorkerStats:
    """Counters returned by each parallel worker."""
    worker_id  : int
    saved      : int   = 0
    errors     : int   = 0
    skipped    : int   = 0    # valid empty results (no CCASS data)
    retried    : int   = 0    # stocks that succeeded on a retry
    blocked    : int   = 0    # FetchResult("blocked") hits
    network    : int   = 0    # FetchResult("network") hits
    elapsed_sec: float = 0.0

    @property
    def total(self) -> int:
        return self.saved + self.errors + self.skipped

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total > 0 else 0.0

    def summary(self) -> str:
        return (
            f"Worker {self.worker_id}: saved={self.saved} "
            f"empty={self.skipped} errors={self.errors} "
            f"blocked={self.blocked} network={self.network} "
            f"retried={self.retried} "
            f"error_rate={self.error_rate:.1%} "
            f"elapsed={self.elapsed_sec:.0f}s"
        )


@dataclass
class FetchResult:
    """Structured return type for SDWBrowser.fetch().

    status values:
      "ok"      — data fetched, participants present
      "empty"   — valid response, no CCASS participants (stock not in SDW)
      "blocked" — Akamai / WAF block page detected
      "network" — transport-level failure (timeout, connection error, SSL, etc.)
    """
    status : str            # "ok" | "empty" | "blocked" | "network"
    data   : HoldingEntry | None
    error  : str = ""


@dataclass
class DateSummary:
    """Aggregate result for one fetch date."""
    ds          : str
    universe_sz : int
    already     : int
    saved       : int   = 0
    errors      : int   = 0
    skipped     : int   = 0
    retried     : int   = 0
    elapsed_sec : float = 0.0
    worker_stats: list[WorkerStats] = field(default_factory=list)

    def log_summary(self) -> None:
        pct_done = (self.already + self.saved) / self.universe_sz * 100 \
                   if self.universe_sz else 0
        log.info(
            "  ✅ %s complete — saved=%d empty=%d errors=%d retried=%d "
            "coverage=%.1f%% elapsed=%.0fs",
            self.ds, self.saved, self.skipped, self.errors,
            self.retried, pct_done, self.elapsed_sec,
        )
        for ws in self.worker_stats:
            log.info("    %s", ws.summary())


# ── Helpers ───────────────────────────────────────────────────────────────────

def human_sleep(a: float, b: float) -> None:
    """Sleep for a random duration in [a, b] seconds plus a small jitter."""
    time.sleep(random.uniform(a, b) + random.random() * 0.3)


def _parse_proxy(proxy_url: str) -> dict | None:
    """Convert a proxy URL string to a Playwright proxy config dict.

    Handles both full URLs (http://user:pass@host:port) and bare host:port.
    Returns None if *proxy_url* is empty.
    """
    if not proxy_url:
        return None
    raw = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
    p   = urlparse(raw)
    cfg: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def _chunkify(lst: list, n: int) -> list[list]:
    """Split *lst* into at most *n* roughly equal non-empty chunks."""
    if not lst:
        return []
    k = math.ceil(len(lst) / n)
    return [lst[i:i + k] for i in range(0, len(lst), k)]


def _now_iso() -> str:
    """Return the current UTC datetime as an ISO 8601 string."""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ── Code ranges ───────────────────────────────────────────────────────────────

def code_range(code: str) -> str:
    """Return the RANGES label for a normalised 5-digit code string.

    Raises ValueError if the code does not fall in any known range.
    """
    n = int(normalize_code(code))
    for label, lo, hi in RANGES:
        if lo <= n <= hi:
            return label
    raise ValueError(
        f"code_range: {code!r} (n={n}) not in any known range — "
        "check ccass_universe inclusion rules"
    )


# ── HK holidays ───────────────────────────────────────────────────────────────

_HK_HOLIDAYS: set[date] = {
    date(2025, 1, 1),  date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31),
    date(2025, 4, 4),  date(2025, 4, 18), date(2025, 4, 19), date(2025, 4, 21),
    date(2025, 5, 1),  date(2025, 5, 5),  date(2025, 6, 2),  date(2025, 7, 1),
    date(2025, 9, 30), date(2025, 10, 1), date(2025, 10, 29),
    date(2025, 12, 25), date(2025, 12, 26),
    date(2026, 1, 1),  date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30),
    date(2026, 2, 2),  date(2026, 2, 3),  date(2026, 2, 4),
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20),
    date(2026, 4, 3),  date(2026, 4, 4),  date(2026, 4, 5),  date(2026, 4, 6),
    date(2026, 5, 1),  date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 1),
    date(2026, 9, 7),  date(2026, 10, 1), date(2026, 10, 26),
    date(2026, 12, 25), date(2026, 12, 26),
}
try:
    import holidays as _hol
    _HK_HOLIDAYS = _HK_HOLIDAYS | set(_hol.HongKong())
except ImportError:
    pass


# ── Schedule ──────────────────────────────────────────────────────────────────

def _fetch_date_for_friday(friday: date) -> date | None:
    """Return *friday* if not a holiday, Thursday if Friday is a holiday, else None."""
    thu = friday - timedelta(days=1)
    if friday not in _HK_HOLIDAYS:
        return friday
    if thu not in _HK_HOLIDAYS:
        return thu
    return None


def all_fetch_dates(up_to: date | None = None) -> list[date]:
    """Return all scheduled fetch dates from START_DATE up to *up_to* (inclusive).

    Automatically skips dates older than HKEX_WINDOW_MONTHS since HKEX no
    longer serves data for those dates.
    """
    up_to    = up_to or date.today()
    earliest = (
        date.today().replace(year=date.today().year - 1)
        if HKEX_WINDOW_MONTHS == 12
        else date.today() - timedelta(days=HKEX_WINDOW_MONTHS * 30)
    )
    start  = max(START_DATE, earliest)
    result : list[date] = []
    d      = start
    while d.weekday() != 4:   # advance to first Friday
        d += timedelta(days=1)
    while d <= up_to:
        fd = _fetch_date_for_friday(d)
        if fd:
            result.append(fd)
        d += timedelta(weeks=1)
    return result


# ── SQLite database ───────────────────────────────────────────────────────────

def init_db(path: str = DB_PATH) -> None:
    """Create tables and indexes if they don't already exist.

    Also stamps the schema_version in a settings table so future migrations
    can detect what version an existing DB was created with.
    """
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                date        TEXT    NOT NULL,
                code        TEXT    NOT NULL,
                total_sh    INTEGER NOT NULL DEFAULT 0,
                issued_sh   INTEGER NOT NULL DEFAULT 0,
                fetched_at  TEXT,
                PRIMARY KEY (date, code)
            );

            CREATE TABLE IF NOT EXISTS holdings (
                date    TEXT    NOT NULL,
                code    TEXT    NOT NULL,
                pid     TEXT    NOT NULL,
                name    TEXT    NOT NULL,
                shares  INTEGER NOT NULL,
                pct     REAL    NOT NULL,
                PRIMARY KEY (date, code, pid)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meta_date
                ON metadata(date);
            CREATE INDEX IF NOT EXISTS idx_meta_code
                ON metadata(code);
            CREATE INDEX IF NOT EXISTS idx_hold_date_code
                ON holdings(date, code);
            CREATE INDEX IF NOT EXISTS idx_hold_code
                ON holdings(code);
        """)
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),)
        )
        conn.commit()
    log.info("DB initialised (schema_version=%d): %s", SCHEMA_VERSION, path)


@contextmanager
def get_conn(path: str = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Yield a SQLite connection with WAL mode and busy-timeout configured.

    WAL journal mode allows concurrent readers alongside a single writer,
    which is safe for our multi-threaded fetch + read workload.
    Auto-commits on clean exit; rolls back on exception.
    """
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def vacuum_db(path: str = DB_PATH) -> None:
    """Run VACUUM to reclaim disk space and defragment the database."""
    log.info("Running VACUUM on %s …", path)
    t0   = time.monotonic()
    conn = sqlite3.connect(path)
    conn.execute("VACUUM")
    conn.close()
    log.info("VACUUM complete in %.1fs", time.monotonic() - t0)


def verify_db(path: str = DB_PATH) -> bool:
    """Run SQLite integrity check and cross-validate metadata vs holdings.

    Returns True if everything is clean, False if any issues are found.
    Logs a detailed report either way.
    """
    ok = True
    log.info("Verifying DB integrity: %s", path)
    with get_conn(path) as conn:
        # SQLite built-in integrity check
        result = conn.execute("PRAGMA integrity_check").fetchall()
        for row in result:
            msg = row[0]
            if msg != "ok":
                log.error("  integrity_check: %s", msg)
                ok = False
        if ok:
            log.info("  integrity_check: ok")

        # Cross-check: every holdings row should have a matching metadata row
        orphans = conn.execute("""
            SELECT COUNT(*) FROM holdings h
            WHERE NOT EXISTS (
                SELECT 1 FROM metadata m WHERE m.date=h.date AND m.code=h.code
            )
        """).fetchone()[0]
        if orphans:
            log.warning("  %d orphan holdings rows (no matching metadata)", orphans)
            ok = False
        else:
            log.info("  orphan holdings check: ok")

        # Summary counts
        n_dates = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM metadata"
        ).fetchone()[0]
        n_codes = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM metadata"
        ).fetchone()[0]
        n_hold  = conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
        n_meta  = conn.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
        log.info(
            "  DB summary: %d dates, %d codes, %d metadata rows, %d holding rows",
            n_dates, n_codes, n_meta, n_hold,
        )
    return ok


# ── Stock universe ────────────────────────────────────────────────────────────

def get_sdw_universe() -> list[str]:
    """Return the stock universe to fetch.

    Primary source: ccass_universe.get_universe_codes() — single source of truth.
    Fallback: codes already stored in the SQLite DB when the primary fails.
    """
    try:
        codes = get_universe_codes()
        if codes:
            log.info("SDW universe: %d codes from ccass_universe", len(codes))
            return codes
    except Exception as exc:
        log.warning(
            "Could not load universe from ccass_universe: %s — falling back to DB", exc
        )

    try:
        with get_conn() as conn:
            rows = conn.execute("SELECT DISTINCT code FROM metadata").fetchall()
        codes = sorted({r[0] for r in rows}, key=lambda x: int(normalize_code(x)))
        if codes:
            log.info("SDW universe (fallback from DB): %d codes", len(codes))
            return codes
    except Exception as exc:
        log.warning("DB fallback also failed: %s", exc)

    log.warning("No universe available — returning empty list")
    return []


# ── Playwright browser wrapper ────────────────────────────────────────────────

class SDWBrowser:
    """Manages a Playwright Chromium browser that fills the SDW search form
    like a real user — date field, stock code, then click 搜尋.

    Proxy is optional. After PROXY_FAIL_THRESHOLD consecutive proxy failures
    the browser automatically relaunches in direct (no-proxy) mode.
    Context is rotated every CONTEXT_LIFE requests to refresh cookies and
    simulate a returning zh-HK user to Akamai BotManager.
    """

    CONTEXT_LIFE: int = 60   # requests per browser context before rotating

    def __init__(self, proxy: str | None = None) -> None:
        self._proxy_cfg      : dict | None = _parse_proxy(proxy)
        self._using_proxy    : bool        = self._proxy_cfg is not None
        self._pw                           = None
        self._browser                      = None
        self._context                      = None
        self._page                         = None
        self._req_count      : int         = 0
        self._on_sdw         : bool        = False
        self._proxy_failures : int         = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def __enter__(self) -> SDWBrowser:
        self._pw = sync_playwright().start()
        self._launch_browser()
        self._new_context()
        return self

    def __exit__(self, *args: object) -> None:
        for obj, method in [
            (self._context, "close"),
            (self._browser, "close"),
            (self._pw,      "stop"),
        ]:
            try:
                if obj:
                    getattr(obj, method)()
            except Exception:
                pass

    def _launch_browser(self, force_direct: bool = False) -> None:
        """Launch Chromium, optionally through a proxy."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        proxy = self._proxy_cfg if (self._using_proxy and not force_direct) else None
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled",
            ],
            proxy=proxy,
        )
        mode = "proxy" if proxy else "direct (no proxy)"
        log.info("🚀 Browser launched (%s)", mode)

    def _fallback_to_direct(self) -> None:
        """Switch to a direct connection after repeated proxy failures."""
        log.warning(
            "🔀 Proxy failed %d times — switching to direct connection",
            self._proxy_failures,
        )
        self._using_proxy    = False
        self._proxy_failures = 0
        self._launch_browser(force_direct=True)
        self._new_context()

    def _new_context(self) -> None:
        """Close the current context and open a fresh one.

        Rotates user-agent and viewport, and pre-seeds consent cookies so
        Akamai treats the session as a returning zh-HK user.
        """
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass

        ua = random.choice(_USER_AGENTS)
        self._context = self._browser.new_context(
            user_agent          = ua,
            locale              = "zh-HK",
            timezone_id         = "Asia/Hong_Kong",
            viewport            = {
                "width":  random.randint(1280, 1440),
                "height": random.randint(768, 900),
            },
            java_script_enabled = True,
            ignore_https_errors = True,
        )

        consent_val = (
            "isGpcEnabled=0&datestamp=Wed+Apr+01+2026+16%3A00%3A00+GMT%2B0100"
            "&version=202303.2.0&browserGpcFlag=0&isIABGlobal=false"
            "&hosts=&landingPath=NotLandingPage"
            "&groups=C0001%3A1%2CC0003%3A1%2CC0004%3A1%2CC0002%3A1"
            "&AwaitingReconsent=false&geolocation=HK%3BHKG"
        )
        for domain in [".hkexnews.hk", ".www3.hkexnews.hk", "www3.hkexnews.hk"]:
            self._context.add_cookies([
                {"name": "sclang",
                 "value": "zh-HK",                        "domain": domain, "path": "/"},
                {"name": "s_cc",
                 "value": "true",                         "domain": domain, "path": "/"},
                {"name": "OptanonAlertBoxClosed",
                 "value": "2026-04-01T16:00:00.000Z",     "domain": domain, "path": "/"},
                {"name": "OptanonConsent",
                 "value": consent_val,                    "domain": domain, "path": "/"},
            ])

        self._page      = self._context.new_page()
        self._req_count = 0
        self._on_sdw    = False
        log.debug("🌐 New browser context (UA: %s…)", ua[:60])

        # Warm up: seed Akamai bm_* cookies via the main HKEX homepage
        try:
            self._page.goto("https://www.hkexnews.hk/", wait_until="load", timeout=45_000)
            human_sleep(2.0, 4.0)
        except Exception as exc:
            log.debug("Homepage warm-up failed: %s", str(exc)[:80])

        # Navigate to the SDW page so www3-scoped cookies are set
        try:
            self._page.goto(SDW_URL, wait_until="load", timeout=60_000)
            self._on_sdw = True
            human_sleep(1.5, 3.0)
        except Exception as exc:
            log.warning("Initial SDW navigation failed: %s", str(exc)[:80])
            self._on_sdw = False

    # ── fetch ─────────────────────────────────────────────────────────────────

    def fetch_with_status(self, stock_code: str, d: date) -> FetchResult:
        """Fill the SDW search form and parse the result page.

        Returns a FetchResult with a classified status:
          "ok"      — success, participants present
          "empty"   — valid response, no CCASS data for this stock/date
          "blocked" — Akamai/WAF block page detected
          "network" — transport-level failure (timeout, SSL, connection error)

        Never raises; all exceptions are caught and mapped to a FetchResult.
        """
        code5    = normalize_code(stock_code)
        date_str = d.strftime("%Y/%m/%d")

        self._req_count += 1
        if self._req_count >= self.CONTEXT_LIFE:
            log.debug("🔄 Rotating browser context at request %d", self._req_count)
            self._new_context()

        page = self._page

        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            try:
                if not self._on_sdw:
                    page.goto(SDW_URL, wait_until="load", timeout=60_000)
                    self._on_sdw = True
                    human_sleep(1.0, 2.0)

                with page.expect_response(
                    lambda r: "searchsdw_c.aspx" in r.url,
                    timeout=45_000,
                ):
                    page.evaluate(
                        """([dateStr, code5]) => {
                            const dateEl = document.getElementById('txtShareholdingDate');
                            if (dateEl) {
                                dateEl.removeAttribute('readonly');
                                dateEl.value = dateStr;
                            }
                            const codeEl = document.getElementById('txtStockCode');
                            if (codeEl) codeEl.value = code5;
                            const pidEl = document.getElementById('txtParticipantID');
                            const pnmEl = document.getElementById('txtParticipantName');
                            if (pidEl) pidEl.value = '';
                            if (pnmEl) pnmEl.value = '';
                            if (typeof __doPostBack === 'function') {
                                __doPostBack('btnSearch', '');
                            } else {
                                document.getElementById('btnSearch').click();
                            }
                        }""",
                        [date_str, code5],
                    )

                page.wait_for_function(
                    "document.readyState === 'complete'",
                    timeout=45_000,
                )

                content = page.content()

                # ── Block detection ───────────────────────────────────────────
                if any(pat in content for pat in BLOCK_PATTERNS):
                    log.warning(
                        "⚠️ Block detected for %s %s (attempt %d/%d)",
                        code5, date_str, attempt, MAX_FETCH_ATTEMPTS,
                    )
                    self._on_sdw = False
                    return FetchResult("blocked", None, "block page")

                # ── Parse and classify success/empty ─────────────────────────
                entry = _parse_response(content, code5, date_str)
                self._on_sdw         = True
                self._proxy_failures = 0
                if not entry.participants:
                    return FetchResult("empty", entry)
                return FetchResult("ok", entry)

            except Exception as exc:
                err_str      = str(exc)
                is_proxy_err = any(p in err_str for p in [
                    "ProxyError", "RemoteDisconnected", "Unable to connect to proxy",
                    "proxy", "Proxy", "ERR_CONNECTION_CLOSED", "ERR_SSL_PROTOCOL_ERROR",
                    "ERR_TUNNEL_CONNECTION_FAILED", "ERR_CONNECTION_RESET",
                ])

                if is_proxy_err:
                    self._proxy_failures += 1
                    log.warning(
                        "fetch (%s %s) proxy error attempt %d/%d "
                        "(failures=%d): %s",
                        code5, date_str, attempt, MAX_FETCH_ATTEMPTS,
                        self._proxy_failures, err_str[:200],
                    )
                    if self._proxy_cfg and self._proxy_failures >= PROXY_FAIL_THRESHOLD:
                        self._fallback_to_direct()
                    else:
                        self._new_context()
                    page = self._page
                else:
                    self._proxy_failures = 0
                    log.warning(
                        "fetch (%s %s) attempt %d/%d failed: %s",
                        code5, date_str, attempt, MAX_FETCH_ATTEMPTS,
                        err_str[:200],
                    )
                    try:
                        page.goto(SDW_URL, wait_until="load", timeout=60_000)
                        self._on_sdw = True
                    except Exception as nav_exc:
                        log.warning(
                            "re-nav failed: %s — new context", str(nav_exc)[:100]
                        )
                        self._new_context()
                        page = self._page

                # On the final attempt, return a classified FetchResult
                if attempt == MAX_FETCH_ATTEMPTS:
                    log.error(
                        "fetch (%s %s): all %d attempts failed: %s",
                        code5, date_str, MAX_FETCH_ATTEMPTS, err_str[:300],
                    )
                    self._on_sdw = False
                    return FetchResult("network", None, err_str)

        # Should not be reached, but satisfies type checker
        return FetchResult("network", None, "exhausted retries")


# ── HTML response parser ──────────────────────────────────────────────────────

def _parse_num(s: object) -> int:
    """Parse a comma-formatted integer string, returning 0 on failure."""
    try:
        return int(str(s).replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return 0


def _clean_cell(s: str) -> str:
    """Strip a leading 'Label: ' prefix from a table cell value."""
    return re.sub(r'^[^:：]+[:：]\s*', '', s).strip()


def _parse_response(html: str, code5: str, date_str: str) -> HoldingEntry:
    """Parse the SDW results page HTML into a HoldingEntry.

    participants is an empty list for stocks with no CCASS data — valid,
    not an error. Never returns None.
    """
    soup      = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(" ", strip=True)

    # ── 已發行股份 (Issued Shares) ────────────────────────────────────────────
    issued_sh = 0
    for pat in [
        r"已發行股份[^\d]{0,30}([\d,]{6,})",
        r"Issued\s+Shares[^\d]{0,30}([\d,]{6,})",
        r"Number\s+of\s+Issued\s+Shares[^\d]{0,30}([\d,]{6,})",
    ]:
        m = re.search(pat, full_text)
        if m:
            issued_sh = _parse_num(m.group(1))
            if issued_sh > 0:
                break

    # ── Participant rows ───────────────────────────────────────────────────────
    participants     : list[Participant] = []
    total_sh_fallback: int               = 0

    for tr in soup.find_all("tr"):
        tds     = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 5:
            continue
        pid_raw = _clean_cell(tds[0])
        sh_raw  = _clean_cell(tds[3]).replace(",", "")
        pct_raw = _clean_cell(tds[4]).replace("%", "").strip()
        if not pid_raw or not sh_raw.isdigit():
            continue
        if pid_raw.lower() in ("參與者編號", "id", "participant id"):
            continue
        try:
            sh = int(sh_raw)
            participants.append(Participant(
                pid  = pid_raw,
                name = _clean_cell(tds[1]),
                sh   = sh,
                pct  = float(pct_raw) if pct_raw else 0.0,
            ))
            total_sh_fallback += sh
        except (ValueError, TypeError):
            continue

    # ── 總數 (Grand Total) ────────────────────────────────────────────────────
    total_sh = 0
    for pat in [r"總數[^\d]{0,20}([\d,]{6,})", r"Grand\s+Total[^\d]{0,20}([\d,]{6,})"]:
        m = re.search(pat, full_text)
        if m:
            total_sh = _parse_num(m.group(1))
            if total_sh > 0:
                break

    if total_sh == 0:
        for tr in soup.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if "總數" in " ".join(tds) or "Grand Total" in " ".join(tds):
                for cell in reversed(tds):
                    num = _parse_num(cell)
                    if num > 1_000_000:
                        total_sh = num
                        break
                if total_sh > 0:
                    break

    if total_sh == 0 and total_sh_fallback > 0:
        total_sh = total_sh_fallback
        log.warning(
            "SDW %s %s: 總數 not found — using participant sum %d",
            code5, date_str, total_sh,
        )

    if not participants:
        log.debug("SDW: 0 records for %s on %s — no CCASS data", code5, date_str)

    participants.sort(key=lambda x: -x.sh)
    return HoldingEntry(
        participants = participants,
        total_sh     = total_sh,
        issued_sh    = issued_sh,
    )


# ── Parallel worker ───────────────────────────────────────────────────────────

def _worker(
    worker_id    : int,
    codes        : list[str],
    d            : date,
    blocked_codes: dict[str, int],
    active_workers: list[int],
    block_lock   : threading.Lock,
) -> tuple[list[tuple[str, HoldingEntry]], WorkerStats]:
    """Fetch a chunk of stock codes for date *d* using a dedicated browser.

    Uses FetchResult status classification to apply the right retry strategy:
      "ok" / "empty"  — accept immediately, no retry
      "network"       — fast retry (short sleep, up to MAX_FETCH_ATTEMPTS)
      "blocked"       — rotate context + longer cooldown, don't retry same code;
                        increments blocked_codes[code] for persistent memory

    Persistent block memory: codes whose block count exceeds
    BLOCK_SKIP_THRESHOLD are skipped entirely for the remainder of this date.
    The orchestrator reads active_workers[0] (a shared mutable int) to check
    whether the worker cap has been reduced mid-run.

    Per-worker circuit breaker still fires on too many consecutive errors.
    Adaptive sleep backs off when the overall error rate climbs above
    ERROR_RATE_THRESHOLD.

    Returns (results, stats) where results is a list of (code, HoldingEntry).
    """
    BLOCK_SKIP_THRESHOLD: int = 3   # skip a code after this many lifetime blocks

    stats         = WorkerStats(worker_id=worker_id)
    results       : list[tuple[str, HoldingEntry]] = []
    consec_errors = 0
    t0            = time.monotonic()
    sleep_mult    = 1.0   # adaptive backoff multiplier

    with SDWBrowser(proxy=_PROXY) as browser:
        for ci, code in enumerate(codes, 1):

            # ── Persistent block skip ─────────────────────────────────────
            with block_lock:
                current_blocks = blocked_codes.get(code, 0)
            if current_blocks >= BLOCK_SKIP_THRESHOLD:
                log.info(
                    "  [W%d %d/%d] ⏭  %s skipped — blocked %d times previously",
                    worker_id, ci, len(codes), code, current_blocks,
                )
                stats.skipped += 1
                continue

            human_sleep(PRE_SLEEP_MIN * sleep_mult, PRE_SLEEP_MAX * sleep_mult)

            res: FetchResult | None = None

            for attempt in range(MAX_FETCH_ATTEMPTS):
                res = browser.fetch_with_status(code, d)

                if res.status == "ok":
                    stats.saved  += 1
                    consec_errors = 0
                    if attempt > 0:
                        stats.retried += 1
                    log.debug(
                        "  [W%d %d/%d] ✓ %s (%d participants)",
                        worker_id, ci, len(codes), code,
                        len(res.data.participants),  # type: ignore[union-attr]
                    )
                    results.append((code, res.data))  # type: ignore[arg-type]
                    break

                elif res.status == "empty":
                    stats.skipped += 1
                    consec_errors  = 0
                    log.debug(
                        "  [W%d %d/%d] — %s no CCASS data",
                        worker_id, ci, len(codes), code,
                    )
                    results.append((code, res.data))  # type: ignore[arg-type]
                    break

                elif res.status == "network":
                    stats.errors  += 1
                    stats.network += 1
                    consec_errors += 1
                    wait = 2 * (attempt + 1)   # fast retry: 2 s, 4 s, 6 s …
                    log.warning(
                        "  [W%d %d/%d] ✗ %s network error attempt %d/%d "
                        "— retry in %ds: %s",
                        worker_id, ci, len(codes), code,
                        attempt + 1, MAX_FETCH_ATTEMPTS, wait,
                        res.error[:120],
                    )
                    time.sleep(wait)
                    continue   # retry same code

                elif res.status == "blocked":
                    stats.errors  += 1
                    stats.blocked += 1
                    consec_errors += 1

                    # ── Persistent block memory ───────────────────────────
                    with block_lock:
                        blocked_codes[code] = blocked_codes.get(code, 0) + 1
                        new_count = blocked_codes[code]
                    skip_after = BLOCK_SKIP_THRESHOLD
                    log.warning(
                        "  [W%d %d/%d] 🚫 %s blocked "
                        "(lifetime blocks=%d/%d) — rotating context, cooling down",
                        worker_id, ci, len(codes), code,
                        new_count, skip_after,
                    )
                    if new_count >= skip_after:
                        log.warning(
                            "  [W%d] ⛔ %s will be skipped for remainder of run "
                            "(blocked %d times)",
                            worker_id, code, new_count,
                        )

                    # Immediate mitigation: fresh identity
                    browser._new_context()
                    cooldown = 30 + random.uniform(10, 30)
                    time.sleep(cooldown)
                    break   # don't retry the same code immediately after a block

            # Circuit breaker: too many consecutive failures of any kind
            if consec_errors >= CIRCUIT_BREAKER_LIMIT:
                log.error(
                    "  [W%d] 🚫 Circuit breaker tripped at %d consecutive errors",
                    worker_id, consec_errors,
                )
                break

            # Adaptive sleep: back off when error rate is high, recover when it drops
            if stats.total >= 10:
                if stats.error_rate > ERROR_RATE_THRESHOLD and sleep_mult < 4.0:
                    sleep_mult = min(sleep_mult * BACKOFF_MULTIPLIER, 4.0)
                    log.info(
                        "  [W%d] ⚠️ Error rate %.1f%% — backing off "
                        "(sleep_mult=%.1fx)",
                        worker_id, stats.error_rate * 100, sleep_mult,
                    )
                elif stats.error_rate <= ERROR_RATE_THRESHOLD and sleep_mult > 1.0:
                    sleep_mult = max(sleep_mult / BACKOFF_MULTIPLIER, 1.0)
                    log.debug(
                        "  [W%d] ✅ Error rate %.1f%% — recovering "
                        "(sleep_mult=%.1fx)",
                        worker_id, stats.error_rate * 100, sleep_mult,
                    )

            human_sleep(SLEEP_MIN * sleep_mult, SLEEP_MAX * sleep_mult)

    stats.elapsed_sec = time.monotonic() - t0
    return results, stats


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _save_results(
    results : list[tuple[str, HoldingEntry]],
    ds      : str,
    path    : str = DB_PATH,
) -> int:
    """Persist a batch of (code, HoldingEntry) to the SQLite DB.

    Returns the number of rows actually written.
    """
    saved = 0
    with get_conn(path) as conn:
        for code, entry in results:
            conn.execute(
                """INSERT OR REPLACE INTO metadata
                   (date, code, total_sh, issued_sh, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (ds, code, entry.total_sh, entry.issued_sh, _now_iso()),
            )
            for p in entry.participants:
                conn.execute(
                    """INSERT OR REPLACE INTO holdings
                       (date, code, pid, name, shares, pct)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ds, code, p.pid, p.name, p.sh, p.pct),
                )
            saved += 1
    return saved


def _already_fetched(codes: list[str], ds: str, path: str = DB_PATH) -> set[str]:
    """Return the subset of *codes* that already have a metadata row for *ds*."""
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT code FROM metadata WHERE date = ?", (ds,)
        ).fetchall()
    return {r[0] for r in rows} & set(codes)


def build_clean(
    dates : list[date],
    path  : str  = DB_PATH,
    update: bool = False,
) -> None:
    """Main orchestration loop: fetch all codes for each date in *dates*.

    Distributes codes across up to active_workers[0] parallel SDWBrowser
    workers. Two adaptive mechanisms span the entire run (across all dates):

    1. Persistent block memory  (blocked_codes)
       Each time a code returns "blocked", blocked_codes[code] is incremented
       by the worker. Codes that reach BLOCK_SKIP_THRESHOLD (3) are silently
       skipped by every subsequent worker for the rest of the run.

    2. Adaptive worker count  (active_workers)
       After each worker completes, if cumulative blocks for that date exceed
       ADAPTIVE_WORKER_BLOCK_LIMIT (2), the active worker cap is reduced to
       REDUCED_WORKERS (2). The reduced cap persists across subsequent dates.

    Global circuit-breaker cooldowns are also applied per-date:
      total_blocked > GLOBAL_BLOCK_THRESHOLD  → 5-minute cooldown
      total_network > GLOBAL_NETWORK_THRESHOLD → 30-second cooldown

    Args:
        dates:  List of fetch dates (from all_fetch_dates()).
        path:   SQLite DB path.
        update: When True, skip dates where all codes are already present.
    """
    GLOBAL_BLOCK_THRESHOLD     : int = 5
    GLOBAL_NETWORK_THRESHOLD   : int = 20
    ADAPTIVE_WORKER_BLOCK_LIMIT: int = 2   # blocks in one date before reducing workers
    REDUCED_WORKERS            : int = 2

    init_db(path)
    universe = get_sdw_universe()
    if not universe:
        log.error("Empty universe — aborting")
        return

    # ── Shared adaptive state (persists across all dates) ─────────────────
    blocked_codes : dict[str, int] = {}          # code → lifetime block count
    active_workers: list[int]      = [MAX_WORKERS]  # mutable cap; [0] is current value
    block_lock    : threading.Lock = threading.Lock()  # guards blocked_codes mutations

    for d in dates:
        ds      = d.strftime("%Y-%m-%d")
        already = _already_fetched(universe, ds, path)

        # Filter out permanently skip-listed codes before building pending list
        skip_set = {c for c, n in blocked_codes.items() if n >= 3}
        pending  = [c for c in universe if c not in already and c not in skip_set]

        if skip_set:
            log.info(
                "  ⛔ %d code(s) excluded due to persistent blocks: %s%s",
                len(skip_set),
                ", ".join(sorted(skip_set)[:10]),
                " …" if len(skip_set) > 10 else "",
            )

        if update and not pending:
            log.info("  ⏭  %s — all %d codes already present, skipping", ds, len(universe))
            continue

        n_workers = active_workers[0]
        log.info(
            "━━━ %s — %d pending / %d total (already=%d skipped=%d workers=%d)",
            ds, len(pending), len(universe), len(already), len(skip_set), n_workers,
        )

        t0      = time.monotonic()
        chunks  = _chunkify(pending, n_workers)
        summary = DateSummary(
            ds          = ds,
            universe_sz = len(universe),
            already     = len(already),
        )

        # ── Parallel fetch ─────────────────────────────────────────────────
        total_blocked = 0
        total_network = 0

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(
                    _worker, wid, chunk, d, blocked_codes, active_workers, block_lock
                ): wid
                for wid, chunk in enumerate(chunks, 1)
            }
            for fut in as_completed(futures):
                wid = futures[fut]
                try:
                    w_results, w_stats = fut.result()
                except Exception as exc:
                    log.error("Worker %d raised: %s", wid, exc)
                    continue

                # Accumulate per-worker stats into summary
                summary.saved   += w_stats.saved
                summary.errors  += w_stats.errors
                summary.skipped += w_stats.skipped
                summary.retried += w_stats.retried
                summary.worker_stats.append(w_stats)

                total_blocked += w_stats.blocked
                total_network += w_stats.network

                # ── Adaptive worker count ─────────────────────────────────
                if (
                    total_blocked > ADAPTIVE_WORKER_BLOCK_LIMIT
                    and active_workers[0] > REDUCED_WORKERS
                ):
                    active_workers[0] = REDUCED_WORKERS
                    log.warning(
                        "🔻 %d blocks detected this date — reducing active "
                        "workers to %d for remainder of run",
                        total_blocked, REDUCED_WORKERS,
                    )

                # Persist this worker's results immediately
                if w_results:
                    _save_results(w_results, ds, path)

        # ── Global circuit breakers ────────────────────────────────────────
        if total_blocked > GLOBAL_BLOCK_THRESHOLD:
            log.warning(
                "🚫 %d blocks across workers — cooling down 5 minutes", total_blocked
            )
            time.sleep(300)
        elif total_network > GLOBAL_NETWORK_THRESHOLD:
            log.warning(
                "⚡ %d network errors across workers — short cooldown", total_network
            )
            time.sleep(30)

        summary.elapsed_sec = time.monotonic() - t0
        summary.log_summary()

        if d != dates[-1]:
            log.debug("Sleeping %.0fs between dates …", INTER_DATE_SLEEP_SEC)
            time.sleep(INTER_DATE_SLEEP_SEC)

# ── CLI helpers ───────────────────────────────────────────────────────────────

def _query(code: str, path: str = DB_PATH) -> None:
    """Print all holdings rows for *code* ordered by date desc, shares desc."""
    code5 = normalize_code(code)
    with get_conn(path) as conn:
        rows = conn.execute(
            """SELECT h.date, h.pid, h.name, h.shares, h.pct,
                      m.total_sh, m.issued_sh
               FROM   holdings h
               JOIN   metadata m USING (date, code)
               WHERE  h.code = ?
               ORDER  BY h.date DESC, h.shares DESC""",
            (code5,),
        ).fetchall()

    if not rows:
        print(f"No holdings found for {code5}")
        return

    current_date = None
    for r in rows:
        if r["date"] != current_date:
            current_date = r["date"]
            print(
                f"\n{'─'*72}\n"
                f"  {code5}  {current_date}   "
                f"total={r['total_sh']:,}  issued={r['issued_sh']:,}"
            )
        print(
            f"    {r['pid']:<12} {r['name']:<40} "
            f"{r['shares']:>15,}  {r['pct']:>6.2f}%"
        )


def _stats(path: str = DB_PATH) -> None:
    """Print a summary of what's in the database."""
    with get_conn(path) as conn:
        n_dates = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM metadata"
        ).fetchone()[0]
        n_codes = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM metadata"
        ).fetchone()[0]
        n_meta  = conn.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
        n_hold  = conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
        earliest = conn.execute(
            "SELECT MIN(date) FROM metadata"
        ).fetchone()[0]
        latest = conn.execute(
            "SELECT MAX(date) FROM metadata"
        ).fetchone()[0]
        db_size = os.path.getsize(path) / 1_048_576

    print(
        f"\n{'═'*60}\n"
        f"  DB path    : {path}\n"
        f"  DB size    : {db_size:.1f} MB\n"
        f"  Date range : {earliest} → {latest}  ({n_dates} dates)\n"
        f"  Codes      : {n_codes}\n"
        f"  Metadata   : {n_meta:,} rows\n"
        f"  Holdings   : {n_hold:,} rows\n"
        f"{'═'*60}"
    )


def _export_csv(code: str, path: str = DB_PATH) -> None:
    """Write all holdings for *code* to <code>_holdings.csv."""
    code5    = normalize_code(code)
    out_path = f"{code5}_holdings.csv"
    with get_conn(path) as conn:
        rows = conn.execute(
            """SELECT h.date, h.code, h.pid, h.name, h.shares, h.pct,
                      m.total_sh, m.issued_sh
               FROM   holdings h
               JOIN   metadata m USING (date, code)
               WHERE  h.code = ?
               ORDER  BY h.date, h.shares DESC""",
            (code5,),
        ).fetchall()

    if not rows:
        print(f"No data for {code5}")
        return

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "code", "pid", "name", "shares", "pct",
                         "total_sh", "issued_sh"])
        for r in rows:
            writer.writerow(list(r))

    print(f"Exported {len(rows):,} rows → {out_path}")


def _migrate_json(path: str = DB_PATH) -> None:
    """Import legacy JSON range files into the SQLite DB.

    Supports both filename conventions:
      - ccass_sdw_<range>_<year>.json  (e.g. ccass_sdw_0001_3999_2025.json)
      - holdings_<range>_<year>.json   (older naming)

    Auto-detects two JSON structure variants:

    Variant A — date-keyed (outer key is a date string):
      {
        "2025-04-05": {
          "00700": {
            "participants": [{"pid": "B01234", "name": "...", "sh": 100, "pct": 0.5}],
            "total_sh": 200,
            "issued_sh": 1000
          }
        }
      }

    Variant B — code-keyed (outer key is a stock code):
      {
        "00700": {
          "2025-04-05": {
            "participants": [...],
            "total_sh": 200,
            "issued_sh": 1000
          }
        }
      }

    Skips any (date, code) pairs already present in the DB.
    """
    import glob

    patterns = ["ccass_sdw_*.json", "holdings_*.json"]
    files: list[str] = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    files = sorted(set(files))

    if not files:
        print(
            f"No JSON files found in {os.getcwd()}\n"
            f"Expected: ccass_sdw_<range>_<year>.json or holdings_<range>_<year>.json"
        )
        return

    print(f"Found {len(files)} file(s) to import:")
    for f in files:
        print(f"  {f}")

    init_db(path)
    total_imported = 0
    total_skipped  = 0

    for fpath in files:
        log.info("Importing %s …", fpath)
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            log.warning("  ✗ skip %s: %s", fpath, exc)
            continue

        if not isinstance(data, dict):
            log.warning("  ✗ skip %s: top-level is not a dict", fpath)
            continue

        # ── Auto-detect structure variant ─────────────────────────────────
        # Sample the first key to determine layout.
        first_key    = next(iter(data))
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        variant_a    = bool(date_pattern.match(first_key))

        def iter_records(d: dict, *, date_first: bool):
            if date_first:
                # Variant A: { "YYYY-MM-DD": { code: payload } }
                for ds, codes_dict in d.items():
                    if not isinstance(codes_dict, dict):
                        continue
                    for code, payload in codes_dict.items():
                        yield ds, code, payload
            else:
                # Variant B: { code: { "YYYY-MM-DD": payload } }
                _dp = re.compile(r"^\d{4}-\d{2}-\d{2}$")
                for code, dates_dict in d.items():
                    if not isinstance(dates_dict, dict):
                        continue
                    for ds, payload in dates_dict.items():
                        if not _dp.match(ds):
                            continue
                        yield ds, code, payload

        imported = 0
        skipped  = 0
        with get_conn(path) as conn:
            for ds, code, payload in iter_records(data, date_first=variant_a):
                try:
                    code5 = normalize_code(code)
                except Exception:
                    continue

                exists = conn.execute(
                    "SELECT 1 FROM metadata WHERE date=? AND code=?",
                    (ds, code5),
                ).fetchone()
                if exists:
                    skipped += 1
                    continue

                total_sh  = payload.get("total_sh",  0)
                issued_sh = payload.get("issued_sh", 0)
                conn.execute(
                    """INSERT OR REPLACE INTO metadata
                       (date, code, total_sh, issued_sh, fetched_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (ds, code5, total_sh, issued_sh, _now_iso()),
                )
                for p in payload.get("participants", []):
                    conn.execute(
                        """INSERT OR REPLACE INTO holdings
                           (date, code, pid, name, shares, pct)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (ds, code5,
                         p.get("pid",  ""),
                         p.get("name", ""),
                         p.get("sh", p.get("shares", 0)),
                         p.get("pct", 0.0)),
                    )
                imported += 1

        log.info(
            "  ✓ %s — imported=%d skipped(already present)=%d",
            fpath, imported, skipped,
        )
        total_imported += imported
        total_skipped  += skipped

    print(
        f"\nMigration complete — "
        f"{total_imported} records imported, "
        f"{total_skipped} already present (skipped) "
        f"across {len(files)} file(s)"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CCASS SDW per-stock participant holdings fetcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db",          default=DB_PATH, metavar="PATH",
                   help="SQLite DB path (default: %(default)s)")
    p.add_argument("--date",        metavar="YYYY-MM-DD",
                   help="Fetch a single specific date")
    p.add_argument("--update",      action="store_true",
                   help="Only fetch dates not already in the DB")
    p.add_argument("--query",       metavar="CODE",
                   help="Print holdings for a stock code and exit")
    p.add_argument("--stats",       action="store_true",
                   help="Print DB summary statistics and exit")
    p.add_argument("--export-csv",  metavar="CODE",
                   help="Export holdings history for CODE to CSV and exit")
    p.add_argument("--verify",      action="store_true",
                   help="Run DB integrity checks and exit")
    p.add_argument("--migrate-json", action="store_true",
                   help="Import legacy JSON range files into the DB and exit")
    p.add_argument("--vacuum",      action="store_true",
                   help="Run VACUUM to reclaim DB disk space and exit")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    db   = args.db

    # ── Read-only / maintenance commands ──────────────────────────────────
    if args.query:
        _query(args.query, db)
        return

    if args.stats:
        _stats(db)
        return

    if args.export_csv:
        _export_csv(args.export_csv, db)
        return

    if args.verify:
        ok = verify_db(db)
        sys.exit(0 if ok else 1)

    if args.migrate_json:
        _migrate_json(db)
        return

    if args.vacuum:
        vacuum_db(db)
        return

    # ── Fetch commands ────────────────────────────────────────────────────
    if args.date:
        try:
            d = date.fromisoformat(args.date)
        except ValueError:
            log.error("Invalid date format: %r  (expected YYYY-MM-DD)", args.date)
            sys.exit(1)
        dates = [d]
    else:
        dates = all_fetch_dates()

    if not dates:
        log.warning("No fetch dates in range — nothing to do")
        return

    log.info(
        "Starting%s fetch for %d date(s): %s → %s",
        " update" if args.update else "",
        len(dates),
        dates[0].isoformat(),
        dates[-1].isoformat(),
    )
    build_clean(dates, path=db, update=args.update)
    log.info("Done.")


if __name__ == "__main__":
    main()
