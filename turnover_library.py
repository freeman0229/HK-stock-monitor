"""
turnover_library.py — HKEX Daily Turnover & Volume Library
===========================================================
Stores daily turnover (HKD) and volume (shares) for all stocks
from the HKEX daily quotation file.

Library files: turnover_{YYYY}.json — one per year

Structure:
{
  "meta": {"year": 2026, "last_updated": "...", "total_days": N},
  "by_date": {
    "2026-03-19": {
      "00700": {"tv": 26800000000, "vol": 62450000, "close": 481.60},
      ...
    }
  }
}

API for main.py:
  from turnover_library import save_day, get_tv, get_tv_history, all_stored_dates
"""

import os, json, logging
from datetime import date, datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

START_DATE = date(2018, 3, 1)


# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"turnover_{year}.json"

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
    log.info("Saved turnover_%d.json: %d days, %.2f MB",
             year, len(lib["by_date"]), mb)

def all_stored_dates() -> set:
    stored = set()
    for year in all_years():
        p = lib_path(year)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                stored.update(json.load(f).get("by_date", {}).keys())
    return stored


# ── API for main.py ───────────────────────────────────────────────────────────

def save_day(d: datetime, records: dict):
    """
    Save one day's turnover/volume into the library.
    records: {code: {"tv": int, "vol": int, "close": float}}
    """
    if not records:
        return
    ds   = d.strftime("%Y-%m-%d")
    year = d.year
    lib  = load_year(year)
    lib["by_date"][ds] = records
    save_year(year, lib)


def get_tv(code: str, ds_yyyymmdd: str) -> float:
    """
    Return turnover for a stock on a given date (YYYYMMDD format).
    Returns 0.0 if not found.
    """
    year = int(ds_yyyymmdd[:4])
    ds   = f"{ds_yyyymmdd[:4]}-{ds_yyyymmdd[4:6]}-{ds_yyyymmdd[6:8]}"
    p    = lib_path(year)
    if not os.path.exists(p):
        return 0.0
    with open(p, encoding="utf-8") as f:
        rec = json.load(f).get("by_date", {}).get(ds, {}).get(code, {})
    if isinstance(rec, dict):
        return float(rec.get("tv", 0))
    return float(rec)


def get_vwap(code: str, ds_yyyymmdd: str) -> float:
    """
    Return VWAP (成交均價) for a stock on a given date (YYYYMMDD format).
    VWAP = tv (HKD) / vol (shares).
    Uses stored "vwap" field if present (new records from build_turnover.py),
    otherwise computes from tv/vol for backward compatibility.
    Returns 0.0 if data missing or vol=0.
    """
    year = int(ds_yyyymmdd[:4])
    ds   = f"{ds_yyyymmdd[:4]}-{ds_yyyymmdd[4:6]}-{ds_yyyymmdd[6:8]}"
    p    = lib_path(year)
    if not os.path.exists(p):
        return 0.0
    with open(p, encoding="utf-8") as f:
        rec = json.load(f).get("by_date", {}).get(ds, {}).get(code, {})
    if not isinstance(rec, dict):
        return 0.0
    if rec.get("vwap", 0):
        return float(rec["vwap"])
    tv  = float(rec.get("tv",  0))
    vol = float(rec.get("vol", 0))
    return round(tv / vol, 4) if vol > 0 else 0.0


def get_vol_history(code: str, n: int, before: str) -> list:
    """
    Return last n share volume values for a stock before date `before`
    (YYYY-MM-DD), newest-first. Skips days with no data.
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
            rec = by_date[ds].get(code5, {})
            vol = rec.get("vol", 0) if isinstance(rec, dict) else 0
            if vol > 0:
                result.append(int(vol))
            if len(result) >= n:
                return result
    return result


def get_tv_history(code: str, n: int, before: str) -> list:
    """
    Return last n turnover values (HKD) for a stock before date `before`
    (YYYY-MM-DD), newest-first. Skips days with no data.
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
            rec = by_date[ds].get(code5, {})
            tv  = rec.get("tv", 0) if isinstance(rec, dict) else rec
            if tv > 0:
                result.append(float(tv))
            if len(result) >= n:
                return result
    return result



def get_close_history(code: str, n: int, before: str) -> list:
    """
    Return last n closing prices for a stock before date `before`
    (YYYY-MM-DD), newest-first. Skips days with no data.
    Alias: get_price_history (preferred name for chart construction).
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
            rec   = by_date[ds].get(code5, {})
            close = rec.get("close", 0.0) if isinstance(rec, dict) else 0.0
            if close > 0:
                result.append(float(close))
            if len(result) >= n:
                return result
    return result



# Preferred alias for chart construction — same as get_close_history
get_price_history = get_close_history


def get_close(code: str, ds_yyyymmdd: str) -> float:
    """
    Return closing price for a stock on a given date (YYYYMMDD format).
    Returns 0.0 if not found.
    """
    year = int(ds_yyyymmdd[:4])
    ds   = f"{ds_yyyymmdd[:4]}-{ds_yyyymmdd[4:6]}-{ds_yyyymmdd[6:8]}"
    p    = lib_path(year)
    if not os.path.exists(p):
        return 0.0
    with open(p, encoding="utf-8") as f:
        rec = json.load(f).get("by_date", {}).get(ds, {}).get(code.zfill(5), {})
    if isinstance(rec, dict):
        return float(rec.get("close", 0.0))
    return 0.0


def load_recent(n_days: int, before: str) -> dict:
    """
    Return a dict of {YYYYMMDD: {code: {tv, vol}}} for the last n_days
    before `before` (YYYY-MM-DD). Used as a drop-in for the old
    daily_turnover_history store in _turnover_avg and _value_at.
    """
    result = {}
    count  = 0
    for year in sorted(all_years(), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before:
                continue
            yyyymmdd = ds.replace("-", "")
            result[yyyymmdd] = by_date[ds]
            count += 1
            if count >= n_days:
                return result
    return result

