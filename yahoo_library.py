"""
yahoo_library.py - Yahoo Finance OHLCV History Library

Fetches OHLCV data from Yahoo Finance for HK stocks.
Stores data in yahoo_{YYYY}.json files, one per year.

File structure matches turnover_{YYYY}.json but with 'open' added:
  meta: year, last_updated, total_days, source
  by_date: date_str -> code5 -> {open, high, low, close, vol}

Price convention:
  open, high, low  — raw unadjusted traded prices
  close            — dividend/split adjusted closing price (Adj Close)
  vol              — raw traded volume

Codes stored as 5-digit zero-padded strings.
"""

import json
import logging
import os
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

START_YEAR  = 1995
SLEEP_BATCH = 1.5
BATCH_SIZE  = 100
MAX_RETRIES = 3
RETRY_SLEEP = 10


def lib_path(year: int) -> str:
    return f"yahoo_{year}.json"


def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year, "source": "yahoo"}, "by_date": {}}


def save_year(year: int, lib: dict):
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
        "source":       "yahoo",
    }
    p = lib_path(year)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(p) / 1e6
    log.info("Saved %s: %d days  %.2f MB", p, len(lib["by_date"]), mb)


def all_stored_codes_for_year(year: int) -> set:
    p = lib_path(year)
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        by_date = json.load(f).get("by_date", {})
    codes = set()
    for day_data in by_date.values():
        codes.update(day_data.keys())
    return codes


def to_yahoo_ticker(code5: str) -> str:
    return str(int(code5)) + ".HK"


def from_yahoo_ticker(ticker: str) -> str:
    return normalize_code(ticker.replace(".HK", "").replace(".hk", ""))


def fetch_batch(codes: list, start: date, end: date) -> dict:
    """Fetch OHLCV for a batch of codes with retry. Returns {code5: {date_str: rec}}."""
    tickers = [to_yahoo_ticker(c) for c in codes]
    result  = {c: {} for c in codes}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = yf.download(
                tickers,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,  # keep raw OHLC; use Adj Close separately for close
                progress=False,
                group_by="ticker",
                threads=True,
            )
            if raw is None or raw.empty:
                return result

            for ticker, code5 in zip(tickers, codes):
                try:
                    if len(tickers) == 1:
                        df = raw
                    else:
                        lvl = raw.columns.get_level_values(0)
                        if ticker not in lvl:
                            continue
                        df = raw[ticker]
                    if df is None or df.empty:
                        continue
                    for idx, row in df.iterrows():
                        ds = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                        # open/high/low: raw unadjusted traded prices
                        o = float(row.get("Open",   row.get("open",   0)) or 0)
                        h = float(row.get("High",   row.get("high",   0)) or 0)
                        l = float(row.get("Low",    row.get("low",    0)) or 0)
                        # close: dividend/split adjusted (Adj Close) for price continuity
                        c = float(row.get("Adj Close", row.get("adj close",
                                  row.get("Close",     row.get("close", 0)))) or 0)
                        v = int(  row.get("Volume", row.get("volume", 0)) or 0)
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

        except Exception as e:
            if attempt < MAX_RETRIES:
                log.warning("fetch_batch attempt %d/%d failed: %s - retrying in %ds",
                            attempt, MAX_RETRIES, e, RETRY_SLEEP * attempt)
                time.sleep(RETRY_SLEEP * attempt)
            else:
                log.error("fetch_batch failed after %d attempts: %s", MAX_RETRIES, e)

    return result


def fetch_and_save_year(year: int, universe: list, rebuild: bool = False,
                        upload_fn=None):
    """
    Fetch all universe stocks for a given year from Yahoo Finance.
    Saves incrementally after every batch - timeouts do not lose completed work.
    Calls upload_fn(year) after each save if provided (for immediate R2 upload).
    """
    start = date(year, 1, 1)
    end   = date(year, 12, 31)
    if end > date.today():
        end = date.today() - timedelta(days=1)
    if start > date.today():
        log.info("Yahoo %d: future year - skipping", year)
        return

    lib = load_year(year) if not rebuild else {"meta": {}, "by_date": {}}

    if not rebuild:
        existing = all_stored_codes_for_year(year)
        to_fetch = [c for c in universe if c not in existing]
        log.info("Yahoo %d: %d to fetch (%d already stored)", year, len(to_fetch), len(existing))
    else:
        to_fetch = list(universe)
        log.info("Yahoo %d (rebuild): %d codes", year, len(to_fetch))

    if not to_fetch:
        log.info("Yahoo %d: nothing to fetch", year)
        return

    saved = failed = 0

    for i in range(0, len(to_fetch), BATCH_SIZE):
        batch = to_fetch[i:i + BATCH_SIZE]
        log.info("Yahoo %d: batch %d-%d / %d",
                 year, i + 1, min(i + BATCH_SIZE, len(to_fetch)), len(to_fetch))

        batch_data = fetch_batch(batch, start, end)

        for code5, days in batch_data.items():
            if not days:
                failed += 1
                continue
            for ds, rec in days.items():
                lib["by_date"].setdefault(ds, {})[code5] = rec
            saved += 1

        lib["by_date"] = dict(sorted(lib["by_date"].items()))
        save_year(year, lib)

        if upload_fn:
            try:
                upload_fn(year)
            except Exception as e:
                log.warning("upload_fn failed for year %d: %s", year, e)

        time.sleep(SLEEP_BATCH)

    log.info("Yahoo %d: done. Saved=%d Failed=%d", year, saved, failed)


def patch_turnover_2026(universe: list):
    """
    Find dates in turnover_2026.json where high=0 for most stocks.
    Fill open, high, low, prev_close from Yahoo adjusted prices.
    Preserves all HKEX fields (vol, tv, vwap, name_en, name_zh, close).
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

    log.info("patch-2026: fetching %s to %s for %d stocks",
             fetch_start, fetch_end, len(universe))

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


if __name__ == "__main__":
    import argparse
    from ccass_universe import get_universe_codes

    ap = argparse.ArgumentParser(description="Yahoo Finance OHLCV library builder")
    ap.add_argument("--year",        type=int)
    ap.add_argument("--from-year",   type=int, default=START_YEAR, dest="from_year")
    ap.add_argument("--to-year",     type=int, default=date.today().year,     dest="to_year")
    ap.add_argument("--rebuild",     action="store_true")
    ap.add_argument("--patch-2026",  action="store_true", dest="patch_2026")
    args = ap.parse_args()

    universe = list(get_universe_codes())
    log.info("Universe: %d stocks", len(universe))

    if args.patch_2026:
        patch_turnover_2026(universe)
    elif args.year:
        fetch_and_save_year(args.year, universe, rebuild=args.rebuild)
    else:
        for yr in range(args.from_year, args.to_year + 1):
            log.info("=== Year %d ===", yr)
            fetch_and_save_year(yr, universe, rebuild=args.rebuild)
