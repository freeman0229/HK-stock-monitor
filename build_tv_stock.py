"""
build_tv_stock.py — Per-stock TV+Short library builder
=======================================================
Reads all local turnover_{YYYY}.json and short_{YYYY}.json files,
merges them per stock per day, and uploads tv_{code5}.json to R2.

tv_{code5}.json schema:
  {
    "meta": {"code5": "00700", "last_updated": "2026-05-18", "total_days": N},
    "by_date": {
      "2025-01-02": {"close": 123.4, "high": 125.0, "low": 122.0, "vol": 1234567},
      "2026-05-15": {"close": 481.6, "high": 485.0, "low": 479.2,
                     "vol": 62450000, "tv": 26800000000, "vwap": 429.3,
                     "sv": 1234567, "st": 456789012}
    }
  }

Field availability by source:
  turnover_2025.json  → close, high, low, vol            (no tv/vwap)
  turnover_2026.json  → close, high, low, vol, tv, vwap  (full from 2026-02-02)
  short_2026.json     → sv, st                           (from 2026-02-02)

Rules:
  • All modes sync existing tv_*.json from R2 before processing (preserves history)
  • Never overwrites existing dates — only appends new ones
  • Scans all local turnover_YYYY.json and short_YYYY.json automatically
  • Uploads to R2 immediately after each stock is saved
  • Skips stocks with no data

Changelog:
  [Fix 1] Added _sync_r2_turnover_files(): downloads all turnover_YYYY.json source
          files from R2 before processing. Previously the script assumed turnover
          files were local, but they live in R2 (ephemeral CI runners start with
          none). Without this fix, only turnover_2026.json (written by the daily
          build_turnover.py job) was available, so tv_*.json files were missing
          all 2025 data. Called at the start of run() and the --code single-stock
          path so all modes benefit.
  [Fix 2] _sync_r2_tv_files() now accepts an optional `codes` set. In daily
          incremental mode, active stocks are determined first (from the latest
          date in turnover data), then only those tv_*.json files are fetched
          from R2 via targeted aws s3 cp calls instead of a full aws s3 sync.
          This cuts the daily tv-build step from ~61 min (sync all 2280 files)
          to ~2-3 min (sync only today's active stocks, typically ~1500-2000).
          Backfill and rebuild modes are unchanged — they still use full sync.
  [Fix 3] Parallel R2 downloads and uploads using ThreadPoolExecutor(R2_WORKERS=8).
          Each tv_{code5}.json is an independent file — no two workers share a path.
          Downloads: _sync_r2_tv_files() parallelised → ~50 min → ~6 min.
          Uploads: build_stock() upload_to_r2() parallelised in run() → ~20 min → ~3 min.
          The "never overwrite" guarantee is unchanged — each worker operates on
          its own local file with no shared mutable state.
  [Fix 4] _sync_r2_turnover_files() skips prior years in incremental mode.
          2025 data never changes day-to-day — downloading turnover_2025.json on
          every daily run wastes time. Only the current year is downloaded in
          incremental mode. Rebuild and backfill modes still download all years.
"""

import argparse
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from ccass_universe import get_universe_codes, normalize_code

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

R2_BUCKET   = "s3://hk-stock-monitor"
R2_ENDPOINT = os.environ.get("R2_ENDPOINT_URL", "")
R2_CDN      = "https://pub-0b0781d969ec4b38b173f889109244a9.r2.dev"

# [Fix 3] Parallel workers for R2 download and upload.
# Each worker touches only its own tv_{code5}.json — fully thread-safe.
# 8 is a conservative sweet spot; higher risks R2 rate-limiting.
R2_WORKERS = 8


# ── File paths ────────────────────────────────────────────────────────────────

def tv_path(code5: str) -> str:
    return f"tv_{code5}.json"


# ── Load local source files ───────────────────────────────────────────────────

def load_all_turnover() -> dict:
    """
    Load all local turnover_{YYYY}.json files.
    Returns {date_str: {code5: rec}} merged across all years.
    Never overwrites — later years take priority on date overlap (shouldn't happen).
    """
    by_date = {}
    pattern = re.compile(r"^turnover_(\d{4})\.json$")
    files   = sorted(f for f in os.listdir(".") if pattern.match(f))
    if not files:
        log.warning("No local turnover_YYYY.json files found")
        return by_date
    for fname in files:
        log.info("Loading %s …", fname)
        try:
            with open(fname, encoding="utf-8") as f:
                data = json.load(f)
            for ds, stocks in data.get("by_date", {}).items():
                if ds not in by_date:
                    by_date[ds] = {}
                for code, rec in stocks.items():
                    if isinstance(rec, dict):
                        by_date[ds][normalize_code(code)] = rec
        except Exception as e:
            log.error("Failed to load %s: %s", fname, e)
    log.info("Turnover: %d trading days loaded", len(by_date))
    return by_date


def load_all_short() -> dict:
    """
    Load all local short_{YYYY}.json files.
    Returns {date_str: {code5: {sv, st}}} merged across all years.
    """
    by_date = {}
    pattern = re.compile(r"^short_(\d{4})\.json$")
    files   = sorted(f for f in os.listdir(".") if pattern.match(f))
    if not files:
        log.warning("No local short_YYYY.json files found")
        return by_date
    for fname in files:
        log.info("Loading %s …", fname)
        try:
            with open(fname, encoding="utf-8") as f:
                data = json.load(f)
            for ds, stocks in data.get("by_date", {}).items():
                if ds not in by_date:
                    by_date[ds] = {}
                for code, rec in stocks.items():
                    if isinstance(rec, dict) and rec.get("sv", 0) > 0:
                        by_date[ds][normalize_code(code)] = {
                            "sv": int(rec["sv"]),
                            "st": int(rec.get("st", 0)),
                        }
        except Exception as e:
            log.error("Failed to load %s: %s", fname, e)
    log.info("Short: %d trading days loaded", len(by_date))
    return by_date


# ── R2 upload / download ──────────────────────────────────────────────────────

def upload_to_r2(code5: str) -> bool:
    if not R2_ENDPOINT:
        log.debug("R2_ENDPOINT_URL not set — skipping upload for %s", code5)
        return False
    path = tv_path(code5)
    if not os.path.exists(path):
        return False
    try:
        cmd = ["aws", "s3", "cp", path, f"{R2_BUCKET}/{path}",
               "--endpoint-url", R2_ENDPOINT, "--no-progress"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log.debug("Uploaded %s to R2", path)
            return True
        log.warning("R2 upload failed for %s: %s", code5, r.stderr.strip()[:200])
        return False
    except Exception as e:
        log.warning("R2 upload exception for %s: %s", code5, e)
        return False


# ── Per-stock build ───────────────────────────────────────────────────────────

def _sync_r2_turnover_files(incremental: bool = False):
    """
    Download turnover_YYYY.json source files from R2 if not already local.
    Called once at startup so load_all_turnover() sees the full history.

    [Fix 4] In incremental mode, only downloads the current year's file —
    prior years (e.g. 2025) never change day-to-day so there is no point
    re-downloading them on every daily run. Rebuild/backfill modes download
    all years as before.
    """
    if not R2_ENDPOINT:
        log.warning("R2_ENDPOINT_URL not set — cannot sync turnover files from R2")
        return
    try:
        # List all turnover_YYYY.json objects in the bucket
        cmd = ["aws", "s3", "ls", f"{R2_BUCKET}/",
               "--endpoint-url", R2_ENDPOINT]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log.warning("Could not list R2 bucket: %s", r.stderr.strip()[:200])
            return
        remote_files = re.findall(r"turnover_(\d{4})\.json", r.stdout)
        if not remote_files:
            log.warning("No turnover_YYYY.json files found in R2")
            return

        current_year = str(date.today().year)
        if incremental:
            # [Fix 4] Daily incremental: only need current year — prior years frozen
            years_to_download = [y for y in sorted(remote_files) if y >= current_year]
            log.info("Incremental mode: syncing turnover files for years %s only",
                     years_to_download)
        else:
            years_to_download = sorted(remote_files)
            log.info("Found turnover files in R2: %s", years_to_download)

        for year in years_to_download:
            fname = f"turnover_{year}.json"
            if os.path.exists(fname):
                log.info("%s already local — skipping download", fname)
                continue
            log.info("Downloading %s from R2 …", fname)
            cmd = ["aws", "s3", "cp", f"{R2_BUCKET}/{fname}", fname,
                   "--endpoint-url", R2_ENDPOINT, "--no-progress"]
            rc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if rc.returncode == 0:
                log.info("Downloaded %s", fname)
            else:
                log.warning("Failed to download %s: %s", fname, rc.stderr.strip()[:200])
    except Exception as e:
        log.warning("_sync_r2_turnover_files failed: %s", e)


def _sync_r2_tv_files(codes: set = None):
    """
    Download tv_*.json files from R2.

    [Fix 2] If `codes` is given, fetch only those files via parallel
    aws s3 cp calls (one per stock) — O(active) instead of O(universe).
    Used in daily incremental mode.

    [Fix 3] Downloads are parallelised with ThreadPoolExecutor(R2_WORKERS).
    Each worker writes to its own tv_{code5}.json — no shared file paths,
    fully thread-safe.

    If `codes` is None, falls back to aws s3 sync for the full bucket —
    used by backfill/rebuild modes where all stocks are needed.
    """
    if not R2_ENDPOINT:
        return

    if codes is not None:
        log.info("Syncing %d tv_*.json files from R2 (targeted, %d workers) …",
                 len(codes), R2_WORKERS)
        ok = fail = skip = 0

        def _download_one(code5: str):
            path = tv_path(code5)
            if os.path.exists(path):
                return "skip"
            try:
                cmd = ["aws", "s3", "cp",
                       f"{R2_BUCKET}/{path}", path,
                       "--endpoint-url", R2_ENDPOINT, "--no-progress"]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                return "ok" if r.returncode == 0 else "fail"
            except Exception as e:
                log.warning("R2 cp failed for %s: %s", code5, e)
                return "fail"

        with ThreadPoolExecutor(max_workers=R2_WORKERS) as executor:
            futures = {executor.submit(_download_one, c): c for c in sorted(codes)}
            for future in as_completed(futures):
                result = future.result()
                if result == "ok":
                    ok += 1
                elif result == "skip":
                    skip += 1
                else:
                    fail += 1

        log.info("Targeted sync done: downloaded=%d skipped=%d not_on_r2=%d",
                 ok, skip, fail)
    else:
        # Full sync — backfill / rebuild modes
        try:
            cmd = ["aws", "s3", "sync", R2_BUCKET, ".",
                   "--endpoint-url", R2_ENDPOINT,
                   "--exclude", "*",
                   "--include", "tv_*.json",
                   "--no-progress"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                count = len(re.findall(r"tv_\d{5}\.json", r.stdout))
                log.info("Synced %d tv_*.json files from R2", count)
            else:
                log.warning("R2 sync warning: %s", r.stderr.strip()[:200])
        except Exception as e:
            log.warning("R2 sync failed: %s -- will start fresh", e)


def _list_r2_tv_codes() -> set:
    """List all tv_{code5}.json files already on R2 via a single aws s3 ls call."""
    if not R2_ENDPOINT:
        return set()
    try:
        cmd = ["aws", "s3", "ls", f"{R2_BUCKET}/",
               "--endpoint-url", R2_ENDPOINT]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        codes = re.findall(r"tv_(\d{5})\.json", r.stdout)
        log.info("R2 existing tv files: %d", len(codes))
        return set(codes)
    except Exception as e:
        log.warning("Could not list R2 tv files: %s -- will process all", e)
        return set()


def build_stock(code5: str, tv_all: dict, sh_all: dict,
                rebuild: bool = False, backfill: bool = False) -> int:
    """
    Build/update tv_{code5}.json for one stock.
    Returns number of new days added.
    Does NOT upload to R2 — caller handles upload (parallelised in run()).
    """
    path = tv_path(code5)
    if rebuild:
        existing = {}
    elif os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f).get("by_date", {})
        except Exception:
            existing = {}
    else:
        existing = {}

    added = 0
    merged = dict(existing)  # copy — we'll add new dates only

    # Collect all dates this stock appears in turnover
    stock_dates = sorted(ds for ds, stocks in tv_all.items() if code5 in stocks)

    for ds in stock_dates:
        if not rebuild and ds in merged:
            continue  # never overwrite

        tv_rec = tv_all[ds][code5]
        close = float(tv_rec.get("close", 0.0))
        high  = float(tv_rec.get("high",  0.0))
        low   = float(tv_rec.get("low",   0.0))
        vol   = int(tv_rec.get("vol",     0))

        if close <= 0 and vol <= 0:
            continue  # skip empty records

        rec = {}
        if close > 0: rec["close"] = close
        if high  > 0: rec["high"]  = high
        if low   > 0: rec["low"]   = low
        if vol   > 0: rec["vol"]   = vol

        # tv and vwap — only present in turnover_2026 from 2026-02-02
        tv  = int(tv_rec.get("tv",   0))
        vwap = float(tv_rec.get("vwap", 0.0))
        if tv   > 0: rec["tv"]   = tv
        if vwap > 0: rec["vwap"] = round(vwap, 4)

        # Short data — merge from short library if available on this date
        sh_day = sh_all.get(ds, {})
        sh_rec = sh_day.get(code5)
        if sh_rec:
            rec["sv"] = sh_rec["sv"]
            rec["st"] = sh_rec["st"]

        merged[ds] = rec
        added += 1

    if added == 0 and not rebuild:
        return 0

    # Sort by date and save locally — upload handled by caller
    lib = {
        "meta": {
            "code5":        code5,
            "last_updated": date.today().isoformat(),
            "total_days":   len(merged),
        },
        "by_date": dict(sorted(merged.items())),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))

    return added


# ── Main runner ───────────────────────────────────────────────────────────────

def run(universe: list, rebuild: bool = False, backfill: bool = False):
    incremental = not rebuild and not backfill

    # [Fix 4] Skip prior-year turnover files in incremental mode — they never change
    log.info("Syncing turnover source files from R2 …")
    _sync_r2_turnover_files(incremental=incremental)

    log.info("Loading source files …")
    tv_all = load_all_turnover()
    sh_all = load_all_short()

    if not tv_all:
        log.error("No turnover data found — aborting")
        return

    if rebuild:
        active = set(universe)
        # Full sync needed — all stocks required
        log.info("Syncing all tv_*.json from R2 (rebuild) …")
        _sync_r2_tv_files(codes=None)
    else:
        if backfill:
            active = set(universe)
            log.info("Backfill: %d stocks to process", len(active))
            log.info("Syncing all tv_*.json from R2 (backfill) …")
            _sync_r2_tv_files(codes=None)
        else:
            # [Fix 2+3] Incremental: determine active stocks first, then parallel
            # targeted download of only their tv files from R2
            latest_ds = max(tv_all.keys())
            active    = set(tv_all[latest_ds].keys()) & set(universe)
            log.info("Incremental: %d stocks active on %s", len(active), latest_ds)
            _sync_r2_tv_files(codes=active)

    total   = len(active)
    updated = skipped = 0

    # [Fix 3] Build all stocks locally first, then parallel upload to R2.
    # build_stock() no longer calls upload_to_r2() — we batch-upload here.
    stocks_to_upload = []
    for i, code5 in enumerate(sorted(active), 1):
        added = build_stock(code5, tv_all, sh_all, rebuild=rebuild, backfill=backfill)
        if added > 0:
            log.info("[%d/%d] %s — +%d days", i, total, code5, added)
            updated += 1
            stocks_to_upload.append(code5)
        else:
            skipped += 1

    # Parallel upload — each worker uploads its own tv_{code5}.json to R2.
    # Writes go to independent R2 keys — no collision possible.
    if stocks_to_upload and R2_ENDPOINT:
        log.info("Uploading %d tv_*.json files to R2 (%d workers) …",
                 len(stocks_to_upload), R2_WORKERS)
        upload_ok = upload_fail = 0
        with ThreadPoolExecutor(max_workers=R2_WORKERS) as executor:
            futures = {
                executor.submit(upload_to_r2, code5): code5
                for code5 in stocks_to_upload
            }
            for future in as_completed(futures):
                if future.result():
                    upload_ok += 1
                else:
                    upload_fail += 1
        log.info("Upload done: ok=%d failed=%d", upload_ok, upload_fail)

    # Clean up local tv_ files (runner is ephemeral, but keep it tidy)
    for code5 in universe:
        path = tv_path(code5)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    log.info("Done. updated=%d skipped=%d", updated, skipped)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Per-stock TV+Short library builder → R2"
    )
    ap.add_argument("--rebuild", action="store_true",
                    help="Full rebuild — overwrites all existing dates")
    ap.add_argument("--backfill", action="store_true",
                    help="Backfill all stocks without overwriting — for initial R2 population")
    ap.add_argument("--code", type=str, default=None,
                    help="Process a single stock (e.g. --code 00700)")
    args = ap.parse_args()

    if args.code:
        code5 = normalize_code(args.code)
        log.info("Syncing turnover source files from R2 …")
        _sync_r2_turnover_files(incremental=False)  # single stock: always full history
        tv_all = load_all_turnover()
        sh_all = load_all_short()
        added  = build_stock(code5, tv_all, sh_all,
                             rebuild=args.rebuild, backfill=args.backfill)
        if added > 0:
            upload_to_r2(code5)
        log.info("%s — %d days added", code5, added)
    else:
        universe = sorted(get_universe_codes())
        log.info("Universe: %d stocks | rebuild=%s", len(universe), args.rebuild)
        run(universe, rebuild=args.rebuild, backfill=args.backfill)


if __name__ == "__main__":
    main()
