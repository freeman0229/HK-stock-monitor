"""
short_library.py — HKEX Daily Short Selling Library
=====================================================
Stores daily short selling data for all stocks from the HKEX
daily short sell file (ashtmain.htm).

Source: https://www.hkex.com.hk/eng/stat/smstat/ssturnover/ncms/ashtmain.htm

Library files: short_{YYYY}.json — one per year

Structure:
{
  "meta": {"year": 2026, "last_updated": "...", "total_days": N},
  "by_date": {
    "2026-03-19": {
      "00700": {"sv": 1234567, "st": 456789012.0, "name": "騰訊控股"},
      ...
    }
  }
}

sv   = short volume (shares)
st   = short turnover (HKD)
name = Chinese stock name from ashtmain_c.htm

API for main.py:
  from short_library import save_day, get_short_history, all_stored_dates
"""

import os, json, logging
from datetime import date, datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

START_DATE = date(2018, 3, 1)


# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"short_{year}.json"

def all_years() -> list:
    return list(range(START_DATE.year, date.today().year + 1))

def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year}, "by_date": {}}

def save_year(year: int, lib: dict):
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
    }
    with open(lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(lib_path(year)) / 1e6
    log.info("Saved short_%d.json: %d days, %.2f MB",
             year, len(lib["by_date"]), mb)

def all_stored_dates() -> set:
    stored = set()
    for year in all_years():
        if os.path.exists(lib_path(year)):
            with open(lib_path(year), encoding="utf-8") as f:
                stored.update(json.load(f).get("by_date", {}).keys())
    return stored


# ── API for main.py ───────────────────────────────────────────────────────────

def save_day(d: datetime, records: dict):
    """
    Save one day's short selling data into the library.
    records: {code: {"sv": int, "st": float, "name": str}}
    """
    if not records:
        return
    ds   = d.strftime("%Y-%m-%d")
    year = d.year
    lib  = load_year(year)
    lib["by_date"][ds] = records
    save_year(year, lib)


def get_short_history(code: str, n: int, before: str) -> list:
    """
    Return last n days of short data for a stock before date `before` (YYYY-MM-DD).
    Returns list of {"date": ds, "sv": int, "st": float} newest-first.
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
            entry = by_date[ds].get(code5)
            if entry:
                result.append({"date": ds, **entry})
            if len(result) >= n:
                return result
    return result


def get_short_ratio_history(code: str, n: int, before: str, tv_store: dict) -> list:
    """
    Return last n short ratios for a stock before `before`.
    Ratio = short volume (shares) / traded volume (shares) * 100.
    Requires daily_turnover_history store for volume lookup.
    Returns list of floats, newest-first. Skips days with no volume data.
    """
    code5  = code.zfill(5)
    hist   = get_short_history(code, n * 3, before)  # fetch extra in case of gaps
    result = []
    for entry in hist:
        ds_key = entry["date"].replace("-", "")       # YYYYMMDD for tv_store
        tv_rec = tv_store.get(ds_key, {}).get(code5, 0)
        vol    = tv_rec.get("vol", 0) if isinstance(tv_rec, dict) else 0
        if vol > 0:
            result.append(round(entry["sv"] / vol * 100, 2))
        if len(result) >= n:
            break
    return result

def get_short_name(code: str, before: str = None) -> str | None:
    """
    Return the most recent Chinese name for a stock from the short sell records.
    Uses the most recent stored day (optionally before `before` date).
    """
    code5 = code.zfill(5)
    for year in sorted(all_years(), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p): continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if before and ds >= before: continue
            entry = by_date[ds].get(code5)
            if entry and isinstance(entry, dict):
                name = entry.get("name")
                if name:
                    return name
    return None
