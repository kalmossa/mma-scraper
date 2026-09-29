"""
collect_fm_urls.py
Automatically collect every FightMatrix profile URL for the 14 divisions
(men + women), ready to feed to the batch scraper.

USAGE
    py collect_fm_urls.py               # top 100 per division (4 pages)
    py collect_fm_urls.py --pages 8     # top 200 per division (8 pages)
    py collect_fm_urls.py --div lhw     # a single division
    py collect_fm_urls.py --all         # all divisions, top 100 each

Output: collected_fm_urls.txt (one URL per line)
        + a per-division summary on screen

Then pass the file to scrape_batch.py
"""

import re
import sys
import time
import argparse
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

# Divisions FightMatrix (slug → label lisible)
DIVISIONS = {
    # Hommes
    "hvy":  ("heavyweight-265-lbs",              "Heavyweight (265)"),
    "lhw":  ("light-heavyweight-185-205-lbs",    "Light Heavyweight"),
    "mw":   ("middleweight",                     "Middleweight"),
    "ww":   ("welterweight",                     "Welterweight"),
    "lw":   ("lightweight",                      "Lightweight"),
    "fw":   ("featherweight",                    "Featherweight"),
    "bw":   ("bantamweight",                     "Bantamweight"),
    "fly":  ("flyweight",                        "Flyweight"),
    # Femmes
    # NB: FightMatrix has a typo in this slug (feathe-weight without 'r') -- it is intentional
    "w_fw": ("womens-featheweight",              "Women's Featherweight"),
    "w_bw": ("womens-bantamweight",              "Women's Bantamweight"),
    "w_fl": ("womens-flyweight",                 "Women's Flyweight"),
    "w_sw": ("womens-strawweight",               "Women's Strawweight"),
    "w_aw": ("womens-atomweight",                "Women's Atomweight"),
}

BASE_URL   = "https://www.fightmatrix.com/mma-ranks/{slug}?PageNum={page}"
PROFILE_RE = re.compile(r'href="(https://www\.fightmatrix\.com/fighter-profile/[^"]+)"')
PROFILE_RE_REL = re.compile(r'href="(/fighter-profile/[^"]+)"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DELAY_BETWEEN_PAGES = 0.8   # seconds between requests (polite)
DELAY_BETWEEN_DIVS  = 1.5   # seconds between divisions


def fetch_page(slug: str, page: int) -> str | None:
    url = BASE_URL.format(slug=slug, page=page)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None   # division does not exist or there is no next page
        print(f"  [WARN] HTTP {r.status_code} for {url}")
        return None
    except requests.RequestException as e:
        print(f"  [ERROR] {url}: {e}")
        return None


def extract_profile_urls(html: str) -> list[str]:
    """Extract every /fighter-profile/ URL from the HTML page."""
    found = set()
    # Liens absolus
    for url in PROFILE_RE.findall(html):
        found.add(url.rstrip("/"))
    # Relative links (seen on some FM pages)
    for path in PROFILE_RE_REL.findall(html):
        found.add("https://www.fightmatrix.com" + path.rstrip("/"))
    return sorted(found)


def collect_division(key: str, slug: str, label: str, max_pages: int) -> list[str]:
    """Collect every fighter URL for one division."""
    all_urls: set[str] = set()
    print(f"\n  [{label}]", end="", flush=True)

    for page in range(1, max_pages + 1):
        html = fetch_page(slug, page)
        if not html:
            break   # no next page
        urls = extract_profile_urls(html)
        if not urls:
            break   # empty page => end of pagination
        new = set(urls) - all_urls
        all_urls.update(new)
        print(f" p{page}({len(new)})", end="", flush=True)
        time.sleep(DELAY_BETWEEN_PAGES)

    print(f" → {len(all_urls)} total")
    return sorted(all_urls)


def main():
    parser = argparse.ArgumentParser(description="Collect FightMatrix URLs per division")
    parser.add_argument("--pages", type=int, default=4,
                        help="Number of pages per division (25 fighters/page). Default: 4 (top 100)")
    parser.add_argument("--div", choices=list(DIVISIONS.keys()) + ["all"], default="all",
                        help="Specific division (e.g. lhw, mw) or 'all' (default)")
    parser.add_argument("--out", default="data/scraper_output/fightmatrix/collected_fm_urls.txt",
                        help="Output file (default: collected_fm_urls.txt)")
    args = parser.parse_args()

    divs_to_scrape = (
        list(DIVISIONS.items())
        if args.div == "all"
        else [(args.div, DIVISIONS[args.div])]
    )

    print("=" * 60)
    print(f"  collect_fm_urls.py")
    print(f"  Divisions: {len(divs_to_scrape)}")
    print(f"  Max pages per division: {args.pages} (~{args.pages * 25} fighters)")
    print(f"  Output: {args.out}")
    print("=" * 60)

    all_collected: dict[str, list[str]] = {}
    grand_total: set[str] = set()

    for key, (slug, label) in divs_to_scrape:
        urls = collect_division(key, slug, label, args.pages)
        all_collected[label] = urls
        grand_total.update(urls)
        time.sleep(DELAY_BETWEEN_DIVS)

    # Write the file
    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        for label, urls in all_collected.items():
            f.write(f"# {label} ({len(urls)} fighters)\n")
            for url in urls:
                f.write(url + "\n")
            f.write("\n")

    # Recapitulatif
    print("\n" + "=" * 60)
    print(f"  RECAPITULATIF")
    print("=" * 60)
    for label, urls in all_collected.items():
        print(f"  {label:<30} {len(urls):>4} fighters")
    print("-" * 60)
    print(f"  TOTAL (deduplique)          {len(grand_total):>4} fighters")
    print(f"\n  File written: {out_path.resolve()}")
    print("\n  NEXT STEP:")
    print("  py scrape_batch.py")
    print("  -> paste the content of collected_fm_urls.txt")
    print("     (lines starting with # are ignored automatically)")
    print("=" * 60)


if __name__ == "__main__":
    main()
