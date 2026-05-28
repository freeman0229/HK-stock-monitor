"""
yahoo_library.py - Yahoo Finance OHLCV History Library

Fetches OHLCV data from Yahoo Finance for HK stocks.
Stores data in per-stock files: yahoo_{code5}.json

File structure:
  meta: code5, last_updated, source
  by_date: date_str -> {open, high, low, close, vol}

Price convention:
  open, high, low  -- raw unadjusted traded prices
  close            -- raw (unadjusted) closing price. Adj Close avoided because
                      yahoo high/low are raw prices; using adj close for the sigma
                      computation creates a cross-series mismatch on ex-div dates.
  vol              -- raw traded volume

Codes stored as 5-digit zero-padded strings.
Files stored on R2: pub-0b0781d969ec4b38b173f889109244a9.r2.dev/yahoo_{code5}.json

Changelog:
  [Fix 1] fetch_batch: added SIGALRM-based hard timeout (FETCH_TIMEOUT_SEC) around
          yf.download(). Without this, a stalled connection hangs the entire CI job
          indefinitely (root cause of the 1h16m failure). On timeout the batch is
          skipped and an empty result is returned -- the stock will be retried next run.
  [Fix 2] fetch_batch: timeout handler raises FetchTimeoutError (custom subclass of
          Exception) so it can be caught narrowly without masking other errors.
  [Fix 3] fetch_batch: SIGALRM is only used on POSIX platforms (Linux/macOS). On
          Windows the timeout is silently skipped (no-op) to avoid AttributeError.
  [Fix 4] fetch_universe: stocks already on R2 are now properly skipped per-batch
          rather than silently passed through when the filtered batch is empty.
  [Fix 5] fetch_and_save_all_years: upload_fn exception now logs code5 correctly
          (was logging the local variable name, not the value).
  [Fix 6] Suppress yfinance internal ERROR logs for YFTzMissingError (delisted /
          no timezone stocks e.g. 2478.HK, 2461.HK). Not real errors - yf.download()
          returns empty data and stocks are correctly skipped.
  [Fix 7] store raw Close instead of Adj Close as the "close" field. Yahoo high/low
          are raw prices; using adj close creates a cross-series mismatch on ex-div
          dates (e.g. 02800 pays semi-annual dividends -- adj close drops by dividend
          amount while raw high/low stay at pre-div levels, inflating sigma_up and
          producing visible spikes in the 個股波幅通道 upper band).
          Adj Close is kept as fallback only for stocks where raw Close is unavailable.
  [Fix 8] SLEEP_BATCH reduced from 10s → 3s. yf.download() makes one HTTP
          request per batch of 20 tickers — SLEEP_BATCH is a between-batch pause,
          not a per-ticker delay. 3s is a meaningful rate-limit guard and saves
          ~9 min over 133 batches. --update / --rebuild use the same constant
          and benefit equally from the reduction.
"""

import json
import logging
import os
import platform
import signal
import time
from datetime import date, timedelta

import yfinance as yf
from ccass_universe import normalize_code

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# Fix 6: suppress yfinance internal ERROR logs for delisted/timezone-missing stocks.
# yf.download() prints "N Failed downloads: YFTzMissingError('possibly delisted')"
# at ERROR level. These are not real errors - empty data is returned and stocks are
# correctly skipped. Without this filter they appear alarming in CI logs.
class _YFTzFilter(logging.Filter):
    def filter(self, record):
        return "YFTzMissingError" not in record.getMessage()

for _yf_logger_name in ("yfinance", "yfinance.base", "yfinance.download"):
    logging.getLogger(_yf_logger_name).addFilter(_YFTzFilter())

START_YEAR      = 1995
# [Fix 8] Reduced from 10s → 3s. yf.download() fetches all BATCH_SIZE tickers
# in a single HTTP request, so SLEEP_BATCH guards between batch-level API calls
# (133 total for 2652 stocks), not per-ticker. 3s is still a meaningful pause
# against Yahoo rate-limiting while saving ~9 min over a full --today run.
# --update and --rebuild are unaffected in terms of safety; they sleep between
# batches the same way and their per-year sleep(2) is unchanged.
SLEEP_BATCH     = 3
BATCH_SIZE      = 20
MAX_RETRIES     = 3
RETRY_SLEEP     = 30
# Hard per-batch network timeout in seconds.
# yf.download() has no built-in timeout; without this a single stalled connection
# blocks the entire CI job until GitHub kills it (~1h16m failure observed 2026-05-20).
FETCH_TIMEOUT_SEC = 120


# ── Timeout helpers ───────────────────────────────────────────────────────────

class FetchTimeoutError(Exception):
    """Raised when yf.download() exceeds FETCH_TIMEOUT_SEC."""


def _timeout_handler(signum, frame):
    raise FetchTimeoutError(f"yf.download timed out after {FETCH_TIMEOUT_SEC}s")


def _set_alarm(seconds: int):
    """Arm SIGALRM. No-op on Windows (signal.SIGALRM not available)."""
    if platform.system() != "Windows":
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(seconds)


def _cancel_alarm():
    """Disarm SIGALRM. No-op on Windows."""
    if platform.system() != "Windows":
        signal.alarm(0)


# ── File helpers ──────────────────────────────────────────────────────────────

def stock_path(code5: str) -> str:
    return f"yahoo_{code5}.json"


def load_stock(code5: str) -> dict:
    p = stock_path(code5)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"code5": code5, "source": "yahoo"}, "by_date": {}}


def save_stock(code5: str, lib: dict):
    lib["meta"] = {
        "code5":        code5,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
        "source":       "yahoo",
    }
    p = stock_path(code5)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))


# ── R2 helpers ────────────────────────────────────────────────────────────────

R2_BASE = "https://pub-0b0781d969ec4b38b173f889109244a9.r2.dev"


def list_r2_yahoo_codes() -> set:
    """List all yahoo_{code5}.json files already on R2 using AWS CLI."""
    import subprocess
    import re
    endpoint = os.environ.get("R2_ENDPOINT_URL", "")
    if not endpoint:
        log.warning("R2_ENDPOINT_URL not set -- cannot list R2 files, will fetch all")
        return set()
    try:
        cmd = ["aws", "s3", "ls", "s3://hk-stock-monitor/",
               "--endpoint-url", endpoint]
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            cmd.append("--no-sign-request")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        codes = re.findall(r"yahoo_(\d{5})\.json", r.stdout)
        log.info("R2 existing yahoo files: %d", len(codes))
        return set(codes)
    except Exception as e:
        log.warning("Could not list R2 yahoo files: %s -- will fetch all", e)
        return set()


# ── Ticker conversion ─────────────────────────────────────────────────────────

def to_yahoo_ticker(code5: str) -> str:
    return str(int(code5)).zfill(4) + ".HK"


def from_yahoo_ticker(ticker: str) -> str:
    return normalize_code(ticker.upper().replace(".HK", ""))


def _get_val(row, *keys) -> float:
    """Try multiple column name variants, return float or 0."""
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                f = float(v)
                if f == f:  # NaN check
                    return f
            except (TypeError, ValueError):
                pass
    return 0.0


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_batch(codes: list, start: date, end: date) -> dict:
    """
    Fetch OHLCV for a batch of codes. Returns {code5: {date_str: rec}}.

    Fix 1-3: wraps yf.download() in a SIGALRM hard timeout (FETCH_TIMEOUT_SEC).
    If the download stalls, FetchTimeoutError is raised, the batch is skipped,
    and an empty result dict is returned. The stock will be retried on the next
    scheduled run. Without this guard, a single stalled TCP connection hung the
    entire CI job for approx 76 min before GitHub killed it (observed 2026-May-20).
    """
    tickers = [to_yahoo_ticker(c) for c in codes]
    result  = {c: {} for c in codes}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _set_alarm(FETCH_TIMEOUT_SEC)
            try:
                raw = yf.download(
                    tickers,
                    start=start.isoformat(),
                    end=(end + timedelta(days=1)).isoformat(),
                    auto_adjust=False,  # raw OHLC + separate Adj Close column
                    progress=False,
                    group_by="ticker",
                    threads=False,
                )
            finally:
                _cancel_alarm()

            if raw is None or raw.empty:
                return result

            for ticker, code5 in zip(tickers, codes):
                try:
                    if len(tickers) == 1:
                        df = raw
                    else:
                        # MultiIndex with group_by="ticker": columns are (ticker, field)
                        cols = raw.columns
                        if hasattr(cols, "get_level_values"):
                            if ticker in cols.get_level_values(0):
                                df = raw[ticker]
                            elif ticker in cols.get_level_values(1):
                                df = raw.xs(ticker, axis=1, level=1)
                            else:
                                continue
                        else:
                            continue
                    if df is None or df.empty:
                        continue
                    for idx, row in df.iterrows():
                        ds = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                        o = _get_val(row, "Open",      "open")
                        h = _get_val(row, "High",      "high")
                        l = _get_val(row, "Low",       "low")
                        c = _get_val(row, "Close", "close", "Adj Close", "adj close")  # raw close; adj only as fallback
                        v = int(_get_val(row, "Volume", "volume"))
                        if c > 0:
                            result[code5][ds] = {
                                "open":  round(o, 4),
                                "high":  round(h, 4),
                                "low":   round(l, 4),
                                "close": round(c, 4),
                                "vol":   v,
                            }
                except Exception as e:
                    log.debug("fetch_batch: error processing %s: %s", ticker, e)
                    continue
            return result

        except FetchTimeoutError as e:
            # Hard timeout -- do NOT retry; the connection is stuck.
            # Return empty so caller skips these stocks. They will be retried next run.
            log.warning(
                "fetch_batch TIMEOUT for batch starting %s (attempt %d/%d): %s -- skipping batch",
                codes[0] if codes else "?", attempt, MAX_RETRIES, e,
            )
            _cancel_alarm()  # safety: ensure alarm is disarmed before returning
            return result

        except Exception as e:
            _cancel_alarm()  # ensure alarm is disarmed on any other exception
            if attempt < MAX_RETRIES:
                sleep_for = RETRY_SLEEP * attempt
                log.warning(
                    "fetch_batch attempt %d/%d failed: %s -- retrying in %ds",
                    attempt, MAX_RETRIES, e, sleep_for,
                )
                time.sleep(sleep_for)
            else:
                log.error("fetch_batch failed after %d attempts: %s", MAX_RETRIES, e)

    return result


# ── Single-stock full history ─────────────────────────────────────────────────

def fetch_and_save_all_years(code5: str, from_year: int, to_year: int,
                             rebuild: bool = False, upload_fn=None):
    """
    Fetch all years for a single stock and save to yahoo_{code5}.json.
    Incremental: skips dates already stored unless rebuild=True.
    Never overwrites existing dates (safe re-run guarantee).
    """
    lib = load_stock(code5) if not rebuild else {"meta": {}, "by_date": {}}
    existing_dates = set(lib["by_date"].keys()) if not rebuild else set()

    total_saved = 0
    for year in range(from_year, to_year + 1):
        start = date(year, 1, 1)
        end   = min(date(year, 12, 31), date.today())
        if start > date.today():
            continue

        # Check if this year already has data
        year_dates = {d for d in existing_dates if d.startswith(str(year))}
        # Expect ~240 trading days per year; skip if reasonably complete
        if not rebuild and len(year_dates) >= 200:
            continue

        batch_data = fetch_batch([code5], start, end)
        days = batch_data.get(code5, {})
        for ds, rec in days.items():
            lib["by_date"][ds] = rec
            total_saved += 1

    if total_saved > 0 or rebuild:
        lib["by_date"] = dict(sorted(lib["by_date"].items()))
        save_stock(code5, lib)
        if upload_fn:
            try:
                upload_fn(code5)
            except Exception as e:
                # Fix 5: was logging variable name not value
                log.warning("upload_fn failed for %s: %s", code5, e)

    return total_saved


# ── Universe-wide historical fetch ────────────────────────────────────────────

def fetch_universe(universe: list, from_year: int, to_year: int,
                   rebuild: bool = False, upload_fn=None):
    """
    Fetch year by year for batches of stocks.
    Accumulates all years per stock before saving -- one file per stock with all history.
    Never overwrites existing dates (safe re-run guarantee).

    Fix 4: when the per-batch filter removes all stocks (all already on R2),
    the batch is now explicitly skipped via `continue` instead of falling through
    to fetch_batch with an empty list (which returns immediately but wastes a
    SLEEP_BATCH sleep and a misleading log line).
    """
    log.info("Universe: %d stocks | years: %d-%d | rebuild: %s",
             len(universe), from_year, to_year, rebuild)

    saved = failed = 0

    # Get existing R2 files once upfront -- much faster than per-stock checks
    existing_on_r2 = set() if rebuild else list_r2_yahoo_codes()
    if existing_on_r2:
        log.info("Skipping %d stocks already on R2", len(existing_on_r2))

    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i:i + BATCH_SIZE]

        # Fix 4: skip entire batch early if all stocks already on R2
        if not rebuild:
            batch = [c for c in batch if c not in existing_on_r2]
            if not batch:
                continue  # was missing -- caused unnecessary sleep + log noise

        log.info("Processing batch %d-%d / %d",
                 i + 1, min(i + BATCH_SIZE, len(universe)), len(universe))

        # Accumulate data for all stocks in batch across all years
        batch_all = {code5: {} for code5 in batch}

        for year in range(from_year, to_year + 1):
            start = date(year, 1, 1)
            end   = min(date(year, 12, 31), date.today())
            if start > date.today():
                continue
            year_data = fetch_batch(batch, start, end)
            for code5 in batch:
                batch_all[code5].update(year_data.get(code5, {}))
            time.sleep(2)  # short sleep between years within same batch

        # Save each stock's accumulated data
        for code5 in batch:
            days = batch_all[code5]
            if not days:
                failed += 1
                continue
            lib = load_stock(code5) if not rebuild else {"meta": {}, "by_date": {}}
            lib["by_date"].update(days)
            lib["by_date"] = dict(sorted(lib["by_date"].items()))
            save_stock(code5, lib)
            if upload_fn:
                try:
                    upload_fn(code5)
                except Exception as e:
                    log.warning("upload_fn failed for %s: %s", code5, e)
            saved += 1

        time.sleep(SLEEP_BATCH)
        log.info("Batch %d-%d done. Saved so far: %d",
                 i + 1, min(i + BATCH_SIZE, len(universe)), saved)

    log.info("Done. Saved=%d Failed=%d", saved, failed)


# ── patch-2026 ────────────────────────────────────────────────────────────────

def patch_turnover_2026(universe: list):
    """
    Find dates in turnover_2026.json where high=0 for most stocks.
    Fill open, high, low, prev_close from Yahoo adjusted prices.
    Preserves all HKEX fields (vol, tv, vwap, name_en, name_zh, close).
    Never overwrites dates that already have valid high > 0.
    """
    tv_path = "turnover_2026.json"
    if not os.path.exists(tv_path):
        log.error("turnover_2026.json not found")
        return

    with open(tv_path, encoding="utf-8") as f:
        tv = json.load(f)

    by_date = tv.get("by_date", {})

    bad_dates = []
    for ds in sorted(by_date.keys()):
        recs  = by_date[ds]
        total = len(recs)
        if total == 0:
            continue
        has_hl = sum(1 for r in recs.values()
                     if isinstance(r, dict) and r.get("high", 0) > 0)
        if has_hl / total < 0.5:
            bad_dates.append(ds)

    if not bad_dates:
        log.info("patch-2026: no bad dates found")
        return

    log.info("patch-2026: %d dates to patch: %s ... %s",
             len(bad_dates), bad_dates[0], bad_dates[-1])

    fetch_start = date.fromisoformat(bad_dates[0]) - timedelta(days=5)
    fetch_end   = date.fromisoformat(bad_dates[-1])

    all_yahoo = {}
    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i:i + BATCH_SIZE]
        log.info("patch-2026: batch %d-%d / %d",
                 i + 1, min(i + BATCH_SIZE, len(universe)), len(universe))
        for code5, days in fetch_batch(batch, fetch_start, fetch_end).items():
            if days:
                all_yahoo[code5] = days
        time.sleep(SLEEP_BATCH)

    patched = 0
    for ds in bad_dates:
        if ds not in by_date:
            continue
        for code5, rec in by_date[ds].items():
            if not isinstance(rec, dict):
                continue
            yahoo_days = all_yahoo.get(code5, {})
            yahoo_rec  = yahoo_days.get(ds)
            if not yahoo_rec:
                continue
            if rec.get("high", 0) == 0:
                rec["open"] = yahoo_rec["open"]
                rec["high"] = yahoo_rec["high"]
                rec["low"]  = yahoo_rec["low"]
                patched += 1
            prev_dates = sorted(d for d in yahoo_days if d < ds)
            if prev_dates:
                rec["prev_close"] = yahoo_days[prev_dates[-1]]["close"]

    log.info("patch-2026: patched %d records across %d dates", patched, len(bad_dates))

    with open(tv_path, "w", encoding="utf-8") as f:
        json.dump(tv, f, ensure_ascii=False, separators=(",", ":"))
    log.info("patch-2026: saved %s (%.2f MB)",
             tv_path, os.path.getsize(tv_path) / 1e6)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from ccass_universe import get_universe_codes

    ap = argparse.ArgumentParser(description="Yahoo Finance OHLCV per-stock library builder")
    ap.add_argument("--from-year",  type=int, default=START_YEAR, dest="from_year")
    ap.add_argument("--to-year",    type=int, default=date.today().year, dest="to_year")
    ap.add_argument("--rebuild",    action="store_true")
    ap.add_argument("--patch-2026", action="store_true", dest="patch_2026")
    args = ap.parse_args()

    universe = list(get_universe_codes())
    log.info("Universe: %d stocks", len(universe))

    if args.patch_2026:
        patch_turnover_2026(universe)
    else:
        fetch_universe(universe, args.from_year, args.to_year, rebuild=args.rebuild)
