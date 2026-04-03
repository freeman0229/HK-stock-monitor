"""
ccass_universe.py — Authoritative HK + Stock Connect Stock Universe
====================================================================
Single source of truth for which stocks to track across ALL scripts
(build_turnover.py, sfc_library.py, ccass_sdw_library.py, main.py).

Data source:
  Chinese names : https://www3.hkexnews.hk/sdw/search/stocklist_c.aspx
  English names : https://www3.hkexnews.hk/sdw/search/stocklist.aspx

Inclusion rules (CCASS stock list, by code range):
  INCLUDED
    00001–03999   HK Main Board equities (primary range)
    06000–06999   HK Main Board equities (newer codes)
    07489, 07618  Specific ETFs kept by exception
    09600–09699   HK new listings (WVR / W-share / new economy)
    09851–09999   HK new listings (overflow range)
    30000–31999   China ChiNext (创业板) + China ETFs via Stock Connect

  EXCLUDED
    04000–04999   Debt Securities (bonds, incl. 04332-04338 AMGEN-T etc.)
    07000–07999   ETF/structured products (except 07489, 07618)
    08000–08999   Equity Securities (GEM)
    09000–09599   ETFs, crypto products (Bitcoin ETF etc.), structured
    09700–09850   Leveraged / Inverse products
    70000–79999   Shenzhen A-shares (SSE Shenzhen main board)
    80000–89999   RMB dual-counter stocks (xxx-R suffix)
    90000–95999   Shanghai A-shares
    Everything else (DW, CBBC, EW, Depositary Receipts) — not in CCASS JSON

Fields tracked per stock (used across all downstream scripts):
  volume, turnover, 總數 (CCASS Grand Total), 佔已發行股份百分比,
  累積沽空股數, 累積沽空金額, close price, 沽空股數, 沽空金額

Usage:
  from ccass_universe import get_universe, get_universe_codes

  # Full dict: {code5: {en, zh}}
  universe = get_universe(date_str="20260401")

  # Just the sorted list of 5-digit codes
  codes = get_universe_codes(date_str="20260401")

  # Check if a code is in universe
  from ccass_universe import is_included
  is_included("09660")   # True
  is_included("09001")   # False
"""

import json
import logging
import os
import time
from datetime import date, datetime
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL_ZH = "https://www3.hkexnews.hk/sdw/search/stocklist_c.aspx"
BASE_URL_EN = "https://www3.hkexnews.hk/sdw/search/stocklist.aspx"

CACHE_DIR      = "ccass_cache"
CACHE_TTL_DAYS = 1          # re-fetch if cache older than this
TIMEOUT        = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkexnews.hk/",
    "Accept":     "application/json, */*",
}

os.makedirs(CACHE_DIR, exist_ok=True)

# ── Specific exceptions ───────────────────────────────────────────────────────

_KEEP_SPECIFIC    = frozenset({"07489", "07618"})
_EXCLUDE_SPECIFIC = frozenset({"04332", "04333", "04335", "04336", "04337", "04338"})

# ── Core filter ───────────────────────────────────────────────────────────────

def is_included(code: str) -> bool:
    """
    Return True if this 5-digit CCASS code belongs in the tracked universe.

    Applies all inclusion/exclusion rules as documented in the module header.
    Works with zero-padded 5-digit strings (e.g. "09660") or bare ints.
    """
    code5 = str(code).zfill(5)

    # Hard-coded exceptions first
    if code5 in _EXCLUDE_SPECIFIC:
        return False
    if code5 in _KEEP_SPECIFIC:
        return True

    n = int(code5)

    # ── HK Main Board ────────────────────────────────────────────────────────
    if 1     <= n <= 3999:   return True   # 00001–03999
    if 6000  <= n <= 6999:   return True   # 06000–06999

    # ── Excluded HK ranges ───────────────────────────────────────────────────
    if 4000  <= n <= 4999:   return False  # Debt Securities
    if 7000  <= n <= 7999:   return False  # ETF/structured (except kept above)
    if 8000  <= n <= 8999:   return False  # GEM

    # ── 09xxx: partial include ───────────────────────────────────────────────
    if 9000  <= n <= 9599:   return False  # ETFs, crypto products, structured
    if 9600  <= n <= 9699:   return True   # HK new listings (WVR / W-share)
    if 9700  <= n <= 9850:   return False  # Leveraged / Inverse products
    if 9851  <= n <= 9999:   return True   # HK newer listings

    # ── China A-shares via Stock Connect ─────────────────────────────────────
    if 30000 <= n <= 31999:  return True   # ChiNext + China ETFs  ← KEEP
    if 70000 <= n <= 79999:  return False  # Shenzhen main board   ← EXCLUDE
    if 80000 <= n <= 89999:  return False  # RMB dual-counter (-R) ← EXCLUDE
    if 90000 <= n <= 95999:  return False  # Shanghai A-shares     ← EXCLUDE

    return False


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _cache_path(date_str: str, lang: str) -> str:
    return os.path.join(CACHE_DIR, f"ccass_universe_{date_str}_{lang}.json")


def _fetch_list(date_str: str, lang: str = "zh") -> list[dict]:
    """
    Fetch the raw CCASS stock list JSON for the given date.
    Returns list of {"c": code, "n": name}.
    lang: "zh" (Chinese) or "en" (English)
    Caches to disk for CACHE_TTL_DAYS.
    """
    cache = _cache_path(date_str, lang)
    if os.path.exists(cache):
        age = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache))).days
        if age < CACHE_TTL_DAYS:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)

    url    = BASE_URL_ZH if lang == "zh" else BASE_URL_EN
    params = {"sortby": "stockcode", "shareholdingdate": date_str}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        log.info("CCASS universe fetched (%s %s): %d stocks", date_str, lang, len(data))
        return data
    except Exception as e:
        log.error("CCASS fetch failed (%s %s): %s", date_str, lang, e)
        # Try to return stale cache
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def get_universe(date_str: Optional[str] = None) -> dict[str, dict]:
    """
    Return the filtered stock universe for the given date.

    date_str: "YYYYMMDD" — defaults to today.

    Returns:
        { "00700": {"zh": "騰訊控股", "en": "TENCENT HOLDINGS LIMITED"},
          "09660": {"zh": "地平線機器人-W", "en": "HORIZON ROBOTICS"},
          ... }

    Chinese names come from the primary CCASS endpoint (most up-to-date).
    English names come from the secondary endpoint.
    Only stocks passing is_included() are returned.
    """
    date_str = date_str or date.today().strftime("%Y%m%d")

    # Fetch both language lists
    zh_list = _fetch_list(date_str, "zh")
    if not zh_list:
        log.warning("get_universe: empty zh list for %s", date_str)
        return {}

    en_list = _fetch_list(date_str, "en")
    en_map  = {row["c"].zfill(5): row["n"] for row in en_list}

    universe = {}
    for row in zh_list:
        code5 = row["c"].zfill(5)
        if not is_included(code5):
            continue
        universe[code5] = {
            "zh": row["n"],
            "en": en_map.get(code5, ""),
        }

    log.info("get_universe(%s): %d stocks after filtering", date_str, len(universe))
    return universe


def get_universe_codes(date_str: Optional[str] = None) -> list[str]:
    """Return sorted list of 5-digit codes in the universe."""
    return sorted(get_universe(date_str).keys())


def universe_code_set(date_str: Optional[str] = None) -> frozenset[str]:
    """Return frozenset of 5-digit codes — fast membership tests."""
    return frozenset(get_universe_codes(date_str))


# ── Backward-compatible helpers ───────────────────────────────────────────────

def get_all_stock_codes(date_str: Optional[str] = None) -> list[str]:
    """Alias for get_universe_codes — drop-in replacement for legacy callers."""
    return get_universe_codes(date_str)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="CCASS stock universe utility")
    ap.add_argument("--date",   metavar="YYYYMMDD", default=date.today().strftime("%Y%m%d"),
                    help="Shareholding date (default: today)")
    ap.add_argument("--check",  metavar="CODE",
                    help="Check whether a specific code is included")
    ap.add_argument("--export", metavar="FILE",
                    help="Export universe to JSON file")
    ap.add_argument("--summary", action="store_true",
                    help="Print breakdown by code range")
    args = ap.parse_args()

    if args.check:
        code5 = args.check.zfill(5)
        result = is_included(code5)
        print(f"{code5}: {'INCLUDED ✅' if result else 'EXCLUDED ❌'}")

    elif args.summary or args.export:
        universe = get_universe(args.date)
        by_range = {}
        for code in universe:
            n = int(code)
            if   n <= 3999:               rng = "00xxx-03xxx (HK Main Board)"
            elif n <= 6999:               rng = "06xxx       (HK Main Board)"
            elif n <= 9999:               rng = "07489/07618/09600-09699/09851-09999"
            elif n <= 31999:              rng = "30xxx-31xxx (ChiNext + CN ETF)"
            else:                         rng = "other"
            by_range[rng] = by_range.get(rng, 0) + 1

        print(f"\nCCASS Universe  {args.date}  →  {len(universe):,} stocks")
        print("─" * 60)
        for rng, cnt in sorted(by_range.items(), key=lambda x: x[0]):
            print(f"  {rng:<45} {cnt:>5}")
        print("─" * 60)

        if args.export:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(universe, f, ensure_ascii=False, indent=2)
            print(f"\nExported to {args.export}")

    else:
        # Default: print summary
        universe = get_universe(args.date)
        print(f"CCASS Universe {args.date}: {len(universe):,} stocks")
        print("Sample:")
        for code, names in list(universe.items())[:5]:
            print(f"  {code}  {names['zh']:<20}  {names['en']}")
