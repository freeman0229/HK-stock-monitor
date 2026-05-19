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

Usage:
  python build_tv_stock.py              # incremental update (normal daily use)
  python build_tv_stock.py --backfill   # backfill all stocks, never overwrites (initial R2 population)
  python build_tv_stock.py --rebuild    # full rebuild, overwrites everything
  python build_tv_stock.py --code 00700 # single stock
"""

import argparse
import json
import logging
import os
import re
import subprocess
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

def _sync_r2_tv_files():
    """Batch download all tv_*.json from R2 using aws s3 sync (fast, one call)."""
    if not R2_ENDPOINT:
        return
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
    """
    # Load existing data:
    # - rebuild: start fresh, overwrite everything
    # - all other modes: file pre-synced from R2 by _sync_r2_tv_files() in run()
    #   so os.path.exists(path) will be True for stocks already on R2
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
        # Both backfill and daily incremental: load from local disk.
        # Files are pre-synced from R2 by _sync_r2_tv_files() in run()
        # before build_stock() is called — so existing history is preserved.
        # Note: os.path.exists(path) is already handled by the elif branch above.
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

    # Sort by date and save
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

    upload_to_r2(code5)
    return added


# ── Main runner ───────────────────────────────────────────────────────────────

def run(universe: list, rebuild: bool = False, backfill: bool = False):
    log.info("Loading source files …")
    tv_all = load_all_turnover()
    sh_all = load_all_short()

    if not tv_all:
        log.error("No turnover data found — aborting")
        return

    if rebuild:
        # Full rebuild: process every stock, overwrite all existing dates
        active = set(universe)
    else:
        # Both backfill and daily incremental: sync existing tv_*.json from R2
        # in one batch call so we can merge new dates into existing history.
        log.info("Syncing existing tv_*.json from R2 …")
        _sync_r2_tv_files()

        if backfill:
            # Backfill: process all stocks in universe
            active = set(universe)
            log.info("Backfill: %d stocks to process", len(active))
        else:
            # Daily incremental: only process stocks active on the most recent
            # trading day — avoids uploading unchanged stocks every day.
            latest_ds = max(tv_all.keys())
            active    = set(tv_all[latest_ds].keys()) & set(universe)
            log.info("Incremental: %d stocks active on %s", len(active), latest_ds)

    total   = len(active)
    updated = skipped = 0

    for i, code5 in enumerate(sorted(active), 1):
        added = build_stock(code5, tv_all, sh_all, rebuild=rebuild, backfill=backfill)
        if added > 0:
            log.info("[%d/%d] %s — +%d days", i, total, code5, added)
            updated += 1
        else:
            skipped += 1

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
        code5   = normalize_code(args.code)
        tv_all  = load_all_turnover()
        sh_all  = load_all_short()
        added   = build_stock(code5, tv_all, sh_all, rebuild=args.rebuild, backfill=args.backfill)
        log.info("%s — %d days added", code5, added)
    else:
        universe = sorted(get_universe_codes())
        log.info("Universe: %d stocks | rebuild=%s", len(universe), args.rebuild)
        run(universe, rebuild=args.rebuild, backfill=args.backfill)


if __name__ == "__main__":
    main()
