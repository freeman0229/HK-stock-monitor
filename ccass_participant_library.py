"""
ccass_participant_library.py — CCASS Participant List Library
=============================================================
Fetches and stores the list of CCASS Participants (Intermediaries)
from HKEXnews. Two columns: Participant ID and Participant Name.

Source:
  https://www.hkexnews.hk/ccass_part_list.htm

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
  from ccass_participant_library import get_participant, get_all_participants
"""

import os, json, logging, argparse
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

URL      = "https://www.hkexnews.hk/ccass_part_list.htm"
LIB_FILE = "ccass_participants.json"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (compatible; DataBot/1.0)",
    "Referer":    "https://www.hkexnews.hk/",
}


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
    """
    try:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        participants = {}

        # The page has a table with two columns: ID | Name
        # Try all tables — use the one with the most rows
        tables = soup.find_all("table")
        if not tables:
            log.warning("No tables found on %s", URL)
            return None

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
            log.warning("Parsed 0 participants from %s", URL)
            return None

        log.info("Fetched %d participants from CCASS list", len(participants))
        return participants

    except Exception as e:
        log.error("fetch failed: %s", e)
        return None


# ── Build / update ────────────────────────────────────────────────────────────

def build(update_only: bool = False):
    """Fetch and save the participant list."""
    if update_only and not is_stale():
        log.info("Participant list is up to date (last updated: %s)",
                 load_lib()["meta"].get("last_updated"))
        return

    participants = fetch()
    if participants is None:
        log.error("Failed to fetch participant list")
        return

    lib = {"meta": {}, "participants": participants}
    save_lib(lib)


# ── API for other modules ─────────────────────────────────────────────────────

def get_participant(pid: str) -> str | None:
    """
    Return the name for a participant ID, or None if not found.
    Case-insensitive lookup.
    """
    lib = load_lib()
    p = lib.get("participants", {})
    # Exact match first
    if pid in p:
        return p[pid]
    # Case-insensitive
    pid_up = pid.upper()
    for k, v in p.items():
        if k.upper() == pid_up:
            return v
    return None


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
