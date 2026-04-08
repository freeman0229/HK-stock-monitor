"""
sfc_library.py — SFC Aggregated Reportable Short Positions Library
===================================================================
Fetches weekly SFC aggregated reportable short position data.

Source:
  https://www.sfc.hk/TC/Regulatory-functions/Market/Short-position-reporting/
  Aggregated-reportable-short-positions-of-specified-shares

Published: Tuesdays (data as of previous Friday close)
Schedule:  Saturday run in daily-sync.yml (picks up the week's report)
Storage:   sfc_{YYYY}.json — one per year

Structure:
{
  "meta": {"year": 2026, "last_updated": "...", "total_dates": N},
  "by_date": {
    "2026-03-14": {                            ← reporting date (Friday)
      "__total__": {"sh": 9876543210, "hkd": 987654321000.0},
      "00700": {"sh": 123456789, "hkd": 45678901234.0, "pct": 1.23, "name": "TENCENT"},
      ...
    }
  }
}

sh   = aggregated reportable short position (shares)
hkd  = aggregated reportable short position (HKD)
pct  = % of issued shares that are reported short
name = English stock name from SFC file

Usage:
  python sfc_library.py                  # full backfill from START_DATE
  python sfc_library.py --update         # only fetch missing dates
  python sfc_library.py --date 2026-03-14
  python sfc_library.py --query 00700

API:
  from sfc_library import get_short_position, get_position_history, get_total_history
"""

import os, json, re, time, logging, argparse, io
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

START_DATE  = date(2025, 3, 21)
CACHE_DIR   = "sfc_cache"
SLEEP_SEC   = 2.0

SFC_PAGE_TC = (
    "https://www.sfc.hk/TC/Regulatory-functions/Market/Short-position-reporting/"
    "Aggregated-reportable-short-positions-of-specified-shares"
)
SFC_PAGE_EN = (
    "https://www.sfc.hk/en/Regulatory-functions/Market/Short-position-reporting/"
    "Aggregated-reportable-short-positions-of-specified-shares"
)

# SFC serves static Excel files. Known URL patterns (try in order):
# Pattern A: direct CDN path with date in filename
# Pattern B: older static hosting
_EXCEL_URL_PATTERNS = [
    "https://www.sfc.hk/TC/data/short-position/AggregatedShortPos_{date}.xlsx",
    "https://www.sfc.hk/TC/data/short-position/aggregated/AggregatedShortPos_{date}.xlsx",
    "https://www.sfc.hk/en/data/short-position/AggregatedShortPos_{date}.xlsx",
    "https://www.sfc.hk/en/data/short-position/aggregated/AggregatedShortPos_{date}.xlsx",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.sfc.hk/",
    "Accept":     "text/html,application/xhtml+xml,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
}

os.makedirs(CACHE_DIR, exist_ok=True)

# ── Schedule helpers ──────────────────────────────────────────────────────────

def _prev_friday(ref: date = None) -> date:
    """Most recent Friday on or before ref."""
    ref = ref or date.today()
    return ref - timedelta(days=(ref.weekday() - 4) % 7)

def all_report_fridays(up_to: date = None) -> list[date]:
    """All Fridays from START_DATE up to up_to (inclusive)."""
    up_to  = up_to or date.today()
    result = []
    d = START_DATE
    while d.weekday() != 4:          # advance to first Friday
        d += timedelta(days=1)
    while d <= up_to:
        result.append(d)
        d += timedelta(weeks=1)
    return result

# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"sfc_{year}.json"

def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year}, "by_date": {}}

def save_year(year: int, lib: dict):
    n = len(lib["by_date"])
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_dates":  n,
    }
    with open(lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    kb = os.path.getsize(lib_path(year)) / 1024
    log.info("Saved sfc_%d.json  %d dates  %.0f KB", year, n, kb)

def all_stored_dates() -> set:
    stored = set()
    for year in range(START_DATE.year, date.today().year + 1):
        p = lib_path(year)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                stored.update(json.load(f).get("by_date", {}).keys())
    return stored

# ── Page scrape: discover Excel download links ────────────────────────────────

def _scrape_excel_links() -> list[str]:
    """
    Scrape the SFC TC/EN short position page for .xlsx / .xls download hrefs.
    Returns a list of absolute URLs.
    """
    links = []
    for url in (SFC_PAGE_TC, SFC_PAGE_EN):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.search(r"\.(xlsx|xls|csv)(\?|$)", href, re.I):
                    if href.startswith("http"):
                        links.append(href)
                    elif href.startswith("/"):
                        links.append("https://www.sfc.hk" + href)
            if links:
                log.info("Page scrape found %d Excel links from %s", len(links), url)
                return links
        except Exception as e:
            log.warning("Page scrape failed for %s: %s", url, e)
    return links

# ── Excel download & parse ────────────────────────────────────────────────────

def _download_excel(report_date: date) -> bytes | None:
    """
    Try to download the Excel file for report_date.
    1. Check local cache.
    2. Try known URL patterns.
    3. Try scraped links that contain the date string.
    Returns raw bytes or None.
    """
    ds_nodash = report_date.strftime("%Y%m%d")
    cache_file = os.path.join(CACHE_DIR, f"sfc_{ds_nodash}.xlsx")

    # Cache hit
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return f.read()

    # Try known URL patterns
    for pat in _EXCEL_URL_PATTERNS:
        url = pat.format(date=ds_nodash)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                with open(cache_file, "wb") as f:
                    f.write(r.content)
                log.info("Downloaded %s (%d bytes) from %s", ds_nodash, len(r.content), url)
                return r.content
        except Exception:
            pass

    # Try scraped links
    scraped = _scrape_excel_links()
    for link in scraped:
        if ds_nodash in link or report_date.strftime("%d%m%Y") in link:
            try:
                r = requests.get(link, headers=HEADERS, timeout=30)
                if r.status_code == 200 and len(r.content) > 1000:
                    with open(cache_file, "wb") as f:
                        f.write(r.content)
                    log.info("Downloaded %s via scraped link %s", ds_nodash, link)
                    return r.content
            except Exception:
                pass

    log.warning("Could not download Excel for %s", report_date.isoformat())
    return None

def _parse_excel(data: bytes, report_date: date) -> dict | None:
    """
    Parse SFC aggregated short position Excel file.
    Handles various SFC Excel layouts.

    Expected columns (flexible column detection):
      Stock Code | Stock Name | Short Position (Shares) | Short Position (HKD)
      [optional] % of Issued Shares

    Returns {code5: {sh, hkd, pct, name}, "__total__": {sh, hkd}} or None.
    """
    try:
        import openpyxl
    except ImportError:
        log.error("openpyxl not installed — run: pip install openpyxl")
        return None

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active

        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        log.error("Failed to open Excel: %s", e)
        return None

    # ── Locate header row ─────────────────────────────────────────────────────
    # Look for a row where one cell contains 'Code' or '代號' or 'Shares'
    header_idx = None
    col_code = col_name = col_sh = col_hkd = col_pct = None

    def _is_hkd_col(c: str) -> bool:
        # SFC Excel sometimes uses fullwidth U+FF04 ＄ instead of ASCII $
        # which makes the naive "hk$" check silently fail → hkd stored as 0
        cn = c.replace("＄", "$")
        return any(kw in cn for kw in ("hk$", "hkd", "港元", "金額", "value", "amount"))

    def _is_sh_col(c: str) -> bool:
        return ("share" in c or "股數" in c or "淡倉" in c) and not _is_hkd_col(c)

    for i, row in enumerate(rows):
        cells = [str(c).lower() if c is not None else "" for c in row]
        combined = " ".join(cells)
        if (("code" in combined or "代號" in combined or "股份代號" in combined)
                and ("share" in combined or "股數" in combined or "淡倉" in combined)):
            header_idx = i
            for j, c in enumerate(cells):
                if ("code" in c or "代號" in c) and col_code is None:
                    col_code = j
                elif ("name" in c or "名稱" in c) and col_name is None:
                    col_name = j
                elif _is_sh_col(c) and col_sh is None:
                    col_sh = j
                elif _is_hkd_col(c) and col_hkd is None:
                    col_hkd = j
                elif ("%" in c or "percent" in c or "issued" in c
                      or "已發行" in c or "百分比" in c) and col_pct is None:
                    col_pct = j
            break

    if header_idx is None:
        # Fallback: try to auto-detect from data shape
        # Assume first 4 columns: code, name, shares, hkd
        for i, row in enumerate(rows):
            if row and row[0] is not None:
                v = str(row[0]).strip()
                if re.match(r"^\d{4,5}$", v):
                    header_idx = i - 1
                    col_code, col_name, col_sh, col_hkd = 0, 1, 2, 3
                    log.warning("Header not found; auto-detected data start at row %d", i)
                    break

    if header_idx is None:
        log.error("Cannot find header row in Excel file for %s", report_date.isoformat())
        return None

    log.info("Excel header at row %d: code=%s name=%s sh=%s hkd=%s pct=%s",
             header_idx, col_code, col_name, col_sh, col_hkd, col_pct)

    # ── Parse data rows ───────────────────────────────────────────────────────
    def to_num(v) -> float:
        if v is None:
            return 0.0
        try:
            return float(str(v).replace(",", "").replace(" ", ""))
        except Exception:
            return 0.0

    result = {}
    total_sh = 0.0
    total_hkd = 0.0

    for row in rows[header_idx + 1:]:
        if not row or row[col_code] is None:
            continue
        raw_code = str(row[col_code]).strip().lstrip("0")
        if not raw_code.isdigit():
            continue
        code_int = int(raw_code)
        if code_int < 1 or code_int > 9999:   # skip warrants / invalid
            continue
        code5 = str(code_int).zfill(5)

        sh   = to_num(row[col_sh])  if col_sh  is not None else 0.0
        hkd  = to_num(row[col_hkd]) if col_hkd is not None else 0.0
        pct  = to_num(row[col_pct]) if col_pct is not None else 0.0
        name = str(row[col_name]).strip() if col_name is not None and row[col_name] else ""

        if sh <= 0 and hkd <= 0:
            continue

        result[code5] = {"sh": int(sh), "hkd": round(hkd, 2), "pct": round(pct, 4), "name": name}
        total_sh  += sh
        total_hkd += hkd

    if not result:
        log.warning("No valid rows parsed from Excel for %s", report_date.isoformat())
        return None

    result["__total__"] = {"sh": int(total_sh), "hkd": round(total_hkd, 2)}
    log.info("Parsed %d stocks for %s (total HKD %.2fbn)",
             len(result) - 1, report_date.isoformat(), total_hkd / 1e9)
    return result

# ── Build / update ────────────────────────────────────────────────────────────

def reparse(specific_date: date = None):
    """
    Re-parse all (or one) already-cached Excel files from sfc_cache/ and
    overwrite the stored JSON records. Use this after fixing column-detection
    logic to repair existing files without re-downloading anything.
    """
    dates = [specific_date] if specific_date else all_report_fridays()
    reparsed = no_cache = parse_fail = 0
    for d in dates:
        cache_file = os.path.join(CACHE_DIR, f"sfc_{d.strftime('%Y%m%d')}.xlsx")
        if not os.path.exists(cache_file):
            no_cache += 1
            continue
        with open(cache_file, "rb") as f:
            raw = f.read()
        records = _parse_excel(raw, d)
        if not records:
            log.warning("Re-parse failed for %s", d.isoformat())
            parse_fail += 1
            continue
        lib = load_year(d.year)
        lib["by_date"][d.isoformat()] = records
        save_year(d.year, lib)
        reparsed += 1
        total_hkd = records.get("__total__", {}).get("hkd", 0)
        log.info("Re-parsed %s -> %d stocks  total HKD %.2fbn",
                 d.isoformat(), len(records) - 1, total_hkd / 1e9)
    log.info("Reparse done: %d reparsed | %d no cache | %d failed",
             reparsed, no_cache, parse_fail)

def build(update_only: bool = False, specific_date: date = None):
    if specific_date:
        dates_to_fetch = [specific_date]
    else:
        all_dates = all_report_fridays()
        stored    = all_stored_dates()
        dates_to_fetch = [d for d in all_dates if d.isoformat() not in stored]
        log.info("%s: %d dates to fetch (%d already stored, %d total)",
                 "Update" if update_only else "Build",
                 len(dates_to_fetch), len(stored), len(all_dates))

    if not dates_to_fetch:
        log.info("Already up to date.")
        return

    # Scrape page once to cache link list
    scraped_links = _scrape_excel_links()
    if scraped_links:
        log.info("Page scrape found %d candidate links", len(scraped_links))

    fetched = 0
    for di, d in enumerate(dates_to_fetch, 1):
        log.info("── [%d/%d] %s ──", di, len(dates_to_fetch), d.isoformat())
        raw = _download_excel(d)
        if not raw:
            time.sleep(SLEEP_SEC)
            continue

        records = _parse_excel(raw, d)
        if not records:
            time.sleep(SLEEP_SEC)
            continue

        lib = load_year(d.year)
        lib["by_date"][d.isoformat()] = records
        save_year(d.year, lib)
        fetched += 1
        time.sleep(SLEEP_SEC)

    log.info("Build complete: %d/%d dates fetched", fetched, len(dates_to_fetch))

# ── API ───────────────────────────────────────────────────────────────────────

def get_short_position(code: str, ds: str) -> dict:
    """Return {sh, hkd, pct, name} for stock on YYYY-MM-DD, or {}."""
    year = int(ds[:4])
    p    = lib_path(year)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("by_date", {}).get(ds, {}).get(code.zfill(5), {})

def get_position_history(code: str, n: int, before: str) -> list:
    """Last n weekly snapshots before `before` (YYYY-MM-DD), newest-first.
    Returns [{"date": ds, "sh": N, "hkd": N, "pct": N}, ...]"""
    code5  = code.zfill(5)
    result = []
    for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before:
                continue
            rec = by_date[ds].get(code5)
            if rec:
                result.append({"date": ds, **rec})
            if len(result) >= n:
                return result
    return result

def get_total_history(n: int, before: str) -> list:
    """Last n weekly market totals before `before`, newest-first.
    Returns [{"date": ds, "sh": N, "hkd": N}, ...]"""
    result = []
    for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before:
                continue
            total = by_date[ds].get("__total__")
            if total:
                result.append({"date": ds, **total})
            if len(result) >= n:
                return result
    return result

# ── CLI ───────────────────────────────────────────────────────────────────────

def _query(code: str, top: int):
    code5  = code.zfill(5)
    hist   = get_position_history(code5, top,
                                   (date.today() + timedelta(1)).isoformat())
    if not hist:
        print(f"No data for {code5}"); return
    print(f"\n{code5}  ({len(hist)} weeks)")
    print(f"{'Date':<12} {'Shares':>16} {'HKD':>20} {'%':>8}")
    print("─" * 60)
    for h in hist:
        print(f"{h['date']:<12} {h['sh']:>16,} {h['hkd']:>20,.0f} {h.get('pct',0):>7.2f}%")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--update",  action="store_true", help="Only fetch missing dates")
    ap.add_argument("--reparse", action="store_true", help="Re-parse cached Excel files (fixes col detection bugs)")
    ap.add_argument("--inspect", action="store_true", help="Print stored totals — verify hkd != 0")
    ap.add_argument("--date",    metavar="YYYY-MM-DD", help="Target one specific date")
    ap.add_argument("--query",   metavar="CODE",       help="Show stored position history")
    ap.add_argument("--top",     type=int, default=20)
    args = ap.parse_args()

    if args.query:
        _query(args.query, args.top)
    elif args.reparse:
        reparse(specific_date=date.fromisoformat(args.date) if args.date else None)
    elif args.inspect:
        for year in range(START_DATE.year, date.today().year + 1):
            p = lib_path(year)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                by_date = json.load(f).get("by_date", {})
            print(f"\n-- sfc_{year}.json ({len(by_date)} dates) --")
            print(f"{'Date':<12} {'Total HKD':>20} {'Total Sh':>16} {'Stocks':>7}")
            print("-" * 60)
            for ds in sorted(by_date.keys()):
                t      = by_date[ds].get("__total__", {})
                stocks = len([k for k in by_date[ds] if k != "__total__"])
                hkd    = t.get("hkd", 0)
                sh     = t.get("sh",  0)
                flag   = "  <- HKD=0 !" if hkd == 0 else ""
                print(f"{ds:<12} {hkd:>20,.0f} {sh:>16,} {stocks:>7}{flag}")
    else:
        build(update_only=args.update,
              specific_date=date.fromisoformat(args.date) if args.date else None)
