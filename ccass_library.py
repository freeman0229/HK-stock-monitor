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

import os, sys, json, time, re, logging, argparse
import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from stock_ref import STOCKS
except ImportError:
    STOCKS = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www3.hkexnews.hk/",
}
# Chinese mutualmarket page — cleaner table, GET-based with txtShareholdingDate param
BASE_URL   = "https://www3.hkexnews.hk/sdw/search/mutualmarket_c.aspx"
SLEEP_SEC  = 1.5
START_DATE = date(2025, 1, 1)


# ── Trading day helpers ───────────────────────────────────────────────────────

try:
    import holidays as hol
    _HK_HOLIDAYS = hol.HongKong()
except ImportError:
    _HK_HOLIDAYS = set()

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HK_HOLIDAYS

def last_trading_day(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d

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

            def clean(s):
                return re.sub(r'^[^:：]+[:：]\s*', '', s).strip()

            code_raw = clean(tds[0]).replace(",", "")
            name_raw = clean(tds[1]).strip()
            sh_raw   = clean(tds[2]).replace(",", "")
            pct_raw  = clean(tds[3]).replace("%", "").strip()

            if not code_raw.isdigit() or not sh_raw.isdigit():
                continue

            code = str(int(code_raw)).zfill(5)
            records[code] = {
                "sh":   int(sh_raw),
                "pct":  float(pct_raw) if pct_raw else 0.0,
                "name": name_raw,
            }

        if not records:
            log.warning("CCASS: 0 records for %s", date_str)
            return None

        log.info("CCASS %s: %d stocks", date_str, len(records))
        return records

    except Exception as e:
        log.error("fetch_ccass failed (%s): %s", date_str, e)
        return None


# ── Build / update ────────────────────────────────────────────────────────────

def build(update_only: bool = False):
    stored  = all_stored_dates()
    end     = last_trading_day(date.today() - timedelta(days=1))
    start   = last_trading_day(START_DATE)
    trading = all_trading_days(start, end)

    if update_only and stored:
        last = date.fromisoformat(max(stored))
        trading = [d for d in trading if d > last]
        log.info("Update: %d new days after %s", len(trading), last.isoformat())
    else:
        trading = [d for d in trading if d.isoformat() not in stored]
        log.info("Build: %d trading days to fetch", len(trading))

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
                missing.append(d)
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
    code5 = code.zfill(5)
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
    hist = stock_history(code)
    if not hist:
        print(f"No CCASS data for {code.zfill(5)}")
        return
    if weeks:
        cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
        hist = [(ds, d) for ds, d in hist if ds >= cutoff]

    name = STOCKS.get(code.zfill(5), {}).get("zh", code.zfill(5))
    print(f"\n{code.zfill(5)} — {name}  ({len(hist)} days)")
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
        zh = STOCKS.get(code, {}).get("zh", "")
        print(f"{code:<8} {zh[:35]:<36} {data['sh']:>18,} {data['pct']:>7.2f}%")

def export_stock_csv(code: str):
    hist = stock_history(code)
    if not hist:
        print(f"No CCASS data for {code.zfill(5)}"); return
    rows = []
    prev_sh = None
    for ds, data in hist:
        sh  = data.get("sh", 0)
        pct = data.get("pct", 0.0)
        delta = sh - prev_sh if prev_sh is not None else None
        rows.append({"date": ds, "stock_code": code.zfill(5),
                     "name_zh": STOCKS.get(code.zfill(5), {}).get("zh", ""),
                     "shareholding": sh, "pct_listed": pct, "delta": delta})
        prev_sh = sh
    path = f"{code.zfill(5)}_ccass_history.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"Exported {len(rows)} rows to {path}")


def get_ccass_name(code: str) -> str | None:
    """Return the most recent Chinese name for a stock from CCASS records."""
    code5 = code.zfill(5)
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


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CCASS Southbound Library")
    ap.add_argument("--update", action="store_true",  help="Fetch only new dates")
    ap.add_argument("--query",  metavar="CODE",        help="Stock history e.g. 00700")
    ap.add_argument("--date",   metavar="YYYY-MM-DD",  help="All stocks for a date")
    ap.add_argument("--weeks",  type=int,              help="Limit query to last N weeks")
    ap.add_argument("--export", metavar="CODE",        help="Export to CSV")
    args = ap.parse_args()

    if   args.query:  query_stock(args.query, args.weeks)
    elif args.date:   query_date(args.date)
    elif args.export: export_stock_csv(args.export)
    else:             build(update_only=args.update)


# ── API for main.py ───────────────────────────────────────────────────────────

def get_pct_history(code: str, n: int, before: str) -> list:
    """
    Return the last n pct_listed values for a stock strictly before date `before`
    (YYYY-MM-DD), sorted newest-first. Used by main.py for pct_avg5/20.
    """
    code5  = code.zfill(5)
    result = []
    # Scan from most recent year backwards
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
    code5  = code.zfill(5)
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
