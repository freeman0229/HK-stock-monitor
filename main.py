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
EMPTY_QUOTE = pd.DataFrame(columns=["stock_code", "name", "name_chi",
                                     "turnover", "shares", "high", "low", "close"])

def get_daily_quotation(date: datetime = None) -> pd.DataFrame:
    """
    Parses HKEX d{YYMMDD}c.htm (Big5, served as ISO-8859-1).

    Pattern B — 12 columns:
      CODE  NAME_ENG  CHI_NAME  CURR  PRV  BID  ASK  HIGH  LOW  CLOSE  SHARES  TURNOVER
      g1    g2        g3              skip×3       g4   g5   g6   g7     g8
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
        PAT = re.compile(
            r'^[\*\s]{0,5}(\d{1,5})\s+'              # g1: code
            r'(\S[^\u3000\n]{1,22}?)\s{2,}'          # g2: English name
            r'(.{1,30}?)\s*'                          # g3: Chinese name
            r'(?:HKD|USD|CNY|EUR|GBP)\s+'            # currency (skip)
            r'[\d,.NA-]+\s+'                          # PRV   (skip)
            r'[\d,.NA-]+\s+'                          # BID   (skip)
            r'[\d,.NA-]+\s+'                          # ASK   (skip)
            r'([\d,.NA-]+)\s+'                        # g4: HIGH
            r'([\d,.NA-]+)\s+'                        # g5: LOW
            r'([\d,.NA-]+)\s+'                        # g6: CLOSE
            r'([\d,]{5,})\s+'                         # g7: shares (volume)
            r'([\d,]{8,})\s*$'                        # g8: HKD turnover
        )

        def _price(s: str) -> float:
            s = s.replace(",", "").strip()
            return float(s) if s not in ("NA", "--", "", "N/A") else 0.0

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
            high     = _price(m.group(4))
            low      = _price(m.group(5))
            close    = _price(m.group(6))
            volume   = float(m.group(7).replace(',', ''))
            turnover = float(m.group(8).replace(',', ''))

            if not _is_valid_chinese(name_chi):
                name_chi = name_eng
            if turnover <= 0:
                continue
            # Keep highest-turnover record if code appears twice
            if code not in best or turnover > best[code]["turnover"]:
                zh = get_zh_name(code) or (name_chi if _is_valid_chinese(name_chi) else name_eng)
                best[code] = {
                    "stock_code": code,
                    "name":       name_eng,
                    "name_chi":   zh,
                    "turnover":   turnover,
                    "shares":     volume,
                    "high":       high,
                    "low":        low,
                    "close":      close,
                }
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
                log.warning("Daily quotation 404 for %s, trying %s",
                            date_str, prev.strftime("%y%m%d"))
                return get_daily_quotation(prev)
        log.warning("Daily quotation not available for %s: %s", date_str, e)
        return EMPTY_QUOTE
    except Exception as e:
        log.error("get_daily_quotation failed (%s): %s", date_str, e)
        return EMPTY_QUOTE

# ── Source 2: Short selling ───────────────────────────────────────────────────
# URL priority: modern page (table HTML) → legacy static file (Big5 pre-text)
_SS_URL_MODERN = (
    "https://www.hkex.com.hk/Market-Data/Statistics/Securities-Market/"
    "Short-Selling-Turnover-Today?sc_lang=zh-HK"
)
_SS_URL_LEGACY = (
    "https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/ashtmain_c.htm"
)
EMPTY_SHORT = pd.DataFrame(columns=["stock_code", "name", "short_volume", "short_turnover"])

# Pre-formatted text pattern: CODE  CHI_NAME  SHARES  AMOUNT
_SS_PRE_PAT  = re.compile(
    r'^\s{0,8}(\d{1,6})\s{1,4}'
    r'(.+?)\s{2,}'
    r'([\d,]+)\s+'
    r'([\d,]+)\s*$'
)
_SS_SKIP = frozenset({
    "股票代號","沽空成交量","合計","TOTAL","CODE","NAME OF STOCK","代號","股票名稱"
})

def _is_valid_cjk_name(s: str) -> bool:
    return bool(s) and any('\u4e00' <= c <= '\u9fff' for c in s)

def _parse_ss_table(raw: bytes) -> list[dict]:
    """Parse the modern HKEX HTML table (UTF-8). Returns list of row dicts."""
    text = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    # Find table containing 代號/CODE and 股數/VOLUME
    target = None
    for table in soup.find_all("table"):
        tc = table.get_text()
        if ("代號" in tc or "CODE" in tc) and ("股數" in tc or "VOLUME" in tc):
            target = table
            break
    if not target:
        return []
    # Detect column positions from header row
    col_code = col_name = col_sv = col_st = None
    header_row = target.find("tr")
    if header_row:
        for i, cell in enumerate(header_row.find_all(["th", "td"])):
            h = cell.get_text(strip=True)
            if "代號" in h or h.upper() in ("CODE", "STOCK CODE"):
                col_code = i
            elif "名稱" in h or "NAME" in h.upper():
                col_name = i
            elif "股數" in h or "VOLUME" in h.upper() or "SHARES" in h.upper():
                col_sv = i
            elif "金額" in h or "AMOUNT" in h.upper() or "TURNOVER" in h.upper():
                col_st = i
    if col_code is None: col_code = 0
    if col_name is None: col_name = 1
    if col_sv   is None: col_sv   = 2
    if col_st   is None: col_st   = 3
    rows = []
    seen = set()
    for tr in target.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) <= max(col_code, col_name, col_sv, col_st):
            continue
        raw_code = cells[col_code].strip().lstrip("0")
        if not raw_code.isdigit():
            continue
        code_int = int(raw_code)
        if code_int < 1 or code_int > 9999:
            continue
        code = str(code_int).zfill(5)
        if code in seen:
            continue
        seen.add(code)
        name = cells[col_name].strip()
        try:
            sv = float(cells[col_sv].replace(",", ""))
            st = float(cells[col_st].replace(",", ""))
        except (ValueError, AttributeError):
            continue
        if sv <= 0 and st <= 0:
            continue
        rows.append({"stock_code": code,
                     "name":         name if _is_valid_cjk_name(name) else "",
                     "short_volume": sv,
                     "short_turnover": st})
    return rows

def _parse_ss_pre(raw: bytes, encoding: str) -> list[dict]:
    """Parse Big5/latin-1 <pre>-formatted short sell file."""
    try:
        text = raw.decode(encoding, errors="replace")
    except Exception:
        text = raw.decode("latin-1", errors="replace")
    pre  = BeautifulSoup(text, "html.parser").find("pre")
    body = pre.get_text() if pre else text
    rows = []
    seen = set()
    for line in body.splitlines():
        m = _SS_PRE_PAT.match(line)
        if not m:
            continue
        code_int = int(m.group(1))
        if code_int < 1 or code_int > 9999:
            continue
        code = str(code_int).zfill(5)
        name = m.group(2).strip()
        if name in _SS_SKIP or not name or code in seen:
            continue
        seen.add(code)
        sv = to_num(m.group(3))
        st = to_num(m.group(4))
        if sv <= 0 and st <= 0:
            continue
        rows.append({"stock_code": code, "name": name,
                     "short_volume": sv, "short_turnover": st})
    return rows

def get_short_sell_today() -> pd.DataFrame:
    """
    Fetch today's short selling data.
    Priority: modern HKEX page (HTML table) → legacy Big5 static file.
    Returns DataFrame with columns: stock_code, name, short_volume, short_turnover.
    """
    # ── Try modern page (HTML table, UTF-8) ───────────────────────────────────
    try:
        r = requests.get(_SS_URL_MODERN, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.content) > 2000:
            text = r.content.decode("utf-8", errors="replace")
            if "代號" in text or "股票名稱" in text or (
                    "Short" in text and "<table" in text.lower()):
                rows = _parse_ss_table(r.content)
                if len(rows) >= 10:
                    log.info("Short sell today: %d records (modern page)", len(rows))
                    return pd.DataFrame(rows)
                log.debug("Modern page table had only %d rows — falling back", len(rows))
    except Exception as e:
        log.debug("Modern short sell URL failed: %s", e)

    # ── Fall back to legacy static file (Big5) ────────────────────────────────
    try:
        r = requests.get(_SS_URL_LEGACY, headers=HEADERS, timeout=30)
        r.raise_for_status()
        rows = _parse_ss_pre(r.content, "big5")
        df   = pd.DataFrame(rows) if rows else EMPTY_SHORT
        log.info("Short sell today: %d records (legacy file)", len(df))
        return df
    except Exception as e:
        log.error("get_short_sell_today failed: %s", e)
        return EMPTY_SHORT

def save_short_sell(date: datetime, df: pd.DataFrame):
    if df.empty:
        return
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
                     "short_avg5": round(sum(v) / len(v), 2) if v else 0.0})
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
    if df.empty:
        return
    tv_save_day(date, {r.stock_code: {
        "tv":    int(r.turnover),
        "vol":   int(r.shares),
        "high":  float(r.high)  if hasattr(r, "high")  and r.high  else 0.0,
        "low":   float(r.low)   if hasattr(r, "low")   and r.low   else 0.0,
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

def classify_insight(code, stock_type, short_ratio, short_avg5, short_ratio_t2,
                     turnover, tv_avg5, turnover_t2,
                     ccass_delta, ccass_consec,
                     pct_delta=0.0,
                     days_to_cover=0.0, vol_ratio=0.0,
                     tv_ratio30=0.0, pct_dev30=0.0,
                     sb_net=0) -> str | None:
    lo, hi, spike_warn, cover_drop = THRESHOLDS.get(stock_type, THRESHOLDS["general"])
    r_today = turnover / tv_avg5 if tv_avg5 > 0 else 1.0

    if days_to_cover > 5 and vol_ratio > 2:                      return "🔥 挾倉風險"
    if (vol_ratio  >  2.5
            and tv_ratio30 >  2.0
            and pct_dev30  >= 0.5):                               return "🐉 異常亢奮"
    if (1.8 <= vol_ratio  <= 2.5
            and 1.5 <= tv_ratio30 <= 2.0
            and 0.2 <= pct_dev30  <= 0.5):                        return "🏦 北水增持"

    flow_out   = sb_net < 0 and pct_delta < 0
    high_short = short_ratio > hi + spike_warn
    if flow_out and high_short:   return "🚨 北水流出｜異常高沽空"
    if flow_out:                  return "🚨 北水流出"
    if high_short:                return "🚨 異常高沽空"

    if (short_avg5 > lo and short_ratio < short_avg5 * cover_drop
            and r_today > 1.30):                                  return "📉 空頭平倉"
    return None

# ── Main analysis ─────────────────────────────────────────────────────────────
def run_analysis():
    today       = datetime.now()
    trading_day = last_trading_day(today)
    log.info("=== analysis — trading day: %s ===", trading_day.strftime("%Y-%m-%d"))
    today_ds = trading_day.strftime("%Y-%m-%d")

    _seed_name_map_from_ref()

    # 1. Daily quotation
    df_quote = get_daily_quotation(trading_day)
    if df_quote.empty:
        # HKEX file not yet published — rebuild from turnover cache
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
                        "high":       _vals.get("high",  0.0),
                        "low":        _vals.get("low",   0.0),
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
    vol_map      = dict(zip(df_quote["stock_code"], df_quote["shares"]))

    # 2. Short selling
    df_short  = get_short_sell_today()
    save_short_sell(trading_day, df_short)
    short_map     = {}
    short_vol_map = {}
    for row in df_short.itertuples():
        traded_vol = vol_map.get(row.stock_code, 0)
        if traded_vol > 0:
            short_map[row.stock_code] = round(row.short_volume / traded_vol * 100, 2)
        short_vol_map[row.stock_code] = int(row.short_volume)

    # 3. Short average (35-day window to cover 10-day avg lookback with gaps)
    _tv_recent    = tv_load_recent(35, today_ds)
    _sa_df        = get_short_avg_ratio(stock_codes, 10, _tv_recent, today_ds)
    short_avg_map = dict(zip(_sa_df["stock_code"], _sa_df["short_avg5"]))

    # T-2: actual trade date that today's CCASS settlement reflects
    t2_date = ccass_trade_date(trading_day)
    t2_key  = t2_date.strftime("%Y%m%d")
    log.info("T-2 trade date (CCASS settlement): %s", t2_key)

    # 4. CCASS — fetch today; fall back to previous trading day if empty.
    #    Track which date was actually fetched to avoid writing stale data
    #    under today's key (Bug 3 fix).
    df_ccass   = get_ccass_southbound(trading_day)
    ccass_save_date = trading_day
    if df_ccass.empty:
        prev_td  = last_trading_day(trading_day - timedelta(days=1))
        log.info("CCASS empty for %s, trying %s",
                 trading_day.strftime("%Y-%m-%d"), prev_td.strftime("%Y-%m-%d"))
        df_ccass = get_ccass_southbound(prev_td)
        ccass_save_date = prev_td   # save under the date we actually fetched

    ccass_sh_map  = {}
    ccass_pct_map = {}
    if not df_ccass.empty:
        ccass_sh_map  = dict(zip(df_ccass["stock_code"], df_ccass["shareholding"]))
        ccass_pct_map = dict(zip(df_ccass["stock_code"], df_ccass["pct_listed"]))

    df_cs = get_ccass_delta_and_avg(stock_codes, ccass_sh_map, today_ds,
                                    today_pct_map=ccass_pct_map)

    # Save CCASS under the date that was actually fetched
    if not df_ccass.empty:
        ccass_save_day(ccass_save_date, {r.stock_code: {
            "sh":   r.shareholding,
            "pct":  r.pct_listed,
            "name": r.name,
        } for r in df_ccass.itertuples()})

    ccass_delta_map      = dict(zip(df_cs["stock_code"], df_cs["ccass_delta"]))
    ccass_consec_map     = dict(zip(df_cs["stock_code"], df_cs["ccass_consec"]))
    ccass_streak_pct_map = dict(zip(df_cs["stock_code"], df_cs["ccass_streak_pct"]))
    pct_listed_map       = dict(zip(df_cs["stock_code"], df_cs["pct_listed"]))
    pct_delta_map        = dict(zip(df_cs["stock_code"], df_cs["pct_delta"]))

    # 4b. SFC cumulative short position
    sfc_map = {}
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
                    sfc_sh   = pos["sh"]
                    sfc_hkd  = pos.get("hkd", 0.0)
                    total_sh = sdw_get_total_sh(code, today_ds)
                    sfc_pct  = round(sfc_sh / total_sh * 100, 4) if total_sh > 0 else 0.0
                    sfc_map[code] = {"sfc_sh": sfc_sh, "sfc_hkd": sfc_hkd, "sfc_pct": sfc_pct}
                log.info("SFC short positions: %d stocks from %s", len(sfc_map), _latest_sfc_ds)
        except Exception as e:
            log.warning("SFC map build failed: %s", e)

    # 5. Southbound top10
    from sc_top10_library import (fetch_day as sc_fetch_day, save_year as sc_save_year,
                                   load_year as sc_load_year, lib_path as sc_lib_path)
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

    sb_map       = {}
    sb_date_used = today_ds
    _MIN_SB      = 5

    sb_map = _build_sb_map(get_top10(today_ds))
    if sb_map and len(sb_map) < _MIN_SB:
        log.warning("Southbound top10: only %d stocks for %s — discarding", len(sb_map), today_ds)
        sb_map = {}

    if not sb_map:
        log.info("Southbound top10: not in library for %s — attempting live fetch", today_ds)
        live_rec   = sc_fetch_day(trading_day.date() if hasattr(trading_day, "date") else
                                  _date.fromisoformat(today_ds))
        live_count = len(live_rec.get("top10", [])) if live_rec else 0
        if live_rec and live_count >= _MIN_SB:
            year = trading_day.year
            lib  = sc_load_year(year)
            lib["by_date"][today_ds] = live_rec
            sc_save_year(year, lib)
            sb_map = _build_sb_map(live_rec.get("top10", []))
            log.info("Southbound top10: live fetch — %d stocks for %s", len(sb_map), today_ds)
        else:
            log.info("Southbound top10: live fetch returned %d stocks (< %d)", live_count, _MIN_SB)

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

    # 5a. sb_consec and sb_net_prev
    def _sb_consec_and_prev(code: str) -> tuple[int, int]:
        history  = get_top10_history(code, 30, today_ds)
        prev_net = (history[0]["buy"] - history[0]["sell"]) if history else 0
        today_net = sb_map[code]["sb_net"]
        if today_net < 0:
            consec = -1
            for entry in history:
                net = entry["buy"] - entry["sell"]
                if net < 0:   consec -= 1
                elif net == 0: continue
                else:          break
            return consec, prev_net
        if today_net == 0:
            return 0, prev_net
        consec = 1
        for entry in history:
            net = entry["buy"] - entry["sell"]
            if net > 0:   consec += 1
            elif net == 0: continue
            else:          break
        return consec, prev_net

    sb_consec_map = {}
    sb_prev_map   = {}
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

        short_vol_today = short_vol_map.get(code, 0)
        vol_hist30      = get_vol_history(code, 30, today_ds)
        avg_vol30       = sum(vol_hist30) / len(vol_hist30) if vol_hist30 else 0
        today_vol       = int(row.shares)
        days_to_cover   = round(short_vol_today / avg_vol30, 2) if avg_vol30 > 0 else 0.0
        vol_ratio       = round(today_vol / avg_vol30, 2)       if avg_vol30 > 0 else 0.0

        tv_hist30  = get_tv_history(code, 30, today_ds)
        tv_avg30   = sum(tv_hist30) / len(tv_hist30) if tv_hist30 else 0.0
        tv_ratio30 = round(float(row.turnover) / tv_avg30, 2) if tv_avg30 > 0 else 0.0
        pct_hist30 = get_pct_history(code, 30, today_ds)
        pct_avg30_lvl = round(sum(pct_hist30) / len(pct_hist30), 4) if pct_hist30 else 0.0
        pct_dev30  = round(pct_listed - pct_avg30_lvl, 4) if pct_avg30_lvl > 0 else 0.0

        tv_t2_raw   = get_tv(code, t2_key)
        # T-2 short: use the day after t2_date as the exclusive upper bound
        _t2_before  = (t2_date + timedelta(days=1)).strftime("%Y-%m-%d")
        _t2_short   = get_short_history(code, 1, _t2_before)
        short_sv_t2 = _t2_short[0]["sv"] if _t2_short else 0
        vol_t2_raw  = _tv_recent.get(t2_key, {}).get(code, {})
        vol_t2      = vol_t2_raw.get("vol", 0) if isinstance(vol_t2_raw, dict) else 0
        short_ratio_t2 = round(short_sv_t2 / vol_t2 * 100, 2) if vol_t2 > 0 else short_ratio
        turnover_t2    = tv_t2_raw if tv_t2_raw > 0 else float(row.turnover)

        stock_type = classify_stock(code, row.name)
        _, ind_zh  = get_industry(code)

        sb          = sb_map.get(code, {})
        has_history = len(tv_hist30) >= 5 and len(vol_hist30) >= 5
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
            "code": code, "name": row.name,
            "name_chi": getattr(row, "name_chi", row.name),
            "stock_type": stock_type, "industry_zh": ind_zh,
            "turnover": int(row.turnover),
            "sb_buy":   sb.get("sb_buy",   0),
            "sb_sell":  sb.get("sb_sell",  0),
            "sb_net":   sb.get("sb_net",   0),
            "sb_total": sb.get("sb_total", 0),
            "sb_net_prev": int(sb_prev_map.get(code, 0)),
            "sb_consec":   int(sb_consec_map.get(code, 0)),
            "short_ratio":    round(short_ratio, 2),
            "short_avg5":     round(short_avg5, 2),
            "short_ratio_t2": round(short_ratio_t2, 2),
            "short_vol":      int(short_vol_today),
            "days_to_cover":  days_to_cover,
            "vol_ratio":      vol_ratio,
            "sfc_sh":   sfc_map.get(code, {}).get("sfc_sh",  0),
            "sfc_hkd":  sfc_map.get(code, {}).get("sfc_hkd", 0.0),
            "sfc_pct":  sfc_map.get(code, {}).get("sfc_pct", 0.0),
            "tv_ratio30": tv_ratio30,
            "pct_dev30":  round(pct_dev30, 4),
            "ccass_trade_date":  t2_date.strftime("%Y-%m-%d"),
            "ccass_delta":       int(ccass_delta),
            "ccass_consec":      int(ccass_consec),
            "ccass_streak_pct":  round(ccass_streak_pct, 4),
            "pct_listed": round(pct_listed, 4),
            "pct_delta":  round(pct_delta,  4),
            "insight": insight,
        })

    # 7b. Append sc_top10 stocks not in today's quotation
    _codes_in_results = {r["code"] for r in results}
    _nm        = load_store(NAME_MAP_FILE)
    _sb_extras = 0
    for _code, _sb in sb_map.items():
        if _code in _codes_in_results:
            continue
        _nm_entry  = _nm.get(_code, {})
        _name_eng  = _nm_entry.get("en", _code)
        _name_chi  = _nm_entry.get("zh", "") or get_zh_name(_code) or _name_eng
        _stype     = classify_stock(_code, _name_eng)
        _, _ind_zh = get_industry(_code)
        _consec, _prev = _sb_consec_and_prev(_code)
        _tv       = get_tv(_code, trading_day.strftime("%Y%m%d"))
        _turnover = _tv if _tv > 0 else _sb.get("sb_total", 0)
        _prev_rank = prev_ranks.get(_code)
        results.append({
            "rank": len(results) + 1,
            "rank_change": 0 if _prev_rank is None else _prev_rank - (len(results) + 1),
            "rank_new": _prev_rank is None,
            "code": _code, "name": _name_eng, "name_chi": _name_chi,
            "stock_type": _stype, "industry_zh": _ind_zh,
            "turnover": _turnover,
            "sb_buy":      _sb.get("sb_buy",  0),
            "sb_sell":     _sb.get("sb_sell", 0),
            "sb_net":      _sb.get("sb_net",  0),
            "sb_total":    _sb.get("sb_total",0),
            "sb_net_prev": int(_prev),
            "sb_consec":   int(_consec),
            "short_ratio": 0.0, "short_avg5": 0.0, "short_ratio_t2": 0.0,
            "short_vol": 0, "days_to_cover": 0.0, "vol_ratio": 0.0,
            "sfc_sh":  sfc_map.get(_code, {}).get("sfc_sh",  0),
            "sfc_hkd": sfc_map.get(_code, {}).get("sfc_hkd", 0.0),
            "sfc_pct": sfc_map.get(_code, {}).get("sfc_pct", 0.0),
            "tv_ratio30": 0.0, "pct_dev30": 0.0,
            "ccass_trade_date":  t2_date.strftime("%Y-%m-%d"),
            "ccass_delta":       int(ccass_delta_map.get(_code, 0)),
            "ccass_consec":      int(ccass_consec_map.get(_code, 0)),
            "ccass_streak_pct":  round(ccass_streak_pct_map.get(_code, 0.0), 4),
            "pct_listed": round(pct_listed_map.get(_code, ccass_pct_map.get(_code, 0.0)), 4),
            "pct_delta":  round(pct_delta_map.get(_code, 0.0), 4),
            "insight": None,
        })
        _sb_extras += 1
    if _sb_extras:
        log.info("Added %d sc_top10 stocks not in quotation", _sb_extras)

    # 8. Persist
    output = {
        "update_time": trading_day.strftime("%Y-%m-%d %H:%M"),
        "sb_date":     sb_date_used,
        "name_map":    load_store(NAME_MAP_FILE),
        "stocks":      results,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
    log.info("data.json written: %d stocks", len(results))
    save_rank_history(trading_day, results)

    # 9. Telegram
    if results:
        flagged     = [s for s in results if s["insight"]]
        new_entries = [s for s in results if s["rank_new"]]
        big_movers  = [s for s in results if not s["rank_new"] and s["rank_change"] >= 5]
        top         = results[0]
        top_rc      = (f" [↑{top['rank_change']}]" if top["rank_change"] > 0
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
