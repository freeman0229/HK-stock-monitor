"""
ccass_sdw_library.py — CCASS Per-Stock Participant Holdings Library
====================================================================
Fetches weekly CCASS participant-level shareholding for ALL stocks
listed in the SFC short-position universe.

Source (holdings):  https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx
Stock universe:     SFC short-position library (sfc_library.py)
                    ~1286 shortable HK stocks — updated weekly

Schedule: every Friday; Thursday fallback if Friday is a HK holiday.
          Both Thu+Fri holiday → skip week.
Start:    2025-03-23  (first trading Friday = 2025-03-28)

Library files split by stock code range — one set of 7 files per year:
  ccass_sdw_0001_1000_{YYYY}.json  — codes 00001–01000  (~237 stocks, ~15 MB/yr)
  ccass_sdw_1001_2000_{YYYY}.json  — codes 01001–02000  (~201 stocks, ~13 MB/yr)
  ccass_sdw_2001_3000_{YYYY}.json  — codes 02001–03000  (~239 stocks, ~15 MB/yr)
  ccass_sdw_3001_4000_{YYYY}.json  — codes 03001–04000  (~246 stocks, ~15 MB/yr)
  ccass_sdw_4001_7000_{YYYY}.json  — codes 04001–07000  (~77 stocks,   ~5 MB/yr)
  ccass_sdw_7001_9999_{YYYY}.json  — codes 07001–09999  (~201 stocks, ~13 MB/yr)
  ccass_sdw_10000plus_{YYYY}.json  — codes 10000+       (~85 stocks,   ~5 MB/yr)

Schema v2:
{
  "meta": {"year": 2026, "range": "0001_4000", "schema_version": 2, ...},
  "by_date": {
    "2026-03-21": {
      "00700": {
        "p":         [{"pid": "B01234", "name": "...", "sh": 1234567, "pct": 1.23}, ...],
        "total_sh":  9234567890,   ← 總數 (total CCASS-settled shares)
        "issued_sh": 9567000000    ← 已發行股份/權證/單位
      }
    }
  }
}

Fields per participant:
  pid      — 參與者編號 (Participant ID)
  name     — 中央結算系統參與者名稱
  sh       — 持股量 (shares held)
  pct      — 佔已發行股份/權證/單位百分比

Usage:
  python ccass_sdw_library.py                    # full backfill
  python ccass_sdw_library.py --update           # only new dates
  python ccass_sdw_library.py --date 2026-03-21  # one specific date
  python ccass_sdw_library.py --query 00700      # show holdings for a stock
  python ccass_sdw_library.py --migrate          # upgrade old files to range-split format

API:
  from ccass_sdw_library import get_holders, get_total_sh,
                                 get_latest_total_sh, get_holders_history
"""

import os, json, re, time, logging, argparse
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SDW_URL  = "https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www3.hkexnews.hk/",
}

START_DATE     = date(2025, 3, 23)   # first Friday = 2025-03-28
SLEEP_SEC      = 1.5
SCHEMA_VERSION = 2

# ── Code ranges ───────────────────────────────────────────────────────────────

RANGES = [
    ("0001_1000",    1,  1000),
    ("1001_2000", 1001,  2000),
    ("2001_3000", 2001,  3000),
    ("3001_4000", 3001,  4000),
    ("4001_7000", 4001,  7000),
    ("7001_9999", 7001,  9999),
    ("10000plus",10000, 99999),
]

def code_range(code: str) -> str:
    """Return the range label for a 5-digit code string."""
    n = int(code)
    for label, lo, hi in RANGES:
        if lo <= n <= hi:
            return label
    return "0001_1000"   # fallback

# ── HK holidays ───────────────────────────────────────────────────────────────

_HK_HOLIDAYS = {
    date(2025, 1, 1),  date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31),
    date(2025, 4, 4),  date(2025, 4, 18), date(2025, 4, 19), date(2025, 4, 21),
    date(2025, 5, 1),  date(2025, 5, 5),  date(2025, 6, 2),  date(2025, 7, 1),
    date(2025, 9, 30), date(2025, 10, 1), date(2025, 10, 29),
    date(2025, 12, 25),date(2025, 12, 26),
    date(2026, 1, 1),  date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30),
    date(2026, 2, 2),  date(2026, 2, 3),  date(2026, 2, 4),
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20),
    date(2026, 4, 3),  date(2026, 4, 4),  date(2026, 4, 5),  date(2026, 4, 6),
    date(2026, 5, 1),  date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 1),
    date(2026, 9, 7),  date(2026, 10, 1), date(2026, 10, 26),
    date(2026, 12, 25),date(2026, 12, 26),
}
try:
    import holidays as _hol
    _HK_HOLIDAYS = _HK_HOLIDAYS | set(_hol.HongKong())
except ImportError:
    pass


# ── Schedule ──────────────────────────────────────────────────────────────────

def _fetch_date_for_friday(friday: date) -> date | None:
    """Friday → actual fetch date (Thu fallback if Friday is holiday). None = skip week."""
    thu = friday - timedelta(days=1)
    if friday not in _HK_HOLIDAYS:
        return friday
    if thu not in _HK_HOLIDAYS:
        return thu
    return None

def all_fetch_dates(up_to: date = None) -> list[date]:
    """All scheduled weekly fetch dates from START_DATE to up_to."""
    up_to  = up_to or date.today()
    result = []
    d = START_DATE
    while d.weekday() != 4:          # advance to first Friday
        d += timedelta(days=1)
    while d <= up_to:
        fd = _fetch_date_for_friday(d)
        if fd:
            result.append(fd)
        d += timedelta(weeks=1)
    return result


# ── File I/O ──────────────────────────────────────────────────────────────────

def lib_path(year: int, range_label: str) -> str:
    return f"ccass_sdw_{range_label}_{year}.json"

def load_range(year: int, range_label: str) -> dict:
    p = lib_path(year, range_label)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {"year": year, "range": range_label,
                     "schema_version": SCHEMA_VERSION}, "by_date": {}}

def save_range(year: int, range_label: str, lib: dict):
    n_dates  = len(lib["by_date"])
    n_stocks = sum(len(v) for v in lib["by_date"].values())
    lib["meta"] = {
        "year":           year,
        "range":          range_label,
        "last_updated":   date.today().isoformat(),
        "total_dates":    n_dates,
        "total_stocks":   n_stocks,
        "schema_version": SCHEMA_VERSION,
    }
    p = lib_path(year, range_label)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    kb = os.path.getsize(p) / 1024
    log.info("Saved %s  %d dates  %d stocks  %.0f KB", p, n_dates, n_stocks, kb)

def all_stored_dates() -> set[str]:
    """Return all dates stored across any range file."""
    stored = set()
    for year in range(START_DATE.year, date.today().year + 1):
        for label, _, _ in RANGES:
            p = lib_path(year, label)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    stored.update(json.load(f).get("by_date", {}).keys())
    return stored

def _stored_codes_for_date(ds: str) -> set[str]:
    """Return all stock codes already stored for a given date across all range files."""
    year = int(ds[:4])
    codes = set()
    for label, _, _ in RANGES:
        p = lib_path(year, label)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            codes.update(json.load(f).get("by_date", {}).get(ds, {}).keys())
    return codes


# ── Schema normalisation ──────────────────────────────────────────────────────

def _to_v2(raw) -> dict:
    """Normalise v1 (flat list) or v2 (dict with p key) to v2 dict."""
    if isinstance(raw, list):
        return {"p": raw, "total_sh": 0, "issued_sh": 0}
    if isinstance(raw, dict):
        return raw if "p" in raw else {"p": [], "total_sh": 0, "issued_sh": 0}
    return {"p": [], "total_sh": 0, "issued_sh": 0}


# ── Stock universe from SFC library ──────────────────────────────────────────

def get_sfc_universe() -> list[str]:
    """
    Return sorted list of 5-digit stock codes from the SFC short-position library.
    Falls back to a broad range scan if SFC library is not available.
    """
    codes = set()

    # Try importing sfc_library
    try:
        from sfc_library import all_stored_dates as sfc_stored, lib_path as sfc_lib_path
        import json as _json
        for year in range(2018, date.today().year + 1):
            p = sfc_lib_path(year)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                by_date = _json.load(f).get("by_date", {})
            for ds, day in by_date.items():
                for code in day.keys():
                    if code != "__total__" and code.isdigit():
                        codes.add(code.zfill(5))
        if codes:
            log.info("SFC universe: %d unique codes from sfc_library", len(codes))
            return sorted(codes, key=lambda x: int(x))
    except Exception as e:
        log.warning("Could not load SFC universe from sfc_library: %s", e)

    # Fallback: use codes from existing SDW files
    for year in range(START_DATE.year, date.today().year + 1):
        for label, _, _ in RANGES:
            p = lib_path(year, label)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                by_date = json.load(f).get("by_date", {})
            for ds, stocks in by_date.items():
                codes.update(stocks.keys())

    if codes:
        log.info("SFC universe (fallback from SDW cache): %d codes", len(codes))
        return sorted(codes, key=lambda x: int(x))

    log.warning("No SFC universe available — returning empty list")
    return []


# ── SDW fetch for one stock ───────────────────────────────────────────────────

def _parse_num(s) -> int:
    try:
        return int(str(s).replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return 0


def fetch_stock(stock_code: str, d: date) -> dict | None:
    """
    Fetch CCASS participant holdings for one stock on one date.

    Each call does its own GET (viewstate) + POST (data) — ASP.NET
    __EVENTVALIDATION tokens are single-use per page load and cannot be
    shared across requests.

    Returns {p, total_sh, issued_sh} or None if no data found.
    """
    code5    = stock_code.zfill(5)
    date_str = d.strftime("%Y/%m/%d")
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)

        # GET to retrieve fresh ASP.NET viewstate tokens
        r1 = sess.get(SDW_URL, timeout=15)
        r1.raise_for_status()
        soup1 = BeautifulSoup(r1.text, "html.parser")

        def hv(name):
            tag = soup1.find("input", {"name": name})
            return tag["value"] if tag else ""

        # POST with stock code and date
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
        }, timeout=20)
        r2.raise_for_status()

        soup2     = BeautifulSoup(r2.text, "html.parser")
        full_text = soup2.get_text(" ", strip=True)

        # ── 已發行股份/權證/單位 (最近更新數目) ─────────────────────────────
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
        # Table columns:
        #   [0] 參與者編號  [1] 中央結算系統參與者名稱  [2] (skip)  [3] 持股量  [4] 佔...百分比
        def clean(s):
            return re.sub(r'^[^:：]+[:：]\s*', '', s).strip()

        participants      = []
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

        # ── 總數 ─────────────────────────────────────────────────────────────
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

        # 2. Scan table rows for a footer containing 總數
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

        # 3. Fallback: sum participant rows
        if total_sh == 0 and total_sh_fallback > 0:
            total_sh = total_sh_fallback
            log.warning("SDW %s %s: 總數 not found — using participant sum %d",
                        code5, date_str, total_sh)

        if not participants:
            log.debug("SDW: 0 records for %s on %s", code5, date_str)
            return None

        participants.sort(key=lambda x: -x["sh"])
        return {"p": participants, "total_sh": total_sh, "issued_sh": issued_sh}

    except Exception as e:
        log.error("fetch_stock (%s %s): %s", code5, date_str, e)
        return None


# ── Build / update ────────────────────────────────────────────────────────────

def build(update_only: bool = False, specific_date: date = None,
          max_minutes: float = 0, range_label: str = None):
    """
    Build or update SDW files.

    range_label — if given (e.g. "0001_1000"), only fetch stocks in that range.
                  Used by the parallel matrix strategy in GitHub Actions where
                  7 jobs run simultaneously, each owning one range file.
                  When None, fetches all ranges (original behaviour).

    max_minutes — stop gracefully after N minutes (0 = no limit).
    """
    deadline = (time.monotonic() + max_minutes * 60) if max_minutes > 0 else None

    # Resolve which ranges this job owns
    if range_label:
        owned_ranges = [(lbl, lo, hi) for lbl, lo, hi in RANGES if lbl == range_label]
        if not owned_ranges:
            log.error("Unknown range_label %r — valid: %s",
                      range_label, [r[0] for r in RANGES])
            return
        log.info("Range filter: %s only", range_label)
    else:
        owned_ranges = RANGES

    if specific_date:
        dates_to_fetch = [specific_date]
    else:
        all_dates = all_fetch_dates()

        if update_only:
            # Only fetch dates newer than the most recently stored date
            stored_all = all_stored_dates()
            last = date.fromisoformat(max(stored_all)) if stored_all else None
            all_dates = [d for d in all_dates if last is None or d > last]
            log.info("Update mode: %d new dates after %s",
                     len(all_dates), last.isoformat() if last else "none")

        # Determine which dates still need fetching.
        # For a range-specific run: a date is incomplete if fewer than 95% of
        # that range's universe codes are stored in its range file.
        # For a full run: use the global stored-dates set (faster).
        if range_label:
            # Pre-compute universe for this range once (not per date)
            universe_all = get_sfc_universe()
            owned_lo, owned_hi = owned_ranges[0][1], owned_ranges[0][2]
            range_universe = [c for c in universe_all if owned_lo <= int(c) <= owned_hi]
            threshold = max(1, int(len(range_universe) * 0.95))

            def _range_complete(ds):
                year = int(ds[:4])
                lbl  = owned_ranges[0][0]
                p    = lib_path(year, lbl)
                if not os.path.exists(p):
                    return False
                with open(p, encoding="utf-8") as f:
                    day = json.load(f).get("by_date", {}).get(ds, {})
                return len(day) >= threshold

            dates_to_fetch = [d for d in all_dates if not _range_complete(d.isoformat())]
        else:
            stored_all = all_stored_dates()
            dates_to_fetch = [d for d in all_dates if d.isoformat() not in stored_all]

        log.info("%s: %d dates to fetch (%d in schedule)",
                 "Update" if update_only else "Build",
                 len(dates_to_fetch), len(all_fetch_dates()))

    if not dates_to_fetch:
        log.info("Already up to date")
        return

    universe = get_sfc_universe()
    if not universe:
        log.error("Empty stock universe — aborting")
        return

    # Filter universe to owned ranges only
    if range_label:
        owned_lo = owned_ranges[0][1]
        owned_hi = owned_ranges[0][2]
        universe = [c for c in universe if owned_lo <= int(c) <= owned_hi]
        log.info("Filtered universe: %d codes in range %s", len(universe), range_label)
    else:
        log.info("Stock universe: %d codes across %d ranges", len(universe), len(RANGES))

    if deadline:
        log.info("Time limit: %.0f minutes", max_minutes)

    for di, d in enumerate(dates_to_fetch, 1):
        if deadline and time.monotonic() >= deadline:
            log.info("Time limit reached after %d/%d dates — stopping cleanly",
                     di - 1, len(dates_to_fetch))
            break

        ds         = d.isoformat()
        year       = d.year
        log.info("── [%d/%d] %s ──", di, len(dates_to_fetch), ds)

        # Only load range files this job owns
        range_libs = {label: load_range(year, label) for label, _, _ in owned_ranges}

        # For range-specific runs read already-stored codes from the owned range file only
        # (avoids scanning all 7 range files when only 1 is relevant)
        if range_label:
            lbl = owned_ranges[0][0]
            already = set(range_libs[lbl]["by_date"].get(ds, {}).keys())
        else:
            already = _stored_codes_for_date(ds)
        todo = [c for c in universe if c not in already]
        log.info("  %d stocks to fetch (%d already stored)", len(todo), len(already))

        fetched = 0
        timed_out = False
        for ci, code in enumerate(todo, 1):
            if deadline and time.monotonic() >= deadline:
                log.info("  Time limit reached mid-date at stock [%d/%d] — saving progress",
                         ci, len(todo))
                timed_out = True
                break

            entry = fetch_stock(code, d)
            if entry:
                rl = code_range(code)
                range_libs[rl]["by_date"].setdefault(ds, {})[code] = entry
                fetched += 1
            time.sleep(SLEEP_SEC)

            # Checkpoint: save all ranges every 50 stocks
            if ci % 50 == 0:
                for label, lib in range_libs.items():
                    if lib["by_date"].get(ds):
                        save_range(year, label, lib)
                log.info("  [%d/%d] %d saved so far", ci, len(todo), fetched)

        # Final save for this date (complete or partial)
        for label, lib in range_libs.items():
            if lib["by_date"].get(ds):
                save_range(year, label, lib)
        status = "partial" if timed_out else "done"
        log.info("  %s %s: %d/%d stocks saved", ds, status, fetched, len(todo))

        if timed_out:
            break

    log.info("Build complete")


# ── Migration from old single-file format ────────────────────────────────────

def migrate_to_range_split():
    """
    Migrate old ccass_sdw_{YYYY}.json files (single file per year)
    to the new range-split format ccass_sdw_{range}_{YYYY}.json.
    Safe to re-run — already-migrated files are skipped.
    """
    for year in range(START_DATE.year, date.today().year + 1):
        old_path = f"ccass_sdw_{year}.json"
        if not os.path.exists(old_path):
            continue
        with open(old_path, encoding="utf-8") as f:
            old_lib = json.load(f)
        by_date = old_lib.get("by_date", {})
        if not by_date:
            log.info("ccass_sdw_%d.json: empty — skipping", year)
            continue

        # Check if already migrated
        range_files_exist = all(
            os.path.exists(lib_path(year, label))
            for label, _, _ in RANGES
        )
        if range_files_exist:
            log.info("Range files for %d already exist — skipping migration", year)
            continue

        log.info("Migrating ccass_sdw_%d.json (%d dates)…", year, len(by_date))
        range_libs = {label: load_range(year, label) for label, _, _ in RANGES}
        migrated = 0
        for ds, stocks in by_date.items():
            for code, raw in stocks.items():
                rl  = code_range(code)
                entry = _to_v2(raw)
                range_libs[rl]["by_date"].setdefault(ds, {})[code] = entry
                migrated += 1
        for label, lib in range_libs.items():
            if lib["by_date"]:
                save_range(year, label, lib)
        log.info("Migrated %d records from ccass_sdw_%d.json", migrated, year)

    log.info("Migration complete")


# ── API ───────────────────────────────────────────────────────────────────────

def _load_for_code(code: str, ds: str) -> dict:
    year  = int(ds[:4])
    label = code_range(code.zfill(5))
    p     = lib_path(year, label)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("by_date", {}).get(ds, {})

def get_holders(stock_code: str, ds: str) -> list:
    """Return participant list [{pid, name, sh, pct}] or []."""
    raw = _load_for_code(stock_code, ds).get(stock_code.zfill(5))
    return _to_v2(raw)["p"] if raw is not None else []

def get_total_sh(stock_code: str, ds: str) -> int:
    """Return 總數 for a stock on YYYY-MM-DD, or 0."""
    raw = _load_for_code(stock_code, ds).get(stock_code.zfill(5))
    return _to_v2(raw).get("total_sh", 0) if raw is not None else 0

def get_issued_sh(stock_code: str, ds: str) -> int:
    """Return 已發行股份 for a stock on YYYY-MM-DD, or 0."""
    raw = _load_for_code(stock_code, ds).get(stock_code.zfill(5))
    return _to_v2(raw).get("issued_sh", 0) if raw is not None else 0

def get_latest_total_sh(stock_code: str, before: str = None) -> int:
    """Most recent 總數 for a stock (optionally before a YYYY-MM-DD date)."""
    code5  = stock_code.zfill(5)
    label  = code_range(code5)
    cutoff = before or (date.today() + timedelta(days=1)).isoformat()
    for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
        p = lib_path(year, label)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= cutoff:
                continue
            raw = by_date[ds].get(code5)
            if raw is None:
                continue
            total_sh = _to_v2(raw).get("total_sh", 0)
            if total_sh > 0:
                return total_sh
    return 0

def get_holders_history(stock_code: str, n: int, before: str) -> list:
    """
    Last n weekly snapshots before `before` (YYYY-MM-DD), newest-first.
    Returns [{date, holders, total_sh, issued_sh}, ...].
    """
    code5  = stock_code.zfill(5)
    label  = code_range(code5)
    result = []
    for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
        p = lib_path(year, label)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for ds in sorted(by_date.keys(), reverse=True):
            if ds >= before:
                continue
            raw = by_date[ds].get(code5)
            if raw is None:
                continue
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
        label = code_range(code5)
        for year in sorted(range(START_DATE.year, date.today().year + 1), reverse=True):
            p = lib_path(year, label)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                by_date = json.load(f).get("by_date", {})
            dates = [d for d, s in by_date.items() if code5 in s]
            if dates:
                ds = max(dates)
                break
    if not ds:
        print(f"No data for {code5}")
        return
    year  = int(ds[:4])
    label = code_range(code5)
    with open(lib_path(year, label), encoding="utf-8") as f:
        raw = json.load(f).get("by_date", {}).get(ds, {}).get(code5)
    if raw is None:
        print(f"No data for {code5} on {ds}")
        return
    entry   = _to_v2(raw)
    holders = entry["p"]
    print(f"\n{code5}  {ds}  ({len(holders)} participants)  "
          f"[range: {label}]")
    print(f"  總數:       {entry.get('total_sh',  0):>20,}")
    print(f"  已發行股份: {entry.get('issued_sh', 0):>20,}")
    print(f"{'#':<4} {'ID':<12} {'Name':<40} {'Shares':>16} {'%':>8}")
    print("─" * 84)
    for i, h in enumerate(holders[:top], 1):
        print(f"{i:<4} {h['pid']:<12} {h['name'][:39]:<40} "
              f"{h['sh']:>16,} {h['pct']:>7.2f}%")
    hist = get_holders_history(code5, 6, (date.today() + timedelta(1)).isoformat())
    if hist:
        print(f"\nAvailable dates: {[h['date'] for h in hist]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CCASS SDW Participant Holdings Library")
    ap.add_argument("--update",       action="store_true",
                    help="Fetch only dates newer than last stored")
    ap.add_argument("--date",         metavar="YYYY-MM-DD",
                    help="Fetch one specific date")
    ap.add_argument("--max-minutes",  type=float, default=0, metavar="N",
                    help="Stop after N minutes (0 = no limit). Use 12 for 15-min CI jobs.")
    ap.add_argument("--query",        metavar="CODE",
                    help="Show participant holdings for a stock code")
    ap.add_argument("--top",          type=int, default=20,
                    help="Number of top participants to show (default 20)")
    ap.add_argument("--range",        metavar="LABEL",
                    help="Only fetch stocks in this range (e.g. 0001_1000). "
                         "Used by parallel matrix jobs — each job owns one range.")
    ap.add_argument("--migrate",      action="store_true",
                    help="Migrate old ccass_sdw_YYYY.json files to range-split format")
    args = ap.parse_args()

    if args.migrate:
        migrate_to_range_split()
    elif args.query:
        _query(args.query, args.top, args.date)
    else:
        build(update_only=args.update,
              specific_date=date.fromisoformat(args.date) if args.date else None,
              max_minutes=args.max_minutes,
              range_label=getattr(args, 'range', None))
