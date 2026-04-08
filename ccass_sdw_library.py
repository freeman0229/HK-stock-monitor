"""
ccass_sdw_library.py — CCASS Per-Stock Participant Holdings Library
====================================================================
Fetches weekly CCASS participant-level shareholding for stocks with
turnover > 66,600,000 HKD.

Schedule: every Friday. If Friday is a HK public holiday → use Thursday.
          If both Thursday and Friday are holidays → skip that week.
Start:    2025-03-21

Source (holdings): https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx
Source (filter):   https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm

Summary values captured from page:
  total_sh   — 總數 (total CCASS-settled shares for this stock)
               Used as denominator for 累積沽空% = sfc_sh / total_sh * 100
  issued_sh  — 已發行股份/權證/單位 (最近更新數目)

Library files: ccass_sdw_{YYYY}.json — one per year

Schema v2 (current):
{
  "meta": {"year": 2026, ..., "schema_version": 2},
  "by_date": {
    "2026-03-19": {
      "00700": {
        "p":         [{pid, name, sh, pct}, ...],   sorted by sh desc
        "total_sh":  9234567890,
        "issued_sh": 9567000000
      }
    }
  }
}

Schema v1 (legacy): "00700": [{pid, name, sh, pct}, ...]  — all API reads handle both.

Usage:
  python ccass_sdw_library.py                    # full backfill
  python ccass_sdw_library.py --update           # only new dates
  python ccass_sdw_library.py --date 2026-03-19  # one date
  python ccass_sdw_library.py --query 00700
  python ccass_sdw_library.py --migrate          # upgrade files to v2

API:
  from ccass_sdw_library import get_holders, get_holders_history,
                                 get_total_sh, get_latest_total_sh, save_day
"""

import os, json, re, time, logging, argparse
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SDW_URL  = "https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx"
QUOT_URL = "https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{date}c.htm"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www3.hkexnews.hk/",
}
QUOT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkex.com.hk/",
}
START_DATE     = date(2025, 3, 21)
MIN_TURNOVER   = 66_600_000
SLEEP_SEC      = 1.5
SCHEMA_VERSION = 2

_HK_HOLIDAYS = {
    date(2025,4,4),  date(2025,4,18), date(2025,4,19), date(2025,4,21),
    date(2025,5,1),  date(2025,5,5),  date(2025,7,1),  date(2025,10,1),
    date(2025,10,7), date(2025,12,25),date(2025,12,26),
    date(2026,1,1),  date(2026,1,28), date(2026,1,29), date(2026,1,30),
    date(2026,2,2),  date(2026,2,3),  date(2026,2,4),
    date(2026,3,20), date(2026,4,3),  date(2026,4,6),
    date(2026,5,1),  date(2026,5,4),  date(2026,5,5),
    date(2026,6,19), date(2026,7,1),  date(2026,10,1),
    date(2026,10,5), date(2026,10,6), date(2026,10,7), date(2026,10,8),
    date(2026,12,25),date(2026,12,26),
}
try:
    import holidays as _hol
    _HK_HOLIDAYS = _HK_HOLIDAYS | set(_hol.HongKong())
except ImportError:
    pass


# ── Schedule ──────────────────────────────────────────────────────────────────

def weekly_fetch_date(friday: date) -> date | None:
    thu = friday - timedelta(days=1)
    if friday not in _HK_HOLIDAYS: return friday
    if thu    not in _HK_HOLIDAYS: return thu
    return None

def all_fetch_dates(up_to: date = None) -> list:
    up_to  = up_to or date.today()
    result = []
    d = START_DATE
    while d.weekday() != 4:
        d += timedelta(days=1)
    while d <= up_to:
        fd = weekly_fetch_date(d)
        if fd:
            result.append(fd)
        d += timedelta(weeks=1)
    return result


# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int) -> str:
    return f"ccass_sdw_{year}.json"

def load_year(year: int) -> dict:
    p = lib_path(year)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year, "schema_version": SCHEMA_VERSION}, "by_date": {}}

def save_year(year: int, lib: dict):
    n_dates  = len(lib["by_date"])
    n_stocks = sum(len(v) for v in lib["by_date"].values())
    lib["meta"] = {
        "year":           year,
        "last_updated":   date.today().isoformat(),
        "total_dates":    n_dates,
        "total_stocks":   n_stocks,
        "schema_version": SCHEMA_VERSION,
    }
    with open(lib_path(year), "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    kb = os.path.getsize(lib_path(year)) / 1024
    log.info("Saved ccass_sdw_%d.json  %d dates  %d stocks  %.0f KB",
             year, n_dates, n_stocks, kb)

def all_stored_dates() -> set:
    stored = set()
    for year in range(START_DATE.year, date.today().year + 1):
        p = lib_path(year)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                stored.update(json.load(f).get("by_date", {}).keys())
    return stored

def save_day(d: date, stock_code: str, entry: dict):
    """entry = {"p": [...], "total_sh": N, "issued_sh": N}"""
    if not entry:
        return
    ds  = d.isoformat()
    lib = load_year(d.year)
    lib["by_date"].setdefault(ds, {})[stock_code.zfill(5)] = entry
    save_year(d.year, lib)


# ── Schema normalisation ──────────────────────────────────────────────────────

def _to_v2(raw) -> dict:
    """Normalise v1 (flat list) or v2 (dict) to v2 dict."""
    if isinstance(raw, list):
        return {"p": raw, "total_sh": 0, "issued_sh": 0}
    if isinstance(raw, dict):
        return raw if "p" in raw else {"p": [], "total_sh": 0, "issued_sh": 0}
    return {"p": [], "total_sh": 0, "issued_sh": 0}

def migrate_schema():
    """Upgrade all ccass_sdw_YYYY.json files from v1 to v2. Safe to re-run."""
    for year in range(START_DATE.year, date.today().year + 1):
        p = lib_path(year)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            lib = json.load(f)
        if lib.get("meta", {}).get("schema_version", 1) >= SCHEMA_VERSION:
            log.info("ccass_sdw_%d.json already v%d — skip", year, SCHEMA_VERSION)
            continue
        changed = 0
        for ds, stocks in lib["by_date"].items():
            for code, raw in stocks.items():
                if isinstance(raw, list):
                    stocks[code] = {"p": raw, "total_sh": 0, "issued_sh": 0}
                    changed += 1
        save_year(year, lib)
        log.info("ccass_sdw_%d.json: %d entries → v2", year, changed)
    log.info("Migration complete")


# ── Turnover filter ───────────────────────────────────────────────────────────

def _last_trading_day(ref: date = None) -> date:
    """Return the most recent weekday that is not a HK public holiday."""
    d = ref or date.today()
    for _ in range(10):
        if d.weekday() < 5 and d not in _HK_HOLIDAYS:
            return d
        d -= timedelta(days=1)
    return ref or date.today()

def get_qualifying_stocks(ref_date: date = None) -> list:
    # Always use the most recent trading day so this works correctly when
    # called on weekends or public holidays (e.g. sdw-build triggered Saturday).
    d        = _last_trading_day(ref_date)
    date_str = d.strftime("%y%m%d")
    url      = QUOT_URL.format(date=date_str)
    log.info("Qualifying stocks: using quotation date %s", d.isoformat())
    try:
        r = requests.get(url, headers=QUOT_HEADERS, timeout=30)
        r.raise_for_status()
        try:
            text = r.content.decode("big5", errors="replace")
        except Exception:
            text = r.content.decode("latin-1", errors="replace")

        pre  = BeautifulSoup(text, "html.parser").find("pre")
        body = pre.get_text() if pre else text

        PAT = re.compile(
            r"^[\*\s]{0,5}(\d{1,5})\s+(\S[^\u3000\n]{1,22}?)\s{2,}"
            r"(.{1,30}?)\s*(?:HKD|USD|CNY|EUR|GBP)\s+"
            r"[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+[\d,.NA-]+\s+"
            r"[\d,]{5,}\s+"
            r"([\d,]{8,})\s*$"
        )
        best = {}
        for line in body.splitlines():
            m = PAT.match(line)
            if not m: continue
            code_int = int(m.group(1))
            if code_int > 9999: continue
            code = str(code_int).zfill(5)
            tv   = float(m.group(4).replace(",", ""))
            if tv > 0 and (code not in best or tv > best[code]):
                best[code] = tv
        codes = [c for c, tv in best.items() if tv >= MIN_TURNOVER]
        log.info("Qualifying stocks %s: %d (tv >= %s HKD)",
                 d.isoformat(), len(codes), f"{MIN_TURNOVER:,}")
        return codes

    except requests.HTTPError as e:
        log.warning("Quotation %s for %s — skipping", e.response.status_code, date_str)
        return []
    except Exception as e:
        log.error("get_qualifying_stocks (%s): %s", date_str, e)
        return []


# ── SDW fetch for one stock ───────────────────────────────────────────────────

def _parse_num(s: str) -> int:
    try:
        return int(str(s).replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return 0

def fetch_stock(stock_code: str, d: date) -> dict | None:
    """
    Fetch CCASS participant holdings for one stock on one date.
    Returns {"p": [...], "total_sh": N, "issued_sh": N} or None.

    total_sh  = 總數 (total CCASS-settled shares shown on page footer)
    issued_sh = 已發行股份/權證/單位 (最近更新數目) from page header
    """
    code5    = stock_code.zfill(5)
    date_str = d.strftime("%Y/%m/%d")
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)

        r1 = sess.get(SDW_URL, timeout=30)
        r1.raise_for_status()
        soup1 = BeautifulSoup(r1.text, "html.parser")

        def hv(name):
            tag = soup1.find("input", {"name": name})
            return tag["value"] if tag else ""

        r2 = sess.post(SDW_URL, data={
            "__EVENTTARGET":        "btnSearch",
            "__EVENTARGUMENT":      "",
            "__VIEWSTATE":          hv("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": hv("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION":    hv("__EVENTVALIDATION"),
            "txtShareholdingDate":  date_str,
            "txtStockCode":         code5,
            "txtParticipantID":     "",
            "txtParticipantName":   "",
        }, timeout=60)
        r2.raise_for_status()

        soup2     = BeautifulSoup(r2.text, "html.parser")
        full_text = soup2.get_text(" ", strip=True)

        # ── 已發行股份/權證/單位 (最近更新數目) ─────────────────────────────
        # Shown in the page header / stock summary section.
        issued_sh = 0
        for pat in [
            r"已發行股份[^\d]{0,30}([\d,]{6,})",
            r"Issued\s+Shares[^\d]{0,30}([\d,]{6,})",
            r"Number\s+of\s+Issued\s+Shares[^\d]{0,30}([\d,]{6,})",
        ]:
            m = re.search(pat, full_text)
            if m:
                issued_sh = _parse_num(m.group(1))
                if issued_sh > 0:
                    break

        # ── Participant rows ─────────────────────────────────────────────────
        def clean(s):
            return re.sub(r'^[^:：]+[:：]\s*', '', s).strip()

        participants  = []
        total_sh_fallback = 0

        for tr in soup2.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 5:
                continue
            pid_raw = clean(tds[0])
            sh_raw  = clean(tds[3]).replace(",", "")
            pct_raw = clean(tds[4]).replace("%", "").strip()
            if not pid_raw or not sh_raw.isdigit():
                continue
            if pid_raw.lower() in ("參與者編號", "id", "participant id"):
                continue
            try:
                sh = int(sh_raw)
                participants.append({
                    "pid":  pid_raw,
                    "name": clean(tds[1]),
                    "sh":   sh,
                    "pct":  float(pct_raw) if pct_raw else 0.0,
                })
                total_sh_fallback += sh
            except (ValueError, TypeError):
                continue

        # ── 總數 from page footer ────────────────────────────────────────────
        # The SDW page footer row typically has fewer tds (<5) so it was
        # skipped in the loop above. Search page text and then table rows.
        total_sh = 0

        # 1. Regex on full page text
        for pat in [
            r"總數[^\d]{0,20}([\d,]{6,})",
            r"Grand\s+Total[^\d]{0,20}([\d,]{6,})",
        ]:
            m = re.search(pat, full_text)
            if m:
                total_sh = _parse_num(m.group(1))
                if total_sh > 0:
                    break

        # 2. Scan all trs for a row containing 總數/Total text
        if total_sh == 0:
            for tr in soup2.find_all("tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                row_text = " ".join(tds)
                if "總數" in row_text or "Grand Total" in row_text:
                    for cell in reversed(tds):
                        num = _parse_num(cell)
                        if num > 1_000_000:
                            total_sh = num
                            break
                    if total_sh > 0:
                        break

        # 3. Fallback: sum of participant rows
        if total_sh == 0 and total_sh_fallback > 0:
            total_sh = total_sh_fallback
            log.warning("SDW %s %s: 總數 not found — using participant sum %d",
                        code5, date_str, total_sh)

        if not participants:
            log.warning("SDW: 0 records for %s on %s", code5, date_str)
            return None

        participants.sort(key=lambda x: -x["sh"])
        log.debug("SDW %s %s: %d participants  total_sh=%d  issued_sh=%d",
                  code5, date_str, len(participants), total_sh, issued_sh)

        return {"p": participants, "total_sh": total_sh, "issued_sh": issued_sh}

    except Exception as e:
        log.error("fetch_stock (%s %s): %s", code5, date_str, e)
        return None


# ── Build / update ────────────────────────────────────────────────────────────

def build(update_only: bool = False, specific_date: date = None):
    if specific_date:
        dates_to_fetch = [specific_date]
    else:
        all_dates = all_fetch_dates()
        stored    = all_stored_dates()
        dates_to_fetch = [d for d in all_dates if d.isoformat() not in stored]
        log.info("%s: %d dates to fetch (%d already stored, %d total in schedule)",
                 "Update" if update_only else "Build",
                 len(dates_to_fetch), len(stored), len(all_dates))

    if not dates_to_fetch:
        log.info("Already up to date")
        return

    codes = get_qualifying_stocks()
    if not codes:
        log.error("Could not get qualifying stocks — aborting")
        return
    log.info("Using %d qualifying stocks (tv >= %s) for all dates",
             len(codes), f"{MIN_TURNOVER:,}")

    for di, d in enumerate(dates_to_fetch, 1):
        log.info("-- [%d/%d] %s --", di, len(dates_to_fetch), d.isoformat())
        lib     = load_year(d.year)
        ds      = d.isoformat()
        already = set(lib["by_date"].get(ds, {}).keys())
        todo    = [c for c in codes if c not in already]
        log.info("  %d stocks to fetch (%d already stored)", len(todo), len(already))

        fetched = 0
        for ci, code in enumerate(todo, 1):
            entry = fetch_stock(code, d)
            if entry:
                lib["by_date"].setdefault(ds, {})[code] = entry
                fetched += 1
            time.sleep(SLEEP_SEC)
            if ci % 20 == 0:
                save_year(d.year, lib)
                log.info("    [%d/%d] %d saved so far", ci, len(todo), fetched)

        save_year(d.year, lib)
        log.info("  %s done: %d/%d stocks saved", ds, fetched, len(todo))

    log.info("Build complete")


# ── API ───────────────────────────────────────────────────────────────────────

def get_holders(stock_code: str, ds: str) -> list:
    """Return participant list [{pid, name, sh, pct}] or []. Handles v1 + v2."""
    year = int(ds[:4])
    p    = lib_path(year)
    if not os.path.exists(p): return []
    with open(p, encoding="utf-8") as f:
        raw = json.load(f).get("by_date", {}).get(ds, {}).get(stock_code.zfill(5))
    return _to_v2(raw)["p"] if raw is not None else []

def get_total_sh(stock_code: str, ds: str) -> int:
    """Return 總數 for a stock on YYYY-MM-DD, or 0."""
    year = int(ds[:4])
    p    = lib_path(year)
    if not os.path.exists(p): return 0
    with open(p, encoding="utf-8") as f:
        raw = json.load(f).get("by_date", {}).get(ds, {}).get(stock_code.zfill(5))
    return _to_v2(raw).get("total_sh", 0) if raw is not None else 0

def get_issued_sh(stock_code: str, ds: str) -> int:
    """Return 已發行股份 for a stock on YYYY-MM-DD, or 0."""
    year = int(ds[:4])
    p    = lib_path(year)
    if not os.path.exists(p): return 0
    with open(p, encoding="utf-8") as f:
        raw = json.load(f).get("by_date", {}).get(ds, {}).get(stock_code.zfill(5))
    return _to_v2(raw).get("issued_sh", 0) if raw is not None else 0

def get_latest_total_sh(stock_code: str, before: str = None) -> int:
    """Most recent 總數 for a stock (optionally before a date). Returns 0 if not found."""
    code5  = stock_code.zfill(5)
    cutoff = before or (date.today() + timedelta(days=1)).isoformat()
    for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p): continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= cutoff: continue
            raw = by_date[ds].get(code5)
            if raw is None: continue
            total_sh = _to_v2(raw).get("total_sh", 0)
            if total_sh > 0:
                return total_sh
    return 0

def get_holders_history(stock_code: str, n: int, before: str) -> list:
    """Last n weekly snapshots before `before`, newest-first.
    Returns [{"date": ds, "holders": [...], "total_sh": N, "issued_sh": N}, ...]"""
    code5  = stock_code.zfill(5)
    result = []
    for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
        p = lib_path(year)
        if not os.path.exists(p): continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before: continue
            raw = by_date[ds].get(code5)
            if raw is None: continue
            entry = _to_v2(raw)
            if entry["p"]:
                result.append({
                    "date":      ds,
                    "holders":   entry["p"],
                    "total_sh":  entry.get("total_sh",  0),
                    "issued_sh": entry.get("issued_sh", 0),
                })
            if len(result) >= n:
                return result
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _query(code: str, top: int, ds: str = None):
    code5 = code.zfill(5)
    if not ds:
        for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
            p = lib_path(year)
            if not os.path.exists(p): continue
            with open(p, encoding="utf-8") as f:
                by_date = json.load(f).get("by_date", {})
            dates = [d for d, s in by_date.items() if code5 in s]
            if dates: ds = max(dates); break
    if not ds:
        print(f"No data for {code5}"); return
    year = int(ds[:4])
    with open(lib_path(year), encoding="utf-8") as f:
        raw = json.load(f).get("by_date", {}).get(ds, {}).get(code5)
    if raw is None:
        print(f"No data for {code5} on {ds}"); return
    entry = _to_v2(raw)
    holders = entry["p"]
    print(f"\n{code5}  {ds}  ({len(holders)} participants)")
    print(f"  總數:       {entry.get('total_sh',  0):>20,}")
    print(f"  已發行股份: {entry.get('issued_sh', 0):>20,}")
    print(f"{'#':<4} {'ID':<12} {'Name':<40} {'Shares':>16} {'%':>8}")
    print("─" * 84)
    for i, h in enumerate(holders[:top], 1):
        print(f"{i:<4} {h['pid']:<12} {h['name'][:39]:<40} {h['sh']:>16,} {h['pct']:>7.2f}%")
    hist = get_holders_history(code5, 6, (date.today()+timedelta(1)).isoformat())
    if hist:
        print(f"\nAvailable dates: {[h['date'] for h in hist]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--update",  action="store_true")
    ap.add_argument("--date",    metavar="YYYY-MM-DD")
    ap.add_argument("--query",   metavar="CODE")
    ap.add_argument("--top",     type=int, default=20)
    ap.add_argument("--migrate", action="store_true", help="Upgrade all files to v2 schema")
    args = ap.parse_args()

    if args.migrate:
        migrate_schema()
    elif args.query:
        _query(args.query, args.top, args.date)
    else:
        build(update_only=args.update,
              specific_date=date.fromisoformat(args.date) if args.date else None)
