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
Always runs direct (no proxy).

Parallel fetch: ThreadPoolExecutor with MAX_WORKERS=3 browsers running
concurrently — one browser per worker, each handling its own code chunk.
Each worker has its own per-worker circuit breaker; if a worker trips it
the other workers keep running and the failed chunk is retried once.

Adaptive sleep: inter-request delay backs off automatically when the
error rate within a worker rises above ERROR_RATE_THRESHOLD.

Usage:
  python ccass_sdw_library.py                         # full backfill
  python ccass_sdw_library.py --update                # only new dates
  python ccass_sdw_library.py --date 2026-03-21       # one specific date
  python ccass_sdw_library.py --query 00700           # show holdings for a stock
  python ccass_sdw_library.py --stats                 # DB summary statistics
  python ccass_sdw_library.py --export-csv 00700      # export history to CSV
  python ccass_sdw_library.py --export-charts         # write sdw_{code}.json files
  python ccass_sdw_library.py --verify                # check DB integrity
  python ccass_sdw_library.py --migrate-json          # import old JSON range files
  python ccass_sdw_library.py --vacuum                # reclaim DB disk space
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from queue import Queue, Empty as QueueEmpty
from typing import Generator

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
CIRCUIT_BREAKER_LIMIT  : int   = 5     # consecutive errors per worker before giving up
MAX_FETCH_ATTEMPTS     : int   = 3     # retries per individual stock fetch
DIRECT_FAIL_THRESHOLD  : int   = 3     # consecutive non-transient failures before marking browser dead
NETWORK_FAIL_THRESHOLD : int   = 8     # consecutive transient failures before marking browser dead
WORKER_TIMEOUT_SEC     : int   = int(os.getenv("SDW_WORKER_TIMEOUT", "5400"))  # 90 min wall-clock cap per worker
MAX_REQUEUE            : int   = 2     # max times a dead-browser code can be requeued

# Error classification for dead-browser logic
# Transient: environmental blip — browser is fine, runner network hiccuped
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_CONNECTION_TIMED_OUT",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_SSL_",
    "net::ERR_CERT_",
    "TimeoutError",
    "Timeout",
)
# Dead: Chromium process is gone — no point retrying at all
_DEAD_PATTERNS: tuple[str, ...] = (
    "Target closed",
    "Browser closed",
    "Connection refused",
    "Protocol error",
    "Session closed",
)

# Data
START_DATE         : date = date(2025, 4, 5)
HKEX_WINDOW_MONTHS : int  = 12     # rolling availability window on HKEX SDW
SCHEMA_VERSION     : int  = 4

# Known-empty cache
EMPTY_SKIP_WEEKS   : int  = 4      # skip a code after this many consecutive empty weeks
EMPTY_RECHECK_WEEKS: int  = 8      # re-check a skipped code every N weeks regardless
MAX_SKIP_WEEKS     : int  = 16     # hard cap: force recheck if last_check_date stalled

# Blocked-codes persistence
BLOCK_EXPIRY_WEEKS : int  = 4      # auto-clear a blocked code after this many weeks with no new block


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
    """Return the most recent non-holiday weekday on or before friday.

    Walk back up to 5 days (the full Mon-Fri week):
    - Normal week         -> Friday
    - Friday holiday      -> Thursday
    - Thu+Fri holiday     -> Wednesday
    - Mon-Fri all holiday -> None (genuine full-week holiday e.g. CNY, skip)
    """
    d = friday
    for _ in range(5):
        if d.weekday() < 5 and d not in _HK_HOLIDAYS:
            return d
        d -= timedelta(days=1)
    return None


def all_fetch_dates(up_to: date | None = None) -> list[date]:
    """Return all scheduled fetch dates from START_DATE up to *up_to* (inclusive).

    Automatically skips dates older than HKEX_WINDOW_MONTHS since HKEX no
    longer serves data for those dates.
    """
    up_to    = up_to or date.today()
    earliest = date.today() - timedelta(days=HKEX_WINDOW_MONTHS * 30)
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

            CREATE TABLE IF NOT EXISTS known_empty (
                code            TEXT PRIMARY KEY,
                consecutive     INTEGER NOT NULL DEFAULT 0,
                last_empty_date TEXT,
                last_check_date TEXT
            );

            CREATE TABLE IF NOT EXISTS blocked_codes (
                code          TEXT PRIMARY KEY,
                consecutive   INTEGER NOT NULL DEFAULT 0,
                last_blocked  TEXT,   -- ISO date of most recent block hit
                last_cleared  TEXT    -- ISO date of most recent successful fetch
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
    """Run VACUUM to reclaim disk space and defragment the database.

    VACUUM must run outside a transaction and cannot use WAL mode during
    the operation, so we open a plain connection rather than get_conn().
    isolation_level=None gives autocommit, which VACUUM requires.
    """
    log.info("Running VACUUM on %s …", path)
    t0   = time.monotonic()
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
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


# ── Public API (imported by main.py) ─────────────────────────────────────────

def _ensure_db(path: str = DB_PATH) -> None:
    """Ensure the DB exists and all tables are present.

    Called at the top of every public read function so main.py can call
    them safely without first going through build_clean (which calls
    init_db internally).  The check is a fast sqlite PRAGMA — negligible
    overhead on the normal path where the DB already exists.
    """
    try:
        with sqlite3.connect(path, timeout=10) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        if not {"metadata", "holdings"}.issubset(tables):
            init_db(path)
    except Exception:
        init_db(path)


def get_holders(code: str, ds: str, path: str = DB_PATH) -> list[dict]:
    """Return holders for *code* on date *ds*, sorted by shares desc.

    Each entry: {pid, name, sh, pct}.
    Returns [] if no data.
    """
    _ensure_db(path)
    code5 = normalize_code(code)
    with get_conn(path) as conn:
        rows = conn.execute(
            """SELECT pid, name, shares AS sh, pct
               FROM   holdings
               WHERE  code = ? AND date = ?
               ORDER  BY shares DESC""",
            (code5, ds),
        ).fetchall()
    return [dict(r) for r in rows]


def get_holders_history(
    code: str,
    n:    int,
    before: str,
    path: str = DB_PATH,
) -> list[list[dict]]:
    """Return up to *n* holder snapshots for *code* strictly before *before*.

    Returns a list of snapshots, newest first.  Each snapshot is a list of
    {pid, name, sh, pct} dicts sorted by shares desc.
    """
    _ensure_db(path)
    code5 = normalize_code(code)
    with get_conn(path) as conn:
        dates = conn.execute(
            """SELECT DISTINCT date FROM metadata
               WHERE  code = ? AND date < ?
               ORDER  BY date DESC
               LIMIT  ?""",
            (code5, before, n),
        ).fetchall()
        result = []
        for (ds,) in dates:
            rows = conn.execute(
                """SELECT pid, name, shares AS sh, pct
                   FROM   holdings
                   WHERE  code = ? AND date = ?
                   ORDER  BY shares DESC""",
                (code5, ds),
            ).fetchall()
            result.append([dict(r) for r in rows])
    return result


def get_latest_total_sh(code: str, before_or_eq: str, path: str = DB_PATH) -> int:
    """Return the most recent total_sh for *code* on or before *before_or_eq*.

    Returns 0 if no data.
    """
    _ensure_db(path)
    code5 = normalize_code(code)
    with get_conn(path) as conn:
        row = conn.execute(
            """SELECT total_sh FROM metadata
               WHERE  code = ? AND date <= ?
               ORDER  BY date DESC
               LIMIT  1""",
            (code5, before_or_eq),
        ).fetchone()
    return int(row[0]) if row and row[0] else 0


def get_total_sh_bulk(
    before_or_eq: str,
    path: str = DB_PATH,
) -> dict[str, int]:
    """Return {code: total_sh} for the most recent date <= *before_or_eq* per code.

    Used by main.py to pre-load all total_sh values in one pass.
    Uses a GROUP BY + self-join instead of a correlated subquery for
    better performance on large metadata tables.
    """
    _ensure_db(path)
    with get_conn(path) as conn:
        rows = conn.execute(
            """SELECT m.code, m.total_sh
               FROM   metadata m
               INNER JOIN (
                   SELECT code, MAX(date) AS max_date
                   FROM   metadata
                   WHERE  date <= ?
                   GROUP  BY code
               ) latest ON m.code = latest.code AND m.date = latest.max_date""",
            (before_or_eq,),
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows if r[1]}


# ── Chart export ──────────────────────────────────────────────────────────────

def export_charts(
    out_dir : str  = ".",
    weeks   : int  = 52,
    path    : str  = DB_PATH,
) -> int:
    """Write one sdw_{code}.json per active stock for the frontend chart.

    File format (mirrors the old range-file structure the HTML already reads):
      {
        "dates":  ["2025-04-05", ...],          // sorted ascending
        "by_date": {
          "2025-04-05": {
            "p":        [{pid, name, sh, pct}, ...],
            "total_sh":  12345678,
            "issued_sh": 23456789
          }
        }
      }

    Only stocks that have at least one holdings row are exported.
    Only the most recent *weeks* Friday dates are included.
    Returns the number of files written.
    """
    os.makedirs(out_dir, exist_ok=True)

    # Cutoff: oldest date to include
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()

    written = 0
    with get_conn(path) as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM holdings ORDER BY code"
        ).fetchall()]

        for code in codes:
            meta_rows = conn.execute(
                """SELECT date, total_sh, issued_sh FROM metadata
                   WHERE  code = ? AND date >= ?
                   ORDER  BY date""",
                (code, cutoff),
            ).fetchall()
            if not meta_rows:
                continue

            by_date: dict = {}
            for row in meta_rows:
                ds, total_sh, issued_sh = row["date"], row["total_sh"], row["issued_sh"]
                holders = conn.execute(
                    """SELECT pid, name, shares AS sh, pct
                       FROM   holdings
                       WHERE  code = ? AND date = ?
                       ORDER  BY shares DESC""",
                    (code, ds),
                ).fetchall()
                by_date[ds] = {
                    "p":         [{"pid": h["pid"], "name": h["name"],
                                   "sh": h["sh"],   "pct": h["pct"]} for h in holders],
                    "total_sh":  int(total_sh  or 0),
                    "issued_sh": int(issued_sh or 0),
                }

            out = {
                "dates":   sorted(by_date.keys()),
                "by_date": by_date,
            }
            fpath = os.path.join(out_dir, f"sdw_{code}.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
            written += 1

    return written


# ── Stock universe ────────────────────────────────────────────────────────────

def get_sdw_universe(path: str = DB_PATH) -> list[str]:
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
        with get_conn(path) as conn:
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

    Always runs direct (no proxy). Context is rotated every CONTEXT_LIFE
    requests to refresh cookies and simulate a returning zh-HK user to
    Akamai BotManager.
    """

    CONTEXT_LIFE: int = 200   # base requests per browser context before rotating

    def __init__(self) -> None:
        self._pw             = None
        self._browser        = None
        self._context        = None
        self._page           = None
        self._req_count       : int  = 0
        self._on_sdw          : bool = False
        self._direct_failures : int  = 0   # non-transient per-stock failures
        self._network_failures: int  = 0   # transient DNS/TLS/timeout failures
        self.dead             : bool = False
        # Jitter the rotation threshold ±20 requests so multiple workers don't
        # all rotate simultaneously, avoiding a burst of warm-up traffic.
        self._context_life: int = self.CONTEXT_LIFE + random.randint(-20, 20)

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

    def _launch_browser(self) -> None:
        """Launch Chromium in direct mode, bypassing any OS-level proxy env vars."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled",
                "--no-proxy-server",   # ignore http_proxy / https_proxy env vars
            ],
        )
        log.info("🚀 Browser launched (direct)")

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
        if self._req_count >= self._context_life:
            log.debug("🔄 Rotating browser context at request %d", self._req_count)
            self._new_context()
            # Re-jitter threshold for the next context window
            self._context_life = self.CONTEXT_LIFE + random.randint(-20, 20)

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

                # ── Block detection: string patterns ──────────────────────────
                if any(pat in content for pat in BLOCK_PATTERNS):
                    log.warning(
                        "⚠️ Block detected for %s %s (attempt %d/%d)",
                        code5, date_str, attempt, MAX_FETCH_ATTEMPTS,
                    )
                    self._on_sdw = False
                    return FetchResult("blocked", None, "block page")

                # ── Block detection: structural validation ────────────────────
                # Akamai often returns HTTP 200 with a JS challenge, blank table,
                # or partial HTML that has no SDW form at all. Catch these silent
                # blocks by requiring the SDW search form marker to be present.
                if not _is_valid_sdw_page(content):
                    log.warning(
                        "⚠️ Invalid SDW structure for %s %s (attempt %d/%d) — "
                        "silent block or page not loaded",
                        code5, date_str, attempt, MAX_FETCH_ATTEMPTS,
                    )
                    self._on_sdw = False
                    return FetchResult("blocked", None, "invalid structure")

                # ── Parse and classify success/empty ─────────────────────────
                entry = _parse_response(content, code5, date_str)
                self._on_sdw           = True
                self._direct_failures  = 0
                self._network_failures = 0
                if not entry.participants:
                    return FetchResult("empty", entry)
                return FetchResult("ok", entry)

            except Exception as exc:
                err_str = str(exc)

                # ── Immediate dead: Chromium process is gone ──────────────────
                if any(p in err_str for p in _DEAD_PATTERNS):
                    log.error(
                        "💀 Browser process gone (%s %s): %s",
                        code5, date_str, err_str[:200],
                    )
                    self.dead = True
                    self._on_sdw = False
                    return FetchResult("network", None, err_str)

                is_transient = any(p in err_str for p in _TRANSIENT_PATTERNS)

                # Only score failures on the final attempt — one stubborn stock
                # = one failure tick, not MAX_FETCH_ATTEMPTS ticks.
                if attempt == MAX_FETCH_ATTEMPTS:
                    if is_transient:
                        self._network_failures += 1
                        log.warning(
                            "fetch (%s %s) transient error #%d: %s",
                            code5, date_str, self._network_failures, err_str[:200],
                        )
                        if self._network_failures >= NETWORK_FAIL_THRESHOLD:
                            log.error(
                                "💀 %d consecutive transient errors — "
                                "runner network degraded, marking dead",
                                self._network_failures,
                            )
                            self.dead = True
                            self._on_sdw = False
                            return FetchResult("network", None, err_str)
                    else:
                        self._direct_failures += 1
                        log.warning(
                            "fetch (%s %s) direct failure #%d: %s",
                            code5, date_str, self._direct_failures, err_str[:200],
                        )
                        if self._direct_failures >= DIRECT_FAIL_THRESHOLD:
                            log.error(
                                "💀 %d consecutive non-transient failures — "
                                "marking browser dead",
                                self._direct_failures,
                            )
                            self.dead = True
                            self._on_sdw = False
                            return FetchResult("network", None, err_str)

                # Final attempt: return without re-nav (re-nav could throw and
                # start a phantom extra attempt via the outer except).
                if attempt == MAX_FETCH_ATTEMPTS:
                    log.error(
                        "fetch (%s %s): all %d attempts failed: %s",
                        code5, date_str, MAX_FETCH_ATTEMPTS, err_str[:300],
                    )
                    self._on_sdw = False
                    return FetchResult("network", None, err_str)

                log.warning(
                    "fetch (%s %s) attempt %d/%d failed: %s",
                    code5, date_str, attempt, MAX_FETCH_ATTEMPTS, err_str[:200],
                )

                # Transient errors: sleep and retry without re-nav — the
                # connection usually recovers on its own and a re-nav on a
                # broken network would just eat retry budget.
                if is_transient:
                    human_sleep(3.0, 6.0)
                else:
                    try:
                        page.goto(SDW_URL, wait_until="load", timeout=60_000)
                        self._on_sdw = True
                    except Exception as nav_exc:
                        log.warning(
                            "re-nav failed: %s — new context", str(nav_exc)[:100]
                        )
                        self._new_context()
                        page = self._page

        # Should not be reached, but satisfies type checker
        return FetchResult("network", None, "exhausted retries")


# ── SDW page structural validator ────────────────────────────────────────────

# Form markers: ASP.NET control IDs unique to the SDW search page.
# These will never appear in an Akamai block page or generic error response.
_SDW_FORM_MARKERS: tuple[str, ...] = (
    "txtShareholdingDate",   # date input field
    "txtStockCode",          # stock code input field
    "btnSearch",             # search button
)

# Data markers: content present on any valid SDW result — including empty
# results where no participants are listed (the header/summary row is still
# rendered).  Could theoretically appear in a block page help snippet, so
# used as a secondary signal only.
_SDW_DATA_MARKERS: tuple[str, ...] = (
    "Participant ID",        # English column header
    "參與者編號",              # Chinese column header
    "已發行股份",              # Issued shares label (always present)
    "Issued Shares",         # English issued shares label
)


def _is_valid_sdw_page(html: str) -> bool:
    """Return True if *html* looks like a real SDW search result page.

    Requires:
      • At least one form marker  — ASP.NET control IDs that are impossible
        to spoof in an Akamai block page or JS challenge response.
      • At least one data marker  — confirms the page body loaded, not just
        a bare form shell with no content (e.g. mid-load truncation).

    This is stricter than a flat "any marker" check: a block page quoting
    "Issued Shares" in help text will always fail the form-marker requirement,
    and a truncated load that has the form but no rendered table is caught by
    the data-marker requirement.
    """
    has_form  = any(m in html for m in _SDW_FORM_MARKERS)
    data_hits = sum(m in html for m in _SDW_DATA_MARKERS)
    return has_form and data_hits >= 1


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
        # Table normally has 5 cols: pid, name, addr, sh, pct
        # When HKEX omits pct column (e.g. post-placement before issued_sh update)
        # table has only 4 cols: pid, name, addr, sh — handle both cases
        if len(tds) < 4:
            continue
        pid_raw = _clean_cell(tds[0])
        sh_raw  = _clean_cell(tds[3]).replace(",", "")
        pct_raw = _clean_cell(tds[4]).replace("%", "").strip() if len(tds) >= 5 else ""
        if not pid_raw or not sh_raw.isdigit():
            continue
        if pid_raw.lower() in ("參與者編號", "id", "participant id"):
            continue
        try:
            sh = int(sh_raw)
            try:
                pct = float(pct_raw) if pct_raw else 0.0
            except (ValueError, TypeError):
                log.debug(
                    "SDW %s: non-numeric pct %r for pid %s — defaulting to 0.0",
                    code5, pct_raw, pid_raw,
                )
                pct = 0.0
            participants.append(Participant(
                pid  = pid_raw,
                name = _clean_cell(tds[1]),
                sh   = sh,
                pct  = pct,
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
    work_queue   : Queue,
    d            : date,
    blocked_codes: dict[str, int],
    block_lock   : threading.Lock,
    global_state : dict,
    state_lock   : threading.Lock,
) -> tuple[list[tuple[str, HoldingEntry]], WorkerStats]:
    """Pull codes from *work_queue* and fetch them for date *d*.

    Uses a shared Queue instead of a static chunk, so workers naturally
    load-balance — fast workers pull more codes, slow/blocked ones pull fewer.

    global_state holds cross-worker counters:
      total_errors  — total network errors across all workers this date
      total_blocks  — total block hits across all workers this date
    Used by the global rate limiter: if global block rate rises, all workers
    increase their sleep multiplier via state_lock.

    Dead browser exits immediately. Circuit breaker exits on consecutive errors.
    """
    BLOCK_SKIP_THRESHOLD: int = 3

    stats         = WorkerStats(worker_id=worker_id)
    results       : list[tuple[str, HoldingEntry]] = []
    consec_errors = 0
    t0            = time.monotonic()
    sleep_mult    = 1.0

    with SDWBrowser() as browser:
        while True:

            # ── Wall-clock timeout guard ──────────────────────────────────
            # Catches the "slow but not frozen" case: worker is alive but
            # spending too long due to backoff, retries, or a sluggish runner.
            # A truly frozen Playwright process won't reach here — that's
            # handled by future.result(timeout=) in the orchestrator.
            if time.monotonic() - t0 > WORKER_TIMEOUT_SEC:
                log.error(
                    "  [W%d] ⏰ Wall-clock timeout after %.0fs — exiting",
                    worker_id, time.monotonic() - t0,
                )
                break

            # ── Pull next code from queue ─────────────────────────────────
            try:
                code = work_queue.get_nowait()
            except QueueEmpty:
                break

            # ── Dead browser check ────────────────────────────────────────
            if browser.dead:
                with state_lock:
                    count = global_state["requeue_counts"].get(code, 0) + 1
                    global_state["requeue_counts"][code] = count

                if count <= MAX_REQUEUE:
                    log.error(
                        "  [W%d] 💀 Browser dead — requeuing %s (attempt %d/%d)",
                        worker_id, code, count, MAX_REQUEUE,
                    )
                    # Put back BEFORE task_done so queue's unfinished count
                    # never drops to zero prematurely (avoids join() deadlock).
                    work_queue.put(code)
                else:
                    log.error(
                        "  [W%d] 💀 %s requeued %d times and still failing — dropping",
                        worker_id, code, MAX_REQUEUE,
                    )
                    stats.errors += 1

                work_queue.task_done()
                break

            # ── Persistent block skip ─────────────────────────────────────
            with block_lock:
                current_blocks = blocked_codes.get(code, 0)
            if current_blocks >= BLOCK_SKIP_THRESHOLD:
                log.info(
                    "  [W%d] ⏭  %s skipped (blocked %d times)",
                    worker_id, code, current_blocks,
                )
                stats.skipped += 1
                work_queue.task_done()
                continue

            # ── Global rate limiter: slow down if block rate is high ───────
            with state_lock:
                g_blocks     = global_state["total_blocks"]
                g_total      = global_state["total_fetched"] or 1
                g_block_rate = g_blocks / g_total
            if g_block_rate > 0.10 and sleep_mult < 3.0:
                sleep_mult = min(sleep_mult * 1.5, 3.0)
                log.info(
                    "  [W%d] 🌐 Global block rate %.1f%% — "
                    "increasing sleep to %.1fx",
                    worker_id, g_block_rate * 100, sleep_mult,
                )
            elif g_block_rate <= 0.05 and sleep_mult > 1.0:
                sleep_mult = max(sleep_mult / 1.5, 1.0)

            human_sleep(PRE_SLEEP_MIN * sleep_mult, PRE_SLEEP_MAX * sleep_mult)

            res = browser.fetch_with_status(code, d)

            with state_lock:
                global_state["total_fetched"] += 1

            if res.status == "ok":
                stats.saved  += 1
                consec_errors = 0
                log.debug("  [W%d] ✓ %s (%d participants)",
                          worker_id, code, len(res.data.participants))  # type: ignore
                results.append((code, res.data))  # type: ignore[arg-type]

            elif res.status == "empty":
                stats.skipped += 1
                consec_errors  = 0
                log.debug("  [W%d] — %s no CCASS data", worker_id, code)
                results.append((code, res.data))  # type: ignore[arg-type]

            elif res.status == "network":
                stats.errors  += 1
                stats.network += 1
                consec_errors += 1
                with state_lock:
                    global_state["total_errors"] += 1
                log.warning("  [W%d] ✗ %s network — retrying once: %s",
                            worker_id, code, res.error[:120])
                time.sleep(5)
                retry = browser.fetch_with_status(code, d)
                with state_lock:
                    global_state["total_fetched"] += 1
                if retry.status in ("ok", "empty"):
                    stats.retried += 1
                    consec_errors  = 0
                    if stats.errors > 0:   # guard: never let errors go negative
                        stats.errors -= 1
                    if retry.status == "ok":
                        stats.saved += 1
                    else:
                        stats.skipped += 1
                    results.append((code, retry.data))  # type: ignore[arg-type]
                else:
                    log.error("  [W%d] ✗✗ %s failed on retry", worker_id, code)
                    if browser.dead:
                        with state_lock:
                            count = global_state["requeue_counts"].get(code, 0) + 1
                            global_state["requeue_counts"][code] = count
                        if count <= MAX_REQUEUE:
                            log.error(
                                "  [W%d] 💀 Browser dead after retry — "
                                "requeuing %s (attempt %d/%d)",
                                worker_id, code, count, MAX_REQUEUE,
                            )
                            work_queue.put(code)
                        else:
                            log.error(
                                "  [W%d] 💀 %s requeued %d times and still "
                                "failing — dropping",
                                worker_id, code, MAX_REQUEUE,
                            )
                            stats.errors += 1
                        work_queue.task_done()
                        break

            elif res.status == "blocked":
                stats.errors  += 1
                stats.blocked += 1
                consec_errors += 1
                with state_lock:
                    global_state["total_blocks"] += 1
                with block_lock:
                    blocked_codes[code] = blocked_codes.get(code, 0) + 1
                    new_count = blocked_codes[code]
                log.warning(
                    "  [W%d] 🚫 %s blocked (lifetime=%d/%d)",
                    worker_id, code, new_count, BLOCK_SKIP_THRESHOLD,
                )
                if new_count >= BLOCK_SKIP_THRESHOLD:
                    log.warning("  [W%d] ⛔ %s skip-listed", worker_id, code)
                browser._new_context()
                time.sleep(30 + random.uniform(10, 30))

            # Circuit breaker
            if consec_errors >= CIRCUIT_BREAKER_LIMIT:
                log.error("  [W%d] 🚫 Circuit breaker — %d consecutive errors",
                          worker_id, consec_errors)
                work_queue.task_done()
                break

            # Adaptive per-worker sleep
            if stats.total >= 10:
                if stats.error_rate > ERROR_RATE_THRESHOLD and sleep_mult < 4.0:
                    sleep_mult = min(sleep_mult * BACKOFF_MULTIPLIER, 4.0)
                    log.info("  [W%d] ⚠️ Error rate %.1f%% — sleep_mult=%.1fx",
                             worker_id, stats.error_rate * 100, sleep_mult)
                elif stats.error_rate <= ERROR_RATE_THRESHOLD and sleep_mult > 1.0:
                    sleep_mult = max(sleep_mult / BACKOFF_MULTIPLIER, 1.0)

            human_sleep(SLEEP_MIN * sleep_mult, SLEEP_MAX * sleep_mult)
            work_queue.task_done()

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
            if entry.participants:
                saved += 1
    return saved


def _already_fetched(codes: list[str], ds: str, path: str = DB_PATH) -> set[str]:
    """Return the subset of *codes* that already have a metadata row for *ds*."""
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT code FROM metadata WHERE date = ?", (ds,)
        ).fetchall()
    return {r[0] for r in rows} & set(codes)


def _get_known_empty_skip_set(
    universe: list[str], ds: str, path: str = DB_PATH
) -> set[str]:
    """Return codes that should be skipped this date due to known-empty history.

    A code is skipped if it has been empty for EMPTY_SKIP_WEEKS consecutive
    weeks AND fewer than EMPTY_RECHECK_WEEKS weeks have passed since the last
    check.

    Two escape hatches force a recheck regardless:
      1. Scheduled recheck: weeks_since >= EMPTY_RECHECK_WEEKS (normal cadence).
      2. Hard cap: weeks_since >= MAX_SKIP_WEEKS — fires when last_check_date
         has stalled (e.g. the stock was blocked every time it came up for
         recheck), preventing indefinite suppression.

    If last_check_date is NULL the code is always included (never skip a stock
    we have no check record for).
    """
    skip: set[str] = set()
    today = ds  # ISO string comparison is fine for YYYY-MM-DD
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT code, consecutive, last_check_date FROM known_empty"
        ).fetchall()
    code_set = set(universe)
    for row in rows:
        code, consecutive, last_check = row[0], row[1], row[2]
        if code not in code_set:
            continue
        if consecutive < EMPTY_SKIP_WEEKS:
            continue
        if not last_check:
            # No check record — always include for a fresh check
            continue
        weeks_since = (
            date.fromisoformat(today) - date.fromisoformat(last_check)
        ).days / 7
        if weeks_since >= EMPTY_RECHECK_WEEKS:
            continue   # scheduled recheck due
        if weeks_since >= MAX_SKIP_WEEKS:
            continue   # hard cap: last_check_date stalled, force recheck
        skip.add(code)
    return skip


def _update_known_empty(
    results: list[tuple[str, HoldingEntry]],
    ds     : str,
    path   : str = DB_PATH,
) -> None:
    """Update known_empty table after a fetch round.

    - Codes with empty HoldingEntry → increment consecutive count
    - Codes with participants → reset consecutive count to 0
    """
    with get_conn(path) as conn:
        for code, entry in results:
            if entry.participants:
                # Has data — reset empty streak
                conn.execute(
                    """INSERT INTO known_empty (code, consecutive, last_check_date)
                       VALUES (?, 0, ?)
                       ON CONFLICT(code) DO UPDATE SET
                           consecutive     = 0,
                           last_check_date = excluded.last_check_date""",
                    (code, ds),
                )
            else:
                # Empty — increment streak
                conn.execute(
                    """INSERT INTO known_empty
                           (code, consecutive, last_empty_date, last_check_date)
                       VALUES (?, 1, ?, ?)
                       ON CONFLICT(code) DO UPDATE SET
                           consecutive     = consecutive + 1,
                           last_empty_date = excluded.last_empty_date,
                           last_check_date = excluded.last_check_date""",
                    (code, ds, ds),
                )


# ── Blocked-codes persistence ─────────────────────────────────────────────────

def _load_blocked_codes(path: str = DB_PATH) -> dict[str, int]:
    """Load persisted blocked_codes from DB, expiring stale entries.

    A code is expired (reset to 0) if last_blocked is older than
    BLOCK_EXPIRY_WEEKS — meaning it hasn't been blocked recently and
    deserves a fresh attempt.  Returns {code: consecutive_count}.
    """
    today    = date.today().isoformat()
    cutoff   = (date.today() - timedelta(weeks=BLOCK_EXPIRY_WEEKS)).isoformat()
    result   : dict[str, int] = {}

    try:
        with get_conn(path) as conn:
            rows = conn.execute(
                """SELECT code, consecutive, last_blocked
                   FROM   blocked_codes
                   WHERE  consecutive > 0"""
            ).fetchall()
        for row in rows:
            code, consecutive, last_blocked = row[0], row[1], row[2]
            if last_blocked and last_blocked < cutoff:
                # Expired — skip; will be reset to 0 on next flush
                continue
            result[code] = consecutive
        if result:
            log.info(
                "Loaded %d persisted blocked codes from DB", len(result)
            )
    except Exception as exc:
        log.warning("Could not load blocked_codes from DB: %s", exc)

    return result


def _flush_blocked_codes(
    blocked_codes: dict[str, int],
    ds           : str,
    path         : str = DB_PATH,
) -> None:
    """Persist the current in-memory blocked_codes dict to DB.

    - Codes with consecutive > 0 → upsert with updated last_blocked date.
    - Codes with consecutive == 0 → record last_cleared date (successful fetch).
    """
    if not blocked_codes:
        return
    try:
        with get_conn(path) as conn:
            for code, consecutive in blocked_codes.items():
                if consecutive > 0:
                    conn.execute(
                        """INSERT INTO blocked_codes
                               (code, consecutive, last_blocked)
                           VALUES (?, ?, ?)
                           ON CONFLICT(code) DO UPDATE SET
                               consecutive  = excluded.consecutive,
                               last_blocked = excluded.last_blocked""",
                        (code, consecutive, ds),
                    )
                else:
                    conn.execute(
                        """INSERT INTO blocked_codes
                               (code, consecutive, last_cleared)
                           VALUES (?, 0, ?)
                           ON CONFLICT(code) DO UPDATE SET
                               consecutive  = 0,
                               last_cleared = excluded.last_cleared""",
                        (code, ds),
                    )
    except Exception as exc:
        log.warning("Could not flush blocked_codes to DB: %s", exc)


def _clear_blocked_codes(
    successful_codes: list[str],
    blocked_codes   : dict[str, int],
    ds              : str,
    path            : str = DB_PATH,
) -> None:
    """Reset consecutive count to 0 for codes that fetched successfully.

    Updates both the in-memory dict and the DB so a clean fetch clears
    the block record immediately rather than waiting for expiry.
    """
    cleared = []
    for code in successful_codes:
        if blocked_codes.get(code, 0) > 0:
            blocked_codes[code] = 0
            cleared.append(code)
    if not cleared:
        return
    try:
        with get_conn(path) as conn:
            for code in cleared:
                conn.execute(
                    """INSERT INTO blocked_codes
                           (code, consecutive, last_cleared)
                       VALUES (?, 0, ?)
                       ON CONFLICT(code) DO UPDATE SET
                           consecutive  = 0,
                           last_cleared = excluded.last_cleared""",
                    (code, ds),
                )
        log.debug("Cleared block record for %d code(s): %s", len(cleared), cleared[:5])
    except Exception as exc:
        log.warning("Could not clear blocked_codes in DB: %s", exc)



def build_clean(
    dates : list[date],
    path  : str  = DB_PATH,
    update: bool = False,
) -> None:
    """Main orchestration loop: fetch all codes for each date in *dates*.

    Improvements over the static-chunk approach:
      - Dynamic work queue: workers pull from a shared Queue for natural
        load balancing — no idle workers while others are blocked.
      - Known-empty cache: codes with EMPTY_SKIP_WEEKS consecutive empty
        results are skipped until EMPTY_RECHECK_WEEKS passes.
      - Global rate limiter: shared block/error counters let all workers
        back off together when the global block rate exceeds 10%.
      - Adaptive worker recovery: clean dates step the worker cap back up.
    """
    GLOBAL_BLOCK_THRESHOLD     : int = 5
    GLOBAL_NETWORK_THRESHOLD   : int = 20
    ADAPTIVE_WORKER_BLOCK_LIMIT: int = 2
    REDUCED_WORKERS            : int = 2
    WORKER_RECOVERY_STREAK     : int = 2

    init_db(path)
    universe = get_sdw_universe(path)
    if not universe:
        log.error("Empty universe — aborting")
        return

    blocked_codes : dict[str, int] = _load_blocked_codes(path)
    active_workers: int            = MAX_WORKERS
    block_lock    : threading.Lock = threading.Lock()
    state_lock    : threading.Lock = threading.Lock()
    clean_streak  : int            = 0

    for d in dates:
        ds      = d.strftime("%Y-%m-%d")
        already = _already_fetched(universe, ds, path)

        # Known-empty skip set
        empty_skip = _get_known_empty_skip_set(universe, ds, path)

        # Persistent block skip set
        skip_set = {c for c, n in blocked_codes.items() if n >= 3}

        pending = [
            c for c in universe
            if c not in already
            and c not in skip_set
            and c not in empty_skip
        ]

        if skip_set or empty_skip:
            log.info(
                "  ⛔ skipped: %d blocked + %d known-empty = %d total",
                len(skip_set), len(empty_skip), len(skip_set) + len(empty_skip),
            )

        if update and not pending:
            log.info("  ⏭  %s — all codes already present or skipped", ds)
            clean_streak  += 1
            active_workers = _maybe_recover_workers(
                active_workers, clean_streak, WORKER_RECOVERY_STREAK, MAX_WORKERS
            )
            continue

        n_workers = active_workers
        log.info(
            "━━━ %s — %d pending / %d total "
            "(already=%d blocked=%d empty_skip=%d workers=%d)",
            ds, len(pending), len(universe),
            len(already), len(skip_set), len(empty_skip), n_workers,
        )

        # ── Build dynamic work queue ──────────────────────────────────────
        work_queue   = Queue()
        for code in pending:
            work_queue.put(code)

        global_state = {"total_fetched": 0, "total_errors": 0, "total_blocks": 0,
                        "requeue_counts": {}}

        t0      = time.monotonic()
        summary = DateSummary(
            ds          = ds,
            universe_sz = len(universe),
            already     = len(already),
        )

        total_blocked = 0
        total_network = 0
        all_results   : list[tuple[str, HoldingEntry]] = []

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(
                    _worker, wid, work_queue, d,
                    blocked_codes, block_lock, global_state, state_lock,
                ): wid
                for wid in range(1, n_workers + 1)
            }
            for fut in as_completed(futures):
                wid = futures[fut]
                try:
                    # Allow a small buffer over WORKER_TIMEOUT_SEC so the
                    # inner wall-clock guard fires first under normal
                    # circumstances. This outer timeout only catches a truly
                    # frozen Playwright process that never returns from a call.
                    w_results, w_stats = fut.result(timeout=WORKER_TIMEOUT_SEC + 120)
                except FutureTimeoutError:
                    log.error(
                        "Worker %d timed out (Playwright frozen?) — "
                        "abandoning; zombie process will be reaped at job exit",
                        wid,
                    )
                    continue
                except Exception as exc:
                    log.error("Worker %d raised: %s", wid, exc)
                    continue

                summary.saved   += w_stats.saved
                summary.errors  += w_stats.errors
                summary.skipped += w_stats.skipped
                summary.retried += w_stats.retried
                summary.worker_stats.append(w_stats)

                total_blocked += w_stats.blocked
                total_network += w_stats.network
                all_results.extend(w_results)

                # Save this worker's results immediately — preserves progress
                # if a subsequent worker times out or the job is cancelled
                if w_results:
                    log.info(
                        "Worker %d done — saving %d results to DB immediately",
                        wid, len([r for r in w_results if r[1].participants]),
                    )
                    _save_results(w_results, ds, path)
                    _update_known_empty(w_results, ds, path)
                    successful_w = [c for c, e in w_results if e.participants]
                    _clear_blocked_codes(successful_w, blocked_codes, ds, path)

        # All workers done — log total saved (already written above per-worker)
        if not all_results:
            log.info("No results to save for %s", ds)

        # Flush updated blocked_codes to DB so next CI run starts with
        # current state rather than rediscovering blocks from scratch.
        _flush_blocked_codes(blocked_codes, ds, path)

        # Adaptive worker scaling
        if total_blocked > ADAPTIVE_WORKER_BLOCK_LIMIT and active_workers > REDUCED_WORKERS:
            active_workers = REDUCED_WORKERS
            clean_streak   = 0
            log.warning(
                "🔻 %d blocks — reducing workers to %d",
                total_blocked, REDUCED_WORKERS,
            )
        elif total_blocked == 0 and summary.errors == 0:
            clean_streak  += 1
            active_workers = _maybe_recover_workers(
                active_workers, clean_streak, WORKER_RECOVERY_STREAK, MAX_WORKERS
            )
        else:
            clean_streak = 0

        # Global circuit breakers
        if total_blocked > GLOBAL_BLOCK_THRESHOLD:
            log.warning("🚫 %d blocks — cooling down 5 minutes", total_blocked)
            time.sleep(300)
        elif total_network > GLOBAL_NETWORK_THRESHOLD:
            log.warning("⚡ %d network errors — short cooldown", total_network)
            time.sleep(30)

        summary.elapsed_sec = time.monotonic() - t0
        summary.log_summary()

        if d != dates[-1]:
            log.debug("Sleeping %.0fs between dates …", INTER_DATE_SLEEP_SEC)
            time.sleep(INTER_DATE_SLEEP_SEC)


def _maybe_recover_workers(
    current: int, streak: int, threshold: int, cap: int
) -> int:
    """Step worker count up by 1 after *threshold* consecutive clean dates."""
    if streak > 0 and streak % threshold == 0 and current < cap:
        new = current + 1
        log.info("⬆️  %d clean date(s) — recovering workers %d → %d", streak, current, new)
        return new
    return current

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

    # getsize after connection is closed — avoids holding the WAL lock
    # while doing filesystem I/O, and works even if the DB is 0 bytes.
    db_size = os.path.getsize(path) / 1_048_576 if os.path.exists(path) else 0.0

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
    p.add_argument("--export-charts", action="store_true",
                   help="Export per-stock sdw_{code}.json chart files and exit")
    p.add_argument("--charts-dir",   default=".", metavar="DIR",
                   help="Output directory for --export-charts (default: %(default)s)")
    p.add_argument("--charts-weeks", default=52, type=int, metavar="N",
                   help="Weeks of history to include in chart files (default: %(default)s)")
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

    if args.export_charts:
        export_charts(out_dir=args.charts_dir, weeks=args.charts_weeks, path=db)
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
