"""
di_library.py — HKEx Disclosure of Interests (DI) Library Builder
===================================================================
Scrapes substantial shareholder (≥5%) filing notices from the HKEx DI system
for stocks in ccass_universe, rolling 2-year window to today.

Source: https://di.hkex.com.hk/filing/di/NSAllFormList.aspx
Forms:  Form 1 (individual SS), Form 2 (corporate SS), Form 3A (director)

Library files: di_{YYYY}.json  — one per year, same pattern as sfc_library.py

Structure:
{
  "meta": {"year": 2026, "last_updated": "...", "total_records": N, "stocks": N},
  "by_stock": {
    "00700": [
      {
        "date":         "2026-03-14",      # event date
        "form":         "Form 2",
        "shareholder":  "Prosus N.V.",
        "reason":       "S",
        "reason_zh":    "減持",
        "shares_delta": -5000000,
        "avg_price":    400.5,
        "shares_held":  320000000,
        "pct_held":     13.42,
        "ref":          "IS20260314E00123"
      }, ...
    ], ...
  }
}

Usage:
  python di_library.py                  # full build for all stocks in ccass_universe
  python di_library.py --update         # current year only (for daily-sync cron)
  python di_library.py --stock 00700    # single stock
  python di_library.py --query 00700    # print history
  python di_library.py --query 00700 --weeks 52
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ccass_universe import get_universe_codes, normalize_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

def _get_stock_codes() -> list[str]:
    """Return the stock universe — ccass_universe is the single source of truth."""
    try:
        codes = get_universe_codes()
        if codes:
            return codes
    except Exception as e:
        log.warning("Could not load universe from ccass_universe: %s", e)
    log.warning("Empty universe — returning empty list")
    return []

HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
               "Referer":   "https://di.hkex.com.hk/"}
BASE_URL   = "https://di.hkex.com.hk/filing/di/NSAllFormList.aspx"
START_DATE = date.today().replace(year=date.today().year - 2)  # rolling 2-year window
SLEEP_SEC  = 1.2
CACHE_DIR  = "di_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

REASON_MAP = {
    "P":"增持", "S":"減持", "A":"其他", "D":"股息",
    "I":"初次", "G":"贈與", "W":"認股", "B":"回購", "IP":"初次",
}


# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"di_{year}.json"

def all_years() -> list:
    return list(range(START_DATE.year, date.today().year + 1))

def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year}, "by_stock": {}}

def save_year(year: int, lib: dict):
    total = sum(len(v) for v in lib["by_stock"].values())
    lib["meta"] = {
        "year":          year,
        "last_updated":  date.today().isoformat(),
        "total_records": total,
        "stocks":        len(lib["by_stock"]),
    }
    with open(lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(lib_path(year)) / 1e6
    log.info("Saved di_%d.json: %d stocks, %d records, %.2f MB",
             year, len(lib["by_stock"]), total, mb)


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_path(code: str, year: int) -> str:
    return os.path.join(CACHE_DIR, f"di_{code}_{year}.json")

def _load_cache(code: str, year: int):
    p = _cache_path(code, year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None

def _save_cache(code: str, year: int, records: list):
    with open(_cache_path(code, year), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, separators=(",", ":"))


# ── Scraping ──────────────────────────────────────────────────────────────────

def _first_num(cell: str):
    """Extract the first number from a cell string. Returns float or None."""
    cell = cell.replace(",", "")
    nums = re.findall(r'[\-\+]?\d+(?:\.\d+)?', cell)
    return float(nums[0]) if nums else None


def _parse_table(soup) -> list[dict]:
    """Parse DI filing table from BeautifulSoup response."""
    records = []

    # Find the main results table
    table = None
    for t in soup.find_all("table"):
        txt = t.get_text()
        if any(k in txt for k in ["大股東", "Shareholder", "表格序號", "Form No"]):
            table = t
            break
    if not table:
        return records

    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue

        cells = [td.get_text(separator=" ", strip=True) for td in tds]
        cells = [re.sub(r'\s+', ' ', c).strip() for c in cells]

        # Extract filing ref from links
        ref = ""
        for a in tr.find_all("a", href=True):
            m = re.search(r'fn=([A-Z0-9]+)', a["href"])
            if m:
                ref = m.group(1)
                break

        # Find event date (DD/MM/YYYY format)
        event_date = None
        for cell in cells:
            m = re.search(r'(\d{2}/\d{2}/\d{4})', cell)
            if m:
                try:
                    event_date = datetime.strptime(m.group(1), "%d/%m/%Y").date().isoformat()
                    break
                except ValueError:
                    pass
        if not event_date:
            continue

        # Extract form type
        form_type = ""
        for cell in cells:
            m = re.search(r'(?:Form|表格)\s*(\d[A-Z]?)', cell, re.I)
            if m:
                form_type = f"Form {m.group(1)}"
                break

        # Shareholder name — longest text cell that isn't a date/number/reason
        shareholder = ""
        for cell in cells[1:4]:
            if (len(cell) > 4
                    and not re.match(r'^[\d,.\-\+%/\s]+$', cell)
                    and not re.search(r'\d{2}/\d{2}/\d{4}', cell)
                    and cell not in REASON_MAP
                    and "Form" not in cell):
                shareholder = cell[:100]
                break

        # Reason code
        reason = ""
        for cell in cells:
            if cell.strip() in REASON_MAP:
                reason = cell.strip()
                break

        # Numeric fields — parse all numbers from each cell
        shares_delta = None
        avg_price    = None
        shares_held  = None
        pct_held     = None

        for cell in cells:
            val = _first_num(cell)
            if val is None:
                continue
            if '%' in cell and pct_held is None:
                pct_held = round(val, 4)
            elif abs(val) >= 1e6 and shares_held is None:
                shares_held = int(val)
            elif abs(val) >= 1e4 and shares_delta is None and shares_held:
                shares_delta = int(val)
            elif 0 < val < 5000 and avg_price is None:
                avg_price = round(val, 4)

        # Infer direction from reason
        if shares_delta and reason == "S" and shares_delta > 0:
            shares_delta = -shares_delta

        records.append({
            "date":         event_date,
            "form":         form_type,
            "shareholder":  shareholder,
            "reason":       reason,
            "reason_zh":    REASON_MAP.get(reason, "其他"),
            "shares_delta": shares_delta,
            "avg_price":    avg_price,
            "shares_held":  shares_held,
            "pct_held":     pct_held,
            "ref":          ref,
        })

    return records


def fetch_di_stock_year(code: str, year: int) -> list[dict]:
    """Fetch all DI pages for one stock in one calendar year."""
    code5  = normalize_code(code)
    code_4 = str(int(code5)).zfill(4)
    y_start = max(date(year, 1, 1), START_DATE)
    y_end   = min(date(year, 12, 31), date.today())
    if y_start > y_end:
        return []

    sd = y_start.strftime("%d/%m/%Y")
    ed = y_end.strftime("%d/%m/%Y")

    all_records = []
    page = 1

    while True:
        params = {
            "sa2":  "an", "sd":   sd, "ed":   ed,
            "cid":  "0",  "sa1":  "cl",
            "scsd": sd,   "sced": ed,
            "sc":   code_4, "src": "MAIN",
            "lang": "ZH", "pg":  str(page),
        }
        try:
            resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.warning("  %s year=%d page=%d failed: %s", code, year, page, e)
            break

        soup   = BeautifulSoup(resp.text, "html.parser")
        rows   = _parse_table(soup)
        all_records.extend(rows)

        # Pagination: check for next page link
        has_next = bool(soup.find("a", string=re.compile(r'下一頁|Next page|›|\>')))
        if not has_next or len(rows) == 0:
            break
        page += 1
        time.sleep(SLEEP_SEC)

    return all_records


# ── Build ─────────────────────────────────────────────────────────────────────

def build(codes: list = None, update_only: bool = False):
    codes = [normalize_code(c) for c in (codes or _get_stock_codes())]
    if not codes:
        log.error("Empty stock universe — aborting")
        return
    years = all_years()
    today = date.today().year

    log.info("DI library: %d stocks, %d years", len(codes), len(years))

    for year in years:
        lib = load_year(year)

        for i, code in enumerate(codes, 1):
            # Skip completed past years unless missing from lib
            if update_only and year < today and code in lib["by_stock"]:
                continue

            cached = _load_cache(code, year)
            # Use cache for past years; always re-fetch current year
            if cached is not None and year < today:
                records = cached
            else:
                log.info("[%d/%d] %s year=%d", i, len(codes), code, year)
                records = fetch_di_stock_year(code, year)
                _save_cache(code, year, records)
                time.sleep(SLEEP_SEC)

            lib["by_stock"][code] = records
            if records:
                log.info("  %s %d: %d filings", code, year, len(records))

        save_year(year, lib)

    # Print summary
    log.info("── File sizes ──")
    total_mb = 0
    for year in years:
        p = lib_path(year)
        if os.path.exists(p):
            mb = os.path.getsize(p) / 1e6
            total_mb += mb
            with open(p) as f:
                m = json.load(f).get("meta", {})
            log.info("  di_%d.json  %d stocks  %d records  %.2f MB",
                     year, m.get("stocks",0), m.get("total_records",0), mb)
    log.info("  Total: %.2f MB", total_mb)


# ── Query ─────────────────────────────────────────────────────────────────────

def query_stock(code: str, weeks: int = None):
    code5 = normalize_code(code)
    all_records = []
    for year in all_years():
        if not os.path.exists(lib_path(year)):
            continue
        with open(lib_path(year), encoding="utf-8") as f:
            lib = json.load(f)
        all_records.extend(lib.get("by_stock", {}).get(code5, []))

    if not all_records:
        print(f"No DI filings found for {code5}")
        return

    all_records.sort(key=lambda x: x["date"])
    if weeks:
        cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
        all_records = [r for r in all_records if r["date"] >= cutoff]

    name = code5
    print(f"\n{code5} — {name}  ({len(all_records)} filings)")
    print(f"{'Date':<12} {'Shareholder':<36} {'原因':<5} {'Δ Shares':>14} {'Held':>14} {'%':>7}")
    print("─" * 94)
    for r in all_records:
        delta = f"{r['shares_delta']:+,}" if r.get('shares_delta') else "—"
        held  = f"{r['shares_held']:,}"   if r.get('shares_held')  else "—"
        pct   = f"{r['pct_held']:.2f}%"   if r.get('pct_held')     else "—"
        name_ = (r.get('shareholder') or '—')[:35]
        print(f"{r['date']:<12} {name_:<36} {r['reason_zh']:<5} {delta:>14} {held:>14} {pct:>7}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HKEx DI Library")
    ap.add_argument("--update", action="store_true", help="Current year only")
    ap.add_argument("--stock",  metavar="CODE",      help="Single stock e.g. 00700")
    ap.add_argument("--query",  metavar="CODE",      help="Print filing history")
    ap.add_argument("--weeks",  type=int,            help="Limit query to last N weeks")
    args = ap.parse_args()

    if   args.query: query_stock(args.query, args.weeks)
    elif args.stock: build(codes=[args.stock], update_only=args.update)
    else:            build(update_only=args.update)
