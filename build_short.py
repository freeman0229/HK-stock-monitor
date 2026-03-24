"""
build_short.py — HKEX Short Selling Backfill
=============================================
Fetches daily short selling data for every trading day from START_DATE
to today, adds missing dates and repairs records without the name field.

Sources tried in order for each date:
  1. Modern HKEX page        (today only — UTF-8, HTML table)
  2. Legacy Chinese file      ashtmain_c.htm  (today, Big5, <pre> text)
  3. Archive Chinese file     ashtmain{YYMMDD}c.htm  (historical, Big5)
  4. Archive English file     ashtmain{YYMMDD}.htm   (historical, latin-1)

Schema stored per stock per day in short_{YYYY}.json:
  "YYYY-MM-DD": {
    "00001": {"sv": 2573000, "st": 156766225.0, "name": "長和"},
    ...
  }

Usage:
  python build_short.py               # fetch all missing / no-name dates
  python build_short.py --dry-run     # show plan, no changes
  python build_short.py --date 260320 # force-refetch one date (YYMMDD)
  python build_short.py --from 260101 # override start date (YYMMDD)
  python build_short.py --fix-names   # backfill missing name field only
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

START_DATE  = date(2026, 3, 20)
SLEEP_SEC   = 1.5
MIN_RECORDS = 10   # fewer = fetch failed or half-day / holiday

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":         "https://www.hkex.com.hk/",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}

# ── URL constants ─────────────────────────────────────────────────────────────

# Today — modern HKEX page (UTF-8, HTML table)
_URL_MODERN = (
    "https://www.hkex.com.hk/Market-Data/Statistics/Securities-Market/"
    "Short-Selling-Turnover-Today?sc_lang=zh-HK"
)
# Today — legacy static file (Big5, <pre> text)
_URL_TODAY_CN = (
    "https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/ashtmain_c.htm"
)
# Historical — archive Chinese Big5 (date in filename: YYMMDD)
_URL_ARCHIVE_CN = (
    "https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ashtmain{yymmdd}c.htm"
)
# Historical — archive English latin-1 (date in filename: YYMMDD)
_URL_ARCHIVE_EN = (
    "https://www.hkex.com.hk/eng/stat/smstat/ssturnover/ashtmain{yymmdd}.htm"
)

# ── HK holidays ───────────────────────────────────────────────────────────────

_HK_HOL_HARDCODED = {
    # 2025
    "2025-01-01",
    "2025-01-29","2025-01-30","2025-01-31",
    "2025-04-04","2025-04-05","2025-04-07",
    "2025-05-01","2025-05-05",
    "2025-06-02",
    "2025-07-01",
    "2025-09-30","2025-10-01","2025-10-29",
    "2025-12-25","2025-12-26",
    # 2026
    "2026-01-01",
    "2026-02-17","2026-02-18","2026-02-19","2026-02-20",
    "2026-04-03","2026-04-04","2026-04-05","2026-04-06",
    "2026-05-01","2026-05-25",
    "2026-06-19",
    "2026-07-01",
    "2026-09-07",
    "2026-10-01","2026-10-26",
    "2026-12-25","2026-12-26",
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

# ── Library I/O ───────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"short_{year}.json"

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
    kb = os.path.getsize(lib_path(year)) / 1024
    log.info("Saved short_%d.json: %d days  %.0f KB",
             year, len(lib["by_date"]), kb)

def day_status(ds: str, lib: dict) -> str:
    """
    Returns:
      'missing'  — date not in library
      'no_name'  — stored but all records lack the name field (old schema)
      'ok'       — complete
    """
    day = lib.get("by_date", {}).get(ds)
    if not day:
        return "missing"
    sample = next((v for v in day.values() if isinstance(v, dict)), None)
    if sample and "name" not in sample:
        return "no_name"
    return "ok"

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_raw(d: date) -> tuple[bytes | None, str]:
    """
    Download raw bytes for date d.
    Returns (raw_bytes, source_label) or (None, '').
    source_label is one of: 'modern', 'today_cn', 'archive_cn', 'archive_en'.
    """
    yymmdd   = d.strftime("%y%m%d")
    is_today = (d == date.today())

    # 1. Modern page — today only, UTF-8
    if is_today:
        try:
            r = requests.get(_URL_MODERN, headers=HEADERS, timeout=30)
            if r.status_code == 200 and len(r.content) > 2000:
                text = r.content.decode("utf-8", errors="replace")
                # Only accept if the page actually contains data (not a JS shell)
                if ("代號" in text or "股票名稱" in text or
                        ("Short" in text and "<table" in text.lower())):
                    log.info("Fetched %s via modern page", d)
                    return r.content, "modern"
        except Exception as e:
            log.debug("Modern page failed: %s", e)

    # 2. Legacy today file — today only, Big5
    if is_today:
        try:
            r = requests.get(_URL_TODAY_CN, headers=HEADERS, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                log.info("Fetched %s via legacy today file", d)
                return r.content, "today_cn"
        except Exception as e:
            log.debug("Legacy today file failed: %s", e)

    # 3. Archive — Chinese Big5
    url_cn = _URL_ARCHIVE_CN.format(yymmdd=yymmdd)
    try:
        r = requests.get(url_cn, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.content) > 1000:
            log.info("Fetched %s via archive CN", d)
            return r.content, "archive_cn"
        if r.status_code == 404:
            log.debug("Archive CN 404 for %s", d)
    except Exception as e:
        log.debug("Archive CN failed for %s: %s", d, e)

    # 4. Archive — English latin-1
    url_en = _URL_ARCHIVE_EN.format(yymmdd=yymmdd)
    try:
        r = requests.get(url_en, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.content) > 1000:
            log.info("Fetched %s via archive EN", d)
            return r.content, "archive_en"
        if r.status_code == 404:
            log.debug("Archive EN 404 for %s — likely non-trading day", d)
    except Exception as e:
        log.debug("Archive EN failed for %s: %s", d, e)

    log.warning("All URL strategies failed for %s", d)
    return None, ""

# ── Parse ─────────────────────────────────────────────────────────────────────

def _is_valid_cjk(s: str) -> bool:
    return bool(s) and any('\u4e00' <= c <= '\u9fff' for c in s)

# Pre-formatted text pattern (ashtmain_c.htm / archive files):
# Columns: CODE  CHI_NAME  SHARES  AMOUNT
_PRE_PAT  = re.compile(
    r'^\s{0,8}(\d{1,6})\s{1,4}'    # g1: code (no leading zeros)
    r'(.+?)\s{2,}'                  # g2: Chinese name
    r'([\d,]+)\s+'                  # g3: shares (sv)
    r'([\d,]+)\s*$'                 # g4: amount HKD (st)
)
_PRE_SKIP = frozenset({
    "股票代號","沽空成交量","合計","TOTAL","CODE","NAME OF STOCK","代號","股票名稱"
})

def _parse_pre(raw: bytes, encoding: str) -> dict:
    """
    Parse Big5 / latin-1 <pre>-formatted short sell file.
    Returns {code5: {sv, st, name}}.
    """
    try:
        text = raw.decode(encoding, errors="replace")
    except Exception:
        text = raw.decode("latin-1", errors="replace")

    pre  = BeautifulSoup(text, "html.parser").find("pre")
    body = pre.get_text() if pre else text

    records = {}
    for line in body.splitlines():
        m = _PRE_PAT.match(line)
        if not m:
            continue
        code_int = int(m.group(1))
        if code_int < 1 or code_int > 9999:
            continue
        code = str(code_int).zfill(5)
        name = m.group(2).strip()
        if name in _PRE_SKIP or not name:
            continue
        sv = int(m.group(3).replace(",", ""))
        st = float(m.group(4).replace(",", ""))
        if sv <= 0 and st <= 0:
            continue
        # Keep highest-sv record if code appears twice
        if code not in records or sv > records[code]["sv"]:
            records[code] = {"sv": sv, "st": st, "name": name}
    return records


def _parse_table(raw: bytes) -> dict:
    """
    Parse the modern HKEX HTML table page (UTF-8).
    Expected columns: 代號 | 股票名稱 | 股數 | 金額
    Column positions detected dynamically from header row.
    Returns {code5: {sv, st, name}}.
    """
    text = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")

    # Find the table containing short selling data
    target = None
    for table in soup.find_all("table"):
        tc = table.get_text()
        if ("代號" in tc or "CODE" in tc) and ("股數" in tc or "VOLUME" in tc):
            target = table
            break

    if not target:
        return {}

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

    # Fallback to assumed column order
    if col_code is None: col_code = 0
    if col_name is None: col_name = 1
    if col_sv   is None: col_sv   = 2
    if col_st   is None: col_st   = 3

    records = {}
    for tr in target.find_all("tr")[1:]:      # skip header
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
        name = cells[col_name].strip()
        try:
            sv = int(cells[col_sv].replace(",", ""))
            st = float(cells[col_st].replace(",", ""))
        except (ValueError, AttributeError):
            continue
        if sv <= 0 and st <= 0:
            continue
        if code not in records or sv > records[code]["sv"]:
            records[code] = {
                "sv":   sv,
                "st":   st,
                "name": name if _is_valid_cjk(name) else "",
            }
    return records


def parse(raw: bytes, source: str) -> dict:
    """
    Dispatch to the correct parser.
    Returns {code5: {sv, st, name}} or empty dict on failure.
    """
    if source == "modern":
        records = _parse_table(raw)
        if not records:
            # Modern URL returned HTML but no table found — try pre-parser as fallback
            log.debug("Table parse returned 0 records — trying pre parser")
            records = _parse_pre(raw, "utf-8")
        return records
    elif source in ("today_cn", "archive_cn"):
        return _parse_pre(raw, "big5")
    else:   # archive_en
        return _parse_pre(raw, "latin-1")

# ── Build ─────────────────────────────────────────────────────────────────────

def build(start: date, end: date,
          force_date: date | None = None, dry_run: bool = False):
    """
    Main build loop: iterate trading days, fetch and store any that are
    missing or incomplete.
    """
    libs = {y: load_year(y) for y in range(start.year, end.year + 1)}

    days_to_check = [force_date] if force_date else all_trading_days(start, end)

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

    log.info("Build plan: %d to fetch, %d already ok", len(fetch_queue), skipped)
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
        raw, source = fetch_raw(d)
        if not raw:
            failed += 1
            time.sleep(SLEEP_SEC)
            continue

        records = parse(raw, source)
        if len(records) < MIN_RECORDS:
            log.warning("  Only %d records — HKEX not published yet or holiday "
                        "(skipping)", len(records))
            failed += 1
            time.sleep(SLEEP_SEC)
            continue

        ds   = d.isoformat()
        year = d.year
        lib  = load_year(year)
        lib["by_date"][ds] = records
        save_year(year, lib)
        total_sv = sum(r["sv"] for r in records.values())
        log.info("  Saved %s: %d stocks  sv=%s",
                 ds, len(records), f"{total_sv:,}")
        fetched += 1
        time.sleep(SLEEP_SEC)

    log.info("Done: %d fetched, %d failed/skipped", fetched, failed)


def fix_names(start: date, end: date, dry_run: bool = False):
    """
    For any already-stored date where name field is absent, re-fetch and
    patch the names in-place without overwriting sv/st values.
    2026-03-20 was stored without names — this fixes it.
    """
    days       = all_trading_days(start, end)
    needs_fix  = []
    for d in days:
        lib    = load_year(d.year)
        ds     = d.isoformat()
        day    = lib["by_date"].get(ds, {})
        if day:
            sample = next((v for v in day.values() if isinstance(v, dict)), None)
            if sample and "name" not in sample:
                needs_fix.append(d)

    log.info("fix-names: %d dates need name backfill", len(needs_fix))
    for d in needs_fix:
        log.info("  → %s", d.isoformat())

    if dry_run:
        return

    for d in needs_fix:
        raw, source = fetch_raw(d)
        if not raw:
            log.warning("Could not fetch %s for name fix", d)
            time.sleep(SLEEP_SEC)
            continue
        fresh = parse(raw, source)
        if len(fresh) < MIN_RECORDS:
            log.warning("Too few records for %s — skipping name fix", d)
            time.sleep(SLEEP_SEC)
            continue

        # Patch names into existing records, preserving original sv/st
        lib = load_year(d.year)
        ds  = d.isoformat()
        day = lib["by_date"][ds]
        updated = 0
        for code, rec in day.items():
            if isinstance(rec, dict) and "name" not in rec:
                rec["name"] = fresh.get(code, {}).get("name", "")
                updated += 1
        save_year(d.year, lib)
        log.info("  %s: patched %d names", ds, updated)
        time.sleep(SLEEP_SEC)

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HKEX short selling backfill")
    ap.add_argument("--dry-run",   action="store_true",
                    help="Show plan without making any changes")
    ap.add_argument("--date",      metavar="YYMMDD",
                    help="Force-refetch one specific date, e.g. 260320")
    ap.add_argument("--from",      dest="from_date", metavar="YYMMDD",
                    help="Override start date (default: 260320)")
    ap.add_argument("--fix-names", action="store_true",
                    help="Backfill missing name field in already-stored records")
    args = ap.parse_args()

    today = date.today()

    if args.fix_names:
        start = START_DATE
        if args.from_date:
            ds    = args.from_date.strip()
            start = date(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        fix_names(start, today, dry_run=args.dry_run)

    elif args.date:
        ds    = args.date.strip()
        force = date(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        build(force, force, force_date=force, dry_run=args.dry_run)

    else:
        start = START_DATE
        if args.from_date:
            ds    = args.from_date.strip()
            start = date(2000 + int(ds[:2]), int(ds[2:4]), int(ds[4:6]))
        build(start, today, dry_run=args.dry_run)
