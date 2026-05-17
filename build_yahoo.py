"""
build_yahoo.py - Daily incremental update for Yahoo Finance OHLCV library

Modes:
  --update              Incremental: fetch only missing days for each stock (default)
  --rebuild             Full refetch from START_YEAR, overwrite everything
  --code XXXXX          Process a single stock only (combine with --update or --rebuild)

Update logic (--update):
  - Load existing yahoo_{code5}.json
  - Find last stored date
  - If last date == today → skip (already complete)
  - If last date < today → fetch from last_date+1 to today
  - If file missing     → fetch full history from START_YEAR
  - Merge: never overwrite existing dates, only add new ones
  - Save → upload to R2

Upload:
  Requires R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY env vars.
  Each file is uploaded immediately after save (same pattern as yahoo_library.py).

Usage:
  python build_yahoo.py                        # incremental update, all stocks
  python build_yahoo.py --update               # same
  python build_yahoo.py --rebuild              # full rebuild, all stocks
  python build_yahoo.py --update --code 00700  # single stock, incremental
  python build_yahoo.py --rebuild --code 00700 # single stock, full rebuild
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
    SLEEP_BATCH,
    START_YEAR,
    fetch_batch,
    load_stock,
    save_stock,
    stock_path,
    to_yahoo_ticker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

R2_BUCKET   = "s3://hk-stock-monitor"
R2_ENDPOINT = os.environ.get("R2_ENDPOINT_URL", "")


# ── R2 upload ────────────────────────────────────────────────────────────────

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
        cmd = [
            "aws", "s3", "cp", path,
            f"{R2_BUCKET}/{path}",
            "--endpoint-url", R2_ENDPOINT,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log.debug("Uploaded %s to R2", path)
            return True
        else:
            log.warning("R2 upload failed for %s: %s", code5, r.stderr.strip())
            return False
    except Exception as e:
        log.warning("R2 upload exception for %s: %s", code5, e)
        return False


# ── Core: update a single stock ──────────────────────────────────────────────

def update_stock(code5: str, rebuild: bool = False) -> str:
    """
    Update one stock. Returns status string: 'skipped', 'updated', 'full', 'empty'.

    rebuild=False (incremental):
      - Load existing file
      - Find last stored date
      - Skip if already up to date
      - Fetch only missing range
      - Merge without overwriting existing dates

    rebuild=True:
      - Fetch full history from START_YEAR to today
      - Overwrite everything
    """
    today = date.today()

    if rebuild:
        lib          = {"meta": {}, "by_date": {}}
        fetch_start  = date(START_YEAR, 1, 1)
        fetch_end    = today
        mode         = "rebuild"
    else:
        lib = load_stock(code5)
        existing = lib.get("by_date", {})

        if existing:
            last_date_str = max(existing.keys())
            last_date     = date.fromisoformat(last_date_str)

            if last_date >= today:
                log.debug("%s — already up to date (%s)", code5, last_date_str)
                return "skipped"

            fetch_start = last_date + timedelta(days=1)
        else:
            # No existing data → full history fetch
            fetch_start = date(START_YEAR, 1, 1)

        fetch_end = today
        mode      = "incremental"

    if fetch_start > fetch_end:
        return "skipped"

    log.debug("%s — fetching %s → %s [%s]", code5, fetch_start, fetch_end, mode)

    batch_data = fetch_batch([code5], fetch_start, fetch_end)
    new_days   = batch_data.get(code5, {})

    if not new_days and rebuild:
        log.warning("%s — no data returned (rebuild)", code5)
        return "empty"

    if not new_days:
        # Nothing new from Yahoo for this range; file already current
        return "skipped"

    existing = lib.get("by_date", {})
    added    = 0

    if rebuild:
        # Full overwrite
        lib["by_date"] = dict(sorted(new_days.items()))
        added = len(new_days)
    else:
        # Incremental: only add dates not already stored — never overwrite
        for ds, rec in new_days.items():
            if ds not in existing:
                existing[ds] = rec
                added += 1
        lib["by_date"] = dict(sorted(existing.items()))

    if added == 0:
        return "skipped"

    save_stock(code5, lib)
    upload_to_r2(code5)

    log.info("%s — +%d days [%s → %s]", code5, added, fetch_start, fetch_end)
    return "full" if rebuild else "updated"


# ── Batch runner ─────────────────────────────────────────────────────────────

def run(universe: list, rebuild: bool = False):
    """
    Process all stocks in batches.
    Batches match yahoo_library.py conventions (BATCH_SIZE=20, SLEEP_BATCH=10s).
    For incremental updates only stocks with missing data are actually fetched;
    stocks that are already current are short-circuited without a network call.
    """
    today     = date.today()
    total     = len(universe)
    skipped   = updated = empty = failed = 0

    log.info("Starting %s run | %d stocks | today=%s",
             "rebuild" if rebuild else "incremental update", total, today)

    for i in range(0, total, BATCH_SIZE):
        batch = universe[i : i + BATCH_SIZE]

        # --- Incremental: pre-screen which stocks actually need fetching ---
        if not rebuild:
            needs_fetch   = []
            already_done  = []

            for code5 in batch:
                lib      = load_stock(code5)
                existing = lib.get("by_date", {})
                if existing:
                    last = date.fromisoformat(max(existing.keys()))
                    if last >= today:
                        already_done.append(code5)
                        continue
                needs_fetch.append(code5)

            skipped += len(already_done)

            if not needs_fetch:
                log.debug("Batch %d–%d: all %d stocks already up to date",
                          i + 1, min(i + BATCH_SIZE, total), len(batch))
                continue

            # Determine fetch window: earliest missing day across batch
            fetch_start = today  # will be narrowed below
            per_stock_start: dict[str, date] = {}

            for code5 in needs_fetch:
                lib      = load_stock(code5)
                existing = lib.get("by_date", {})
                if existing:
                    start = date.fromisoformat(max(existing.keys())) + timedelta(days=1)
                else:
                    start = date(START_YEAR, 1, 1)
                per_stock_start[code5] = start
                if start < fetch_start:
                    fetch_start = start

            fetch_end = today

            log.info("Batch %d–%d / %d | fetching %d stocks [%s → %s]",
                     i + 1, min(i + BATCH_SIZE, total), total,
                     len(needs_fetch), fetch_start, fetch_end)

            # Single network call for the whole batch over the required window
            batch_data = fetch_batch(needs_fetch, fetch_start, fetch_end)

            for code5 in needs_fetch:
                new_days = batch_data.get(code5, {})
                if not new_days:
                    log.warning("%s — no data returned", code5)
                    empty += 1
                    continue

                lib      = load_stock(code5)
                existing = lib.get("by_date", {})
                start    = per_stock_start[code5]
                added    = 0

                for ds, rec in new_days.items():
                    # Only add dates from this stock's own start onwards
                    if ds >= start.isoformat() and ds not in existing:
                        existing[ds] = rec
                        added += 1

                if added == 0:
                    skipped += 1
                    continue

                lib["by_date"] = dict(sorted(existing.items()))
                save_stock(code5, lib)
                upload_to_r2(code5)
                log.info("%s — +%d days", code5, added)
                updated += 1

        # --- Rebuild: fetch full history for each stock in batch ---
        else:
            log.info("Batch %d–%d / %d | rebuild",
                     i + 1, min(i + BATCH_SIZE, total), total)

            for year in range(START_YEAR, today.year + 1):
                year_start = date(year, 1, 1)
                year_end   = min(date(year, 12, 31), today)
                if year_start > today:
                    continue

                year_data = fetch_batch(batch, year_start, year_end)
                for code5 in batch:
                    lib = load_stock(code5)
                    lib["by_date"].update(year_data.get(code5, {}))
                    save_stock(code5, lib)
                time.sleep(2)

            for code5 in batch:
                lib = load_stock(code5)
                lib["by_date"] = dict(sorted(lib["by_date"].items()))
                save_stock(code5, lib)
                upload_to_r2(code5)
                if lib["by_date"]:
                    updated += 1
                else:
                    empty += 1

        time.sleep(SLEEP_BATCH)

    log.info(
        "Done. updated=%d skipped=%d empty=%d failed=%d",
        updated, skipped, empty, failed,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Incremental daily updater for Yahoo OHLCV per-stock library"
    )
    ap.add_argument(
        "--update",
        action="store_true",
        help="Incremental update (default): add only missing days, never overwrite",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="Full rebuild from START_YEAR — overwrites all existing data",
    )
    ap.add_argument(
        "--code",
        type=str,
        default=None,
        help="Process a single stock code only (e.g. --code 00700)",
    )
    args = ap.parse_args()

    # Default mode is --update
    rebuild = args.rebuild

    if args.code:
        from yahoo_library import normalize_code  # reuse normalisation if available
        try:
            from ccass_universe import normalize_code as nc
            code5 = nc(args.code)
        except Exception:
            code5 = args.code.zfill(5)
        log.info("Single-stock mode: %s | rebuild=%s", code5, rebuild)
        status = update_stock(code5, rebuild=rebuild)
        log.info("%s — %s", code5, status)
    else:
        universe = sorted(get_universe_codes())
        log.info("Universe: %d stocks", len(universe))
        run(universe, rebuild=rebuild)


if __name__ == "__main__":
    main()
