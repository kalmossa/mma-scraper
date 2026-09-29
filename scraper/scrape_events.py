"""
scrape_events.py
BATCH Tapology events scraper -> `events` + `event_fights` tables.

WORKFLOW
1. Run the script without arguments.
2. Paste 1+ Tapology event URLs (1 per line), then an EMPTY line to validate.
3. The script confirms, then processes each URL in a batch.
4. For each URL:
     - If tapology_url already exists in the database -> UPDATE (refresh metadata + bouts).
     - Otherwise -> INSERT.
5. Q (or Ctrl+C) to cancel before starting.

PARSING - new Tapology format 2024+
Tapology renders the fight cards in JavaScript (React). Bot protection redirects
classic scrapers. We use Playwright (headless Chromium) to:
  1. Load https://www.tapology.com/fightcenter (session cookie)
  2. Navigate with JS to the event URL (bypasses the bot redirect)
  3. Extract the rendered text with page.inner_text('body')

New Tapology fight card format:
  [F1 info] BOUT_LABEL Weight Rounds add_circle [F2 info]
  [F2 info next f1] BOUT_LABEL Weight Rounds add_circle [F2 info]
  ...

We keep the "add_circle" and "manage_search" icons as separators:
  - add_circle  = F1/F2 separator
  - manage_search = fighter indexed by Tapology (immediately follows the name)

USAGE
    py scrape_events.py                       # interactive, dry-run by default
    py scrape_events.py --commit              # interactive, writes to the database
    py scrape_events.py --commit --verbose    # same + log of the parsed bouts
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import logging
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import urllib.request as _urllib_req

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent  # repository root
SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))
import db_connection as db  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
LOG = logging.getLogger("scrape_events")

# Force UTF-8 on stdout (Windows cp1252 does not support every character)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SLEEP_BETWEEN = 12.0   # seconds between events (+ random variation in the loop)
FAILED_LOG = ROOT / "failed_event_urls.txt"

_MONTHS = {"january":1,"jan":1,"february":2,"feb":2,"march":3,"mar":3,"april":4,"apr":4,
           "may":5,"june":6,"jun":6,"july":7,"jul":7,"august":8,"aug":8,
           "september":9,"sept":9,"sep":9,"october":10,"oct":10,
           "november":11,"nov":11,"december":12,"dec":12}

# Icons to remove - we KEEP add_circle and manage_search as anchors
_ICON_NOISE = re.compile(
    r"\b(?:grid_view|verified|favorite|location_on|"
    r"calendar_month|format_list_bulleted|blur_on|scale|swipe_right|forum|"
    r"more_horiz|chevron_right|chevron_left)\b",
    re.IGNORECASE,
)

# Markers for the end of the fight card section
# warning: do not include "Cancellations" here - this word also appears in the
# Tapology navigation tabs "Fight Card | Cancellations | Weigh Ins" and
# would truncate the fight card before the bouts.
_END_MARKERS = re.compile(
    r"(?:Cancelled\s*&?\s*Fizzled|Awards?\s*&?\s*Bonuses?|"
    r"Fight\s*Referees?|Event\s*Discussion|Tapology\s*Predictions|"
    r"Update\s*/?\s*Claim\s*(?:Page|Event)|Quick\s*Card\b|"
    r"Dispute\s*Fight\s*Result|Fighter\s*Birthdays)",
    re.IGNORECASE,
)

# Promotions - patterns on the event NAME (regex, case-insensitive)
# warning: these patterns apply on the NAME first only to avoid
# false positives due to the Tapology navigation menu (which contains "UFC|ONE|PFL..."
# on ALL the pages, whatever the real promotion).
_PROMO_NAME_PATTERNS = {
    "UFC":           re.compile(r"\b(?:ufc\b|ultimate fighting championship)", re.I),
    "ONE":           re.compile(r"\bone\s+(?:championship|fc|friday fights|warrior series)\b", re.I),
    "PFL":           re.compile(r"\bpfl\b|\bprofessional fighters league\b", re.I),
    "KSW":           re.compile(r"\bksw\b|\bkonfrontacja\b", re.I),
    "BELLATOR":      re.compile(r"\bbellator\b", re.I),
    "RIZIN":         re.compile(r"\brizin\b", re.I),
    "BKFC":          re.compile(r"\bbkfc\b|\bbare knuckle\b", re.I),
    "LFA":           re.compile(r"\blfa\b|\blegacy fighting alliance\b", re.I),
    "CAGE WARRIORS": re.compile(r"\bcage warriors\b", re.I),
    "INVICTA":       re.compile(r"\binvicta\b", re.I),
    "BRAVE CF":      re.compile(r"\bbrave cf\b|\bbrave combat\b", re.I),
    "ACB":           re.compile(r"\bacb\b|\babsolute championship\b", re.I),
    "GLORY":         re.compile(r"\bglory\s+(?:kickboxing|\d)", re.I),
    "ROAD FC":       re.compile(r"\broad fc\b", re.I),
    "PANCRASE":      re.compile(r"\bpancrase\b", re.I),
    "SHOOTO":        re.compile(r"\bshooto\b", re.I),
    "ROAD TO UFC":   re.compile(r"\broad to ufc\b", re.I),
}


# PLAYWRIGHT - JS navigation to bypass Tapology's bot protection

import random

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

UA_STR = UA_POOL[0]

# Playwright anti-detection init script (masks webdriver + plugins + language)
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = {runtime: {}};
"""


def _navigate_and_extract(page, url: str) -> tuple[str | None, str | None, bool]:
    """Load a Tapology event.

    Strategy: direct goto with wait networkidle.
    If redirected to the home page -> blocked=True (IP ban / bot check).
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        # Direct goto (more reliable than the /fightcenter warmup + JS nav which
        # triggered the Cloudflare bot check).
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        except PWTimeout:
            pass

        # Variable wait to let the React JS render the content
        time.sleep(random.uniform(1.5, 2.5))

        final_url = page.url
        title     = page.title()

        # If Cloudflare/bot check -> redirects to the home page or a challenge
        home_urls = {
            "https://www.tapology.com",
            "https://www.tapology.com/",
            "https://www.tapology.com/fightcenter",
            "https://www.tapology.com/fightcenter/",
        }
        if final_url.rstrip("/") in {u.rstrip("/") for u in home_urls}:
            text_len = len(re.sub(r"\s+", " ", page.inner_text("body")).strip())
            LOG.error(f"Redirected to home (len={text_len}, url={final_url}) for {url}")
            return None, None, True

        text = re.sub(r"\s+", " ", page.inner_text("body")).strip()

        try:
            poster_url = page.evaluate(
                "() => { var m=document.querySelector('meta[property=\"og:image\"]'); return m?m.content:''; }"
            ) or ""
        except Exception:
            poster_url = ""

        # Block detection via the TITLE - the most reliable signature:
        # - Page event valide : "Event Name | MMA Event | Tapology"
        #                       "Event Name | Boxing Event | Tapology"
        #                       "Event Name | Kickboxing Event | Tapology"
        # - Banned/nav page  : "FightCenter | Tapology" or "Tapology | MMA..."
        # The title is set by the server BEFORE the JS, so reliable even under a ban.
        title_lower = title.lower()
        is_valid_title = (
            "| tapology" in title_lower
            and "fightcenter | tapology" not in title_lower
            and "tapology | mma" not in title_lower
            and len(title) > 20  # titre trop court = mauvaise page
        )

        blocked = (
            len(text) < 200
            or (final_url.rstrip("/") in {u.rstrip("/") for u in home_urls}
                and "/events/" in url)
            or not is_valid_title
        )
        if blocked:
            LOG.error(f"Blocked (title={title!r}, len={len(text)}) for {url}")
            return None, None, True

        LOG.info(f"Playwright OK: {final_url!r} | '{title[:60]}'")
        return text, poster_url or None, False

    except Exception as e:  # noqa: BLE001
        LOG.error(f"Playwright error: {type(e).__name__}: {e}")
        return None, None, False


def make_browser_page(p, proxy: str | None = None):
    """Open a configured Playwright browser + context + page (anti-bot).

    proxy: 'http://host:port' or 'http://user:pass@host:port' (None = direct)
    """
    # --no-proxy-server = ignore the Windows system proxy (WinHTTP, VPN, etc.)
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                   "--disable-dev-shm-usage", "--no-proxy-server"]
    # explicit proxy only if provided by the user (otherwise direct connection)
    browser = p.chromium.launch(headless=True, args=launch_args,
                                proxy={"server": proxy} if proxy else None)
    ua = random.choice(UA_POOL)
    ctx = browser.new_context(
        user_agent=ua,
        viewport={"width": random.choice([1280, 1366, 1440, 1920]),
                  "height": random.choice([720, 768, 900, 1080])},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    page = ctx.new_page()
    page.add_init_script(_STEALTH_SCRIPT)
    # playwright-stealth: deep patch against Cloudflare detection
    try:
        from playwright_stealth import Stealth
        Stealth().apply_stealth_sync(page)
    except (ImportError, Exception):
        pass  # optional, works without it
    return browser, ctx, page


def fetch_html_playwright(url: str, proxy: str | None = None):
    """Single-URL mode (compat): opens an ephemeral browser for 1 event."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        LOG.error("Playwright not installed. Run: py -m pip install playwright && py -m playwright install chromium")
        return None, None
    with sync_playwright() as p:
        browser, _, page = make_browser_page(p, proxy=proxy)
        text, poster, _blocked = _navigate_and_extract(page, url)
        browser.close()
        return text, poster


def fetch_text_selenium_event(url: str):
    """Selenium (real Chrome) to bypass Cloudflare - returns (inner_text, poster_url)."""
    import os, time as _time
    try:
        from scrape_mma import _get_selenium_driver
    except ImportError:
        LOG.error("scrape_mma not found - Selenium unavailable")
        return None, None
    try:
        drv = _get_selenium_driver()
    except Exception as e:
        LOG.error(f"Selenium driver impossible: {e}")
        return None, None
    try:
        # Visit the Tapology main page first (cookies / session)
        if "tapology.com/fightcenter" not in drv.current_url:
            drv.get("https://www.tapology.com/fightcenter")
            _time.sleep(3)
        drv.get(url)
        _time.sleep(5)  # let the React JS render
        title = drv.title or ""
        if "just a moment" in title.lower() or "cloudflare" in title.lower():
            LOG.warning("Cloudflare blocks Selenium too - solve the captcha in Chrome then run again")
            return None, None
        inner_text = drv.execute_script("return document.body ? document.body.innerText : ''") or ""
        # Look for the poster URL (og:image)
        poster_url = None
        try:
            el = drv.find_element("css selector", 'meta[property="og:image"]')
            poster_url = el.get_attribute("content") or None
        except Exception:
            pass
        return inner_text, poster_url
    except Exception as e:
        LOG.error(f"Selenium fetch event : {e}")
        return None, None


#  normalisation

def strip_accents(s: str) -> str:
    if not s: return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_name(s: str) -> str:
    if not s: return ""
    s = re.sub(r"\s*\([^()]*\)\s*", " ", s)
    s = re.sub(r"['''ʼ`´]", "", s)
    s = re.sub(r"[.\-]", " ", s)
    s = strip_accents(s).lower()
    return re.sub(r"\s+", " ", s).strip()


def slug_from_url(url: str) -> str:
    parts = [x for x in urlparse(url).path.split("/") if x]
    return parts[-1] if parts else ""


def detect_promotion(text: str, name: str = "") -> str:
    """Detect the promotion from the event NAME first.

    We avoid searching in `text` (whole page) because the Tapology navigation
    menu contains "UFC | ONE | PFL..." on ALL the pages -> false positives.
    Fallback on the first 200 chars of the text only (before the nav bar).
    """
    # 1. Absolute priority: the event name
    for promo, pat in _PROMO_NAME_PATTERNS.items():
        if pat.search(name):
            return promo

    # 2. Fallback: slug in the URL (text[:0]) - no page text here
    # We do NOT use the full text to avoid nav bar false positives.
    # Exception: we look in the first ~150 chars (before any nav link).
    header = text[:150]
    for promo, pat in _PROMO_NAME_PATTERNS.items():
        if pat.search(header):
            return promo

    return ""


# parsing - event metadata

def _dedup_event_name(raw_name: str) -> str:
    """Remove the redundant UPPERCASE version of the name in the Tapology header.

    Tapology often shows "SLUG ProperCase" where the ALL-CAPS slug
    precedes the real Title Case name. E.g.:
      "ONE FRIDAY FIGHTS 155 ONE Friday Fights 155" -> "ONE Friday Fights 155"
      "LUBSANOV VS. BAYONA Lubsanov vs. Bayona"     -> "Lubsanov vs. Bayona"
      "UFC 307 UFC 307: Pereira vs. Rountree"       -> "UFC 307: Pereira vs. Rountree"

    Return words[half:] (not just words[half:half*2]) to keep the full
    subtitle when the slug is shorter than the full name.
    """
    words = raw_name.split()
    n = len(words)
    if n < 4:
        return raw_name
    def norm_w(w: str) -> str:
        return w.lower().rstrip(".:")
    # Try every possible slug length (from the exact half down to floor(n/2))
    for half in range(n // 2, 0, -1):
        if half * 2 > n:
            continue
        first  = " ".join(norm_w(w) for w in words[:half])
        second = " ".join(norm_w(w) for w in words[half : half * 2])
        if first == second:
            # Return everything after the slug (not just the identical part)
            # to keep the subtitle: "UFC 307: Pereira vs. Rountree"
            return " ".join(words[half:])
    return raw_name


def _strip_event_subtitle(raw_name: str) -> str:
    """Remove the Tapology subtitle (Muay Thai, Kickboxing, MMA Event...).

    Tapology sometimes adds a description after the name when inner_text
    removes the pipes: "ONE FF 155 Muay Thai Kickboxing MMA Event".
    """
    raw_name = re.sub(
        r"\s*(?:[|│].*|(?:\bMuay\s+Thai\b|\bKickboxing\b|\bK-?1\b|\bBoxing\b|\bMMA\b"
        r"|\bGrappling\b|\bWrestling\b)\s+(?:&|\bEvent\b|\bCombat\b).*)$",
        "", raw_name, flags=re.I,
    ).strip()
    if len(raw_name) > 60:
        raw_name = " ".join(raw_name.split()[:6])
    return raw_name


def parse_event_meta(full_text: str, url: str, page_title: str = "") -> dict:
    """Parse the event metadata from the page text."""
    meta: dict = {"tapology_url": url, "slug": slug_from_url(url)}

    # Event name
    # strategy 0 (priority): playwright page title
    # Guaranteed format: "Event name | MMA Event | Tapology"
    # This is the most reliable source - set by the server, not by the JS.
    raw_name = ""
    if page_title:
        # Extract everything before the first " | "
        parts = re.split(r"\s*\|\s*", page_title)
        if len(parts) >= 2:
            candidate = parts[0].strip()
            # Filter generic titles (homepage, fightcenter)
            if candidate and len(candidate) > 2 and candidate.lower() not in (
                "tapology", "fightcenter", "mma event", "upcoming", "completed"
            ):
                raw_name = candidate

    # strategy 1: tapology header "fightcenter SLUG propercase upcoming|completed"
    # Works for ALL events (UFC, ONE, boxing, regional...)
    m_hdr = re.search(
        r"FIGHTCENTER\s+([A-Z][\w\s.':\-#&0-9]+?)\s+(?:UPCOMING|COMPLETED|RECENT|RESULTS)\b",
        full_text[:700],
    )
    if m_hdr:
        raw_name = re.sub(r"\s+", " ", m_hdr.group(1)).strip()
        raw_name = _dedup_event_name(raw_name)
        raw_name = _strip_event_subtitle(raw_name)

    # Strategie 2 (fallback) : promo keyword + terminateur
    if not raw_name:
        m_name = re.search(
            r"\b((?:UFC|ONE|PFL|Bellator|KSW|RIZIN|BKFC|LFA|Cage\s+Warriors|Invicta|"
            r"Road\s+to\s+UFC|Fight\s+Night|Brave\s+CF|Pancrase|Shooto|Rizin|"
            r"Reality\s+Fighting|Combat\s+FC)[\w\s:\-#.&]+?)(?:\s*\||\s*UPCOMING|\s*RECENT|\s*RESULTS)",
            full_text[:800], re.I,
        )
        if m_name:
            raw_name = re.sub(r"\s+", " ", m_name.group(1)).strip()
            raw_name = _dedup_event_name(raw_name)
            raw_name = _strip_event_subtitle(raw_name)

    # strategy 3 (last resort): first token before upcoming/completed
    if not raw_name:
        m2 = re.search(r"^(.+?)\s+(?:UPCOMING|RECENT|COMPLETED|MMA\s+EVENT)", full_text[:500], re.I)
        if m2:
            raw_name = re.sub(r"\s+", " ", m2.group(1)).strip()
            raw_name = _dedup_event_name(raw_name)
            raw_name = _strip_event_subtitle(raw_name)

    # Strategy 4: og:title in the text ("Pacquiao vs. Provodnikov | Event | Tapology")
    if not raw_name:
        m_title = re.search(r'"og:title"\s+content="([^"]+)"', full_text)
        if not m_title:
            # inner_text may contain "TITLE | Event | Tapology" in the first 300 chars
            m_title = re.search(
                r"^([^\n|]{4,80}?)\s*\|\s*(?:Event|MMA Event|Upcoming|Completed)\b",
                full_text[:400], re.I,
            )
        if m_title:
            candidate = re.sub(r"\s+", " ", m_title.group(1)).strip().rstrip("|").strip()
            # Remove the " | Tapology" suffix or similar
            candidate = re.sub(r"\s*\|\s*Tapology.*$", "", candidate, flags=re.I).strip()
            if 3 < len(candidate) < 80:
                raw_name = candidate

    # Strategy 5 (ultimate): cleaned URL slug
    # Ex: "140205-pacquiao-vs-provodnikov" → "Pacquiao vs Provodnikov"
    if not raw_name:
        slug_clean = re.sub(r"^\d+-", "", meta.get("slug", "")).replace("-", " ")
        if slug_clean:
            raw_name = " ".join(w.capitalize() for w in slug_clean.split())

    meta["name"] = raw_name

    # Date
    meta["date"] = None
    # format: "MAY 22, 2026" or "may 22, 2026"
    m_dt = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+"
        r"(\d{1,2}),?\s+(\d{4})",
        full_text, re.I,
    )
    if m_dt:
        try:
            meta["date"] = date(
                int(m_dt.group(3)),
                _MONTHS[m_dt.group(1).lower()],
                int(m_dt.group(2)),
            ).isoformat()
        except (ValueError, KeyError):
            pass

    if not meta["date"]:
        # Format: "09.27.2025" or "06.14.2026"
        m_dt2 = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", full_text)
        if m_dt2:
            try:
                meta["date"] = date(int(m_dt2.group(3)), int(m_dt2.group(1)), int(m_dt2.group(2))).isoformat()
            except ValueError:
                pass

    # Venue + Lieu
    # Tapology shows: location_on <Venue> <City>, <Country>
    # We first extract the location_on section, then parse city/country/venue inside it.
    meta["venue"]   = None
    meta["city"]    = None
    meta["country"] = None

    # Strategy 1: explicit "Venue:" and "Location:" blocks (Tapology detail panel)
    # Ex : "Venue: Delta Center Location: Salt Lake City, Utah, United States Enclosure: Octagon"
    # Strategy: capture the whole Location: block up to the next metadata field, then split on ","
    m_venue_explicit = re.search(
        r"\bVenue:\s*(.{1,100}?)(?=\s*(?:Location:|Enclosure:|TV\s+Announcers:|Ownership:|Ring\s+Announcer:|Ticket|Attendance:|MMA\s+Bouts:|Promotion\s+Links:|$))",
        full_text, re.I,
    )
    m_loc_explicit = re.search(
        r"\bLocation:\s*(.{1,100}?)(?=\s*(?:Enclosure:|TV\s+Announcers:|Ownership:|Ring\s+Announcer:|Ticket\s+Revenue|Attendance:|MMA\s+Bouts:|Promotion\s+Links:|$))",
        full_text, re.I,
    )
    if m_venue_explicit:
        v = m_venue_explicit.group(1).strip().rstrip(".,;")
        if v and len(v) < 80 and v.lower() not in ("n/a", "tba", "unknown"):
            meta["venue"] = v
    if m_loc_explicit:
        loc_val = m_loc_explicit.group(1).strip().rstrip(".,;")
        parts   = [p.strip() for p in loc_val.split(",") if p.strip()]
        # "Salt Lake City, Utah, United States" → ['Salt Lake City', 'Utah', 'United States']
        # "Washington, D.C."                   → ['Washington', 'D.C.']
        # "Bangkok, Thailand"                  → ['Bangkok', 'Thailand']
        if len(parts) >= 3:
            meta["city"]    = parts[0]
            meta["country"] = parts[-1]
        elif len(parts) == 2:
            meta["city"]    = parts[0]
            meta["country"] = parts[1]
        elif len(parts) == 1:
            meta["country"] = parts[0]

    # Strategy 2: location_on icon (material icon in inner_text)
    if not meta.get("city"):
        m_loc_block = re.search(
            r"\blocation_on\b\s+(.+?)(?=\s*(?:\bcalendar_month\b|\bformat_list\b|\bFIGHT\b|\bCARD\b|\bUPCOMING\b|\bRECENT\b|\d{4}))",
            full_text, re.I,
        )
        loc_text = re.sub(r"\s+", " ", m_loc_block.group(1)).strip() if m_loc_block else ""

        # "Cookies" is in the GDPR text ("Cookies, IDs...") - exclude it explicitly
        _CITY_COUNTRY = re.compile(
            r"(Bangkok|Tokyo|Las Vegas|London|Paris|New York|Sao Paulo|Singapore|Macau|Manila|"
            r"Seoul|Jakarta|Abu Dhabi|Dublin|Amsterdam|Warsaw|Berlin|Sydney|Melbourne|"
            r"Salt Lake City|Anaheim|Miami|Dallas|Houston|San Francisco|Chicago|Boston|Denver|"
            r"Atlanta|Sacramento|Washington|Portland|Nashville|Phoenix|Detroit|"
            r"Irkutsk|Moscow|St\.?\s*Petersburg|Krasnodar|"
            r"Davao\s+City|Davao|Cebu\s+City|Cebu|Quezon\s+City|Quezon|"
            r"Kuala\s+Lumpur|Johor\s+Bahru|Penang|"
            r"(?!Cookies|Precise|Store|Access|Data|Personal|Partner|Purpose|Vendor|Consent)"
            r"[A-Z][a-z]{4,}),\s*"    # generic: at least 5 chars, excludes GDPR words
            r"(Thailand|Japan|United\s+States|USA|UK|United\s+Kingdom|France|Brazil|"
            r"Russia|Philippines|South\s+Korea|Indonesia|UAE|Ireland|Netherlands|Poland|"
            r"Germany|Australia|Malaysia|Canada|Italy|Spain|Sweden|Norway|"
            r"TH|JP|US|GB|FR|BR|RU|PH|KR|ID|AE|IE|NL|PL|DE|AU|MY|CA|IT|ES|SE|NO)",
        )
        for search_text in (loc_text, full_text):
            m_cc = _CITY_COUNTRY.search(search_text)
            if m_cc:
                city    = m_cc.group(1).strip()
                country = m_cc.group(2).strip()
                meta["city"]    = city
                meta["country"] = country
                # Venue = text before the city in loc_text (if available)
                if loc_text and not meta.get("venue"):
                    pre = loc_text[:loc_text.lower().find(city.lower())].strip()
                    if pre and len(pre) < 60:
                        meta["venue"] = pre
                break

    # Promotion
    meta["promotion"] = detect_promotion(full_text[:3000], meta.get("name", ""))

    # Fallback: if the promotion is unknown, extract it from the event name.
    # "Lion Championship 31" → "Lion Championship"
    # "JFL Fight Night 22"   → "JFL Fight Night"
    # "DEEP Hamamatsu Impact 2026 1st Round" → "DEEP"
    if not meta["promotion"] and meta.get("name"):
        _promo_strip = re.sub(
            r"\s+(?:\d+|[IVXLCDM]{1,6}|#\s*\d+)[\s:.\-].*$"   # trailing number/roman/edition
            r"|\s+(?:\d+|[IVXLCDM]{1,6})$",                     # trailing number/roman at end
            "", meta["name"], flags=re.I,
        ).strip()
        if _promo_strip and len(_promo_strip) >= 2:
            meta["promotion"] = _promo_strip

    # Status
    if meta.get("date"):
        try:
            d = date.fromisoformat(meta["date"])
            meta["status"] = "upcoming" if d >= date.today() else "completed"
        except ValueError:
            meta["status"] = "completed"
    else:
        # Look for an indicator in the text
        if re.search(r"\bUPCOMING\b", full_text[:500]):
            meta["status"] = "upcoming"
        else:
            meta["status"] = "completed"

    return meta


#  patterns regex fight card

# Bout label etendu : UFC, ONE, boxing, grappling, etc.
# includes combined labels like "MUAY THAI MAIN event", "kickboxing CO-MAIN", "MMA prelim"
_BOUT_LABEL_PATTERN = re.compile(
    r"\b"
    r"((?:(?:Muay\s+Thai|Kickboxing|K-?1|Boxing|MMA|Grappling|Submission\s+Grappling|Wrestling)\s+)?"
    r"(?:Main\s+Event|Co[-\s]?Main(?:\s+Event)?|Main\s+Card|Early\s+Prelims?|Prelims?|Featured\s+Bout|"
    r"Super\s+(?:Main\s+)?Series|ONE\s+Super\s+Series|Opening\s+Bout|Super\s+Bout|"
    r"Championship\s+Bout|Title\s+Bout|Amateur(?:\s+MMA)?(?:\s+Bout)?))\s+"
    r"(\d{2,3})\s+"                 # weight in lbs
    r"(\d{1,2})\s*[xX×]\s*(\d{1,2})"  # rounds (N x R) - boxing can have 10/12 rounds
    r"(?:\s*\|\s*(?:Pro|Am))?"     # | Pro / | Am (optional)
    r"(?:\s*#\s*(\d+))?",          # bout number (optional)
    re.IGNORECASE,
)

# Discipline alone as a label (ONE Friday Fights style)
# e.g. "MUAY THAI 135 3 x 3 # 14" without a section
_DISCIPLINE_ONLY_LABEL = re.compile(
    r"\b(Muay\s+Thai|Kickboxing|K-?1|Boxing|MMA\s+Bout|Grappling\s+Bout|Submission\s+Grappling)\s+"
    r"(\d{2,3})\s+"
    r"(\d{1,2})\s*[xX×]\s*(\d{1,2})"  # boxing can have 10/12 rounds
    r"(?:\s*\|\s*(?:Pro|Am))?"
    r"(?:\s*#\s*(\d+))?",
    re.IGNORECASE,
)

# Method for completed events (old static HTML format)
_METHOD_KO_SUB = re.compile(
    r"\b(KO/TKO|TKO|KO|Submission|Sub|DQ|No\s+Contest|NC|Draw)"
    r"(?:\s*,\s*([^,\n]+?))?\s+(\d+:\d{2})\s+Round\s+(\d+)",
    re.IGNORECASE,
)
_METHOD_DEC = re.compile(
    r"\b(Decision)\s*,\s*(Split|Unanimous|Majority|Technical)\s+(\d+)\s+Rounds",
    re.IGNORECASE,
)

# Titre
_TITLE_PATTERN = re.compile(
    r"\b((?:UFC|ONE|PFL|Bellator|Interim|Vacant|Undisputed)\s+(?:Interim\s+)?"
    r"(?:Light\s+Heavyweight|Heavyweight|Middleweight|Welterweight|Lightweight|"
    r"Featherweight|Bantamweight|Flyweight|Strawweight|Atomweight)\s+"
    r"(?:Championship|Title))\b",
    re.IGNORECASE,
)


#  utilitaires

def extract_fight_card_section(full_text: str) -> str:
    """Isolate the fight card section in the page text.

    Cascading strategy:
    1. Look for a known section title (FIGHT CARD, CARD, LINEUP...)
    2. If absent but add_circle is present -> fallback: return from
       300 chars before the first add_circle (captures the F1 block).
    """
    # 1. Known section titles (in order of preference)
    # NOTE: "FIGHT CARD" (uppercase) = body section; "Fight Card" = nav tab.
    # We try CASE-SENSITIVE first to target the body section only.
    for pat, flags in [
        (r"\bFIGHT\s+CARD\b",   0),        # uppercase = section body Tapology
        (r"\bFight\s+Card\b",   re.I),     # fallback case-insensitive
        (r"\bBOUT\s+CARD\b",    re.I),
        (r"\bBOUT\s+LINEUP\b",  re.I),
    ]:
        m = re.search(pat, full_text, flags)
        if not m:
            continue
        text = full_text[m.end():]
        m_end = _END_MARKERS.search(text)
        if m_end:
            text = text[:m_end.start()]
        # Valid: must contain at least one add_circle OR a SoS pattern OR be long
        if text.strip() and ("add_circle" in text.lower() or
                             re.search(r"\bStrength\s+of\s+Schedule\b", text, re.I) or
                             len(text) > 300):
            return text.strip()

    # 2. add_circle fallback: the fight card is rendered in JS without a section title
    m_ac = re.search(r"\badd_circle\b", full_text, re.I)
    if m_ac:
        # Go back 400 chars to include the F1 block of the first bout
        start = max(0, m_ac.start() - 400)
        text = full_text[start:]
        m_end = _END_MARKERS.search(text)
        if m_end:
            text = text[:m_end.start()]
        return text.strip()

    return ""


def clean_text_keep_seps(s: str) -> str:
    """Strip icons except add_circle and manage_search, normalize whitespace."""
    s = _ICON_NOISE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _wc_from_lbs(lbs: int | None) -> str:
    if not lbs: return ""
    return {
        105: "Atomweight", 108: "Atomweight", 112: "Strawweight", 115: "Strawweight",
        125: "Flyweight", 135: "Bantamweight", 145: "Featherweight", 155: "Lightweight",
        165: "Super Lightweight", 170: "Welterweight", 175: "Super Welterweight",
        185: "Middleweight", 205: "Light Heavyweight", 225: "Cruiserweight", 265: "Heavyweight",
    }.get(lbs, "")


def _wc_abbrev_to_full(wc: str) -> str:
    wc = re.sub(r"^(?:UFC|ONE|PFL)\s+", "", wc, flags=re.I).strip()
    mapping = [
        ("Light HW", "Light Heavyweight"), ("Light Heavyweight", "Light Heavyweight"),
        ("HeavyW", "Heavyweight"), ("MiddleW", "Middleweight"), ("WelterW", "Welterweight"),
        ("LightW", "Lightweight"), ("FeatherW", "Featherweight"), ("BantamW", "Bantamweight"),
        ("StrawW", "Strawweight"), ("AtomW", "Atomweight"), ("FlyW", "Flyweight"),
    ]
    for short, full in mapping:
        if wc.lower() == short.lower():
            return full
    return wc


def _label_to_section(label: str) -> str:
    ll = label.lower()
    if "early prelim" in ll: return "Early Prelims"
    if "prelim"       in ll: return "Prelims"
    return "Main Card"


def _norm_method(raw: str | None) -> str | None:
    if not raw: return None
    return {"KO/TKO":"KO/TKO","TKO":"TKO","KO":"KO","SUBMISSION":"SUB","SUB":"SUB",
            "DEC":"DEC","DECISION":"DEC","DQ":"DQ","NO CONTEST":"NC","NC":"NC",
            "DRAW":"DRAW"}.get(raw.upper(), raw)


# NAME extraction from a text block

# Words to exclude from names: divisions, promotions, labels, disciplines, etc.
# warning: include the isolated words (e.g. "MUAY" + "THAI" appear separately in ONE events)
_NON_NAME_WORDS = re.compile(
    r"^(?:UFC|ONE|PFL|Bellator|KSW|RIZIN|BKFC|LFA|UBJJ|Unranked|Pro|Am|"
    r"Heavyweight|Middleweight|Welterweight|Lightweight|Featherweight|"
    r"Bantamweight|Flyweight|Strawweight|Atomweight|"
    r"HeavyW|MiddleW|WelterW|LightW|FeatherW|BantamW|FlyW|StrawW|AtomW|"
    r"Light\s+HW|Main\s+Card|Main\s+Event|Co.Main|Prelim|Featured|"
    r"Muay|Thai|Muay\s+Thai|Kickboxing|K1|Boxing|MMA|Grappling|Wrestling|"
    r"Championship|Bout|Combat|Series|Super|Opening|Title|Card|Event|"
    r"League|Fighting|Submission|Lethwei|Sambo|Judo|Karate|Taekwondo|Round|Total|"
    r"Decision|Split|Unanimous|Majority|Uppercuts|Overhand|Hook|Punch|Punches|"
    r"Injury|Stoppage|Guillotine|Choke|Lock|Americana|Triangle|Armbar|Kneebar|"
    # Cities / places frequent in ONE/Asia Pacific profiles (never fighter names)
    r"Bangkok|Phuket|Irkutsk|Moscow|Osaka|Singapore|"
    r"Davao|Cebu|Quezon|Makati|Taguig|Tagum|Bislig|"
    r"Buenavista|Pantukan|Marfori|Laguna|Angeles|"
    r"Lumpur|Penang|Johor|Krasnodar|"
    # Generic location words
    r"City|Heights|Region|Province|District)$",
    re.IGNORECASE,
)

_KNOWN_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
_CONNECTORS     = {"dos", "de", "da", "van", "von", "el", "la", "al", "bin", "binti", "por"}


def _extract_name_from_text(text: str, take_last: bool = True) -> str:
    """
    Extract a fighter name from a text block.

    - If manage_search is present: the name is EVERYTHING before it
    - Otherwise: extract the last (take_last=True) or first (False)
      sequence of Title Case words, stopping at the record (N-N) or division

    Also handles the Tapology format for COMPLETED events which adds:
      "W/L Name Up/Down to X-Y" (result badge + record change)
    """
    t = text.strip()
    if not t:
        return ""

    # Clean the artifacts of the Tapology "completed" format:
    # 1. "Up to 12-2" / "Down to 13-6" (record change after the fight)
    t = re.sub(r"\b(?:Up|Down)\s+to\s+\d+[-–]\d+(?:[-–]\d+)?\b", "", t, flags=re.I)
    # 2. Isolated W/L badges just BEFORE a name (e.g. "W Alex", "L Khalil")
    # We only remove the W/L occurrences that precede a capital letter.
    # We repeat until stable to handle the "W L Name" cases.
    for _ in range(3):
        prev = t
        t = re.sub(r"(?:^|(?<=\s))\b([WL])\b(?=\s+[A-Z])", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t == prev:
            break
    # 3a. Method info at the start of the F1 block (take_last=True).
    # The completed F1 block starts with the title + method, then "Total N", then the name.
    #     Ex: "UFC LHW Championship KO/TKO, UPPERCUTS 4:32 Round 4 of 5, 19:32 Total 3 Alex Pereira"
    if take_last:
        # Remove everything before "Total N " if present (method before the name)
        t_stripped = re.sub(r"^.*?\bTotal\s+\d+\s*", "", t, flags=re.I).strip()
        if t_stripped:
            t = t_stripped
        else:
            # Fallback: remove the method part before the name by looking for the first residual W/L
            # If no "Total", try removing up to the first isolated Title Case token (= the name)
            t = re.sub(r"^(?:.*?\b(?:Round|Rounds|,)\b)+\s*", "", t, flags=re.I).strip() or t
        t = re.sub(r"\s+", " ", t).strip()

    # 3b. For F2 (take_last=False): remove the method info of the NEXT FIGHT
    # that appears after the F2 name when there is no "Total N" separator.
    # E.g.: "José Aldo KO/TKO, RIB INJURY 5:00 Round 1 of 3 Roman Dolidze"
    if not take_last:
        t = re.sub(
            r"\b(?:KO[/\\]TKO|TKO|KO|Decision|Unanimous|Majority|Split|"
            r"Submission|SUB|No\s+Contest|Draw|Disqualification|DQ)\b.*$",
            "", t, flags=re.I,
        ).strip()
        t = re.sub(r"\s+", " ", t).strip()

    # Strategy 1: manage_search as a precise anchor
    if "manage_search" in t.lower():
        parts = re.split(r"\s+manage_search\s*", t, flags=re.I)
        # take_last=True -> last fragment before manage_search
        # take_last=False → premier fragment
        relevant = parts[-2] if (take_last and len(parts) >= 2) else parts[0]
        name = _clean_name_block(relevant, last_words=take_last)
        if name:
            return name

    # Strategie 2 : extraction Title Case
    # Remove the parenthesized content (region, division, nickname...)
    # Ex: "Noel Castillano 0-1 # 214 FlyW (Asia Southeast)" → "Noel Castillano 0-1 # 214 FlyW"
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Remove the non-name suffixes (record, rank, score, promotion)
    cleaned = t
    # Stop at the first record if take_last=False
    if not take_last:
        m_rec = re.search(r"\b\d+-\d+(?:-\d+)?\b", cleaned)
        if m_rec:
            cleaned = cleaned[:m_rec.start()]
        # Stop at the first discipline marker
        m_disc = re.search(
            r"\b(?:Muay\s+Thai|Kickboxing|K-?1|Boxing|MMA|Grappling|Unranked|"
            r"#\s*\d+|UFC|ONE|PFL)\b",
            cleaned, re.I,
        )
        if m_disc:
            cleaned = cleaned[:m_disc.start()]
    else:
        # For F1: remove the non-name elements from the end
        # NOTE: the 2-letter ISO patterns (TH, RU...) must be case-sensitive
        # (re.I would match "Yi", "Do", "La"... and truncate valid names).
        for pat in [
            r"\s+\d+\s*$",                              # score trailing
            r"\s+(?:Light\s+HW|[A-Z][a-z]+W)\s*$",     # division abreg.
            r"\s+(?:UFC|ONE|PFL|Bellator|Unranked)\s*$",
            r"\s+#\s*\d+\s*$",                          # rank
            r"\s+\d+-\d+(?:-\d+)?\s*$",                 # record
            r"\s+\w{2,20},\s+\w{2,20}\s*$",             # "Bangkok, Thailand" or "Bangkok, TH" - BEFORE country-only
        ]:
            cleaned = re.sub(pat, "", cleaned, flags=re.I).strip()
        # ISO country codes - WITHOUT re.I so as not to strip "Yi", "Do", "La", etc.
        for pat_cs in [
            r"\s*,\s*[A-Z]{2}$",                        # ", TH" (majuscules uniquement)
            r"\s+[A-Z]{2}$",                            # "TH" alone at the end of the string
        ]:
            cleaned = re.sub(pat_cs, "", cleaned).strip()

    return _clean_name_block(cleaned, last_words=take_last)


def _clean_name_block(block: str, last_words: bool = True) -> str:
    """Extract the last or first sequence of Title Case words."""
    words = block.strip().split()
    if not words:
        return ""

    groups: list[list[str]] = []
    cur: list[str] = []

    for w in words:
        clean = w.strip(".,;:()")
        if not clean:
            continue
        lower = clean.lower()
        # filtre codes pays ISO 2 lettres (TH, RU, JP, US...) : 2 lettres majuscules
        is_country_code = (len(clean) == 2 and clean.isupper())
        if (
            clean[0].isupper()
            and not _NON_NAME_WORDS.match(clean)
            and not re.match(r"^\d", clean)
            and not is_country_code
            or lower in _KNOWN_SUFFIXES | _CONNECTORS
        ):
            cur.append(clean)
        else:
            if cur:
                groups.append(cur)
                cur = []

    if cur:
        groups.append(cur)

    if not groups:
        return ""

    group = groups[-1] if last_words else groups[0]
    name = " ".join(group).strip()
    # Minimal validation: at least 2 chars, at least 1 space (first name + last name)
    if len(name) < 3 or len(group) < 1:
        return ""
    return name


#  parser principal — nouveau format tapology 2024+

def _parse_new_format(card_text: str, status: str) -> list[dict]:
    """
    New Tapology format: [F1 info] BOUT_LABEL weight NxR add_circle [F2 info]

    Keeps add_circle and manage_search in the text to use them as anchors.
    """
    # Clean the icons except add_circle and manage_search
    text = clean_text_keep_seps(card_text)
    if not text:
        return []

    # Find all the bout labels
    labels: list[dict] = []
    for pat in (_BOUT_LABEL_PATTERN, _DISCIPLINE_ONLY_LABEL):
        for m in pat.finditer(text):
            labels.append({
                "label":      re.sub(r"\s+", " ", m.group(1)).strip(),
                "weight_lbs": int(m.group(2)),
                "rounds":     f"{m.group(3)}x{m.group(4)}",
                "start":      m.start(),
                "end":        m.end(),
            })
    labels.sort(key=lambda x: x["start"])
    if not labels:
        return []

    # Dedup labels (discipline_only can match inside bout_label)
    deduped: list[dict] = []
    for lbl in labels:
        overlap = any(
            abs(lbl["start"] - prev["start"]) < 5
            for prev in deduped
        )
        if not overlap:
            deduped.append(lbl)
    labels = deduped

    # Find all the add_circle positions
    add_positions = [m.start() for m in re.finditer(r"\badd_circle\b", text, re.I)]

    bouts: list[dict] = []
    seen: set[tuple] = set()

    for lbl in labels:
        # F1: text between the previous add_circle (or start) and the label
        prev_add = max((p for p in add_positions if p < lbl["start"]), default=-1)
        f1_block = text[prev_add + len("add_circle") + 1 if prev_add >= 0 else 0 : lbl["start"]]

        # F2: text between the next add_circle and the next label (or end)
        next_add = next((p for p in add_positions if p > lbl["end"]), None)
        if next_add is None:
            continue
        f2_start = next_add + len("add_circle") + 1  # +1 to skip the space after "add_circle"

        # End of the f2 block: next bout label or end of text
        next_label_start = next(
            (l["start"] for l in labels if l["start"] > lbl["end"]),
            len(text),
        )
        # First add_circle STRICTLY AFTER next_add and before the next label
        # (= separator between the F2 of this bout and the F1 of the next one, if present)
        # warning: do not use next_add itself as the upper bound (previous bug)
        next_add_after = next(
            (p for p in add_positions if p > next_add and p < next_label_start),
            next_label_start,
        )
        f2_block = text[f2_start:next_add_after]

        # Name extraction
        f1_name = _extract_name_from_text(f1_block, take_last=True)
        f2_name = _extract_name_from_text(f2_block, take_last=False)

        if not f1_name or not f2_name:
            continue
        if f1_name.lower() == f2_name.lower():
            continue

        pair_key = tuple(sorted([f1_name.lower(), f2_name.lower()]))
        if pair_key in seen:
            continue
        seen.add(pair_key)

        # Records & ranks
        f1_rec  = _first_record(f1_block)
        f2_rec  = _first_record(f2_block)
        f1_rank = _first_rank(f1_block)
        f2_rank = _first_rank(f2_block)

        # Weight class
        wc = _wc_from_lbs(lbl["weight_lbs"]) or ""

        # Title fight ?
        title_text = ""
        for t in _TITLE_PATTERN.finditer(f1_block):
            title_text = t.group(1).strip()

        # Winner / method - detected from the W/L badges and the method text (completed format)
        winner_name = None
        method_raw = method_detail = time_str = None
        round_num = None

        if status == "completed":
            # Detect the W badge in the f1 block (before the name) or f2
            # Pattern: " W Name" or "^W Name" just before a Title Case name
            if re.search(r"(?:^|\s)\bW\b\s+[A-Z]", f1_block):
                winner_name = f1_name
            elif re.search(r"(?:^|\s)\bW\b\s+[A-Z]", f2_block):
                winner_name = f2_name
            # Method: extracted from the f1 block (contains the method of the current fight)
            # format: "KO/TKO, uppercuts 4:32 round 4 of 5"
            m_method = re.search(
                r"\b(KO[/\\]TKO|TKO|KO|Decision|Submission|SUB|No\s+Contest|Draw|DQ|Disqualification)\b",
                f1_block, re.I,
            )
            if m_method:
                method_raw = m_method.group(1).upper().replace("\\", "/")
                # detail after the method (e.g. "uppercuts", "split", "unanimous")
                after = f1_block[m_method.end():].lstrip(",. ")
                m_detail = re.match(r"([A-Z][A-Z\s]+?)(?:\s+\d|$)", after)
                if m_detail:
                    method_detail = m_detail.group(1).strip()
            # Round
            m_rnd = re.search(r"\bRound\s+(\d+)\s+of", f1_block, re.I)
            if m_rnd:
                round_num = int(m_rnd.group(1))
            # Temps (ex: "4:32")
            m_time = re.search(r"\b(\d{1,2}:\d{2})\b", f1_block)
            if m_time:
                time_str = m_time.group(1)

        bouts.append({
            "card_section":      _label_to_section(lbl["label"]),
            "bout_label":        lbl["label"],
            "bout_order":        len(bouts),
            "fighter1_name":     f1_name,
            "fighter2_name":     f2_name,
            "fighter1_record":   f1_rec,
            "fighter2_record":   f2_rec,
            "fighter1_ufc_rank": f1_rank,
            "fighter2_ufc_rank": f2_rank,
            "weight_class":      wc,
            "weight_lbs":        lbl["weight_lbs"],
            "rounds":            lbl["rounds"],
            "is_title_fight":    bool(title_text),
            "title_text":        title_text,
            "status":            status,
            "winner_name":       winner_name,
            "method":            _norm_method(method_raw),
            "method_detail":     method_detail,
            "round_num":         round_num,
            "time_str":          time_str,
        })

    return bouts


def _first_record(text: str) -> str | None:
    m = re.search(r"\b(\d+-\d+(?:-\d+)?)\b", text)
    return m.group(1) if m else None


def _first_rank(text: str) -> str | None:
    m = re.search(r"#\s*(\d+)", text)
    return m.group(1) if m else None


#  parser fallback — ancien format statique (W/L strength of schedule)

# Pattern fighter UPCOMING ancien format (Strength of Schedule)
_OLD_SOS_PATTERN = re.compile(
    r"([A-Z][\w\.'\-À-ſß]+(?:\s+[\w\.'\-À-ſß]+){0,4})\s+"
    r"(\d+-\d+(?:-\d+)?)\s+"
    r"(?:#\s*(\d+|[Nn][Rr]|--)\s+)?"
    r"(?:#\s*(\d+|[Nn][Rr]|--)\s+)?"
    r"((?:UFC\s+)?(?:Light\s+HW|Light\s+Heavyweight|HeavyW|LightW|BantamW|FeatherW|MiddleW|WelterW|FlyW|StrawW|AtomW|"
    r"Heavyweight|Middleweight|Welterweight|Lightweight|Featherweight|Bantamweight|Flyweight|Strawweight|Atomweight|"
    r"Open\s*Weight|Catchweight))\s+"
    r"(\d+|NA|--|N/A)\s+Strength\s+of\s+Schedule",
    re.IGNORECASE | re.UNICODE,
)

# Pattern fighter COMPLETED ancien format (W/L Up to/Down to)
_OLD_WL_PATTERN = re.compile(
    r"\b(W|L)\s+"
    r"([A-Z][\w\.'\-À-ſß]+(?:\s+[\w\.'\-À-ſß]+){0,4})"
    r"\s+(?:Up|Down)\s+to\s+(\d+-\d+(?:-\d+)?)\b",
    re.UNICODE,
)

# Old-format bout label (no discipline prefix)
_OLD_LABEL_PATTERN = re.compile(
    r"\b(Main\s+Event|Co[-\s]?Main(?:\s+Event)?|Main\s+Card|Early\s+Prelims?|Prelims?|Featured\s+Bout)\s+"
    r"(\d{2,3})\s+(\d\s*x\s*\d)(?:\s+#\s*(\d+))?",
    re.IGNORECASE,
)


def _parse_old_sos(card_text: str, status: str) -> list[dict]:
    """Old format with Strength of Schedule (pre-2024)."""
    text   = _ICON_NOISE.sub(" ", card_text)
    text   = re.sub(r"\badd_circle\b|\bmanage_search\b", " ", text, flags=re.I)
    text   = re.sub(r"\s+", " ", text).strip()

    fighters = []
    for m in _OLD_SOS_PATTERN.finditer(text):
        raw = m.group(1).strip()
        fighters.append({
            "name": raw, "record": m.group(2), "ufc_rank": m.group(4),
            "division": m.group(5).strip(), "start": m.start(), "end": m.end(),
        })

    labels = []
    for m in _OLD_LABEL_PATTERN.finditer(text):
        labels.append({
            "label": re.sub(r"\s+", " ", m.group(1)).strip(),
            "weight_lbs": int(m.group(2)),
            "rounds": re.sub(r"\s+", "", m.group(3)),
            "start": m.start(), "end": m.end(),
        })

    bouts = []
    seen  = set()
    used  = set()

    for lbl in labels:
        f1_idx = None
        for i, f in enumerate(fighters):
            if i in used: continue
            if f["end"] <= lbl["start"]: f1_idx = i
            else: break
        f2_idx = None
        for i, f in enumerate(fighters):
            if i in used: continue
            if f["start"] >= lbl["end"]:
                f2_idx = i; break
        if f1_idx is None or f2_idx is None:
            continue
        f1, f2 = fighters[f1_idx], fighters[f2_idx]
        pair = tuple(sorted([f1["name"].lower(), f2["name"].lower()]))
        if pair in seen: continue
        seen.add(pair); used.add(f1_idx); used.add(f2_idx)

        wc = _wc_abbrev_to_full(re.sub(r"^(?:UFC|ONE)\s+", "", f1["division"], flags=re.I))
        bouts.append(_make_upcoming_bout(
            card_section=_label_to_section(lbl["label"]),
            bout_label=lbl["label"], bout_order=len(bouts),
            f1_name=f1["name"], f2_name=f2["name"],
            f1_record=f1["record"], f2_record=f2["record"],
            f1_rank=f1["ufc_rank"], f2_rank=f2["ufc_rank"],
            weight_class=wc, weight_lbs=lbl["weight_lbs"], rounds=lbl["rounds"],
            title_text="", status=status,
        ))
    return bouts


def _parse_old_wl(card_text: str, status: str) -> list[dict]:
    """Old format with W/L Up to/Down to (pre-2024)."""
    text = _ICON_NOISE.sub(" ", card_text)
    text = re.sub(r"\badd_circle\b|\bmanage_search\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()

    fighters = []
    for m in _OLD_WL_PATTERN.finditer(text):
        fighters.append({
            "result": m.group(1).upper(), "name": m.group(2).strip(),
            "record": m.group(3), "start": m.start(), "end": m.end(),
        })

    labels = []
    for m in _OLD_LABEL_PATTERN.finditer(text):
        labels.append({
            "label": re.sub(r"\s+", " ", m.group(1)).strip(),
            "weight_lbs": int(m.group(2)),
            "rounds": re.sub(r"\s+", "", m.group(3)),
            "start": m.start(), "end": m.end(),
        })

    methods = _collect_methods_old(text)
    bouts   = []
    seen    = set()
    used_f  = set()
    used_m  = set()

    for lbl in labels:
        f1_idx = None
        for i, f in enumerate(fighters):
            if i in used_f: continue
            if f["end"] <= lbl["start"]: f1_idx = i
            else: break
        f2_idx = None
        for i, f in enumerate(fighters):
            if i in used_f: continue
            if f["start"] >= lbl["end"]:
                f2_idx = i; break
        if f1_idx is None or f2_idx is None:
            continue
        f1, f2 = fighters[f1_idx], fighters[f2_idx]
        pair = tuple(sorted([f1["name"].lower(), f2["name"].lower()]))
        if pair in seen: continue
        seen.add(pair); used_f.add(f1_idx); used_f.add(f2_idx)

        method, m_idx = _find_method_before(methods, f1["start"], used_m)
        if method: used_m.add(m_idx)

        winner = f1["name"] if f1["result"] == "W" else (f2["name"] if f2["result"] == "W" else None)
        m_raw  = method["method_raw"] if method else None
        bouts.append({
            "card_section":      _label_to_section(lbl["label"]),
            "bout_label":        lbl["label"],
            "bout_order":        len(bouts),
            "fighter1_name":     f1["name"],
            "fighter2_name":     f2["name"],
            "fighter1_record":   f1["record"],
            "fighter2_record":   f2["record"],
            "fighter1_ufc_rank": None,
            "fighter2_ufc_rank": None,
            "weight_class":      _wc_from_lbs(lbl["weight_lbs"]) or "",
            "weight_lbs":        lbl["weight_lbs"],
            "rounds":            lbl["rounds"],
            "is_title_fight":    False,
            "title_text":        "",
            "status":            status,
            "winner_name":       winner,
            "method":            _norm_method(m_raw),
            "method_detail":     method["method_detail"] if method else None,
            "round_num":         method["round_num"] if method else None,
            "time_str":          method["time_str"] if method else None,
        })
    return bouts


def _collect_methods_old(text: str) -> list[dict]:
    methods = []
    for m in _METHOD_KO_SUB.finditer(text):
        methods.append({"method_raw": m.group(1).upper(), "method_detail": (m.group(2) or "").strip(),
                        "time_str": m.group(3), "round_num": int(m.group(4)),
                        "start": m.start(), "end": m.end()})
    for m in _METHOD_DEC.finditer(text):
        methods.append({"method_raw": "DEC", "method_detail": m.group(2).capitalize(),
                        "time_str": None, "round_num": int(m.group(3)),
                        "start": m.start(), "end": m.end()})
    methods.sort(key=lambda x: x["start"])
    return methods


def _find_method_before(methods, pos, used):
    chosen, idx = None, None
    for i, m in enumerate(methods):
        if i in used: continue
        if m["end"] <= pos: chosen, idx = m, i
        else: break
    return chosen, idx


def _make_upcoming_bout(*, card_section, bout_label, bout_order,
                        f1_name, f2_name, f1_record, f2_record, f1_rank, f2_rank,
                        weight_class, weight_lbs, rounds, title_text, status) -> dict:
    return {
        "card_section": card_section, "bout_label": bout_label, "bout_order": bout_order,
        "fighter1_name": f1_name, "fighter2_name": f2_name,
        "fighter1_record": f1_record, "fighter2_record": f2_record,
        "fighter1_ufc_rank": f1_rank, "fighter2_ufc_rank": f2_rank,
        "weight_class": weight_class, "weight_lbs": weight_lbs, "rounds": rounds,
        "is_title_fight": bool(title_text), "title_text": title_text,
        "status": status, "winner_name": None, "method": None,
        "method_detail": None, "round_num": None, "time_str": None,
    }


# parse_fight_card orchestration

def parse_fight_card(full_text: str, status: str) -> tuple[list[dict], str]:
    """Orchestrate the parsers in cascade. Return (bouts, strategy_name)."""
    card_text = extract_fight_card_section(full_text)
    if not card_text:
        return [], "no_card_text"

    # Stage 1: nouveau format Tapology (add_circle + manage_search)
    bouts = _parse_new_format(card_text, status)
    if bouts:
        return bouts, "new_format"

    # Stage 2: ancien format SoS (pre-2024)
    bouts = _parse_old_sos(card_text, status)
    if bouts:
        return bouts, "old_sos"

    # Stage 3: ancien format W/L (completed)
    bouts = _parse_old_wl(card_text, status)
    if bouts:
        return bouts, "old_wl"

    return [], "no_match"


#  DB

def load_fighter_index(conn) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM fighters")
    idx = {}
    for fid, name in cur.fetchall():
        key = norm_name(name)
        if key: idx[key] = fid
    return idx


def resolve_fighter_id(name: str, idx: dict[str, int]) -> int | None:
    return idx.get(norm_name(name)) if name else None


def find_matching_event(conn, meta: dict, bouts: list[dict]) -> int | None:
    """
    Find an event ALREADY in the database matching the one we are scraping, EVEN if it
    comes from another source (Tapology vs ESPN -> different URLs).

    Strategy:
      1. Exact match by URL (re-scrape of the same source).
      2. Cross-source: same date +/-1 day + common fighter pairs.
      3. Fallback: same date +/-1 day + very similar name (common tokens).
         Covers the case where the candidate event has no bouts in the database yet.
    """
    cur = conn.cursor()

    # 1. URL exacte
    if meta.get("tapology_url"):
        cur.execute("SELECT id FROM events WHERE tapology_url = %s", (meta["tapology_url"],))
        row = cur.fetchone()
        if row:
            return row[0]

    if not meta.get("date"):
        return None

    # Candidates by date (+/-1 day to cover ESPN/Tapology timezone offsets)
    cur.execute("SELECT id FROM events WHERE date BETWEEN %s::date - 1 AND %s::date + 1",
                (meta["date"], meta["date"]))
    candidate_ids = [r[0] for r in cur.fetchall()]
    if not candidate_ids:
        return None

    best_id, best_shared = None, 0

    # 2. Fighter pairs
    if bouts:
        pairs = set()
        for b in bouts:
            a  = norm_name(b.get("fighter1_name", ""))
            bb = norm_name(b.get("fighter2_name", ""))
            if len(a) > 3 and len(bb) > 3:
                pairs.add(frozenset((a, bb)))
        if pairs:
            for cid in candidate_ids:
                cur.execute(
                    "SELECT fighter1_name, fighter2_name FROM event_fights WHERE event_id = %s",
                    (cid,),
                )
                cpairs = set()
                for f1, f2 in cur.fetchall():
                    a, bb = norm_name(f1 or ""), norm_name(f2 or "")
                    if len(a) > 3 and len(bb) > 3:
                        cpairs.add(frozenset((a, bb)))
                if not cpairs:
                    continue
                shared = pairs & cpairs
                ratio  = len(shared) / max(1, min(len(pairs), len(cpairs)))
                if (len(shared) >= 2 or ratio >= 0.5) and len(shared) > best_shared:
                    best_id, best_shared = cid, len(shared)

    if best_id:
        return best_id

    # 3. Name fallback: at least 60% of the long tokens in common
    if meta.get("name"):
        norm_new = {w for w in norm_name(meta["name"]).split() if len(w) > 3}
        if norm_new:
            for cid in candidate_ids:
                cur.execute("SELECT name FROM events WHERE id = %s", (cid,))
                row = cur.fetchone()
                if not row or not row[0]:
                    continue
                norm_old = {w for w in norm_name(row[0]).split() if len(w) > 3}
                if not norm_old:
                    continue
                shared = norm_new & norm_old
                ratio  = len(shared) / max(1, min(len(norm_new), len(norm_old)))
                if ratio >= 0.6 and len(shared) >= 2 and len(shared) > best_shared:
                    best_id, best_shared = cid, len(shared)

    return best_id


def upsert_event(conn, meta: dict, awards: list, bouts: list[dict] | None = None) -> tuple[int, str]:
    cur = conn.cursor()
    eid = find_matching_event(conn, meta, bouts or [])
    if eid:
        # We NEVER downgrade a completed event to upcoming: if the existing event
        # already has results and the new scrape is "upcoming", we keep
        # the completed status. Same for the poster (we keep the existing one if better).
        cur.execute("SELECT status, tapology_url, poster_url FROM events WHERE id=%s", (eid,))
        old_status, old_url, old_poster = cur.fetchone()
        new_status = meta.get("status")
        # do not demote completed -> upcoming
        if old_status == "completed" and new_status == "upcoming":
            new_status = "completed"
        # keep the existing URL (so that re-scrapes from the original source still match)
        keep_url = old_url or meta.get("tapology_url")
        keep_poster = meta.get("poster_url") or old_poster
        cur.execute("""
            UPDATE events SET name=%s, slug=%s, promotion=%s, date=%s,
                venue=%s, city=%s, country=%s, status=%s, awards=%s,
                poster_url=%s, tapology_url=%s, updated_at=NOW()
            WHERE id=%s
        """, (meta.get("name"), meta.get("slug"), meta.get("promotion"), meta.get("date"),
              meta.get("venue"), meta.get("city"), meta.get("country"),
              new_status, json.dumps(awards),
              keep_poster, keep_url, eid))
        return eid, "UPDATE"
    cur.execute("""
        INSERT INTO events (name,slug,promotion,date,venue,city,country,tapology_url,status,awards,poster_url)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (meta.get("name"), meta.get("slug"), meta.get("promotion"), meta.get("date"),
          meta.get("venue"), meta.get("city"), meta.get("country"),
          meta.get("tapology_url"), meta.get("status"), json.dumps(awards),
          meta.get("poster_url")))
    return cur.fetchone()[0], "INSERT"


def _ensure_event_fights_date_column(conn):
    """Add the event_date column to event_fights if absent (idempotent)."""
    cur = conn.cursor()
    cur.execute("""
        ALTER TABLE event_fights ADD COLUMN IF NOT EXISTS event_date DATE
    """)
    conn.commit()


def upsert_event_fights(conn, event_id: int, bouts: list[dict], idx: dict[str, int],
                        event_date: str | None = None) -> tuple[int, int]:
    cur = conn.cursor()
    cur.execute("DELETE FROM event_fights WHERE event_id=%s", (event_id,))
    matched = 0
    for i, b in enumerate(bouts):
        # Use the pre-resolved IDs if already present in the bout (set by scrape_one)
        f1_id    = b.get("fighter1_id") or resolve_fighter_id(b["fighter1_name"], idx)
        f2_id    = b.get("fighter2_id") or resolve_fighter_id(b["fighter2_name"], idx)
        winner_id = b.get("winner_id") or (resolve_fighter_id(b.get("winner_name"), idx) if b.get("winner_name") else None)
        # Store the resolved IDs for reuse (update_fighters_from_event)
        b["fighter1_id"] = f1_id
        b["fighter2_id"] = f2_id
        b["winner_id"]   = winner_id
        if f1_id: matched += 1
        if f2_id: matched += 1
        cur.execute("""
            INSERT INTO event_fights (
                event_id, card_section, bout_order,
                fighter1_id, fighter2_id, fighter1_name, fighter2_name,
                weight_class, weight_lbs, rounds, is_title_fight,
                winner_id, winner_name,
                method, method_detail, round_num, time_str, status, event_date
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (event_id, b["card_section"], i,
              f1_id, f2_id, b["fighter1_name"], b["fighter2_name"],
              b.get("weight_class") or None, b.get("weight_lbs"),
              b.get("rounds") or None, bool(b.get("is_title_fight")),
              winner_id, b.get("winner_name") or None,
              b.get("method") or None, b.get("method_detail") or None,
              b.get("round_num"), b.get("time_str") or None,
              b.get("status", "upcoming"), event_date))
    return matched, len(bouts)


def update_fighters_from_event(conn, bouts: list[dict], meta: dict, commit: bool) -> tuple[int, int]:
    """
    Update the fighters who fought in the event:
      - record_total_wins/losses/draws, record_ufc_wins/losses
      - wins_by_ko_tko, wins_by_submission, wins_by_decision (and losses_*)
      - win_percentage_int, finish_rate_int
      - last_fight_date, is_active, current_streak, last_5_results
      - fight_history (new entry added at the end of the list)
      - current_league

    Dedup: if fight_history already contains a fight with the same
    (event_date, normalized opponent), we do not increment again.

    Return (n_updated, n_skipped).
    """
    import json as _json

    event_name  = meta.get("name") or ""
    event_date  = meta.get("date") or ""     # ISO YYYY-MM-DD
    promotion   = (meta.get("promotion") or "").upper()
    status      = meta.get("status") or "completed"

    # Only if the event is completed and we have a date
    if status != "completed" or not event_date:
        LOG.warning("update_fighters: event not completed or without a date, skip.")
        return 0, 0

    cur = conn.cursor()
    n_updated = 0
    n_skipped = 0

    def _norm_simple(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").lower().strip())

    def _norm_opp(s: str) -> str:
        """Normalize an opponent name for cross-source dedup.
        Tapology stores 'Name #4 Heavyweight'; ESPN gives 'Name'.
        We remove the ' #N...' suffix to compare the two."""
        cleaned = re.sub(r"\s*#\d+.*$", "", (s or "")).strip()
        return _norm_simple(cleaned)

    def _date_close(d1, d2, days: int = 4) -> bool:
        """True if d1 and d2 are less than `days` days apart (tolerates date gaps
        between sources: UFC-FR vs FightMatrix can differ by 1-2 days)."""
        s1, s2 = str(d1 or "")[:10], str(d2 or "")[:10]
        if not s1 or not s2:
            return False
        if s1 == s2:
            return True
        try:
            a = date.fromisoformat(s1)
            b = date.fromisoformat(s2)
            return abs((a - b).days) <= days
        except ValueError:
            return False

    def _method_category(method: str | None) -> str:
        m = (method or "").upper()
        if m in ("KO", "TKO", "KO/TKO"): return "ko_tko"
        if m in ("SUB", "SUBMISSION"):    return "sub"
        if m in ("DEC", "DECISION"):      return "dec"
        return "other"

    for bout in bouts:
        f1_id = bout.get("fighter1_id")
        f2_id = bout.get("fighter2_id")
        if not f1_id and not f2_id:
            n_skipped += 1
            continue

        winner_id = bout.get("winner_id")
        method    = bout.get("method") or ""
        mcat      = _method_category(method)
        m_upper   = method.upper()
        is_draw   = m_upper in ("DRAW", "D")
        is_nc     = m_upper in ("NC", "NO CONTEST")

        # SAFETY: if we cannot determine the result (no winner_id AND
        # no explicit draw), we DO NOT TOUCH the records.
        # Typical case: event scraped as 'upcoming' before the fight = no winner.
        # -> For these bouts, re-scrape the event with the UFC-FR/Tapology source
        # to get the results, then run again.
        if winner_id is None and not is_draw:
            n_skipped += 1
            continue

        for fighter_id in [x for x in [f1_id, f2_id] if x]:
            if fighter_id == f1_id:
                opponent_name = bout["fighter2_name"]
                opponent_id   = f2_id
            else:
                opponent_name = bout["fighter1_name"]
                opponent_id   = f1_id

            # Result for this fighter
            if is_draw:
                result = "D"
            elif winner_id == fighter_id:
                result = "W"
            else:
                result = "L"

            # Database read
            cur.execute("""
                SELECT record_total_wins, record_total_losses, record_total_draws,
                       record_total_nc, record_ufc_wins, record_ufc_losses,
                       record_other_wins, record_other_losses,
                       wins_by_ko_tko, wins_by_submission, wins_by_decision,
                       losses_by_ko_tko, losses_by_submission, losses_by_decision,
                       last_fight_date, current_streak, last_5_results, fight_history
                FROM fighters WHERE id = %s
            """, (fighter_id,))
            row = cur.fetchone()
            if not row:
                n_skipped += 1
                continue

            (wins, losses, draws, nc, ufc_w, ufc_l, other_w, other_l,
             w_ko, w_sub, w_dec, l_ko, l_sub, l_dec,
             last_date, streak_raw, l5, fh_raw) = row

            # Parse fight_history
            try:
                fh = _json.loads(fh_raw) if isinstance(fh_raw, str) else (fh_raw or {})
            except Exception:
                fh = {}
            if isinstance(fh, dict):
                fights_list = list(fh.get("fights") or [])
            elif isinstance(fh, list):
                fights_list = list(fh)
            else:
                fights_list = []

            # -- 1. Cleaning of the existing duplicates in fight_history ----------
            # Protects against previous runs that added duplicates
            # (dedup bug: old Tapology/FM entries without an event_date field).
            # We group by (normalized_opponent, event_date) and keep
            # the most complete entry if a duplicate is detected.
            # Tapology stores the opponent WITH a rank: "Sergei Pavlovich #4 HW"
            # ESPN gives the raw name: "Sergei Pavlovich"
            # -> _norm_opp() removes the " #N..." suffix to match the two.
            _seen_keys: dict[tuple, int] = {}
            _clean_fl: list[dict] = []
            for _ff in fights_list:
                _fk = (
                    _norm_opp(_ff.get("opponent", "")),
                    (_ff.get("event_date") or "")[:10],
                )
                if _fk in _seen_keys:
                    _prev = _clean_fl[_seen_keys[_fk]]
                    # Keep the most complete entry (event_date > method)
                    if (not _prev.get("event_date") and _ff.get("event_date")) or \
                       (not _prev.get("method")     and _ff.get("method")):
                        _clean_fl[_seen_keys[_fk]] = _ff
                else:
                    _seen_keys[_fk] = len(_clean_fl)
                    _clean_fl.append(_ff)
            # Second pass: remove the entries WITHOUT a date when a DATED
            # version exists for the same opponent. E.g.: the legacy Tapology entry
            # ("Pavlovich #4 HW", no date) is a duplicate of the ESPN entry
            # ("Pavlovich", "2026-05-30"). Since their keys are different
            # (different dates), the 1st pass does not merge them.
            _dated_norms = {_norm_opp(_ff.get("opponent", ""))
                            for _ff in _clean_fl if (_ff.get("event_date") or "")}
            fights_list = [_ff for _ff in _clean_fl
                           if (_ff.get("event_date") or "")
                           or _norm_opp(_ff.get("opponent", "")) not in _dated_norms]

            # -- 2. Dedup: is this fight already in fight_history? ------------
            # FIX BUG 1: old Tapology/FM entries without event_date
            # -> _date_close("", date) retournait False -> doublon ajoute.
            # FIX BUG 2: Tapology names with the rank "#N WeightClass" did not
            # match the clean ESPN names -> duplicate despite the same fight.
            opp_norm = _norm_opp(opponent_name)

            def _already_present(fights, opp_n, opp_id, ev_date):
                for _ff in fights:
                    _name_ok = (
                        (opp_id and _ff.get("opponent_id") == opp_id)
                        or (opp_n and _norm_opp(_ff.get("opponent", "")) == opp_n)
                    )
                    if not _name_ok:
                        continue
                    _ed = (_ff.get("event_date") or "")[:10]
                    if not _ed:
                        return True   # legacy entry without a date -> match on name alone
                    if _date_close(_ed, ev_date):
                        return True
                return False

            if _already_present(fights_list, opp_norm, opponent_id, event_date):
                LOG.debug(f"  skip fighter_id={fighter_id} : fight already in fight_history")
                n_skipped += 1
                continue

            # -- 3. Update of the wins/losses/draws counters -------------------
            wins  = (wins  or 0)
            losses = (losses or 0)
            draws  = (draws  or 0)
            nc     = (nc    or 0)
            ufc_w  = (ufc_w or 0)
            ufc_l  = (ufc_l or 0)
            other_w = (other_w or 0)
            other_l = (other_l or 0)

            # the record is split in two: UFC and everything else. A PFL or
            # Bellator fight must therefore go into "everything else", otherwise total != ufc + other
            if result == "W":
                wins += 1
                if promotion == "UFC": ufc_w += 1
                else:                  other_w += 1
            elif result == "L":
                losses += 1
                if promotion == "UFC": ufc_l += 1
                else:                  other_l += 1
            elif result == "D":
                draws += 1

            total   = wins + losses + draws + nc
            win_pct = round(wins * 100 / total) if total > 0 else 0

            # last_fight_date: keep the most recent one
            new_last = event_date
            if last_date:
                _ex = str(last_date)[:10]
                if _ex > event_date:
                    new_last = _ex

            # last_5_results : oldest->newest, append
            l5_list = [r.strip().upper() for r in (l5 or "").split(",")
                       if r.strip().upper() in ("W", "L", "D")] if l5 else []
            l5_list.append(result)
            new_l5 = ",".join(l5_list[-5:])

            # current_streak
            s_n, s_kind = 0, result
            if streak_raw:
                m_s = re.match(r"(\d+)([WLD])", str(streak_raw).strip().upper())
                if m_s:
                    s_n, s_kind = int(m_s.group(1)), m_s.group(2)
            new_streak = f"{s_n + 1}{result}" if s_kind == result else f"1{result}"

            # -- 4. Adding the new fight to fight_history --------------------
            # fight_history is ordered from MOST RECENT to oldest, and all the
            # rest of the code relies on it (streaks, trends...). A
            # simple append put it at the end, so it was read as the oldest
            # fight of the career. We insert it right after the fights more
            # recent than it, generally at the very top.
            nouveau = {
                "result":        result,
                "opponent":      opponent_name,
                "opponent_id":   opponent_id,
                "event":         event_name,
                "event_date":    event_date,
                "date":          "",   # champ legacy
                "method":        method,
                "method_detail": bout.get("method_detail") or "",
                "round":         bout.get("round_num"),
            }
            pos = 0
            for k, _ff in enumerate(fights_list):
                _ed = (_ff.get("event_date") or "")[:10]
                if _ed and _ed > event_date:
                    pos = k + 1
            fights_list.insert(pos, nouveau)

            # -- 5. Recount of the methods from the COMPLETE fight_history ----------------
            # FIX: we recount from the whole fight_history rather than
            # incrementing. Fixes existing wrong values (e.g. 0% KO
            # despite wins by KO) and handles the legacy Tapology/FM format
            # where the "date" field contains the method ("TKO (Punches) Round 1")
            # and the "method" field is empty.
            def _cat_entry(f):
                m = (f.get("method") or "").strip()
                if not m:
                    m = (f.get("date") or "").strip()   # format legacy
                return _method_category(m)

            w_ko = w_sub = w_dec = 0
            l_ko = l_sub = l_dec = 0
            for _ff in fights_list:
                _r   = (_ff.get("result") or "").upper()
                _cat = _cat_entry(_ff)
                if _r == "W":
                    if _cat == "ko_tko":   w_ko  += 1
                    elif _cat == "sub":    w_sub += 1
                    elif _cat == "dec":    w_dec += 1
                elif _r == "L":
                    if _cat == "ko_tko":   l_ko  += 1
                    elif _cat == "sub":    l_sub += 1
                    elif _cat == "dec":    l_dec += 1

            fin_rate = round((w_ko + w_sub) * 100 / wins) if wins > 0 else 0

            new_fh = _json.dumps({
                "source": "event_scrape",
                "count":  len(fights_list),
                "fights": fights_list,
            }, ensure_ascii=False)

            # current_league
            new_league = promotion if promotion in (
                "UFC", "PFL", "ONE", "BELLATOR", "KSW", "RIZIN", "LFA", "INVICTA", "BRAVE CF"
            ) else None

            if commit:
                cur.execute("""
                    UPDATE fighters SET
                        record_total_wins   = %s,
                        record_total_losses = %s,
                        record_total_draws  = %s,
                        total_fights        = %s,
                        record_ufc_wins     = %s,
                        record_ufc_losses   = %s,
                        record_other_wins   = %s,
                        record_other_losses = %s,
                        wins_by_ko_tko      = %s,
                        wins_by_submission  = %s,
                        wins_by_decision    = %s,
                        losses_by_ko_tko    = %s,
                        losses_by_submission= %s,
                        losses_by_decision  = %s,
                        win_percentage_int  = %s,
                        finish_rate_int     = %s,
                        last_fight_date     = %s,
                        is_active           = TRUE,
                        current_streak      = %s,
                        last_5_results      = %s,
                        fight_history       = %s,
                        current_league      = COALESCE(%s, current_league),
                        last_scraped_at     = NOW(),
                        updated_at          = NOW()
                    WHERE id = %s
                """, (wins, losses, draws, total,
                      ufc_w, ufc_l, other_w, other_l,
                      w_ko, w_sub, w_dec,
                      l_ko, l_sub, l_dec,
                      win_pct, fin_rate,
                      new_last, new_streak, new_l5, new_fh,
                      new_league, fighter_id))

            tag = "" if commit else "[DRY-RUN] "
            LOG.info(f"  {tag}id={fighter_id} vs {opponent_name[:20]:<20} [{result}] "
                     f"-> {wins}W-{losses}L | streak={new_streak} | l5={new_l5}")
            n_updated += 1

    return n_updated, n_skipped


def resolve_null_fighter_ids(conn, commit: bool) -> dict[str, int]:
    """
    Bulk UPDATE: fills the NULL fighter1_id / fighter2_id / winner_id
    in event_fights when the fighter exists in the fighters table.

    Uses a direct SQL join (case-insensitive) -- no Python norm_name,
    but covers the vast majority of cases (names are already ASCII on both sides).

    Return {f1_updated, f2_updated, winner_updated}.
    """
    cur = conn.cursor()

    if commit:
        cur.execute("""
            UPDATE event_fights ef
            SET fighter1_id = f.id
            FROM fighters f
            WHERE ef.fighter1_id IS NULL
              AND LOWER(f.name) = LOWER(ef.fighter1_name)
        """)
        f1 = cur.rowcount

        cur.execute("""
            UPDATE event_fights ef
            SET fighter2_id = f.id
            FROM fighters f
            WHERE ef.fighter2_id IS NULL
              AND LOWER(f.name) = LOWER(ef.fighter2_name)
        """)
        f2 = cur.rowcount

        cur.execute("""
            UPDATE event_fights ef
            SET winner_id = f.id
            FROM fighters f
            WHERE ef.winner_id IS NULL
              AND ef.winner_name IS NOT NULL
              AND LOWER(f.name) = LOWER(ef.winner_name)
        """)
        wi = cur.rowcount
        conn.commit()
    else:
        # Dry-run: only counts
        cur.execute("""SELECT COUNT(*) FROM event_fights ef JOIN fighters f ON LOWER(f.name)=LOWER(ef.fighter1_name) WHERE ef.fighter1_id IS NULL""")
        f1 = cur.fetchone()[0]
        cur.execute("""SELECT COUNT(*) FROM event_fights ef JOIN fighters f ON LOWER(f.name)=LOWER(ef.fighter2_name) WHERE ef.fighter2_id IS NULL""")
        f2 = cur.fetchone()[0]
        cur.execute("""SELECT COUNT(*) FROM event_fights ef JOIN fighters f ON LOWER(f.name)=LOWER(ef.winner_name) WHERE ef.winner_id IS NULL AND ef.winner_name IS NOT NULL""")
        wi = cur.fetchone()[0]

    return {"fighter1_id": f1, "fighter2_id": f2, "winner_id": wi}


def run_recompute_methods(conn, commit: bool, verbose: bool = False) -> int:
    """
    Recompute wins_by_ko_tko / wins_by_submission / wins_by_decision
    (and losses + finish_rate_int) for ALL the fighters that have a fight_history.

    Only uses the fight_history JSON stored in the database: does NOT touch
    record_total_wins/losses (incremental values preserved).

    Handles the legacy Tapology/FM format ("date" field = method string,
    empty "method" field) AND the event_scrape format (direct "method" field).

    Run through:
        py scraper/scrape_events.py --recompute-methods [--commit]
    """
    import json as _json

    def _method_cat(f):
        m = (f.get("method") or "").strip()
        if not m:
            m = (f.get("date") or "").strip()
        mu = m.upper()
        if any(k in mu for k in ("KO", "TKO", "KNOCKOUT")): return "ko_tko"
        if "SUB" in mu:     return "sub"
        if "DEC" in mu:     return "dec"
        return "other"

    cur = conn.cursor()
    cur.execute("""
        SELECT id, fight_history, record_total_wins
        FROM fighters
        WHERE fight_history IS NOT NULL AND fight_history != 'null'
    """)
    rows = cur.fetchall()
    print(f"  {len(rows)} fighters with fight_history to reprocess...")

    n_updated = 0
    for fid, fh_raw, total_wins in rows:
        try:
            fh = _json.loads(fh_raw) if isinstance(fh_raw, str) else (fh_raw or {})
        except Exception:
            continue
        if isinstance(fh, dict):
            fights = fh.get("fights") or []
        elif isinstance(fh, list):
            fights = fh
        else:
            continue
        if not fights:
            continue

        # Internal dedup: remove the duplicates by (opponent_norm, event_date)
        # _norm_opp removes the Tapology rank "Name #4 Heavyweight" -> "Name"
        def _norm_opp_local(s):
            cleaned = re.sub(r"\s*#\d+.*$", "", (s or "")).strip()
            return re.sub(r"\s+", " ", cleaned.lower().strip())

        seen: dict[tuple, int] = {}
        clean: list[dict] = []
        for f in fights:
            k = (_norm_opp_local(f.get("opponent") or ""),
                 (f.get("event_date") or "")[:10])
            if k not in seen:
                seen[k] = len(clean)
                clean.append(f)
            else:
                prev = clean[seen[k]]
                if (not prev.get("event_date") and f.get("event_date")) or \
                   (not prev.get("method")     and f.get("method")):
                    clean[seen[k]] = f
        # Second pass: remove the entries without a date that have a dated
        # version for the same opponent (Tapology "#N" names vs clean ESPN names)
        _dated_n = {_norm_opp_local(f.get("opponent") or "") for f in clean if (f.get("event_date") or "")}
        clean = [f for f in clean
                 if (f.get("event_date") or "")
                 or _norm_opp_local(f.get("opponent") or "") not in _dated_n]

        w_ko = w_sub = w_dec = 0
        l_ko = l_sub = l_dec = 0
        for f in clean:
            r   = (f.get("result") or "").upper()
            cat = _method_cat(f)
            if r == "W":
                if cat == "ko_tko":  w_ko  += 1
                elif cat == "sub":   w_sub += 1
                elif cat == "dec":   w_dec += 1
            elif r == "L":
                if cat == "ko_tko":  l_ko  += 1
                elif cat == "sub":   l_sub += 1
                elif cat == "dec":   l_dec += 1

        wins_for_rate = total_wins or 0
        fin_rate = round((w_ko + w_sub) * 100 / wins_for_rate) if wins_for_rate > 0 else 0

        # Detect whether fight_history has changed (duplicates removed)
        fh_changed = len(clean) != len(fights)
        new_fh = _json.dumps({"source": fh.get("source", "event_scrape") if isinstance(fh, dict) else "event_scrape",
                               "count": len(clean), "fights": clean}, ensure_ascii=False) if fh_changed else None

        if verbose:
            print(f"  id={fid:6d} : KO={w_ko} SUB={w_sub} DEC={w_dec} | "
                  f"lKO={l_ko} lSUB={l_sub} lDEC={l_dec} | fin={fin_rate}% "
                  f"{'[DEDUP -' + str(len(fights)-len(clean)) + ']' if fh_changed else ''}")

        if commit:
            if new_fh:
                cur.execute("""
                    UPDATE fighters SET
                        wins_by_ko_tko       = %s,
                        wins_by_submission   = %s,
                        wins_by_decision     = %s,
                        losses_by_ko_tko     = %s,
                        losses_by_submission = %s,
                        losses_by_decision   = %s,
                        finish_rate_int      = %s,
                        fight_history        = %s,
                        updated_at           = NOW()
                    WHERE id = %s
                """, (w_ko, w_sub, w_dec, l_ko, l_sub, l_dec, fin_rate, new_fh, fid))
            else:
                cur.execute("""
                    UPDATE fighters SET
                        wins_by_ko_tko       = %s,
                        wins_by_submission   = %s,
                        wins_by_decision     = %s,
                        losses_by_ko_tko     = %s,
                        losses_by_submission = %s,
                        losses_by_decision   = %s,
                        finish_rate_int      = %s,
                        updated_at           = NOW()
                    WHERE id = %s
                """, (w_ko, w_sub, w_dec, l_ko, l_sub, l_dec, fin_rate, fid))
        n_updated += 1

    if commit:
        conn.commit()
    return n_updated


def backfill_event_fighters(conn, event_id: int, idx: dict[str, int], commit: bool) -> tuple[int, int]:
    """
    Update the fighters of an event ALREADY in the database, by reading the event_fights
    table directly (NO re-scrape, free). Re-resolves the missing names->IDs
    with the current index (more complete than at the time of the initial scrape).

    ONLY acts on bouts whose result is known (winner_id present or explicit
    draw). Bouts scraped as 'upcoming' (without a result) are ignored:
    the event has to be re-scraped to get their results.

    Return (n_updated, n_skipped).
    """
    cur = conn.cursor()
    cur.execute("SELECT name, date, promotion, status FROM events WHERE id = %s", (event_id,))
    ev = cur.fetchone()
    if not ev:
        return 0, 0
    name, ev_date, promo, _status = ev
    if not ev_date:
        return 0, 0
    ev_date_iso = str(ev_date)[:10]
    if ev_date_iso > date.today().isoformat():
        return 0, 0   # future event -> no results to backfill

    meta = {"name": name or "", "date": ev_date_iso,
            "promotion": promo or "", "status": "completed"}

    cur.execute("""
        SELECT fighter1_name, fighter2_name, fighter1_id, fighter2_id,
               winner_id, winner_name, method, method_detail
        FROM event_fights WHERE event_id = %s ORDER BY bout_order
    """, (event_id,))
    bouts: list[dict] = []
    for f1n, f2n, f1i, f2i, wi, wn, method, mdetail in cur.fetchall():
        f1i = f1i or resolve_fighter_id(f1n, idx)
        f2i = f2i or resolve_fighter_id(f2n, idx)
        wi  = wi  or (resolve_fighter_id(wn, idx) if wn else None)
        bouts.append({
            "fighter1_name": f1n, "fighter2_name": f2n,
            "fighter1_id":   f1i, "fighter2_id":   f2i,
            "winner_id":     wi,  "winner_name":   wn,
            "method":        method, "method_detail": mdetail,
        })

    return update_fighters_from_event(conn, bouts, meta, commit)


# --- UFC-FR event parser ----------------------------------------------------

_FR_WC_MAP: dict[str, str] = {
    "poids paille":     "Strawweight",
    "poids mouches":    "Flyweight",
    "poids mouche":     "Flyweight",
    "poids coqs":       "Bantamweight",
    "poids coq":        "Bantamweight",
    "poids plumes":     "Featherweight",
    "poids plume":      "Featherweight",
    "poids legers":     "Lightweight",
    "poids leger":      "Lightweight",
    "poids mi-moyens":  "Welterweight",
    "poids mi-moyen":   "Welterweight",
    "poids moyens":     "Middleweight",
    "poids moyen":      "Middleweight",
    "poids mi-lourds":  "Light Heavyweight",
    "mi-lourds":        "Light Heavyweight",
    "demi-lourds":      "Light Heavyweight",
    "poids lourds":     "Heavyweight",
    "poids lourd":      "Heavyweight",
    "super-lourds":     "Super Heavyweight",
}

_FR_MONTHS_NUM = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}


def _strip_accents_simple(s: str) -> str:
    """Replace the common accented characters (without unicodedata)."""
    for src, dst in (("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
                     ("à", "a"), ("â", "a"), ("ô", "o"), ("û", "u"),
                     ("î", "i"), ("ï", "i"), ("ç", "c"), ("É", "E"),
                     ("È", "E"), ("Ê", "E"), ("À", "A"), ("Â", "A")):
        s = s.replace(src, dst)
    return s


def _parse_fr_event_date(text: str) -> str | None:
    """'Samedi 6 juin 2026' or 'Date Samedi 6 juin 2026 Lieu...' -> '2026-06-06'"""
    text_clean = _strip_accents_simple(text.lower())
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text_clean)
    if not m:
        return None
    mo = _FR_MONTHS_NUM.get(m.group(2))
    if not mo:
        return None
    try:
        return date(int(m.group(3)), mo, int(m.group(1))).isoformat()
    except ValueError:
        return None


def _ufcfr_weight_class(cat_label: str) -> tuple[str, int | None]:
    """'Combat Categorie - Poids Mi-Moyens' -> ('Welterweight', None)"""
    label = re.sub(r"^Combat\s+Cat[eé]gorie\s*[-–]\s*", "", cat_label, flags=re.I).strip()
    m_lbs = re.search(r"(\d+)\s*lbs", label, re.I)
    if m_lbs:
        lbs = int(m_lbs.group(1))
        return _wc_from_lbs(lbs) or f"Catchweight {lbs}", lbs
    key = _strip_accents_simple(label.lower())
    if key in _FR_WC_MAP:
        return _FR_WC_MAP[key], None
    for fr_key, en_val in _FR_WC_MAP.items():
        if fr_key in key:
            return en_val, None
    return label, None


def _ufcfr_card_section(card_type: str) -> str:
    ct = card_type.lower()
    if "early prelim" in ct:
        return "Early Prelims"
    if "prelim" in ct:
        return "Prelims"
    return "Main Card"


def fetch_html_ufc_fr(url: str) -> str | None:
    """Simple fetch of a UFC-FR page (no Cloudflare protection)."""
    try:
        req = _urllib_req.Request(
            url,
            headers={"User-Agent": UA_POOL[0],
                     "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        LOG.error(f"UFC-FR fetch error: {e}")
        return None


def parse_ufc_fr_event(html: str, url: str) -> tuple[dict, list[dict]]:
    """Parse the UFC-FR event page. Return (meta, bouts)."""
    soup = BeautifulSoup(html, "lxml")

    # --- Metadata ---
    meta: dict = {"tapology_url": url, "slug": slug_from_url(url), "promotion": "UFC"}

    h1 = soup.find("h1")
    meta["name"] = h1.get_text(strip=True) if h1 else ""

    meta_el = soup.find(class_="event-meta-list") or soup.find(class_="event-details-box")
    meta_text = meta_el.get_text(" ", strip=True) if meta_el else ""
    meta["date"] = _parse_fr_event_date(meta_text)

    # Location from "Date ... Lieu X, Y, Z Carte ..."
    m_lieu = re.search(r"Lieu\s+(.+?)(?:\s+Carte|\s+Diffusion|$)", meta_text, re.I)
    meta["venue"] = meta["city"] = meta["country"] = None
    if m_lieu:
        lieu_parts = [p.strip() for p in m_lieu.group(1).strip().split(",")]
        meta["venue"]   = lieu_parts[0] if lieu_parts else None
        meta["city"]    = lieu_parts[1].strip() if len(lieu_parts) > 1 else None
        meta["country"] = lieu_parts[-1].strip() if len(lieu_parts) > 1 else None

    if meta.get("date"):
        try:
            d = date.fromisoformat(meta["date"])
            meta["status"] = "upcoming" if d >= date.today() else "completed"
        except ValueError:
            meta["status"] = "completed"
    else:
        meta["status"] = "completed"

    og_img = soup.find("meta", attrs={"property": "og:image"})
    meta["poster_url"] = og_img.get("content") if og_img else None

    # --- Bouts ---
    bouts: list[dict] = []
    status = meta["status"]

    for container in soup.find_all(class_="fight-card-container"):
        header = container.find(class_="fight-header")
        if not header:
            continue
        cat_el      = header.find(class_="cat-label")
        card_type_el = header.find(class_="card-type")
        cat_text    = cat_el.get_text(strip=True) if cat_el else ""
        card_type   = card_type_el.get_text(strip=True) if card_type_el else ""

        wc_en, wc_lbs = _ufcfr_weight_class(cat_text)
        card_section  = _ufcfr_card_section(card_type)
        bout_label    = re.sub(r"\s*\(\d+\)\s*$", "", card_type).strip()

        body = container.find(class_="fight-body")
        if not body:
            continue

        sides = body.find_all(class_=re.compile(r"\bfighter-side\b"))
        if len(sides) < 2:
            continue

        def _fighter_info(side) -> tuple[str, str, str | None]:
            name_a  = side.find("a", class_="fighter-name")
            name    = name_a.get_text(strip=True) if name_a else ""
            pill    = side.find(class_=re.compile(r"\btrend-pill\b"))
            result  = pill.get_text(strip=True).upper() if pill else ""
            rank_el = side.find(class_="rank-tag")
            rank    = re.sub(r"[#\s]", "", rank_el.get_text(strip=True)) if rank_el else None
            return name, result, rank

        f1_name, f1_result, f1_rank = _fighter_info(sides[0])
        f2_name, f2_result, f2_rank = _fighter_info(sides[1])
        if not f1_name or not f2_name:
            continue

        winner_name = f1_name if f1_result == "WIN" else (f2_name if f2_result == "WIN" else None)

        # Result: "DEC - Unanimous (...) - Round : 5 - 5:00"
        method = method_detail = time_str = None
        round_num = None
        res_el = container.find(class_="fight-result")
        if res_el:
            res_text = res_el.get_text(strip=True)
            parts = [p.strip() for p in res_text.split(" - ")]
            if parts:
                method = parts[0].upper()
                for p in parts:
                    m_r = re.match(r"Round\s*:\s*(\d+)", p, re.I)
                    if m_r:
                        round_num = int(m_r.group(1))
                for p in reversed(parts):
                    if re.match(r"\d{1,2}:\d{2}$", p.strip()):
                        time_str = p.strip()
                        break
                detail_parts = []
                for p in parts[1:]:
                    if re.match(r"Round\s*:", p, re.I):
                        break
                    if re.match(r"\d{1,2}:\d{2}$", p.strip()):
                        break
                    detail_parts.append(p)
                method_detail = " - ".join(detail_parts) if detail_parts else None

        bouts.append({
            "card_section":      card_section,
            "bout_label":        bout_label,
            "bout_order":        len(bouts),
            "fighter1_name":     f1_name,
            "fighter2_name":     f2_name,
            "fighter1_record":   None,
            "fighter2_record":   None,
            "fighter1_ufc_rank": f1_rank,
            "fighter2_ufc_rank": f2_rank,
            "weight_class":      wc_en,
            "weight_lbs":        wc_lbs,
            "rounds":            "5x5" if card_section == "Main Card" else "3x5",
            "is_title_fight":    False,
            "title_text":        "",
            "status":            status,
            "winner_name":       winner_name,
            "method":            _norm_method(method),
            "method_detail":     method_detail,
            "round_num":         round_num,
            "time_str":          time_str,
        })

    return meta, bouts


# --- scrape 1 URL -------------------------------------------------------------

def _detect_source(url: str) -> str:
    if "tapology.com" in url:
        return "tapology"
    if "ufc-fr.com" in url and "evenement" in url:
        return "ufc_fr"
    return "unknown"


def scrape_one(url: str, conn, idx: dict[str, int], commit: bool, verbose: bool,
               debug: bool = False, page=None) -> str:
    url = url.strip().rstrip("/")
    source = _detect_source(url)

    if source == "unknown":
        LOG.error(f"URL not recognized (tapology or ufc-fr.com/evenement-): {url}")
        return "FAIL"

    # -- UFC-FR: simple HTML fetch (no Playwright, no Cloudflare) --
    if source == "ufc_fr":
        print("  UFC-FR fetch in progress...")
        html = fetch_html_ufc_fr(url)
        if not html:
            return "FAIL"
        if debug:
            slug = url.rstrip("/").split("/")[-1]
            dbg_path = ROOT / f"debug_{slug}.txt"
            dbg_path.write_text(html, encoding="utf-8")
            print(f"  [DEBUG] raw HTML -> {dbg_path.name} ({len(html)} chars)")
        meta, bouts = parse_ufc_fr_event(html, url)
        strategy = "ufc_fr"

    # -- Tapology: Playwright or Selenium --
    else:
        if "/events/" not in url:
            LOG.error(f"URL is not a Tapology event: {url}"); return "FAIL"
        page_title = ""
        use_selenium = (
            os.environ.get("SCRAPER_SELENIUM") == "1"
            and os.environ.get("SCRAPER_SELENIUM") != "0"
        )
        if use_selenium:
            print(f"  Selenium (Chrome) in progress...")
            full_text, poster_url = fetch_text_selenium_event(url)
            if not full_text:
                return "FAIL"
        elif page is not None:
            print(f"  Playwright in progress...")
            full_text, poster_url, blocked = _navigate_and_extract(page, url)
            if not full_text:
                return "BLOCKED" if blocked else "FAIL"
            try:
                page_title = page.title()
            except Exception:
                pass
        else:
            print(f"  Playwright in progress...")
            full_text, poster_url = fetch_html_playwright(url, proxy=None)
            if not full_text:
                return "FAIL"

        if debug:
            slug = url.rstrip("/").split("/")[-1]
            dbg_path = ROOT / f"debug_{slug}.txt"
            dbg_path.write_text(full_text, encoding="utf-8")
            print(f"  [DEBUG] raw text -> {dbg_path.name} ({len(full_text)} chars)")

        meta    = parse_event_meta(full_text, url, page_title=page_title)
        meta["poster_url"] = poster_url
        bouts, strategy = parse_fight_card(full_text, meta.get("status", "completed"))

    venue   = meta.get("venue") or ""
    city    = meta.get("city") or ""
    country = meta.get("country") or ""
    lieu    = " -- ".join(filter(None, [venue, f"{city}, {country}".strip(", ")])) or "?"

    print(f"\n  SOURCE   : {source}")
    print(f"  EVENT    : {meta.get('name') or '?'}")
    print(f"  DATE     : {meta.get('date') or '?'}")
    print(f"  PROMO    : {meta.get('promotion') or '?'}")
    print(f"  VENUE    : {lieu}")
    print(f"  STATUS   : {meta.get('status')}")
    print(f"  BOUTS    : {len(bouts)}  [{strategy}]")

    if verbose and bouts:
        for b in bouts:
            title  = f" [{b['title_text']}]" if b.get("title_text") else ""
            winner = f"  -> {b['winner_name']}" if b.get("winner_name") else ""
            method = f" ({b['method']})" if b.get("method") else ""
            lbs    = f" {b['weight_lbs']}lbs" if b.get("weight_lbs") else ""
            r1     = b.get("fighter1_record") or "?-?"
            r2     = b.get("fighter2_record") or "?-?"
            print(f"    [{b['card_section']:12s}] {b['bout_label']}{lbs}{title}")
            print(f"      {b['fighter1_name']:28s} ({r1})  vs  {b['fighter2_name']} ({r2}){winner}{method}")

    # Dry-run: shows what would be updated, but writes nothing
    if not commit:
        # Simulates the ID resolution for the report
        for b in bouts:
            b["fighter1_id"] = resolve_fighter_id(b["fighter1_name"], idx)
            b["fighter2_id"] = resolve_fighter_id(b["fighter2_name"], idx)
            b["winner_id"]   = resolve_fighter_id(b.get("winner_name"), idx) if b.get("winner_name") else None
        matched_ids = sum(1 for b in bouts for fid in [b.get("fighter1_id"), b.get("fighter2_id")] if fid)
        print(f"  -> DRY-RUN | {matched_ids}/{len(bouts)*2} fighters recognized in the database")
        if meta.get("status") == "completed":
            n_upd, n_skip = update_fighters_from_event(conn, bouts, meta, commit=False)
            print(f"  -> DRY-RUN | {n_upd} fighters would be updated, {n_skip} skips")
        print("  -> Add --commit to write to the database")
        return "SKIP_DRYRUN"

    if not meta.get("name"):
        LOG.error("  -> No event name extracted, skip")
        return "FAIL"

    event_id, op = upsert_event(conn, meta, [], bouts=bouts)
    matched, total = upsert_event_fights(conn, event_id, bouts, idx,
                                         event_date=meta.get("date"))
    # Update the records/history of the fighters involved
    n_upd, n_skip = update_fighters_from_event(conn, bouts, meta, commit=True)
    conn.commit()
    if n_upd == 0 and n_skip > 0:
        upd_msg = f"0 fighters updated ({n_skip} already in history = OK, up to date)"
    elif n_upd == 0 and matched == 0:
        upd_msg = "0 fighters updated (no fighter recognized in the database)"
    elif n_upd == 0:
        upd_msg = f"0 fighters updated (bouts without a scraped result - re-scrape the event)"
    else:
        upd_msg = f"{n_upd} fighters updated"
    print(f"  -> {op} event_id={event_id} | {matched}/{total*2} fighters matches | {upd_msg}")
    return op


#  input interactif

def read_urls_interactive() -> list[str]:
    print("\n  Paste 1 or more event URLs (1 per line).")
    print("  Accepted sources: Tapology (/events/...) or UFC-FR (evenement-N.html)")
    print("  Empty line to validate, Q to cancel.\n")
    urls: list[str] = []
    while True:
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Annule."); return []
        if not line: break
        if line.lower() in ("q", "quit", "exit"):
            print("  Cancelled."); return []
        for u in re.split(r"\s+", line):
            u = u.strip()
            if u.startswith("http"):
                urls.append(u)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            out.append(u); seen.add(u)
    return out


def read_urls_file(path: Path) -> list[str]:
    """Read a file of URLs (1/line). Ignore empty lines + # comments.
    Also strips inline comments (URL  # label)."""
    urls, seen = [], set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        u = line.split("#", 1)[0].strip().rstrip("/")
        if u.startswith("http") and u not in seen:
            seen.add(u); urls.append(u)
    return urls


def load_existing_event_urls(conn) -> dict[str, dict]:
    """URLs already in the database -> {url: {id, date}}. Allows:
      - resume (skip of the upcoming events already in the database)
      - re-scraping PAST events to get their results.
    """
    cur = conn.cursor()
    cur.execute("SELECT id, tapology_url, date FROM events WHERE tapology_url IS NOT NULL")
    out: dict[str, dict] = {}
    for eid, url, dt in cur.fetchall():
        if url:
            out[url.rstrip("/")] = {"id": eid, "date": str(dt)[:10] if dt else None}
    return out


def log_failure(url: str, reason: str):
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{url}\t{reason}\n")


#  MAIN

# Waiting policy on a Tapology IP ban (the user wants to WAIT, not skip)
BAN_WAIT_BASE   = 120     # 1er wait : 2 min
BAN_WAIT_MAX    = 1800    # plafond : 30 min
BAN_MAX_RETRIES = 12      # ~3-4h of cumulative waiting max on 1 URL before logging it
RECREATE_EVERY  = 3       # recreate the browser context every N consecutive blocks


# Whitelist of MMA promotions (the backfill excludes boxing/kickboxing by default,
# which would pollute the MMA records). Case-insensitive substring match.
_MMA_PROMO_WHITELIST = (
    "UFC", "PFL", "ONE", "BELLATOR", "KSW", "RIZIN", "LFA", "INVICTA",
    "BRAVE", "CAGE WARRIORS", "ACA", "ACB", "M-1", "ROAD FC", "PANCRASE",
    "IMMAF", "EAGLE", "DWCS", "CONTENDER", "FURY FC", "OKTAGON", "ARES",
    "PFL EUROPE", "GAMEBRED", "XMMA", "TITAN FC", "CFFC", "UAE WARRIORS",
)


def _is_mma_promo(promo: str | None) -> bool:
    if not promo:
        return False
    p = promo.upper()
    return any(w in p for w in _MMA_PROMO_WHITELIST)


def run_merge_duplicate_events(commit: bool) -> int:
    """
    Merge the duplicated events (same date + same fighters, different
    sources -> Tapology + UFC-FR). Keep the MOST COMPLETE event (max winners,
    then max bouts), delete the others.

    Fixes the "1089 (upcoming, 0 results) + 8415 (completed, 12 results)" bug.
    """
    print("=" * 62)
    print("  MERGE of the duplicated events (cross-source)")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'}")
    print("=" * 62)

    conn = db.get_connection()
    if not conn:
        print("  [ERROR] Database connection impossible."); return 1
    cur = conn.cursor()

    # Load all the events with date + their fighter pairs
    cur.execute("SELECT id, name, date FROM events WHERE date IS NOT NULL ORDER BY date")
    events = cur.fetchall()
    print(f"  {len(events)} events with a date\n")

    # Index date -> [event_ids]
    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for eid, name, dt in events:
        by_date[str(dt)[:10]].append((eid, name))

    def event_pairs(eid):
        cur.execute("SELECT fighter1_name, fighter2_name FROM event_fights WHERE event_id=%s", (eid,))
        s = set()
        for f1, f2 in cur.fetchall():
            a, b = norm_name(f1 or ""), norm_name(f2 or "")
            if len(a) > 3 and len(b) > 3:
                s.add(frozenset((a, b)))
        return s

    def event_winners(eid):
        cur.execute("SELECT COUNT(*) FROM event_fights WHERE event_id=%s AND winner_id IS NOT NULL", (eid,))
        return cur.fetchone()[0]

    def event_total_bouts(eid):
        cur.execute("SELECT COUNT(*) FROM event_fights WHERE event_id=%s", (eid,))
        return cur.fetchone()[0]

    n_groups = 0
    n_deleted = 0

    for dt, evs in by_date.items():
        if len(evs) < 2:
            continue
        # Build the pairs for each event of this date
        pair_map = {eid: event_pairs(eid) for eid, _ in evs}

        # Simple clustering: union-find by pair overlap
        clusters: list[list[int]] = []
        for eid, _name in evs:
            placed = False
            for cl in clusters:
                # compare with a member of the cluster
                ref = cl[0]
                shared = pair_map[eid] & pair_map[ref]
                if not pair_map[eid] or not pair_map[ref]:
                    continue
                ratio = len(shared) / max(1, min(len(pair_map[eid]), len(pair_map[ref])))
                if len(shared) >= 2 or ratio >= 0.5:
                    cl.append(eid)
                    placed = True
                    break
            if not placed:
                clusters.append([eid])

        for cl in clusters:
            if len(cl) < 2:
                continue
            n_groups += 1
            # Keep the one with the most winners, then the most bouts
            ranked = sorted(cl, key=lambda e: (event_winners(e), event_total_bouts(e)), reverse=True)
            keeper = ranked[0]
            losers = ranked[1:]
            names = {eid: nm for eid, nm in evs}
            print(f"  [{dt}] KEEP id={keeper} ({event_winners(keeper)}W/{event_total_bouts(keeper)}b) "
                  f"<- deleted {losers}")
            for lid in losers:
                if commit:
                    cur.execute("DELETE FROM event_fights WHERE event_id=%s", (lid,))
                    cur.execute("DELETE FROM events WHERE id=%s", (lid,))
                n_deleted += 1

    if commit:
        conn.commit()
    conn.close()

    print("\n" + "=" * 62)
    print(f"  {n_groups} duplicate groups | {n_deleted} events deleted")
    if not commit:
        print("  DRY-RUN: nothing deleted. Add --commit to apply.")
    print("=" * 62)
    return 0


def run_backfill_fighters(commit: bool, verbose: bool,
                          since: str | None = None,
                          promotion: str | None = None) -> int:
    """
    Catch up the fighters' records from the completed events already in the database.
    Reads the event_fights table (no re-scrape, no network request).

    SAFETY (the events table mixes MMA / boxing / kickboxing):
      - by default, ONLY the whitelisted MMA promotions are processed
        (otherwise boxing fights would be added to the MMA records).
      - --promotion NAME : restrict to a specific promotion (ILIKE).
      - --since DATE     : restrict to events >= this date (recent catch-up).
      - a bout is applied ONLY if its result is known (winner_id present).

    Idempotent: the tolerant dedup prevents double counting if the fight is
    already in the fighter's fight_history.
    """
    print("=" * 62)
    print("  BACKFILL fighters from completed events in the database")
    print(f"  Mode: {'COMMIT' if commit else 'DRY-RUN'}")
    if promotion: print(f"  Promotion filter : {promotion}")
    if since:     print(f"  Since filter     : {since}")
    if not promotion:
        print("  Whitelisted MMA promotions only (boxing/kickboxing excluded)")
    print("=" * 62)

    conn = db.get_connection()
    if not conn:
        print("  [ERROR] Database connection impossible."); return 1

    print("\n  Loading the fighters index from the database...")
    idx = load_fighter_index(conn)
    print(f"  {len(idx)} fighters indexes")

    where = ["date IS NOT NULL", "date <= CURRENT_DATE"]
    params: list = []
    if since:
        where.append("date >= %s")
        params.append(since)
    if promotion:
        where.append("promotion ILIKE %s")
        params.append(f"%{promotion}%")
    where_sql = " AND ".join(where)

    cur = conn.cursor()
    cur.execute(f"""
        SELECT id, name, date, promotion FROM events
        WHERE {where_sql}
        ORDER BY date ASC
    """, tuple(params))
    all_events = cur.fetchall()

    # MMA whitelist filter (unless an explicit --promotion is provided by the user)
    if promotion:
        events = [(eid, name, dt) for eid, name, dt, _p in all_events]
    else:
        events = [(eid, name, dt) for eid, name, dt, p in all_events if _is_mma_promo(p)]
        print(f"  {len(all_events)} past events -> {len(events)} kept (MMA filter)")
    print(f"  {len(events)} completed events to process\n")

    # Step 0: resolve the NULL fighter_id / winner_id in event_fights
    print("  Step 1/2: resolution of the NULL fighter_id / winner_id in event_fights...")
    id_stats = resolve_null_fighter_ids(conn, commit)
    print(f"    fighter1_id: {id_stats['fighter1_id']} bouts updated")
    print(f"    fighter2_id: {id_stats['fighter2_id']} bouts updated")
    print(f"    winner_id  : {id_stats['winner_id']} bouts updated")
    if not commit:
        print("    (DRY-RUN: the figures above would be applied with --commit)")
    print()

    # Rebuild the index if we committed (new resolved IDs)
    if commit and (id_stats["fighter1_id"] or id_stats["fighter2_id"]):
        idx = load_fighter_index(conn)

    print(f"  Step 2/2: update of the fighters' records from {len(events)} events...")
    t_start = time.time()
    total_upd = total_skip = events_touched = 0

    for i, (eid, name, dt) in enumerate(events, 1):
        try:
            n_upd, n_skip = backfill_event_fighters(conn, eid, idx, commit)
        except Exception as e:  # noqa: BLE001
            LOG.error(f"  event_id={eid} ({name}) : error {e}")
            try: conn.rollback()
            except Exception: pass
            continue
        if commit and n_upd:
            conn.commit()
        total_upd  += n_upd
        total_skip += n_skip
        if n_upd:
            events_touched += 1
            if verbose:
                print(f"  [{i}/{len(events)}] {str(dt)[:10]} {name[:45]:<45} -> {n_upd} MAJ")
        if i % 250 == 0:
            print(f"  ... {i}/{len(events)} events | {total_upd} fighters updated (cumulative) "
                  f"({time.time()-t_start:.0f}s)")

    conn.close()
    elapsed = time.time() - t_start
    print("\n" + "=" * 62)
    print(f"  BACKFILL DONE ({elapsed:.0f}s)")
    print(f"   events with update: {events_touched}/{len(events)}")
    print(f"   fighters updated: {total_upd}")
    print(f"   bouts skipped     : {total_skip}  (unknown result or already in history)")
    if not commit:
        print("\n  DRY-RUN: nothing written. Add --commit to apply.")
    print("=" * 62)
    return 0


def main():
    p = argparse.ArgumentParser(description="Scraper BATCH d'events (Tapology Playwright + UFC-FR)")
    p.add_argument("--commit",  action="store_true", help="Write to the database (default: dry-run)")
    p.add_argument("--verbose", "-v", action="store_true", help="Show the bout details")
    p.add_argument("--debug",   action="store_true", help="Save the raw HTML/text")
    p.add_argument("--file",    help="URL file (1/line) -> batch mode")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    p.add_argument("--refresh", action="store_true", help="Re-scrape even the events already in the database")
    p.add_argument("--proxy",   help="HTTP/HTTPS proxy for Tapology (e.g. http://host:port)")
    p.add_argument("--backfill-fighters", action="store_true",
                   help="Update the fighters from the completed events already in the database "
                        "(reads event_fights, no re-scrape). MMA whitelist by default.")
    p.add_argument("--fix-ids", action="store_true",
                   help="Resolve the NULL fighter1_id/fighter2_id/winner_id in event_fights "
                        "through a direct SQL join. Fast, no re-scrape.")
    p.add_argument("--merge-events", action="store_true",
                   help="Merge the cross-source duplicated events (same date + same fighters).")
    p.add_argument("--populate-fight-dates", action="store_true",
                   help="Add/populate event_fights.event_date from events.date.")
    p.add_argument("--recompute-methods", action="store_true",
                   help="Recompute wins_by_ko_tko/sub/dec + finish_rate for all the fighters "
                        "from their fight_history. Also fixes the duplicates in fight_history.")
    p.add_argument("--since", help="Backfill: events >= this date (e.g. 2025-01-01)")
    p.add_argument("--promotion", help="Backfill: restrict to a promotion (ILIKE, e.g. UFC)")
    p.add_argument("urls", nargs="*", help="URLs (sinon interactif)")
    args = p.parse_args()

    # -- Mode merge-events standalone --
    if args.merge_events:
        return run_merge_duplicate_events(args.commit)

    # -- Mode populate-fight-dates standalone --
    if args.populate_fight_dates:
        print("=" * 62)
        print("  Column event_fights.event_date")
        print(f"  Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
        print("=" * 62)
        conn3 = db.get_connection()
        if not conn3:
            print("  [ERROR] Database connection impossible."); return 1
        _ensure_event_fights_date_column(conn3)
        cur3 = conn3.cursor()
        if args.commit:
            cur3.execute("""UPDATE event_fights ef SET event_date = e.date
                            FROM events e WHERE ef.event_id = e.id
                            AND (ef.event_date IS DISTINCT FROM e.date)""")
            print(f"  {cur3.rowcount} bouts updated with event_date")
            conn3.commit()
        else:
            cur3.execute("""SELECT COUNT(*) FROM event_fights ef JOIN events e ON ef.event_id=e.id
                            WHERE ef.event_date IS DISTINCT FROM e.date""")
            print(f"  {cur3.fetchone()[0]} bouts would be updated (--commit to apply)")
        conn3.close()
        return 0

    # -- Mode recompute-methods standalone --
    if args.recompute_methods:
        print("=" * 62)
        print("  Recompute methodes wins/losses depuis fight_history")
        print(f"  Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
        print("=" * 62)
        _conn_rm = db.get_connection()
        if not _conn_rm:
            print("  [ERROR] Database connection impossible."); return 1
        _n = run_recompute_methods(_conn_rm, args.commit, verbose=args.verbose)
        _conn_rm.close()
        print(f"\n  {_n} fighters traites.")
        if not args.commit:
            print("  DRY-RUN. Add --commit to write to the database.")
        print("=" * 62)
        return 0

    # -- Mode fix-ids standalone --
    if args.fix_ids:
        print("=" * 62)
        print("  FIX NULL fighter_id / winner_id in event_fights")
        print(f"  Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
        print("=" * 62)
        conn2 = db.get_connection()
        if not conn2:
            print("  [ERROR] Database connection impossible."); return 1
        stats2 = resolve_null_fighter_ids(conn2, args.commit)
        conn2.close()
        for k, v in stats2.items():
            print(f"  {k:15s} : {v} bouts updated")
        if not args.commit:
            print("\n  DRY-RUN. Add --commit to apply.")
        return 0

    # -- Standalone backfill mode: catches up the fighters' records from the
    # completed events already in the database, without re-scraping (reads event_fights).
    if args.backfill_fighters:
        return run_backfill_fighters(args.commit, args.verbose,
                                     since=args.since, promotion=args.promotion)

    print("=" * 62)
    print("  Scraper Events (Tapology + UFC-FR)")
    print(f"  Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("=" * 62)

    if args.file:
        urls = read_urls_file(Path(args.file))
    elif args.urls:
        urls = args.urls
    else:
        urls = read_urls_interactive()
    if not urls:
        print("\n  No URL. Exit."); return 0

    print(f"\n  {len(urls)} URL(s) to process.")
    if not args.yes:
        try:
            confirm = input("  Start? (Enter=yes, other=cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Annule."); return 0
        if confirm:
            print("  Cancelled."); return 0

    if FAILED_LOG.exists(): FAILED_LOG.unlink()

    conn = db.get_connection()
    if not conn:
        print("  [ERROR] Database connection impossible."); return 1

    print("\n  Loading the fighters index from the database...")
    idx = load_fighter_index(conn)
    print(f"  {len(idx)} fighters indexes")

    # Events already in the database: {url: {id, date}}.
    # - UPCOMING event already in the database -> skip (nothing new, except --refresh)
    # - PAST event (date <= today) -> we ALWAYS reprocess it to
    # get/refresh the results and update the fighters.
    done: dict[str, dict] = {}
    if args.commit and not args.refresh:
        done = load_existing_event_urls(conn)
        print(f"  {len(done)} events already in the database (upcoming -> skip ; past -> reprocessed for results)")

    stats   = {"INSERT": 0, "UPDATE": 0, "FAIL": 0, "BLOCKED": 0,
               "SKIP_DRYRUN": 0, "SKIP_DONE": 0}
    t_start = time.time()

    proxy = getattr(args, "proxy", None)
    if proxy:
        print(f"  Proxy: {proxy}")

    # Playwright only if at least 1 Tapology URL is in the batch
    needs_playwright = any(_detect_source(u) == "tapology" for u in urls)

    browser = ctx = page = None
    pw_ctx  = None

    try:
        if needs_playwright:
            from playwright.sync_api import sync_playwright
            pw_ctx = sync_playwright().__enter__()
            browser, ctx, page = make_browser_page(pw_ctx, proxy=proxy)

        consecutive_blocks = 0
        for i, url in enumerate(urls, 1):
            url = url.rstrip("/")
            print(f"\n[{i}/{len(urls)}] {url}")

            if url in done:
                ev_date = done[url].get("date")
                is_past = bool(ev_date and ev_date <= date.today().isoformat())
                if not is_past:
                    print("  -> already in the database (upcoming), skip (resume)")
                    stats["SKIP_DONE"] += 1
                    continue
                # past event: we reprocess it to get/refresh the results
                print("  -> already in the database but past event: re-scrape for results + fighter update")

            # UFC-FR: no need for Playwright
            src      = _detect_source(url)
            use_page = page if src == "tapology" else None

            tries = 0
            op = "FAIL"
            while True:
                try:
                    try:
                        conn.cursor().execute("SELECT 1")
                    except Exception:
                        LOG.warning("DB connection lost, reconnecting...")
                        try: conn.close()
                        except Exception: pass
                        conn = db.get_connection()
                        idx  = load_fighter_index(conn)
                        LOG.info("Reconnecte.")
                    op = scrape_one(url, conn, idx, commit=args.commit,
                                    verbose=args.verbose, debug=args.debug, page=use_page)
                except Exception as e:  # noqa: BLE001
                    LOG.exception(f"Scrape error: {e}"); op = "FAIL"
                    try: conn.rollback()
                    except Exception: pass

                if op != "BLOCKED":
                    consecutive_blocks = 0
                    break

                tries += 1
                consecutive_blocks += 1
                if tries > BAN_MAX_RETRIES:
                    LOG.error(f"  Still blocked after {BAN_MAX_RETRIES} retries -> logged")
                    op = "BLOCKED"
                    break
                wait = min(BAN_WAIT_BASE * (2 ** (tries - 1)), BAN_WAIT_MAX)
                LOG.warning(f"  Tapology ban/block -> waiting {wait}s puis retry "
                            f"({tries}/{BAN_MAX_RETRIES})")
                if consecutive_blocks % RECREATE_EVERY == 0 and browser is not None:
                    try: browser.close()
                    except Exception: pass
                    browser, ctx, page = make_browser_page(pw_ctx, proxy=proxy)
                    use_page = page
                    LOG.info("  Browser recreated (new context)")
                time.sleep(wait)

            stats[op] = stats.get(op, 0) + 1
            if op in ("FAIL", "BLOCKED"):
                log_failure(url, op.lower())
            if i < len(urls) and src == "tapology":
                time.sleep(SLEEP_BETWEEN + random.uniform(0, 6))

    except KeyboardInterrupt:
        print("\n  Interrupted. Summary below.")
    finally:
        if browser:
            try: browser.close()
            except Exception: pass
        if pw_ctx:
            try: pw_ctx.__exit__(None, None, None)
            except Exception: pass
        conn.close()

    elapsed = time.time() - t_start
    print("\n" + "=" * 62)
    print(f"  SUMMARY ({elapsed:.1f}s)")
    for k, v in stats.items():
        if v: print(f"   {k:14s} : {v}")
    print("=" * 62)
    if stats.get("FAIL") or stats.get("BLOCKED"):
        print(f"  Failed/blocked URLs: {FAILED_LOG} (run again with --file on it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
