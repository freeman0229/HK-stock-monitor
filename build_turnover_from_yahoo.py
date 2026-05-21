"""
build_turnover_from_yahoo.py — Build turnover_{YYYY}.json from R2 yahoo data
=============================================================================
One-off historical backfill script. Do NOT modify build_turnover.py.

Source : https://pub-0b0781d969ec4b38b173f889109244a9.r2.dev/yahoo_{YYYY}.json
Output : turnover_{YYYY}.json  (same format as build_turnover.py)

Yahoo fields:  {open, high, low, close, vol}
Turnover fields written:
  high, low, close, vol  ← from yahoo directly
  prev_close             ← previous trading day's close from yahoo
  name_en, name_zh       ← "" (not available in yahoo)
  tv                     ← 0  (HKD turnover not available in yahoo)
  vwap                   ← 0  (not available in yahoo)

Rules:
  • Incremental by default — skips dates already in turnover_{YYYY}.json
  • Use --rebuild to overwrite existing data
  • Use --year to process a single year
  • Use --from-year / --to-year to process a range

Changelog:
  [Fix 1] fetch_yahoo(): replaced yearly-aggregate fetch (yahoo_{YYYY}.json) with
          per-stock fetch (yahoo_{code5}.json). R2 only stores per-stock files;
          yahoo_2025.json does not exist on R2, causing fetch_yahoo() to always
          return empty and producing an incomplete turnover_2025.json that was
          missing many stocks (e.g. 00005, 02800). New logic fetches each stock's
          file, filters to the requested year, and inverts to the expected
          {date: {code5: rec}} structure. Legacy local yahoo_{YYYY}.json still
          supported as a fallback for backwards compatibility.

Usage:
  python build_turnover_from_yahoo.py                        # 2025 only (default)
  python build_turnover_from_yahoo.py --year 2024            # single year
  python build_turnover_from_yahoo.py --from-year 2020 --to-year 2025
  python build_turnover_from_yahoo.py --rebuild --year 2025  # force overwrite
"""

import argparse
import json
import logging
import os
from datetime import date

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("build_turnover_from_yahoo.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

R2_BASE    = "https://pub-0b0781d969ec4b38b173f889109244a9.r2.dev"
DEFAULT_YEAR = 2025  # most common use case

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
}

# ── Turnover library I/O ──────────────────────────────────────────────────────

def _tv_path(year: int) -> str:
    return f"turnover_{year}.json"

def _load_tv(year: int) -> dict:
    p = _tv_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "by_date": {}}

def _save_tv(year: int, lib: dict):
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
        "source":       "yahoo_r2",
    }
    p = _tv_path(year)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(p) / 1e6
    log.info("Saved %s: %d days  %.2f MB", p, len(lib["by_date"]), mb)

# ── Fetch yahoo from R2 ───────────────────────────────────────────────────────

def fetch_yahoo(year: int) -> dict:
    """
    Build a by_date dict for the given year from per-stock yahoo_{code5}.json
    files on R2.  R2 stores one file per stock (not one per year), so we fetch
    each stock's file, filter to the requested year, and invert the structure:
        {date_str: {code5: {open, high, low, close, vol}}}

    Falls back to a local yahoo_{YYYY}.json aggregate file if it exists (legacy
    support for environments where the yearly file was pre-built).
    """
    # Legacy fallback: yearly aggregate file (local only — not on R2)
    local_path = f"yahoo_{year}.json"
    if os.path.exists(local_path):
        log.info("Loading local %s (legacy aggregate) ...", local_path)
        try:
            with open(local_path, encoding="utf-8") as f:
                data = json.load(f)
            by_date = data.get("by_date", {})
            log.info("yahoo_%d.json (local): %d dates loaded", year, len(by_date))
            return by_date
        except Exception as e:
            log.warning("Failed to read local %s: %s -- falling through to per-stock fetch", local_path, e)

    # Primary path: fetch per-stock yahoo_{code5}.json from R2 CDN
    # and invert to {date: {code5: rec}} structure filtered to `year`.
    from ccass_universe import get_universe_codes
    universe = sorted(get_universe_codes())
    log.info("Fetching per-stock yahoo files from R2 for year %d (%d stocks) ...", year, len(universe))

    year_str  = str(year)
    by_date: dict = {}
    ok = skipped = failed = 0

    for i, code5 in enumerate(universe):
        url = f"{R2_BASE}/yahoo_{code5}.json"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                skipped += 1
                continue
            r.raise_for_status()
            stock_data = r.json().get("by_date", {})
            for ds, rec in stock_data.items():
                if not ds.startswith(year_str):
                    continue
                if ds not in by_date:
                    by_date[ds] = {}
                by_date[ds][code5] = rec
            ok += 1
        except Exception as e:
            log.warning("Failed to fetch yahoo_%s.json: %s", code5, e)
            failed += 1

        if (i + 1) % 100 == 0:
            log.info("  Progress: %d / %d stocks | dates so far: %d", i + 1, len(universe), len(by_date))

    log.info("Per-stock fetch done: ok=%d skipped=%d failed=%d | dates=%d", ok, skipped, failed, len(by_date))
    return by_date

# ── Convert ───────────────────────────────────────────────────────────────────

def convert(yahoo_by_date: dict, existing_dates: set, rebuild: bool) -> dict:
    """
    Convert yahoo by_date to turnover by_date format.
    prev_close is derived from the previous date's close in the yahoo data.
    """
    # Sort dates so we can look up previous day
    all_dates = sorted(yahoo_by_date.keys())
    result    = {}

    for i, ds in enumerate(all_dates):
        if not rebuild and ds in existing_dates:
            continue  # already in turnover library

        day_stocks = yahoo_by_date[ds]
        if not day_stocks:
            continue

        # Get previous date's data for prev_close
        prev_ds   = all_dates[i - 1] if i > 0 else None
        prev_data = yahoo_by_date.get(prev_ds, {}) if prev_ds else {}

        converted = {}
        for code5, rec in day_stocks.items():
            high  = rec.get("high",  0) or 0
            low   = rec.get("low",   0) or 0
            close = rec.get("close", 0) or 0
            vol   = rec.get("vol",   0) or 0

            if close <= 0:
                continue  # skip records with no price

            prev_close = prev_data.get(code5, {}).get("close", 0) or 0

            converted[code5] = {
                "name_en":    "",
                "name_zh":    "",
                "prev_close": round(prev_close, 4),
                "high":       round(high,  4),
                "low":        round(low,   4),
                "close":      round(close, 4),
                "vol":        int(vol),
                "tv":         0,
                "vwap":       0,
            }

        if converted:
            result[ds] = converted

    return result

# ── Build ─────────────────────────────────────────────────────────────────────

def build_year(year: int, rebuild: bool = False):
    """Fetch yahoo data for one year and write to turnover_{YYYY}.json."""
    yahoo_by_date = fetch_yahoo(year)
    if not yahoo_by_date:
        log.warning("No yahoo data for %d — skipping", year)
        return

    tv_lib         = _load_tv(year) if not rebuild else {"meta": {}, "by_date": {}}
    existing_dates = set(tv_lib["by_date"].keys()) if not rebuild else set()

    log.info("turnover_%d: %d existing dates, %d yahoo dates",
             year, len(existing_dates), len(yahoo_by_date))

    new_data = convert(yahoo_by_date, existing_dates, rebuild)

    if not new_data:
        log.info("turnover_%d: nothing new to add", year)
        return

    log.info("turnover_%d: adding %d new dates", year, len(new_data))
    tv_lib["by_date"].update(new_data)
    tv_lib["by_date"] = dict(sorted(tv_lib["by_date"].items()))
    _save_tv(year, tv_lib)

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Build turnover_{YYYY}.json from R2 yahoo data (one-off backfill)"
    )
    ap.add_argument("--year",      type=int, default=None,
                    help=f"Single year to process (default: {DEFAULT_YEAR})")
    ap.add_argument("--from-year", type=int, default=DEFAULT_YEAR, dest="from_year",
                    help="Start year for range processing")
    ap.add_argument("--to-year",   type=int, default=DEFAULT_YEAR, dest="to_year",
                    help="End year for range processing")
    ap.add_argument("--rebuild",   action="store_true",
                    help="Overwrite existing dates (default: incremental)")
    args = ap.parse_args()

    if args.year:
        log.info("=== Processing year %d ===", args.year)
        build_year(args.year, rebuild=args.rebuild)
    else:
        for yr in range(args.from_year, args.to_year + 1):
            log.info("=== Processing year %d ===", yr)
            build_year(yr, rebuild=args.rebuild)

    log.info("Done.")
