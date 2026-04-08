import os, json, time, logging, re
import pandas as pd
import requests
import holidays
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from stock_ref import get_zh_name, get_industry, get_type, STOCKS
from ccass_library import (get_pct_history, get_sh_history,
                            save_day as ccass_save_day,
                            all_stored_dates as ccass_all_stored_dates)
from short_library import save_day as short_save_day, get_short_history, get_short_ratio_history
from turnover_library import (save_day as tv_save_day, get_tv_history,
                               get_vol_history, get_close_history, get_close,
                               load_recent as tv_load_recent, get_tv)
try:
    from sfc_library import get_short_position as sfc_get_position, all_report_fridays as sfc_fridays
    _SFC_AVAILABLE = True
except ImportError:
    _SFC_AVAILABLE = False
try:
    from ccass_sdw_library import get_latest_total_sh as sdw_get_total_sh
    _SDW_AVAILABLE = True
except ImportError:
    _SDW_AVAILABLE = False

from sc_top10_library import get_top10, get_top10_history

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
# CCASS southbound settles T+2 on days BOTH HK and mainland exchanges are open.
# Mainland holidays differ from HK — CNY, Golden Week etc. extend settlement.
# Hardcoded for accuracy; holidays.China() used as fallback for future years.
# Note: China also has make-up Saturdays (補班) — not modelled here as they
# are settlement days but the effect on T+2 is minimal for daily analysis.
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
    Correctly handles CNY, Golden Week, and other extended holidays.
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
    """
    Given a CCASS settlement date, return the actual trade date (T-2).
    Uses joint HK+CN calendar for accuracy across long holidays.
    """
    return business_days_back(settlement_date, 2)

# ── Shared helpers ────────────────────────────────────────────────────────────
def fmt_code(val) -> str:
    return str(val).strip().lstrip("0").zfill(5)

def to_num(s) -> float:
    try:    return float(str(s).replace(",", "").strip())
    except: return 0.0

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
    # stock_ref verified names always win — don't overwrite them with Big5 decode
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

# ── Source 1: Daily quotation ─────────────────────────────────────────────────
EMPTY_QUOTE = pd.DataFrame(columns=["stock_code", "name", "name_chi", "turnover", "shares"])

def get_daily_quotation(date: datetime = None) -> pd.DataFrame:
    """
    Parses HKEX c.htm (Big5, served as ISO-8859-1).
    Pattern A: CODE NAME CHI HKD TURNOVER SHARES HIGH LOW
    Pattern B: CODE NAME CHI HKD PRV BID CLOSE HIGH LOW CLOSE SHARES TURNOVER
    """
    date     = date or datetime.now()
    date_str = date.strftime("%y%m%d")
    url      = f"https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{date_str}c.htm"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()

        try:
            text    = resp.content.decode("big5", errors="replace")
            has_chi = any('\u4e00' <= c <= '\u9fff' for c in text[:5000])
            if not has_chi:
                raise ValueError
        except Exception:
            text = resp.content.decode("latin-1", errors="replace")
            log.warning("Daily quotation: latin-1 fallback for %s", date_str)

        pre  = BeautifulSoup(text, "html.parser").find("pre")
        body = pre.get_text() if pre else text

        # Pattern B: CODE NAME CHI CURR PRV BID ASK HIGH LOW CLOSE SHARES TURNOVER
        # Verified format of d{YYMMDD}c.htm — 6 price columns, then shares, then turnover.
        # group(1)=code  group(2)=eng  group(3)=chi  group(4)=shares  group(5)=turnover
        PAT = re.compile(
            r"^[\*\s]{0,5}(\d{1,5})\s+(\S[^\u3000\n]{1,22}?)\s{2,}"
            r"(.{1,30}?)\s*(?:HKD|USD|CNY|EUR|GBP)\s+"
            r"[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+"
            r"([\d,]{5,})\s+"          # group(4) = shares
            r"([\d,]{8,})\s*$"         # group(5) = HKD turnover
        )

        best     = {}
        name_map = {}

        for line in body.splitlines():
            m = PAT.match(line)
            if not m:
                continue
            code_int = int(m.group(1))
            if code_int > 9999:
                continue
            code     = str(code_int).zfill(5)
            name_eng = m.group(2).strip()
            name_chi = re.sub(r'[\u3000\uff20\uff64\s]+$', '', m.group(3)).strip()
            volume   = float(m.group(4).replace(',', ''))
            turnover = float(m.group(5).replace(',', ''))
            if not _is_valid_chinese(name_chi):
                name_chi = name_eng
            if turnover <= 0:
                continue
            if code not in best or turnover > best[code]["turnover"]:
                zh = get_zh_name(code) or (name_chi if _is_valid_chinese(name_chi) else name_eng)
                best[code] = {"stock_code": code, "name": name_eng,
                              "name_chi": zh, "turnover": turnover,
                              "shares": volume, "close": 0.0}
                if not get_zh_name(code):
                    name_map[code] = {"en": name_eng, "zh": zh}

        records = list(best.values())

        if not records:
            log.warning("Daily quotation: 0 records for %s", date_str)
            return EMPTY_QUOTE

        _update_name_map(name_map)
        df = pd.DataFrame(records)
        df = df[df["turnover"] > 0].sort_values("turnover", ascending=False).reset_index(drop=True)
        log.info("Daily quotation: %d records for %s (top: %s %s)",
                 len(df), date_str, df.iloc[0]["stock_code"], df.iloc[0]["name"])
        return df

    except requests.HTTPError as e:
        if e.response.status_code == 404:
            prev = last_trading_day(date - timedelta(days=1))
            if prev != date:
                log.warning("Daily quotation 404 for %s, trying %s", date_str, prev.strftime("%y%m%d"))
                return get_daily_quotation(prev)
        log.warning("Daily quotation not available for %s: %s", date_str, e)
        return EMPTY_QUOTE
    except Exception as e:
        log.error("get_daily_quotation failed (%s): %s", date_str, e)
        return EMPTY_QUOTE

# ── Source 2: Short selling ───────────────────────────────────────────────────
# Chinese version — 4 cols: 股票代號 名稱 沽空成交量 沽空成交額
SHORT_SELL_URL = "https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/ashtmain_c.htm"
EMPTY_SHORT    = pd.DataFrame(columns=["stock_code", "name", "short_volume", "short_turnover"])

# Pattern: CODE  CHI_NAME  VOLUME  TURNOVER
# The Chinese file is Big5-encoded; names contain CJK characters
_SS_PAT = re.compile(
    r"^\s{0,8}(\d{1,6})\s{1,4}(.+?)\s{2,}([\d,]+)\s+([\d,]+)\s*$"
)
_SS_SKIP = {"股票代號", "沽空成交量", "合計", "TOTAL", "CODE", "NAME OF STOCK"}

def get_short_sell_today() -> pd.DataFrame:
    try:
        resp = requests.get(SHORT_SELL_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        # Chinese file is Big5 encoded
        try:
            text = resp.content.decode("big5", errors="replace")
        except Exception:
            text = resp.content.decode("latin-1", errors="replace")
        rows = []
        for line in text.splitlines():
            m = _SS_PAT.match(line)
            if not m:
                continue
            code = fmt_code(m.group(1))
            name = m.group(2).strip()
            if not code or name in _SS_SKIP:
                continue
            sv = to_num(m.group(3))
            st = to_num(m.group(4))
            if sv <= 0 and st <= 0:
                continue
            rows.append({"stock_code":     code,
                         "name":           name,
                         "short_volume":   sv,
                         "short_turnover": st})
        df = pd.DataFrame(rows) if rows else EMPTY_SHORT
        log.info("Short sell today: %d records", len(df))
        return df
    except Exception as e:
        log.error("get_short_sell_today failed: %s", e)
        return EMPTY_SHORT

def save_short_sell(date: datetime, df: pd.DataFrame):
    if df.empty:
        return
    # Store all 4 columns: sv, st, and name
    records = {r.stock_code: {
        "sv":   int(r.short_volume),
        "st":   float(r.short_turnover),
        "name": r.name,
    } for r in df.itertuples()}
    short_save_day(date, records)
    log.info("Saved short sell: %s (%d)", date.strftime("%Y-%m-%d"), len(records))

def get_short_avg_ratio(stock_codes: list, days: int, daily_tv: dict,
                        before: str) -> pd.DataFrame:
    rows = []
    for c in stock_codes:
        v = get_short_ratio_history(c, days, before, daily_tv)
        rows.append({"stock_code": c,
                     "short_ratio_avg5": round(sum(v) / len(v), 2) if v else 0.0})
    return pd.DataFrame(rows)

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
    Compute CCASS metrics for each stock using pct_listed (% of issued shares)
    as the primary signal — comparable across all stocks regardless of share count.

    today_pct_map: {code: pct_listed} for today, from df_ccass.

    Fields returned:
      pct_listed    — today's % held in CCASS (from HKEX)
      pct_delta     — today pct minus yesterday pct (percentage points)
      ccass_consec  — consecutive days pct moved in same direction as today
                      (positive = accumulating streak, negative = distributing)
      ccass_streak_pct — cumulative pct change over the current streak
      ccass_delta   — raw share count change (kept for internal use)
    """
    if today_pct_map is None:
        today_pct_map = {}
    rows = []
    for code in stock_codes:
        today_sh  = today_map.get(code, 0)
        pct_today = today_pct_map.get(code, 0.0)

        # ── pct history: newest-first, up to 25 days back ─────────────────────
        pct_hist = get_pct_history(code, days, today_ds)   # [yesterday, day-2, ...]

        pct_prev  = pct_hist[0] if pct_hist else 0.0
        pct_delta = round(pct_today - pct_prev, 4) if pct_prev > 0 else 0.0

        # pct_deltas between consecutive historical days (newest-first)
        pct_deltas = [
            round(pct_hist[i] - pct_hist[i + 1], 4)
            for i in range(len(pct_hist) - 1)
            if pct_hist[i] > 0 and pct_hist[i + 1] > 0
        ]

        # consecutive days pct moved in same direction as today
        # flat days (delta == 0) are skipped — they don't break the streak
        # also accumulate the total pct move over the streak
        direction = 1 if pct_delta > 0 else (-1 if pct_delta < 0 else 0)
        consec = 0
        streak_pct = pct_delta   # start with today's move
        if direction != 0:
            for d in pct_deltas:          # walk back from yesterday
                if d == 0:
                    continue              # flat day — skip, keep streak alive
                if d * direction > 0:
                    consec += direction   # same direction, extend streak
                    streak_pct += d       # accumulate historical deltas
                else:
                    break                 # opposite direction, streak ends

        # raw share delta (still used by classify_insight thresholds)
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
    if df.empty:
        return
    tv_save_day(date, {r.stock_code: {
        "tv":    int(r.turnover),
        "vol":   int(r.shares),
        "close": float(r.close) if hasattr(r, "close") and r.close else 0.0,
    } for r in df.itertuples()})

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
    """Returns type from stock_ref, falls back to keyword matching."""
    t = get_type(code)
    if t:
        return t
    n = name.upper()
    ETF_CODES    = {"02800","02828","03033","03032","03188","02846","03140","03037","03011","02823"}
    STABLE_KW    = ("BANK","ENERGY","POWER","GAS","PETRO","SINOPEC","CNOOC","MTR","UTILITY")
    BLUECHIP_KW  = ("TENCENT","MEITUAN","ALIBABA","BABA","XIAOMI","HSBC","AIA","PING AN",
                    "HKEX","CK ","HENDERSON","SHK","SWIRE","GALAXY","SANDS","MELCO")
    if code in ETF_CODES:                               return "etf"
    if any(k in n for k in STABLE_KW):                 return "stable"
    if any(k in n for k in BLUECHIP_KW):               return "bluechip"
    return "general"

THRESHOLDS = {
    #              lo    hi  spike  cover_drop
    # cover_drop: today must be below (avg × cover_drop) to fire 空頭平倉
    # 0.60 = ratio only needs to drop 40% from avg (e.g. 20% → 12%)
    "etf":      (40.0, 70.0, 15.0, 0.60),
    "stable":   ( 5.0, 10.0, 15.0, 0.60),
    "bluechip": (10.0, 20.0, 10.0, 0.60),
    "general":  (10.0, 25.0, 15.0, 0.60),
}

def classify_insight(code, stock_type, short_ratio, short_avg5, short_ratio_t2,
                     turnover, tv_avg5, turnover_t2,
                     ccass_delta, ccass_consec,
                     pct_delta=0.0,
                     days_to_cover=0.0, vol_ratio=0.0,
                     tv_ratio30=0.0, pct_dev30=0.0,
                     sb_net=0) -> str | None:
    """
    Returns one primary signal string, or None.
    Signals 4 (北水流出) and 5 (異常高沽空) are independent — both checked,
    combined with | if both fire since they track different phenomena.
    """
    lo, hi, spike_warn, cover_drop = THRESHOLDS.get(stock_type, THRESHOLDS["general"])
    r_today = turnover / tv_avg5 if tv_avg5 > 0 else 1.0   # used in 空頭平倉

    # ── 挾倉風險 — short squeeze danger (highest priority) ────────────────────
    if days_to_cover > 5 and vol_ratio > 2:                      return "🔥 挾倉風險"

    # ── 異常亢奮 — abnormal excitement ────────────────────────────────────────
    if (vol_ratio  >  2.5
            and tv_ratio30 >  2.0
            and pct_dev30  >= 0.5):                               return "🐉 異常亢奮"

    # ── 北水增持 — quiet northbound accumulation ──────────────────────────────
    if (1.8 <= vol_ratio  <= 2.5
            and 1.5 <= tv_ratio30 <= 2.0
            and 0.2 <= pct_dev30  <= 0.5):                        return "🏦 北水增持"

    # ── 北水流出 + 異常高沽空 — independent signals, can both fire ───────────
    flow_out  = sb_net < 0 and pct_delta < 0
    high_short = short_ratio > hi + spike_warn
    if flow_out and high_short:   return "🚨 北水流出｜異常高沽空"
    if flow_out:                  return "🚨 北水流出"
    if high_short:                return "🚨 異常高沽空"

    # ── 空頭平倉 ──────────────────────────────────────────────────────────────
    if (short_avg5 > lo and short_ratio < short_avg5 * cover_drop
            and r_today > 1.30):                                  return "📉 空頭平倉"
    return None

# ── Bootstrap ─────────────────────────────────────────────────────────────────
def bootstrap_history(days: int = 10):
    from turnover_library import all_stored_dates as tv_all_stored
    existing_tv    = tv_all_stored()
    existing_ccass = ccass_all_stored_dates()

    target, dates_to_fetch, checked = last_trading_day(datetime.now() - timedelta(days=1)), [], 0
    while len(dates_to_fetch) < days and checked < 30:
        dates_to_fetch.append(target)
        target  = last_trading_day(target - timedelta(days=1))
        checked += 1

    needed = [d for d in dates_to_fetch
              if d.strftime("%Y-%m-%d") not in existing_tv
              or d.strftime("%Y-%m-%d") not in existing_ccass]
    if not needed:
        return
    log.info("Bootstrap: %d dates to fetch", len(needed))

    for d in needed:
        key = d.strftime("%Y%m%d")
        if d.strftime("%Y-%m-%d") not in existing_tv:
            df_q = get_daily_quotation(d)
            if not df_q.empty:
                save_daily_turnover(d, df_q)
                log.info("Bootstrap quotation: %s (%d)", key, len(df_q))
            time.sleep(1)
        if d.strftime("%Y-%m-%d") not in existing_ccass:
            df_c = get_ccass_southbound(d)
            if not df_c.empty:
                ccass_save_day(d, {r.stock_code: {
                    "sh":   r.shareholding,
                    "pct":  r.pct_listed,
                    "name": r.name,
                } for r in df_c.itertuples()})
                log.info("Bootstrap CCASS: %s (%d)", key, len(df_c))
            time.sleep(1)

# ── Main analysis ─────────────────────────────────────────────────────────────
def run_analysis():
    today       = datetime.now()
    trading_day = last_trading_day(today)
    log.info("=== analysis — trading day: %s ===", trading_day.strftime("%Y-%m-%d"))
    today_ds = trading_day.strftime("%Y-%m-%d")

    _seed_name_map_from_ref()
    bootstrap_history(days=25)

    # 1. Daily quotation
    df_quote = get_daily_quotation(trading_day)
    if df_quote.empty:
        # HKEX file not yet published — rebuild from local turnover cache so the
        # analysis can still run with yesterday's rankings instead of hard-aborting.
        log.warning("Daily quotation unavailable for %s — attempting cache fallback", today_ds)
        _fallback_tv = tv_load_recent(1, today_ds)
        if _fallback_tv:
            _fallback_ds  = max(_fallback_tv.keys())
            _fallback_day = _fallback_tv[_fallback_ds]
            _nm           = load_store(NAME_MAP_FILE)
            _fb_rows      = []
            for _code, _vals in _fallback_day.items():
                if isinstance(_vals, dict) and _vals.get("tv", 0) > 0:
                    _nm_entry = _nm.get(_code, {})
                    _fb_rows.append({
                        "stock_code": _code,
                        "name":       _nm_entry.get("en", _code),
                        "name_chi":   _nm_entry.get("zh", _code),
                        "turnover":   _vals["tv"],
                        "shares":     _vals.get("vol", 0),
                        "close":      _vals.get("close", 0.0),
                    })
            if _fb_rows:
                df_quote = (pd.DataFrame(_fb_rows)
                              .sort_values("turnover", ascending=False)
                              .reset_index(drop=True))
                log.warning("Cache fallback: using %s data (%d stocks)", _fallback_ds, len(df_quote))
                send_telegram(
                    f"⚠️ 港股看板：{today_ds} 日報表未能獲取，"
                    f"以 {_fallback_ds} 緩存數據繼續分析（排名僅供參考）。"
                )
        if df_quote.empty:
            msg = "⚠️ 港股看板：今日日報表未能獲取，且無緩存數據，分析中止。"
            log.error(msg); send_telegram(msg); return

    save_daily_turnover(trading_day, df_quote)
    stock_codes  = df_quote["stock_code"].tolist()
    turnover_map = dict(zip(df_quote["stock_code"], df_quote["turnover"]))
    # Traded shares (成交股數) from daily quotation — used as short ratio denominator
    vol_map      = dict(zip(df_quote["stock_code"], df_quote["shares"]))

    # 2. Short selling
    df_short  = get_short_sell_today()
    save_short_sell(trading_day, df_short)
    short_map     = {}
    short_vol_map = {}
    for row in df_short.itertuples():
        traded_vol = vol_map.get(row.stock_code, 0)
        if traded_vol > 0:
            # short_ratio = 沽空股數 / 成交股數 * 100  (shares ÷ shares)
            short_map[row.stock_code] = round(row.short_volume / traded_vol * 100, 2)
        short_vol_map[row.stock_code] = int(row.short_volume)

    # 3. Short avg
    _tv_recent    = tv_load_recent(15, today_ds)
    _sa_df        = get_short_avg_ratio(stock_codes, 10, _tv_recent, today_ds)
    short_avg_map = dict(zip(_sa_df["stock_code"], _sa_df["short_ratio_avg5"]))  # avg over 10 days despite "avg5" name in library

    # T-2: the actual trade date that today's CCASS settlement reflects
    # Uses joint HK+CN calendar so long holidays are correctly handled
    t2_date           = ccass_trade_date(trading_day)
    t2_key            = t2_date.strftime("%Y%m%d")
    log.info("T-2 trade date (CCASS settlement): %s", t2_key)

    # 4. CCASS
    df_ccass = get_ccass_southbound(trading_day)
    if df_ccass.empty:
        prev_td  = last_trading_day(trading_day - timedelta(days=1))
        log.info("CCASS empty for %s, trying %s", trading_day.strftime("%Y-%m-%d"), prev_td.strftime("%Y-%m-%d"))
        df_ccass = get_ccass_southbound(prev_td)

    ccass_sh_map  = {}
    ccass_pct_map = {}
    if not df_ccass.empty:
        ccass_sh_map  = dict(zip(df_ccass["stock_code"], df_ccass["shareholding"]))
        ccass_pct_map = dict(zip(df_ccass["stock_code"], df_ccass["pct_listed"]))

    df_cs         = get_ccass_delta_and_avg(stock_codes, ccass_sh_map, today_ds,
                                            today_pct_map=ccass_pct_map)
    # Save today's CCASS into the year-split library
    if not df_ccass.empty:
        ccass_save_day(trading_day, {r.stock_code: {
            "sh":   r.shareholding,
            "pct":  r.pct_listed,
            "name": r.name,
        } for r in df_ccass.itertuples()})
    ccass_delta_map     = dict(zip(df_cs["stock_code"], df_cs["ccass_delta"]))
    ccass_consec_map    = dict(zip(df_cs["stock_code"], df_cs["ccass_consec"]))
    ccass_streak_pct_map= dict(zip(df_cs["stock_code"], df_cs["ccass_streak_pct"]))
    pct_listed_map      = dict(zip(df_cs["stock_code"], df_cs["pct_listed"]))
    pct_delta_map       = dict(zip(df_cs["stock_code"], df_cs["pct_delta"]))

    # 4b. SFC cumulative short position → sfc_pct per stock
    # sfc_pct = SFC reportable short shares / SDW 總數 (total CCASS-settled shares) * 100
    # Uses the most recent SFC Friday report on or before today.
    sfc_map = {}   # code -> {"sfc_sh": N, "sfc_hkd": N, "sfc_pct": float}
    if _SFC_AVAILABLE and _SDW_AVAILABLE:
        try:
            from datetime import date as _date2
            _sfc_fridays = [d for d in sfc_fridays() if d <= trading_day.date()]
            if _sfc_fridays:
                _latest_sfc_ds = max(_sfc_fridays).isoformat()
                for code in stock_codes:
                    pos = sfc_get_position(code, _latest_sfc_ds)
                    if not pos or pos.get("sh", 0) <= 0:
                        continue
                    sfc_sh    = pos["sh"]
                    sfc_hkd   = pos.get("hkd", 0.0)
                    total_sh  = sdw_get_total_sh(code, today_ds)
                    sfc_pct   = round(sfc_sh / total_sh * 100, 4) if total_sh > 0 else 0.0
                    sfc_map[code] = {"sfc_sh": sfc_sh, "sfc_hkd": sfc_hkd, "sfc_pct": sfc_pct}
                log.info("SFC short positions: %d stocks from %s", len(sfc_map), _latest_sfc_ds)
        except Exception as e:
            log.warning("SFC map build failed: %s", e)

    # 5. Southbound top10 (from sc_top10_library)
    # Try library first; if today not stored yet, do a live fetch directly from HKEX.
    # HKEX publishes SC data same-day (usually by 18:00–20:00 HKT).
    from sc_top10_library import fetch_day as sc_fetch_day, save_year as sc_save_year, load_year as sc_load_year, lib_path as sc_lib_path
    from datetime import date as _date

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

    sb_map = {}   # code -> {buy, sell, net, rank, total} in HKD
    sb_date_used = today_ds

    # Minimum stocks to consider sc_top10 data valid.
    _MIN_SB = 5

    # 1. Try library (already stored from previous run or --reparse)
    sb_map = _build_sb_map(get_top10(today_ds))
    if sb_map and len(sb_map) < _MIN_SB:
        log.warning("Southbound top10: library has only %d stocks for %s — discarding, will re-fetch",
                    len(sb_map), today_ds)
        sb_map = {}

    # 2. If not in library, try live fetch from HKEX and store immediately
    if not sb_map:
        log.info("Southbound top10: not in library for %s — attempting live fetch", today_ds)
        live_rec = sc_fetch_day(trading_day.date() if hasattr(trading_day, 'date') else _date.fromisoformat(today_ds))
        live_count = len(live_rec.get("top10", [])) if live_rec else 0
        if live_rec and live_count >= _MIN_SB:
            year = trading_day.year
            lib  = sc_load_year(year)
            lib["by_date"][today_ds] = live_rec
            sc_save_year(year, lib)
            sb_map = _build_sb_map(live_rec.get("top10", []))
            log.info("Southbound top10: live fetch succeeded — %d stocks for %s", len(sb_map), today_ds)
        else:
            log.info("Southbound top10: live fetch returned %d stocks for %s (< %d, not saved)",
                     live_count, today_ds, _MIN_SB)

    # 3. Fall back to most recent available day if still empty
    if not sb_map:
        prev_td = last_trading_day(trading_day - timedelta(days=1))
        prev_ds = prev_td.strftime("%Y-%m-%d")
        sb_map  = _build_sb_map(get_top10(prev_ds))
        if sb_map:
            sb_date_used = prev_ds
            log.info("Southbound top10: using previous day %s (%d stocks)", prev_ds, len(sb_map))
        else:
            log.warning("Southbound top10: no data for today or yesterday")

    log.info("Southbound top10: %d stocks for %s", len(sb_map), sb_date_used)

    # 5a. Compute sb_consec and sb_net_prev for each stock in sb_map
    # Logic: look back through top10 history; count consecutive days with net > 0
    # (buy > sell). A day the stock is absent = fell off table = streak reset.
    # Also grab previous day's net for ranking-change comparison.
    def _sb_consec_and_prev(code: str) -> tuple[int, int]:
        """Returns (consecutive_net_buy_days_including_today, prev_day_net_flow).

        Streak logic:
        - today always counts as day 1 if it's in sb_map (it is, by definition)
        - walk back through history; extend streak on net-buy days
        - skip net-zero days (buy == sell) — consistent with CCASS consec
        - break on net-sell days
        """
        history = get_top10_history(code, 30, today_ds)
        # history is reverse-chrono: [yesterday, day-before, ...]
        prev_net = (history[0]["buy"] - history[0]["sell"]) if history else 0

        today_net = sb_map[code]["sb_net"]   # today is always in sb_map here
        if today_net < 0:
            # today is net-sell — count how many consecutive sell days
            consec = -1
            for entry in history:
                net = entry["buy"] - entry["sell"]
                if net < 0:
                    consec -= 1
                elif net == 0:
                    continue
                else:
                    break
            return consec, prev_net
        if today_net == 0:
            return 0, prev_net

        # today is net-buy → start streak at 1, extend with prior net-buy days
        consec = 1
        for entry in history:
            net = entry["buy"] - entry["sell"]
            if net > 0:
                consec += 1       # same direction, extend
            elif net == 0:
                continue          # flat day — skip, don't break streak
            else:
                break             # net-sell — streak ends
        return consec, prev_net

    sb_consec_map  = {}
    sb_prev_map    = {}
    for code in sb_map:
        consec, prev = _sb_consec_and_prev(code)
        sb_consec_map[code] = consec
        sb_prev_map[code]   = prev

    # 6. Previous ranks
    prev_ranks = get_prev_ranks(exclude_date=trading_day)

    # 7. Build results
    results = []
    for i, row in enumerate(df_quote.itertuples(), 1):
        code         = row.stock_code
        short_ratio  = short_map.get(code, 0.0)
        short_avg5   = short_avg_map.get(code, 0.0)
        ccass_delta      = ccass_delta_map.get(code, 0)
        ccass_consec     = ccass_consec_map.get(code, 0)
        ccass_streak_pct = ccass_streak_pct_map.get(code, 0.0)
        pct_listed       = pct_listed_map.get(code, 0.0)
        pct_delta        = pct_delta_map.get(code, 0.0)
        tv_avg5          = _turnover_avg(code, today_ds, 5)

        # ── 挾倉風險: short volume vs avg 30-day share volume ──────────────────
        short_vol_today = short_vol_map.get(code, 0)
        vol_hist30      = get_vol_history(code, 30, today_ds)
        avg_vol30       = sum(vol_hist30) / len(vol_hist30) if vol_hist30 else 0
        today_vol       = int(row.shares)
        days_to_cover   = round(short_vol_today / avg_vol30, 2) if avg_vol30 > 0 else 0.0
        vol_ratio       = round(today_vol / avg_vol30, 2)       if avg_vol30 > 0 else 0.0

        # ── 30-day averages for 機構增持 ────────────────────────────────────────
        tv_hist30  = get_tv_history(code, 30, today_ds)
        tv_avg30   = sum(tv_hist30) / len(tv_hist30) if tv_hist30 else 0.0
        tv_ratio30 = round(float(row.turnover) / tv_avg30, 2) if tv_avg30 > 0 else 0.0
        # pct_avg30: 30-day average pct_listed level (not delta)
        pct_hist30 = get_pct_history(code, 30, today_ds)
        pct_avg30_lvl = round(sum(pct_hist30) / len(pct_hist30), 4) if pct_hist30 else 0.0
        # deviation of today's pct from 30-day avg (percentage points)
        pct_dev30  = round(pct_listed - pct_avg30_lvl, 4) if pct_avg30_lvl > 0 else 0.0

        tv_t2_raw      = get_tv(code, t2_key)
        _t2_short      = get_short_history(code, 1, t2_date.strftime("%Y-%m-%d") + "z")
        # T-2 short ratio: short volume (shares) / traded volume (shares)
        short_sv_t2    = _t2_short[0]["sv"] if _t2_short else 0
        vol_t2_raw     = _tv_recent.get(t2_key, {}).get(code, {})
        vol_t2         = vol_t2_raw.get("vol", 0) if isinstance(vol_t2_raw, dict) else 0
        short_ratio_t2 = round(short_sv_t2 / vol_t2 * 100, 2) if vol_t2 > 0 else short_ratio
        turnover_t2    = tv_t2_raw if tv_t2_raw > 0 else float(row.turnover)

        stock_type       = classify_stock(code, row.name)
        _, ind_zh        = get_industry(code)

        sb           = sb_map.get(code, {})   # must be before classify_insight
        # Suppress ratio-based signals if history is too thin (< 5 days) to avoid
        # false positives on new stocks where 1-2 day averages are not meaningful
        has_history  = len(tv_hist30) >= 5 and len(vol_hist30) >= 5
        insight = classify_insight(
            code, stock_type, short_ratio, short_avg5, short_ratio_t2,
            int(row.turnover), tv_avg5, turnover_t2,
            int(ccass_delta), int(ccass_consec),
            pct_delta=pct_delta,
            days_to_cover=days_to_cover if has_history else 0.0,
            vol_ratio=vol_ratio         if has_history else 0.0,
            tv_ratio30=tv_ratio30       if has_history else 0.0,
            pct_dev30=pct_dev30         if has_history else 0.0,
            sb_net=sb.get("sb_net", 0)
        )

        prev_rank   = prev_ranks.get(code)
        rank_new    = prev_rank is None
        rank_change = 0 if rank_new else prev_rank - i
        results.append({
            "rank": i, "rank_change": rank_change, "rank_new": rank_new,
            "code": code, "name": row.name, "name_chi": getattr(row, "name_chi", row.name),
            "stock_type": stock_type, "industry_zh": ind_zh,
            "turnover": int(row.turnover),
            "sb_buy":   sb.get("sb_buy",   0),
            "sb_sell":  sb.get("sb_sell",  0),
            "sb_net":   sb.get("sb_net",   0),
            "sb_total": sb.get("sb_total", 0),
            "sb_net_prev": int(sb_prev_map.get(code, 0)),
            "sb_consec":   int(sb_consec_map.get(code, 0)),
            "short_ratio": round(short_ratio, 2), "short_avg5": round(short_avg5, 2),
            "short_ratio_t2": round(short_ratio_t2, 2),
            "short_vol":      int(short_vol_today),
            "days_to_cover":  days_to_cover,
            "vol_ratio":      vol_ratio,
            "sfc_sh":   sfc_map.get(code, {}).get("sfc_sh",  0),
            "sfc_hkd":  sfc_map.get(code, {}).get("sfc_hkd", 0.0),
            "sfc_pct":  sfc_map.get(code, {}).get("sfc_pct", 0.0),
            "tv_ratio30":     tv_ratio30,
            "pct_dev30":      round(pct_dev30, 4),
            "ccass_trade_date": t2_date.strftime("%Y-%m-%d"),
            "ccass_delta":       int(ccass_delta),
            "ccass_consec":      int(ccass_consec),
            "ccass_streak_pct":  round(ccass_streak_pct, 4),
            "pct_listed": round(pct_listed, 4),
            "pct_delta":  round(pct_delta,  4),
            "insight": insight,
        })

    # 7. Persist
    output = {"update_time": trading_day.strftime("%Y-%m-%d %H:%M"),
              "sb_date": sb_date_used,
              "name_map": load_store(NAME_MAP_FILE), "stocks": results}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
    log.info("data.json written: %d stocks", len(results))
    save_rank_history(trading_day, results)

    # 8. Telegram
    if results:
        flagged     = [s for s in results if s["insight"]]
        new_entries = [s for s in results if s["rank_new"]]
        big_movers  = [s for s in results if not s["rank_new"] and s["rank_change"] >= 5]
        top         = results[0]
        top_rc      = f" [↑{top['rank_change']}]" if top['rank_change'] > 0 else (" [new]" if top['rank_new'] else "")
        lines = [
            "📊 港股策略板",
            f"時間: {output['update_time']}",
            f"榜首: {top['name_chi']} ({top['code']}){top_rc} 成交額 {top['turnover']:,}",
            f"異動股: {len(flagged)} 隻 | 新進榜: {len(new_entries)} 隻",
        ]
        if new_entries:
            lines.append("⭐ 新進: " + "、".join(f"{s['name_chi']}({s['code']})" for s in new_entries[:3]))
        if big_movers:
            lines.append("🔺 大升: " + "、".join(f"{s['name_chi']} ↑{s['rank_change']}" for s in big_movers[:3]))
        if flagged:
            lines.append("─────────────")
            for s in flagged[:5]:
                rc = f" [↑{s['rank_change']}]" if s['rank_change'] > 0 else (" [new]" if s['rank_new'] else "")
                lines.append(f"{s['insight']} {s['name_chi']}({s['code']}){rc} | 沽空率 {s['short_ratio']}% | CCASS {'+' if s['pct_delta']>=0 else ''}{s['pct_delta']}pp")
        send_telegram("\n".join(lines))


if __name__ == "__main__":
    run_analysis()
