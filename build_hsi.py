"""
build_hsi.py — HSI Weekly Price History Builder
=================================================
Source: Yahoo Finance API (yfinance)
Ticker: ^HSI (Hang Seng Index)

Stores weekly OHLCV data in hsi_weekly.json.

Structure:
{
  "meta": {
    "ticker":       "^HSI",
    "last_updated": "2026-04-12",
    "total_weeks":  N,
    "date_from":    "1986-12-29",
    "date_to":      "2026-04-11"
  },
  "weekly": {
    "1986-12-29": {"o": 2568.30, "h": 2568.30, "l": 2568.30, "c": 2568.30, "v": 0},
    "1987-01-05": {"o": 2636.50, "h": 2685.40, "l": 2589.50, "c": 2636.50, "v": 0},
    ...
  }
}

Usage:
  python build_hsi.py               # fetch all missing weeks (incremental)
  python build_hsi.py --rebuild     # re-fetch full history from scratch
  python build_hsi.py --dry-run     # preview without writing
"""

import argparse
import json
import logging
import os
from datetime import date, datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("build_hsi.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

TICKER    = "^HSI"
OUT_FILE  = "hsi_weekly.json"

# ── I/O ───────────────────────────────────────────────────────────────────────

def load_lib() -> dict:
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "weekly": {}}

def save_lib(lib: dict):
    weekly  = lib["weekly"]
    dates   = sorted(weekly.keys())
    lib["meta"] = {
        "ticker":       TICKER,
        "last_updated": date.today().isoformat(),
        "total_weeks":  len(weekly),
        "date_from":    dates[0]  if dates else "",
        "date_to":      dates[-1] if dates else "",
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(OUT_FILE) / 1e6
    log.info("Saved %s: %d weeks  %.2f MB  (%s → %s)",
             OUT_FILE, len(weekly), mb, lib["meta"]["date_from"], lib["meta"]["date_to"])

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_all() -> dict:
    """Fetch full ^HSI weekly history via yfinance. Returns {date_str: ohlcv}."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed — run: pip install yfinance")
        return {}

    log.info("Fetching %s weekly history (max period)…", TICKER)
    try:
        ticker = yf.Ticker(TICKER)
        df     = ticker.history(period="max", interval="1wk", auto_adjust=True)
        if df.empty:
            log.error("Empty dataframe returned from yfinance")
            return {}
        log.info("Fetched %d weeks (%s → %s)",
                 len(df), df.index[0].date(), df.index[-1].date())
        result = {}
        for ts, row in df.iterrows():
            ds = ts.strftime("%Y-%m-%d")
            result[ds] = {
                "o": round(float(row["Open"]),  2),
                "h": round(float(row["High"]),  2),
                "l": round(float(row["Low"]),   2),
                "c": round(float(row["Close"]), 2),
                "v": int(row.get("Volume", 0) or 0),
            }
        return result
    except Exception as e:
        log.error("Fetch failed: %s", e)
        return {}

# ── Build ─────────────────────────────────────────────────────────────────────

def build(dry_run: bool = False, rebuild: bool = False):
    lib    = load_lib() if not rebuild else {"meta": {}, "weekly": {}}
    stored = set(lib["weekly"].keys())
    log.info("Existing: %d weeks stored", len(stored))

    new_data = fetch_all()
    if not new_data:
        log.error("No data fetched — aborting")
        return

    added = 0
    for ds, rec in new_data.items():
        if rebuild or ds not in stored:
            lib["weekly"][ds] = rec
            added += 1

    log.info("Added/updated: %d weeks  (total: %d)", added, len(lib["weekly"]))

    if not dry_run:
        save_lib(lib)
    else:
        log.info("[dry-run] would write %d weeks to %s", len(lib["weekly"]), OUT_FILE)

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HSI Weekly History Builder")
    ap.add_argument("--rebuild",  action="store_true", help="Re-fetch full history")
    ap.add_argument("--dry-run",  action="store_true", help="Preview without writing")
    args = ap.parse_args()
    build(dry_run=args.dry_run, rebuild=args.rebuild)
