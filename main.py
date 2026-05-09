# ── Changelog ─────────────────────────────────────────────────────────────────
# [Fix 1 — 2026-05-04] classify_insight: replaced pct_dev with top10_pct_delta.
#   pct_dev (Stock Connect %) was only meaningful for ~100 SC stocks; always 0
#   for the rest of ccass_universe. top10_pct_delta (CCASS SDW institutional
#   concentration change) covers all stocks. Thresholds unchanged.
#
# [Fix 2 — 2026-05-04] Removed stale pct_dev computation and data.json output.
#   pct_avg24_lvl/_pct_hist() calls removed; "pct_dev" key removed from output.
#
# [Fix 3 — 2026-05-04] _sdw_prev_date NameError when SDW unavailable.
#   Variable now initialised to None before the if _SDW_AVAILABLE block.
#
# [Fix 4 — 2026-05-04] top10_pct_delta suppressed on first SDW week.
#   Guard changed from `if _prev_top10_pct > 0` to `if _prev_holders` — delta
#   fires whenever a previous snapshot exists, even if prev concentration was 0.
#
# [Fix 5 — 2026-05-04] avg_vol24 denominator wrong when today_vol = 0.
#   Old code counted today even with zero volume, inflating denominator.
#   Fixed: only count today if today_vol > 0; use consistent _prior_days var.
#
# [Fix 6 — 2026-05-04] sfc_week_delta mixed denominators.
#   sfc_prev_pct was read from saved sfc_hist[0].pct (possibly from a prior run
#   with a different total_sh). Now recomputed from raw _prev_sh / total_sh for
#   consistency with current week's sfc_pct.
#
# [Fix 7 — 2026-05-04] _sdw_summary _sc_name stale in max_inc/max_dec loop.
#   Post-loop name re-resolution for max_holder/max_conc now correctly looks up
#   by stored code, not last loop iteration's _sc_name. Added clarifying comment.
#
# [Fix 8 — 2026-05-07] SDW total_sh fallback queried non-existent 'total_sh' table.
#   sdw_get_total_sh_bulk() returns empty if HKEX hasn't published today's SDW.
#   Now falls back to most recent available date in total_sh table, preventing
#   concentration/top10_pct/turnover_24d from being zeroed out for all stocks.
#
# [Fix 9 — 2026-05-05] sc_top10 fallback: wrong holiday logic + insufficient lookback.
#   Old code used last_trading_day() (HK holidays only) and only tried 1 day back.
#   Stock Connect requires BOTH HK and CN exchanges open — CN Golden Week causes
#   sc_top10 to be empty for multiple consecutive HK trading days.
#   Fix: walk back using joint HK+CN open check (_is_sc_open), up to 5 valid SC
#   trading days (14 calendar days max). Handles Golden Week and other long holidays.
# ──────────────────────────────────────────────────────────────────────────────

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
from turnover_library import load_year as tv_load_year, load_recent as tv_load_recent, \
                            get_high_history as tv_get_high_history, \
                            get_low_history  as tv_get_low_history
from sc_top10_library import get_top10, get_top10_history, get_sb_summary
try:
    from sfc_library import get_short_position as sfc_get_position, \
    all_report_fridays as sfc_fridays, get_position_history as sfc_get_history, \
    all_stored_dates as sfc_stored_dates, save_pct_bulk as sfc_save_pct_bulk
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
    MAX_LEN = 4000  # Telegram hard limit is 4096 chars; split to be safe
    chunks = [msg[i:i+MAX_LEN] for i in range(0, len(msg), MAX_LEN)]
    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": chunk}, timeout=15
            )
            if r.status_code == 200:
                log.info("Telegram sent")
            else:
                log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
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
EMPTY_SHORT = pd.DataFrame(columns=["stock_code", "name", "short_volume", "short_turnover"])

def get_short_sell_today(trading_day: datetime) -> tuple:
    """
    Load short selling data from the library for trading_day.
    Falls back to the most recent prior day if today's data is not yet
    available (current day short is stored after the next day's file is fetched).

    """
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
        # Step back one trading day
        prev = target - timedelta(days=1)
        while prev.weekday() >= 5 or datetime(prev.year, prev.month, prev.day).date() in HK_HOLIDAYS:
            prev -= timedelta(days=1)
        target = prev
    log.warning("Short sell: no data found near %s", trading_day.strftime("%Y-%m-%d"))
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
            try:
                pct_val = float(pr) if pr else 0.0
            except ValueError:
                pct_val = 0.0
            rows.append({"stock_code":   normalize_code(cr),
                         "name":         _clean_cell(tds[1]),
                         "shareholding": int(sr),
                         "pct_listed":   pct_val})

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
        streak_pct = 0.0
        if direction != 0:
            streak_pct = pct_delta
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

# ── Rank history ──────────────────────────────────────────────────────────────
RANK_HISTORY_FILE = "rank_history.json"

def save_rank_history(date: datetime, results: list):
    store = load_store(RANK_HISTORY_FILE)
    ds_key = date.strftime("%Y%m%d")
    # Only store rank — all other fields are never read back from rank_history.
    store[ds_key] = {r["code"]: r["rank"] for r in results}
    # Keep only the 2 most recent days — get_prev_ranks() only ever reads yesterday.
    for old_key in sorted(store.keys())[:-2]:
        del store[old_key]
    save_store(RANK_HISTORY_FILE, store)

def get_prev_ranks(exclude_date: datetime = None) -> dict:
    """Return rankings from the most recent stored day, excluding today."""
    store = load_store(RANK_HISTORY_FILE)
    if not store:
        return {}
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
# ETF code list — checked FIRST before stock_ref so it cannot be overridden by
# a wrong type in stock_ref. Name keywords catch future listings not yet in the list.
_ETF_CODES = {
    "02800","02801","02809","02816","02817","02819","02821","02822","02823","02825",
    "02827","02828","02832","02835","02836","02838","02839","02840","02846",
    "03009","03010","03011","03012","03013","03019","03020","03021","03024",
    "03032","03033","03035","03037","03040","03041","03049","03056","03060",
    "03067","03070","03072","03079","03081","03086","03087","03096","03097",
    "03110","03115","03118","03122","03127","03128","03129","03143","03145",
    "03147","03150","03160","03161","03162","03165","03171","03175","03188",
    "02803","02820","02845","03003","03191","03486",
    # previously missing ETFs
    "03451","03190","03415","03195","03455",
}
_ETF_NAME_KW = ("ETF", "TRACKER FUND", "INDEX FUND",
                "GX CHINA", "GX HK", "GX ", "CSOP", "PREMIA")

# Derivative products — leveraged/inverse/futures; structurally extreme short ratios
_DERIV_NAME_KW = ("LEVERAGED", "INVERSE", "FUTURES ETF", "L&I")

# Bond instruments — bond ETFs + retail/govt bonds (04xxx series)
_BOND_CODES = {
    "02829",  # iShares China Govt Bond ETF 安碩中國國債
    "03108",  # Premia ESG領先債券ETF 嗎實ESG領
}
_BOND_NAME_KW = ("BOND ETF", "GOLD ETF", "MONEY MARKET ETF",
                 "IBOND", "EXCHANGE FUND NOTE", "EFN", " BOND ")

def classify_stock(code: str, name: str) -> str:
    n = name.upper()
    # Non-equity checks FIRST — take priority over stock_ref
    if code in _BOND_CODES or any(k in n for k in _BOND_NAME_KW): return "bond"
    if any(k in n for k in _DERIV_NAME_KW):                        return "derivative"
    if code in _ETF_CODES or any(k in n for k in _ETF_NAME_KW):   return "etf"
    # Also treat any 04xxx code as bond (HK retail/govt bonds)
    if code.startswith("04"):                                       return "bond"
    # stock_ref explicit types (bluechip/stable/general) come next
    t = get_type(code)
    if t: return t
    # Keyword fallback for stocks not in stock_ref
    STABLE_KW   = ("BANK","ENERGY","POWER","GAS","PETRO","SINOPEC","CNOOC","MTR","UTILITY")
    BLUECHIP_KW = ("TENCENT","MEITUAN","ALIBABA","BABA","XIAOMI","HSBC","AIA","PING AN",
                   "HKEX","CK ","HENDERSON","SHK","SWIRE","GALAXY","SANDS","MELCO")
    if any(k in n for k in STABLE_KW):   return "stable"
    if any(k in n for k in BLUECHIP_KW): return "bluechip"
    return "general"

THRESHOLDS = {
    #              lo    hi  spike  cover_drop
    "etf":      (40.0, 70.0, 15.0, 0.60),
    "bond":     (40.0, 70.0, 15.0, 0.60),
    "derivative":(40.0,70.0, 15.0, 0.60),
    "stable":   ( 5.0, 10.0, 15.0, 0.60),
    "bluechip": (10.0, 20.0, 10.0, 0.60),
    "general":  (10.0, 25.0, 15.0, 0.60),
}

SFC_THRESHOLDS = {
    #              spike_up  unwind
    "etf":      (    3.0,    -3.0 ),
    "bond":     (    3.0,    -3.0 ),
    "derivative":(   3.0,    -3.0 ),
    "stable":   (    1.0,    -1.0 ),
    "bluechip": (    1.5,    -1.5 ),
    "general":  (    1.0,    -1.0 ),
}

# ── 鎖倉臨界點 classifier ────────────────────────────────────────────────────
_LOCKUP_LARGE  = 1_000_000_000   # 10億 HKD
_LOCKUP_MID    =   200_000_000   #  2億 HKD

def lockup_threshold(tv_avg24: float) -> float:
    if tv_avg24 >= _LOCKUP_LARGE:
        return 75.0
    if tv_avg24 >= _LOCKUP_MID:
        return 60.0
    return 90.0

# ── 換手率 delta thresholds ──────────────────────────────────────────────────
_TURNOVER_DELTA_HIGH     = 7.0
_TURNOVER_DELTA_ELEVATED = 4.0

def classify_insight(stock_type, short_ratio, short_avg,
                     turnover, tv_avg5,
                     pct_delta=0.0,
                     days_to_cover=0.0, vol_ratio=0.0,
                     tv_ratio=0.0, top10_pct_delta=0.0,
                     sb_net=0,
                     sfc_week_delta=0.0,
                     delta_turnover_24d=0.0) -> str | None:
    # ETFs, bonds and derivatives never generate signals — structural behaviour
    if stock_type in ("etf", "bond", "derivative"):               return None
    lo, hi, spike_warn, cover_drop = THRESHOLDS.get(stock_type, THRESHOLDS["general"])
    sfc_up, sfc_dn = SFC_THRESHOLDS.get(stock_type, SFC_THRESHOLDS["general"])
    r_today = turnover / tv_avg5 if tv_avg5 > 0 else 0.0

    if days_to_cover > 5 and vol_ratio > 2:                      return "🔥 挾倉風險"
    if (vol_ratio        >  2.5
            and tv_ratio >  2.0
            and top10_pct_delta >= 0.5):                          return "🐉 異常亢奮"
    if (1.8 <= vol_ratio        <= 2.5
            and 1.5 <= tv_ratio <= 2.0
            and 0.2 <= top10_pct_delta <= 0.5):                   return "🏦 大戶增持"
    if sb_net > 0 and pct_delta > 0:                             return "🏦 北水增持"

    if sfc_week_delta >= sfc_up:                                  return f"⚠️ 沽空倉位急增 +{sfc_week_delta:.1f}pp"
    if sfc_week_delta <= sfc_dn:                                  return f"📊 沽空倉位大減 {sfc_week_delta:.1f}pp"

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
    _universe_names = get_universe()

    # ── 1. Daily quotation ────────────────────────────────────────────────────
    _nm      = load_store(NAME_MAP_FILE)
    _lib_day = tv_load_year(trading_day.year).get("by_date", {}).get(today_ds, {})

    if not _lib_day:
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

    quote_map    = {}
    _name_updates = {}
    for _code, _rec in _lib_day.items():
        if not isinstance(_rec, dict) or _rec.get("tv", 0) <= 0:
            continue
        _code5 = normalize_code(_code)
        _nm_entry = _nm.get(_code5, {})
        _name_en  = _rec.get("name_en") or _nm_entry.get("en") or _code5
        _name_zh  = _rec.get("name_zh") or _nm_entry.get("zh") or _name_en
        quote_map[_code5] = {
            "tv":       int(_rec["tv"]),
            "vol":      int(_rec.get("vol", 0)),
            "close":    float(_rec.get("close", 0.0)),
            "name":     _name_en,
            "name_chi": _name_zh,
        }
        if _code5 not in _nm:
            _name_updates[_code5] = {"en": _name_en, "zh": _name_zh}
    if _name_updates:
        _update_name_map(_name_updates)
    log.info("Turnover library: %d stocks for %s", len(quote_map), today_ds)

    # ── 2. Short selling ──────────────────────────────────────────────────────
    df_short, short_date = get_short_sell_today(trading_day)
    _short_vol_map_ref = quote_map
    if short_date and short_date != today_ds:
        _short_lib_day = tv_load_year(int(short_date[:4])).get("by_date", {}).get(short_date, {})
        if _short_lib_day:
            _short_vol_map_ref = {
                normalize_code(code): {"vol": int(rec.get("vol", 0))}
                for code, rec in _short_lib_day.items()
                if isinstance(rec, dict)
            }
            log.info("Short ratio: using %s volume for denominator", short_date)
    short_map     = {}
    short_vol_map = {}
    short_st_map  = {}
    for row in df_short.itertuples():
        code = row.stock_code
        sv   = int(row.short_volume)
        st   = float(row.short_turnover)
        short_vol_map[code] = sv
        short_st_map[code]  = st
        traded_vol = _short_vol_map_ref.get(code, {}).get("vol", 0)
        if traded_vol > 0:
            short_map[code] = round(sv / traded_vol * 100, 2)

    # ── 3. CCASS southbound ───────────────────────────────────────────────────
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
    stock_universe = {normalize_code(c)
                      for c in (set(short_vol_map.keys()) |
                                set(ccass_sh_map.keys()) |
                                set(quote_map.keys()))
                      if _universe_included(c)}

    def _sort_key(code):
        tv = quote_map.get(code, {}).get("tv", 0)
        st = short_st_map.get(code, 0)
        return (-tv, -st)

    stock_codes = sorted(stock_universe, key=_sort_key)
    log.info("Stock universe: %d stocks (%d from quotation, %d from short sell, %d from CCASS)",
             len(stock_codes), len(quote_map), len(short_vol_map), len(ccass_sh_map))

    # ── 5. CCASS deltas ───────────────────────────────────────────────────────
    _tv_recent = tv_load_recent(35, today_ds)

    _sdw_best_date: str | None = None
    _sdw_holders_map: dict[str, list] = {}

    if _SDW_AVAILABLE:
        from ccass_sdw_library import DB_PATH as _SDW_DB_PATH, get_conn as _sdw_get_conn
        import sqlite3 as _sqlite3
        try:
            with _sdw_get_conn(_SDW_DB_PATH) as _sc:
                _sdw_best_date = _sc.execute(
                    "SELECT MAX(date) FROM metadata WHERE date <= ?", (today_ds,)
                ).fetchone()[0]
            if _sdw_best_date:
                if _sdw_best_date != today_ds:
                    log.info("SDW holders: today not available — using %s", _sdw_best_date)
                with _sdw_get_conn(_SDW_DB_PATH) as _sc:
                    _sdw_rows = _sc.execute(
                        """SELECT h.code, h.pid, COALESCE(p.name,'') AS name, h.shares, h.pct
                           FROM   holdings h
                           LEFT JOIN participants p ON p.pid = h.pid
                           WHERE  h.date = ?
                           ORDER  BY h.code, h.shares DESC""",
                        (_sdw_best_date,),
                    ).fetchall()
                for _row in _sdw_rows:
                    _c = normalize_code(_row["code"])
                    _sdw_holders_map.setdefault(_c, []).append({
                        "pid": _row["pid"], "name": _row["name"],
                        "sh":  _row["shares"], "pct": _row["pct"],
                    })
                log.info("SDW holders: %d stocks loaded from %s",
                         len(_sdw_holders_map), _sdw_best_date)
        except Exception as _e:
            log.warning("SDW bulk holder load failed: %s — falling back to per-stock", _e)

    _sdw_total_sh_date = _sdw_best_date or today_ds
    _sdw_total_sh_map = sdw_get_total_sh_bulk(_sdw_total_sh_date) if _SDW_AVAILABLE else {}
    if _SDW_AVAILABLE:
        _sdw_total_sh_map = {normalize_code(k): v for k, v in _sdw_total_sh_map.items()}
        log.info("SDW total_sh: %d stocks from %s", len(_sdw_total_sh_map), _sdw_total_sh_date)
        # Fallback: if today's total_sh not yet published, use most recent available date
        # Note: get_total_sh_bulk already uses MAX(date) <= before_or_eq per code,
        # so empty result means the DB itself has no data (e.g. failed download).
        if not _sdw_total_sh_map:
            try:
                with _sdw_get_conn(_SDW_DB_PATH) as _sc:
                    _fb_date = _sc.execute(
                        "SELECT MAX(date) FROM metadata WHERE date < ?",
                        (_sdw_total_sh_date,)
                    ).fetchone()
                if _fb_date and _fb_date[0]:
                    _sdw_total_sh_date = _fb_date[0]
                    _sdw_total_sh_map = {normalize_code(k): v
                                         for k, v in sdw_get_total_sh_bulk(_sdw_total_sh_date).items()}
                    log.info("SDW total_sh fallback: %d stocks from %s",
                             len(_sdw_total_sh_map), _sdw_total_sh_date)
            except Exception as _e:
                log.warning("SDW total_sh fallback failed: %s", _e)

    _sdw_prev_holders_map: dict[str, list] = {}
    _sdw_prev_date: str | None = None   # safe default — referenced in _sdw_summary below
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
                        """SELECT h.code, h.pid, COALESCE(p.name,'') AS name, h.shares, h.pct
                           FROM   holdings h
                           LEFT JOIN participants p ON p.pid = h.pid
                           WHERE  h.date = ?
                           ORDER  BY h.code, h.shares DESC""",
                        (_sdw_prev_date,),
                    ).fetchall()
                for _row in _sdw_prev_rows:
                    _c = normalize_code(_row["code"])
                    _sdw_prev_holders_map.setdefault(_c, []).append({
                        "pid": _row["pid"], "name": _row["name"],
                        "sh":  _row["shares"], "pct": _row["pct"],
                    })
                log.info("SDW prev holders: %d stocks loaded from %s",
                         len(_sdw_prev_holders_map), _sdw_prev_date)
        except Exception as _e:
            log.warning("SDW bulk prev holder load failed: %s", _e)

    # Use T-2 date as cutoff so pct_prev aligns with pct_today (both from CCASS T-2 settlement)
    _ccass_ds = t2_date.strftime("%Y-%m-%d")
    df_cs = get_ccass_delta_and_avg(stock_codes, ccass_sh_map, _ccass_ds,
                                    today_pct_map=ccass_pct_map)
    ccass_delta_map      = dict(zip(df_cs["stock_code"], df_cs["ccass_delta"]))
    ccass_consec_map     = dict(zip(df_cs["stock_code"], df_cs["ccass_consec"]))
    ccass_streak_pct_map = dict(zip(df_cs["stock_code"], df_cs["ccass_streak_pct"]))
    pct_listed_map       = dict(zip(df_cs["stock_code"], df_cs["pct_listed"]))
    pct_delta_map        = dict(zip(df_cs["stock_code"], df_cs["pct_delta"]))

    _sa_df        = get_short_avg_ratio(stock_codes, 10, _tv_recent, today_ds)
    short_avg_map = dict(zip(_sa_df["stock_code"], _sa_df["short_avg"]))

    # ── 6. SFC cumulative short positions ─────────────────────────────────────
    sfc_map = {}
    if _SFC_AVAILABLE:
        try:
            _today_str    = trading_day.date().isoformat()
            _sfc_stored   = sfc_stored_dates()
            _sfc_fridays  = sorted(d for d in _sfc_stored if d and isinstance(d, str) and d <= _today_str)
            if _sfc_fridays:
                _latest_sfc_ds = _sfc_fridays[-1]
                for code in stock_codes:
                    pos = sfc_get_position(code, _latest_sfc_ds)
                    if not pos or not pos.get("sh") or pos["sh"] <= 0:
                        continue
                    sfc_sh  = pos["sh"]
                    sfc_hkd = pos.get("hkd", 0.0)
                    # Use bulk-loaded total_sh map (avoids per-stock DB call, works without SDW)
                    total_sh = _sdw_total_sh_map.get(code, 0)
                    sfc_pct  = round(sfc_sh / total_sh * 100, 4) if total_sh > 0 else 0.0

                    sfc_hist = sfc_get_history(code, 5, _latest_sfc_ds)
                    # Recompute prev_pct from raw sh + same total_sh denominator for
                    # consistency — sfc_hist[0].pct may have been saved by a prior run
                    # with a different total_sh, making the delta unreliable.
                    _prev_sh       = (sfc_hist[0].get("sh") or 0) if sfc_hist else 0
                    sfc_prev_pct   = round(_prev_sh / total_sh * 100, 4) if total_sh > 0 and _prev_sh > 0 else 0.0
                    sfc_week_delta = round(sfc_pct - sfc_prev_pct, 4) if sfc_prev_pct > 0 else 0.0
                    sfc_prev_hkd   = (sfc_hist[0].get("hkd") or 0.0) if sfc_hist else 0.0
                    sfc_hkd_delta  = int(round(sfc_hkd - sfc_prev_hkd, 0)) if sfc_prev_hkd > 0 else 0
                    sfc_sh_delta   = int(sfc_sh - _prev_sh) if _prev_sh > 0 else 0

                    sfc_map[code] = {
                        "sfc_sh":         sfc_sh,
                        "sfc_sh_delta":   sfc_sh_delta,
                        "sfc_hkd":        sfc_hkd,
                        "sfc_hkd_delta":  sfc_hkd_delta,
                        "sfc_pct":        sfc_pct,
                        "sfc_week_delta": sfc_week_delta,
                    }
                log.info("SFC short positions: %d stocks from %s%s",
                         len(sfc_map), _latest_sfc_ds,
                         " (pct=0, SDW unavailable)" if not _SDW_AVAILABLE else "")
                # Persist pct into sfc_YYYY.json so frontend reads it directly
                # (avoids recomputing at render time and keeps data.json lean)
                if _SDW_AVAILABLE and sfc_map:
                    sfc_save_pct_bulk(
                        _latest_sfc_ds,
                        {code: v["sfc_pct"] for code, v in sfc_map.items()}
                    )
        except Exception as e:
            log.warning("SFC map build failed: %s", e)

    # ── 7. Southbound top10 ────────────────────────────────────────────────────
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
        # Walk back up to 10 calendar days, skipping days where EITHER HK or CN is closed.
        # Stock Connect requires BOTH exchanges open — CN Golden Week / HK holidays both count.
        def _is_sc_open(d: datetime) -> bool:
            hk_open = d.weekday() < 5 and d.date() not in HK_HOLIDAYS
            cn_open = d.weekday() < 5 and not _is_cn_holiday(d)
            return hk_open and cn_open

        d = trading_day - timedelta(days=1)
        found_days = 0
        for _ in range(14):  # safety limit
            if _is_sc_open(d):
                prev_ds = d.strftime("%Y-%m-%d")
                sb_map  = _build_sb_map(get_top10(prev_ds))
                if sb_map:
                    sb_date_used = prev_ds
                    log.info("Southbound top10: using previous day %s (%d stocks)",
                             prev_ds, len(sb_map))
                    break
                found_days += 1
                if found_days >= 5:
                    break  # tried 5 valid SC trading days, give up
            d -= timedelta(days=1)
        if not sb_map:
            log.warning("Southbound top10: no data — run sc_top10_library.py --update")

    log.info("Southbound top10: %d stocks for %s", len(sb_map), sb_date_used)

    def _sb_consec_and_prev(code: str) -> tuple:
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

    # ── 8. Previous ranks ──────────────────────────────────────────────────────
    prev_ranks = get_prev_ranks(exclude_date=trading_day)

    # ── 9. Name lookup ─────────────────────────────────────────────────────────
    _short_name_map = {} if df_short.empty else dict(
        zip(df_short["stock_code"], df_short["name"])
    )

    def _get_names(code: str) -> tuple:
        ref_zh   = get_zh_name(code)
        uni      = _universe_names.get(code, {})
        q        = quote_map.get(code, {})
        sh_name  = _short_name_map.get(code, "")
        cc_name  = ccass_name_map.get(code, "")
        nm_entry = _nm.get(code, {})

        name_chi = (ref_zh or uni.get("zh") or q.get("name_chi")
                    or sh_name or cc_name or nm_entry.get("zh") or code)
        name_eng = (q.get("name") or uni.get("en") or nm_entry.get("en") or ref_zh or code)
        return name_eng, name_chi

    # ── Pre-load all library data into memory ─────────────────────────────────
    log.info("Pre-loading library data into memory …")

    def _flat_by_date(load_fn, years):
        out = {}
        for y in years:
            out.update(load_fn(y).get("by_date", {}))
        return out

    _years = list(range(trading_day.year - 1, trading_day.year + 1))

    def _flat_by_date_recent(load_fn, years, n_days=30):
        all_dates = {}
        for y in years:
            all_dates.update(load_fn(y).get("by_date", {}))
        recent = sorted(all_dates.keys(), reverse=True)[:n_days]
        return {ds: all_dates[ds] for ds in recent}

    _tv_all   = _flat_by_date(tv_load_year,  _years)
    _sh_all   = _flat_by_date(sl_load_year,  _years)
    _cc_all   = _flat_by_date_recent(_cc_load_year, _years, 30)
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

    # ── 10. Build results ─────────────────────────────────────────────────────
    results = []

    for i, code in enumerate(stock_codes, 1):
        q            = quote_map.get(code, {})
        turnover     = q.get("tv", 0)
        today_vol    = q.get("vol", 0)
        name_eng, name_chi = _get_names(code)

        short_ratio  = short_map.get(code, 0.0)
        short_avg    = short_avg_map.get(code, 0.0)
        short_vol_today = short_vol_map.get(code, 0)

        ccass_delta      = ccass_delta_map.get(code, 0)
        ccass_consec     = ccass_consec_map.get(code, 0)
        ccass_streak_pct = ccass_streak_pct_map.get(code, 0.0)
        pct_listed       = pct_listed_map.get(code, 0.0)
        pct_delta        = pct_delta_map.get(code, 0.0)
        tv_avg5_vals = _tv_hist(code, 5, today_ds)
        tv_avg5      = sum(tv_avg5_vals) / len(tv_avg5_vals) if tv_avg5_vals else 0.0

        vol_hist24  = _vol_hist(code, 24, today_ds)
        # Always include today in the 24-day average; cap history slice at 23 prior days
        _prior_days  = vol_hist24[:23]
        _vol24_days  = (1 if today_vol > 0 else 0) + len(_prior_days)
        avg_vol24    = (today_vol + sum(_prior_days)) / _vol24_days if _vol24_days > 0 else 0
        days_to_cover = round(short_vol_today / avg_vol24, 2) if avg_vol24 > 0 else 0.0
        vol_ratio     = round(today_vol / avg_vol24, 2)       if avg_vol24 > 0 else 0.0

        tv_hist24  = _tv_hist(code, 24, today_ds)
        tv_avg24   = sum(tv_hist24) / len(tv_hist24) if tv_hist24 else 0.0
        tv_ratio   = round(turnover / tv_avg24, 2)  if tv_avg24 > 0 else 0.0
        # pct_dev removed — was based on Stock Connect % (mutualmarket ?t=hk) which is
        # only meaningful for ~100 SC stocks. classify_insight now uses top10_pct_delta
        # (CCASS SDW top-10 institutional concentration change) which covers all stocks.

        # ── Kan-style channel σ (global, asymmetric) ─────────────────────────
        # σ_up   = std(high[i] - close[i-1])  — upward move from prev close to this day's high
        # σ_down = std(close[i-1] - low[i])   — downward move from prev close to this day's low
        # Computed over ALL available history (global σ, not rolling).
        _all_dates_sorted = sorted(_tv_all.keys())
        _up_moves, _dn_moves = [], []
        for _di in range(1, len(_all_dates_sorted)):
            _ds_prev = _all_dates_sorted[_di - 1]
            _ds_cur  = _all_dates_sorted[_di]
            _rec_prev = _tv_all[_ds_prev].get(code, {})
            _rec_cur  = _tv_all[_ds_cur].get(code, {})
            if not isinstance(_rec_prev, dict) or not isinstance(_rec_cur, dict): continue
            _pc = _rec_prev.get("close", 0.0)
            _hi = _rec_cur.get("high",  0.0)
            _lo = _rec_cur.get("low",   0.0)
            if _pc > 0 and _hi > _pc: _up_moves.append(_hi - _pc)
            if _pc > 0 and _lo > 0 and _pc > _lo: _dn_moves.append(_pc - _lo)
        def _std(arr):
            if len(arr) < 2: return 0.0
            m = sum(arr) / len(arr)
            return (sum((x - m) ** 2 for x in arr) / len(arr)) ** 0.5
        sigma_up   = round(_std(_up_moves),   4)
        sigma_down = round(_std(_dn_moves),   4)

        # vol_ratio_5: today vol / 5-day avg vol (for channel band width)
        _vol5 = _vol_hist(code, 5, today_ds)
        _avg_vol5 = sum(_vol5) / len(_vol5) if _vol5 else 0.0
        vol_ratio_5 = round(today_vol / _avg_vol5, 4) if _avg_vol5 > 0 and today_vol > 0 else 1.0

        _holders = _sdw_holders_map.get(code) or []
        if not _holders and _SDW_AVAILABLE:
            _holders = sdw_get_holders(code, today_ds)
        _total_sh_conc = _sdw_total_sh_map.get(code, 0)
        if _holders and _total_sh_conc > 0:
            top5_sh = sum(h.get('sh', 0) for h in _holders[:5])
            concentration = round(top5_sh / _total_sh_conc * 100, 2)
        else:
            concentration = round(sum(h.get('pct', 0) for h in _holders[:5]), 2)

        top10_sh  = sum(h.get('sh', 0) for h in _holders[:10])
        top10_pct = round(top10_sh / _total_sh_conc * 100, 2) if _total_sh_conc > 0 and top10_sh > 0 else 0.0
        _prev_holders    = _sdw_prev_holders_map.get(code) or []
        if not _prev_holders and _SDW_AVAILABLE:
            _prev_snap = sdw_get_holders_history(code, 1, today_ds)
            _prev_holders = _prev_snap[0] if _prev_snap else []
        _prev_top10_sh   = sum(h.get('sh', 0) for h in _prev_holders[:10])
        _prev_top10_pct  = round(_prev_top10_sh / _total_sh_conc * 100, 2) if _total_sh_conc > 0 and _prev_top10_sh > 0 else 0.0
        # Use presence of prev holders (not _prev_top10_pct > 0) as guard — avoids
        # suppressing delta on the first week a stock enters SDW tracking.
        top10_pct_delta  = round(top10_pct - _prev_top10_pct, 4) if _prev_holders else 0.0

        vwap = round(turnover / today_vol, 4) if turnover > 0 and today_vol > 0 else 0.0

        vol_24d          = today_vol + sum(_prior_days)
        turnover_24d     = round(vol_24d / _total_sh_conc * 100, 4) if _total_sh_conc > 0 and vol_24d > 0 else 0.0
        vol_24d_prev     = sum(vol_hist24)  # all 24 prior-day vols (excludes today) for prev window
        prev_turnover_24d  = round(vol_24d_prev / _total_sh_conc * 100, 4) if _total_sh_conc > 0 and vol_24d_prev > 0 else 0.0
        delta_turnover_24d = round(turnover_24d - prev_turnover_24d, 4)

        stock_type = classify_stock(code, name_eng)
        _, ind_zh  = get_industry(code)

        # ── 挾倉風險評分 (squeeze score 0–14) ──────────────────────────────────
        _sq_lo, _sq_hi, _sq_spike = THRESHOLDS.get(stock_type, THRESHOLDS["general"])[:3]
        _sh10 = _sh_hist(code, 10, today_ds)
        dtc_avg_10d = round(
            sum(h["sv"] / avg_vol24 for h in _sh10 if avg_vol24 > 0) / len(_sh10), 2
        ) if _sh10 and avg_vol24 > 0 else 0.0

        conc_s = 4 if concentration >= 30 else 3 if concentration >= 20 else 2 if concentration >= 10 else 1 if concentration >= 5 else 0
        sr_s   = 3 if short_ratio > _sq_hi + _sq_spike else 2 if short_ratio > _sq_hi else 1 if short_ratio > _sq_lo else 0
        sr_b   = 1 if short_avg > 0 and short_ratio > short_avg else 0
        dtc_s  = 3 if days_to_cover > 10 else 2 if days_to_cover >= 6 else 1 if days_to_cover >= 3 else 0
        dtc_b  = 1 if dtc_avg_10d > 0 and days_to_cover > dtc_avg_10d else 0
        vol_s  = 2 if vol_ratio >= 2.5 else 1 if vol_ratio >= 2.0 else 0
        squeeze_score = conc_s + sr_s + sr_b + dtc_s + dtc_b + vol_s

        sb          = sb_map.get(code, {})
        has_history = len(tv_hist24) >= 5 and len(vol_hist24) >= 5
        insight = classify_insight(
            stock_type, short_ratio, short_avg,
            turnover, tv_avg5,
            pct_delta=pct_delta,
            days_to_cover=days_to_cover    if has_history else 0.0,
            vol_ratio=vol_ratio            if has_history else 0.0,
            tv_ratio=tv_ratio              if has_history else 0.0,
            top10_pct_delta=top10_pct_delta if has_history else 0.0,
            sb_net=sb.get("sb_net", 0),
            sfc_week_delta=sfc_map.get(code, {}).get("sfc_week_delta", 0.0),
            delta_turnover_24d=delta_turnover_24d,
        )

        prev_rank   = prev_ranks.get(code)
        rank_new    = prev_rank is None
        rank_change = 0 if rank_new else prev_rank - i

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
            "sfc_sh_delta":   sfc_map.get(code, {}).get("sfc_sh_delta",   0),
            "sfc_hkd":        sfc_map.get(code, {}).get("sfc_hkd",        0.0),
            "sfc_hkd_delta":  sfc_map.get(code, {}).get("sfc_hkd_delta",  0.0),
            "sfc_pct":        sfc_map.get(code, {}).get("sfc_pct",        0.0),
            "sfc_week_delta": sfc_map.get(code, {}).get("sfc_week_delta", 0.0),
            "tv_ratio":  tv_ratio,
            "sigma_up":        sigma_up,
            "sigma_down":      sigma_down,
            "vol_ratio_5":     vol_ratio_5,
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
            # sdw_holders omitted from data.json — too large for localStorage quota.
            # The SDW panel lazy-fetches per-stock sdw_{code}.json files on demand instead.
        })

    # ── 11. Persist ───────────────────────────────────────────────────────────
    save_rank_history(trading_day, results)
    log.info("rank_history.json updated for %s", today_ds)

    if suppress_telegram:
        log.info("suppress_telegram=True — data.json skipped (backfill/date mode)")
        return

    _insight_by_code      = {r["code"]: r["insight"]          for r in results}
    _pct_delta_by_code    = {r["code"]: r["pct_delta"]        for r in results}
    _ccass_consec_by_code = {r["code"]: r["ccass_consec"]     for r in results}
    _ccass_streak_by_code = {r["code"]: r["ccass_streak_pct"] for r in results}
    northbound_flow = []
    for code, sb in sb_map.items():
        _ne, _nc = _get_names(code)
        northbound_flow.append({
            "code":             code,
            "name":             _ne,
            "name_chi":         _nc,
            "sb_buy":           sb["sb_buy"],
            "sb_sell":          sb["sb_sell"],
            "sb_net":           sb["sb_net"],
            "sb_total":         sb.get("sb_total", 0),
            "sb_net_prev":      int(sb_prev_map.get(code, 0)),
            "sb_consec":        int(sb_consec_map.get(code, 0)),
            "turnover":         quote_map.get(code, {}).get("tv", 0),
            "insight":          _insight_by_code.get(code),
            "pct_delta":        _pct_delta_by_code.get(code, 0.0),
            "ccass_consec":     _ccass_consec_by_code.get(code, 0),
            "ccass_streak_pct": _ccass_streak_by_code.get(code, 0.0),
        })
    northbound_flow.sort(key=lambda x: abs(x["sb_net"]), reverse=True)

    ccass_holdings = sorted(
        [
            {
                "code":        row.stock_code,
                "name_chi":    ccass_name_map.get(row.stock_code, row.stock_code),
                "shareholding": int(row.shareholding),
                "pct_listed":  round(float(row.pct_listed), 4),
            }
            for row in df_ccass.itertuples()
            if row.pct_listed > 0
        ],
        key=lambda x: x["pct_listed"],
        reverse=True,
    )
    log.info("ccass_holdings: %d stocks written to data.json", len(ccass_holdings))

    # ── SDW summary cards (pre-computed server-side) ──────────────────────────
    _sdw_summary = {"date": _sdw_best_date, "prev_date": _sdw_prev_date if _SDW_AVAILABLE else None,
                    "max_conc": None, "max_holder": None, "max_inc": None, "max_dec": None}
    if _SDW_AVAILABLE and _sdw_holders_map:
        _uni_names = _universe_names
        _sc_max_pct = 0.0;  _sc_max_pct_code = None; _sc_max_pct_holder = None
        _sc_max_conc = 0.0; _sc_max_conc_code = None
        _sc_max_inc = None; _sc_max_inc_val = -999.0
        _sc_max_dec = None; _sc_max_dec_val =  999.0
        for _sc_code, _sc_holders in _sdw_holders_map.items():
            if not _sc_holders: continue
            _sc_total = _sdw_total_sh_map.get(_sc_code, 0)
            # Resolve name once per code — used by all four summary metrics below
            _sc_name = _uni_names.get(_sc_code, {}).get("zh") or _sc_code
            _sc_en   = _uni_names.get(_sc_code, {}).get("en") or ""
            # Skip non-equity instruments — ETFs/bonds/derivatives distort all four metrics
            if classify_stock(_sc_code, _sc_en) in ("etf", "bond", "derivative"): continue
            _sc_top_pct = _sc_holders[0].get("pct", 0.0) if _sc_holders else 0.0
            if _sc_top_pct > _sc_max_pct:
                _sc_max_pct = _sc_top_pct; _sc_max_pct_code = _sc_code
                _sc_max_pct_holder = _sc_holders[0].get("name", "")
            if _sc_total > 0:
                _sc_top5sh = sum(h.get("sh", 0) for h in _sc_holders[:5])
                _sc_conc = _sc_top5sh / _sc_total * 100
                if _sc_conc > _sc_max_conc:
                    _sc_max_conc = _sc_conc; _sc_max_conc_code = _sc_code
            _sc_prev = _sdw_prev_holders_map.get(_sc_code, [])
            if _sc_prev:
                _sc_now10  = sum(h.get("pct", 0) for h in _sc_holders[:10])
                _sc_prev10 = sum(h.get("pct", 0) for h in _sc_prev[:10])
                _sc_delta  = round(_sc_now10 - _sc_prev10, 2)
                if _sc_delta > _sc_max_inc_val:
                    _sc_max_inc_val = _sc_delta; _sc_max_inc = {"code": _sc_code, "name": _sc_name, "delta": _sc_delta}
                if _sc_delta < _sc_max_dec_val:
                    _sc_max_dec_val = _sc_delta; _sc_max_dec = {"code": _sc_code, "name": _sc_name, "delta": _sc_delta}
        if _sc_max_pct_code:
            _sc_name = _uni_names.get(_sc_max_pct_code, {}).get("zh") or _sc_max_pct_code
            _sdw_summary["max_holder"] = {"code": _sc_max_pct_code, "name": _sc_name,
                                           "holder": _sc_max_pct_holder, "pct": round(_sc_max_pct, 2)}
        if _sc_max_conc_code:
            _sc_name = _uni_names.get(_sc_max_conc_code, {}).get("zh") or _sc_max_conc_code
            _sdw_summary["max_conc"] = {"code": _sc_max_conc_code, "name": _sc_name, "conc": round(_sc_max_conc, 1)}
        if _sc_max_inc: _sdw_summary["max_inc"] = _sc_max_inc
        if _sc_max_dec: _sdw_summary["max_dec"] = _sc_max_dec

    output = {
        "update_time":      trading_day.strftime("%Y-%m-%d %H:%M"),
        "sb_date":          sb_date_used,
        "sb_summary":       get_sb_summary(sb_date_used),
        "northbound_flow":  northbound_flow,
        "ccass_holdings":   ccass_holdings,
        "sdw_summary":      _sdw_summary,
        # name_map removed — JS never reads it from data.json (saved separately as name_map.json)
        "stocks":           results,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
    log.info("data.json written: %d stocks", len(results))

    # ── 12. Telegram ──────────────────────────────────────────────────────
    if results:
        flagged     = [s for s in results if s["insight"]]
        new_entries = [s for s in results if s["rank_new"]]
        big_movers  = [s for s in results if not s["rank_new"] and s["rank_change"] >= 5]
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

    def _parse_date(s: str) -> datetime:
        return datetime.strptime(s, "%Y-%m-%d")

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
