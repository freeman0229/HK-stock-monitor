"""
build_turnover.py — HKEX Daily Quotation Backfill
==================================================
Fetches full-market daily quotation data for every trading day from
START_DATE to today, storing all listed stocks with non-zero turnover.

Sources:
  Today   : https://www.hkex.com.hk/chi/stat/smstat/dayquot/qtn_c.asp
  Archive : https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm

Both use Pattern B (Big5-encoded pre-formatted text):
  代號  NAME OF STOCK  股票名稱  CURR  PRV  BID  ASK  HIGH  LOW  收市  成交股數  成交金額

Columns stored per stock per day in turnover_{YYYY}.json:
  tv      — 成交金額 (HKD turnover)
  vol     — 成交股數 (shares traded)
  high    — 最高 (intraday high)
  low     — 最低 (intraday low)
  close   — 收市 (closing price)

Also updates name_map.json with English (NAME OF STOCK) and Chinese (股票名稱) names.

MIN_RECORDS = 200: on any real trading day the full-market file contains 500+ stocks
with non-zero turnover. Days with fewer than 200 records were saved by main.py's
limited save (top ~60 stocks only) and are treated as incomplete, triggering a
re-fetch from the full-market source.

Usage:
  python build_turnover.py              # fetch all missing / bad dates
  python build_turnover.py --dry-run    # show what would be fetched, no changes
  python build_turnover.py --date 260320  # force-refetch one date (YYMMDD)
  python build_turnover.py --from 260201  # override start date (YYMMDD)
"""

import os, re, json, time, logging, argparse
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

try:
    import holidays as hol_lib
    _HK_HOLIDAYS = hol_lib.HongKong()
    _USE_HOL_LIB = True
except ImportError:
    _USE_HOL_LIB = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

START_DATE  = date(2026, 2, 2)
SLEEP_SEC   = 1.5
# A real full-market file has 500–800 stocks with non-zero turnover.
# 200 is a safe lower bound — anything below means the data is incomplete
# (e.g. saved by main.py's limited top-60 pass) and needs a full re-fetch.
MIN_RECORDS = 200

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkex.com.hk/",
}

NAME_MAP_FILE = "name_map.json"

# ── URLs ──────────────────────────────────────────────────────────────────────

# Live full-market quotation (today only, Big5, <pre> text)
_URL_TODAY = "https://www.hkex.com.hk/chi/stat/smstat/dayquot/qtn_c.asp"

# Archive full-market quotation (historical dates, Big5, <pre> text)
_URL_ARCHIVE = "https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{yymmdd}c.htm"

# ── HK Public Holidays ────────────────────────────────────────────────────────

_HK_HOL_HARDCODED = {
    "2025-01-01",
    "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-04-04", "2025-04-05", "2025-04-07",
    "2025-05-01", "2025-05-05",
    "2025-06-02",
    "2025-07-01",
    "2025-09-30", "2025-10-01", "2025-10-29",
    "2025-12-25", "2025-12-26",
    "2026-01-01",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-25",
    "2026-06-19",
    "2026-07-01",
    "2026-09-07",
    "2026-10-01", "2026-10-26",
    "2026-12-25", "2026-12-26",
}

def _is_hk_holiday(d: date) -> bool:
    if _USE_HOL_LIB:
        return d in _HK_HOLIDAYS
    return d.isoformat() in _HK_HOL_HARDCODED

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not _is_hk_holiday(d)

def all_trading_days(start: date, end: date) -> list:
    result, d = [], start
    while d <= end:
        if is_trading_day(d):
            result.append(d)
        d += timedelta(days=1)
    return result

# ── Library I/O ───────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"turnover_{year}.json"

def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "by_date": {}}

def save_year(year: int, lib: dict):
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
    }
    with open(lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(lib_path(year)) / 1e6
    log.info("Saved %s: %d days, %.2f MB", lib_path(year), len(lib["by_date"]), mb)

def load_name_map() -> dict:
    if os.path.exists(NAME_MAP_FILE):
        with open(NAME_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_name_map(nm: dict):
    with open(NAME_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(nm, f, ensure_ascii=False, separators=(",", ":"))

# ── Status check ──────────────────────────────────────────────────────────────

def day_status(ds: str, lib: dict) -> str:
    """
    'missing'   — date not stored
    'partial'   — stored but record count < MIN_RECORDS (incomplete, e.g. saved
                  by main.py's limited top-60 pass — needs full re-fetch)
    'ok'        — complete (>= MIN_RECORDS stocks)
    """
    day = lib.get("by_date", {}).get(ds)
    if not day:
        return "missing"
    if len(day) < MIN_RECORDS:
        return "partial"
    return "ok"

# ── Fetch ─────────────────────────────────────────────────────────────────────

def _decode(content: bytes) -> str:
    """Decode Big5 (traditional Chinese); fall back to latin-1."""
    try:
        text = content.decode("big5", errors="replace")
        if any("\u4e00" <= c <= "\u9fff" for c in text[:5000]):
            return text
    except Exception:
        pass
    return content.decode("latin-1", errors="replace")

def fetch_raw(d: date) -> str | None:
    """
    Download and decode the quotation file for date d.
    Today → qtn_c.asp (live full-market file)
    Historical → d{YYMMDD}c.htm (archive)
    Returns <pre> body text or None.
    """
    is_today = (d == date.today())

    if is_today:
        url = _URL_TODAY
    else:
        url = _URL_ARCHIVE.format(yymmdd=d.strftime("%y%m%d"))

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 404:
            log.warning("404 for %s — not published yet or non-trading day", d)
            return None
        resp.raise_for_status()
        text = _decode(resp.content)
        pre  = BeautifulSoup(text, "html.parser").find("pre")
        return pre.get_text() if pre else text
    except requests.HTTPError as e:
        log.warning("HTTP %s for %s (%s)", e.response.status_code, d, url)
        return None
    except Exception as e:
        log.error("fetch_raw failed for %s: %s", d, e)
        return None

# ── Pattern B parser ──────────────────────────────────────────────────────────
#
# Columns in order (space-separated):
#   代號   NAME OF STOCK   股票名稱   CURR   PRV   BID   ASK   HIGH   LOW   收市   成交股數   成交金額
#   g1     g2              g3         skip   skip  skip  skip  g4     g5    g6     g7         g8

_PAT = re.compile(
    r"^[\*\s]{0,5}(\d{1,5})\s+"          # g1: 代號 (stock code, no leading zeros)
    r"(\S[^\u3000\n]{1,22}?)\s{2,}"      # g2: NAME OF STOCK (English)
    r"(.{1,30}?)\s*"                      # g3: 股票名稱 (Chinese)
    r"(?:HKD|USD|CNY|EUR|GBP)\s+"        # currency (skip)
    r"[\d,.NA-]+\s+"                      # PRV    (skip)
    r"[\d,.NA-]+\s+"                      # BID    (skip)
    r"[\d,.NA-]+\s+"                      # ASK    (skip)
    r"([\d,.NA-]+)\s+"                    # g4: HIGH (最高)
    r"([\d,.NA-]+)\s+"                    # g5: LOW  (最低)
    r"([\d,.NA-]+)\s+"                    # g6: 收市 (close)
    r"([\d,]{5,})\s+"                     # g7: 成交股數 (volume)
    r"([\d,]{8,})\s*$"                    # g8: 成交金額 (turnover HKD)
)

def _to_float(s: str) -> float:
    s = s.replace(",", "").strip()
    return float(s) if s not in ("NA", "--", "", "N/A") else 0.0

def _is_valid_cjk(s: str) -> bool:
    if not s:
        return False
    cjk     = sum(1 for c in s if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")
    garbage = s.count("\ufffd") + s.count("?")
    return cjk >= 1 and garbage < 3

def parse_dayquot(body: str) -> tuple:
    """
    Parse Pattern B pre-formatted text.
    Returns (records, name_updates):
      records      — list of dicts with all fields, sorted by tv desc
      name_updates — {code: {"en": str, "zh": str}} for name_map.json
    """
    best: dict = {}
    for line in body.splitlines():
        m = _PAT.match(line)
        if not m:
            continue
        code_int = int(m.group(1))
        if code_int > 9999:       # warrants, CBBCs, structured products
            continue
        code     = str(code_int).zfill(5)
        name_en  = m.group(2).strip()
        name_zh  = re.sub(r"[\u3000\uff20\uff64\s]+$", "", m.group(3)).strip()
        if not _is_valid_cjk(name_zh):
            name_zh = name_en    # fall back if Big5 decode garbled
        high  = _to_float(m.group(4))
        low   = _to_float(m.group(5))
        close = _to_float(m.group(6))
        vol   = int(m.group(7).replace(",", ""))
        tv    = int(m.group(8).replace(",", ""))
        if tv <= 0:
            continue
        # Keep the record with highest turnover if code appears twice
        if code not in best or tv > best[code]["tv"]:
            best[code] = {
                "code":    code,
                "name_en": name_en,
                "name_zh": name_zh,
                "tv":      tv,
                "vol":     vol,
                "high":    high,
                "low":     low,
                "close":   close,
            }

    records = sorted(best.values(), key=lambda x: x["tv"], reverse=True)
    name_updates = {
        r["code"]: {"en": r["name_en"], "zh": r["name_zh"]}
        for r in records
        if r["name_zh"] and r["name_zh"] != r["name_en"]
    }
    return records, name_updates

# ── Save one day ──────────────────────────────────────────────────────────────

def save_day(d: date, records: list, name_updates: dict, dry_run: bool = False):
    if not records:
        log.warning("No records to save for %s — skipping", d)
        return
    ds       = d.isoformat()
    year     = d.year
    total_tv = sum(r["tv"] for r in records)
    log.info("%s  %d stocks  HKD %s", ds, len(records), f"{total_tv:,.0f}")
    if dry_run:
        log.info("  [dry-run] would save %d records", len(records))
        return

    lib = load_year(year)
    lib["by_date"][ds] = {
        r["code"]: {
            "tv":    r["tv"],
            "vol":   r["vol"],
            "high":  r["high"],
            "low":   r["low"],
            "close": r["close"],
        }
        for r in records
    }
    save_year(year, lib)

    # Update name_map — never overwrite verified entries
    nm      = load_name_map()
    changed = False
    for code, names in name_updates.items():
        if code not in nm or not nm[code].get("verified"):
            nm[code] = names
            changed  = True
    if changed:
        save_name_map(nm)
        log.info("  name_map updated (%d codes)", len(name_updates))

# ── Main build logic ──────────────────────────────────────────────────────────

def build(start: date, end: date,
          force_date: date | None = None, dry_run: bool = False):
    """
    Iterate trading days from start→end.
    Fetch any day whose status is 'missing' or 'partial'.
    If force_date is given, process only that date regardless of status.
    """
    years_needed = set(range(start.year, end.year + 1))
    libs = {y: load_year(y) for y in years_needed}

    days_to_check = [force_date] if force_date else all_trading_days(start, end)

    fetch_queue = []
    skipped     = 0
    for d in days_to_check:
        ds     = d.isoformat()
        lib    = libs.get(d.year, {"by_date": {}})
        status = day_status(ds, lib)
        if status == "ok" and not force_date:
            skipped += 1
            continue
        fetch_queue.append((d, status))

    log.info("Build plan: %d to fetch, %d already ok (skipped)", len(fetch_queue), skipped)
    for d, st in fetch_queue:
        log.info("  → %s  [%s]", d.isoformat(), st)

    if not fetch_queue:
        log.info("Nothing to do — all dates complete.")
        return
    if dry_run:
        log.info("[dry-run] No changes made.")
        return

    fetched = failed = 0
    for d, status in fetch_queue:
        log.info("Fetching %s  (%s)…", d.isoformat(), status)
        body = fetch_raw(d)
        if not body:
            log.warning("  Empty response — skipping %s", d)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        records, name_updates = parse_dayquot(body)
        if not records:
            log.warning("  0 records parsed for %s — skipping", d)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        if len(records) < MIN_RECORDS:
            log.warning(
                "  Only %d records for %s (need >= %d) — "
                "HKEX file not yet published or non-trading day, skipping.",
                len(records), d, MIN_RECORDS
            )
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        save_day(d, records, name_updates, dry_run=False)
        fetched += 1
        time.sleep(SLEEP_SEC)

    log.info("Done: %d fetched, %d failed/skipped", fetched, failed)

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HKEX turnover backfill")
    ap.add_argument("--dry-run",   action="store_true",
                    help="Print what would be fetched without making changes")
    ap.add_argument("--date",      metavar="YYMMDD",
                    help="Force-refetch one specific date, e.g. 260320")
    ap.add_argument("--from",      dest="from_date", metavar="YYMMDD",
                    help="Override start date (default: 260202)")
    args = ap.parse_args()

    today = date.today()

    if args.date:
        ds    = args.date.strip()
        force = date(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        build(force, force, force_date=force, dry_run=args.dry_run)
    else:
        start = START_DATE
        if args.from_date:
            ds    = args.from_date.strip()
            start = date(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        build(start, today, dry_run=args.dry_run)
