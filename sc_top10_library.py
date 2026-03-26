"""
sc_top10_library.py — HKEX Stock Connect Southbound Top 10 Library
===================================================================
Fetches daily top 10 most actively traded stocks by southbound investors
from HKEX Stock Connect Historical Daily statistics.

Source URL pattern:
  https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_{YYYYMMDD}c.js

Data: SSE Southbound + SZSE Southbound top 10 each (combined up to 20 unique stocks)

Library files: sc_top10_{YYYY}.json — one per year

Structure:
{
  "meta": {"year": 2026, "last_updated": "...", "total_days": N},
  "by_date": {
    "2026-03-04": {
      "sse_summary":  {"total": 89251.46, "buy": 44080.13, "sell": 45171.33,
                       "trades": 1658014, "etf": 2626.27},
      "szse_summary": {"total": 52874.61, "buy": 26749.89, "sell": 26124.72,
                       "trades":  990929, "etf": 1661.55},
      "top10": [
        {"code": "09988", "name": "阿里巴巴－Ｗ",
         "sse_buy": 2752383420, "sse_sell": 4389977600, "sse_total": 7142361020,
         "szse_buy": 1983633040, "szse_sell": 3805295831, "szse_total": 5788928871,
         "buy": 4736016460, "sell": 8195273431, "total": 12931289891,
         "rank_sse": 1, "rank_szse": 1},
        ...
      ]
    }
  }
}

Usage:
  python sc_top10_library.py              # full build from START_DATE to today
  python sc_top10_library.py --update     # only new dates
  python sc_top10_library.py --query 2026-03-04
  python sc_top10_library.py --export     # export all to CSV
"""

import os, sys, json, time, re, logging, argparse
import requests
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_URL   = "https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_{date}c.js"
START_DATE = date(2025, 9, 1)    # SC top10 data available from Sep 2025
SLEEP_SEC  = 1.2
CACHE_DIR  = "sc_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Historical-Daily?sc_lang=zh-HK",
}

# ── Trading day helpers ───────────────────────────────────────────────────────

try:
    import holidays as _hol
    _HK = _hol.HongKong()
except Exception:
    _HK = set()

# Mainland China public holidays — Stock Connect is closed when EITHER
# exchange is on holiday.
_CN_HOLIDAY_DATES = {
    "2024-01-01","2024-02-12","2024-02-13","2024-02-14","2024-02-15","2024-02-16",
    "2024-04-04","2024-04-05","2024-05-01","2024-05-02","2024-05-03",
    "2024-06-10","2024-09-16","2024-09-17",
    "2024-10-01","2024-10-02","2024-10-03","2024-10-04","2024-10-07",
    "2025-01-01","2025-01-27","2025-01-28","2025-01-29","2025-01-30","2025-01-31",
    "2025-04-04","2025-05-01","2025-05-02","2025-05-05",
    "2025-06-02",
    "2025-10-01","2025-10-02","2025-10-03","2025-10-06","2025-10-07","2025-10-08",
    "2026-01-01","2026-01-28","2026-01-29","2026-01-30","2026-02-02","2026-02-03","2026-02-04",
    "2026-04-06","2026-05-01","2026-05-04","2026-05-05",
    "2026-06-19",
    "2026-10-01","2026-10-02","2026-10-05","2026-10-06","2026-10-07","2026-10-08",
}

try:
    _CN_LIB = _hol.China()
except Exception:
    _CN_LIB = set()

def _is_cn_holiday(d: date) -> bool:
    return d.isoformat() in _CN_HOLIDAY_DATES or d in _CN_LIB

def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d in _HK:
        return False
    if _is_cn_holiday(d):
        return False
    return True

def last_trading_day(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d

def all_trading_days(start: date, end: date) -> list:
    days, d = [], start
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"sc_top10_{year}.json"

def all_years() -> list:
    return list(range(START_DATE.year, date.today().year + 1))

def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year}, "by_date": {}}

def save_year(year: int, lib: dict):
    lib["meta"] = {
        "year":         year,
        "last_updated": date.today().isoformat(),
        "total_days":   len(lib["by_date"]),
    }
    with open(lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    mb = os.path.getsize(lib_path(year)) / 1e6
    log.info("Saved sc_top10_%d.json: %d days, %.2f MB",
             year, len(lib["by_date"]), mb)

def all_stored_dates() -> set:
    stored = set()
    for year in all_years():
        if os.path.exists(lib_path(year)):
            with open(lib_path(year), encoding="utf-8") as f:
                stored.update(json.load(f).get("by_date", {}).keys())
    return stored


# ── Parse ─────────────────────────────────────────────────────────────────────

def _to_f(s) -> float:
    try:    return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError, AttributeError): return 0.0

def _to_i(s) -> int:
    try:    return int(str(s).replace(",", "").strip())
    except (ValueError, TypeError, AttributeError): return 0

def _clean_name(s: str) -> str:
    return s.strip().rstrip("\u3000").strip()

def _parse_summary(table: dict) -> dict:
    schema = table.get("schema", [[]])[0]
    trs    = table.get("tr", [])
    vals   = [tr["td"][0][0] for tr in trs if tr.get("td")]
    result = {}
    for key, val in zip(schema, vals):
        k = key.lower().replace(" ", "_")
        result[k] = _to_i(val) if "count" in k or "dqb" in k else _to_f(val)
    return {
        "total":  result.get("total_turnover",   0.0),
        "buy":    result.get("buy_turnover",     0.0),
        "sell":   result.get("sell_turnover",    0.0),
        "trades": _to_i(str(int(result.get("total_trade_count", 0)))),
        "etf":    result.get("etf_turnover",     0.0),
    }

def _parse_top10(table: dict, is_southbound: bool) -> list:
    """
    Parse style:2 top10Table — per-stock rows.
    Defensive against HKEX returning row values as ints/floats instead of strings.
    """
    stocks  = []
    skipped = 0
    for tr in table.get("tr", []):
        tds = tr.get("td", [])
        if not tds:
            continue
        row = tds[0]
        try:
            if not is_southbound:
                if len(row) < 4:
                    continue
                rank = _to_i(row[0])
                if rank <= 0:
                    continue
                try:
                    code_int = int(float(str(row[1]).strip()))
                except (ValueError, TypeError):
                    skipped += 1
                    continue
                if code_int <= 0:
                    skipped += 1
                    continue
                stocks.append({
                    "rank":  rank,
                    "code":  str(code_int).zfill(6),
                    "name":  _clean_name(str(row[2])),
                    "total": _to_i(row[3]),
                })
            else:
                if len(row) < 6:
                    continue
                rank = _to_i(row[0])
                if rank <= 0:
                    continue
                try:
                    code_int = int(float(str(row[1]).strip()))
                except (ValueError, TypeError):
                    skipped += 1
                    continue
                if code_int <= 0:
                    skipped += 1
                    continue
                stocks.append({
                    "rank":  rank,
                    "code":  str(code_int).zfill(5),
                    "name":  _clean_name(str(row[2])),
                    "buy":   _to_i(row[3]),
                    "sell":  _to_i(row[4]),
                    "total": _to_i(row[5]),
                })
        except Exception as e:
            skipped += 1
            log.debug("_parse_top10: skipped row: %s", e)
    if skipped:
        log.warning("_parse_top10: skipped %d rows (format issues)", skipped)
    return stocks

def parse_js(text: str) -> dict | None:
    m = re.search(r"tabData\s*=\s*(\[.*\])", text, re.DOTALL)
    if not m:
        log.warning("tabData not found in JS")
        return None
    try:
        tab = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        log.warning("JSON parse error: %s", e)
        return None

    markets = {item["market"]: item for item in tab}
    result  = {}

    sse_sb     = markets.get("SSE Southbound")
    sse_stocks = {}
    if sse_sb:
        for content in sse_sb.get("content", []):
            tbl = content.get("table", {})
            if content["style"] == 1:
                result["sse_summary"] = _parse_summary(tbl)
            elif content["style"] == 2:
                for s in _parse_top10(tbl, is_southbound=True):
                    sse_stocks[s["code"]] = s

    szse_sb     = markets.get("SZSE Southbound")
    szse_stocks = {}
    if szse_sb:
        for content in szse_sb.get("content", []):
            tbl = content.get("table", {})
            if content["style"] == 1:
                result["szse_summary"] = _parse_summary(tbl)
            elif content["style"] == 2:
                for s in _parse_top10(tbl, is_southbound=True):
                    szse_stocks[s["code"]] = s

    # Merge SSE + SZSE keeping per-exchange breakdown
    merged = {}
    for s in sse_stocks.values():
        code = s["code"]
        merged[code] = {
            "code":       code,
            "name":       s["name"],
            "sse_buy":    s["buy"],
            "sse_sell":   s["sell"],
            "sse_total":  s["total"],
            "szse_buy":   0,
            "szse_sell":  0,
            "szse_total": 0,
            "buy":        s["buy"],
            "sell":       s["sell"],
            "total":      s["total"],
            "rank_sse":   s["rank"],
            "rank_szse":  0,
        }
    for s in szse_stocks.values():
        code = s["code"]
        if code not in merged:
            merged[code] = {
                "code":       code,
                "name":       s["name"],
                "sse_buy":    0,
                "sse_sell":   0,
                "sse_total":  0,
                "szse_buy":   s["buy"],
                "szse_sell":  s["sell"],
                "szse_total": s["total"],
                "buy":        s["buy"],
                "sell":       s["sell"],
                "total":      s["total"],
                "rank_sse":   0,
                "rank_szse":  s["rank"],
            }
        else:
            merged[code]["szse_buy"]   = s["buy"]
            merged[code]["szse_sell"]  = s["sell"]
            merged[code]["szse_total"] = s["total"]
            merged[code]["buy"]       += s["buy"]
            merged[code]["sell"]      += s["sell"]
            merged[code]["total"]     += s["total"]
            merged[code]["rank_szse"]  = s["rank"]

    n_sse, n_szse, n_merged = len(sse_stocks), len(szse_stocks), len(merged)
    if n_sse < 10:
        log.warning("parse_js: only %d SSE stocks (expected 10)", n_sse)
    if n_szse < 10:
        log.warning("parse_js: only %d SZSE stocks (expected 10)", n_szse)
    log.info("parse_js: SSE=%d SZSE=%d merged=%d (overlap=%d)",
             n_sse, n_szse, n_merged, n_sse + n_szse - n_merged)

    result["top10"] = sorted(merged.values(), key=lambda x: x["total"], reverse=True)
    return result


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _cache_path(d: date) -> str:
    return os.path.join(CACHE_DIR, f"sc_{d.strftime('%Y%m%d')}.json")

def fetch_day(d: date) -> dict | None:
    """Fetch and parse southbound top 10 data for one trading day."""
    cp = _cache_path(d)
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            raw = f.read()
        parsed = parse_js(raw)
        if parsed is not None:
            n = len(parsed.get("top10", []))
            if n >= 10:
                return parsed
            log.warning("  cache for %s has only %d stocks — re-fetching", d.isoformat(), n)
        else:
            log.info("  stale cache for %s — re-fetching", d.isoformat())
        os.remove(cp)

    url = BASE_URL.format(date=d.strftime("%Y%m%d"))
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 404:
            log.warning("  404: %s", url)
            return None
        r.raise_for_status()
        with open(cp, "w", encoding="utf-8") as f:
            f.write(r.text)
        return parse_js(r.text)
    except Exception as e:
        log.error("  fetch failed (%s): %s", d.isoformat(), e)
        return None


# ── Build / update ────────────────────────────────────────────────────────────

def _is_valid_top10(rec: dict) -> bool:
    top10 = rec.get("top10", [])
    if len(top10) < 10:
        return False
    valid_codes = [s for s in top10 if s.get("code", "").isdigit()]
    return len(valid_codes) >= 10

def _incomplete_dates() -> set:
    incomplete = set()
    for year in all_years():
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds, rec in by_date.items():
            if not _is_valid_top10(rec):
                incomplete.add(ds)
    if incomplete:
        log.info("Incomplete dates (< 10 valid stocks): %d — will re-fetch", len(incomplete))
    return incomplete

def build(update_only: bool = False):
    stored     = all_stored_dates()
    end        = last_trading_day(date.today())
    trading    = all_trading_days(last_trading_day(START_DATE), end)
    incomplete = _incomplete_dates()

    if update_only and stored:
        last     = date.fromisoformat(max(stored))
        new_days = [d for d in trading if d > last]
        repair   = [d for d in trading if d.isoformat() in incomplete]
        trading  = repair + new_days
        log.info("Update: %d new + %d incomplete to repair", len(new_days), len(repair))
    else:
        trading = [d for d in trading
                   if d.isoformat() not in stored or d.isoformat() in incomplete]
        log.info("Build: %d trading days to fetch", len(trading))

    if not trading:
        log.info("Already up to date")
        return

    by_year: dict = {}
    for d in trading:
        by_year.setdefault(d.year, []).append(d)

    missing    = []
    consec_404 = 0
    MAX_CONSEC = 30

    for year, days in sorted(by_year.items()):
        lib = load_year(year)
        log.info("── Year %d: %d days ──", year, len(days))
        for i, d in enumerate(days, 1):
            rec = fetch_day(d)
            if rec and _is_valid_top10(rec):
                lib["by_date"][d.isoformat()] = rec
                top   = rec.get("top10", [])
                names = ", ".join(s["code"] for s in top[:3])
                log.info("  [%d/%d] %s  %d stocks  top: %s",
                         i, len(days), d.isoformat(), len(top), names)
                consec_404 = 0
            elif rec:
                top = rec.get("top10", [])
                log.warning("  [%d/%d] %s  only %d stocks — not saved",
                            i, len(days), d.isoformat(), len(top))
                missing.append(d)
            else:
                missing.append(d)
                consec_404 += 1
                log.warning("  [%d/%d] %s  no data (%d consec)",
                            i, len(days), d.isoformat(), consec_404)
                if consec_404 >= MAX_CONSEC:
                    log.warning("  %d consecutive 404s — HKEX archive limit, stopping", MAX_CONSEC)
                    save_year(year, lib)
                    break
            time.sleep(SLEEP_SEC)
            if i % 20 == 0:
                save_year(year, lib)
        else:
            save_year(year, lib)
            continue
        save_year(year, lib)
        break

    log.info("── Done ──")
    if missing:
        log.warning("%d dates missing: %s%s",
                    len(missing),
                    [d.isoformat() for d in missing[:5]],
                    "..." if len(missing) > 5 else "")


# ── Query ─────────────────────────────────────────────────────────────────────

def query_date(ds: str):
    year = int(ds[:4])
    if not os.path.exists(lib_path(year)):
        print(f"No library for {year}")
        return
    with open(lib_path(year), encoding="utf-8") as f:
        rec = json.load(f).get("by_date", {}).get(ds)
    if not rec:
        print(f"No data for {ds}")
        return

    def fmt(n): return f"{n/1e8:>8.2f}億"

    ss = rec.get("sse_summary",  {})
    zs = rec.get("szse_summary", {})
    print(f"\n港股通十大活躍證券 — {ds}")
    print(f"{'':4} {'SSE 南向':>12}  {'SZSE 南向':>12}")
    print(f"  Total  {fmt(ss.get('total',0)*1e6)}  {fmt(zs.get('total',0)*1e6)}")
    print(f"  Buy    {fmt(ss.get('buy',0)*1e6)}  {fmt(zs.get('buy',0)*1e6)}")
    print(f"  Sell   {fmt(ss.get('sell',0)*1e6)}  {fmt(zs.get('sell',0)*1e6)}")
    print(f"\n{'Rank':<5} {'Code':<7} {'Name':<14} {'Buy':>12} {'Sell':>12} {'Total':>12} {'SSE':>4} {'SZ':>4}")
    print("─" * 72)
    for i, s in enumerate(rec.get("top10", []), 1):
        print(f"{i:<5} {s['code']:<7} {s['name']:<14} "
              f"{fmt(s['buy']):>12} {fmt(s['sell']):>12} {fmt(s['total']):>12} "
              f"{s.get('rank_sse',0):>4} {s.get('rank_szse',0):>4}")


# ── API for main.py ───────────────────────────────────────────────────────────

def get_top10(ds: str) -> list:
    """Return top10 list for a date string YYYY-MM-DD. Empty list if not found."""
    year = int(ds[:4])
    p    = lib_path(year)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        rec = json.load(f).get("by_date", {}).get(ds, {})
    return rec.get("top10", [])

def get_top10_codes(ds: str) -> set:
    return {s["code"] for s in get_top10(ds)}

def get_top10_history(code: str, n: int, before: str) -> list:
    """
    Return last n days where code appeared in top10, before date `before`.
    Each entry: {"date": "YYYY-MM-DD", "buy": int, "sell": int, "total": int}
    """
    code5  = code.zfill(5)
    result = []
    for year in sorted(all_years(), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before:
                continue
            for s in by_date[ds].get("top10", []):
                if s["code"] == code5:
                    result.append({"date": ds, "buy": s["buy"],
                                   "sell": s["sell"], "total": s["total"]})
                    break
            if len(result) >= n:
                return result
    return result


# ── Export ────────────────────────────────────────────────────────────────────

def export_csv():
    rows = []
    for year in all_years():
        if not os.path.exists(lib_path(year)):
            continue
        with open(lib_path(year), encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds, rec in sorted(by_date.items()):
            for s in rec.get("top10", []):
                rows.append({"date": ds, **s})
    if not rows:
        print("No data to export")
        return
    import csv
    path = "sc_top10_history.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"Exported {len(rows)} rows to {path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HKEX Southbound Top 10 Library")
    ap.add_argument("--update",      action="store_true",  help="Fetch new dates + repair incomplete")
    ap.add_argument("--query",       metavar="YYYY-MM-DD", help="Show data for a date")
    ap.add_argument("--export",      action="store_true",  help="Export all data to CSV")
    ap.add_argument("--clear-cache", action="store_true",  help="Delete sc_cache/ and rebuild")
    ap.add_argument("--reparse",     action="store_true",  help="Remove incomplete entries and re-fetch")
    args = ap.parse_args()

    if args.clear_cache:
        import shutil
        if os.path.exists(CACHE_DIR):
            shutil.rmtree(CACHE_DIR)
            os.makedirs(CACHE_DIR)
            log.info("sc_cache cleared")
    elif args.reparse:
        removed = 0
        for year in all_years():
            p = lib_path(year)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                lib = json.load(f)
            before = len(lib.get("by_date", {}))
            lib["by_date"] = {ds: rec for ds, rec in lib.get("by_date", {}).items()
                              if len(rec.get("top10", [])) >= 10}
            after = len(lib["by_date"])
            if before != after:
                save_year(year, lib)
                removed += before - after
                log.info("Year %d: removed %d incomplete, kept %d", year, before-after, after)
        log.info("Reparse: removed %d incomplete entries — run --update to re-fetch", removed)
        build(update_only=False)
    elif args.query:
        query_date(args.query)
    elif args.export:
        export_csv()
    else:
        build(update_only=args.update)
