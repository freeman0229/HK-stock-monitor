"""
build_short.py — HKEX Short Selling → Short Library
=====================================================
Source : https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm
Section: #adj_short  (bottom of file, Big5-encoded <pre> block)

Key rule:
  d{D}c.htm bottom section contains short selling data for D-1.
  e.g. d260327c.htm says "在26/03/2026的總賣空成交量" → short date is 2026-03-26.

  To save short for date D:
    → fetch d{next_trading_day(D)}c.htm and parse its bottom section.

Section detection:
  The column header line contains both 股數 (U+80A1 U+6578) and "SH":
      代號  股票名稱  股數(SH)  金額($)

Source columns (4 total):
  [*/%] CODE  股票名稱  股數(SH)  金額($)
        代號     name     sv        st

Stored per stock per day in short_{YYYY}.json:
  name  ← 股票名稱   (Chinese name)
  sv    ← 股數(SH)   (short sell shares)
  st    ← 金額($)    (short sell HKD amount)

Rules:
  • Codes 1–9999 only. * or % prefix before code is stripped.
  • If code appears more than once, keep the higher-sv record.
  • Overwrites existing records for each date (true rebuild).

Usage:
  python build_short.py               # rebuild START_DATE to today
  python build_short.py --dry-run     # preview without writing
  python build_short.py --date 260326 # short for 260326 (fetches 260327)
  python build_short.py --from 260201 # override start date (YYMMDD)
"""

import os, re, json, time, logging, argparse
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

try:
    import holidays as _hol
    _HK_HOLIDAYS = _hol.HongKong()
    _USE_HOL = True
except ImportError:
    _USE_HOL = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("build_short.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

START_DATE = date(2026, 2, 2)
SLEEP_SEC  = 1.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkex.com.hk/",
}

_HK_HOL_HARDCODED = {
    "2025-01-01","2025-01-29","2025-01-30","2025-01-31",
    "2025-04-04","2025-04-05","2025-04-07",
    "2025-05-01","2025-05-05","2025-06-02","2025-07-01",
    "2025-09-30","2025-10-01","2025-10-29","2025-12-25","2025-12-26",
    "2026-01-01",
    "2026-02-17","2026-02-18","2026-02-19","2026-02-20",
    "2026-04-03","2026-04-04","2026-04-05","2026-04-06",
    "2026-05-01","2026-05-25","2026-06-19","2026-07-01",
    "2026-09-07","2026-10-01","2026-10-26","2026-12-25","2026-12-26",
}

def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if _USE_HOL:
        return d not in _HK_HOLIDAYS
    return d.isoformat() not in _HK_HOL_HARDCODED

def all_trading_days(start: date, end: date) -> list:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out

def next_trading_day(d: date) -> date:
    """Return the next trading day after d."""
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


# ── Library I/O ───────────────────────────────────────────────────────────────

def _lib_path(year: int) -> str:
    return f"short_{year}.json"

def _load(year: int) -> dict:
    p = _lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "by_date": {}}

def _save(year: int, lib: dict):
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
    }
    with open(_lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(_lib_path(year)) / 1e6
    log.info("Saved %s: %d days  %.2f MB", _lib_path(year), len(lib["by_date"]), mb)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(d: date) -> str | None:
    """Fetch d{YYMMDD}c.htm and return Big5-decoded <pre> text, or None."""
    url = (f"https://www.hkex.com.hk/chi/stat/smstat/dayquot/"
           f"d{d.strftime('%y%m%d')}c.htm")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 404:
            log.warning("404 — %s not yet published or non-trading day", d)
            return None
        resp.raise_for_status()
        try:
            text = resp.content.decode("big5", errors="replace")
            if not any("\u4e00" <= c <= "\u9fff" for c in text[:5000]):
                raise ValueError
        except Exception:
            text = resp.content.decode("latin-1", errors="replace")
            log.warning("latin-1 fallback for %s", d)
        pre = BeautifulSoup(text, "html.parser").find("pre")
        return pre.get_text() if pre else text
    except Exception as e:
        log.error("Fetch failed %s: %s", d, e)
        return None


# ── Parse ─────────────────────────────────────────────────────────────────────
#
# Section detection:
#   Scan lines until we find one containing BOTH:
#     股數 (U+80A1 U+6578)  AND  "SH"
#   This is the column header line: 代號  股票名稱  股數(SH)  金額($)
#
# Data line format:
#   [*/%] CODE  NAME_ZH  SV  ST
#   g1           g2       g3  g4
#
# * or % may appear before CODE — stripped by the pattern.
# Skip lines whose name field is a known header/total word.

_SHORT_LINE = re.compile(
    r"^[*%\s]{0,4}(\d{1,4})\s+"   # g1  代號  (1–4 digits; * % prefix stripped)
    r"(.{2,40}?)\s{2,}"            # g2  股票名稱  (ends at 2+ spaces)
    r"([\d,]+)\s+"                 # g3  股數(SH) → sv
    r"([\d,]+)\s*$"                # g4  金額($)  → st
)

_SKIP_NAMES = {
    "\u5408\u8a08",              # 合計
    "TOTAL",
    "\u4ee3\u865f",              # 代號
    "\u80a1\u7968\u540d\u7a31", # 股票名稱
    "\u80a1\u6578",              # 股數
    "\u91d1\u984d",              # 金額
}


def parse(body: str) -> dict:
    """
    Parse the short selling section of d{YYMMDD}c.htm.

    Section starts after the column header line containing 股數 AND 'SH'.
    Returns {code5: record} for codes 1–9999 with sv > 0.

    Record schema:
        name  (str)   股票名稱
        sv    (int)   股數(SH)  ← short sell shares
        st    (float) 金額($)   ← short sell HKD amount
    """
    lines    = body.splitlines()
    in_short = False
    out      = {}

    for i, line in enumerate(lines):
        stripped = line.strip()

        if not in_short:
            # Detect column header: 股數 (U+80A1 U+6578) AND "SH" on same line
            if "\u80a1\u6578" in stripped and "SH" in stripped:
                in_short = True
                log.debug("Short section found at line %d: %s", i, stripped[:80])
            continue

        # Skip blank or known header/total lines
        if not stripped or stripped in _SKIP_NAMES:
            continue

        m = _SHORT_LINE.match(line)
        if not m:
            continue

        code_int = int(m.group(1))
        if not (1 <= code_int <= 9999):
            continue

        code = str(code_int).zfill(5)
        name = m.group(2).strip()

        if name in _SKIP_NAMES or not name:
            continue

        sv = int(m.group(3).replace(",", ""))      # 股數(SH)
        st = float(m.group(4).replace(",", ""))    # 金額($)

        if sv <= 0:
            continue

        # Keep higher-sv record if code appears twice
        if code not in out or sv > out[code]["sv"]:
            out[code] = {"name": name, "sv": sv, "st": st}

    if not out:
        log.warning(
            "parse: 0 short records — column header '股數...SH' may not have "
            "matched. Check build_short.log and verify Big5 decoding."
        )
    return out


# ── Save ──────────────────────────────────────────────────────────────────────

def save_day(d: date, records: dict, dry_run: bool = False):
    if not records:
        log.warning("No short records for %s — skipping", d)
        return
    ds = d.isoformat()
    log.info("%s  %d stocks  total SV: %s shares",
             ds, len(records), f"{sum(r['sv'] for r in records.values()):,}")
    if dry_run:
        log.info("  [dry-run] not written")
        return
    year = d.year
    lib  = _load(year)
    lib["by_date"][ds] = records
    _save(year, lib)


# ── Build ─────────────────────────────────────────────────────────────────────

def build(start: date, end: date, dry_run: bool = False):
    """
    For each short date D in [start, end]:
      Fetch d{next_trading_day(D)}c.htm
      Parse bottom section → save short for D.

    Rationale:
      d{D}c.htm bottom section = short data for D-1.
      So to get short for D, we need the NEXT day's file.
      e.g. short for 2026-03-26 is in d260327c.htm.

    Note: short for the most recent trading day is not yet available
    until the following day's file is published. This is expected.
    """
    short_dates = all_trading_days(start, end)
    log.info("Short build: %d dates  %s → %s  dry_run=%s",
             len(short_dates), start, end, dry_run)

    ok = failed = 0
    for short_date in short_dates:
        fetch_date = next_trading_day(short_date)
        log.info("Short for %s — fetching %s …", short_date, fetch_date)
        body = fetch(fetch_date)
        if not body:
            log.warning("Cannot get short for %s (file %s unavailable — "
                        "today's short is not available until tomorrow)",
                        short_date, fetch_date)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        records = parse(body)
        if not records:
            log.warning("0 short records parsed from %s's file", fetch_date)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        save_day(short_date, records, dry_run=dry_run)
        ok += 1
        time.sleep(SLEEP_SEC)

    log.info("Done. Saved=%d  Failed=%d", ok, failed)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HKEX short selling library builder")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview without writing")
    ap.add_argument("--date",    metavar="YYMMDD",
                    help="Short for this date, e.g. 260326 (fetches 260327's file)")
    ap.add_argument("--from",    dest="from_date", metavar="YYMMDD",
                    help="Override start date, e.g. 260201")
    args = ap.parse_args()

    def _d(s): return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:]))

    if args.date:
        d = _d(args.date)
        build(d, d, dry_run=args.dry_run)
    else:
        start = _d(args.from_date) if args.from_date else START_DATE
        build(start, date.today(), dry_run=args.dry_run)
