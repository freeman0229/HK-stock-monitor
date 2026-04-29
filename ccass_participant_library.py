"""
ccass_participant_library.py — CCASS Participant List Library
=============================================================
Fetches and stores the list of CCASS Participants (Intermediaries)
from HKEXnews. Two columns: Participant ID and Participant Name.

Source:
  https://www3.hkexnews.hk/sdw/search/ccass_part_list_c.htm?sortby=partid&shareholdingdate=YYYYMMDD

Library file: ccass_participants.json

Structure:
{
  "meta": {
    "last_updated": "2026-03-20",
    "total": 512
  },
  "participants": {
    "B01234": "CHINA INTERNATIONAL CAPITAL CORP HK SECS LTD",
    "C00019": "CITIBANK N.A.",
    ...
  }
}

Participant IDs typically follow the pattern:
  B/C/D/E/F/G/H/I/M/P/T/U/W/X + digits
  B = Broker
  C = Custodian
  (etc.)

Usage:
  python ccass_participant_library.py              # fetch and save
  python ccass_participant_library.py --update     # only if stale (>7 days)
  python ccass_participant_library.py --query B01234
  python ccass_participant_library.py --search "CITIBANK"

API for other modules:
  from ccass_participant_library import get_participant, get_all_participants, get_group, group_holdings
"""

import argparse
import json
import logging
import os
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Base URL — shareholdingdate is set dynamically to last trading day at fetch time
URL_BASE = "https://www3.hkexnews.hk/sdw/search/ccass_part_list_c.htm"
LIB_FILE = "ccass_participants.json"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkexnews.hk/",
}

_FETCH_RETRIES  = 3    # total attempts before giving up
_RETRY_SLEEP    = 5    # seconds between retries

# ── Trading day helper ────────────────────────────────────────────────────────

# Hardcoded HK public holidays (same set used across all pipeline scripts).
_HK_HOLIDAYS = {
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

def _last_trading_day() -> date:
    """Return the most recent past HK trading day (never today).

    HKEX SDW participant list only has data for *past* trading dates —
    requesting today's date returns an empty page even on a trading day.
    Stepping back from yesterday avoids the 'No tables found' error that
    occurs when the pipeline runs before HKEX publishes today's data (~6pm HKT).
    """
    d = date.today() - timedelta(days=1)   # start from yesterday
    for _ in range(14):                     # safety limit — no holiday run > 2 weeks
        if d.weekday() < 5 and d.isoformat() not in _HK_HOLIDAYS:
            return d
        d -= timedelta(days=1)
    raise ValueError("_last_trading_day: no trading day found within 14 days")


# ── File I/O ──────────────────────────────────────────────────────────────────

def load_lib() -> dict:
    if os.path.exists(LIB_FILE):
        with open(LIB_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "participants": {}}


def save_lib(lib: dict):
    lib["meta"]["last_updated"] = date.today().isoformat()
    lib["meta"]["total"] = len(lib["participants"])
    with open(LIB_FILE, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, separators=(",", ":"))
    kb = os.path.getsize(LIB_FILE) / 1024
    log.info("Saved %s: %d participants, %.1f KB", LIB_FILE, lib["meta"]["total"], kb)


def is_stale(max_days: int = 7) -> bool:
    """Return True if the library is missing or older than max_days."""
    lib = load_lib()
    last = lib.get("meta", {}).get("last_updated")
    if not last:
        return True
    return date.today() - date.fromisoformat(last) > timedelta(days=max_days)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch() -> dict | None:
    """
    Fetch the CCASS participant list page and parse the two-column table.
    Returns {participant_id: participant_name} or None on failure.

    Uses the last trading day (not today) because HKEX only serves a populated
    table for *past* dates — requesting today's date before ~6pm HKT returns
    an empty page with no table.

    Retries up to _FETCH_RETRIES times with _RETRY_SLEEP second gaps before
    giving up, to handle transient HKEX network/bot-detection failures.
    """
    trade_ds = _last_trading_day().strftime('%Y%m%d')
    url = f"{URL_BASE}?sortby=partid&shareholdingdate={trade_ds}"

    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            log.info("Fetching participant list (attempt %d/%d): %s",
                     attempt, _FETCH_RETRIES, url)
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            participants = {}

            # The page has a table with two columns: ID | Name
            # Try all tables — use the one with the most rows
            tables = soup.find_all("table")
            if not tables:
                log.warning("No tables found on %s (attempt %d/%d)",
                            url, attempt, _FETCH_RETRIES)
                if attempt < _FETCH_RETRIES:
                    log.info("Retrying in %ds …", _RETRY_SLEEP)
                    time.sleep(_RETRY_SLEEP)
                continue

            best_table = max(tables, key=lambda t: len(t.find_all("tr")))
            rows = best_table.find_all("tr")

            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2:
                    continue
                pid  = cells[0].strip()
                name = cells[1].strip()
                # Participant IDs are alphanumeric, typically 6–8 chars
                if not pid or not name or pid.lower() in ("id", "participant id", "code"):
                    continue
                participants[pid] = name

            if not participants:
                log.warning("Parsed 0 participants from %s (attempt %d/%d)",
                            url, attempt, _FETCH_RETRIES)
                if attempt < _FETCH_RETRIES:
                    log.info("Retrying in %ds …", _RETRY_SLEEP)
                    time.sleep(_RETRY_SLEEP)
                continue

            log.info("Fetched %d participants from %s", len(participants), url)
            return participants

        except Exception as e:
            log.error("fetch failed (attempt %d/%d): %s", attempt, _FETCH_RETRIES, e)
            if attempt < _FETCH_RETRIES:
                log.info("Retrying in %ds …", _RETRY_SLEEP)
                time.sleep(_RETRY_SLEEP)

    log.error("All %d fetch attempts failed for %s", _FETCH_RETRIES, url)
    return None


# ── Build / update ────────────────────────────────────────────────────────────

def build(update_only: bool = False):
    """Fetch and save the participant list.

    If all fetch attempts fail, falls back to the existing cached library
    (however stale) so downstream scripts can continue working.
    """
    if update_only:
        if not is_stale():
            lib = load_lib()
            log.info("Participant list is up to date (last updated: %s)",
                     lib["meta"].get("last_updated"))
            return

    participants = fetch()
    if participants is None:
        # All retries exhausted — fall back to stale cache if available
        existing = load_lib()
        if existing.get("participants"):
            log.warning(
                "Fetch failed — continuing with stale cache from %s (%d participants)",
                existing["meta"].get("last_updated", "unknown"),
                len(existing["participants"]),
            )
        else:
            log.error("Fetch failed and no cached data available")
        return

    lib = {"meta": {}, "participants": participants}
    save_lib(lib)


# ── API for other modules ─────────────────────────────────────────────────────

def get_participant(pid: str) -> str | None:
    """
    Return the name for a participant ID, or None if not found.
    Participant IDs are always uppercase in the source — normalise before lookup.
    """
    p = load_lib().get("participants", {})
    return p.get(pid.upper())


def get_all_participants() -> dict:
    """Return the full {id: name} dict."""
    return load_lib().get("participants", {})


def search_participants(query: str) -> list[tuple[str, str]]:
    """
    Search participants by name (case-insensitive substring match).
    Returns list of (id, name) tuples sorted by id.
    """
    q = query.upper()
    results = [
        (pid, name)
        for pid, name in load_lib().get("participants", {}).items()
        if q in name.upper() or q in pid.upper()
    ]
    return sorted(results)



# ── Participant groups ────────────────────────────────────────────────────────

# Major institutional holders (大型券商/託管行)
# Note: these sets require manual maintenance as membership changes.
DAHU_IDS = {
    "B01555", "B01451", "B01224",
    "C00010", "C00019", "C00074", "C00093", "C00039", "C00111",
    "B01274", "B01161", "B01110", "B01504",
    "C00100", "C00033", "B01366",
}

# Northbound / mainland China participants (北水)
BEISHUI_IDS = {"A00003", "A00004", "A00005"}

# Group labels
GROUP_DAHU    = "大戶"
GROUP_BEISHUI = "北水"
GROUP_SANHU   = "散戶"


def get_group(pid: str) -> str:
    """Return the group label for a participant ID."""
    if pid in BEISHUI_IDS:
        return GROUP_BEISHUI
    if pid in DAHU_IDS:
        return GROUP_DAHU
    return GROUP_SANHU


def group_holdings(holders: list) -> dict:
    """
    Group a list of participant holding records by 大戶 / 北水 / 散戶.

    Input:  list of {pid, name, sh, pct} (from ccass_sdw_library.get_holders)
    Output: {
        "大戶":  {"sh": int, "pct": float, "participants": [...]},
        "北水":  {"sh": int, "pct": float, "participants": [...]},
        "散戶":  {"sh": int, "pct": float, "participants": [...]},
    }
    """
    groups = {
        GROUP_DAHU:    {"sh": 0, "pct": 0.0, "participants": []},
        GROUP_BEISHUI: {"sh": 0, "pct": 0.0, "participants": []},
        GROUP_SANHU:   {"sh": 0, "pct": 0.0, "participants": []},
    }
    for h in holders:
        g = get_group(h.get("pid", ""))
        groups[g]["sh"]  += h.get("sh", 0)
        groups[g]["pct"] += h.get("pct", 0.0)
        groups[g]["participants"].append(h)
    # Round pct to avoid floating point accumulation drift
    for g in groups.values():
        g["pct"] = round(g["pct"], 4)
    return groups

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CCASS Participant List Library")
    ap.add_argument("--update", action="store_true",
                    help="Fetch only if library is >7 days old")
    ap.add_argument("--query",  metavar="ID",
                    help="Look up a participant by ID")
    ap.add_argument("--search", metavar="QUERY",
                    help="Search participants by name or ID substring")
    args = ap.parse_args()

    if args.query:
        name = get_participant(args.query)
        print(f"{args.query}: {name}" if name else f"{args.query}: not found")
    elif args.search:
        results = search_participants(args.search)
        if not results:
            print(f"No participants matching '{args.search}'")
        else:
            print(f"{len(results)} result(s):")
            for pid, name in results:
                print(f"  {pid:<10} {name}")
    else:
        build(update_only=args.update)
