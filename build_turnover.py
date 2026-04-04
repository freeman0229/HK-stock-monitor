"""
build_turnover.py — HKEX Daily Quotation → Turnover Library
=============================================================
Source : https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm
Section: #quotations  (top of file, Big5-encoded <pre> block)

Source columns (12 total):
  [*/%] CODE  NAME OF STOCK  股票名稱  CUR  PRV  BID  ASK  HIGH  LOW  CLOSING  SHARES TRADED  TURNOVER ($)
        代號                           貨幣  前收市                              收市    成交股數          成交金額

Stored per stock per day in turnover_{YYYY}.json:
  name_en    ← NAME OF STOCK   (English)
  name_zh    ← 股票名稱         (Chinese)
  prev_close ← 前收市 / PRV
  close      ← 收市 / CLOSING
  vol        ← 成交股數 / SHARES TRADED
  tv         ← 成交金額 / TURNOVER ($)

Rules:
  • Codes 1–99999. * or % prefix before code is stripped.
  • TRADING SUSPENDED lines are skipped.
  • If code appears more than once, keep the higher-vol record.
  • Incremental by default — skips dates already in library.
  • If code appears more than once, keep the higher-vol record.

Usage:
  python build_turnover.py               # fetch only missing dates (normal daily use)
  python build_turnover.py --rebuild     # re-fetch all dates from scratch
  python build_turnover.py --dry-run     # preview without writing
  python build_turnover.py --date 260326 # single date (YYMMDD)
  python build_turnover.py --from 260201 # override start date (YYMMDD)
"""

import os, re, json, time, logging, argparse
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from ccass_universe import is_included as _is_included, normalize_code as _normalize_code

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
        logging.FileHandler("build_turnover.log", encoding="utf-8"),
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

# ── Library I/O ───────────────────────────────────────────────────────────────

def _lib_path(year: int) -> str:
    return f"turnover_{year}.json"

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
    """Fetch d{YYMMDD}c.htm and return Big5-decoded <pre> text, or None.

    The HKEX quotation file splits data across MULTIPLE <pre> blocks
    (one per page of ~150 stocks each).  find("pre") only gets the first
    page.  We join ALL blocks — the turnover regex (_PAT) won't match
    short-section lines (different column count, no currency code).
    """
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
        # Join ALL <pre> blocks — quotation data spans multiple pages
        soup = BeautifulSoup(text, "html.parser")
        pres = soup.find_all("pre")
        if pres:
            log.debug("%s: %d <pre> blocks found", d, len(pres))
            return "\n".join(p.get_text() for p in pres)
        return text
    except Exception as e:
        log.error("Fetch failed %s: %s", d, e)
        return None

# ── Parse ─────────────────────────────────────────────────────────────────────
#
# Source column order (12 columns):
#   [*/%] CODE  NAME_EN  NAME_ZH  CUR  PRV  BID  ASK  HIGH  LOW  CLOSING  SHARES  TURNOVER
#   g1           g2       g3       skip g4   skip skip skip  skip g5       g6      g7
#
# * or % may appear before CODE — stripped by the pattern.
# TRADING SUSPENDED / 暫停買賣 lines have no numeric data — skipped before regex.

_PAT = re.compile(
    r"^[*%\s]{0,8}(\d{1,5})\s+"           # g1  代號  (1–5 digits; * % prefix stripped; up to 7 leading spaces)
    r"(\S[^\u3000\n]{1,25}?)\s{2,}"       # g2  NAME OF STOCK (ends at 2+ spaces)
    r"(.{1,35}?)\s*"                       # g3  股票名稱
    r"(?:HKD|USD|CNY|RMB|EUR|GBP|AUD|JPY|SGD)\s+"  # CUR (skip; RMB used for H-share dual-currency counters)
    r"([\d,.]+)\s+"                        # g4  前收市 / PRV
    r"[\d,.NA-]+\s+"                       # BID  (skip)
    r"[\d,.NA-]+\s+"                       # ASK  (skip)
    r"[\d,.NA-]+\s+"                       # HIGH (skip)
    r"[\d,.NA-]+\s+"                       # LOW  (skip)
    r"([\d,.]+)\s+"                        # g5  收市 / CLOSING
    r"([\d,]+)\s+"                         # g6  成交股數 → vol
    r"([\d,]+)\s*$"                        # g7  成交金額 → tv
)

def _num(s: str) -> float:
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return 0.0

def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)

def parse(body: str) -> dict:
    """
    Parse the quotation section of d{YYMMDD}c.htm.
    Returns {code5: record} for universe-eligible stocks with vol > 0.
    TRADING SUSPENDED lines are silently skipped.
    Stock eligibility determined by ccass_universe.is_included().

    Record schema:
        name_en    (str)    NAME OF STOCK
        name_zh    (str)    股票名稱
        prev_close (float)  前收市
        close      (float)  收市
        vol        (int)    成交股數
        tv         (int)    成交金額
    """
    out = {}
    for line in body.splitlines():
        # Skip suspended lines — they have no numeric vol/tv
        if "SUSPENDED" in line or "\u66ab\u505c" in line:
            continue
        m = _PAT.match(line)
        if not m:
            continue
        code_int = int(m.group(1))
        code        = _normalize_code(code_int)
        if not _is_included(code):
            continue
        name_en     = m.group(2).strip()
        name_zh_raw = re.sub(r"[\u3000\uff20\uff64\s]+$", "", m.group(3)).strip()
        name_zh     = name_zh_raw if _has_cjk(name_zh_raw) else name_en
        prev_close  = _num(m.group(4))
        close       = _num(m.group(5))
        vol         = int(m.group(6).replace(",", ""))
        tv          = int(m.group(7).replace(",", ""))
        if vol <= 0:
            continue
        # Keep higher-vol record if code appears twice
        if code not in out or vol > out[code]["vol"]:
            vwap = round(tv / vol, 4) if vol > 0 else 0.0
            out[code] = {
                "name_en":    name_en,
                "name_zh":    name_zh,
                "prev_close": prev_close,
                "close":      close,
                "vol":        vol,
                "tv":         tv,
                "vwap":       vwap,
            }
    return out

# ── Incremental skip ─────────────────────────────────────────────────────────

def _existing_dates() -> set:
    """Return all date strings already saved across all turnover_{YYYY}.json files."""
    existing = set()
    for fname in os.listdir("."):
        if fname.startswith("turnover_") and fname.endswith(".json"):
            try:
                with open(fname, encoding="utf-8") as f:
                    existing.update(json.load(f).get("by_date", {}).keys())
            except Exception:
                pass
    return existing

# ── Build ─────────────────────────────────────────────────────────────────────

def build(start: date, end: date, dry_run: bool = False, rebuild: bool = False):
    """
    For each trading day D in [start, end]:
      Fetch d{D}c.htm → parse top section → save turnover for D.

    By default skips dates already present in the library (incremental).
    Pass rebuild=True to re-fetch everything regardless.

    Batches all writes by year — one file write per year, not per date.
    """
    days = all_trading_days(start, end)

    if not rebuild:
        existing = _existing_dates()
        days = [d for d in days if d.isoformat() not in existing]
        log.info("Turnover build: %d new dates to fetch  (skipping %d already saved)  dry_run=%s",
                 len(days), len(all_trading_days(start, end)) - len(days), dry_run)
    else:
        log.info("Turnover build (--rebuild): %d dates  %s → %s  dry_run=%s",
                 len(days), start, end, dry_run)

    if not days:
        log.info("Nothing to do — all dates already in library.")
        return

    # Accumulate in memory, keyed by year → {date_str: records}
    by_year: dict[int, dict] = {}
    ok = failed = 0

    for d in days:
        log.info("Fetching %s …", d)
        body = fetch(d)
        if not body:
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        records = parse(body)
        if not records:
            log.warning("%s: 0 records parsed", d)
            failed += 1
            time.sleep(SLEEP_SEC)
            continue
        ds   = d.isoformat()
        year = d.year
        log.info("%s  %d stocks  TV: HKD %s",
                 ds, len(records), f"{sum(r['tv'] for r in records.values()):,.0f}")
        if not dry_run:
            by_year.setdefault(year, {})[ds] = records
        ok += 1
        time.sleep(SLEEP_SEC)

    # Write once per year
    if not dry_run:
        for year, new_dates in by_year.items():
            lib = _load(year)
            lib["by_date"].update(new_dates)
            _save(year, lib)
    else:
        log.info("[dry-run] would write %d year file(s)",
                 len({d.year for d in days}))

    log.info("Done. Saved=%d  Failed=%d", ok, failed)

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HKEX turnover library builder")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview without writing")
    ap.add_argument("--rebuild", action="store_true",
                    help="Re-fetch all dates even if already in library")
    ap.add_argument("--date",    metavar="YYMMDD",
                    help="Rebuild a single date, e.g. 260326")
    ap.add_argument("--from",    dest="from_date", metavar="YYMMDD",
                    help="Override start date, e.g. 260201")
    args = ap.parse_args()

    def _d(s): return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:]))

    if args.date:
        d = _d(args.date)
        build(d, d, dry_run=args.dry_run, rebuild=True)
    else:
        start = _d(args.from_date) if args.from_date else START_DATE
        build(start, date.today(), dry_run=args.dry_run, rebuild=args.rebuild)
