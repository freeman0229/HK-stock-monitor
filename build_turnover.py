"""
build_turnover.py — HKEX Daily Quotation Backfill
==================================================
Fetches d{YYMMDD}c.htm for every trading day from START_DATE to today,
adds missing dates and re-fetches bad data into turnover_{YYYY}.json.

New schema (adds high / low; fixes close which was always 0):
  {code: {"tv": int, "vol": int, "high": float, "low": float, "close": float}}

Also updates name_map.json with English + Chinese names from each fetch.

Usage:
  python build_turnover.py              # fetch all missing / bad dates
  python build_turnover.py --dry-run    # show what would be fetched, no changes
  python build_turnover.py --date 260320  # force-refetch one specific date (YYMMDD)
  python build_turnover.py --from 260201  # override start date (YYMMDD)
"""

import os, re, json, time, logging, argparse
from datetime import date, timedelta, datetime

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

START_DATE      = date(2026, 2, 2)   # first date to include
SLEEP_SEC       = 1.5                # polite delay between requests
# Minimum number of stock records to consider a fetch valid.
# Archive files return the full market (~500-800 stocks); today-only fetches
# return a narrower set. 30 is a safe lower bound — any real trading day
# will have far more. Do NOT use a total-HKD threshold: stored data contains
# only the top ~50-60 stocks, but archive re-fetches return all active stocks
# whose combined HKD can be 10-15B (well below any "50B" floor) yet be valid.
MIN_RECORDS     = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkex.com.hk/",
}

NAME_MAP_FILE = "name_map.json"

# ── HK Public Holidays (hardcoded fallback if `holidays` library absent) ─────
# Sources: HKSAR Labour Department official gazetted holidays 2026
_HK_HOL_HARDCODED = {
    # 2026
    "2026-01-01",                            # New Year's Day
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # Lunar New Year
    "2026-04-03", "2026-04-04", "2026-04-06",  # Good Friday / Easter
    "2026-04-05",                            # Ching Ming Festival
    "2026-05-01",                            # Labour Day
    "2026-05-25",                            # Buddha's Birthday
    "2026-06-19",                            # Dragon Boat Festival
    "2026-07-01",                            # HKSAR Establishment Day
    "2026-09-07",                            # Day after Mid-Autumn Festival
    "2026-10-01",                            # National Day
    "2026-10-26",                            # Chung Yeung Festival
    "2026-12-25", "2026-12-26",              # Christmas
    # 2025 (for bootstrap if start date is earlier)
    "2025-01-01",
    "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-04-04", "2025-04-05", "2025-04-07",
    "2025-05-01", "2025-05-05",
    "2025-06-02",
    "2025-07-01",
    "2025-09-30",
    "2025-10-01",
    "2025-10-29",
    "2025-12-25", "2025-12-26",
}

def _is_hk_holiday(d: date) -> bool:
    if _USE_HOL_LIB:
        return d in _HK_HOLIDAYS
    return d.isoformat() in _HK_HOL_HARDCODED

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not _is_hk_holiday(d)

def all_trading_days(start: date, end: date) -> list[date]:
    result, d = [], start
    while d <= end:
        if is_trading_day(d):
            result.append(d)
        d += timedelta(days=1)
    return result

# ── Turnover library I/O ──────────────────────────────────────────────────────

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

def _record_has_price(rec: dict) -> bool:
    """True if this record already has the new high/low/close fields."""
    return isinstance(rec, dict) and "high" in rec

def day_status(ds: str, lib: dict) -> str:
    """
    Returns one of:
      'missing'   — date not stored at all
      'bad_tv'    — stored but record count < MIN_RECORDS (incomplete fetch)
      'no_price'  — stored but missing high/low/close fields (old schema)
      'ok'        — complete
    """
    day = lib.get("by_date", {}).get(ds)
    if not day:
        return "missing"
    total = sum(v["tv"] for v in day.values() if isinstance(v, dict) and "tv" in v)
    if len(day) < MIN_RECORDS:
        return "bad_tv"
    # Check if any record is missing the new price fields
    sample = next((v for v in day.values() if isinstance(v, dict)), None)
    if sample and not _record_has_price(sample):
        return "no_price"
    return "ok"

# ── Fetch & parse ─────────────────────────────────────────────────────────────

def _yymmdd(d: date) -> str:
    """date → YYMMDD string used in HKEX URL."""
    return d.strftime("%y%m%d")

def fetch_raw(d: date) -> str | None:
    """Download and Big5-decode d{YYMMDD}c.htm. Returns <pre> body or None."""
    url = f"https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{_yymmdd(d)}c.htm"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 404:
            log.warning("404 for %s (non-trading day or not yet published)", d)
            return None
        resp.raise_for_status()
        # Primary: Big5 (traditional Chinese); fallback: latin-1
        try:
            text = resp.content.decode("big5", errors="replace")
            if not any('\u4e00' <= c <= '\u9fff' for c in text[:5000]):
                raise ValueError("No CJK detected, trying latin-1")
        except (ValueError, UnicodeDecodeError):
            text = resp.content.decode("latin-1", errors="replace")
            log.debug("latin-1 fallback for %s", d)
        pre = BeautifulSoup(text, "html.parser").find("pre")
        return pre.get_text() if pre else text
    except requests.HTTPError as e:
        log.warning("HTTP %s for %s", e.response.status_code, d)
        return None
    except Exception as e:
        log.error("fetch_raw failed for %s: %s", d, e)
        return None


# Pattern B (corrected):
# CODE   NAME_ENG   CHI_NAME   CURR   PRV   BID   ASK   HIGH   LOW   CLOSE   SHARES   TURNOVER
# g1     g2         g3                                   g4     g5    g6      g7       g8
_PAT = re.compile(
    r'^[\*\s]{0,5}(\d{1,5})\s+'              # g1: stock code (no leading zeros)
    r'(\S[^\u3000\n]{1,22}?)\s{2,}'          # g2: English name
    r'(.{1,30}?)\s*'                         # g3: Chinese name
    r'(?:HKD|USD|CNY|EUR|GBP)\s+'           # currency label (skip)
    r'[\d,.NA-]+\s+'                         # PRV close    (skip)
    r'[\d,.NA-]+\s+'                         # BID          (skip)
    r'[\d,.NA-]+\s+'                         # ASK          (skip)
    r'([\d,.NA-]+)\s+'                       # g4: HIGH
    r'([\d,.NA-]+)\s+'                       # g5: LOW
    r'([\d,.NA-]+)\s+'                       # g6: CLOSE    ← was missing in old code
    r'([\d,]{5,})\s+'                        # g7: SHARES (volume)
    r'([\d,]{8,})\s*$'                       # g8: TURNOVER (HKD)
)

def _to_float(s: str) -> float:
    s = s.replace(",", "").strip()
    if s in ("NA", "--", "", "N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

def _is_valid_cjk(s: str) -> bool:
    if not s:
        return False
    cjk     = sum(1 for c in s if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    garbage = s.count('\ufffd') + s.count('?')
    return cjk >= 1 and garbage < 3

def parse_dayquot(body: str) -> tuple[list[dict], dict]:
    """
    Parse <pre> body into records.

    Returns:
      records   list of dicts with all fields, sorted by tv desc
      name_updates  {code: {"en": str, "zh": str}} for name_map.json
    """
    best: dict[str, dict] = {}
    for line in body.splitlines():
        m = _PAT.match(line)
        if not m:
            continue
        code_int = int(m.group(1))
        if code_int > 9999:          # warrants, CBBCs, structured products
            continue
        code     = str(code_int).zfill(5)
        name_eng = m.group(2).strip()
        name_chi = re.sub(r'[\u3000\uff20\uff64\s]+$', '', m.group(3)).strip()
        if not _is_valid_cjk(name_chi):
            name_chi = name_eng           # fall back if Big5 decode garbled
        high  = _to_float(m.group(4))
        low   = _to_float(m.group(5))
        close = _to_float(m.group(6))
        vol   = int(m.group(7).replace(",", ""))
        tv    = int(m.group(8).replace(",", ""))
        if tv <= 0:
            continue
        # Deduplicate: keep the record with highest turnover if code appears twice
        if code not in best or tv > best[code]["tv"]:
            best[code] = {
                "code":     code,
                "name_eng": name_eng,
                "name_chi": name_chi,
                "tv":       tv,
                "vol":      vol,
                "high":     high,
                "low":      low,
                "close":    close,
            }
    records = sorted(best.values(), key=lambda x: x["tv"], reverse=True)
    name_updates = {
        r["code"]: {"en": r["name_eng"], "zh": r["name_chi"]}
        for r in records
        if r["name_chi"] and r["name_chi"] != r["name_eng"]
    }
    return records, name_updates

# ── Save one day ──────────────────────────────────────────────────────────────

def save_day(d: date, records: list[dict], name_updates: dict, dry_run: bool = False):
    if not records:
        log.warning("No records to save for %s — skipping", d)
        return
    ds        = d.isoformat()
    year      = d.year
    total_tv  = sum(r["tv"] for r in records)
    log.info(
        "%s  %d stocks  HKD %s",
        ds, len(records), f"{total_tv:,.0f}"
    )
    if dry_run:
        log.info("  [dry-run] would save %d records", len(records))
        return
    # Turnover library
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
    # Name map — only add new entries; never overwrite verified names
    nm = load_name_map()
    changed = False
    for code, names in name_updates.items():
        if code not in nm or not nm[code].get("verified"):
            nm[code] = names
            changed = True
    if changed:
        save_name_map(nm)
        log.info("  name_map updated (%d codes)", len(name_updates))

# ── Main build logic ──────────────────────────────────────────────────────────

def build(start: date, end: date, force_date: date | None = None, dry_run: bool = False):
    """
    Iterates trading days from start→end.
    Fetches any day whose status is not 'ok'.
    If force_date is given, only process that one day regardless of status.
    """
    # Load all years we might touch
    years_needed = set(range(start.year, end.year + 1))
    libs = {y: load_year(y) for y in years_needed}

    if force_date:
        days_to_check = [force_date]
    else:
        days_to_check = all_trading_days(start, end)

    fetch_queue: list[tuple[date, str]] = []
    skipped = 0

    for d in days_to_check:
        ds     = d.isoformat()
        lib    = libs.get(d.year, {"by_date": {}})
        status = day_status(ds, lib)
        if status == "ok" and not force_date:
            skipped += 1
            continue
        fetch_queue.append((d, status))

    log.info(
        "Build plan: %d days to fetch, %d already ok (skipped)",
        len(fetch_queue), skipped
    )
    if not fetch_queue:
        log.info("Nothing to do — all dates are complete.")
        return

    # Summary of what will be fetched
    for d, status in fetch_queue:
        log.info("  → %s  [%s]", d.isoformat(), status)

    if dry_run:
        log.info("[dry-run] No changes made.")
        return

    fetched = failed = 0
    for d, status in fetch_queue:
        log.info("Fetching %s (%s)…", d.isoformat(), status)
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
                "  Only %d records — HKEX file not yet published or non-trading day "
                "(skipping save).", len(records)
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
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be fetched without making any changes"
    )
    ap.add_argument(
        "--date", metavar="YYMMDD",
        help="Force-refetch one specific date, e.g. 260320"
    )
    ap.add_argument(
        "--from", dest="from_date", metavar="YYMMDD",
        help="Override start date (default: 260202)"
    )
    args = ap.parse_args()

    today = date.today()

    if args.date:
        ds = args.date.strip()
        force = date(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        build(force, force, force_date=force, dry_run=args.dry_run)
    else:
        start = START_DATE
        if args.from_date:
            ds    = args.from_date.strip()
            start = date(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        build(start, today, dry_run=args.dry_run)
