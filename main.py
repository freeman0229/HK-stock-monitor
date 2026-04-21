import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

import holidays
import pandas as pd
import requests
from bs4 import BeautifulSoup
from stock_ref import get_zh_name, get_industry, get_type, STOCKS
from ccass_universe import get_universe, is_included as _universe_included, normalize_code
from ccass_library import get_pct_history, get_sh_history, load_year as _cc_load_year
from short_library import get_short_ratio_history, load_year as sl_load_year
from turnover_library import load_year as tv_load_year, load_recent as tv_load_recent
from sc_top10_library import get_top10, get_top10_history, get_sb_summary
try:
    from sfc_library import get_short_position as sfc_get_position, \
    all_report_fridays as sfc_fridays, get_position_history as sfc_get_history
    _SFC_AVAILABLE = True
except ImportError:
    _SFC_AVAILABLE = False
try:
    from ccass_sdw_library import get_latest_total_sh as sdw_get_total_sh, \
                              get_total_sh_bulk   as sdw_get_total_sh_bulk, \
                              get_holders         as sdw_get_holders, \
                              get_holders_history as sdw_get_holders_history
    _SDW_AVAILABLE = True
except ImportError:
    _SDW_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkex.com.hk/",
}

# ── Trading day helpers ───────────────────────────────────────────────────────
HK_HOLIDAYS = holidays.HongKong()

def is_trading_day(d: datetime = None) -> bool:
    d = d or datetime.now()
    return d.weekday() < 5 and d.date() not in HK_HOLIDAYS

def last_trading_day(d: datetime = None) -> datetime:
    d = d or datetime.now()
    for _ in range(14):   # safety limit — no holiday run longer than 2 weeks
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise ValueError(f"last_trading_day: no trading day found within 14 days of {d}")

# ── Mainland China stock exchange holidays (southbound settlement) ────────────
_CN_HOLIDAY_DATES = {
    # 2023
    "2023-01-02","2023-01-23","2023-01-24","2023-01-25","2023-01-26","2023-01-27",
    "2023-04-05","2023-04-29","2023-04-30","2023-05-01","2023-05-03",
    "2023-06-22","2023-06-23","2023-09-29","2023-10-02","2023-10-03",
    "2023-10-04","2023-10-05","2023-10-06",
    # 2024
    "2024-01-01","2024-02-12","2024-02-13","2024-02-14","2024-02-15","2024-02-16",
    "2024-04-04","2024-04-05","2024-05-01","2024-05-02","2024-05-03",
    "2024-06-10","2024-09-16","2024-09-17","2024-10-01","2024-10-02","2024-10-03",
    "2024-10-04","2024-10-07",
    # 2025
    "2025-01-01","2025-01-27","2025-01-28","2025-01-29","2025-01-30","2025-01-31",
    "2025-04-04","2025-05-01","2025-05-02","2025-05-05",
    "2025-06-02","2025-10-01","2025-10-02","2025-10-03","2025-10-06","2025-10-07","2025-10-08",
    # 2026
    "2026-01-01","2026-01-28","2026-01-29","2026-01-30","2026-02-02","2026-02-03","2026-02-04",
    "2026-04-06","2026-05-01","2026-05-04","2026-05-05",
    "2026-06-19","2026-10-01","2026-10-02","2026-10-05","2026-10-06","2026-10-07","2026-10-08",
}
try:
    _CN_HOLIDAYS_LIB = holidays.China()
except Exception:
    _CN_HOLIDAYS_LIB = set()

def _is_cn_holiday(d: datetime) -> bool:
    ds = d.strftime("%Y-%m-%d")
    return ds in _CN_HOLIDAY_DATES or d.date() in _CN_HOLIDAYS_LIB

def business_days_back(d: datetime, n: int) -> datetime:
    """
    Return the date n joint HK+CN settlement days before d.
    Southbound CCASS settles T+2 on days both exchanges are open.
    """
    count = 0
    while count < n:
        d -= timedelta(days=1)
        hk_open = d.weekday() < 5 and d.date() not in HK_HOLIDAYS
        cn_open  = d.weekday() < 5 and not _is_cn_holiday(d)
        if hk_open and cn_open:
            count += 1
    return d

def ccass_trade_date(settlement_date: datetime) -> datetime:
    """Given a CCASS settlement date, return the actual trade date (T-2)."""
    return business_days_back(settlement_date, 2)

# ── Shared helpers ────────────────────────────────────────────────────────────
def to_num(s) -> float:
    try:    return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError, AttributeError): return 0.0

def load_store(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_store(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}, timeout=15
        )
        if r.status_code == 200:
            log.info("Telegram sent")
        else:
            log.warning("Telegram error: %s", r.text)
    except Exception as e:
        log.error("Telegram failed: %s", e)

# ── Name map ──────────────────────────────────────────────────────────────────
NAME_MAP_FILE = "name_map.json"

def _is_valid_chinese(s: str) -> bool:
    if not s:
        return False
    cjk     = sum(1 for c in s if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    garbage = s.count('\ufffd') + s.count('?')
    return cjk >= 1 and garbage < 3

def _update_name_map(entries: dict):
    store = load_store(NAME_MAP_FILE)
    for code, data in entries.items():
        if code not in store or not store[code].get("verified"):
            store[code] = data
    save_store(NAME_MAP_FILE, store)

def _seed_name_map_from_ref():
    """Seed name_map.json with verified names from stock_ref on first run."""
    store   = load_store(NAME_MAP_FILE)
    changed = False
    for code, info in STOCKS.items():
        if store.get(code, {}).get("verified"):
            continue
        store[code] = {"en": info["en"], "zh": info["zh"], "verified": True}
        changed = True
    if changed:
        save_store(NAME_MAP_FILE, store)
        log.info("Seeded name_map with %d verified entries from stock_ref", len(STOCKS))

# ── Source 2: Short selling — loaded from short_library ─────────────────────────
# Short selling data is parsed from the adj_short section of d{YYMMDD}c.htm by build_turnover.py.
# Source: HKEX daily quotation  https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm#adj_short
#   sv = 沽空股數 (SH)  — total short sell volume in shares
#   st = 沽空金額 ($)   — total short sell value in HKD
# Each file contains the PREVIOUS trading day's short data, so the library entry
# for a given date was stored when the NEXT day's file was fetched.
EMPTY_SHORT = pd.DataFrame(columns=["stock_code", "name", "short_volume", "short_turnover"])

def get_short_sell_today(trading_day: datetime) -> pd.DataFrame:
    """
    Load short selling data from the library for trading_day.
    Falls back to the most recent prior day if today's data is not yet
    available (current day short is stored after the next day's file is fetched).
    """
    # Work with date objects; convert to datetime only when calling last_trading_day
    target = trading_day.date() if hasattr(trading_day, 'date') else trading_day
    for _ in range(10):
        ds  = target.isoformat()
        day = sl_load_year(target.year).get("by_date", {}).get(ds)
        if day:
            rows = [
                {"stock_code":     code,
                 "name":           v.get("name", ""),
                 "short_volume":   v.get("sv", 0),    # 沽空股數 (SH)
                 "short_turnover": v.get("st", 0.0)}  # 沽空金額 ($)
                for code, v in day.items()
                if isinstance(v, dict) and v.get("sv", 0) > 0
            ]
            if rows:
                if ds != trading_day.strftime("%Y-%m-%d"):
                    log.info("Short sell: using %s (today not yet available)", ds)
                else:
                    log.info("Short sell: %d stocks from %s", len(rows), ds)
                return pd.DataFrame(rows), ds
        # Step back one trading day using date arithmetic (no datetime needed)
        prev = target - timedelta(days=1)
        while prev.weekday() >= 5 or datetime(prev.year, prev.month, prev.day) in HK_HOLIDAYS:
            prev -= timedelta(days=1)
        target = prev
    log.warning("Short sell: no data found near %s",
                trading_day.strftime("%Y-%m-%d"))
    return EMPTY_SHORT, None

# ── Source 3: CCASS southbound ────────────────────────────────────────────────

def _hv(soup, name: str) -> str:
    """Extract hidden ASP.NET form field value from a BeautifulSoup object."""
    tag = soup.find("input", {"name": name})
    return tag["value"] if tag else ""


def _clean_cell(s: str) -> str:
    """Strip leading 'Label: ' prefix from a table cell value."""
    return s.split(":")[-1].strip() if ":" in s else s.strip()


CCASS_URL   = "https://www3.hkexnews.hk/sdw/search/mutualmarket_c.aspx"
EMPTY_CCASS = pd.DataFrame(columns=["stock_code", "name", "shareholding", "pct_listed"])

def get_ccass_southbound(date: datetime = None) -> pd.DataFrame:
    date     = date or datetime.now()
    date_str = date.strftime("%Y/%m/%d")
    try:
        s    = requests.Session()
        s.headers.update(HEADERS)
        r1   = s.get(f"{CCASS_URL}?t=hk", timeout=30)
        r1.raise_for_status()
        soup = BeautifulSoup(r1.text, "html.parser")

        r2 = s.post(f"{CCASS_URL}?t=hk", data={
            "__EVENTTARGET":        "btnSearch",
            "__EVENTARGUMENT":      "",
            "__VIEWSTATE":          _hv(soup, "__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": _hv(soup, "__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION":    _hv(soup, "__EVENTVALIDATION"),
            "txtShareholdingDate":  date_str,
            "t":                    "hk",
        }, timeout=30)
        r2.raise_for_status()

        tables = BeautifulSoup(r2.text, "html.parser").find_all("table")
        table  = max(tables, key=lambda t: len(t.find_all("tr"))) if tables else None
        if not table:
            log.warning("CCASS: no table for %s", date_str)
            return EMPTY_CCASS

        rows = []
        for tr in table.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 4:
                continue
            cr = _clean_cell(tds[0]).replace(",", "")
            sr = _clean_cell(tds[2]).replace(",", "")
            if not cr.isdigit() or not sr.isdigit():
                continue
            pr = _clean_cell(tds[3]).replace("%", "").strip()
            rows.append({"stock_code":   normalize_code(cr),
                         "name":         _clean_cell(tds[1]),
                         "shareholding": int(sr),
                         "pct_listed":   float(pr) if pr else 0.0})

        df = pd.DataFrame(rows)
        log.info("CCASS southbound: %d records for %s", len(df), date_str)
        return df
    except Exception as e:
        log.error("get_ccass_southbound failed (%s): %s", date_str, e)
        return EMPTY_CCASS

def get_ccass_delta_and_avg(stock_codes: list, today_map: dict,
                            today_ds: str, days: int = 25,
                            today_pct_map: dict = None) -> pd.DataFrame:
    """
    Compute CCASS metrics using pct_listed as the primary signal.

    Fields returned:
      pct_listed        — today's % held in CCASS
      pct_delta         — today pct minus yesterday pct (percentage points)
      ccass_consec      — consecutive days pct moved in same direction
      ccass_streak_pct  — cumulative pct change over the current streak
      ccass_delta       — raw share count change
    """
    if today_pct_map is None:
        today_pct_map = {}
    rows = []
    for code in stock_codes:
        today_sh  = today_map.get(code, 0)
        pct_today = today_pct_map.get(code, 0.0)

        pct_hist  = get_pct_history(code, days, today_ds)
        pct_prev  = pct_hist[0] if pct_hist else 0.0
        pct_delta = round(pct_today - pct_prev, 4) if pct_prev > 0 else 0.0

        pct_deltas = [
            round(pct_hist[i] - pct_hist[i + 1], 4)
            for i in range(len(pct_hist) - 1)
            if pct_hist[i] > 0 and pct_hist[i + 1] > 0
        ]

        direction  = 1 if pct_delta > 0 else (-1 if pct_delta < 0 else 0)
        consec     = 0
        streak_pct = pct_delta
        if direction != 0:
            for d in pct_deltas:
                if d == 0:
                    continue
                if d * direction > 0:
                    consec     += direction
                    streak_pct += d
                else:
                    break

        sh_hist = get_sh_history(code, 2, today_ds)
        prev_sh = sh_hist[0] if sh_hist else 0
        delta   = today_sh - prev_sh

        rows.append({
            "stock_code":       code,
            "ccass_delta":      delta,
            "ccass_consec":     consec,
            "ccass_streak_pct": round(streak_pct, 4),
            "pct_listed":       pct_today,
            "pct_delta":        pct_delta,
        })
    return pd.DataFrame(rows)

# ── Turnover history ──────────────────────────────────────────────────────────
RANK_HISTORY_FILE = "rank_history.json"

def save_rank_history(date: datetime, results: list):
    store = load_store(RANK_HISTORY_FILE)
    store[date.strftime("%Y%m%d")] = {
        r["code"]: {
            "rank":             r["rank"],
            "close":            r.get("close", 0.0),
            "vol":              r.get("vol", 0),
            "tv":               r.get("turnover", 0),
            "vwap":             r.get("vwap", 0.0),
            "short_ratio":      r.get("short_ratio", 0.0),
            "sfc_pct":          r.get("sfc_pct", 0.0),
            "concentration":    r.get("concentration", 0.0),
            "lockup_threshold": r.get("lockup_threshold", 60.0),
            "turnover_24d":     r.get("turnover_24d", 0.0),
            "pct_listed":       r.get("pct_listed", 0.0),
        }
        for r in results
    }
    save_store(RANK_HISTORY_FILE, store)

def get_prev_ranks(exclude_date: datetime = None) -> dict:
    """Return rankings from the most recent stored day, excluding today."""
    store = load_store(RANK_HISTORY_FILE)
    if not store:
        return {}
    # Always use exclude_date when provided — datetime.now() is wrong when
    # the job runs just after midnight or on a non-trading day.
    today_key = (exclude_date.strftime("%Y%m%d") if exclude_date
                 else datetime.now().strftime("%Y%m%d"))
    keys = sorted(k for k in store.keys() if k != today_key)
    if not keys:
        return {}
    day = store[keys[-1]]
    # Handle both old format {code: rank} and new format {code: {rank, ...}}
    return {
        code: (v["rank"] if isinstance(v, dict) else v)
        for code, v in day.items()
    }

# ── Stock classification ──────────────────────────────────────────────────────
def classify_stock(code: str, name: str) -> str:
    t = get_type(code)
    if t:
        return t
    n = name.upper()
    ETF_CODES   = {"02800","02828","03033","03032","03188","02846","03140","03037","03011","02823"}
    STABLE_KW   = ("BANK","ENERGY","POWER","GAS","PETRO","SINOPEC","CNOOC","MTR","UTILITY")
    BLUECHIP_KW = ("TENCENT","MEITUAN","ALIBABA","BABA","XIAOMI","HSBC","AIA","PING AN",
                   "HKEX","CK ","HENDERSON","SHK","SWIRE","GALAXY","SANDS","MELCO")
    if code in ETF_CODES:                               return "etf"
    if any(k in n for k in STABLE_KW):                 return "stable"
    if any(k in n for k in BLUECHIP_KW):               return "bluechip"
    return "general"

THRESHOLDS = {
    #              lo    hi  spike  cover_drop
    "etf":      (40.0, 70.0, 15.0, 0.60),
    "stable":   ( 5.0, 10.0, 15.0, 0.60),
    "bluechip": (10.0, 20.0, 10.0, 0.60),
    "general":  (10.0, 25.0, 15.0, 0.60),
}

SFC_THRESHOLDS = {
    #              spike_up  unwind
    "etf":      (    3.0,    -3.0 ),   # ETFs sit at 40–60%; 1pp is noise
    "stable":   (    1.0,    -1.0 ),   # Low base (5–10%); 1pp is meaningful
    "bluechip": (    1.5,    -1.5 ),   # Mid base; slightly more tolerance
    "general":  (    1.0,    -1.0 ),   # Same as stable
}

# ── 鎖倉臨界點 classifier ────────────────────────────────────────────────────
# Threshold at which institutional concentration makes short covering structurally
# difficult. Categorised by 24-day average HKD daily turnover.
#
#   超大型 (tv_avg24 > 10億):  75%  — high liquidity, needs extreme concentration
#   中型   (tv_avg24 2–10億):  60%  — moderate, 60% concentration traps shorts
#   小型   (tv_avg24 < 2億):   90%  — illiquid; near-total lock needed to matter
#
_LOCKUP_LARGE  = 1_000_000_000   # 10億 HKD
_LOCKUP_MID    =   200_000_000   #  2億 HKD

def lockup_threshold(tv_avg24: float) -> float:
    """
    Return the 鎖倉臨界點 (%) for a stock given its 24-day avg HKD turnover.
    超大型 > 10億 → 75%  |  中型 2–10億 → 60%  |  小型 < 2億 → 90%
    """
    if tv_avg24 >= _LOCKUP_LARGE:
        return 75.0
    if tv_avg24 >= _LOCKUP_MID:
        return 60.0
    return 90.0

# ── 換手率 delta thresholds ──────────────────────────────────────────────────
# delta = current_turnover_24d − prev_turnover_24d  [pp]
# Each stock compared to itself (rolling 24-day window, 1-day shift)
_TURNOVER_DELTA_HIGH     = 7.0   # pp — high:     delta >= 7%
_TURNOVER_DELTA_ELEVATED = 4.0   # pp — elevated: delta 4% – 6%
#                                  pp — normal:   delta <= 3%

def classify_insight(stock_type, short_ratio, short_avg,
                     turnover, tv_avg5,
                     pct_delta=0.0,
                     days_to_cover=0.0, vol_ratio=0.0,
                     tv_ratio=0.0, pct_dev=0.0,
                     sb_net=0,
                     sfc_week_delta=0.0,
                     delta_turnover_24d=0.0) -> str | None:
    lo, hi, spike_warn, cover_drop = THRESHOLDS.get(stock_type, THRESHOLDS["general"])
    sfc_up, sfc_dn = SFC_THRESHOLDS.get(stock_type, SFC_THRESHOLDS["general"])
    r_today = turnover / tv_avg5 if tv_avg5 > 0 else 1.0

    if days_to_cover > 5 and vol_ratio > 2:                      return "🔥 挾倉風險"
    if (vol_ratio  >  2.5
            and tv_ratio >  2.0
            and pct_dev  >= 0.5):                                 return "🐉 異常亢奮"
    if (1.8 <= vol_ratio  <= 2.5
            and 1.5 <= tv_ratio <= 2.0
            and 0.2 <= pct_dev  <= 0.5):                          return "🏦 大戶增持"
    if sb_net > 0 and pct_delta > 0:                             return "🏦 北水增持"

    # SFC structural signals: week-on-week jump vs threshold
    if sfc_week_delta >= sfc_up:                                  return f"⚠️ 沽空倉位急增 +{sfc_week_delta:.1f}pp"
    if sfc_week_delta <= sfc_dn:                                  return f"📊 沽空倉位大減 {sfc_week_delta:.1f}pp"

    # 換手率 delta signals: current 24d vs prev 24d rolling window
    if delta_turnover_24d >= _TURNOVER_DELTA_HIGH:                return "📈 換手急升"
    if delta_turnover_24d >= _TURNOVER_DELTA_ELEVATED:            return "🔼 換手上升"

    flow_out   = sb_net < 0 and pct_delta < 0
    high_short = short_ratio > hi + spike_warn and vol_ratio > 2
    if flow_out:                  return "🚨 北水流出"
    if high_short:                return "🚨 不尋常沽空"

    if (short_avg > lo and short_ratio < short_avg * cover_drop
            and r_today > 1.30):                                  return "📉 空頭平倉"
    return None

# ── Main analysis ─────────────────────────────────────────────────────────────
def get_short_avg_ratio(stock_codes: list, days: int, daily_tv: dict,
                        before: str) -> pd.DataFrame:
    """
    Compute mean short ratio over last `days` sessions for each stock.
    Ratio = short_vol / traded_vol * 100, using daily_tv for volume lookup.
    Returns DataFrame with columns: stock_code, short_avg.
    """
    rows = []
    for c in stock_codes:
        v = get_short_ratio_history(c, days, before, daily_tv)
        rows.append({"stock_code": c,
                     "short_avg": round(sum(v) / len(v), 2) if v else 0.0})
    return pd.DataFrame(rows)

def run_analysis(for_date: datetime = None, suppress_telegram: bool = False):
    today       = for_date or datetime.now()
    trading_day = last_trading_day(today)
    log.info("=== analysis — trading day: %s ===", trading_day.strftime("%Y-%m-%d"))
    today_ds = trading_day.strftime("%Y-%m-%d")

    _seed_name_map_from_ref()
    _universe_names = get_universe()  # {code5: {"zh": ..., "en": ...}} — HKEX authoritative names

    # ── 1. Daily quotation — read from turnover library (build_turnover.py owns writes) ──
    _nm      = load_store(NAME_MAP_FILE)
    _lib_day = tv_load_year(trading_day.year).get("by_date", {}).get(today_ds, {})

    if not _lib_day:
        # Fallback: use most recent day already in library
        _fallback = tv_load_recent(1, today_ds)
        if _fallback:
            _fallback_ds = max(_fallback.keys())
            _lib_day     = _fallback[_fallback_ds]
            log.warning("Turnover library: %s not found — using %s as fallback",
                        today_ds, _fallback_ds)
            send_telegram(
                f"⚠️ 港股看板：{today_ds} 成交數據未找到，"
                f"以 {_fallback_ds} 緩存數據繼續分析（排名僅供參考）。"
            )
        else:
            msg = ("⚠️ 港股看板：成交數據庫無數據，分析中止。"
                   "請先執行 build_turnover.py。")
            log.error(msg); send_telegram(msg); return

    # Build quote_map from library data; update name_map with any new names.
    # Source: HKEX daily quotation  https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm
    #   tv   = 成交金額 (TURNOVER $)   — HKD value of all trades
    #   vol  = 成交股數 (SHARES TRADED) — number of shares that changed hands
    #   high = 最高 (HIGH)              — intraday high price
    #   low  = 最低 (LOW)               — intraday low price
    quote_map    = {}
    _name_updates = {}
    for _code, _rec in _lib_day.items():
        if not isinstance(_rec, dict) or _rec.get("tv", 0) <= 0:
            continue
        _nm_entry = _nm.get(_code, {})
        _name_en  = _rec.get("name_en") or _nm_entry.get("en") or _code
        _name_zh  = _rec.get("name_zh") or _nm_entry.get("zh") or _name_en
        quote_map[_code] = {
            "tv":       int(_rec["tv"]),    # 成交金額 (HKD)
            "vol":      int(_rec.get("vol", 0)),  # 成交股數 (shares)
            "close":    float(_rec.get("close", 0.0)),  # 收市價 (CLOSING RATE)
            "name":     _name_en,
            "name_chi": _name_zh,
        }
        if _code not in _nm:
            _name_updates[_code] = {"en": _name_en, "zh": _name_zh}
    if _name_updates:
        _update_name_map(_name_updates)
    log.info("Turnover library: %d stocks for %s", len(quote_map), today_ds)

    # ── 2. Short selling (full market: 800+ stocks) ───────────────────────────
    df_short, short_date = get_short_sell_today(trading_day)
    # save_short_sell is a no-op — data is saved by build_turnover.py
    # Load volume for the short date — may differ from today if short data lags
    _short_vol_map_ref = quote_map  # default: use today's vol
    if short_date and short_date != today_ds:
        # Short data is from a prior day — load that day's volume for a consistent ratio
        _short_lib_day = tv_load_year(int(short_date[:4])).get("by_date", {}).get(short_date, {})
        if _short_lib_day:
            _short_vol_map_ref = {
                code: {"vol": int(rec.get("vol", 0))}
                for code, rec in _short_lib_day.items()
                if isinstance(rec, dict)
            }
            log.info("Short ratio: using %s volume for denominator (matches short date)", short_date)
    short_map     = {}   # code → short_ratio % (沽空股數 / 成交股數 × 100)
    short_vol_map = {}   # code → 沽空股數 (SH)
    short_st_map  = {}   # code → 沽空金額 ($) in HKD
    for row in df_short.itertuples():
        code = row.stock_code
        sv   = int(row.short_volume)    # 沽空股數 (SH)
        st   = float(row.short_turnover)  # 沽空金額 ($)
        short_vol_map[code] = sv
        short_st_map[code]  = st
        traded_vol = _short_vol_map_ref.get(code, {}).get("vol", 0)
        if traded_vol > 0:
            short_map[code] = round(sv / traded_vol * 100, 2)

    # ── 3. CCASS southbound (917 stocks) ─────────────────────────────────────
    t2_date = ccass_trade_date(trading_day)
    log.info("T-2 trade date (CCASS settlement): %s", t2_date.strftime("%Y%m%d"))

    df_ccass = get_ccass_southbound(trading_day)
    if df_ccass.empty:
        prev_td  = last_trading_day(trading_day - timedelta(days=1))
        log.info("CCASS empty for %s, trying %s",
                 trading_day.strftime("%Y-%m-%d"), prev_td.strftime("%Y-%m-%d"))
        df_ccass = get_ccass_southbound(prev_td)

    ccass_sh_map  = {}
    ccass_pct_map = {}
    ccass_name_map = {}
    if not df_ccass.empty:
        ccass_sh_map   = dict(zip(df_ccass["stock_code"], df_ccass["shareholding"]))
        ccass_pct_map  = dict(zip(df_ccass["stock_code"], df_ccass["pct_listed"]))
        ccass_name_map = dict(zip(df_ccass["stock_code"], df_ccass["name"]))

    # ── 4. Full stock universe ────────────────────────────────────────────────
    # Union of: short sell (800+), CCASS (917), quotation (500+)
    # Filtered by ccass_universe.is_included() to exclude debt, GEM,
    # warrants, RMB counters etc. consistently across all data sources.
    # Ranked by: turnover from quotation where available;
    #            short_turnover (st) as proxy for the rest
    stock_universe = {c for c in (set(short_vol_map.keys()) |
                                   set(ccass_sh_map.keys()) |
                                   set(quote_map.keys()))
                      if _universe_included(c)}

    # Sort: quotation stocks (have real tv) first by tv desc,
    #       then remaining by short_turnover desc
    def _sort_key(code):
        tv = quote_map.get(code, {}).get("tv", 0)
        st = short_st_map.get(code, 0)
        return (-tv, -st)

    stock_codes = sorted(stock_universe, key=_sort_key)
    log.info("Stock universe: %d stocks (%d from quotation, %d from short sell, %d from CCASS)",
             len(stock_codes), len(quote_map), len(short_vol_map), len(ccass_sh_map))

    # ── 5. CCASS deltas for the full universe ─────────────────────────────────
    _tv_recent = tv_load_recent(35, today_ds)

    # Pre-load SDW total_sh for all stocks in one pass (7 range files → {code5: total_sh})
    # Source: https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx
    #   total_sh = 總數 (於中央結算系統的持股量，總數) — shares custodied within CCASS
    #   ⚠️  NOT the same as 已發行股份/權證/單位 (最近更新數目) — total issued shares
    #   總數 ≤ 已發行股份 since not all issued shares are held in CCASS
    # Used for: 換手率 (N-day vol / 總數 × 100) and 持倉集中度 (top5_sh / 總數 × 100)
    _sdw_total_sh_map = sdw_get_total_sh_bulk(today_ds) if _SDW_AVAILABLE else {}

    # Bulk-load SDW holders for all stocks to avoid one DB open per stock in the loop.
    # Try today first; fall back to the most recent available SDW date if today
    # has not been fetched yet (avoids the fragile -8 day hardcoded fallback).
    _sdw_holders_map: dict[str, list] = {}
    if _SDW_AVAILABLE:
        from ccass_sdw_library import DB_PATH as _SDW_DB_PATH, get_conn as _sdw_get_conn
        import sqlite3 as _sqlite3
        try:
            with _sdw_get_conn(_SDW_DB_PATH) as _sc:
                # Find most recent SDW date on or before today
                _sdw_best_date = _sc.execute(
                    "SELECT MAX(date) FROM metadata WHERE date <= ?", (today_ds,)
                ).fetchone()[0]
            if _sdw_best_date:
                if _sdw_best_date != today_ds:
                    log.info("SDW holders: today not available — using %s", _sdw_best_date)
                with _sdw_get_conn(_SDW_DB_PATH) as _sc:
                    _sdw_rows = _sc.execute(
                        """SELECT code, pid, name, shares, pct
                           FROM   holdings
                           WHERE  date = ?
                           ORDER  BY code, shares DESC""",
                        (_sdw_best_date,),
                    ).fetchall()
                for _row in _sdw_rows:
                    _c = _row["code"]
                    _sdw_holders_map.setdefault(_c, []).append({
                        "pid": _row["pid"], "name": _row["name"],
                        "sh":  _row["shares"], "pct": _row["pct"],
                    })
                log.info("SDW holders: %d stocks loaded from %s",
                         len(_sdw_holders_map), _sdw_best_date)
        except Exception as _e:
            log.warning("SDW bulk holder load failed: %s — falling back to per-stock", _e)

    # Bulk-load previous SDW snapshot for WoW top10 delta calculation.
    _sdw_prev_holders_map: dict[str, list] = {}
    if _SDW_AVAILABLE:
        try:
            with _sdw_get_conn(_SDW_DB_PATH) as _sc:
                _sdw_prev_date = _sc.execute(
                    """SELECT MAX(date) FROM metadata
                       WHERE date < COALESCE(?, ?)""",
                    (_sdw_best_date, today_ds),
                ).fetchone()[0]
            if _sdw_prev_date:
                with _sdw_get_conn(_SDW_DB_PATH) as _sc:
                    _sdw_prev_rows = _sc.execute(
                        """SELECT code, pid, name, shares, pct
                           FROM   holdings
                           WHERE  date = ?
                           ORDER  BY code, shares DESC""",
                        (_sdw_prev_date,),
                    ).fetchall()
                for _row in _sdw_prev_rows:
                    _c = _row["code"]
                    _sdw_prev_holders_map.setdefault(_c, []).append({
                        "pid": _row["pid"], "name": _row["name"],
                        "sh":  _row["shares"], "pct": _row["pct"],
                    })
                log.info("SDW prev holders: %d stocks loaded from %s",
                         len(_sdw_prev_holders_map), _sdw_prev_date)
        except Exception as _e:
            log.warning("SDW bulk prev holder load failed: %s", _e)
    df_cs = get_ccass_delta_and_avg(stock_codes, ccass_sh_map, today_ds,
                                    today_pct_map=ccass_pct_map)
    ccass_delta_map      = dict(zip(df_cs["stock_code"], df_cs["ccass_delta"]))
    ccass_consec_map     = dict(zip(df_cs["stock_code"], df_cs["ccass_consec"]))
    ccass_streak_pct_map = dict(zip(df_cs["stock_code"], df_cs["ccass_streak_pct"]))
    pct_listed_map       = dict(zip(df_cs["stock_code"], df_cs["pct_listed"]))
    pct_delta_map        = dict(zip(df_cs["stock_code"], df_cs["pct_delta"]))

    # Short average over full universe
    _sa_df        = get_short_avg_ratio(stock_codes, 10, _tv_recent, today_ds)
    short_avg_map = dict(zip(_sa_df["stock_code"], _sa_df["short_avg"]))

    # ── 6. SFC cumulative short positions ────────────────────────────────────
    # Source: SFC Aggregated Reportable Short Positions CSV (published every Friday)
    #   https://www.sfc.hk/-/media/EN/pdf/spr/{YYYY}/{MM}/{DD}/Short_Position_Reporting_Aggregated_Data_{YYYYMMDD}.csv
    #   sh  = 累積沽空股數 (Aggregated Reportable Short Positions — Shares)
    #   hkd = 累積沽空金額 (Aggregated Reportable Short Positions — HK$)
    sfc_map = {}
    if _SFC_AVAILABLE:
        try:
            _sfc_fridays = [d for d in sfc_fridays() if d is not None and d <= trading_day.date()]
            if _sfc_fridays:
                _latest_sfc_ds = max(_sfc_fridays).isoformat()
                for code in stock_codes:
                    pos = sfc_get_position(code, _latest_sfc_ds)
                    if not pos or pos.get("sh", 0) <= 0:
                        continue
                    sfc_sh  = pos["sh"]           # 累積沽空股數 (Shares)
                    sfc_hkd = pos.get("hkd", 0.0) # 累積沽空金額 (HK$)
                    if _SDW_AVAILABLE:
                        total_sh = sdw_get_total_sh(code, today_ds)  # 總數 (CCASS Grand Total)
                        sfc_pct  = round(sfc_sh / total_sh * 100, 4) if total_sh > 0 else 0.0
                    else:
                        sfc_pct = 0.0

                    # Option C: week-on-week delta vs threshold
                    # get_position_history returns newest-first snapshots before latest date
                    sfc_hist = sfc_get_history(code, 5, _latest_sfc_ds)
                    # sfc_hist[0] = last Friday, sfc_hist[1..4] = prior Fridays
                    sfc_prev_pct    = sfc_hist[0].get("pct", 0.0) if sfc_hist else 0.0
                    sfc_week_delta  = round(sfc_pct - sfc_prev_pct, 4) if sfc_prev_pct > 0 else 0.0
                    # HKD week-on-week delta for 沽空增加最多/空頭平倉最多 cards
                    sfc_prev_hkd    = sfc_hist[0].get("hkd", 0.0) if sfc_hist else 0.0
                    sfc_hkd_delta   = round(sfc_hkd - sfc_prev_hkd, 0) if sfc_prev_hkd > 0 else 0.0

                    sfc_map[code] = {
                        "sfc_sh":         sfc_sh,         # 累積沽空股數 (Shares)
                        "sfc_hkd":        sfc_hkd,        # 累積沽空金額 (HK$)
                        "sfc_hkd_delta":  sfc_hkd_delta,  # 累積沽空金額 week-on-week change
                        "sfc_pct":        sfc_pct,         # 累積沽空股數 / 總數 × 100
                        "sfc_week_delta": sfc_week_delta,  # sfc_pct week-on-week change (pp)
                    }
                log.info("SFC short positions: %d stocks from %s%s",
                         len(sfc_map), _latest_sfc_ds,
                         " (pct=0, SDW unavailable)" if not _SDW_AVAILABLE else "")
        except Exception as e:
            log.warning("SFC map build failed: %s", e)

    # ── 7. Southbound top10 ───────────────────────────────────────────────────

    def _build_sb_map(top10_list: list) -> dict:
        m = {}
        for s in top10_list:
            m[s["code"]] = {
                "sb_buy":   s["buy"],
                "sb_sell":  s["sell"],
                "sb_net":   s["buy"] - s["sell"],
                "sb_total": s.get("total", 0),
            }
        return m

    sb_map       = {}
    sb_date_used = today_ds
    _MIN_SB      = 5

    sb_map = _build_sb_map(get_top10(today_ds))
    if sb_map and len(sb_map) < _MIN_SB:
        log.warning("Southbound top10: only %d stocks for %s — discarding", len(sb_map), today_ds)
        sb_map = {}

    if not sb_map:
        # sc_top10_library.py --update runs before main.py in the workflow.
        # If today is still missing, fall back to yesterday's library data.
        prev_td = last_trading_day(trading_day - timedelta(days=1))
        prev_ds = prev_td.strftime("%Y-%m-%d")
        sb_map  = _build_sb_map(get_top10(prev_ds))
        if sb_map:
            sb_date_used = prev_ds
            log.info("Southbound top10: using previous day %s (%d stocks)", prev_ds, len(sb_map))
        else:
            log.warning("Southbound top10: no data — run sc_top10_library.py --update")

    log.info("Southbound top10: %d stocks for %s", len(sb_map), sb_date_used)

    # sb_consec and sb_net_prev
    def _sb_consec_and_prev(code: str) -> tuple[int, int]:
        history   = get_top10_history(code, 30, today_ds)
        prev_net  = (history[0]["buy"] - history[0]["sell"]) if history else 0
        today_net = sb_map.get(code, {}).get("sb_net", 0)
        if today_net < 0:
            consec = -1
            for entry in history:
                net = entry["buy"] - entry["sell"]
                if net < 0:    consec -= 1
                elif net == 0: continue
                else:          break
            return consec, prev_net
        if today_net == 0:
            return 0, prev_net
        consec = 1
        for entry in history:
            net = entry["buy"] - entry["sell"]
            if net > 0:    consec += 1
            elif net == 0: continue
            else:          break
        return consec, prev_net

    sb_consec_map = {}
    sb_prev_map   = {}
    for code in sb_map:
        consec, prev = _sb_consec_and_prev(code)
        sb_consec_map[code] = consec
        sb_prev_map[code]   = prev

    # ── 8. Previous ranks ─────────────────────────────────────────────────────
    prev_ranks = get_prev_ranks(exclude_date=trading_day)

    # ── 9. Name lookup ────────────────────────────────────────────────────────
    # Pre-build short name lookup to avoid O(N) DataFrame scan per stock
    _short_name_map = {} if df_short.empty else dict(
        zip(df_short["stock_code"], df_short["name"])
    )

    def _get_names(code: str) -> tuple[str, str]:
        """Return (name_eng, name_chi) from best available source.

        Priority (chi): stock_ref (verified) > ccass_universe (HKEX SDW) >
                        quotation > short sell > CCASS live > name_map > code
        Priority (eng): quotation > ccass_universe > name_map > stock_ref > code
        """
        ref_zh   = get_zh_name(code)                  # ~130 curated stocks
        uni      = _universe_names.get(code, {})       # HKEX SDW — full universe
        q        = quote_map.get(code, {})
        sh_name  = _short_name_map.get(code, "")
        cc_name  = ccass_name_map.get(code, "")
        nm_entry = _nm.get(code, {})

        name_chi = (ref_zh or uni.get("zh") or q.get("name_chi")
                    or sh_name or cc_name or nm_entry.get("zh") or code)
        name_eng = (q.get("name") or uni.get("en") or nm_entry.get("en") or ref_zh or code)
        return name_eng, name_chi

    # ── Pre-load all library data into memory for fast per-stock lookups ──────
    # Avoids ~8000+ file opens in the stock loop below.
    log.info("Pre-loading library data into memory …")

    def _flat_by_date(load_fn, years):
        """Merge {YYYY: {by_date: {ds: {code: rec}}}} into one flat {ds: {code: rec}}."""
        out = {}
        for y in years:
            out.update(load_fn(y).get("by_date", {}))
        return out

    _years = list(range(2024, trading_day.year + 1))  # last 2 years sufficient for 24-day history

    def _flat_by_date_recent(load_fn, years, n_days=30):
        """Like _flat_by_date but keeps only the most recent n_days dates.
        Sufficient for 24-day rolling history while avoiding loading full multi-year files.
        """
        all_dates = {}
        for y in years:
            all_dates.update(load_fn(y).get("by_date", {}))
        recent = sorted(all_dates.keys(), reverse=True)[:n_days]
        return {ds: all_dates[ds] for ds in recent}

    _tv_all   = _flat_by_date(tv_load_year,  _years)          # turnover: small files, load all
    _sh_all   = _flat_by_date(sl_load_year,  _years)          # short: small files, load all
    _cc_all   = _flat_by_date_recent(_cc_load_year, _years, 30)  # CCASS: large files, last 30 days
    log.info("Pre-loaded: %d tv days | %d short days | %d ccass days",
             len(_tv_all), len(_sh_all), len(_cc_all))

    def _vol_hist(code5, n, before):
        result = []
        for ds in sorted(_tv_all.keys(), reverse=True):
            if ds >= before: continue
            rec = _tv_all[ds].get(code5, {})
            v = rec.get("vol", 0) if isinstance(rec, dict) else 0
            if v > 0: result.append(int(v))
            if len(result) >= n: break
        return result

    def _tv_hist(code5, n, before):
        result = []
        for ds in sorted(_tv_all.keys(), reverse=True):
            if ds >= before: continue
            rec = _tv_all[ds].get(code5, {})
            tv = rec.get("tv", 0) if isinstance(rec, dict) else rec
            if tv > 0: result.append(float(tv))
            if len(result) >= n: break
        return result

    def _sh_hist(code5, n, before):
        result = []
        for ds in sorted(_sh_all.keys(), reverse=True):
            if ds >= before: continue
            rec = _sh_all[ds].get(code5, {})
            if isinstance(rec, dict) and rec.get("sv", 0) > 0:
                result.append({"date": ds, "sv": rec["sv"], "st": rec.get("st", 0)})
            if len(result) >= n: break
        return result

    def _pct_hist(code5, n, before):
        result = []
        for ds in sorted(_cc_all.keys(), reverse=True):
            if ds >= before: continue
            rec = _cc_all[ds].get(code5, {})
            pct = rec.get("pct", 0.0) if isinstance(rec, dict) else 0.0
            if pct > 0: result.append(float(pct))
            if len(result) >= n: break
        return result

    # ── 10. Build results (full universe) ─────────────────────────────────────
    results = []

    for i, code in enumerate(stock_codes, 1):
        q            = quote_map.get(code, {})
        turnover     = q.get("tv", 0)    # 成交金額 (HKD) from HKEX dayquot
        today_vol    = q.get("vol", 0)   # 成交股數 (shares) from HKEX dayquot
        name_eng, name_chi = _get_names(code)

        short_ratio  = short_map.get(code, 0.0)
        short_avg   = short_avg_map.get(code, 0.0)
        short_vol_today = short_vol_map.get(code, 0)

        ccass_delta      = ccass_delta_map.get(code, 0)
        ccass_consec     = ccass_consec_map.get(code, 0)
        ccass_streak_pct = ccass_streak_pct_map.get(code, 0.0)
        pct_listed       = pct_listed_map.get(code, 0.0)
        pct_delta        = pct_delta_map.get(code, 0.0)
        code5        = normalize_code(code)
        tv_avg5_vals = _tv_hist(code5, 5, today_ds)
        tv_avg5      = sum(tv_avg5_vals) / len(tv_avg5_vals) if tv_avg5_vals else 0.0

        vol_hist24  = _vol_hist(code5, 24, today_ds)
        _vol24_days = 1 + min(len(vol_hist24), 23)  # today + up to 23 prior days
        avg_vol24   = (today_vol + sum(vol_hist24[:23])) / _vol24_days if today_vol > 0 or vol_hist24 else 0
        days_to_cover = round(short_vol_today / avg_vol24, 2) if avg_vol24 > 0 else 0.0
        vol_ratio     = round(today_vol / avg_vol24, 2)       if avg_vol24 > 0 else 0.0

        tv_hist24  = _tv_hist(code5, 24, today_ds)
        tv_avg24   = sum(tv_hist24) / len(tv_hist24) if tv_hist24 else 0.0
        tv_ratio   = round(turnover / tv_avg24, 2)  if tv_avg24 > 0 else 0.0
        pct_hist24 = _pct_hist(code5, 24, today_ds)
        pct_avg24_lvl = round(sum(pct_hist24) / len(pct_hist24), 4) if pct_hist24 else 0.0
        pct_dev    = round(pct_listed - pct_avg24_lvl, 4) if pct_avg24_lvl > 0 else 0.0

        # 持倉集中度 — sum(top 5 持股量) / 總數 × 100
        # 總數 = CCASS Grand Total (於中央結算系統的持股量，總數)
        # fallback: sum h.pct directly (each h.pct = holder's 佔已發行股份/權證/單位百分比)
        _holders = _sdw_holders_map.get(code5) or _sdw_holders_map.get(code) or []
        if not _holders and _SDW_AVAILABLE:
            # Last resort: per-stock DB call (only hits when bulk load failed)
            _holders = sdw_get_holders(code, today_ds)
        _total_sh_conc = _sdw_total_sh_map.get(code5, 0)
        if _holders and _total_sh_conc > 0:
            top5_sh = sum(h.get('sh', 0) for h in _holders[:5])
            concentration = round(top5_sh / _total_sh_conc * 100, 2)
        else:
            concentration = round(sum(h.get('pct', 0) for h in _holders[:5]), 2)

        # top10 holders metrics (denominator: 總數)
        top10_sh  = sum(h.get('sh', 0) for h in _holders[:10])                          # 1. raw shares
        top10_pct = round(top10_sh / _total_sh_conc * 100, 2) if _total_sh_conc > 0 and top10_sh > 0 else 0.0  # 2. % of 總數
        # 3. WoW delta — compare against previous SDW snapshot
        _prev_holders    = _sdw_prev_holders_map.get(code5) or _sdw_prev_holders_map.get(code) or []
        if not _prev_holders and _SDW_AVAILABLE:
            # Last resort: per-stock DB call (only hits when bulk load failed)
            _prev_snap = sdw_get_holders_history(code, 1, today_ds)
            _prev_holders = _prev_snap[0] if _prev_snap else []
        _prev_top10_sh   = sum(h.get('sh', 0) for h in _prev_holders[:10])
        _prev_top10_pct  = round(_prev_top10_sh / _total_sh_conc * 100, 2) if _total_sh_conc > 0 and _prev_top10_sh > 0 else 0.0
        top10_pct_delta  = round(top10_pct - _prev_top10_pct, 4) if _prev_top10_pct > 0 else 0.0

        # VWAP = 成交金額 / 成交股數
        vwap = round(turnover / today_vol, 4) if turnover > 0 and today_vol > 0 else 0.0

        # 換手率 (24-day) = (today 成交股數 + prior 23 days' 成交股數) / 總數 × 100
        vol_24d          = today_vol + sum(vol_hist24[:23])  # same window as avg_vol24
        turnover_24d     = round(vol_24d / _total_sh_conc * 100, 4) if _total_sh_conc > 0 and vol_24d > 0 else 0.0
        # prev window = vol_hist24 (days 1–24 back) — yesterday's 24-day window
        vol_24d_prev     = sum(vol_hist24)
        prev_turnover_24d  = round(vol_24d_prev / _total_sh_conc * 100, 4) if _total_sh_conc > 0 and vol_24d_prev > 0 else 0.0
        delta_turnover_24d = round(turnover_24d - prev_turnover_24d, 4)

        stock_type = classify_stock(code, name_eng)
        _, ind_zh  = get_industry(code)

        # ── 挾倉風險評分 (squeeze score 0–14) ────────────────────────────────
        # Mirrors squeezeScoreBreakdown() in index.html — must stay in sync.
        # Components:
        #   concS  0–4  持倉集中度 (concentration %)
        #   srS    0–3  沽空比率 vs type thresholds
        #   srB    0–1  沽空比率 above short_avg (momentum)
        #   dtcS   0–3  回補天數 (days to cover)
        #   dtcB   0–1  dtc above 10-day average (momentum)
        #   volS   0–2  成交量倍數 ≥2.5→2, ≥2.0→1 (mirrors 異常亢奮/挾倉風險 thresholds)
        _sq_lo, _sq_hi, _sq_spike = THRESHOLDS.get(stock_type, THRESHOLDS["general"])[:3]
        _sh10 = _sh_hist(code5, 10, today_ds)
        dtc_avg_10d = round(
            sum(h["sv"] / avg_vol24 for h in _sh10 if avg_vol24 > 0) / len(_sh10), 2
        ) if _sh10 and avg_vol24 > 0 else 0.0

        conc_s = 4 if concentration >= 30 else 3 if concentration >= 20 else 2 if concentration >= 10 else 1 if concentration >= 5 else 0
        sr_s   = 3 if short_ratio > _sq_hi + _sq_spike else 2 if short_ratio > _sq_hi else 1 if short_ratio > _sq_lo else 0
        sr_b   = 1 if short_avg > 0 and short_ratio > short_avg else 0
        dtc_s  = 3 if days_to_cover > 10 else 2 if days_to_cover >= 6 else 1 if days_to_cover >= 3 else 0
        dtc_b  = 1 if dtc_avg_10d > 0 and days_to_cover > dtc_avg_10d else 0
        vol_s  = 2 if vol_ratio >= 2.5 else 1 if vol_ratio >= 2.0 else 0   # mirrors 異常亢奮/挾倉風險 thresholds
        squeeze_score = conc_s + sr_s + sr_b + dtc_s + dtc_b + vol_s

        sb          = sb_map.get(code, {})
        # Signals need price history — suppress for stocks with no turnover history
        has_history = len(tv_hist24) >= 5 and len(vol_hist24) >= 5
        insight = classify_insight(
            stock_type, short_ratio, short_avg,
            turnover, tv_avg5,
            pct_delta=pct_delta,
            days_to_cover=days_to_cover if has_history else 0.0,
            vol_ratio=vol_ratio         if has_history else 0.0,
            tv_ratio=tv_ratio           if has_history else 0.0,
            pct_dev=pct_dev             if has_history else 0.0,
            sb_net=sb.get("sb_net", 0),
            sfc_week_delta=sfc_map.get(code, {}).get("sfc_week_delta", 0.0),
            delta_turnover_24d=delta_turnover_24d,
        )

        prev_rank   = prev_ranks.get(code)
        rank_new    = prev_rank is None
        rank_change = 0 if rank_new else prev_rank - i

        # Use pre-computed maps (populated above for all codes in sb_map).
        # Fall back to ccass_consec for stocks not in today's southbound top10.
        _sb_consec_final = int(sb_consec_map.get(code, 0))
        _sb_prev_final   = int(sb_prev_map.get(code, 0))

        results.append({
            "rank": i, "rank_change": rank_change, "rank_new": rank_new,
            "code": code, "name": name_eng, "name_chi": name_chi,
            "stock_type": stock_type, "industry_zh": ind_zh,
            "turnover": turnover,
            "sb_buy":      sb.get("sb_buy",   0),
            "sb_sell":     sb.get("sb_sell",  0),
            "sb_net":      sb.get("sb_net",   0),
            "sb_total":    sb.get("sb_total", 0),
            "sb_net_prev": _sb_prev_final,
            "sb_consec":   _sb_consec_final,
            "short_ratio":    round(short_ratio, 2),
            "short_avg":      round(short_avg, 2),
            "short_vol":      int(short_vol_today),
            "short_st":       int(short_st_map.get(code, 0)),
            "days_to_cover":  days_to_cover,
            "vol_ratio":      vol_ratio,
            "sfc_sh":         sfc_map.get(code, {}).get("sfc_sh",         0),
            "sfc_hkd":        sfc_map.get(code, {}).get("sfc_hkd",        0.0),
            "sfc_hkd_delta":  sfc_map.get(code, {}).get("sfc_hkd_delta",  0.0),
            "sfc_pct":        sfc_map.get(code, {}).get("sfc_pct",        0.0),
            "sfc_week_delta": sfc_map.get(code, {}).get("sfc_week_delta", 0.0),
            "tv_ratio":  tv_ratio,
            "pct_dev":   round(pct_dev, 4),
            "concentration": concentration,
            "top10_sh":         top10_sh,
            "top10_pct":        top10_pct,
            "top10_pct_delta":  top10_pct_delta,
            "vwap":             vwap,
            "vol":              today_vol,
            "close":            q.get("close", 0.0),
            "turnover_24d":       turnover_24d,
            "delta_turnover_24d": delta_turnover_24d,
            "lockup_threshold": lockup_threshold(tv_avg24),
            "ccass_trade_date":  t2_date.strftime("%Y-%m-%d"),
            "ccass_delta":       int(ccass_delta),
            "ccass_consec":      int(ccass_consec),
            "ccass_streak_pct":  round(ccass_streak_pct, 4),
            "pct_listed": round(pct_listed, 4),
            "pct_delta":  round(pct_delta,  4),
            "insight": insight,
            "squeeze_score": squeeze_score,
            "dtc_avg_10d":   dtc_avg_10d,
            # SDW latest snapshot — used by the 大戶持倉 panel summary cards and table.
            # Full history is in sdw_{code}.json (written by --export-charts).
            "sdw_holders":  [{"pid": h["pid"], "name": h["name"],
                               "sh":  h["sh"],  "pct":  h["pct"]}
                             for h in _holders],
            "sdw_total_sh": int(_total_sh_conc),
        })

    # ── 11. Persist ───────────────────────────────────────────────────────────
    save_rank_history(trading_day, results)
    log.info("rank_history.json updated for %s", today_ds)

    if not suppress_telegram:
        output = {
            "update_time": trading_day.strftime("%Y-%m-%d %H:%M"),
            "sb_date":     sb_date_used,
            "sb_summary":  get_sb_summary(sb_date_used),
            "name_map":    load_store(NAME_MAP_FILE),
            "stocks":      results,
        }
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
        log.info("data.json written: %d stocks", len(results))

        # ── 12. Telegram ──────────────────────────────────────────────────────────
        if results:
            flagged     = [s for s in results if s["insight"]]
            new_entries = [s for s in results if s["rank_new"]]
            big_movers  = [s for s in results if not s["rank_new"] and s["rank_change"] >= 5]
            # Top = highest actual turnover (quotation stocks ranked first)
            top = next((s for s in results if s["turnover"] > 0), results[0])
            top_rc = (f" [↑{top['rank_change']}]" if top["rank_change"] > 0
                      else (" [new]" if top["rank_new"] else ""))
            lines = [
                "📊 港股策略板",
                f"時間: {output['update_time']}",
                f"榜首: {top['name_chi']} ({top['code']}){top_rc} 成交額 {top['turnover']:,}",
                f"異動股: {len(flagged)} 隻 | 新進榜: {len(new_entries)} 隻",
            ]
            if new_entries:
                lines.append("⭐ 新進: " + "、".join(
                    f"{s['name_chi']}({s['code']})" for s in new_entries[:3]))
            if big_movers:
                lines.append("🔺 大升: " + "、".join(
                    f"{s['name_chi']} ↑{s['rank_change']}" for s in big_movers[:3]))
            if flagged:
                lines.append("─────────────")
                for s in flagged[:5]:
                    rc = (f" [↑{s['rank_change']}]" if s["rank_change"] > 0
                          else (" [new]" if s["rank_new"] else ""))
                    lines.append(
                        f"{s['insight']} {s['name_chi']}({s['code']}){rc}"
                        f" | 沽空率 {s['short_ratio']}%"
                        f" | CCASS {'+' if s['pct_delta']>=0 else ''}{s['pct_delta']}pp"
                    )
            send_telegram("\n".join(lines))

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="HK Stock daily analysis")
    ap.add_argument("--date", metavar="YYYY-MM-DD",
                    help="Run analysis for a specific date (backfill mode — no Telegram, no data.json)")
    ap.add_argument("--backfill", metavar="YYYY-MM-DD",
                    help="Backfill rank_history.json from this date to today (all trading days)")
    args = ap.parse_args()

    def _parse_date(s):
        y, m, d = s.split("-")
        return datetime(int(y), int(m), int(d))

    if args.backfill:
        start = _parse_date(args.backfill)
        end   = datetime.now()
        cur   = start
        total = 0
        while cur <= end:
            if is_trading_day(cur):
                log.info("=== backfill: %s ===", cur.strftime("%Y-%m-%d"))
                try:
                    run_analysis(for_date=cur, suppress_telegram=True)
                    total += 1
                except Exception as e:
                    log.error("backfill failed for %s: %s", cur.strftime("%Y-%m-%d"), e)
            cur += timedelta(days=1)
        log.info("Backfill complete: %d trading days processed", total)
    elif args.date:
        run_analysis(for_date=_parse_date(args.date), suppress_telegram=True)
    else:
        run_analysis()
