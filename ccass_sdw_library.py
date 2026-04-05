"""
ccass_sdw_library.py — CCASS Per-Stock Participant Holdings Library
====================================================================
Fetches weekly CCASS participant-level shareholding for ALL stocks
listed in the SFC short-position universe.

Source (holdings):  https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx
Stock universe:     SFC short-position library (sfc_library.py)

Schedule: every Friday; Thursday fallback if Friday is a HK holiday.
Start:    2025-04-05

Library files split by stock code range — one set of 7 files per year:
  ccass_sdw_0001_1000_{YYYY}.json
  ccass_sdw_1001_2000_{YYYY}.json
  ccass_sdw_2001_3000_{YYYY}.json
  ccass_sdw_3001_4000_{YYYY}.json
  ccass_sdw_4001_7000_{YYYY}.json
  ccass_sdw_7001_9999_{YYYY}.json
  ccass_sdw_10000plus_{YYYY}.json

Fetch strategy: Playwright Chromium (headless) — fills the search form
like a real user, bypassing Akamai BotManager detection entirely.

Usage:
  python ccass_sdw_library.py                    # full backfill
  python ccass_sdw_library.py --update           # only new dates
  python ccass_sdw_library.py --date 2026-03-21  # one specific date
  python ccass_sdw_library.py --query 00700      # show holdings for a stock
  python ccass_sdw_library.py --migrate          # upgrade old files to range-split format
"""

import os, json, re, time, logging, argparse, random
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from datetime import date, timedelta
from playwright.sync_api import sync_playwright
from ccass_universe import normalize_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SDW_URL = "https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

BLOCK_PATTERNS = [
    "Access Denied",
    "Too many requests",
    "Request blocked",
    "Service unavailable",
    "403 Forbidden",
    "429 Too Many",
]

_PROXY = os.getenv("SDW_PROXY", "").strip() or None

START_DATE            = date(2025, 4, 5)
SCHEMA_VERSION        = 2
SLEEP_MIN             = 2.0    # base delay between stocks (browser postback adds natural latency)
SLEEP_MAX             = 5.0
PRE_SLEEP_MIN         = 0.5    # think time before each search
PRE_SLEEP_MAX         = 1.5
CIRCUIT_BREAKER_LIMIT = 5      # consecutive errors before declaring blocked
BLOCKED_COOLDOWN_SEC  = 1800   # 30 min cooldown when blocked

# ── Helpers ───────────────────────────────────────────────────────────────────

def human_sleep(a: float, b: float):
    """Sleep for a random duration in [a, b] plus a small extra jitter."""
    time.sleep(random.uniform(a, b) + random.random() * 0.3)

def _parse_proxy(proxy_url: str) -> dict | None:
    """Convert proxy URL string to Playwright proxy config dict.
    Handles both full URLs (http://user:pass@host:port) and bare host:port.
    """
    if not proxy_url:
        return None
    # Ensure scheme present so urlparse works correctly
    raw = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
    p = urlparse(raw)
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username: cfg["username"] = p.username
    if p.password: cfg["password"] = p.password
    return cfg

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
    n = int(code)
    for label, lo, hi in RANGES:
        if lo <= n <= hi:
            return label
    return "0001_1000"

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
    thu = friday - timedelta(days=1)
    if friday not in _HK_HOLIDAYS:
        return friday
    if thu not in _HK_HOLIDAYS:
        return thu
    return None

def all_fetch_dates(up_to: date = None) -> list[date]:
    up_to  = up_to or date.today()
    result = []
    d = START_DATE
    while d.weekday() != 4:
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
    stored = set()
    for year in range(START_DATE.year, date.today().year + 1):
        for label, _, _ in RANGES:
            p = lib_path(year, label)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    stored.update(json.load(f).get("by_date", {}).keys())
    return stored

def _stored_codes_for_date(ds: str) -> set[str]:
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
    if isinstance(raw, list):
        return {"p": raw, "total_sh": 0, "issued_sh": 0}
    if isinstance(raw, dict):
        return raw if "p" in raw else {"p": [], "total_sh": 0, "issued_sh": 0}
    return {"p": [], "total_sh": 0, "issued_sh": 0}

# ── Stock universe ────────────────────────────────────────────────────────────

# ── Stock universe ────────────────────────────────────────────────────────────

def get_sfc_universe() -> list[str]:
    """
    Return the stock universe to fetch from CCASS SDW.
    Source: ccass_universe.get_universe_codes() — the authoritative CCASS
    stock list filtered by is_included().
    Falls back to codes already stored in SDW cache if ccass_universe fails.
    """
    try:
        from ccass_universe import get_universe_codes
        codes = get_universe_codes()
        if codes:
            log.info("SDW universe: %d codes from ccass_universe", len(codes))
            return codes
    except Exception as e:
        log.warning("Could not load universe from ccass_universe: %s — falling back to SDW cache", e)

    # Fallback: use whatever codes are already stored in SDW files
    codes = set()
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
        log.info("SDW universe (fallback from cache): %d codes", len(codes))
        return sorted(codes, key=lambda x: int(x))

    log.warning("No universe available — returning empty list")
    return []

# ── Playwright browser wrapper ────────────────────────────────────────────────

class SDWBrowser:
    """
    Manages a Playwright Chromium browser that fills the SDW search form
    like a real user — date field, stock code, then click 搜尋.

    Context is rotated every CONTEXT_LIFE requests to refresh cookies
    without paying the full browser startup cost each time.
    """

    CONTEXT_LIFE = 60   # requests per browser context before rotating

    def __init__(self, proxy: str | None = None):
        self._proxy_cfg  = _parse_proxy(proxy)
        self._pw         = None
        self._browser    = None
        self._context    = None
        self._page       = None
        self._req_count  = 0
        self._on_sdw     = False   # whether page is already on SDW_URL

    # ── lifecycle ────────────────────────────────────────────────────────────

    def __enter__(self):
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            proxy=self._proxy_cfg,
        )
        self._new_context()
        return self

    def __exit__(self, *args):
        try:
            if self._context: self._context.close()
            if self._browser: self._browser.close()
            if self._pw:      self._pw.stop()
        except Exception:
            pass

    def _new_context(self):
        """Close old context and open a fresh one with a new UA and cookies."""
        if self._context:
            try: self._context.close()
            except Exception: pass

        ua = random.choice(_USER_AGENTS)
        self._context = self._browser.new_context(
            user_agent     = ua,
            locale         = "zh-HK",
            timezone_id    = "Asia/Hong_Kong",
            viewport       = {"width": random.randint(1280, 1440),
                               "height": random.randint(768, 900)},
            java_script_enabled = True,
        )

        # Pre-seed known cookies before any navigation so the site
        # treats us as a returning zh-HK user with consent already given
        consent_val = (
            "isGpcEnabled=0&datestamp=Wed+Apr+01+2026+16%3A00%3A00+GMT%2B0100"
            "&version=202303.2.0&browserGpcFlag=0&isIABGlobal=false"
            "&hosts=&landingPath=NotLandingPage"
            "&groups=C0001%3A1%2CC0003%3A1%2CC0004%3A1%2CC0002%3A1"
            "&AwaitingReconsent=false&geolocation=HK%3BHKG"
        )
        for domain in [".hkexnews.hk", ".www3.hkexnews.hk", "www3.hkexnews.hk"]:
            self._context.add_cookies([
                {"name": "sclang",              "value": "zh-HK",       "domain": domain, "path": "/"},
                {"name": "s_cc",                "value": "true",        "domain": domain, "path": "/"},
                {"name": "OptanonAlertBoxClosed","value": "2026-04-01T16:00:00.000Z", "domain": domain, "path": "/"},
                {"name": "OptanonConsent",      "value": consent_val,   "domain": domain, "path": "/"},
            ])

        self._page     = self._context.new_page()
        self._req_count = 0
        self._on_sdw   = False
        log.info("🌐 New browser context (UA: %s…)", ua[:60])

        # Step 1: Visit main homepage to seed Akamai bm_* cookies
        try:
            self._page.goto("https://www.hkexnews.hk/", wait_until="networkidle", timeout=45_000)
            human_sleep(2.0, 4.0)
        except Exception as e:
            log.warning("Homepage warm-up failed: %s", str(e)[:80])

        # Step 2: Navigate to SDW page so Akamai sets www3-scoped bm_* cookies
        try:
            self._page.goto(SDW_URL, wait_until="networkidle", timeout=60_000)
            self._on_sdw = True
            human_sleep(1.5, 3.0)
        except Exception as e:
            log.warning("Initial SDW navigation failed: %s", str(e)[:80])
            self._on_sdw = False

    # ── fetch ─────────────────────────────────────────────────────────────────

    def fetch(self, stock_code: str, d: date) -> dict | None:
        """
        Fill date + stock code in the SDW search form, click 搜尋,
        wait for the ASP.NET postback to complete, then parse the result.
        Returns {p, total_sh, issued_sh} or None.
        """
        code5    = normalize_code(stock_code)
        date_str = d.strftime("%Y/%m/%d")

        # Rotate context periodically
        self._req_count += 1
        if self._req_count >= self.CONTEXT_LIFE:
            log.info("🔄 Rotating browser context at request %d", self._req_count)
            self._new_context()

        page = self._page

        for attempt in range(1, 4):
            try:
                # Ensure we're on the SDW page
                if not self._on_sdw:
                    page.goto(SDW_URL, wait_until="networkidle", timeout=60_000)
                    self._on_sdw = True
                    human_sleep(1.0, 2.0)

                # ── Set fields + submit via JS, wrapped in expect_navigation ──
                # expect_navigation is set up BEFORE the JS fires so Playwright
                # catches the navigation event the instant __doPostBack triggers it.
                with page.expect_navigation(wait_until="networkidle",
                                            timeout=45_000):
                    page.evaluate(
                        """([dateStr, code5]) => {
                            const dateEl = document.getElementById('txtShareholdingDate');
                            if (dateEl) {
                                dateEl.removeAttribute('readonly');
                                dateEl.value = dateStr;
                            }
                            const codeEl = document.getElementById('txtStockCode');
                            if (codeEl) codeEl.value = code5;
                            const pidEl  = document.getElementById('txtParticipantID');
                            const pnmEl  = document.getElementById('txtParticipantName');
                            if (pidEl)  pidEl.value  = '';
                            if (pnmEl)  pnmEl.value  = '';
                            if (typeof __doPostBack === 'function') {
                                __doPostBack('btnSearch', '');
                            } else {
                                document.getElementById('btnSearch').click();
                            }
                        }""",
                        [date_str, code5]
                    )

                # ── Check for block ───────────────────────────────────────────
                content = page.content()
                if any(p in content for p in BLOCK_PATTERNS):
                    log.warning("⚠️ Block detected for %s %s (attempt %d)",
                                code5, date_str, attempt)
                    if attempt < 3:
                        human_sleep(90, 180)
                        self._new_context()
                        page = self._page
                        continue
                    self._on_sdw = False
                    return None

                # ── Parse results ─────────────────────────────────────────────
                result = _parse_response(content, code5, date_str)
                # Stay on results page — next stock just re-submits the form
                self._on_sdw = True
                return result

            except Exception as e:
                err_str = str(e)
                is_proxy_err = any(p in err_str for p in [
                    "ProxyError", "RemoteDisconnected", "Unable to connect to proxy",
                    "proxy", "Proxy",
                ])

                if attempt < 3:
                    if is_proxy_err:
                        wait = random.uniform(20, 40)
                        log.warning("fetch (%s %s) proxy error attempt %d — new context in %.0fs: %s",
                                    code5, date_str, attempt, wait, err_str[:200])
                        time.sleep(wait)
                        self._new_context()
                        page = self._page
                    else:
                        wait = attempt * 15
                        log.warning("fetch (%s %s) attempt %d failed — retry in %ds: %s",
                                    code5, date_str, attempt, wait, err_str[:200])
                        time.sleep(wait)
                        try:
                            page.goto(SDW_URL, wait_until="networkidle", timeout=60_000)
                            self._on_sdw = True
                        except Exception as nav_e:
                            log.warning("re-nav failed: %s — new context", str(nav_e)[:100])
                            self._new_context()
                            page = self._page
                else:
                    log.error("fetch (%s %s): all 3 attempts failed: %s",
                              code5, date_str, err_str[:300])
                    self._on_sdw = False
                    return None

# ── HTML response parser ──────────────────────────────────────────────────────

def _parse_num(s) -> int:
    try:
        return int(str(s).replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return 0

def _parse_response(html: str, code5: str, date_str: str) -> dict | None:
    """Parse the SDW results page HTML. Same logic as before, now standalone."""
    soup     = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(" ", strip=True)

    # 已發行股份
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

    # Participant rows
    def clean(s):
        return re.sub(r'^[^:：]+[:：]\s*', '', s).strip()

    participants      = []
    total_sh_fallback = 0

    for tr in soup.find_all("tr"):
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

    # 總數
    total_sh = 0
    for pat in [r"總數[^\d]{0,20}([\d,]{6,})", r"Grand\s+Total[^\d]{0,20}([\d,]{6,})"]:
        m = re.search(pat, full_text)
        if m:
            total_sh = _parse_num(m.group(1))
            if total_sh > 0:
                break

    if total_sh == 0:
        for tr in soup.find_all("tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if "總數" in " ".join(tds) or "Grand Total" in " ".join(tds):
                for cell in reversed(tds):
                    num = _parse_num(cell)
                    if num > 1_000_000:
                        total_sh = num
                        break
                if total_sh > 0:
                    break

    if total_sh == 0 and total_sh_fallback > 0:
        total_sh = total_sh_fallback
        log.warning("SDW %s %s: 總數 not found — using participant sum %d",
                    code5, date_str, total_sh)

    if not participants:
        log.debug("SDW: 0 records for %s on %s", code5, date_str)
        return None

    participants.sort(key=lambda x: -x["sh"])
    return {"p": participants, "total_sh": total_sh, "issued_sh": issued_sh}

# ── Build / update ────────────────────────────────────────────────────────────

def build(update_only: bool = False, specific_date: date = None,
          max_minutes: float = 0, range_label: str = None):
    deadline = (time.monotonic() + max_minutes * 60) if max_minutes > 0 else None

    if range_label:
        owned_ranges = [(lbl, lo, hi) for lbl, lo, hi in RANGES if lbl == range_label]
        if not owned_ranges:
            log.error("Unknown range_label %r — valid: %s", range_label, [r[0] for r in RANGES])
            return
        log.info("Range filter: %s only", range_label)
    else:
        owned_ranges = RANGES

    if specific_date:
        dates_to_fetch = [specific_date]
    else:
        all_dates = all_fetch_dates()
        if update_only:
            stored_all = all_stored_dates()
            last = date.fromisoformat(max(stored_all)) if stored_all else None
            all_dates = [d for d in all_dates if last is None or d > last]
            log.info("Update mode: %d new dates after %s",
                     len(all_dates), last.isoformat() if last else "none")

        if range_label:
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

    if range_label:
        owned_lo = owned_ranges[0][1]
        owned_hi = owned_ranges[0][2]
        universe = [c for c in universe if owned_lo <= int(c) <= owned_hi]
        log.info("Filtered universe: %d codes in range %s", len(universe), range_label)
    else:
        log.info("Stock universe: %d codes across %d ranges", len(universe), len(RANGES))

    if deadline:
        log.info("Time limit: %.0f minutes", max_minutes)

    with SDWBrowser(proxy=_PROXY) as browser:
        for di, d in enumerate(dates_to_fetch, 1):
            if deadline and time.monotonic() >= deadline:
                log.info("Time limit reached after %d/%d dates — stopping cleanly",
                         di - 1, len(dates_to_fetch))
                break

            ds   = d.isoformat()
            year = d.year
            log.info("── [%d/%d] %s ──", di, len(dates_to_fetch), ds)

            range_libs = {label: load_range(year, label) for label, _, _ in owned_ranges}

            if range_label:
                lbl     = owned_ranges[0][0]
                already = set(range_libs[lbl]["by_date"].get(ds, {}).keys())
            else:
                already = _stored_codes_for_date(ds)
            todo = [c for c in universe if c not in already]
            log.info("  %d stocks to fetch (%d already stored)", len(todo), len(already))

            fetched       = 0
            timed_out     = False
            blocked       = False
            consec_errors = 0
            _dirty_ranges = set()

            for ci, code in enumerate(todo, 1):

                # ── TIME LIMIT ────────────────────────────────────────────────
                if deadline and time.monotonic() >= deadline:
                    log.info("  Time limit reached mid-date at stock [%d/%d] — saving progress",
                             ci, len(todo))
                    timed_out = True
                    break

                # ── THINK TIME ────────────────────────────────────────────────
                human_sleep(PRE_SLEEP_MIN, PRE_SLEEP_MAX)

                # ── FETCH ─────────────────────────────────────────────────────
                entry = browser.fetch(code, d)

                if entry:
                    rl = code_range(code)
                    range_libs[rl]["by_date"].setdefault(ds, {})[code] = entry
                    _dirty_ranges.add(rl)
                    fetched += 1
                    consec_errors = 0

                else:
                    consec_errors += 1

                    # ── EARLY BACKOFF ─────────────────────────────────────────
                    if consec_errors >= 3:
                        backoff = random.uniform(60, 120)
                        log.warning("  ⚠️ Early backoff (%d errors) — sleeping %.0fs",
                                    consec_errors, backoff)
                        time.sleep(backoff)

                    # ── CIRCUIT BREAKER ───────────────────────────────────────
                    if consec_errors >= CIRCUIT_BREAKER_LIMIT:
                        log.error("  🚫 Circuit breaker: %d consecutive errors — blocked",
                                  consec_errors)
                        blocked = True
                        break

                # ── BASE DELAY ────────────────────────────────────────────────
                human_sleep(SLEEP_MIN, SLEEP_MAX)

                # ── BURST PAUSE every 50 stocks ───────────────────────────────
                if ci % 50 == 0:
                    pause = random.uniform(30, 60)
                    log.info("  😴 Burst pause %.0fs at %d", pause, ci)
                    time.sleep(pause)

                    for label in _dirty_ranges:
                        if range_libs[label]["by_date"].get(ds):
                            save_range(year, label, range_libs[label])
                    _dirty_ranges.clear()
                    log.info("  [%d/%d] %d saved so far", ci, len(todo), fetched)

            for label, lib in range_libs.items():
                if lib["by_date"].get(ds):
                    save_range(year, label, lib)

            if blocked:
                log.info("  %s blocked: %d/%d stocks saved", ds, fetched, len(todo))
                remaining = (deadline - time.monotonic()) if deadline else float("inf")
                cooldown  = min(BLOCKED_COOLDOWN_SEC, remaining - 10)
                if cooldown > 0:
                    log.info("  Cooling down %.0fs before next date...", cooldown)
                    time.sleep(cooldown)
                else:
                    log.info("  No time remaining — stopping")
                    break
                continue

            status = "partial" if timed_out else "done"
            log.info("  %s %s: %d/%d stocks saved", ds, status, fetched, len(todo))
            if timed_out:
                break

    log.info("Build complete")

# ── Migration ─────────────────────────────────────────────────────────────────

def migrate_to_range_split():
    for year in range(START_DATE.year, date.today().year + 1):
        old_path = f"ccass_sdw_{year}.json"
        if not os.path.exists(old_path):
            continue
        with open(old_path, encoding="utf-8") as f:
            old_lib = json.load(f)
        by_date = old_lib.get("by_date", {})
        if not by_date:
            continue
        if all(os.path.exists(lib_path(year, label)) for label, _, _ in RANGES):
            log.info("Range files for %d already exist — skipping", year)
            continue
        log.info("Migrating ccass_sdw_%d.json (%d dates)...", year, len(by_date))
        range_libs = {label: load_range(year, label) for label, _, _ in RANGES}
        migrated = 0
        for ds, stocks in by_date.items():
            for code, raw in stocks.items():
                rl    = code_range(code)
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
    label = code_range(normalize_code(code))
    p     = lib_path(year, label)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("by_date", {}).get(ds, {})

def get_holders(stock_code: str, ds: str) -> list:
    raw = _load_for_code(stock_code, ds).get(normalize_code(stock_code))
    return _to_v2(raw)["p"] if raw is not None else []

def get_total_sh(stock_code: str, ds: str) -> int:
    raw = _load_for_code(stock_code, ds).get(normalize_code(stock_code))
    return _to_v2(raw).get("total_sh", 0) if raw is not None else 0

def get_issued_sh(stock_code: str, ds: str) -> int:
    raw = _load_for_code(stock_code, ds).get(normalize_code(stock_code))
    return _to_v2(raw).get("issued_sh", 0) if raw is not None else 0

def get_latest_total_sh(stock_code: str, before: str = None) -> int:
    code5  = normalize_code(stock_code)
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

def get_total_sh_bulk(ds: str) -> dict:
    result = {}
    year   = int(ds[:4])
    for label, _, _ in RANGES:
        p = lib_path(year, label)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            by_date = json.load(f).get("by_date", {})
        for code5, raw in by_date.get(ds, {}).items():
            ts = _to_v2(raw).get("total_sh", 0)
            if ts > 0:
                result[code5] = ts
    return result

def get_holders_history(stock_code: str, n: int, before: str) -> list:
    code5  = normalize_code(stock_code)
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
    code5 = normalize_code(code)
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
    print(f"\n{code5}  {ds}  ({len(holders)} participants)  [range: {label}]")
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
    ap.add_argument("--update",      action="store_true")
    ap.add_argument("--date",        metavar="YYYY-MM-DD")
    ap.add_argument("--max-minutes", type=float, default=0, metavar="N")
    ap.add_argument("--query",       metavar="CODE")
    ap.add_argument("--top",         type=int, default=20)
    ap.add_argument("--range",       metavar="LABEL")
    ap.add_argument("--migrate",     action="store_true")
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
