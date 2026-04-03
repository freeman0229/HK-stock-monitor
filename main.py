import os, json, time, logging, re
import pandas as pd
import requests
import holidays
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
from stock_ref import get_zh_name, get_industry, get_type, STOCKS
from ccass_library import get_pct_history, get_sh_history
from short_library import (get_short_history, get_short_ratio_history,
                            load_year as sl_load_year)
from turnover_library import (load_year as tv_load_year,
                               get_tv_history, get_vol_history,
                               get_close_history, get_close,
                               load_recent as tv_load_recent, get_tv)
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
                              get_holders         as sdw_get_holders
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
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d

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
def fmt_code(val) -> str:
    return str(val).strip().lstrip("0").zfill(5)

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
# Short selling data is parsed from the bottom of d{YYMMDD}c.htm by build_turnover.py.
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
                 "short_volume":   v.get("sv", 0),
                 "short_turnover": v.get("st", 0.0)}
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

def save_short_sell(date: datetime, df: pd.DataFrame):
    """No-op — short data is now saved by build_turnover.py, not main.py."""
    pass

# ── Source 3: CCASS southbound ────────────────────────────────────────────────
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

        def hv(name):
            tag = soup.find("input", {"name": name})
            return tag["value"] if tag else ""

        r2 = s.post(f"{CCASS_URL}?t=hk", data={
            "__EVENTTARGET":        "btnSearch",
            "__EVENTARGUMENT":      "",
            "__VIEWSTATE":          hv("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": hv("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION":    hv("__EVENTVALIDATION"),
            "txtShareholdingDate":  date_str,
            "t":                    "hk",
        }, timeout=30)
        r2.raise_for_status()

        tables = BeautifulSoup(r2.text, "html.parser").find_all("table")
        table  = max(tables, key=lambda t: len(t.find_all("tr"))) if tables else None
        if not table:
            log.warning("CCASS: no table for %s", date_str)
            return EMPTY_CCASS

        def clean(s): return s.split(":")[-1].strip() if ":" in s else s.strip()

        rows = []
        for tr in table.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 4:
                continue
            cr = clean(tds[0]).replace(",", "")
            sr = clean(tds[2]).replace(",", "")
            if not cr.isdigit() or not sr.isdigit():
                continue
            pr = clean(tds[3]).replace("%", "").strip()
            rows.append({"stock_code":   fmt_code(cr),
                         "name":         clean(tds[1]),
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

def save_daily_turnover(date: datetime, df: pd.DataFrame):
    """No-op — turnover data is written by build_turnover.py, not main.py."""
    pass

def save_rank_history(date: datetime, results: list):
    store = load_store(RANK_HISTORY_FILE)
    store[date.strftime("%Y%m%d")] = {r["code"]: r["rank"] for r in results}
    save_store(RANK_HISTORY_FILE, store)

def get_prev_ranks(exclude_date: datetime = None) -> dict:
    """Return rankings from the most recent stored day, excluding today."""
    store = load_store(RANK_HISTORY_FILE)
    if not store:
        return {}
    today_key = exclude_date.strftime("%Y%m%d") if exclude_date else datetime.now().strftime("%Y%m%d")
    keys = sorted(k for k in store.keys() if k != today_key)
    return store[keys[-1]] if keys else {}

def _turnover_avg(code: str, before: str, n: int) -> float:
    vals = get_tv_history(code, n, before)
    return sum(vals) / len(vals) if vals else 0.0

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

def classify_insight(stock_type, short_ratio, short_avg,
                     turnover, tv_avg5,
                     pct_delta=0.0,
                     days_to_cover=0.0, vol_ratio=0.0,
                     tv_ratio=0.0, pct_dev=0.0,
                     sb_net=0,
                     sfc_week_delta=0.0, sfc_level_dev=0.0) -> str | None:
    lo, hi, spike_warn, cover_drop = THRESHOLDS.get(stock_type, THRESHOLDS["general"])
    sfc_up, sfc_dn = SFC_THRESHOLDS.get(stock_type, SFC_THRESHOLDS["general"])
    r_today = turnover / tv_avg5 if tv_avg5 > 0 else 1.0

    if days_to_cover > 5 and vol_ratio > 2:                      return "🔥 挾倉風險"
    if (vol_ratio  >  2.5
            and tv_ratio >  2.0
            and pct_dev  >= 0.5):                                 return "🐉 異常亢奮"
    if (1.8 <= vol_ratio  <= 2.5
            and 1.5 <= tv_ratio <= 2.0
            and 0.2 <= pct_dev  <= 0.5):                          return "🏦 北水增持"

    # SFC structural signals: week-on-week jump AND elevated vs 4-week avg (Option C)
    if sfc_week_delta >= sfc_up  and sfc_level_dev > 0:           return "⚠️ 沽空倉位急增"
    if sfc_week_delta <= sfc_dn  and sfc_level_dev < 0:           return "📊 沽空倉位大減"

    flow_out   = sb_net < 0 and pct_delta < 0
    high_short = short_ratio > hi + spike_warn and vol_ratio > 2
    if flow_out:                  return "🚨 北水流出"
    if high_short:                return "🚨 異常高沽空"

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

def run_analysis():
    today       = datetime.now()
    trading_day = last_trading_day(today)
    log.info("=== analysis — trading day: %s ===", trading_day.strftime("%Y-%m-%d"))
    today_ds = trading_day.strftime("%Y-%m-%d")

    _seed_name_map_from_ref()

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

    # Build quote_map from library data; update name_map with any new names
    quote_map    = {}
    _name_updates = {}
    for _code, _rec in _lib_day.items():
        if not isinstance(_rec, dict) or _rec.get("tv", 0) <= 0:
            continue
        _nm_entry = _nm.get(_code, {})
        _name_en  = _rec.get("name_en") or _nm_entry.get("en") or _code
        _name_zh  = _rec.get("name_zh") or _nm_entry.get("zh") or _name_en
        quote_map[_code] = {
            "tv":       int(_rec["tv"]),
            "vol":      int(_rec.get("vol", 0)),
            "close":    float(_rec.get("close", 0.0)),
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
    short_map     = {}   # code → short_ratio % (same-day vol/sv pair)
    short_vol_map = {}   # code → short volume (shares)
    short_st_map  = {}   # code → short turnover (HKD)
    for row in df_short.itertuples():
        code = row.stock_code
        sv   = int(row.short_volume)
        st   = float(row.short_turnover)
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
    # Ranked by: turnover from quotation where available;
    #            short_turnover (st) as proxy for the rest
    stock_universe = (set(short_vol_map.keys()) |
                      set(ccass_sh_map.keys()) |
                      set(quote_map.keys()))

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
    # Used for 換手率 = 5-day vol sum / total_sh
    _sdw_total_sh_map = sdw_get_total_sh_bulk(today_ds) if _SDW_AVAILABLE else {}
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
    sfc_map = {}
    if _SFC_AVAILABLE:
        try:
            _sfc_fridays = [d for d in sfc_fridays() if d <= trading_day.date()]
            if _sfc_fridays:
                _latest_sfc_ds = max(_sfc_fridays).isoformat()
                for code in stock_codes:
                    pos = sfc_get_position(code, _latest_sfc_ds)
                    if not pos or pos.get("sh", 0) <= 0:
                        continue
                    sfc_sh  = pos["sh"]
                    sfc_hkd = pos.get("hkd", 0.0)
                    if _SDW_AVAILABLE:
                        total_sh = sdw_get_total_sh(code, today_ds)
                        sfc_pct  = round(sfc_sh / total_sh * 100, 4) if total_sh > 0 else 0.0
                    else:
                        sfc_pct = 0.0

                    # Option C: week-on-week delta AND deviation from 4-week rolling avg
                    # get_position_history returns newest-first snapshots before latest date
                    sfc_hist = sfc_get_history(code, 5, _latest_sfc_ds)
                    # sfc_hist[0] = last Friday, sfc_hist[1..4] = prior Fridays
                    sfc_prev_pct    = sfc_hist[0].get("pct", 0.0) if sfc_hist else 0.0
                    sfc_week_delta  = round(sfc_pct - sfc_prev_pct, 4) if sfc_prev_pct > 0 else 0.0
                    # HKD week-on-week delta for 沽空增加最多/空頭平倉最多 cards
                    sfc_prev_hkd    = sfc_hist[0].get("hkd", 0.0) if sfc_hist else 0.0
                    sfc_hkd_delta   = round(sfc_hkd - sfc_prev_hkd, 0) if sfc_prev_hkd > 0 else 0.0
                    # 4-week rolling average (up to 4 prior Fridays)
                    prior_pcts      = [h.get("pct", 0.0) for h in sfc_hist[:4] if h.get("pct", 0.0) > 0]
                    sfc_avg4        = sum(prior_pcts) / len(prior_pcts) if prior_pcts else 0.0
                    sfc_level_dev   = round(sfc_pct - sfc_avg4, 4) if sfc_avg4 > 0 else 0.0

                    sfc_map[code] = {
                        "sfc_sh":         sfc_sh,
                        "sfc_hkd":        sfc_hkd,
                        "sfc_hkd_delta":  sfc_hkd_delta,
                        "sfc_pct":        sfc_pct,
                        "sfc_week_delta": sfc_week_delta,
                        "sfc_level_dev":  sfc_level_dev,
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
    _nm = load_store(NAME_MAP_FILE)
    # Pre-build short name lookup to avoid O(N) DataFrame scan per stock
    _short_name_map = {} if df_short.empty else dict(
        zip(df_short["stock_code"], df_short["name"])
    )

    def _get_names(code: str) -> tuple[str, str]:
        """Return (name_eng, name_chi) from best available source."""
        # Priority: stock_ref > quotation > short sell > CCASS > name_map > code
        ref_zh   = get_zh_name(code)
        q        = quote_map.get(code, {})
        sh_name  = _short_name_map.get(code, "")
        cc_name  = ccass_name_map.get(code, "")
        nm_entry = _nm.get(code, {})

        name_chi = (ref_zh or q.get("name_chi") or sh_name or cc_name
                    or nm_entry.get("zh") or nm_entry.get("en") or code)
        name_eng = (q.get("name") or nm_entry.get("en") or ref_zh or code)
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

    from turnover_library import load_year as _tv_load_year
    from short_library    import load_year as _sh_load_year_inner
    from ccass_library    import load_year as _cc_load_year

    _years = list(range(2024, trading_day.year + 1))  # last 2 years sufficient for 24-day history
    _tv_all   = _flat_by_date(_tv_load_year,          _years)
    _sh_all   = _flat_by_date(_sh_load_year_inner,    _years)
    _cc_all   = _flat_by_date(_cc_load_year,          _years)
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
        turnover     = q.get("tv", 0)
        today_vol    = q.get("vol", 0)
        name_eng, name_chi = _get_names(code)

        short_ratio  = short_map.get(code, 0.0)
        short_avg   = short_avg_map.get(code, 0.0)
        short_vol_today = short_vol_map.get(code, 0)

        ccass_delta      = ccass_delta_map.get(code, 0)
        ccass_consec     = ccass_consec_map.get(code, 0)
        ccass_streak_pct = ccass_streak_pct_map.get(code, 0.0)
        pct_listed       = pct_listed_map.get(code, 0.0)
        pct_delta        = pct_delta_map.get(code, 0.0)
        code5        = code.zfill(5)
        tv_avg5_vals = _tv_hist(code5, 5, today_ds)
        tv_avg5      = sum(tv_avg5_vals) / len(tv_avg5_vals) if tv_avg5_vals else 0.0

        vol_hist24  = _vol_hist(code5, 24, today_ds)
        avg_vol24   = sum(vol_hist24) / len(vol_hist24) if vol_hist24 else 0
        days_to_cover = round(short_vol_today / avg_vol24, 2) if avg_vol24 > 0 else 0.0
        vol_ratio     = round(today_vol / avg_vol24, 2)       if avg_vol24 > 0 else 0.0

        # 換手率 = sum of last 5 trading days' 成交股數 / 總數 (CCASS-custodied shares) × 100
        vol_5d       = sum(vol_hist24[:5])
        vol_20d      = sum(vol_hist24[:20])  # 20-day vol for monthly 換手率
        _ts          = _sdw_total_sh_map.get(code.zfill(5), 0)
        turnover_5d  = round(vol_5d  / _ts * 100, 4) if _ts > 0 and vol_5d  > 0 else 0.0
        turnover_20d = round(vol_20d / _ts * 100, 4) if _ts > 0 and vol_20d > 0 else 0.0

        # turnover_ratio = this week's 換手率 vs 4-week rolling average
        # weeks 1-3 use the same vol_hist24 already loaded — no extra I/O
        _prior_weeks  = [vol_hist24[i:i+5] for i in range(5, 20, 5)]
        _valid_weeks  = [sum(w) for w in _prior_weeks if len(w) == 5]
        _avg_wk_vol   = sum(_valid_weeks) / len(_valid_weeks) if _valid_weeks else 0
        _avg_to_4w    = _avg_wk_vol / _ts * 100 if _ts > 0 and _avg_wk_vol > 0 else 0.0
        turnover_ratio = round(turnover_5d / _avg_to_4w, 2) if _avg_to_4w > 0 else 0.0

        # net_buy_vol = traded volume minus short-sold volume (non-short demand proxy)
        # Use _tv_recent (already loaded) and short history to build 24-day net_buy series
        net_buy_vol_today = max(0, today_vol - short_vol_today)
        sh_hist24         = _sh_hist(code5, 24, today_ds)
        sh_sv_by_date     = {e["date"]: e["sv"] for e in sh_hist24}
        nbv_hist = []
        for yyyymmdd in sorted(_tv_recent.keys(), reverse=True)[:24]:
            ds_iso = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
            rec    = _tv_recent[yyyymmdd].get(code.zfill(5), {})
            v      = rec.get("vol", 0) if isinstance(rec, dict) else 0
            sv     = sh_sv_by_date.get(ds_iso, 0)
            nbv    = max(0, v - sv)
            if nbv > 0:
                nbv_hist.append(nbv)
        avg_nbv24     = sum(nbv_hist) / len(nbv_hist) if nbv_hist else 0
        net_buy_ratio = round(net_buy_vol_today / avg_nbv24, 2) if avg_nbv24 > 0 else 0.0

        tv_hist24  = _tv_hist(code5, 24, today_ds)
        tv_avg24   = sum(tv_hist24) / len(tv_hist24) if tv_hist24 else 0.0
        tv_ratio   = round(turnover / tv_avg24, 2)  if tv_avg24 > 0 else 0.0
        pct_hist24 = _pct_hist(code5, 24, today_ds)
        pct_avg24_lvl = round(sum(pct_hist24) / len(pct_hist24), 4) if pct_hist24 else 0.0
        pct_dev    = round(pct_listed - pct_avg24_lvl, 4) if pct_avg24_lvl > 0 else 0.0

        # VWAP = tv (HKD) / vol (shares) — backward compatible
        _today_rec = _tv_all.get(today_ds, {}).get(code5, {})
        if isinstance(_today_rec, dict) and _today_rec.get('vwap', 0):
            vwap = float(_today_rec['vwap'])
        elif turnover > 0 and today_vol > 0:
            vwap = round(turnover / today_vol, 4)
        else:
            vwap = 0.0

        # 持倉集中度 — sum(top 5 持股量) / 總數 × 100
        # 總數 = CCASS Grand Total (_sdw_total_sh_map); fallback to 佔已發行% sum
        _holders = sdw_get_holders(code, today_ds) if _SDW_AVAILABLE else []
        if not _holders:
            # Try latest available SDW date if today not yet fetched
            _holders = sdw_get_holders(code, (trading_day - timedelta(days=8)).strftime('%Y-%m-%d')) \
                       if _SDW_AVAILABLE else []
        _total_sh_conc = _sdw_total_sh_map.get(code.zfill(5), 0)
        if _holders and _total_sh_conc > 0:
            top5_sh = sum(h.get('sh', 0) for h in _holders[:5])
            concentration = round(top5_sh / _total_sh_conc * 100, 2)
        else:
            concentration = round(sum(h.get('pct', 0) for h in _holders[:5]), 2)

        stock_type = classify_stock(code, name_eng)
        _, ind_zh  = get_industry(code)

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
            sfc_level_dev=sfc_map.get(code, {}).get("sfc_level_dev",  0.0),
        )

        prev_rank   = prev_ranks.get(code)
        rank_new    = prev_rank is None
        rank_change = 0 if rank_new else prev_rank - i

        # Use pre-computed maps (populated above for all codes in sb_map).
        # Fall back to ccass_consec for stocks not in today's southbound top10.
        _sb_consec_final = int(sb_consec_map.get(code, ccass_consec_map.get(code, 0)))
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
            "turnover_5d":    turnover_5d,
            "turnover_20d":   turnover_20d,
            "turnover_ratio": turnover_ratio,
            "net_buy_vol":    int(net_buy_vol_today),
            "net_buy_ratio":  net_buy_ratio,
            "sfc_sh":         sfc_map.get(code, {}).get("sfc_sh",         0),
            "sfc_hkd":        sfc_map.get(code, {}).get("sfc_hkd",        0.0),
            "sfc_hkd_delta":  sfc_map.get(code, {}).get("sfc_hkd_delta",  0.0),
            "sfc_pct":        sfc_map.get(code, {}).get("sfc_pct",        0.0),
            "sfc_week_delta": sfc_map.get(code, {}).get("sfc_week_delta", 0.0),
            "sfc_level_dev":  sfc_map.get(code, {}).get("sfc_level_dev",  0.0),
            "tv_ratio":  tv_ratio,
            "pct_dev":   round(pct_dev, 4),
            "vwap":          vwap,
            "concentration": concentration,
            "lockup_threshold": lockup_threshold(tv_avg24),
            "ccass_trade_date":  t2_date.strftime("%Y-%m-%d"),
            "ccass_delta":       int(ccass_delta),
            "ccass_consec":      int(ccass_consec),
            "ccass_streak_pct":  round(ccass_streak_pct, 4),
            "pct_listed": round(pct_listed, 4),
            "pct_delta":  round(pct_delta,  4),
            "insight": insight,
        })

    # ── 11. Persist ───────────────────────────────────────────────────────────
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
    save_rank_history(trading_day, results)

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
    run_analysis()
