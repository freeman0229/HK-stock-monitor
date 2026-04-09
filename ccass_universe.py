"""
ccass_universe.py — Authoritative HK Stock Universe
====================================================
Single source of truth for which stocks to track across ALL scripts
(build_turnover.py, sfc_library.py, ccass_sdw_library.py, main.py).

Data sources:
  Chinese names : https://www3.hkexnews.hk/sdw/search/stocklist_c.aspx
  English names : https://www3.hkexnews.hk/sdw/search/stocklist.aspx

Inclusion rules (by code range):
  00001–03999   HK Main Board equities (primary range)
  06000–06999   HK Main Board equities (newer codes)
  07489, 07618  Specific ETFs kept by exception
  09600–09699   HK new listings (WVR / W-share / new economy)
  09851–09999   HK new listings (overflow range)

Fields tracked per stock (used across downstream scripts):
  volume, turnover, 總數 (CCASS Grand Total), 佔已發行股份百分比,
  累積沽空股數, 累積沽空金額, close price, 沽空股數, 沽空金額

Usage:
  from ccass_universe import get_universe, get_universe_codes, is_included

  universe = get_universe(date_str="20260401")   # {code5: {zh, en}}
  codes    = get_universe_codes("20260401")       # sorted list of 5-digit codes
  is_included("09660")                            # True
  is_included("09001")                            # False
"""

import json
import logging
import os
import time
from datetime import date
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_BASE_URL = {
    "zh": "https://www3.hkexnews.hk/sdw/search/stocklist_c.aspx",
    "en": "https://www3.hkexnews.hk/sdw/search/stocklist.aspx",
}

_CACHE_DIR      = "ccass_cache"
_CACHE_TTL_DAYS = 1
_TIMEOUT        = 30

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkexnews.hk/",
    "Accept":     "application/json, */*",
}

os.makedirs(_CACHE_DIR, exist_ok=True)

# ── Inclusion rules ───────────────────────────────────────────────────────────

# Two ETFs kept by exception outside the main inclusion ranges.
_KEEP_SPECIFIC = frozenset({"07489", "07618"})

_INCLUDE_RANGES = (
    (    1,  3999),   # HK Main Board (primary)
    ( 6000,  6999),   # HK Main Board (newer codes)
    ( 9600,  9699),   # WVR / W-share new listings
    ( 9851,  9999),   # HK new listings (overflow)
)

# ── Core helpers ──────────────────────────────────────────────────────────────

def normalize_code(code) -> str:
    """Return canonical 5-digit zero-padded string for any code format.

    Handles int, bare string ('700'), zero-padded ('00700'), etc.

    Raises:
        ValueError: if the input normalises to "00000" (not a valid HKEX code).
    """
    result = str(code).strip().lstrip("0").zfill(5)
    if result == "00000":
        raise ValueError(f"normalize_code: invalid code {code!r} normalises to 00000")
    return result


def is_included(code) -> bool:
    """Return True if the CCASS code belongs in the tracked universe.

    Accepts any format accepted by normalize_code (int, bare or padded string).
    """
    code5 = normalize_code(code)
    if code5 in _KEEP_SPECIFIC:
        return True
    n = int(code5)
    return any(lo <= n <= hi for lo, hi in _INCLUDE_RANGES)


# ── Fetch / cache helpers ─────────────────────────────────────────────────────

def _cache_path(date_str: str, lang: str) -> str:
    return os.path.join(_CACHE_DIR, f"ccass_universe_{date_str}_{lang}.json")


def _cache_is_fresh(path: str) -> bool:
    """True if the cache file exists and is younger than _CACHE_TTL_DAYS."""
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < _CACHE_TTL_DAYS * 86_400


def _fetch_list(date_str: str, lang: str) -> list[dict]:
    """Fetch the raw CCASS stock list JSON for *date_str* in *lang*.

    Returns a list of {"c": code, "n": name} dicts.
    Results are cached to disk; stale cache is used as a fallback on
    network failure.

    Note on cache TTL: _CACHE_TTL_DAYS uses wall-clock seconds so a cache
    written at 23:59 is correctly treated as fresh the next morning until
    86 400 seconds have elapsed.  HKEX does not publish data on weekends or
    public holidays; callers that run on non-trading days should pass the
    most recent trading date explicitly.
    """
    cache = _cache_path(date_str, lang)

    if _cache_is_fresh(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    params = {"sortby": "stockcode", "shareholdingdate": date_str}
    try:
        r = requests.get(
            _BASE_URL[lang], params=params, headers=_HEADERS, timeout=_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        log.info("CCASS universe fetched (%s %s): %d stocks", date_str, lang, len(data))
        return data
    except Exception as exc:
        log.error("CCASS fetch failed (%s %s): %s", date_str, lang, exc)
        if os.path.exists(cache):
            log.warning("Falling back to stale cache for %s %s", date_str, lang)
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        log.error("No cache available for %s %s — returning empty list", date_str, lang)
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def get_universe(date_str: Optional[str] = None) -> dict[str, dict]:
    """Return the filtered stock universe for the given date.

    Args:
        date_str: Shareholding date as "YYYYMMDD". Defaults to today.

    Returns:
        Mapping of 5-digit code → {"zh": chinese_name, "en": english_name}.

        Example::

            {
              "00700": {"zh": "騰訊控股", "en": "TENCENT HOLDINGS LIMITED"},
              "09660": {"zh": "地平線機器人-W", "en": "HORIZON ROBOTICS"},
              ...
            }
    """
    date_str = date_str or date.today().strftime("%Y%m%d")

    zh_list = _fetch_list(date_str, "zh")
    if not zh_list:
        log.warning("get_universe: empty zh list for %s", date_str)
        return {}

    en_map: dict[str, str] = {
        normalize_code(row["c"]): row["n"]
        for row in _fetch_list(date_str, "en")
    }

    universe: dict[str, dict] = {}
    for row in zh_list:
        code5 = normalize_code(row["c"])
        if is_included(code5):
            universe[code5] = {"zh": row["n"], "en": en_map.get(code5, "")}

    log.info("get_universe(%s): %d stocks after filtering", date_str, len(universe))
    return universe


def get_universe_codes(date_str: Optional[str] = None) -> list[str]:
    """Return a sorted list of 5-digit codes in the universe."""
    return sorted(get_universe(date_str))


def universe_code_set(date_str: Optional[str] = None) -> frozenset[str]:
    """Return a frozenset of 5-digit codes for fast membership tests."""
    return frozenset(get_universe(date_str))


# Backward-compatible alias
get_all_stock_codes = get_universe_codes


# ── CLI ───────────────────────────────────────────────────────────────────────

def _range_label(n: int) -> str:
    """Map an included stock code integer to a human-readable range label."""
    if   1    <= n <= 3999: return "00001–03999  HK Main Board (primary)"
    if   6000 <= n <= 6999: return "06000–06999  HK Main Board (newer)"
    if   n in (7489, 7618): return "07489/07618  ETF exceptions"
    if   9600 <= n <= 9699: return "09600–09699  WVR / W-share new listings"
    if   9851 <= n <= 9999: return "09851–09999  HK new listings (overflow)"
    return "other"


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="CCASS stock universe utility")
    ap.add_argument("--date",    metavar="YYYYMMDD", default=date.today().strftime("%Y%m%d"),
                    help="Shareholding date (default: today)")
    ap.add_argument("--check",   metavar="CODE",
                    help="Check whether a specific code is included")
    ap.add_argument("--export",  metavar="FILE",
                    help="Export universe to JSON file")
    ap.add_argument("--summary", action="store_true",
                    help="Print breakdown by code range")
    args = ap.parse_args()

    if args.check:
        code5 = normalize_code(args.check)
        status = "INCLUDED ✅" if is_included(code5) else "EXCLUDED ❌"
        print(f"{code5}: {status}")

    else:
        universe = get_universe(args.date)

        if args.summary or not args.export:
            by_range: dict[str, int] = {}
            for code in universe:
                label = _range_label(int(code))
                by_range[label] = by_range.get(label, 0) + 1

            print(f"\nCCASS Universe  {args.date}  →  {len(universe):,} stocks")
            print("─" * 60)
            for label, cnt in sorted(by_range.items()):
                print(f"  {label:<50} {cnt:>5}")
            print("─" * 60)

        if args.export:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(universe, f, ensure_ascii=False, indent=2)
            print(f"\nExported to {args.export}")
        elif not args.summary:
            print("\nSample:")
            for code, names in list(universe.items())[:5]:
                print(f"  {code}  {names['zh']:<20}  {names['en']}")
