"""
collect_tapology_listings.py
Collect Tapology URLs by scraping the LISTING PAGES per weight class
(instead of searching for each fighter by name).

Advantages over collect_tapology_urls.py (search by name):
  - Covers ALL fighters of every division on Tapology
  - No rate-limiting from individual searches
  - Database name <-> Tapology name matching done in memory (faster)
  - Gives name + URL together -> reliable cross matching

URL pattern:
    https://www.tapology.com/fightcenter?mode=fighters
        &weight_class=Heavyweight&order=last_fight&page=N

USAGE
    # All divisions, top 50 pages (~1250 fighters/div, ~30 min)
    py collect_tapology_listings.py

    # Heavyweight only, 10 pages (~250 fighters)
    py collect_tapology_listings.py --div Heavyweight --pages 10

    # The whole site (long, ~120 min)
    py collect_tapology_listings.py --pages 999

    # Strict matching (score >= 80) to avoid false positives
    py collect_tapology_listings.py --min-score 80

OUTPUT
    tapology_listing_urls.txt    : matched URLs, ready for the batch scraper
    tapology_listing_nomatch.txt : Tapology fighters with no database match
    tapology_listing_unmatched_db.txt : database fighters with no Tapology URL found
"""

import os
import re
import sys
import time
import unicodedata
import argparse
from pathlib import Path
from collections import defaultdict

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

ROOT = Path(__file__).parent.parent  # repository root
SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))
from db_connection import get_connection

BASE_TAP = "https://www.tapology.com"

def make_listing_url(page: int, mode: str = "fighters") -> str:
    """
    Tapology listing URL.
    mode="fighters" : list of MMA fighters (order=last_fight).
    mode="bouts"    : list of past MMA bouts (date_preset=all_time, order=date).
                      Contains 2 profile links per row -> more exhaustive (includes
                      inactive / former fighters).
    """
    if mode == "bouts":
        return (f"{BASE_TAP}/fightcenter?group=pro&sport=mma"
                f"&date_preset=all_time&order=date&page={page}")
    # fighters mode: sport=mma filter to exclude boxers/kickboxers
    return f"{BASE_TAP}/fightcenter?mode=fighters&sport=mma&order=last_fight&page={page}"

# Tapology division names (URL-encoded, requests turns + into %20 automatically)
DIVISIONS = [
    "Heavyweight",
    "Light Heavyweight",
    "Middleweight",
    "Welterweight",
    "Lightweight",
    "Featherweight",
    "Bantamweight",
    "Flyweight",
    "Strawweight",
    "Atomweight",
    "Super Heavyweight",
    "Women's Strawweight",
    "Women's Flyweight",
    "Women's Bantamweight",
    "Women's Featherweight",
    "Women's Atomweight",
]

# Regex: a fighter row in the Tapology listing
# Format : <a href="/fightcenter/fighters/ID-slug">Name</a>
FIGHTER_ROW_RE = re.compile(
    r'href="(/fightcenter/fighters/(\d+)-([^"]+))"[^>]*>\s*([^<]+?)\s*</a>',
    re.DOTALL,
)

# Wider regex to capture just the profile hrefs
PROFILE_RE = re.compile(r'href="(/fightcenter/fighters/(\d+)-([^"]+))"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_TAP + "/fightcenter",
}

DELAY          = 2.5   # seconds between pages (avoids rate-limiting)
DELAY_DIVISION = 45    # seconds between divisions

# --- Selenium (real Chrome, SCRAPER_SELENIUM=1) ---
# Tapology blocks listings fetched with requests (403). With SCRAPER_SELENIUM=1,
# the listing pages are fetched through a shared real Chrome (singleton).
# Solve the captcha by hand once, the cookie is kept for the whole session.
_SELENIUM_DRIVER = None

def _get_driver():
    global _SELENIUM_DRIVER
    if _SELENIUM_DRIVER is not None:
        return _SELENIUM_DRIVER
    import undetected_chromedriver as uc
    from pathlib import Path
    profile_dir = str(Path(__file__).resolve().parent.parent / ".chrome_profile")
    opts = uc.ChromeOptions()
    if os.environ.get("SCRAPER_SELENIUM_HEADLESS") == "1":
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,900")
    _cd_fixed = str(Path(__file__).resolve().parent.parent / "tools" / "chromedriver149.exe")
    _cd_kwargs = {"driver_executable_path": _cd_fixed} if os.path.exists(_cd_fixed) else {}
    drv = uc.Chrome(options=opts, user_data_dir=profile_dir, **_cd_kwargs)
    _SELENIUM_DRIVER = drv
    print("  [Selenium] Chrome UC started (persistent profile - captcha only once).")
    return drv


_CF_MARKERS = (
    "just a moment", "cf-browser-verification", "challenge-platform",
    "cf_chl_opt", "__cf_chl", "turnstile", "checking your browser",
    "enable javascript and cookies to continue",
)
CHALLENGE_WAIT = 180  # s: let the human solve the Turnstile

def _selenium_fetch(url: str) -> str | None:
    try:
        drv = _get_driver()
    except Exception as e:
        print(f"  [Selenium] unavailable ({e}) -> pip install selenium")
        return None
    drv.get(url)
    waited = 0.0
    warned = False
    while waited < CHALLENGE_WAIT:
        html = drv.page_source
        blob = ((drv.title or "") + " " + html[:4000]).lower()
        if any(m in blob for m in _CF_MARKERS):
            if not warned:
                print(f"  [Selenium] CLOUDFLARE -> solve the captcha BY HAND. Waiting up to {CHALLENGE_WAIT}s...", flush=True)
                warned = True
            time.sleep(2.0)
            waited += 2.0
            continue
        # Real listing: must contain fighter profile links
        if "/fightcenter/fighters/" in html:
            return html
        time.sleep(1.0)
        waited += 1.0
    print(f"  [Selenium] challenge/content not solved: {url}")
    return None


# Normalisation

def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[''ʼ']", "", s)
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)
    s = re.sub(r"[^a-zA-Z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def score_match(db_name: str, tap_name: str, tap_slug: str) -> int:
    """Score 0-100 between a database name and a Tapology name/slug."""
    db_n   = normalize(db_name)
    tap_n  = normalize(tap_name)
    slug_n = normalize(tap_slug.replace("-", " "))

    # Exact name
    if db_n == tap_n:   return 100
    if db_n == slug_n:  return 100

    # Full substring
    if db_n in tap_n or tap_n in db_n:  return 90
    if db_n in slug_n:                  return 90

    # All tokens
    tokens = [t for t in db_n.split() if len(t) > 1]
    if tokens and all(t in slug_n for t in tokens): return 80
    if tokens and all(t in tap_n  for t in tokens): return 80

    # First name + last name
    parts = db_name.strip().split()
    if len(parts) >= 2:
        first = normalize(parts[0])
        last  = normalize(parts[-1])
        if first in slug_n and last in slug_n: return 70
        if first in tap_n  and last in tap_n:  return 70
        if len(last) >= 4 and last in slug_n:  return 60

    return 0


# HTTP

def fetch(url: str) -> str | None:
    if os.environ.get("SCRAPER_SELENIUM") != "0":  # Selenium by default (403 otherwise)
        return _selenium_fetch(url)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        print(f"  [HTTP {r.status_code}] {url}")
        return None
    except requests.RequestException as e:
        print(f"  [ERR] {e}")
        return None


def parse_listing_page(html: str) -> list[tuple[str, str, str]]:
    """
    Return a list of (path, fid, slug, name_from_link) from a listing page.
    Tries the regex with the link text first, otherwise just the href.
    """
    results = []
    seen = set()

    # Attempt 1: regex with the name in the link text
    for m in FIGHTER_ROW_RE.finditer(html):
        path, fid, slug = m.group(1), m.group(2), m.group(3)
        name = m.group(4).strip()
        if fid not in seen:
            seen.add(fid)
            results.append((path, fid, slug, name))

    # Fallback: just the hrefs (empty name)
    if not results:
        for path, fid, slug in PROFILE_RE.findall(html):
            if fid not in seen:
                seen.add(fid)
                results.append((path, fid, slug, ""))

    return results


def has_next_page(html: str, current_page: int) -> bool:
    """Check whether a next page exists (link > or page+1)."""
    next_p = current_page + 1
    return (
        f"page={next_p}" in html
        or 'rel="next"' in html
        or f'page%3D{next_p}' in html
    )


# DB

def load_db_fighters(force: bool) -> dict[str, list[dict]]:
    """
    Return a dict normalized_name -> [fighter_dict].
    If force=False, skip fighters that already have a Tapology URL.
    """
    conn = get_connection()
    if not conn:
        print("DB ERROR")
        sys.exit(1)
    cur = conn.cursor()

    cond = "" if force else "WHERE (data_source IS NULL OR data_source NOT ILIKE '%tapology.com%')"
    cur.execute(f"""
        SELECT id, name, data_source
        FROM fighters
        {cond}
        ORDER BY id
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    by_norm: dict[str, list[dict]] = defaultdict(list)
    for fid, name, ds in rows:
        if name:
            nn = normalize(name)
            by_norm[nn].append({"id": fid, "name": name, "data_source": ds})
    return by_norm


# Matching

def match_tapology_to_db(
    tap_fighters: list[tuple[str, str, str, str]],   # (path, fid, slug, name)
    db_by_norm: dict[str, list[dict]],
    min_score: int,
) -> tuple[list[tuple], list[tuple]]:
    """
    Retourne (matched, unmatched).
    matched  : [(db_id, db_name, tap_url, score), ...]
    unmatched: [(tap_url, tap_name), ...]
    """
    matched:   list[tuple] = []
    unmatched: list[tuple] = []

    for path, fid, slug, tap_name in tap_fighters:
        tap_url = BASE_TAP + path
        best_score = 0
        best_db = None

        # Search by normalized name (tap_name and slug)
        candidates_keys = set()
        if tap_name:
            candidates_keys.add(normalize(tap_name))
        slug_clean = normalize(slug.replace("-", " "))
        candidates_keys.add(slug_clean)
        # Also try the parts of the slug
        parts = slug_clean.split()
        if len(parts) >= 2:
            candidates_keys.add(" ".join(parts[:2]))
            candidates_keys.add(parts[-1])

        candidates = []
        for k in candidates_keys:
            candidates += db_by_norm.get(k, [])
        # Also look for database entries whose normalized name contains the slug
        # (matching partiel via scan — limite a 5 candidats proches)
        if not candidates:
            for db_key, db_list in db_by_norm.items():
                if slug_clean in db_key or db_key in slug_clean:
                    candidates += db_list
                    if len(candidates) >= 10:
                        break

        for db_f in candidates:
            s = score_match(db_f["name"], tap_name or slug.replace("-", " "), slug)
            if s > best_score:
                best_score, best_db = s, db_f

        if best_score >= min_score and best_db:
            matched.append((best_db["id"], best_db["name"], tap_url, best_score))
        else:
            unmatched.append((tap_url, tap_name or slug.replace("-", " ")))

    return matched, unmatched


# MAIN

def main():
    parser = argparse.ArgumentParser(description="Collect Tapology URLs through the global listing")
    parser.add_argument("--mode", default="fighters", choices=["fighters", "bouts"],
                        help="fighters=list of MMA fighters (default) | bouts=all-time MMA results (more exhaustive)")
    parser.add_argument("--pages", type=int, default=50,
                        help="Max pages (25 fighters/page, default 50 = ~1250)")
    parser.add_argument("--min-score", type=int, default=70,
                        help="Minimum matching score (default 70)")
    parser.add_argument("--force", action="store_true",
                        help="Include fighters that already have a Tapology URL")
    parser.add_argument("--out",     default="data/scraper_output/tapology/tapology_listing_urls.txt")
    parser.add_argument("--nomatch", default="data/scraper_output/tapology/tapology_listing_nomatch.txt")
    args = parser.parse_args()

    print("=" * 65)
    print("  collect_tapology_listings.py")
    print(f"  Mode        : {args.mode} (sport=mma, all-time)")
    print(f"  Max pages   : {args.pages} (~{args.pages * 25} {'fighters' if args.mode == 'fighters' else 'bouts'})")
    print(f"  Score min   : {args.min_score}/100")
    est = args.pages * DELAY / 60
    print(f"  Estimate    : ~{est:.0f} min")
    print("=" * 65)

    print("\n  Loading the database...", end=" ", flush=True)
    db_by_norm = load_db_fighters(args.force)
    total_db = sum(len(v) for v in db_by_norm.values())
    print(f"{total_db} fighters without a Tapology URL")

    all_tap: list[tuple[str, str, str, str]] = []
    seen_fids: set[str] = set()
    total_pages = 0

    print(f"\n  Tapology global listing ({args.mode}) :", flush=True)
    for page in range(1, args.pages + 1):
        url = make_listing_url(page, args.mode)
        html = fetch(url)
        total_pages += 1

        if not html:
            print(f" [stop p{page}: empty/blocked page]")
            break

        fighters = parse_listing_page(html)
        if not fighters:
            print(f" (end p{page})")
            break

        new = [(p, fid, sl, nm) for p, fid, sl, nm in fighters if fid not in seen_fids]
        for item in new:
            seen_fids.add(item[1])
        all_tap.extend(new)

        print(f" p{page}({len(new)})", end="", flush=True)

        if not has_next_page(html, page):
            print(" [last page]")
            break

        time.sleep(DELAY)

    print(f"\n\n  Pages fetched: {total_pages}")
    print(f"  Unique Tapology fighters: {len(all_tap)}")

    # Match against the database
    print("\n  Matching against the database...", end=" ", flush=True)
    matched, unmatched = match_tapology_to_db(all_tap, db_by_norm, args.min_score)
    print(f"  {len(matched)} matches ({len(unmatched)} without a database match)")

    # Deduplicate (a database fighter only takes its best Tapology match)
    best_per_db: dict[int, tuple] = {}
    for db_id, db_name, tap_url, score in matched:
        if db_id not in best_per_db or score > best_per_db[db_id][3]:
            best_per_db[db_id] = (db_id, db_name, tap_url, score)
    matched_dedup = sorted(best_per_db.values(), key=lambda x: -x[3])

    # ecriture urls
    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# Tapology URLs (listing) — {len(matched_dedup)} fighters\n")
        f.write(f"# Score min : {args.min_score}/100\n\n")
        for db_id, db_name, tap_url, score in matched_dedup:
            f.write(f"{tap_url}  # {db_name} (score={score}, id={db_id})\n")

    # Ecriture nomatch Tapology
    nom_path = Path(args.nomatch)
    with nom_path.open("w", encoding="utf-8") as f:
        f.write(f"# Tapology fighters without a database match - {len(unmatched)}\n\n")
        for tap_url, tap_name in unmatched:
            f.write(f"{tap_url}  # {tap_name}\n")

    # Bilan
    print(f"\n{'='*65}")
    print(f"  FOUND (score >= {args.min_score}) : {len(matched_dedup)}")
    print(f"  Already covered (skipped): {10853 - total_db} approx")
    print(f"  No Tapology match        : {len(unmatched)}")
    print(f"{'='*65}")
    print(f"\n  URLs file     : {out_path.resolve()}")
    print(f"  Nomatch file  : {nom_path.resolve()}")

    if matched_dedup:
        est_scrape = len(matched_dedup) * 2.8 / 60
        print(f"\n  NEXT STEP:")
        print(f"  py scraper/scrape_batch.py --file {args.out} --resume --yes")
        print(f"  Scrape estimate: ~{est_scrape:.0f} min")


if __name__ == "__main__":
    main()
