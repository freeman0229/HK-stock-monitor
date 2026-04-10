"""
ccass_library.py — CCASS Southbound Shareholding Library
==========================================================
Fetches daily CCASS southbound (HK stocks) shareholding for all stocks,
last 12 months, from mutualmarket_c.aspx — simpler GET-based endpoint.

Library files: ccass_{YYYY}.json — one per year

Structure:
{
  "meta": {"year": 2026, "last_updated": "...", "total_days": N, "total_records": N},
  "by_date": {
    "2026-03-14": {
      "00700": {"sh": 4521000000, "pct": 8.43, "name": "騰訊控股"},
      ...
    }
  }
}

Usage:
  python ccass_library.py              # build last 12 months
  python ccass_library.py --update     # only fetch dates newer than last stored
  python ccass_library.py --query 00700
  python ccass_library.py --query 00700 --weeks 52
  python ccass_library.py --date 2026-03-14
  python ccass_library.py --export 00700
"""

import argparse
import json
import logging
import os
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

from ccass_universe import normalize_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www3.hkexnews.hk/",
}
# Chinese mutualmarket page — cleaner table, GET-based with txtShareholdingDate param
BASE_URL   = "https://www3.hkexnews.hk/sdw/search/mutualmarket_c.aspx"
SLEEP_SEC  = 1.5
START_DATE = date(2025, 3, 23)  # Note: 2025-03-23 is a Sunday — first
                                # trading day is 2025-03-24


def _clean_cell(s: str) -> str:
    """Strip leading 'Label: ' prefix from a table cell value."""
    return re.sub(r'^[^:：]+[:：]\s*', '', s).strip()
                                # Note: 2025-03-23 is a Sunday — first
                                # trading day is 2025-03-24


# ── Trading day helpers ───────────────────────────────────────────────────────

try:
    import holidays as hol
    _HK_HOLIDAYS = hol.HongKong()
except ImportError:
    _HK_HOLIDAYS = set()

# Mainland China holidays — CCASS southbound settles only on days both
# HK and mainland exchanges are open. These are CN-only holidays where
# HK is open (CNY extension, Golden Week, etc.).
_CN_HOLIDAY_DATES = {
    # 2024
    "2024-01-01","2024-02-12","2024-02-13","2024-02-14","2024-02-15","2024-02-16",
    "2024-04-04","2024-04-05","2024-05-01","2024-05-02","2024-05-03",
    "2024-06-10","2024-09-16","2024-09-17",
    "2024-10-01","2024-10-02","2024-10-03","2024-10-04","2024-10-07",
    # 2025
    "2025-01-01","2025-01-27","2025-01-28","2025-01-29","2025-01-30","2025-01-31",
    "2025-04-04","2025-05-01","2025-05-02","2025-05-05",
    "2025-06-02",
    "2025-10-01","2025-10-02","2025-10-03","2025-10-06","2025-10-07","2025-10-08",
    # 2026
    "2026-01-01","2026-01-28","2026-01-29","2026-01-30","2026-02-02","2026-02-03","2026-02-04",
    "2026-04-06","2026-05-01","2026-05-04","2026-05-05",
    "2026-06-19",
    "2026-10-01","2026-10-02","2026-10-05","2026-10-06","2026-10-07","2026-10-08",
}

try:
    _CN_LIB = hol.China()
except Exception:
    _CN_LIB = set()

def _is_cn_holiday(d: date) -> bool:
    return d.isoformat() in _CN_HOLIDAY_DATES or d in _CN_LIB

def is_trading_day(d: date) -> bool:
    """True only when both HK and mainland exchanges are open."""
    if d.weekday() >= 5:
        return False
    if d in _HK_HOLIDAYS:
        return False
    if _is_cn_holiday(d):
        return False
    return True

def last_trading_day(d: date) -> date:
    """Return the most recent trading day on or before d."""
    for _ in range(14):   # safety limit — no holiday run longer than 2 weeks
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise ValueError(f"last_trading_day: no trading day found within 14 days of {d}")

def all_trading_days(start: date, end: date) -> list:
    days, d = [], start
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"ccass_{year}.json"

def all_years() -> list:
    return list(range(START_DATE.year, date.today().year + 1))

def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year}, "by_date": {}}

def save_year(year: int, lib: dict):
    dates = lib["by_date"]
    total = sum(len(v) for v in dates.values())
    lib["meta"] = {
        "year":          year,
        "last_updated":  date.today().isoformat(),
        "total_days":    len(dates),
        "total_records": total,
    }
    with open(lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(lib_path(year)) / 1e6
    log.info("Saved ccass_%d.json: %d days, %d records, %.1f MB",
             year, len(dates), total, mb)

def all_stored_dates() -> set:
    stored = set()
    for year in all_years():
        if os.path.exists(lib_path(year)):
            with open(lib_path(year), encoding="utf-8") as f:
                stored.update(json.load(f).get("by_date", {}).keys())
    return stored


# ── API for main.py ───────────────────────────────────────────────────────────

def save_day(d, records: dict):
    """
    Save one day's CCASS data into the library.
    records: {code: {"sh": int, "pct": float, "name": str}}
    Accepts a datetime or date object, or a YYYY-MM-DD string.
    """
    if not records:
        return
    ds   = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
    year = int(ds[:4])
    lib  = load_year(year)
    lib["by_date"][ds] = records
    save_year(year, lib)
    log.info("Saved CCASS to ccass_%d.json: %s (%d stocks)", year, ds, len(records))


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_ccass(d: date) -> dict | None:
    """
    Fetch all HK southbound CCASS holdings for date d.
    Uses GET + POST with ASP.NET viewstate for date selection.
    Returns {stock_code: {"sh": int, "pct": float}} or None.
    """
    date_str = d.strftime("%Y/%m/%d")
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)

        # First GET to get viewstate (some dates still need it)
        r1 = sess.get(f"{BASE_URL}?t=hk", timeout=30)
        r1.raise_for_status()
        soup1 = BeautifulSoup(r1.text, "html.parser")

        def hv(name):
            tag = soup1.find("input", {"name": name})
            return tag["value"] if tag else ""

        # POST with date
        r2 = sess.post(f"{BASE_URL}?t=hk", data={
            "__EVENTTARGET":        "btnSearch",
            "__EVENTARGUMENT":      "",
            "__VIEWSTATE":          hv("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": hv("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION":    hv("__EVENTVALIDATION"),
            "txtShareholdingDate":  date_str,
            "t":                    "hk",
        }, timeout=60)
        r2.raise_for_status()

        soup2 = BeautifulSoup(r2.text, "html.parser")

        # Parse table — each row has label:value format
        # "股份代號:  00700" | "名稱:  騰訊控股" | "持股量:  4521000000" | "百分比:  8.43%"
        records = {}
        for tr in soup2.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 4:
                continue

            code_raw = _clean_cell(tds[0]).replace(",", "")
            name_raw = _clean_cell(tds[1]).strip()
            sh_raw   = _clean_cell(tds[2]).replace(",", "")
            pct_raw  = _clean_cell(tds[3]).replace("%", "").strip()

            if not code_raw.isdigit() or not sh_raw.isdigit():
                continue

            code = normalize_code(code_raw)
            records[code] = {
                "sh":   int(sh_raw),
                "pct":  float(pct_raw) if pct_raw else 0.0,
                "name": name_raw,
            }

        if not records:
            log.debug("CCASS: 0 records for %s — data may not be published yet", date_str)
            return {}

        log.info("CCASS %s: %d stocks", date_str, len(records))
        return records

    except Exception as e:
        log.error("fetch_ccass failed (%s): %s", date_str, e)
        return None


# ── Build / update ────────────────────────────────────────────────────────────


def _dates_missing_name() -> set:
    """Return stored dates where the name field is absent from records."""
    missing = set()
    for year in all_years():
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds, day in by_date.items():
            sample = next((v for v in day.values() if isinstance(v, dict)), None)
            if sample and "name" not in sample:
                missing.add(ds)
    return missing


def build(update_only: bool = False, fix_names: bool = False):
    stored  = all_stored_dates()
    end     = last_trading_day(date.today() - timedelta(days=1))
    start   = last_trading_day(START_DATE)
    trading = all_trading_days(start, end)

    # HKEX CCASS mutualmarket only keeps ~12 months of rolling data.
    # Dates older than 12 months will always return 0 records — stop retrying them.
    cutoff_12m = date.today() - timedelta(days=366)

    if update_only and stored:
        last = date.fromisoformat(max(stored))
        trading = [d for d in trading if d > last]
        log.info("Update: %d new days after %s", len(trading), last.isoformat())
    else:
        trading = [d for d in trading if d.isoformat() not in stored]
        log.info("Build: %d trading days to fetch", len(trading))

    if fix_names:
        # Also include stored dates where name field is missing,
        # but only within HKEX's 12-month retention window
        no_name = _dates_missing_name()
        extra = [d for d in all_trading_days(start, end)
                 if d.isoformat() in no_name
                 and d not in trading
                 and d >= cutoff_12m]
        skipped_old = [d for d in all_trading_days(start, end)
                       if d.isoformat() in no_name and d < cutoff_12m]
        if extra:
            log.info("fix-names: %d dates need name backfill", len(extra))
            trading = sorted(set(trading) | set(extra))
        if skipped_old:
            log.info("fix-names: skipping %d dates older than 12 months "
                     "(HKEX data expired): %s%s",
                     len(skipped_old),
                     ", ".join(d.isoformat() for d in skipped_old[:3]),
                     "..." if len(skipped_old) > 3 else "")

    if not trading:
        log.info("Already up to date")
        return

    # Group by year
    by_year: dict = {}
    for d in trading:
        by_year.setdefault(d.year, []).append(d)

    missing = []
    for year, days in sorted(by_year.items()):
        lib = load_year(year)
        log.info("── Year %d: %d days ──", year, len(days))

        for i, d in enumerate(days, 1):
            log.info("  [%d/%d] %s", i, len(days), d.isoformat())
            records = fetch_ccass(d)
            if records is None:
                # Network/parse failure — retry next run
                missing.append(d)
                continue
            if not records:
                # Empty response — data not published yet, skip silently
                log.debug("  %s: no data published yet — skipping", d.isoformat())
                continue
            lib["by_date"][d.isoformat()] = records
            time.sleep(SLEEP_SEC)

            if i % 20 == 0:
                save_year(year, lib)

        save_year(year, lib)

    # Summary
    log.info("── Summary ──")
    total_mb = 0
    for year in all_years():
        p = lib_path(year)
        if os.path.exists(p):
            mb = os.path.getsize(p) / 1e6
            total_mb += mb
            with open(p) as f:
                m = json.load(f).get("meta", {})
            log.info("  ccass_%d.json  %d days  %d records  %.1f MB",
                     year, m.get("total_days", 0), m.get("total_records", 0), mb)
    log.info("  Total: %.1f MB", total_mb)

    if missing:
        log.warning("%d dates had no data: %s%s",
                    len(missing),
                    ", ".join(d.isoformat() for d in missing[:5]),
                    "..." if len(missing) > 5 else "")


# ── Query helpers ─────────────────────────────────────────────────────────────

def stock_history(code: str) -> list:
    code5 = normalize_code(code)
    rows  = []
    for year in all_years():
        if not os.path.exists(lib_path(year)):
            continue
        with open(lib_path(year), encoding="utf-8") as f:
            lib = json.load(f)
        for ds, stocks in lib.get("by_date", {}).items():
            if code5 in stocks:
                rows.append((ds, stocks[code5]))
    return sorted(rows)

def query_stock(code: str, weeks: int = None):
    code5 = normalize_code(code)
    hist  = stock_history(code5)
    if not hist:
        print(f"No CCASS data for {code5}")
        return
    if weeks:
        cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
        hist = [(ds, d) for ds, d in hist if ds >= cutoff]

    name = get_ccass_name(code5) or code5
    print(f"\n{code5} — {name}  ({len(hist)} days)")
    print(f"{'Date':<12} {'Shareholding':>18} {'% Listed':>10} {'Δ':>14}")
    print("─" * 58)
    prev_sh = None
    for ds, data in hist:
        sh    = data.get("sh", 0)
        pct   = data.get("pct", 0.0)
        delta = ""
        if prev_sh is not None:
            d = sh - prev_sh
            delta = f"{d:+,}" if d != 0 else "—"
        print(f"{ds:<12} {sh:>18,} {pct:>9.2f}% {delta:>14}")
        prev_sh = sh

def query_date(ds: str):
    year = int(ds[:4])
    if not os.path.exists(lib_path(year)):
        print(f"No library for {year}"); return
    with open(lib_path(year), encoding="utf-8") as f:
        lib = json.load(f)
    if ds not in lib["by_date"]:
        print(f"Date {ds} not in library"); return
    records = lib["by_date"][ds]
    rows = sorted(records.items(), key=lambda x: -x[1].get("pct", 0))
    print(f"\n{ds} — {len(records)} stocks (top 100 by % held)")
    print(f"{'Code':<8} {'Name':<36} {'Shareholding':>18} {'%':>8}")
    print("─" * 74)
    for code, data in rows[:100]:
        zh = data.get("name", "") or get_ccass_name(code) or ""
        print(f"{code:<8} {zh[:35]:<36} {data['sh']:>18,} {data['pct']:>7.2f}%")

def export_stock_csv(code: str):
    code5 = normalize_code(code)
    hist  = stock_history(code5)
    if not hist:
        print(f"No CCASS data for {code5}"); return
    name_zh = get_ccass_name(code5) or ""
    rows = []
    prev_sh = None
    for ds, data in hist:
        sh  = data.get("sh", 0)
        pct = data.get("pct", 0.0)
        delta = sh - prev_sh if prev_sh is not None else None
        rows.append({"date": ds, "stock_code": code5,
                     "name_zh": data.get("name", "") or name_zh,
                     "shareholding": sh, "pct_listed": pct, "delta": delta})
        prev_sh = sh
    path = f"{code5}_ccass_history.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"Exported {len(rows)} rows to {path}")


def get_ccass_name(code: str) -> str | None:
    """Return the most recent Chinese name for a stock from CCASS records."""
    code5 = normalize_code(code)
    for year in sorted(all_years(), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p): continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            entry = by_date[ds].get(code5)
            if entry and isinstance(entry, dict):
                name = entry.get("name")
                if name:
                    return name
    return None


def get_pct_history(code: str, n: int, before: str) -> list:
    """
    Return the last n pct_listed values for a stock strictly before date `before`
    (YYYY-MM-DD), sorted newest-first. Used by main.py for pct_avg5/20.
    """
    code5  = normalize_code(code)
    result = []
    for year in sorted(all_years(), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before:
                continue
            entry = by_date[ds].get(code5, {})
            pct   = entry.get("pct", 0.0)
            if pct > 0:
                result.append(pct)
            if len(result) >= n:
                return result
    return result


def get_sh_history(code: str, n: int, before: str) -> list:
    """
    Return the last n shareholding values for a stock strictly before `before`,
    sorted newest-first. Used by main.py for delta and consec computation.
    """
    code5  = normalize_code(code)
    result = []
    for year in sorted(all_years(), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before:
                continue
            entry = by_date[ds].get(code5, {})
            sh    = entry.get("sh", 0)
            if sh > 0:
                result.append(sh)
            if len(result) >= n:
                return result
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CCASS Southbound Library")
    ap.add_argument("--update",     action="store_true", help="Fetch only new dates")
    ap.add_argument("--fix-names",  action="store_true", help="Re-fetch dates missing the name field")
    ap.add_argument("--query",  metavar="CODE",        help="Stock history e.g. 00700")
    ap.add_argument("--date",   metavar="YYYY-MM-DD",  help="All stocks for a date")
    ap.add_argument("--weeks",  type=int,              help="Limit query to last N weeks")
    ap.add_argument("--export", metavar="CODE",        help="Export to CSV")
    args = ap.parse_args()

    if   args.query:  query_stock(args.query, args.weeks)
    elif args.date:   query_date(args.date)
    elif args.export: export_stock_csv(args.export)
    else:             build(update_only=args.update, fix_names=getattr(args, "fix_names", False))

