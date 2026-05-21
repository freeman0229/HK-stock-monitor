"""
build_yahoo.py - Daily incremental update for Yahoo Finance OHLCV library

Modes:
  --today               Daily mode: fetch last 7 days for all stocks, merge into
                        existing R2 files. Fast (~2-3 min). Use in daily job.
  --update              Full incremental: download each file from R2, find last
                        stored date, fetch only missing days. Use for backfill.
  --rebuild             Full refetch from START_YEAR, overwrite everything.
  --code XXXXX          Process a single stock (combine with any mode above).

--today logic (designed for ephemeral CI runners):
  - Fetch last 7 days for all stocks in batches (no R2 download needed)
  - For each stock: download existing file from R2, merge new days (never overwrite),
    upload back to R2
  - Stocks with no new data are skipped
  - Completes in ~2-3 minutes for 2650 stocks

--update logic:
  - Download each yahoo_{code5}.json from R2
  - Find last stored date
  - Fetch from last_date+1 to today
  - Merge without overwriting, upload to R2
  - Use for the "Yahoo Finance Backfill" workflow_dispatch job

Upload:
  Requires R2_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY env vars.

Usage:
  python build_yahoo.py --today                # daily job (fast)
  python build_yahoo.py --update               # full incremental backfill
  python build_yahoo.py --rebuild              # full rebuild from 1995
  python build_yahoo.py --today  --code 00700  # single stock, today mode
  python build_yahoo.py --update --code 00700  # single stock, incremental

Changelog:
  [Fix 1] run_today / run_update / run_rebuild: import and use FetchTimeoutError
          from yahoo_library so batch-level timeout exceptions are caught explicitly
          and logged with the correct stock range, rather than being swallowed by
          the broad `except Exception` or causing unhandled propagation.
  [Fix 2] run_today: on FetchTimeoutError the batch is counted as failed and the
          sleep still runs (prevents tight retry loops when Yahoo is rate-limiting).
  [Fix 3] run_update: per-stock `start` date is now correctly clamped to
          `per_stock_start[code5]` when filtering new_days, preventing accidental
          re-insertion of old dates that were in the broad fetch window.
  [Fix 4] run_rebuild: FetchTimeoutError is now caught and logged per year/batch
          rather than silently aborting the entire rebuild job.
  [Fix 5] All modes: log statements now include failed/timeout counts in summary
          line so CI logs clearly show whether any batches were skipped.
"""

import argparse
import logging
import os
import subprocess
import time
from datetime import date, timedelta

from ccass_universe import get_universe_codes
from yahoo_library import (
    BATCH_SIZE,
    FETCH_TIMEOUT_SEC,
    FetchTimeoutError,
    SLEEP_BATCH,
    START_YEAR,
    fetch_batch,
    load_stock,
    save_stock,
    stock_path,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

R2_BUCKET   = "s3://hk-stock-monitor"
R2_ENDPOINT = os.environ.get("R2_ENDPOINT_URL", "")

# How many days back --today fetches (covers weekends + public holidays)
TODAY_WINDOW = 7


# ── R2 helpers ────────────────────────────────────────────────────────────────

def upload_to_r2(code5: str) -> bool:
    """Upload yahoo_{code5}.json to R2. Returns True on success."""
    if not R2_ENDPOINT:
        log.debug("R2_ENDPOINT_URL not set — skipping upload for %s", code5)
        return False
    path = stock_path(code5)
    if not os.path.exists(path):
        log.warning("upload_to_r2: file not found: %s", path)
        return False
    try:
        cmd = ["aws", "s3", "cp", path, f"{R2_BUCKET}/{path}",
               "--endpoint-url", R2_ENDPOINT, "--no-progress"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log.debug("Uploaded %s to R2", path)
            return True
        log.warning("R2 upload failed for %s: %s", code5, r.stderr.strip())
        return False
    except Exception as e:
        log.warning("R2 upload exception for %s: %s", code5, e)
        return False


def download_from_r2(code5: str) -> bool:
    """Download yahoo_{code5}.json from R2 to local disk. Returns True on success."""
    if not R2_ENDPOINT:
        return False
    path = stock_path(code5)
    try:
        cmd = ["aws", "s3", "cp", f"{R2_BUCKET}/{path}", path,
               "--endpoint-url", R2_ENDPOINT, "--no-progress"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and os.path.exists(path)
    except Exception:
        return False


def cleanup(code5: str):
    """Remove local yahoo_{code5}.json (runner is ephemeral)."""
    try:
        p = stock_path(code5)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


# ── --today mode ──────────────────────────────────────────────────────────────

def run_today(universe: list):
    """
    Fetch last TODAY_WINDOW days for all stocks in batches.
    For each stock: download from R2, merge new days, upload back.
    Never overwrites existing dates.
    Fast: one fetch_batch call per batch of 20 stocks.

    Fix 1-2: FetchTimeoutError is now caught explicitly so a stalled batch
    is logged clearly and counted in the failed total rather than hanging
    the process until GitHub kills the job.
    """
    today       = date.today()
    fetch_start = today - timedelta(days=TODAY_WINDOW)
    fetch_end   = today
    total       = len(universe)
    updated = skipped = failed = timed_out = 0

    log.info("--today mode | %d stocks | window: %s → %s | timeout: %ds/batch",
             total, fetch_start, fetch_end, FETCH_TIMEOUT_SEC)

    for i in range(0, total, BATCH_SIZE):
        batch = universe[i : i + BATCH_SIZE]

        log.info("Batch %d–%d / %d | fetching …",
                 i + 1, min(i + BATCH_SIZE, total), total)

        # Fix 1: catch FetchTimeoutError separately for clear diagnostics
        try:
            batch_data = fetch_batch(batch, fetch_start, fetch_end)
        except FetchTimeoutError as e:
            log.error(
                "Batch %d–%d TIMED OUT after %ds: %s — skipping %d stocks",
                i + 1, min(i + BATCH_SIZE, total), FETCH_TIMEOUT_SEC, e, len(batch),
            )
            timed_out += len(batch)
            failed    += len(batch)
            time.sleep(SLEEP_BATCH)
            continue
        except Exception as e:
            log.error("fetch_batch failed for batch %d–%d: %s", i + 1, i + BATCH_SIZE, e)
            failed += len(batch)
            time.sleep(SLEEP_BATCH)
            continue

        for code5 in batch:
            new_days = batch_data.get(code5, {})
            if not new_days:
                skipped += 1
                continue

            # Download existing file from R2 to merge into
            download_from_r2(code5)
            lib      = load_stock(code5)
            existing = lib.get("by_date", {})
            added    = 0

            for ds, rec in new_days.items():
                if ds not in existing:  # never overwrite
                    existing[ds] = rec
                    added += 1

            if added == 0:
                skipped += 1
                cleanup(code5)
                continue

            lib["by_date"] = dict(sorted(existing.items()))
            save_stock(code5, lib)
            upload_to_r2(code5)
            cleanup(code5)
            log.info("%s — +%d days", code5, added)
            updated += 1

        time.sleep(SLEEP_BATCH)

    # Fix 5: include timed_out in summary
    log.info("--today done. updated=%d skipped=%d failed=%d (timed_out=%d)",
             updated, skipped, failed, timed_out)


# ── --update mode ─────────────────────────────────────────────────────────────

def run_update(universe: list):
    """
    Full incremental update: for each stock download from R2, find last stored
    date, fetch only missing days, merge and upload. Never overwrites.
    Use for the Yahoo Finance Backfill workflow_dispatch job.

    Fix 3: per-stock start date clamping now correctly filters new_days so
    dates earlier than the stock's own last stored date are never inserted,
    even when the batch-level fetch window is wider than any individual stock's gap.
    Fix 1: FetchTimeoutError caught explicitly with clear batch-range logging.
    """
    today   = date.today()
    total   = len(universe)
    updated = skipped = failed = timed_out = 0

    log.info("--update mode | %d stocks | today=%s | timeout: %ds/batch",
             total, today, FETCH_TIMEOUT_SEC)

    for i in range(0, total, BATCH_SIZE):
        batch = universe[i : i + BATCH_SIZE]

        # Pre-screen: download each file, find per-stock fetch window
        needs_fetch: list     = []
        per_stock_start: dict = {}
        fetch_start = today   # will be narrowed

        for code5 in batch:
            download_from_r2(code5)
            lib      = load_stock(code5)
            existing = lib.get("by_date", {})

            if existing:
                last = date.fromisoformat(max(existing.keys()))
                if last >= today:
                    skipped += 1
                    cleanup(code5)
                    continue
                start = last + timedelta(days=1)
            else:
                start = date(START_YEAR, 1, 1)

            needs_fetch.append(code5)
            per_stock_start[code5] = start
            if start < fetch_start:
                fetch_start = start

        if not needs_fetch:
            continue

        log.info("Batch %d–%d / %d | fetching %d stocks [%s → %s]",
                 i + 1, min(i + BATCH_SIZE, total), total,
                 len(needs_fetch), fetch_start, today)

        # Fix 1: FetchTimeoutError caught explicitly
        try:
            batch_data = fetch_batch(needs_fetch, fetch_start, today)
        except FetchTimeoutError as e:
            log.error(
                "Batch %d–%d TIMED OUT after %ds: %s — skipping %d stocks",
                i + 1, min(i + BATCH_SIZE, total), FETCH_TIMEOUT_SEC, e, len(needs_fetch),
            )
            timed_out += len(needs_fetch)
            failed    += len(needs_fetch)
            for code5 in needs_fetch:
                cleanup(code5)
            time.sleep(SLEEP_BATCH)
            continue
        except Exception as e:
            log.error("fetch_batch failed: %s", e)
            failed += len(needs_fetch)
            for code5 in needs_fetch:
                cleanup(code5)
            time.sleep(SLEEP_BATCH)
            continue

        for code5 in needs_fetch:
            new_days = batch_data.get(code5, {})
            if not new_days:
                log.warning("%s — no data returned", code5)
                failed += 1
                cleanup(code5)
                continue

            lib      = load_stock(code5)
            existing = lib.get("by_date", {})
            # Fix 3: use the per-stock start, not the batch-level fetch_start
            start    = per_stock_start[code5]
            added    = 0

            for ds, rec in new_days.items():
                if ds >= start.isoformat() and ds not in existing:  # never overwrite
                    existing[ds] = rec
                    added += 1

            if added == 0:
                skipped += 1
                cleanup(code5)
                continue

            lib["by_date"] = dict(sorted(existing.items()))
            save_stock(code5, lib)
            upload_to_r2(code5)
            cleanup(code5)
            log.info("%s — +%d days", code5, added)
            updated += 1

        time.sleep(SLEEP_BATCH)

    # Fix 5: include timed_out in summary
    log.info("--update done. updated=%d skipped=%d failed=%d (timed_out=%d)",
             updated, skipped, failed, timed_out)


# ── --rebuild mode ────────────────────────────────────────────────────────────

def run_rebuild(universe: list):
    """
    Full rebuild from START_YEAR — overwrites all existing data.

    Fix 4: FetchTimeoutError is now caught per year/batch and logged clearly
    instead of silently aborting the entire rebuild job via unhandled propagation.
    """
    today = date.today()
    total = len(universe)
    updated = failed = timed_out = 0

    log.info("--rebuild mode | %d stocks | %d → %d | timeout: %ds/batch",
             total, START_YEAR, today.year, FETCH_TIMEOUT_SEC)

    for i in range(0, total, BATCH_SIZE):
        batch = universe[i : i + BATCH_SIZE]
        log.info("Batch %d–%d / %d | rebuild",
                 i + 1, min(i + BATCH_SIZE, total), total)

        batch_all = {code5: {} for code5 in batch}

        for year in range(START_YEAR, today.year + 1):
            year_start = date(year, 1, 1)
            year_end   = min(date(year, 12, 31), today)
            if year_start > today:
                continue
            # Fix 4: catch FetchTimeoutError per year so one stalled year
            # doesn't abort the remaining years for this batch
            try:
                year_data = fetch_batch(batch, year_start, year_end)
                for code5 in batch:
                    batch_all[code5].update(year_data.get(code5, {}))
            except FetchTimeoutError as e:
                log.error(
                    "Batch %d–%d year %d TIMED OUT: %s — skipping year for this batch",
                    i + 1, min(i + BATCH_SIZE, total), year, e,
                )
                timed_out += 1
            except Exception as e:
                log.error("fetch_batch year %d failed: %s", year, e)
            time.sleep(2)

        for code5 in batch:
            days = batch_all[code5]
            if not days:
                failed += 1
                continue
            lib = {"meta": {}, "by_date": dict(sorted(days.items()))}
            save_stock(code5, lib)
            upload_to_r2(code5)
            cleanup(code5)
            updated += 1

        time.sleep(SLEEP_BATCH)

    # Fix 5: include timed_out in summary
    log.info("--rebuild done. updated=%d failed=%d (timed_out_years=%d)",
             updated, failed, timed_out)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Yahoo Finance OHLCV library daily updater"
    )
    ap.add_argument("--today",   action="store_true",
                    help="Daily mode: fetch last 7 days only (fast, for CI)")
    ap.add_argument("--update",  action="store_true",
                    help="Full incremental: fetch all missing days per stock")
    ap.add_argument("--rebuild", action="store_true",
                    help="Full rebuild from START_YEAR — overwrites everything")
    ap.add_argument("--code",    type=str, default=None,
                    help="Single stock only (e.g. --code 00700)")
    args = ap.parse_args()

    # Resolve universe
    if args.code:
        try:
            from ccass_universe import normalize_code as nc
            code5 = nc(args.code)
        except Exception:
            code5 = args.code.zfill(5)
        universe = [code5]
        log.info("Single-stock mode: %s", code5)
    else:
        universe = sorted(get_universe_codes())
        log.info("Universe: %d stocks", len(universe))

    # Dispatch mode — default to --today if nothing specified
    if args.rebuild:
        run_rebuild(universe)
    elif args.update:
        run_update(universe)
    else:
        # --today is default (covers both explicit --today and bare invocation)
        run_today(universe)


if __name__ == "__main__":
    main()
