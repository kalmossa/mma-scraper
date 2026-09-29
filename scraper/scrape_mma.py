"""
scrape_mma.py
Consolidated scraper: you enter 3 URLs (FightMatrix + Tapology + Underground/mma.com)
and the script fills / updates `final tables - fighters.csv` in one go,
WITHOUT intermediate prompts.

USAGE (fastest, direct args):
    py -X utf8 scraper/scrape_mma.py <fm_url> <tapo_url> <ug_url>

USAGE (interactive - paste 3 URLs, 1 per line, then an empty line):
    py -X utf8 scraper/scrape_mma.py

The order of the URLs does not matter: the script detects the source from the domain.
"""

import sys, re, csv, json, time, logging, os
from pathlib import Path
from datetime import date, datetime
from urllib.parse import unquote
from bs4 import BeautifulSoup

# Cloudscraper > requests (fallback if not installed)
try:
    import cloudscraper
    _CLIENT = cloudscraper.create_scraper(
        browser={"browser": "firefox", "platform": "windows", "mobile": False}
    )
except ImportError:
    import requests
    _CLIENT = requests.Session()

sys.path.insert(0, str(Path(__file__).parent))
from parse_misc import (
    COLUMNS, lookup_country, fmt_date, days_since,
    parse_record, compute_derived, _fill,
)

ROOT  = Path(__file__).parent.parent  # repository root
FINAL = ROOT / "_temp" / "tables finales - fighters.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}
TIMEOUT      = 25
MAX_RETRIES  = 3
SLEEP_BETWEEN = 1.0

# US states (to fix nationality: "Detroit, Michigan" -> United States)
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "dc",
    # Note: "Georgia" is not included (ambiguous: US state AND country). Tapology
    # usually qualifies it with the country context so we take it as is.
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("scrape_mma")


# http requests

# --- Selenium (real Chrome): a FREE and "unblockable" option against Cloudflare ---
# When the IP is banned (503 / "Just a moment...") and there is no VPN, the
# fetch is routed through a REAL Chrome. Cloudflare sees a human browser -> lets it through.
# Enabled by the env var SCRAPER_SELENIUM=1. A SINGLE driver is shared (singleton):
# the 1st page solves the Cloudflare challenge, the clearance cookie is kept
# for all the following URLs (otherwise the challenge is triggered again every time).
# If a captcha appears, solve it BY HAND once (visible mode, not headless).
_SELENIUM_DRIVER = None


def _version_chrome():
    """the major version of the installed Chrome (e.g. 153), or None if unknown."""
    try:
        import winreg
        cles = ((winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon", "version"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Wow6432Node\Google\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}", "pv"))
        for hive, cle, nom in cles:
            try:
                with winreg.OpenKey(hive, cle) as k:
                    return int(str(winreg.QueryValueEx(k, nom)[0]).split(".")[0])
            except OSError:
                continue
    except ImportError:
        pass   # not on Windows
    import shutil
    import subprocess
    candidats = ["chrome", "google-chrome",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    for c in candidats:
        chemin = shutil.which(c) or (c if os.path.exists(c) else None)
        if not chemin:
            continue
        try:
            sortie = subprocess.run([chemin, "--version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"(\d+)\.", sortie)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def _version_driver(exe):
    """the major version of a chromedriver.exe, or None."""
    import subprocess
    try:
        sortie = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"(\d+)\.", sortie)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _get_selenium_driver():
    global _SELENIUM_DRIVER
    if _SELENIUM_DRIVER is not None:
        return _SELENIUM_DRIVER
    import undetected_chromedriver as uc
    from pathlib import Path
    # Persistent profile: the cf_clearance cookie survives between pages and between runs.
    # Solve the Turnstile ONCE; all the following pages go straight through.
    profile_dir = str(Path(__file__).resolve().parent.parent / ".chrome_profile")
    opts = uc.ChromeOptions()
    if os.environ.get("SCRAPER_SELENIUM_HEADLESS") == "1":
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,900")
    # ChromeDriver: we keep the one from tools/ AS LONG AS it matches the installed Chrome.
    # Chrome updates itself; with a frozen driver we ended up with
    # "only supports Chrome version 149 / current browser version is 153",
    # Selenium giving up and the scrape falling back to cloudscraper -> Tapology 403.
    chrome = _version_chrome()
    _cd_kwargs = {}
    for exe in sorted((Path(__file__).resolve().parent.parent / "tools").glob("chromedriver*.exe")):
        if chrome and _version_driver(exe) == chrome:
            _cd_kwargs = {"driver_executable_path": str(exe)}
            LOG.info(f"Selenium: local chromedriver {exe.name} (Chrome {chrome})")
            break
    if not _cd_kwargs and chrome:
        # no local driver at the right version: UC fetches the matching one
        _cd_kwargs = {"version_main": chrome}
        LOG.info(f"Selenium: no local chromedriver for Chrome {chrome}, automatic download")
    drv = uc.Chrome(options=opts, user_data_dir=profile_dir, **_cd_kwargs)
    drv.set_page_load_timeout(45)   # give up if the page does not load within 45s -> automatic retry
    _SELENIUM_DRIVER = drv
    LOG.info("Selenium: Chrome UC started (persistent profile -> captcha only once)")
    return drv

# Markers of a Cloudflare challenge page (NOT the real content).
_CF_CHALLENGE_MARKERS = (
    "just a moment", "cf-browser-verification", "challenge-platform",
    "cf_chl_opt", "__cf_chl", "turnstile", "checking your browser",
    "enable javascript and cookies to continue", "needs to review the security",
)
# How long we let the human solve the Turnstile before giving up.
SELENIUM_CHALLENGE_WAIT = 180   # seconds

def _is_cloudflare_challenge(html: str, title: str) -> bool:
    blob = (title + " " + html[:4000]).lower()
    return any(m in blob for m in _CF_CHALLENGE_MARKERS)

def _looks_like_real_tapology(html: str) -> bool:
    """Real Tapology profile: we require a real-content marker.
    Also returns False for Tapology 404 pages ('page you requested').
    """
    low = html.lower()
    # Tapology 404 error page -> not a real profile
    if "doesn't look like the page you requested" in low:
        return False
    if "page you requested does not exist" in low:
        return False
    return ("pro mma record" in low) or ("fighter details" in low) or ("fightcenter/bouts" in low)


def _wait_tapology_bouts(drv, max_wait: float = 30.0, stable_needed: int = 2) -> str:
    """
    CRITICAL BUG (June 2026): on a Tapology fighter page, the BIO loads in
    ~1s but the FIGHT LIST is rendered in JS a few seconds later.
    `_looks_like_real_tapology` becomes true as soon as the bio is there -> we captured the HTML
    BEFORE the list was rendered -> 0 or only some of the fights (wrong record:
    Yan 0 fights, Albazi 18/20). Here we wait for the number of bouts
    (data-fighter-bout-target) to be > 0 AND STABLE over several polls,
    scrolling to the bottom to trigger any lazy-load.
    """
    prev = -1
    stable = 0
    waited = 0.0
    html = drv.page_source
    while waited < max_wait:
        try:
            drv.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
        time.sleep(1.2)
        waited += 1.2
        html = drv.page_source
        n = html.count("data-fighter-bout-target")
        if n > 0 and n == prev:
            stable += 1
            if stable >= stable_needed:
                LOG.debug(f"Tapology : {n} bouts rendered and stable after {waited:.0f}s")
                break
        else:
            stable = 0
        prev = n
        # Fighter with no fight listed (start of career) -> do not block for 30s
        if n == 0 and waited >= 6:
            LOG.debug("Tapology: no bout after 6s (fighter without history?)")
            break
    return html

def _selenium_fetch(url: str) -> str | None:
    """
    Fetch through a real Chrome. NEVER RETURNS a Cloudflare challenge page:
    if Cloudflare blocks, we PRINT a message and wait for the human to solve
    the Turnstile in the Chrome window (up to SELENIUM_CHALLENGE_WAIT s). Once
    solved, the cf_clearance cookie is kept -> the following URLs go through
    without a challenge.
    """
    try:
        drv = _get_selenium_driver()
    except Exception as e:
        LOG.error(f"Selenium unavailable ({type(e).__name__}: {e}) -> pip install selenium")
        return None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            drv.get(url)
            waited = 0.0
            warned = False
            while waited < SELENIUM_CHALLENGE_WAIT:
                html  = drv.page_source
                title = (drv.title or "")
                if _is_cloudflare_challenge(html, title):
                    if not warned:
                        LOG.warning(
                            "=> CLOUDFLARE: solve the captcha BY HAND in the Chrome window. "
                            f"Waiting up to {SELENIUM_CHALLENGE_WAIT}s..."
                        )
                        warned = True
                    time.sleep(2.0)
                    waited += 2.0
                    continue
                # Tapology 503 / server error page -> pause 60s and retry
                if "cette page ne fonctionne pas" in html.lower() or "http error 503" in html.lower():
                    LOG.warning("Tapology 503 -> pause 60s before retry")
                    time.sleep(60.0)
                    break  # force a retry (new iteration of the attempt loop)
                # Page loaded, no more challenge. Check that it is real content.
                if detect_source(url) == "tapology" and not _looks_like_real_tapology(html):
                    # Tapology JS profile not populated yet -> give it a bit of time
                    if waited < 8:
                        time.sleep(1.0)
                        waited += 1.0
                        continue
                # Fighter page: the bio is there but the FIGHT LIST is rendered in
                # JS afterwards -> we wait for it to be complete before capturing.
                if "/fightcenter/fighters/" in url and "tapology.com" in url:
                    html = _wait_tapology_bouts(drv)
                if len(html) > 2000:
                    if os.environ.get("SCRAPER_DUMP_HTML") == "1":
                        dump = Path(__file__).parent.parent / "debug" / "tapology_dump.html"
                        dump.parent.mkdir(exist_ok=True)
                        dump.write_text(html, encoding="utf-8")
                        LOG.info(f"HTML dump -> {dump}")
                    return html
                time.sleep(1.0)
                waited += 1.0
            LOG.error(f"Selenium: challenge NOT solved after {SELENIUM_CHALLENGE_WAIT}s -> {url}")
        except Exception as e:
            LOG.warning(f"Selenium {type(e).__name__}: {e} - retry {attempt}/{MAX_RETRIES}")
            if attempt == MAX_RETRIES:
                # Dead driver (frame detached, ReadTimeout...) -> reset for the next call
                global _SELENIUM_DRIVER
                try:
                    _SELENIUM_DRIVER.quit()
                except Exception:
                    pass
                _SELENIUM_DRIVER = None
                LOG.warning("Selenium driver reset (crash detected)")
        time.sleep(2.0 * attempt)
    return None


def fetch_html(url: str) -> str | None:
    """
    Fetch the HTML of a URL.

    For Tapology fighter profiles, we force ?sport=mma to include
    ONLY the MMA fights (no kickboxing / boxing / etc.).

    Three anti-Cloudflare strategies (in order of preference when the IP is banned):
      1. SCRAPER_SELENIUM=1  -> real Chrome (FREE, unblockable, ~2-4 pages/min).
         Solve the captcha by hand ONCE; the cookie is then reused.
         Optional: SCRAPER_SELENIUM_HEADLESS=1 (less reliable against Cloudflare).
      2. SCRAPER_PROXY=...   -> route through a proxy/VPN (socks5://127.0.0.1:9050 for
         Tor, http://user:pass@host:port). NB: Cloudflare often blocks Tor.
      3. cloudscraper (default) -> fast but gets banned at volume.

    Selenium is ONLY used for Tapology (the only source behind Cloudflare);
    FightMatrix / UFC-FR stay on cloudscraper (fast).
    """
    # Profil fighter Tapology -> forcer ?sport=mma (filtre cote serveur)
    if "/fightcenter/fighters/" in url and "tapology.com" in url:
        sep = "&" if "?" in url else "?"
        url = url + sep + "sport=mma"

    # Tapology = always Selenium (Cloudflare blocks cloudscraper).
    # SCRAPER_SELENIUM=0 forces the fallback if needed (rare).
    use_selenium = (
        detect_source(url) == "tapology"
        and os.environ.get("SCRAPER_SELENIUM") != "0"
    )
    if use_selenium:
        html = _selenium_fetch(url)
        if html:
            return html
        LOG.warning("Selenium failed -> cloudscraper fallback")

    last = None
    proxy_url = os.environ.get("SCRAPER_PROXY")
    proxies   = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _CLIENT.get(url, headers=HEADERS, timeout=TIMEOUT, proxies=proxies)
            last = r.status_code
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
            LOG.warning(f"HTTP {r.status_code} ({len(r.text)} chars) - retry {attempt}/{MAX_RETRIES}")
        except Exception as e:
            LOG.warning(f"{type(e).__name__}: {e} - retry {attempt}/{MAX_RETRIES}")
        time.sleep(1.5 * attempt)
    LOG.error(f"Failure after {MAX_RETRIES} attempts (last={last}): {url}")
    return None


def detect_source(url: str) -> str:
    u = url.lower()
    if "fightmatrix.com"      in u: return "fightmatrix"
    if "tapology.com"         in u: return "tapology"
    if "mixedmartialarts.com" in u: return "underground"
    if "ufc-fr.com"           in u: return "ufc_fr"
    # ufc.com: handles /athlete/ and /fr-FR/athlete/ or other languages
    if "ufc.com" in u and "/athlete/" in u: return "ufc"
    return "unknown"


# helper: find a "Label: Value" in any structure

def find_value(soup, label_pattern: str) -> str:
    """
    Look for a label (regex) and return the neighbouring value.
    Handles:
      - <strong>Label:</strong> Value         (Tapology)
      - <li><strong>Label:</strong> Value</li>
      - <td>Label:</td><td>Value</td>          (FightMatrix)
      - <th>Label</th><td>Value</td>
      - <span class=label>Label:</span><span>Value</span>
    """
    rx = re.compile(rf"^\s*{label_pattern}\s*:?\s*$", re.I)
    candidates = soup.find_all(["strong", "b", "th", "dt", "span", "label", "div", "td"])
    for tag in candidates:
        t = tag.get_text(" ", strip=True)
        if not t or len(t) > 60:
            continue
        if not rx.match(t):
            continue

        # Case 1: the value is in the parent, after the label
        parent = tag.parent
        if parent:
            full = parent.get_text(" ", strip=True)
            # Remove the label from the start
            m = re.match(rf"^{re.escape(t)}\s*:?\s*(.+)$", full)
            if m:
                v = m.group(1).strip(" :|·-")
                if v and v.lower() != t.lower().rstrip(":"):
                    return v

        # Case 2: next sibling (next td or next span)
        nxt = tag.find_next_sibling()
        if nxt:
            v = nxt.get_text(" ", strip=True)
            if v:
                return v

        # Case 3: tag with the value as the last text node
        for sib in tag.next_siblings:
            if hasattr(sib, "get_text"):
                v = sib.get_text(" ", strip=True)
            else:
                v = str(sib).strip()
            if v:
                return v
    return ""


def first_match(text: str, *patterns: str, group: int = 1, flags: int = 0) -> str:
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(group)
    return ""


# utils for the name / nickname

# Every possible quote: ASCII + single/double smart quotes
_QUOTE_CHARS = '"“”‘’«»'


def _is_garbage_name(name: str) -> bool:
    """
    True if 'name' is obviously not a fighter name: a domain
    (www.tapology.com), a URL, or a Cloudflare error page marker.
    Safeguard: without it, a badly fetched challenge page set
    name='www.tapology.com' -> everything matched the same garbage row.
    """
    n = (name or "").strip().lower()
    if not n:
        return True
    if re.search(r"\b(?:www\.|https?://|\.com|\.net|\.org|tapology|fightmatrix)\b", n):
        return True
    if any(m in n for m in (
        "just a moment", "attention required", "access denied", "error",
        "cette page ne fonctionne", "this page isn't working", "page not found",
        "503", "404", "ne fonctionne pas", "temporarily unavailable",
        "doesn't look like", "page you requested", "doesn't exist",
        "not found", "page introuvable", "oops", "something went wrong",
    )):
        return True
    # A valid fighter name: between 2 and 60 chars, mostly letters
    if len(name.strip()) > 80:
        return True
    return False


def _extract_name_and_nickname(raw: str) -> tuple[str, str | None]:
    """
    Split a nickname stuck to the name in a raw string.

    Supported formats:
      'Tatsuro Taira ("The Best")'   -> ('Tatsuro Taira', 'The Best')
      'Tatsuro Taira (The Best)'     -> ('Tatsuro Taira', 'The Best')
      'Manel Kape "Prodigio"'         -> ('Manel Kape', 'Prodigio')
      'Manel "Starboy" Kape'          -> ('Manel Kape', 'Starboy')
      'Tatsuro Taira'                 -> ('Tatsuro Taira', None)

    Crucial for multi-source matching: a "polluted" name (with parentheses
    or quotes) no longer matches in the normalized index and causes a duplicate
    INSERT instead of an UPDATE.
    """
    if not raw:
        return "", None

    nickname: str | None = None
    s = raw

    # 1) Nickname in parentheses: "Name (Nick)" or "Name (\"Nick\")"
    m = re.search(rf'\(\s*[{_QUOTE_CHARS}]?\s*([^()' + _QUOTE_CHARS + r']+?)\s*[' + _QUOTE_CHARS + r']?\s*\)', s)
    if m:
        candidate = m.group(1).strip()
        if candidate:
            nickname = candidate
        s = re.sub(r'\s*\([^()]*\)\s*', ' ', s)

    # 2) Nickname in quotes (in the middle or at the end): "Name 'Nick' Rest" / "Name 'Nick'"
    m = re.search(rf'[{_QUOTE_CHARS}]([^{_QUOTE_CHARS}]+?)[{_QUOTE_CHARS}]', s)
    if m:
        candidate = m.group(1).strip()
        if candidate and not nickname:
            nickname = candidate
        s = re.sub(rf'\s*[{_QUOTE_CHARS}][^{_QUOTE_CHARS}]+[{_QUOTE_CHARS}]\s*', ' ', s)

    s = re.sub(r'\s+', ' ', s).strip()
    return s, nickname


#  fightmatrix

def parse_fightmatrix_html(html: str, row: dict, url: str):
    soup = BeautifulSoup(html, "lxml")
    full = soup.get_text(" ", strip=True)
    full = re.sub(r"\s+", " ", full)

    # Name from URL (fallback if the H1 is missing)
    m = re.search(r"/fighter-profile/([^/]+)/(\d+)", url)
    if m and not row.get("name"):
        row["name"] = unquote(m.group(1)).strip()

    # NAME: h1 or title
    h1 = soup.find("h1")
    if h1:
        nm = h1.get_text(" ", strip=True)
        nm = re.sub(r"\s+", " ", nm).strip()
        # FM met "Joshua Van | Fighter Profile" parfois
        nm = re.split(r"\s*\|", nm)[0].strip()
        if nm and 2 < len(nm) < 80 and not re.search(r"record|profile", nm, re.I):
            _fill(row, "name", nm)

    # Bio / records via regex on flattened text (FM uses a lot of label: value)
    # Birth Date: 2001-10-10
    v = first_match(full, r"Birth Date:\s*(\d{4}-\d{2}-\d{2})")
    if v: _fill(row, "date_of_birth", v)

    # Pro Debut Date: 2012-05-13
    v = first_match(full, r"Pro Debut Date:\s*(\d{4}-\d{2}-\d{2})")
    if v: _fill(row, "career_debut_date", v)

    # last fight date: M/DD/YYYY OR YYYY-MM-DD
    v = first_match(full, r"Last Fight Date:\s*(\d{1,2}/\d{1,2}/\d{4})",
                          r"Last Fight Date:\s*(\d{4}-\d{2}-\d{2})")
    if v: _fill(row, "last_fight_date", fmt_date(v))

    # UFC debut
    v = first_match(full, r"UFC Debut:\s*(\d{4}-\d{2}-\d{2})")
    # no dedicated column, we could store it in notes but not critical

    # Pro Record: 22-7-0
    v = first_match(full, r"Pro Record:\s*(\d+-\d+-\d+)")
    if v:
        w, l, d = parse_record(v)
        if w is not None:
            _fill(row, "record_total_wins",   w)
            _fill(row, "record_total_losses", l)
            _fill(row, "record_total_draws",  d)

    # UFC record: 7-3-0 [, N NC]
    m = re.search(r"UFC Record:\s*(\d+)-(\d+)-(\d+)(?:\s*,\s*(\d+)\s*NC)?", full)
    if m:
        _fill(row, "record_ufc_wins",   m.group(1))
        _fill(row, "record_ufc_losses", m.group(2))
        _fill(row, "record_ufc_draws",  m.group(3))
        if m.group(4): _fill(row, "record_total_nc", m.group(4))

    # Rating Points: 430
    v = first_match(full, r"Rating Points?:\s*(\d+)")
    if v: _fill(row, "fightmatrix_rating_points", v)

    # 'Big League' Record: 13-6-0
    v = first_match(full, r"['’]Big League['’]\s*Record:\s*(\d+-\d+-\d+)")
    if v: _fill(row, "fightmatrix_big_league_record", v)

    # 540 Metric: 0.810
    v = first_match(full, r"540 Metric:\s*([\d.]+)")
    if v: _fill(row, "fightmatrix_540_metric", v)

    # Quality Perf. %: 62.1
    v = first_match(full, r"Quality Perf\.?\s*%:\s*([\d.]+)")
    if v: _fill(row, "fightmatrix_quality_perf_pct", v)

    # Win Finish %: 45.5
    v = first_match(full, r"Win Finish\s*%:\s*([\d.]+)")
    if v: _fill(row, "finish_rate_int", str(int(float(v))))

    # Last 5: from the <td> after the label (W W W W L for example)
    last5_val = find_value(soup, r"Last 5")
    if last5_val:
        rs = re.findall(r"[WLD]", last5_val)[:5]
        if rs: _fill(row, "last_5_results", ",".join(rs))
    if not row.get("last_5_results"):
        # Look in the text "Last 5: W W W W W"
        m = re.search(r"Last 5:?\s*((?:[WLD][\s,]+){0,4}[WLD])", full)
        if m:
            rs = re.findall(r"[WLD]", m.group(1))[:5]
            if rs: _fill(row, "last_5_results", ",".join(rs))

    # Current Ranking: #X <Division>  (FM is the reference for the UFC division)
    # Champion: FM shows "C " in place of the rank (literally the letter C followed by the division)
    m = re.search(
        r"Current Ranking:\s*(?:#?(\d+)|(C(?:hampion)?))\s+([A-Za-z][\w +'-]*?weight)",
        full, re.I,
    )
    if m:
        rank_num = m.group(1) or ""
        is_champ = bool(m.group(2))   # matched "C" or "Champion" -> real champion
        wc_raw   = m.group(3).strip()
        # Normalize: FM writes "Women Bantamweight" without an apostrophe.
        # We add "'s" for consistency with the other sources (UFC-FR, Tapology).
        # Then capitalize while avoiding .title() which breaks "Women's" -> "Women'S".
        wc = re.sub(r"(?i)^Women\b(?!')", "Women's", wc_raw)
        wc = " ".join(w.capitalize() for w in wc.split())
        # FM has its own proprietary ranking (not the official UFC rank).
        # We do NOT use FM's rank_num for ufc_official_rank - only UFC-FR is authoritative.
        if is_champ:
            row["is_ufc_champion"] = "TRUE"
        # FM is authoritative for the current division
        row["weight_class_current"] = wc
        # weight_class_origin by default = current (will be overridden by All-Time Rank if different)
        _fill(row, "weight_class_origin", wc)

    # All-Time Rank : "All-Time Rank: #2 Absolute #22 Heavyweight+ #1 Light Heavyweight"
    # The best historical rank (excluding "Absolute") = origin division
    # Makes it possible to detect Jones (HW current / LHW origin), Makhachev (WW current / LW origin), etc.
    m_at = re.search(r"All-Time Rank:([^.\n]{1,250})", full)
    if m_at:
        at_text = m_at.group(1)
        divisions = []
        for nm in re.finditer(r"#(\d+)\s+([A-Z][\w '-]*?weight)\+?", at_text):
            rk  = int(nm.group(1))
            div = nm.group(2).strip().title()
            if div.lower() != "absolute":
                divisions.append((rk, div))
        if divisions:
            divisions.sort(key=lambda x: x[0])  # lowest rank = best
            best_div = divisions[0][1]
            current  = row.get("weight_class_current", "").strip().title()
            # Override origin ONLY if the historical division differs from the current one
            if current and best_div.lower() != current.lower():
                row["weight_class_origin"] = best_div

    # FM has its own P4P ranking (different from the official UFC one) - only UFC-FR is authoritative.

    # Title Bouts: X-Y-Z  → ufc_title_match_win
    # Convention: we store the TOTAL of title-fight wins (win + defenses),
    # not only the number of defenses. More interpretable and aligned with FightMatrix.
    m = re.search(r"Title Bouts?:\s*(\d+)-(\d+)-(\d+)", full)
    if m:
        title_wins   = int(m.group(1))
        title_losses = int(m.group(2))
        if title_wins > 0:
            _fill(row, "ufc_title_match_win", str(title_wins))
        if title_wins > 0 or title_losses == 0:
            # If unbeaten in titles, probably the current champion (no, that is wrong)
            pass  # is_ufc_champion is already handled by the regex above

    # Longest Win/Loss Streak active
    m = re.search(r"Longest Win Streak:\s*(\d+)[^.()]*\(\s*[\d-]*\s*,?\s*active\s*\)", full, re.I)
    if m: row["current_streak"] = f"{m.group(1)}W"
    m = re.search(r"Longest Loss Streak:\s*(\d+)[^.()]*\(\s*[\d-]*\s*,?\s*active\s*\)", full, re.I)
    if m and not row.get("current_streak", "").endswith("W"):
        row["current_streak"] = f"{m.group(1)}L"

    # design decision: photo_url comes exclusively from tapology.
    # FightMatrix no longer touches this column.

    # Fight history from the FightMatrix table
    parse_fm_fight_history(soup, row)


def parse_fm_fight_history(soup, row):
    fights = []
    for tbl in soup.find_all("table"):
        head = tbl.get_text(" ", strip=True).lower()[:300]
        if ("opponent" in head and "event" in head and "outcome" in head):
            raw_tds = tbl.find_all("tr")[1:]
            for tr in raw_tds:
                td_tags = tr.find_all("td")
                tds = [td.get_text(" ", strip=True) for td in td_tags]
                if len(tds) < 4:
                    continue
                result = tds[0][:3].upper().strip()
                if result not in ("W", "L", "D", "NC"):
                    continue
                # Extract the opponent's FM link if present
                opp_fm_url = ""
                if len(td_tags) > 1:
                    opp_a = td_tags[1].find("a", href=re.compile(r"/fighter-profile/"))
                    if opp_a and opp_a.get("href"):
                        href = opp_a["href"]
                        opp_fm_url = href if href.startswith("http") else f"https://www.fightmatrix.com{href}"
                entry = {
                    "result":   result,
                    "opponent": tds[1] if len(tds) > 1 else "",
                    "event":    tds[2] if len(tds) > 2 else "",
                    "date":     tds[3] if len(tds) > 3 else "",
                    "method":   tds[4] if len(tds) > 4 else "",
                }
                if opp_fm_url:
                    entry["opponent_fm_url"] = opp_fm_url
                fights.append(entry)
            if fights:
                break
    if fights:
        _set_fight_history(row, fights, "fightmatrix")


#  tapology

def parse_tapology_html(html: str, row: dict, url: str):
    soup = BeautifulSoup(html, "lxml")
    full = soup.get_text(" ", strip=True)
    full = re.sub(r"\s+", " ", full)

    # NAME + nickname from h1 (formats: "manel "starboy" kape" / "joshua van" / "manel "the prodigio" kape")
    h1 = soup.find("h1")
    if h1:
        raw = h1.get_text(" ", strip=True)
        raw = re.sub(r"\s+", " ", raw)
        # Cut the " | Boxer Page" / " | MMA Fighter" suffix etc.
        # (boxer profiles on Tapology: their H1 = "Name | Boxer Page")
        raw = re.split(r"\s*\|\s*", raw)[0].strip()
        # Format with the nickname in quotes (smart quotes or ASCII)
        m = re.match(r'(.+?)\s+["“‘](.+?)["”’]\s+(.+)', raw)
        if m:
            _fill(row, "name", f"{m.group(1)} {m.group(3)}".strip())
            _fill(row, "nickname", m.group(2).strip())
        else:
            # sometimes the h1 contains "Pro MMA Record: X-Y-Z" -> we cut it
            raw = re.split(r"\s+(?:Pro|MMA Record|Record)\b", raw)[0].strip()
            if raw:
                _fill(row, "name", raw)

    # fallbacks if the h1 is missing / empty / malformed (seen on Tatsuro Taira)
    # The og:title/title of Tapology sometimes contains the nickname stuck on:
    #   "Tatsuro Taira (\"The Best\")"   -> name="Tatsuro Taira", nickname="The Best"
    # We always clean it before _fill to avoid duplicates (a polluted name
    # no longer matches the normalized index and causes an INSERT instead of an UPDATE).
    if not row.get("name"):
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            cand = re.sub(r"\s+", " ", og_title["content"]).strip()
            cand = re.split(r"\s*[-|]\s*(?:MMA|Tapology|Fighter|Profile)", cand, maxsplit=1)[0].strip()
            name_clean, nick = _extract_name_and_nickname(cand)
            if name_clean and not _is_garbage_name(name_clean):
                _fill(row, "name", name_clean)
            if nick:
                _fill(row, "nickname", nick)

    if not row.get("name"):
        title_tag = soup.find("title")
        if title_tag:
            cand = re.sub(r"\s+", " ", title_tag.get_text(" ", strip=True))
            cand = re.split(r"\s*[-|]\s*(?:MMA|Tapology|Fighter|Profile)", cand, maxsplit=1)[0].strip()
            name_clean, nick = _extract_name_and_nickname(cand)
            if name_clean and not _is_garbage_name(name_clean):
                _fill(row, "name", name_clean)
            if nick:
                _fill(row, "nickname", nick)

    if not row.get("name"):
        # Dernier recours : URL "/fightcenter/fighters/145595-tatsuro-taira"
        m_url = re.search(r"/fighters/\d+-([\w-]+)", url, re.I)
        if m_url:
            cand = m_url.group(1).replace("-", " ").strip()
            if cand:
                _fill(row, "name", cand.title())

    # Nickname if still empty: "Nickname: <value>" in the details block
    if not row.get("nickname"):
        nick = find_value(soup, r"Nickname")
        if nick and nick.lower() not in ("n/a", "none", ""):
            _fill(row, "nickname", nick.strip(' "“”'))

    # AGE / DOB : format Tapology "Age: 32 | Date of Birth: 1993 Nov 14"
    m = re.search(r"Age:\s*(\d+)\s*\|\s*Date of Birth:\s*(\d{4})\s+(\w+)\s+(\d+)", full)
    if m:
        _fill(row, "age", m.group(1))
        try:
            dob = datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}", "%Y %b %d")
            _fill(row, "date_of_birth", dob.strftime("%Y-%m-%d"))
        except ValueError:
            pass
    else:
        # Fallback 1: "Date of Birth: 2000 Jan 25" (without the "Age: X |" prefix)
        m_dob2 = re.search(r"Date of Birth:\s*(\d{4})\s+([A-Za-z]{3,9})\s+(\d{1,2})", full)
        if m_dob2 and m_dob2.group(2).lower() != "n/a":
            try:
                dob = datetime.strptime(f"{m_dob2.group(1)} {m_dob2.group(2)} {m_dob2.group(3)}", "%Y %b %d")
                _fill(row, "date_of_birth", dob.strftime("%Y-%m-%d"))
            except ValueError:
                pass
        # Fallback 2 : find_value labels
        v_age = find_value(soup, r"Age")
        if v_age and not row.get("date_of_birth"):
            # Tapology sometimes puts the DOB under the Age label (e.g. "2000-01-25 2000 • Jan 25")
            m_iso = re.search(r"(\d{4}-\d{2}-\d{2})", v_age)
            m_bul = re.search(r"(\d{4})\s*[•·]\s*([A-Za-z]{3,9})\s+(\d{1,2})", v_age)
            if m_iso:
                _fill(row, "date_of_birth", m_iso.group(1))
            elif m_bul:
                try:
                    dob = datetime.strptime(f"{m_bul.group(1)} {m_bul.group(2)} {m_bul.group(3)}", "%Y %b %d")
                    _fill(row, "date_of_birth", dob.strftime("%Y-%m-%d"))
                except ValueError:
                    pass
            else:
                mm = re.search(r"\b(\d{2,3})\b", v_age)
                if mm: _fill(row, "age", mm.group(1))
        v_dob = find_value(soup, r"Date of Birth")
        if v_dob and v_dob.lower() not in ("n/a", "none", ""):
            try:
                dob = datetime.strptime(v_dob.strip(), "%Y %b %d")
                _fill(row, "date_of_birth", dob.strftime("%Y-%m-%d"))
            except ValueError:
                _fill(row, "date_of_birth", fmt_date(v_dob))

    # height : "5'7" (170cm)" -> stocke "5'7""
    m = re.search(r"Height:?\s*(\d+)\s*['’]\s*(\d+)\s*[\"”]?", full)
    if m:
        _fill(row, "height_inches", f"{m.group(1)}'{m.group(2)}\"")

    # reach : "67.5" (171cm)" -> stocke "67.5""
    m = re.search(r"Reach:?\s*([\d.]+)\s*[\"”]", full)
    if m:
        _fill(row, "reach_inches", f'{m.group(1)}"')

    # the stance: 1) label in the structure; 2) regex on the text
    stance_val = find_value(soup, r"Stance")
    if stance_val:
        mm = re.search(r"(Orthodox|Southpaw|Switch|Open Stance)", stance_val, re.I)
        if mm: _fill(row, "stance", mm.group(1).title())
    if not row.get("stance"):
        m = re.search(r"Stance:?\s*(Orthodox|Southpaw|Switch|Open Stance)", full, re.I)
        if m: _fill(row, "stance", m.group(1).title())
    if not row.get("stance"):
        m = re.search(r"\b(Orthodox|Southpaw|Switch)\b", full)
        if m: _fill(row, "stance", m.group(1).title())

    # WEIGHT CLASS (Tapology = weight of the LAST fight -> only used for weight_class_current
    # if FightMatrix did not provide the division through Current Ranking)
    m = re.search(r"Weight Class:?\s*([A-Z][\w '-]*?weight)", full, re.I)
    if m:
        wc = m.group(1).strip().title()
        _fill(row, "weight_class_current", wc)
        # Do NOT overwrite weight_class_origin here: FM will fill it from the ranking

    # is_ufc_champion : source unique = FM "current ranking: C <weight>".
    # If FM gives a numeric rank -> FALSE in scrape() post-parsing.

    # foundation style -> first_sport (base style / original sport)
    fs = find_value(soup, r"Foundation Style")
    if not fs:
        m = re.search(r"Foundation Style:?\s*([\w\- ]{2,40})", full)
        if m: fs = m.group(1).strip()
    if fs:
        fs = re.split(r"\s+(?:Head Coach|Other|Affiliation)", fs)[0].strip()
        if fs.lower() not in ("n/a", "none", "look-see-do", ""):
            _fill(row, "first_sport", fs)

    # NATIONALITY: "Born" as the main source (stable value on Tapology),
    # "Fighting out of" only if Born is missing/empty AND the extracted value
    # is a clean country (not a US state, not junk). Max 50 chars to avoid
    # capturing HTML artifacts.
    _NAT_STOP = (
        "Fighting out of", "Last Fight", "Affiliation", "Foundation Style",
        "Career Disclosed", "College", "Pro MMA", "Head Coach", "Other",
        "Resource", "Personal", "Schedule", "UFC", "Tapology", "Update",
    )
    _nat_stop_rx = "|".join(re.escape(s) for s in _NAT_STOP)

    def _extract_country(text: str) -> str:
        # Cut at the first stray keyword if the regex did not delimit properly
        for stop in _NAT_STOP:
            if stop.lower() in text.lower():
                text = text[:text.lower().find(stop.lower())].strip(" ,|")
                break
        c = text.split(",")[-1].strip(" |")
        if not c or len(c) > 50 or c.lower() in ("n/a", "none"):
            return ""
        if c.lower() in _US_STATES:
            return "United States"
        # Sanity check: the country must only contain letters, spaces, hyphens, dots
        if not re.match(r"^[\w\s\-\.]+$", c):
            return ""
        return c

    _born_rx = re.compile(r"Born:?\s*([^|\n]{2,60}?)(?:\s+(?:" + _nat_stop_rx + r"))", re.I)
    _fof_rx  = re.compile(r"Fighting out of:?\s*([^|\n]{2,60}?)(?:\s+(?:" + _nat_stop_rx + r"))", re.I)

    m_born = _born_rx.search(full)
    m_fof  = _fof_rx.search(full)

    born_country = _extract_country(m_born.group(1).strip()) if m_born else ""
    fof_country  = _extract_country(m_fof.group(1).strip())  if m_fof  else ""

    # Born = main source (birth city = nationality in MMA in 90% of cases).
    # Fighting out of = fallback if Born is empty (e.g. incomplete page).
    nat = born_country or fof_country
    if nat:
        _fill(row, "nationality", nat)

    # PRO MMA record : "pro MMA record: 22-7-0, 1 NC"
    m = re.search(r"Pro MMA Record:?\s*(\d+)-(\d+)-(\d+)(?:[,\s]+(\d+)\s*NC)?", full, re.I)
    if m:
        _fill(row, "record_total_wins",   m.group(1))
        _fill(row, "record_total_losses", m.group(2))
        _fill(row, "record_total_draws",  m.group(3))
        if m.group(4): _fill(row, "record_total_nc", m.group(4))

    # current MMA streak
    m = re.search(r"Current MMA Streak:?\s*(\d+)\s+(Wins?|Losses?)", full, re.I)
    if m:
        ch = "W" if "Win" in m.group(2) else "L"
        row["current_streak"] = f"{m.group(1)}{ch}"

    # LAST fight : "last fight: november 16, 2024 in UFC"
    m = re.search(r"Last Fight:?\s*([A-Z][a-z]+ \d+,\s*\d{4})", full)
    if m: _fill(row, "last_fight_date", fmt_date(m.group(1)))

    # WIN methods (zone "pro MMA statistics")
    stats_start = full.find("Pro MMA Statistics")
    if stats_start < 0:
        stats_start = full.find("Pro MMA Stats")
    stats_end   = full.find("MMA Record By Promotion")
    if stats_end < 0 or stats_end < stats_start:
        stats_end = stats_start + 2000 if stats_start >= 0 else len(full)
    stats_zone = full[stats_start:stats_end] if stats_start >= 0 else full

    for method, w_col, l_col in [
        ("KO/TKO",      "wins_by_ko_tko",     "losses_by_ko_tko"),
        ("Submission",  "wins_by_submission", "losses_by_submission"),
        ("Decision",    "wins_by_decision",   "losses_by_decision"),
    ]:
        m = re.search(rf"{re.escape(method)}\s+(\d+)\s+wins?[,\s]+(\d+)\s+loss", stats_zone, re.I)
        if m:
            _fill(row, w_col, m.group(1))
            _fill(row, l_col, m.group(2))

    # UFC record from promotion breakdown
    m = re.search(r"UFC\s+(\d+)\s+win\s+(\d+)\s+loss\s+(\d+)\s+draw(?:\s+(\d+)\s+no contest)?", full, re.I)
    if m:
        _fill(row, "record_ufc_wins",   m.group(1))
        _fill(row, "record_ufc_losses", m.group(2))
        _fill(row, "record_ufc_draws",  m.group(3))

    # photo : og:image
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        _fill(row, "photo_url", og["content"])
    else:
        # Image in the carousel: Tapology uses images.tapology.com/letterbox_images
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "letterbox_images" in src or "/headshots/" in src:
                _fill(row, "photo_url", src)
                break

    # fight history from tables
    parse_tapology_fight_history(soup, row)


# Mapping of the Tapology method category -> label compatible with parse_fight_method()
_TAPO_CAT = {
    "ko": "KO", "tko": "TKO", "ko/tko": "KO/TKO",
    "sub": "Submission", "submission": "Submission",
    "dec": "Decision", "decision": "Decision",
    "draw": "Draw", "nc": "No Contest",
}
_TAPO_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Tapology sport codes that are NOT classic MMA.
# Tapology tags each bout with a badge: MMA / QMMA / KB / BOX / MT / WR / BJJ...
# We keep ONLY "MMA". Everything else (kickboxing, boxing, muay thai, pure grappling,
# quintet...) is excluded.  If no badge is detected -> we keep it for safety (no false
# positive that would empty the history).
_NON_MMA_SPORT_CODES = {
    "QMMA", "KB", "BOX", "MT", "WR", "BJJ", "JD", "SM", "SAM",
    "KICKBOXING", "BOXING", "MUAY THAI", "GRAPPLING", "WRESTLING",
    "SAMBO", "JUDO", "KARATE",
}

# Backup: striking event names if the badge is missing (2nd safety layer)
_NON_MMA_EVENT_MARKERS = (
    "kickboxing", "muay thai", "muaythai", "glory", "wgp",
    "kunlun", "wako", "superkombat", "it's showtime", "k-1", "enfusion",
    "shootboxing", "wu lin feng", "lion fight", "quintet",
)


def _tapology_sport_code(r) -> str:
    """
    Extract the sport code from a Tapology <div class="result">.
    Return "MMA", "QMMA", "KB", "BOX", "MT"... or "" if not detected.
    """
    # Look for an element with 'sport' in its class or data-attribute
    for el in r.find_all(True):
        cls = " ".join(el.get("class") or []).lower()
        if "sport" in cls or "discipline" in cls:
            code = el.get_text(strip=True).upper()
            if code:
                return code
    # Text fallback: look for a short all-caps token at the end of the div
    txt = r.get_text(" ", strip=True)
    # Tapology sport codes are short uppercase words (2-8 chars)
    # separated, e.g. "... Dec 12 QMMA" or "... R2 MMA"
    m = re.search(r'\b(MMA|QMMA|KB|BOX|MT|WR|BJJ|JD|SM|SAM|K1|K-1)\b', txt)
    if m:
        return m.group(1).upper()
    return ""


def _is_non_mma_event(event_name: str) -> bool:
    """Backup: True if the event name is a known striking promotion."""
    if not event_name:
        return False
    low = event_name.lower()
    return any(m in low for m in _NON_MMA_EVENT_MARKERS)


def parse_tapology_fight_history(soup, row):
    """
    Parse the Tapology fight history.

    IMPORTANT: Tapology does NOT render the fights in <table>/<tr> but in
    <div class="result"> (rendered through Stimulus/Turbo). The old parser looked
    for <tr> and therefore always returned 0 fights -> empty fight_history.

    Each <div class="result"> contains:
      - 1st token = result (W / L / D / NC ; 'C' = Cancelled -> ignored)
      - /fightcenter/fighters/ link = opponent name (clean, for database matching)
      - 1st "X-Y" token = OPPONENT RECORD at the time of the fight (valuable for ratings)
      - /fightcenter/events/ link = event name + date (YYYY Mon DD)
      - /fightcenter/bouts/  link = detailed method ("Triangle Choke · 1:40 · R1")

    Produces fights in the format expected downstream:
      - 'date' field = method, 'event' = event name
      - explicit ISO event_date, opponent_record
    """
    # PRO MMA ONLY.
    # Tapology HTML (2026): each fight is an outer div
    #   <div data-fighter-bout-target="bout" data-status="win|loss|draw|no contest|cancelled"
    #        data-division="pro|amateur" data-sport="mma|grappling|boxing|kickboxing|...">
    # containing a <div class="result">. We iterate over the CONTAINERS (not the .result)
    # to keep access to the data-* attributes which are the ONLY reliable source:
    #   - data-status  -> result (W/L/D/NC) ; 'cancelled' -> skip
    #   - data-division -> 'amateur' -> skip
    #   - data-sport    -> != 'mma' -> skip (grappling, boxe, kickboxing...)
    bout_containers = soup.find_all(attrs={"data-fighter-bout-target": "bout"})
    if not bout_containers:
        # Fallback to the old structure (no more data-attributes): we wrap the .result
        bout_containers = soup.find_all("div", class_="result")

    # Tapology data-status -> result letter. 'cancelled' absent = we skip.
    _STATUS_MAP = {"win": "W", "loss": "L", "draw": "D",
                   "no contest": "NC", "no_contest": "NC", "nc": "NC"}

    fights = []
    skipped_div = skipped_sport = skipped_cancel = skipped_exh = 0
    for bc in bout_containers:
        # bc can be the outer container OR directly a .result (fallback)
        rd = bc.find("div", class_="result") if bc.get("data-fighter-bout-target") else bc
        if rd is None:
            rd = bc
        txt = rd.get_text(" ", strip=True)
        if not txt:
            continue

        # --- FILTRES data-* (fiables) ---
        division = (bc.get("data-division") or "pro").lower()
        if division == "amateur":
            skipped_div += 1
            continue
        status_raw = (bc.get("data-status") or "").lower().strip()
        if status_raw == "cancelled":
            skipped_cancel += 1
            continue
        sport_attr = (bc.get("data-sport") or "").lower().strip()
        if sport_attr and sport_attr != "mma":
            skipped_sport += 1
            continue  # grappling, boxing, kickboxing, qmma, muay thai...

        # Exhibition fights (TUF Season, invitational...): Tapology lists them
        # but does NOT count them in the official record. They are recognized by the fact
        # that the data-type="exhibition" attribute is present OR that the block text
        # contains "Exhibition MMA" in place of the opponent record.
        if (bc.get("data-type", "").lower() == "exhibition"
                or "exhibition mma" in txt.lower()):
            skipped_exh += 1
            continue

        tokens = txt.split()

        # --- RESULT: data-status first (reliable), otherwise the 1st text token ---
        result_letter = _STATUS_MAP.get(status_raw, "")
        if not result_letter:
            tok0 = tokens[0].upper() if tokens else ""
            if tok0 in ("W", "L", "D", "NC"):
                result_letter = tok0
            else:
                continue  # impossible to determine the result -> skip

        # --- OPPONENT: profile link first, otherwise the name block text (BUG A fix) ---
        # Obscure opponents (old regional fights) have no Tapology
        # page: before, we dropped the fight -> incomplete record (Albazi 15-3 instead
        # of 17-3). We then get the name as raw text.
        opp_a = rd.find("a", href=re.compile(r"/fightcenter/fighters/"))
        opponent = opp_a.get_text(" ", strip=True) if opp_a else ""
        opp_tap_url = ("https://www.tapology.com" + opp_a["href"].split("?")[0]) if opp_a and opp_a.get("href") else ""
        if not opponent:
            # The name is in the 1st "font-bold text-neutral-800" div of the row.
            name_div = rd.find("div", class_=re.compile(r"font-bold"))
            if name_div:
                cand = name_div.get_text(" ", strip=True)
                # We avoid capturing an "X-Y" record or a lone number.
                if cand and not re.fullmatch(r"[\d\-]+", cand):
                    opponent = cand
        if not opponent:
            continue  # really nothing usable

        # --- EVENEMENT + DATE (liens event) ---
        event_name = ""
        event_date_iso = ""
        for ev in rd.find_all("a", href=re.compile(r"/fightcenter/events/")):
            t = ev.get_text(" ", strip=True)
            m_d = re.match(r"(\d{4})\s*([A-Za-z]{3,4})\s*(\d{1,2})", t)
            if m_d:
                mo = _TAPO_MONTHS.get(m_d.group(2)[:3].lower())
                if mo:
                    event_date_iso = f"{m_d.group(1)}-{mo:02d}-{int(m_d.group(3)):02d}"
            elif not event_name and not re.match(r"^\d{4}\b", t):
                event_name = t

        # Anti-non-MMA layer 2: if data-sport is absent (fallback), we look at the event.
        if not sport_attr and _is_non_mma_event(event_name):
            skipped_sport += 1
            continue

        # Fallback date in the text if there is no date link
        if not event_date_iso:
            m_t = re.search(r"\b(\d{4})\s+([A-Za-z]{3,4})\s+(\d{1,2})\b", txt)
            if m_t:
                mo = _TAPO_MONTHS.get(m_t.group(2)[:3].lower())
                if mo:
                    event_date_iso = f"{m_t.group(1)}-{mo:02d}-{int(m_t.group(3)):02d}"

        # --- METHODE : categorie (2e token) + technique/round (lien bout) ---
        bout_a = rd.find("a", href=re.compile(r"/fightcenter/bouts/"))
        detail_txt = bout_a.get_text(" ", strip=True) if bout_a else ""
        cat_raw = tokens[1].lower() if len(tokens) > 1 else ""
        cat = _TAPO_CAT.get(cat_raw, "")
        technique = ""
        rnd = ""
        if detail_txt and "·" in detail_txt:
            technique = detail_txt.split("·")[0].strip()
            m_r = re.search(r"R(\d+)", detail_txt)
            if m_r:
                rnd = m_r.group(1)
        elif detail_txt and detail_txt.lower() not in ("win", "loss", "draw", "custom rules"):
            technique = detail_txt

        date_field = cat or ""
        if cat and technique and technique.lower() not in ("custom rules", "win", "loss", ""):
            date_field = f"{cat} ({technique})"
        if rnd:
            date_field = f"{date_field} Round {rnd}".strip()
        if not date_field:
            date_field = detail_txt

        # --- OPPONENT RECORD (BUG B fix) ---
        # The row contains TWO records with distinct title= attributes:
        # "Fighter Record Before Fight" (the fighter themself) and
        # "Opponent Record Before Fight" (the opponent). Before, we took the 1st
        # "X-Y" of the text = the FIGHTER's -> wrong opponent_record.
        opp_record = ""
        opp_span = rd.find("span", title=re.compile(r"Opponent Record", re.I))
        if opp_span:
            m_or = re.search(r"\b(\d+-\d+)(?:-\d+)?\b", opp_span.get_text(strip=True))
            if m_or:
                opp_record = m_or.group(1)
        if not opp_record:
            # Fallback: 2nd record of the text (the 1st = fighter, the 2nd = opponent)
            rec_tokens = re.findall(r"\b(\d+-\d+)(?:-\d+)?\b", txt)
            if len(rec_tokens) >= 2:
                opp_record = rec_tokens[1]
            elif rec_tokens:
                opp_record = rec_tokens[0]

        entry = {
            "result":          result_letter,
            "opponent":        opponent,
            "opponent_record": opp_record,
            "event":           event_name,
            "event_date":      event_date_iso,
            "date":            date_field,
        }
        if opp_tap_url:
            entry["opponent_tapology_url"] = opp_tap_url
        fights.append(entry)

    LOG.debug(
        f"Tapology fight_history : {len(bout_containers)} bouts -> {len(fights)} pro MMA "
        f"(skip amateur={skipped_div}, sport={skipped_sport}, "
        f"cancelled={skipped_cancel}, exhibition={skipped_exh})"
    )

    if len(fights) >= 2:
        _set_fight_history(row, fights, "tapology")


# underground (fighters.mixedmartialarts.com)

def parse_underground_html(html: str, row: dict, url: str):
    soup = BeautifulSoup(html, "lxml")
    full = soup.get_text(" ", strip=True)
    full = re.sub(r"\s+", " ", full)

    # NAME: h1 or title
    # The Underground h1 can contain the nickname stuck to the name:
    # "Manel Kape “Prodigio”"   (nickname at the end, smart quotes)
    # "Manel “Starboy” Kape"    (nickname in the middle)
    # We extract the nickname so as not to pollute the name (used for multi-source matching).
    h1 = soup.find("h1")
    if h1:
        nm = h1.get_text(" ", strip=True)
        nm = re.sub(r"\s+", " ", nm).strip()
        if nm and 2 < len(nm) < 80 and not row.get("name"):
            # Case 1: nickname in the middle "First 'Nick' Last"
            m_mid = re.match(r'(.+?)\s+["“‘](.+?)["”’]\s+(.+)', nm)
            # Case 2: nickname at the end "First Last 'Nick'"
            m_end = re.match(r'(.+?)\s+["“‘](.+?)["”’]\s*$', nm)
            if m_mid:
                _fill(row, "name", f"{m_mid.group(1)} {m_mid.group(3)}".strip())
                _fill(row, "nickname", m_mid.group(2).strip())
            elif m_end:
                _fill(row, "name", m_end.group(1).strip())
                _fill(row, "nickname", m_end.group(2).strip())
            else:
                _fill(row, "name", nm)

    # record breakdown (KO/TKO SUB DEC for wins/losses)
    # Strategy 1: regex on text
    m = re.search(
        r"Record\s*BreakDown[^A-Z]*?KO/TKO\s+SUB\s+DEC\s+Wins?\s+(\d+)\s+(\d+)\s+(\d+)\s+Losses\s+(\d+)\s+(\d+)\s+(\d+)",
        full, re.I,
    )
    if m:
        _fill(row, "wins_by_ko_tko",       m.group(1))
        _fill(row, "wins_by_submission",   m.group(2))
        _fill(row, "wins_by_decision",     m.group(3))
        _fill(row, "losses_by_ko_tko",     m.group(4))
        _fill(row, "losses_by_submission", m.group(5))
        _fill(row, "losses_by_decision",   m.group(6))

    # Strategy 2: table scan (fallback if the HTML differs)
    for tbl in soup.find_all("table"):
        ttxt = tbl.get_text(" ", strip=True)
        if not all(k in ttxt for k in ("KO/TKO", "SUB", "DEC")):
            continue
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 4 and re.match(r"wins?$", cells[0], re.I):
                _fill(row, "wins_by_ko_tko",     cells[1])
                _fill(row, "wins_by_submission", cells[2])
                _fill(row, "wins_by_decision",   cells[3])
            elif len(cells) >= 4 and re.match(r"losses$", cells[0], re.I):
                _fill(row, "losses_by_ko_tko",     cells[1])
                _fill(row, "losses_by_submission", cells[2])
                _fill(row, "losses_by_decision",   cells[3])

    # Finish Rate
    m = re.search(r"Finish Rate\s+([\d.]+)\s*%?", full, re.I)
    if m: _fill(row, "finish_rate_int", str(int(float(m.group(1)))))

    # Current Streak
    m = re.search(r"Current Streak\s+(\d+)\s+(Wins?|Losses?)", full, re.I)
    if m:
        ch = "W" if "Win" in m.group(2) else "L"
        row["current_streak"] = f"{m.group(1)}{ch}"

    # Pro Record (with method breakdown)
    m = re.search(r"Pro Record\s+(\d+)-(\d+)-(\d+)", full)
    if m:
        _fill(row, "record_total_wins",   m.group(1))
        _fill(row, "record_total_losses", m.group(2))
        _fill(row, "record_total_draws",  m.group(3))

    # Height / Age
    m = re.search(r"Height\s+(\d+)\s*['’]\s*(\d+)", full)
    if m: _fill(row, "height_inches", f"{m.group(1)}'{m.group(2)}\"")
    # Age: 2-3 digits (15-99). The old regex `\d+` could match a "1"
    # in a stray sentence (e.g. "Win Streak 1"), causing age=1.
    m = re.search(r"\bAge\b\s+(\d{2,3})\b", full)
    if m: _fill(row, "age", m.group(1))

    # Career length, debut
    m = re.search(r"Career Length\s+([\d.]+\s+\w+)", full, re.I)
    # info text, not critical

    # design decision: photo_url comes exclusively from tapology.
    # Underground no longer touches this column.

    # Fight history (Underground table)
    fights = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3: continue
        first = tds[0].get_text(" ", strip=True).upper()
        m = re.match(r"^(W|L|D|NC|WIN|LOSS|DRAW)\b", first)
        if not m: continue
        cells = [td.get_text(" ", strip=True) for td in tds]
        fights.append({
            "result": m.group(1)[0],
            "raw":    " | ".join(c for c in cells[1:] if c),
        })
    if len(fights) >= 2:
        _set_fight_history(row, fights, "underground")


# UFC bio (4th source: the stance + the first_sport)

_SPORT_PATTERNS = [
    (r"\b(?:lutte(?:ur)?|wrestl(?:ing|er)|grappling)\b",      "Wrestling"),
    (r"\b(?:boxe(?:ur)?|boxing|boxer)\b",                     "Boxing"),
    (r"\bjudo(?:ka)?\b",                                       "Judo"),
    (r"\bkickbox(?:ing|eur)?\b",                              "Kickboxing"),
    (r"\bkarat[eé](?:ka)?\b",                                 "Karate"),
    (r"\btaekwondo\b",                                         "Taekwondo"),
    (r"\bmuay.?thai\b",                                        "Muay Thai"),
    (r"\bsambo\b",                                             "Sambo"),
    (r"\bjiu.?jitsu|bjj\b",                                    "BJJ"),
    (r"\bfootball américain|american football\b",              "Football"),
    (r"\bbasketball\b",                                        "Basketball"),
    (r"\bnatation|swimming\b",                                 "Swimming"),
    (r"\bfootball\b",                                          "Soccer"),
]

def _detect_sport_str(text: str) -> str:
    for pattern, sport in _SPORT_PATTERNS:
        if re.search(pattern, text, re.I):
            return sport
    return ""

def _detect_first_sport(text: str, row: dict):
    sport = _detect_sport_str(text)
    if sport:
        _fill(row, "first_sport", sport)


_STANCE_MAP = {
    "Orthodox": "Orthodox", "Southpaw": "Southpaw", "Switch": "Switch", "Open Stance": "Open Stance",
    "Orthodoxe": "Orthodox", "Gauchère": "Southpaw", "Gauchere": "Southpaw",
    "Gaucher": "Southpaw", "Sud": "Southpaw", "Ambidextre": "Switch",
}

def parse_ufc_html(html: str, row: dict, url: str):
    soup = BeautifulSoup(html, "lxml")
    full = soup.get_text(" ", strip=True)
    full = re.sub(r"\s+", " ", full)

    # NAME from h1
    h1 = soup.find("h1")
    if h1:
        nm = h1.get_text(" ", strip=True)
        nm = re.split(r"\s+\d+-\d+", nm)[0].strip()
        if nm and 2 < len(nm) < 80:
            _fill(row, "name", nm)

    # first sport from the bio text (EN + FR keywords)
    if not row.get("first_sport"):
        _detect_first_sport(full, row)

    # nationality if available
    m = re.search(r"(?:Nationality|Nationalit[eé])\s*:?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ \-']{2,30})", full)
    if m:
        _fill(row, "nationality", m.group(1).strip().rstrip(",.|"))

    # design decision: photo_url comes exclusively from tapology.
    # UFC.com no longer touches this column.


# UFC-FR.com (ufc-fr.com/combattant-XXXX.html)
# Extracts: name/nickname, nationality, birth date, height, reach,
# weight class, UFC record, original sport,
# fighting STYLE (Grappler / Striker / Grinder / etc.)

_FR_WEIGHT_CLASS = {
    "poids paille":    "Strawweight",
    "poids mouches":   "Flyweight",
    "poids coq":       "Bantamweight",
    "poids plumes":    "Featherweight",
    "poids légers":    "Lightweight",
    "poids léger":     "Lightweight",
    "poids mi-moyens": "Welterweight",
    "poids mi-moyen":  "Welterweight",
    "poids moyens":    "Middleweight",
    "poids moyen":     "Middleweight",
    "mi-lourds":       "Light Heavyweight",
    "demi-lourds":     "Light Heavyweight",
    "poids lourds":    "Heavyweight",
    "poids lourd":     "Heavyweight",
    "super-lourds":    "Super Heavyweight",
}

_FR_MONTHS = {
    "janvier": "01", "fevrier": "02", "février": "02",
    "mars": "03", "avril": "04", "mai": "05", "juin": "06",
    "juillet": "07", "aout": "08", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11",
    "decembre": "12", "décembre": "12",
}

def _parse_fr_date(s: str) -> str:
    """'29 septembre 1988' -> '1988-09-29'"""
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", s.strip(), re.I)
    if not m:
        return ""
    day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
    mo = _FR_MONTHS.get(month_str, "")
    if not mo:
        return ""
    return f"{year}-{mo}-{day.zfill(2)}"


def _cm_to_height_str(cm: float) -> str:
    total_in = cm / 2.54
    feet     = int(total_in // 12)
    inches   = round(total_in % 12)
    if inches == 12:
        feet += 1
        inches = 0
    return f"{feet}'{inches}\""


def parse_ufc_fr_html(html: str, row: dict, url: str):
    soup = BeautifulSoup(html, "lxml")

    # Design decision:
    # - photo_url           comes EXCLUSIVELY from Tapology (the good profile photo).
    #   - photo_thumbnail_url comes EXCLUSIVELY from UFC-FR (the official round avatar).
    # UFC-FR therefore does NOT touch photo_url, only photo_thumbnail_url.
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        _fill(row, "photo_thumbnail_url", og_img["content"])

    # stance from the UFC-FR kpi-cards (label "style" or "garde").
    # design decision (May 2026): UFC-FR takes priority over tapology for this field.
    # The stored value is either:
    # - a fighting style (Striker, Grinder, Finisher, Polyvalent, Frappeur, Lutteur...)
    # - a real stance (Orthodox, Southpaw, Switch) - rare on UFC-FR
    # If the value matches an entry of _STANCE_MAP (including the FR variants
    # "Gauchère"/"Orthodoxe"), we normalize to the canonical EN version.
    # otherwise we keep the raw value. authoritative_sources["stance"] includes "ufc_fr"
    # so this value OVERWRITES Tapology's in the upsert.
    # warning: use row["stance"] = (not _fill) to overwrite a possible value
    # set by Tapology in the same scrape batch.
    for kpi in soup.find_all(class_="kpi-title"):
        if kpi.get_text(strip=True) in ("STYLE", "GARDE"):
            nxt = kpi.find_next_sibling()
            if nxt:
                style_val = nxt.get_text(strip=True)
                if style_val and len(style_val) < 25:
                    _sn = next(
                        (v for k, v in _STANCE_MAP.items() if k.lower() == style_val.lower()),
                        "",
                    )
                    row["stance"] = _sn if _sn else style_val
            break

    # UFC RANK / champion / P4P from UFC-FR.
    # The ranks are <a href="..."> links whose URL identifies the type of ranking:
    # - Official division rank: href="classement-homme-poids-XXX.html" (NOT "-fans" at the end)
    #   - P4P                    : href="classement-pound-for-pound.html"
    # - TO IGNORE: any link that contains "fans" or "amateur" or "national"
    # (e.g. "Légers (Fans) #2" -> classement-homme-poids-legers-fans.html)
    # Champion: the division link contains <img src="...belt...">
    # OR the text shows "Champion".
    # Convention: ufc_official_rank = 0 for the champion (instead of NULL/"")
    # -> allows ORDER BY rank ASC without dropping the champions.
    # The source of truth remains is_ufc_champion (TRUE/FALSE).
    champion_detected = False
    rank_detected = None
    is_female_division = False   # detecte via classement-femme-... href
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").lower()
        link_text = a.get_text(" ", strip=True)

        # Exclude anything that looks like a Fans/amateur/national ranking
        if "fans" in href or "amateur" in href or "national" in href:
            continue

        # Official division rank (male or female)
        if re.search(r"classement-(?:homme|femme)-poids-[a-z\-]+\.html$", href, re.I):
            belt = a.find("img", src=re.compile(r"belt", re.I))
            mm = re.search(r"#\s*(\d+)", link_text)
            # warning: a ranking link only identifies the fighter if it carries
            # their real rank (belt badge OR "#N"). The navigation MENU links
            # list every division (male AND female) without a rank -> to be ignored
            # for gender detection, otherwise ALL the fighters would be marked
            # femme (bug : Alistair Overeem -> "Women's Heavyweight").
            is_fighter_rank_link = bool(belt) or bool(mm and int(mm.group(1)) <= 20)

            if is_fighter_rank_link and re.search(r"classement-femme-", href, re.I):
                is_female_division = True

            if belt:
                champion_detected = True
            elif mm and int(mm.group(1)) <= 20:
                # We keep the FIRST rank value found (the UFC tile at the top of the page).
                # Later ones may be links in side cards.
                if rank_detected is None:
                    rank_detected = mm.group(1)
                if re.search(r"\bchampion\b", link_text, re.I):
                    champion_detected = True

        # P4P : "classement-pound-for-pound.html"
        elif re.search(r"classement-pound-for-pound", href, re.I):
            mm = re.search(r"#\s*(\d+)", link_text)
            if mm:
                row["ufc_p4p_rank"] = mm.group(1)

    # Convention : ufc_official_rank=0 signifie systematiquement champion.
    # If UFC-FR shows a "#0" in the link text (champion without a belt badge),
    # we must force is_ufc_champion=TRUE to stay consistent with the convention.
    if champion_detected or rank_detected == "0":
        row["is_ufc_champion"] = "TRUE"
        row["ufc_official_rank"] = "0"
    elif rank_detected is not None:
        row["ufc_official_rank"] = rank_detected

    # Gender: "classement-femme-" signal detected in the rank links.
    # UFC-FR does not mention "Women" in the category names (og:description
    # contains "Poids Coqs" for women's Bantamweight). We therefore rely on the URL
    # of the ranking associated with the profile.
    if is_female_division:
        row["gender"] = "F"

    # Compat: old tile-value structure (just in case).
    for tile in soup.find_all(class_="tile-value"):
        tile_text = tile.get_text(strip=True)
        if re.match(r"^UFC\b", tile_text, re.I):
            belt = tile.find("img", src=re.compile(r"belt", re.I))
            if belt:
                row["is_ufc_champion"] = "TRUE"
                row["ufc_official_rank"] = "0"
            elif not row.get("ufc_official_rank"):
                mm = re.search(r"#(\d+)", tile_text)
                if mm and int(mm.group(1)) <= 20:
                    row["ufc_official_rank"] = mm.group(1)
        elif re.match(r"^P4P\b", tile_text, re.I) and not row.get("ufc_p4p_rank"):
            mm = re.search(r"#(\d+)", tile_text)
            if mm:
                row["ufc_p4p_rank"] = mm.group(1)

    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if not og_desc:
        LOG.warning("  ufc-fr.com: og:description not found - unexpected structure")
        return
    desc = og_desc.get("content", "")
    if not desc:
        return

    # Name + nickname  ("Volkanovski "The Great"" or "Volkanovski")
    m = re.search(r'Combattant\s*:\s*([^,]+)', desc)
    if m:
        raw = m.group(1).strip()
        # Handles every type of quote (ASCII, smart quotes, etc.)
        nm = re.match(r'(.+?)\s+["“”«](.+?)["“”»]', raw)
        if nm:
            _fill(row, "name",     nm.group(1).strip())
            _fill(row, "nickname", nm.group(2).strip())
        else:
            # Remove the quotes at the end of the string if it is "Name "Nickname""
            clean = re.sub(r'\s*["“”«].*["“”»]\s*$', '', raw).strip()
            _fill(row, "name", clean if clean else raw)

    # Nationality (stored in French if the other sources have not already filled it)
    m = re.search(r'Pays\s*:\s*([^,]+)', desc)
    if m:
        pays = m.group(1).strip()
        # Translate the common countries FR->EN for consistency with the other sources
        _FR_COUNTRIES = {
            "Australie": "Australia", "États-Unis": "United States",
            "Etats-Unis": "United States", "Canada": "Canada",
            "Brésil": "Brazil", "Bresil": "Brazil",
            "Russie": "Russia", "France": "France",
            "Japon": "Japan", "Royaume-Uni": "United Kingdom",
            "Angleterre": "United Kingdom", "Ecosse": "United Kingdom",
            "Écosse": "United Kingdom", "Pays de Galles": "United Kingdom",
            "Irlande du Nord": "United Kingdom",
            "Irlande": "Ireland", "Mexique": "Mexico",
            "Nouvelle-Zélande": "New Zealand", "Pologne": "Poland",
            "Géorgie": "Georgia", "Kazakhstan": "Kazakhstan",
            "Pays-Bas": "Netherlands", "Suède": "Sweden",
            "Danemark": "Denmark", "Norvège": "Norway",
            "Nigeria": "Nigeria", "Brésilien": "Brazil",
            "Chine": "China", "Corée du Sud": "South Korea",
            "Allemagne": "Germany", "Italie": "Italy",
            "Espagne": "Spain", "Portugal": "Portugal",
            "Argentine": "Argentina", "Colombie": "Colombia",
            "Equateur": "Ecuador", "Équateur": "Ecuador",
            "Pérou": "Peru", "Perou": "Peru", "Chili": "Chile",
            "Venezuela": "Venezuela", "Cuba": "Cuba",
            "République Tchèque": "Czech Republic",
            "Republique Tcheque": "Czech Republic",
            "Croatie": "Croatia", "Slovaquie": "Slovakia",
            "Hongrie": "Hungary", "Roumanie": "Romania",
            "Ukraine": "Ukraine", "Belarus": "Belarus",
            "Bielorussie": "Belarus", "Biélorussie": "Belarus",
            "Bulgarie": "Bulgaria", "Serbie": "Serbia",
            "Bosnie": "Bosnia and Herzegovina",
            "Daghestan": "Russia", "Daguestan": "Russia",
            "Tchétchénie": "Russia", "Tchetchenie": "Russia",
            "Tunisie": "Tunisia", "Algerie": "Algeria",
            "Algérie": "Algeria", "Maroc": "Morocco",
            "Cameroun": "Cameroon", "Sénégal": "Senegal",
            "Senegal": "Senegal", "Afrique du Sud": "South Africa",
            "Iran": "Iran", "Irak": "Iraq",
            "Turquie": "Turkey", "Armenie": "Armenia", "Arménie": "Armenia",
            "Azerbaidjan": "Azerbaijan", "Azerbaïdjan": "Azerbaijan",
            "Suisse": "Switzerland", "Autriche": "Austria",
            "Belgique": "Belgium", "Luxembourg": "Luxembourg",
            "Islande": "Iceland", "Finlande": "Finland",
            "Thailande": "Thailand", "Thaïlande": "Thailand",
            "Vietnam": "Vietnam", "Inde": "India",
            "Philippines": "Philippines", "Indonesie": "Indonesia",
            "Indonésie": "Indonesia",
        }
        # UFC-FR is authoritative for nationality (the country under which they fight).
        # Overwrites Tapology's "Born: <country>" which gives the birthplace.
        # E.g. Topuria fights under the Georgian flag, but Tapology says "Born: Halle, Germany".
        row["nationality"] = _FR_COUNTRIES.get(pays, pays)

    # Date of birth ("29 septembre 1988")
    m = re.search(r'Naissance\s*:\s*([^,]+)', desc)
    if m:
        dob = _parse_fr_date(m.group(1).strip())
        if dob:
            _fill(row, "date_of_birth", dob)

    # Height in cm -> feet'inches" format
    m = re.search(r'Hauteur\s*:\s*([\d.]+)\s*cm', desc)
    if m:
        _fill(row, "height_inches", _cm_to_height_str(float(m.group(1))))

    # Reach in cm -> decimal inches
    m = re.search(r'allonge\s*:\s*([\d.]+)\s*cm', desc, re.I)
    if m:
        reach_in = round(float(m.group(1)) / 2.54, 1)
        _fill(row, "reach_inches", f'{reach_in}"')

    # category (FR -> EN)
    m = re.search(r'Cat[eé]gorie\s*:\s*([^,]+)', desc)
    if m:
        wc_en = _FR_WEIGHT_CLASS.get(m.group(1).strip().lower(), "")
        if wc_en:
            # If a women's division is detected via the classement-femme-... href,
            # prepend "Women's " to stay consistent with the database convention.
            if is_female_division and not wc_en.lower().startswith("women"):
                wc_en = "Women's " + wc_en
            _fill(row, "weight_class_current", wc_en)

    # UFC record (only if FightMatrix has not already filled it)
    m_v  = re.search(r'Victoire\s*:\s*(\d+)',    desc)
    m_d  = re.search(r'D[eé]faite\s*:\s*(\d+)',  desc)
    m_n  = re.search(r'Nul\s*:\s*(\d+)',          desc)
    m_nc = re.search(r'No Contest\s*:\s*(\d+)',   desc)
    if m_v:  _fill(row, "record_ufc_wins",   m_v.group(1))
    if m_d:  _fill(row, "record_ufc_losses", m_d.group(1))
    if m_n:  _fill(row, "record_ufc_draws",  m_n.group(1))
    if m_nc and m_nc.group(1) != "0":
        _fill(row, "record_total_nc", m_nc.group(1))

    # Arts martiaux → first_sport
    # BJJ is universal in MMA and often listed first on UFC-FR even if it is
    # a boxer or a wrestler - we skip it on the first pass to avoid false positives.
    m = re.search(r'Arts martiaux\s*:(.+)', desc, re.I)
    if m:
        arts_text = m.group(1).strip()
        items = [re.sub(r'\s*\([^)]+\)', '', it).strip()
                 for it in re.split(r',\s*(?!\()', arts_text)]
        # Pass 1 : premier sport non-BJJ
        for item in items:
            sport = _detect_sport_str(item)
            if sport and sport != "BJJ":
                _fill(row, "first_sport", sport)
                break
        # Pass 2: accept BJJ if no other sport was detected
        if not row.get("first_sport"):
            for item in items:
                sport = _detect_sport_str(item)
                if sport:
                    _fill(row, "first_sport", sport)
                    break
        if not row.get("first_sport"):
            _detect_first_sport(arts_text, row)

    # Strengths ("Points forts") -> notes (UFC-FR is the only source allowed for this field)
    # Structure reelle UFC-FR (decouverte 2026-05) :
    #   <div class="data-row">
    #     <div class="data-label">Points forts</div>
    #     <div class="data-value">Combattante polyvalente, ...</div>
    #   </div>
    # The old code looked in h2/h3/h4/strong/dt/span -> NEVER matched
    # this structure -> empty notes for every UFC-FR fighter.
    pf_text = ""
    # Priorite 1 : pattern data-label / data-value (structure reelle UFC-FR)
    for label in soup.find_all("div", class_="data-label"):
        if re.match(r"Points?\s+forts?", label.get_text(strip=True), re.I):
            value = label.find_next_sibling("div", class_="data-value")
            if not value:
                # fallback : n'importe quel sibling div/p
                value = label.find_next_sibling(["div", "p"])
            if value:
                pf_text = value.get_text(" ", strip=True)
            break
    # Priority 2: other tags (in case UFC-FR changes again)
    if not pf_text:
        for el in soup.find_all(["h2", "h3", "h4", "strong", "dt", "span"]):
            el_txt = el.get_text(strip=True)
            if re.match(r"Points?\s+forts?", el_txt, re.I) and len(el_txt) < 25:
                nxt = el.find_next_sibling(["p", "div", "ul"])
                if not nxt and el.parent:
                    nxt = el.parent.find_next_sibling(["p", "div"])
                if nxt:
                    pf_text = nxt.get_text(" ", strip=True)
                break
    # Priorite 3 : og:description (souvent tronquee, fallback faible)
    if not pf_text and desc:
        m = re.search(r'Points?\s+forts?\s*:\s*(.+?)(?:\n|$)', desc, re.I | re.S)
        if m:
            pf_text = m.group(1).strip()
    if pf_text and 10 < len(pf_text) < 1200:
        row["notes"] = pf_text


# fight history: we keep the longest version

# Source hierarchy (design decision).
# Order: Tapology > UFC Stats / event_scrape > FightMatrix > Underground.
# Tapology is authoritative (the most up to date, covers the whole career). UFC Stats and the
# event scrapes are the OFFICIAL source of UFC results -> very reliable,
# above FightMatrix (which sometimes counts amateur fights / is stale).
# Used to arbitrate fight_history AND record_total everywhere (scrape + recompute).
SOURCE_PRIORITY = {
    "tapology":        4,
    "ufcstats+scrape": 3,
    "event_scrape":    3,
    "ufc_fr":          2,
    "ufc":             2,
    "fightmatrix":     2,
    "underground":     1,
    "unknown":         0,
}
# A fight_history whose source has a priority >= this threshold IS AUTHORITATIVE on the
# record_total column (we overwrite, even if the history has FEWER fights).
# Below (fightmatrix, underground) -> "MAX" safety net (we keep the most complete one).
RECORD_AUTHORITATIVE_MIN = 3


def _set_fight_history(row, fights, source):
    payload = {"source": source, "count": len(fights), "fights": fights}
    s = json.dumps(payload, ensure_ascii=False)
    cur = row.get("fight_history", "")
    if not cur:
        row["fight_history"] = s
        return
    # Arbitration by SOURCE HIERARCHY first (Tapology > UFC-FR=FM > UG),
    # then by number of fights in case of a tie. Without it, an FM that lists more
    # rows (amateurs included) overwrote the Tapology history which is nonetheless reliable.
    new_prio = SOURCE_PRIORITY.get(source, 0)
    try:
        old = json.loads(cur)
        old_prio = SOURCE_PRIORITY.get(old.get("source", ""), 0)
        if new_prio < old_prio:
            return                                   # less reliable source -> we keep the existing one
        if new_prio == old_prio and old.get("count", 0) >= len(fights):
            return                                   # tie -> we keep the most complete one
    except Exception:
        if len(cur) > len(s):
            return
    row["fight_history"] = s


# the scrape (orchestrates the 3 sources)

def _compute_style_from_wins(row: dict) -> str:
    """
    Deduce the fighting style from the win methods.
    Categories: Striker, Grappler, Wrestler, Grinder, Finisher.
    Requires at least 3 known fights (ko+sub+dec >= 3).
    """
    try:
        ko  = int(row.get("wins_by_ko_tko")    or 0)
        sub = int(row.get("wins_by_submission") or 0)
        dec = int(row.get("wins_by_decision")   or 0)
        total = ko + sub + dec
        if total < 3:
            return ""

        ko_r  = ko  / total
        sub_r = sub / total
        dec_r = dec / total
        fin_r = (ko + sub) / total
        first = str(row.get("first_sport", "")).lower()

        # Finisher: finishes >= 70% and uses both methods (KO and SUB)
        if fin_r >= 0.70 and ko_r >= 0.15 and sub_r >= 0.15:
            return "Finisher"

        # Striker: majority of KOs
        if ko_r >= 0.40:
            return "Striker"

        # Grappler: majority of submissions
        if sub_r >= 0.35:
            return "Grappler"

        # Wrestler: wrestling background + wins mostly on points
        if any(s in first for s in ("wrestl", "lutte", "sambo", "judo")) and dec_r >= 0.35:
            return "Wrestler"

        # Grinder: wins mostly on points, few finishes
        if dec_r >= 0.55:
            return "Grinder"

        # Fallback : methode dominante
        if ko_r >= sub_r and ko_r >= dec_r:
            return "Striker"
        if sub_r >= ko_r and sub_r >= dec_r:
            return "Grappler"
        return "Grinder"
    except (ValueError, TypeError, ZeroDivisionError):
        return ""


PARSERS = {
    "fightmatrix": parse_fightmatrix_html,
    "tapology":    parse_tapology_html,
    "underground": parse_underground_html,
    "ufc":         parse_ufc_html,
    "ufc_fr":      parse_ufc_fr_html,
}

_KEY_FIELDS = [
    "name", "stance", "first_sport", "nationality",
    "weight_class_current", "weight_class_origin",
    "current_league",
    "is_ufc_champion", "ufc_official_rank",
    "record_total_wins", "record_total_losses", "record_total_nc",
    "wins_by_ko_tko", "wins_by_submission", "wins_by_decision",
    "fightmatrix_rating_points", "fightmatrix_540_metric",
    "current_streak", "last_5_results",
    "date_of_birth", "height_inches", "reach_inches", "photo_url",
]

def _diff_row(before: dict, after: dict) -> tuple[dict, dict]:
    """Return (newly_filled_fields, overwritten_fields) between before and after."""
    nouveaux = {}
    ecrases  = {}
    for k, v in after.items():
        if v and not before.get(k, ""):
            nouveaux[k] = v
        elif v and before.get(k, "") and str(before[k]) != str(v):
            ecrases[k] = f"{before[k]!r} -> {v!r}"
    return nouveaux, ecrases


def scrape(urls: list[str]) -> dict:
    row = {c: "" for c in COLUMNS}
    fetched = []
    # Order: Tapology (rich bio) -> FM (ratings) -> UG (win methods) -> UFC (stance/sport)
    order_key = {"tapology": 0, "fightmatrix": 1, "underground": 2, "ufc": 3, "ufc_fr": 4, "unknown": 99}
    sorted_urls = sorted([u.strip() for u in urls if u and u.strip()],
                         key=lambda u: order_key.get(detect_source(u), 99))

    LOG.info(f"==> {len(sorted_urls)} URL(s) a scraper : {[detect_source(u) for u in sorted_urls]}")

    for url in sorted_urls:
        src = detect_source(url)
        if src == "unknown":
            LOG.warning(f"Unknown URL ignored: {url}")
            continue
        LOG.info(f"[{src.upper()}] {url}")
        html = fetch_html(url)
        if not html:
            LOG.error(f"  -> no HTML (fetch failure)")
            continue
        before = dict(row)
        try:
            PARSERS[src](html, row, url)
            fetched.append(url)
        except Exception as e:
            LOG.exception(f"  -> parse error {src}: {e}")

        # Diagnostic: what THIS parser contributed
        nouveaux, ecrases = _diff_row(before, row)
        nv_keys = [k for k in _KEY_FIELDS if k in nouveaux]
        ec_keys = [k for k in _KEY_FIELDS if k in ecrases]
        if nv_keys:
            apercu = ", ".join(f"{k}={str(nouveaux[k])[:25]}" for k in nv_keys[:6])
            LOG.info(f"  + filled ({len(nouveaux)}) : {apercu}{' ...' if len(nv_keys)>6 else ''}")
        if ec_keys:
            LOG.info(f"  ~ overwritten ({len(ecrases)}) : {', '.join(ec_keys)}")
        if not nouveaux and not ecrases:
            LOG.warning(f"  ! {src.upper()} CONTRIBUTED NOTHING - check the URL or the HTML structure")
        # UFC.com: check first_sport (stance computed after all the parsers)
        if src == "ufc":
            if not row.get("first_sport"):
                LOG.warning(f"  ! UFC parse OK but first_sport not found - bio without a detected sport keyword")
        if src == "ufc_fr":
            if not row.get("first_sport"):
                LOG.warning(f"  ! UFC-FR parse OK but first_sport not found - check Arts martiaux in og:description")
        time.sleep(SLEEP_BETWEEN)

    # Defaults
    _fill(row, "data_source", " | ".join(fetched))
    _fill(row, "is_active", "TRUE")
    _fill(row, "ufc_title_match_win", "0")
    _fill(row, "record_total_nc", "0")
    _fill(row, "is_verified", "TRUE")
    # NOTE: no "M" default for gender nor "0" for current_streak here.
    # Why: in batch mode each URL produces a separate row. If UFC-FR
    # detects Women -> "F" but the next FM scrape sets "M" by default,
    # always_refresh overwrites the "F" in the database. Same for current_streak: a scrape
    # without last_5 overwrote an already computed streak. The database schema already has
    # the defaults ('M' for gender, '0' for current_streak) -> clean INSERT,
    # UPDATE only if we have a real value.

    # is_ufc_champion: TRUE if FM says "C <division>" or UFC-FR says "#C" or rank=0.
    # FALSE ONLY if there is an explicit rank > 0 (ranked but not champion).
    # warning: bool("0") == True in Python -> we cannot test row.get("ufc_official_rank")
    # directly, otherwise rank="0" (champion by convention) would be treated as "has a rank != 0".
    # empty = unknown info -> database schema default FALSE on INSERT, UPDATE leaves it untouched.
    if not row.get("is_ufc_champion"):
        _rank_pre = str(row.get("ufc_official_rank", "")).strip()
        if _rank_pre and _rank_pre != "0":   # explicit rank > 0 -> definitely not champion
            row["is_ufc_champion"] = "FALSE"

    # authoritative last_fight_date from fight_history (1st entry = most recent fight)
    # Tapology can show a scheduled/cancelled fight in "Last Fight" -> we trust
    # the FM history table which only lists fights actually contested.
    if row.get("fight_history"):
        try:
            fh = json.loads(row["fight_history"])
            fights = fh.get("fights") or []
            if fights:
                event_txt = fights[0].get("event", "")
                m_d = re.search(
                    r"\b(January|February|March|April|May|June|July|August|"
                    r"September|October|November|December)\s+(\d+)(?:st|nd|rd|th)?\s+(\d{4})\b",
                    event_txt,
                )
                if m_d:
                    _MONTHS_EN = {
                        "January": 1, "February": 2, "March": 3, "April": 4,
                        "May": 5, "June": 6, "July": 7, "August": 8,
                        "September": 9, "October": 10, "November": 11, "December": 12,
                    }
                    mo = _MONTHS_EN[m_d.group(1)]
                    new_lfd = f"{m_d.group(3)}-{mo:02d}-{int(m_d.group(2)):02d}"
                    if new_lfd != row.get("last_fight_date"):
                        row["last_fight_date"] = new_lfd
                        row["days_inactive"] = ""  # force a recalc in compute_derived
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    compute_derived(row)

    # design decision: stance/style comes exclusively from UFC-FR (or UFC.com).
    # No _compute_style_from_wins fallback: we no longer guess the style from
    # the win methods to avoid polluting the database with estimated values
    # that would be mistakenly read as official before UFC-FR has been scraped.
    # The UFC-FR scraper fills `stance` through the kpi-cards (parse_ufc_fr_html).

    for c in COLUMNS:
        row.setdefault(c, "")
    return row


# upsert into the final csv (update if the name already exists, otherwise insert)

ALWAYS_REFRESH = {
    "updated_at", "last_scraped_at",
    "last_fight_date", "days_inactive", "is_active",
    "gender",  # UFC-FR detects "Women" -> must be able to overwrite the database default "M"
    # weight_class: added here to overwrite the legacy "Unknown" values coming from
    # old CSV imports. Without it, COALESCE (Supabase) and not-empty (CSV) block
    # the correction even when the scraper has a real value (e.g. "Flyweight").
    # Safety: the upsert skips empty values (v==None / v==""),
    # so a scrape without a division will NOT erase an existing correct value.
    "weight_class_current", "weight_class_origin",
    # current_league: systematically derived from the most recent fight
    # via compute_derived(). ALWAYS_REFRESH so that a fighter who changes
    # league (UFC -> PFL or the reverse) is up to date from the next
    # scrape. Safety: if compute_derived does not detect the league, it
    # sets "" and the upsert skips empty values (never an accidental erasure).
    "current_league",
    "current_streak", "last_5_results",
    "fightmatrix_rating_points", "fightmatrix_big_league_record",
    "fightmatrix_540_metric", "fightmatrix_quality_perf_pct",
    "ufc_official_rank", "ufc_p4p_rank", "is_ufc_champion",
    # ufc_title_match_win: essential in ALWAYS_REFRESH, otherwise COALESCE(0, real_value)=0
    # blocks the update when re-scraping with FM after an initial scrape without FM.
    "ufc_title_match_win",
    "record_total_wins", "record_total_losses", "record_total_draws", "record_total_nc",
    "record_ufc_wins", "record_ufc_losses", "record_ufc_draws",
    "record_other_wins", "record_other_losses", "record_other_draws",
    "wins_by_ko_tko", "wins_by_submission", "wins_by_decision",
    "losses_by_ko_tko", "losses_by_submission", "losses_by_decision",
    "split_decision_wins", "split_decision_losses",
    "total_fights", "win_percentage_int", "finish_rate_int",
    "data_source", "data_quality_score", "fight_history",
}

def upsert_csv(row: dict) -> str:
    if not FINAL.exists():
        return f"/!\\ {FINAL.name} not found at {FINAL}"

    with open(FINAL, encoding="utf-8-sig", newline="") as f:
        raw = list(csv.reader(f))
    if len(raw) < 2:
        return "/!\\ CSV empty or corrupted"

    cat_row  = raw[0]
    col_row  = raw[1]
    data_rows = [dict(zip(col_row, r + [""] * (len(col_row) - len(r)))) for r in raw[2:]]

    name_lc = row.get("name", "").strip().lower()
    matched = -1
    if name_lc:
        for i, r in enumerate(data_rows):
            if r.get("name", "").strip().lower() == name_lc:
                matched = i
                break

    if matched >= 0:
        existing = data_rows[matched]
        for k, v in row.items():
            if v == "" or v is None:
                continue
            if k in ALWAYS_REFRESH or not str(existing.get(k, "")).strip():
                existing[k] = v
        # id, ufc_id, fightmatrix_id, mma_com_id all equal
        eid = existing.get("id", "")
        if eid:
            for col in ("ufc_id", "fightmatrix_id", "mma_com_id"):
                existing[col] = eid
        data_rows[matched] = existing
        action = f"UPDATE  id={eid}"
    else:
        ids = []
        for r in data_rows:
            try: ids.append(int(r.get("id", "0") or 0))
            except (ValueError, TypeError): pass
        new_id = (max(ids) + 1) if ids else 1
        row["id"]             = new_id
        row["ufc_id"]         = new_id
        row["fightmatrix_id"] = new_id
        row["mma_com_id"]     = new_id
        data_rows.append(row)
        action = f"INSERT  id={new_id}"

    with open(FINAL, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cat_row)
        writer.writerow(col_row)
        for d in data_rows:
            writer.writerow([d.get(c, "") for c in col_row])

    return f"OK  {action}  : {row.get('name', '?')}"


# the small recap at the end

SUMMARY_FIELDS = [
    ("name", "Name"), ("nickname", "Nickname"),
    ("date_of_birth", "Birth"), ("age", "Age"),
    ("nationality", "Nationality"),
    ("weight_class_current", "Current class"), ("weight_class_origin", "Origin class"),
    ("height_inches", "Height"), ("reach_inches", "Reach"), ("stance", "Stance"),
    ("first_sport", "Sport/Style"),
    ("record_total_wins", "Wins"), ("record_total_losses", "Losses"),
    ("record_total_draws", "Draws"), ("record_total_nc", "NC"),
    ("record_ufc_wins", "UFC W"), ("record_ufc_losses", "UFC L"), ("record_ufc_draws", "UFC D"),
    ("wins_by_ko_tko", "W KO"), ("wins_by_submission", "W SUB"), ("wins_by_decision", "W DEC"),
    ("losses_by_ko_tko", "L KO"), ("losses_by_submission", "L SUB"), ("losses_by_decision", "L DEC"),
    ("career_debut_date", "Debut"), ("last_fight_date", "Last fight"), ("days_inactive", "Days inactive"),
    ("current_streak", "Streak"), ("last_5_results", "Last 5"),
    ("finish_rate_int", "Finish %"), ("win_percentage_int", "Win %"),
    ("fightmatrix_rating_points", "FM Pts"), ("fightmatrix_big_league_record", "FM BigLeague"),
    ("fightmatrix_540_metric", "FM 540"), ("fightmatrix_quality_perf_pct", "FM Quality %"),
    ("ufc_official_rank", "Rank UFC"), ("ufc_p4p_rank", "Rank P4P"),
    ("is_ufc_champion", "Champion"), ("ufc_title_match_win", "Title wins"),
    ("is_verified", "Verified"), ("photo_url", "Photo"), ("data_quality_score", "Score"),
]

def print_summary(row: dict):
    print("\n" + "-" * 60)
    filled = 0
    for k, lbl in SUMMARY_FIELDS:
        v = row.get(k, "")
        if v:
            filled += 1
            disp = str(v)
            if len(disp) > 55: disp = disp[:55] + "..."
            print(f"  {lbl:<18} {disp}")
    print("-" * 60)
    print(f"  Filled: {filled} / {len(SUMMARY_FIELDS)}")
    print("-" * 60)


# le main

def collect_urls(prompt_num: int = 1) -> list[str]:
    print(f"\n  Fighter #{prompt_num} - paste 1 to 3 URLs (1 per line), empty line to validate:")
    urls = []
    while len(urls) < 4:
        try:
            line = input(f"  URL #{len(urls) + 1} : ").strip()
        except (EOFError, KeyboardInterrupt):
            return []
        if not line:
            if urls: break
            else: return []   # Empty input with nothing = quit
        urls.append(line)
    return urls


def main():
    args = [a for a in sys.argv[1:] if a.strip()]

    if args:
        # Direct arg mode: scrape a single fighter and quit
        row = scrape(args)
        print_summary(row)
        print("\n" + upsert_csv(row))
        return

    # Interactive mode: loop until "q" or an empty line
    print("=" * 60)
    print("  scrape_mma  (FightMatrix + Tapology + Underground + UFC)")
    print("  FAST mode: py scrape_mma.py \"URL_FM\" \"URL_TAPO\" \"URL_UG\" \"URL_UFC\"")
    print("  Loop mode: press Q (or an empty line) to quit")
    print("=" * 60)

    fighter_num = 1
    while True:
        print(f"\n{''*60}")
        try:
            first = input(
                f"  Fighter #{fighter_num}"
                " - paste a URL (or Q to quit): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            break

        if first.lower() in ("q", "quit", "exit", ""):
            print("  Goodbye.")
            break

        # Collect the remaining URLs
        urls = [first]
        while len(urls) < 4:
            try:
                line = input(f"  URL #{len(urls) + 1} (Enter = validate): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            urls.append(line)

        row = scrape(urls)
        print_summary(row)
        result = upsert_csv(row)
        print(f"\n  {result}")
        fighter_num += 1

        # Short pause to avoid consecutive rate-limit errors
        time.sleep(0.5)


if __name__ == "__main__":
    main()
