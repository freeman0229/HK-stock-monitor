"""
hkex_backfill.py — Full-market backfill for turnover & short selling libraries
===============================================================================
Run from your project directory (same folder as turnover_YYYY.json, short_YYYY.json).

COLUMN SOURCES
--------------
Quotation section (top of d{YYMMDD}c.htm)
  Columns : 代號  NAME OF STOCK  股票名稱  CURR  PRV  BID  ASK  最高  最低  收市  成交股數  成交金額

  Stored per code in turnover_YYYY.json:
    name_en  ← NAME OF STOCK   (English name)
    name_zh  ← 股票名稱         (Chinese name)
    close    ← 收市             (closing price)
    vol      ← 成交股數         (shares traded)      ✓ confirmed source column
    tv       ← 成交金額         (HKD turnover)       ✓ confirmed source column
    high     ← 最高             (day high)
    low      ← 最低             (day low)

Short selling section (bottom of d{YYMMDD}c.htm)
  Header  : 上日低線調整賣空交易 / 上日短線賣空成交
  Columns : 代號  股票名稱  股數(SH)  金額($)

  Stored per code in short_YYYY.json  (for date D-1):
    name     ← 股票名稱   (Chinese name)
    sv       ← 股數(SH)   (short sell shares)       ✓ confirmed source column
    st       ← 金額($)    (short sell HKD amount)

NOTE: d{D}c.htm embeds the PREVIOUS day's short data.
      Fetching file for D saves short for D-1.
      The script applies this shift automatically.

USAGE
-----
    python hkex_backfill.py                          # fill all gaps
    python hkex_backfill.py --dry-run                # preview only
    python hkex_backfill.py --from 2026-02-02 --to 2026-03-27
    python hkex_backfill.py --force                  # overwrite existing
    python hkex_backfill.py --verify                 # print column map and exit
    python hkex_backfill.py --migrate-names          # add names to old records

Rate limit: 1.5 s/request.  ~38 files = ~1 min total.
"""

import os, re, sys, json, time, logging, argparse
import requests
from datetime import date, datetime, timedelta
from typing import Optional

try:
    import holidays as _hol
    _HK_HOLIDAYS = _hol.HongKong()
except ImportError:
    _HK_HOLIDAYS = set()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("hkex_backfill.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent":    "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":       "https://www.hkex.com.hk/",
    "Accept-Charset":"big5,utf-8;q=0.9,*;q=0.7",
}
SLEEP_SEC = 1.5


# -- Trading day helpers -------------------------------------------------------

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HK_HOLIDAYS

def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d

def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


# -- Library I/O --------------------------------------------------------------

def _tv_path(year: int) -> str:  return f"turnover_{year}.json"
def _sh_path(year: int) -> str:  return f"short_{year}.json"

def _load(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "by_date": {}}

def _save(path: str, lib: dict, year: int, kind: str):
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(path) / 1e6
    log.info("Saved %s [%s]: %d days  %.2f MB", path, kind, len(lib["by_date"]), mb)

def _stored_dates(path_fn) -> set:
    out = set()
    for year in range(2018, date.today().year + 1):
        p = path_fn(year)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out.update(json.load(f).get("by_date", {}).keys())
    return out

def _count_stocks(path: str, ds: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return len(json.load(f).get("by_date", {}).get(ds, {}))


# -- Fetch & decode ------------------------------------------------------------

def fetch_dayquot(d: date) -> Optional[str]:
    """Fetch d{YYMMDD}c.htm. Returns Big5-decoded text, or None on failure."""
    ds  = d.strftime("%y%m%d")
    url = f"https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{ds}c.htm"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 404:
            log.warning("404 -- %s not found", ds)
            return None
        resp.raise_for_status()
        try:
            text = resp.content.decode("big5", errors="replace")
            if not any("\u4e00" <= c <= "\u9fff" for c in text[:5000]):
                raise ValueError
        except Exception:
            text = resp.content.decode("latin-1", errors="replace")
            log.warning("latin-1 fallback for %s", ds)
        return text
    except Exception as e:
        log.error("Fetch failed %s: %s", ds, e)
        return None


def _get_pre(text: str) -> str:
    try:
        from bs4 import BeautifulSoup
        pre = BeautifulSoup(text, "html.parser").find("pre")
        return pre.get_text() if pre else text
    except Exception:
        return text


# -- Parse quotation section (top of file) ------------------------------------
#
# Column order (Pattern B - 12 columns):
#   代號  NAME OF STOCK  股票名稱  CURR  PRV  BID  ASK  最高  最低  收市  成交股數  成交金額
#   g1    g2             g3        skip  skip skip skip g4   g5   g6   g7         g8
#
# g7 = 成交股數 -> vol   (shares traded)
# g8 = 成交金額 -> tv    (HKD turnover)

_QUOTE_PAT = re.compile(
    r"^[\*\s]{0,5}(\d{1,5})\s+"            # g1  代號
    r"(\S[^\u3000\n]{1,22}?)\s{2,}"        # g2  NAME OF STOCK
    r"(.{1,30}?)\s*"                        # g3  股票名稱
    r"(?:HKD|USD|CNY|EUR|GBP)\s+"          # CURR (skip)
    r"[\d,.NA-]+\s+"                        # PRV  (skip)
    r"[\d,.NA-]+\s+"                        # BID  (skip)
    r"[\d,.NA-]+\s+"                        # ASK  (skip)
    r"([\d,.NA-]+)\s+"                      # g4  最高
    r"([\d,.NA-]+)\s+"                      # g5  最低
    r"([\d,.NA-]+)\s+"                      # g6  收市
    r"([\d,]+)\s+"                          # g7  成交股數 -> vol
    r"([\d,]+)\s*$"                         # g8  成交金額 -> tv
)

def _price(s: str) -> float:
    s = s.replace(",", "").strip()
    return float(s) if s not in ("NA", "--", "-", "", "N/A") else 0.0

def _has_cjk(s: str) -> bool:
    return bool(s) and any("\u4e00" <= c <= "\u9fff" for c in s)


def parse_quotation(text: str) -> dict:
    """
    Parse the quotation section of d{YYMMDD}c.htm.

    Returns {code5: record} for all codes 1-9999 with vol > 0.

    All source columns stored:
        name_en  <- NAME OF STOCK  (English)
        name_zh  <- 股票名稱        (Chinese; falls back to name_en)
        close    <- 收市
        vol      <- 成交股數        (shares traded)
        tv       <- 成交金額        (HKD turnover)
        high     <- 最高
        low      <- 最低
    """
    body = _get_pre(text)
    best = {}

    for line in body.splitlines():
        m = _QUOTE_PAT.match(line)
        if not m:
            continue

        code_int = int(m.group(1))
        if not (1 <= code_int <= 9999):
            continue

        code    = str(code_int).zfill(5)
        name_en = m.group(2).strip()
        name_zh = re.sub(r"[\u3000\uff20\uff64\s]+$", "", m.group(3)).strip()
        high    = _price(m.group(4))
        low     = _price(m.group(5))
        close   = _price(m.group(6))
        vol     = int(m.group(7).replace(",", ""))   # 成交股數 -> vol
        tv      = int(m.group(8).replace(",", ""))   # 成交金額 -> tv

        if vol <= 0:
            continue

        if not _has_cjk(name_zh):
            name_zh = name_en   # fall back if no Chinese characters present

        # If code appears more than once, keep the higher-volume record
        if code not in best or vol > best[code]["vol"]:
            best[code] = {
                "name_en": name_en,   # NAME OF STOCK
                "name_zh": name_zh,   # 股票名稱
                "close":   close,     # 收市
                "vol":     vol,       # 成交股數
                "tv":      tv,        # 成交金額
                "high":    high,      # 最高
                "low":     low,       # 最低
            }

    log.debug("parse_quotation: %d records", len(best))
    return best


# -- Parse short selling section (bottom of file) ------------------------------
#
# Section header examples (Big5-decoded):
#   上日低線調整賣空交易
#   上日短線賣空成交
#
# Column order:
#   代號  股票名稱  股數(SH)  金額($)
#   g1    g2        g3        g4
#
# g3 = 股數(SH) -> sv   (short sell shares)
# g4 = 金額($)  -> st   (short sell HKD amount)

_SHORT_HEADERS = [
    "上日低線調整賣空交易",
    "上日短線賣空成交",
    "賣空成交",
    "短線成交",
    "股數(SH)",
    "股數\uff08SH\uff09",     # fullwidth-bracket variant
]
_SHORT_STOPS = [
    "路權行使",
    "海外",
    "其他資料",
    "-- End --",
    "===",
]

# Short data lines: code (1-4 digits), name (>=2 trailing spaces), sv, st
_SHORT_LINE = re.compile(
    r"^\s{0,6}(\d{1,4})\s+"        # g1  代號  (1-9999)
    r"(.{2,40}?)\s{2,}"            # g2  股票名稱
    r"([0-9,]{3,})\s+"             # g3  股數(SH) -> sv
    r"([0-9,]{3,})\s*$"            # g4  金額($)  -> st
)


def parse_short_section(text: str) -> dict:
    """
    Parse the short-selling section of d{YYMMDD}c.htm.
    This section contains the PREVIOUS trading day's data.

    Returns {code5: record} for all codes 1-9999 with sv > 0.

    All source columns stored:
        name  <- 股票名稱  (Chinese name)
        sv    <- 股數(SH)  (short sell shares)
        st    <- 金額($)   (short sell HKD amount)
    """
    body   = _get_pre(text)
    lines  = body.splitlines()
    in_sec = False
    out    = {}

    for i, line in enumerate(lines):
        if not in_sec:
            if any(h in line for h in _SHORT_HEADERS):
                in_sec = True
                log.debug("Short section start line %d: %s", i, line.strip()[:70])
            continue

        if any(s in line for s in _SHORT_STOPS):
            log.debug("Short section stop line %d: %s", i, line.strip()[:70])
            break

        stripped = line.strip()
        if not stripped or stripped.startswith("代號") or stripped.startswith("CODE"):
            continue

        m = _SHORT_LINE.match(line)
        if not m:
            continue

        code_int = int(m.group(1))
        if not (1 <= code_int <= 9999):
            continue

        code = str(code_int).zfill(5)
        name = m.group(2).strip()                    # 股票名稱
        sv   = int(m.group(3).replace(",", ""))      # 股數(SH) -> sv
        st   = float(m.group(4).replace(",", ""))    # 金額($)  -> st

        if sv <= 0:
            continue

        if code not in out or sv > out[code]["sv"]:
            out[code] = {
                "name": name,   # 股票名稱
                "sv":   sv,     # 股數(SH)
                "st":   st,     # 金額($)
            }

    if not out:
        log.warning(
            "parse_short_section: 0 records -- section header may not have matched. "
            "Check hkex_backfill.log and verify Big5 decoding."
        )
    else:
        log.debug("parse_short_section: %d records", len(out))
    return out


# -- Save helpers -------------------------------------------------------------

def save_tv_day(d: date, records: dict, force: bool = False):
    """
    Upsert turnover records for date d.
    ALL fields are stored: name_en, name_zh, close, vol, tv, high, low.
    Existing records are not overwritten unless force=True or new count is larger.
    """
    if not records:
        return
    ds   = d.isoformat()
    year = d.year
    path = _tv_path(year)
    lib  = _load(path)
    prev_n = len(lib["by_date"].get(ds, {}))

    if not force and prev_n >= len(records):
        log.info("TV %s: skip (existing %d >= new %d)", ds, prev_n, len(records))
        return

    lib["by_date"][ds] = records
    _save(path, lib, year, "turnover")
    log.info("TV %s: %d stocks  (was %d)", ds, len(records), prev_n)


def save_sh_day(d: date, records: dict, force: bool = False):
    """
    Upsert short-selling records for date d.
    ALL fields are stored: name, sv, st.
    """
    if not records:
        return
    ds   = d.isoformat()
    year = d.year
    path = _sh_path(year)
    lib  = _load(path)
    prev_n = len(lib["by_date"].get(ds, {}))

    if not force and prev_n >= len(records):
        log.info("SH %s: skip (existing %d >= new %d)", ds, prev_n, len(records))
        return

    lib["by_date"][ds] = records
    _save(path, lib, year, "short")
    log.info("SH %s: %d stocks  (was %d)", ds, len(records), prev_n)


# -- Backfill loop ------------------------------------------------------------

def backfill(start: date, end: date, force: bool = False, dry_run: bool = False):
    """
    For each trading day D in [start, end]:
      Fetch d{D}c.htm
      -> Parse quotation (top)    -> save TV for D
                                     fields: name_en, name_zh, close, vol, tv, high, low
      -> Parse short section (bot)-> save SH for D-1
                                     fields: name, sv, st

    Fetches one extra file past `end` to capture short data for the last day.
    """
    fetch_dates = []
    d = start
    while d <= end:
        if is_trading_day(d):
            fetch_dates.append(d)
        d += timedelta(days=1)
    fetch_dates.append(next_trading_day(end))   # extra for short of last day

    stored_tv = _stored_dates(_tv_path)
    stored_sh = _stored_dates(_sh_path)

    log.info("Backfill: %d files  %s -> %s (+1 extra for short)",
             len(fetch_dates), fetch_dates[0], fetch_dates[-2])
    log.info("Existing: TV=%d days  SH=%d days", len(stored_tv), len(stored_sh))

    tv_saved = sh_saved = errors = 0

    for i, fd in enumerate(fetch_dates):
        ds      = fd.isoformat()
        prev_ds = prev_trading_day(fd).isoformat()

        need_tv = force or (
            ds not in stored_tv or
            _count_stocks(_tv_path(fd.year), ds) < 100
        )
        need_sh = force or prev_ds not in stored_sh

        if not need_tv and not need_sh:
            log.info("[%d/%d] %s -- already complete", i + 1, len(fetch_dates), ds)
            continue

        tags = []
        if need_tv: tags.append(f"TV({ds})")
        if need_sh: tags.append(f"SH({prev_ds})")

        if dry_run:
            log.info("[DRY RUN] %d/%d  %s -> %s",
                     i + 1, len(fetch_dates), ds, "  ".join(tags))
            continue

        log.info("[%d/%d] Fetching %s  [%s]",
                 i + 1, len(fetch_dates), ds, "  ".join(tags))

        text = fetch_dayquot(fd)
        if text is None:
            log.error("Fetch failed -- %s skipped", ds)
            errors += 1
            time.sleep(SLEEP_SEC)
            continue

        if need_tv:
            recs = parse_quotation(text)
            if recs:
                save_tv_day(fd, recs, force=force)
                tv_saved += 1
            else:
                log.warning("TV %s: 0 records parsed", ds)

        if need_sh:
            recs = parse_short_section(text)
            if recs:
                save_sh_day(prev_trading_day(fd), recs, force=force)
                sh_saved += 1
            else:
                log.warning("SH %s: 0 records parsed from %s's file", prev_ds, ds)

        time.sleep(SLEEP_SEC)

    log.info("Done.  TV=%d  SH=%d  Errors=%d", tv_saved, sh_saved, errors)


# -- Migration: add names to existing numeric-only records --------------------

def migrate_names(dry_run: bool = False):
    """
    Existing records only have {tv, vol, high, low, close}.
    Re-fetches dates that are missing name_en and merges name fields in,
    without disturbing numeric values already stored.
    """
    log.info("=== migrate_names: scanning for records missing name_en ===")
    need = []

    for year in range(2018, date.today().year + 1):
        path = _tv_path(year)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            lib = json.load(f)
        for ds, day in lib["by_date"].items():
            sample = next(iter(day.values()), {})
            if isinstance(sample, dict) and "name_en" not in sample:
                need.append(date.fromisoformat(ds))

    if not need:
        log.info("All records already have name fields.")
        return

    log.info("%d dates need name migration", len(need))

    for i, d in enumerate(sorted(need)):
        ds = d.isoformat()
        if dry_run:
            log.info("[DRY RUN] Would migrate %s", ds)
            continue

        log.info("[%d/%d] Migrating %s", i + 1, len(need), ds)
        text = fetch_dayquot(d)
        if text is None:
            time.sleep(SLEEP_SEC)
            continue

        new_recs = parse_quotation(text)
        if not new_recs:
            time.sleep(SLEEP_SEC)
            continue

        path = _tv_path(d.year)
        lib  = _load(path)
        day  = lib["by_date"].get(ds, {})
        changed = 0
        for code, nr in new_recs.items():
            if code in day:
                if "name_en" not in day[code]:
                    day[code]["name_en"] = nr["name_en"]
                    day[code]["name_zh"] = nr["name_zh"]
                    changed += 1
            else:
                day[code] = nr
                changed += 1
        lib["by_date"][ds] = day
        _save(path, lib, d.year, "turnover (name migration)")
        log.info("  %s: %d records updated", ds, changed)
        time.sleep(SLEEP_SEC)


# -- Column map ---------------------------------------------------------------

def print_column_map():
    print()
    print("=" * 65)
    print("  COLUMN -> FIELD MAPPING  (hkex_backfill.py)")
    print("=" * 65)
    print()
    print("  QUOTATION SECTION  (top of d{YYMMDD}c.htm)")
    print("  -> turnover_YYYY.json")
    print()
    print("  Source column   Field     Note")
    print("  NAME OF STOCK   name_en   English name")
    print("  股票名稱          name_zh   Chinese name")
    print("  收市              close     Closing price")
    print("  成交股數          vol       Shares traded       [confirmed]")
    print("  成交金額          tv        HKD turnover amount [confirmed]")
    print("  最高              high      Day high")
    print("  最低              low       Day low")
    print()
    print("  SHORT SECTION  (bottom of d{YYMMDD}c.htm)")
    print("  Header: 上日低線調整賣空交易 / 上日短線賣空成交")
    print("  -> short_YYYY.json  (saved for date D-1)")
    print()
    print("  Source column   Field     Note")
    print("  股票名稱          name      Chinese name")
    print("  股數(SH)          sv        Short sell shares   [confirmed]")
    print("  金額($)           st        Short sell amount")
    print()
    print("  d{D}c.htm always contains short data for D-1.")
    print("  The 1-day shift is applied automatically.")
    print("=" * 65)
    print()


# -- CLI ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="HKEX full-market backfill -- turnover & short libraries"
    )
    ap.add_argument("--from",    dest="from_date", default=None,
                    help="Start YYYY-MM-DD (default: earliest in TV library)")
    ap.add_argument("--to",      dest="to_date",   default=None,
                    help="End YYYY-MM-DD (default: today)")
    ap.add_argument("--force",   action="store_true",
                    help="Overwrite existing records")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only, no writes")
    ap.add_argument("--verify",  action="store_true",
                    help="Print column map and exit")
    ap.add_argument("--migrate-names", action="store_true",
                    help="Add name_en/name_zh to existing records lacking them")
    args = ap.parse_args()

    print_column_map()

    if args.verify:
        return

    if args.migrate_names:
        migrate_names(dry_run=args.dry_run)
        return

    existing = _stored_dates(_tv_path)
    start = (date.fromisoformat(args.from_date) if args.from_date
             else (date.fromisoformat(min(existing)) if existing else date(2026, 2, 2)))
    end   = date.fromisoformat(args.to_date) if args.to_date else date.today()

    log.info("Range: %s -> %s   force=%s  dry_run=%s",
             start, end, args.force, args.dry_run)
    backfill(start, end, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
