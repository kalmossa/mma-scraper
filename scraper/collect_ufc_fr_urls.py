"""
collect_ufc_fr_urls.py
Automatically collect every UFC-FR profile URL for the 14 divisions
(men + women), ready to feed to the batch scraper.

SOURCE: https://www.ufc-fr.com
Coverage: ~7875 fighters (315 pages x 25)

USAGE
    py collect_ufc_fr_urls.py              # all divisions (top ~200 per division)
    py collect_ufc_fr_urls.py --all        # the WHOLE site (315 pages, ~7875 fighters, ~45 min)
    py collect_ufc_fr_urls.py --div lw     # Lightweight only
    py collect_ufc_fr_urls.py --pages 5    # 5 pages per division (100 fighters)

MODES
  --div-mode  : per division (recommended, more targeted)
  --all       : scrape the full alphabetical listing (all promotions, not only UFC)

Output: collected_ufc_fr_urls.txt
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

BASE = "https://www.ufc-fr.com"

# Listing URLs per division
# Format page 1 : /combattant-division-{id}.html
# Format page N>1 : /combattant-division-{id}-page-{(N-1)*20}.html
# 20 fighters per page in division mode

DIVISIONS = {
    # Hommes
    "fly":  (127, "Flyweight"),
    "bw":   (128, "Bantamweight"),
    "fw":   (129, "Featherweight"),
    "lw":   (130, "Lightweight"),
    "ww":   (131, "Welterweight"),
    "mw":   (132, "Middleweight"),
    "lhw":  (133, "Light Heavyweight"),
    "hvy":  (134, "Heavyweight"),
    # Femmes
    "w_sw": (135, "Women's Strawweight"),
    "w_fly":(136, "Women's Flyweight"),
    "w_bw": (137, "Women's Bantamweight"),
    "w_fw": (138, "Women's Featherweight"),
    "w_mw": (139, "Women's Middleweight"),
    "w_aw": (140, "Women's Atomweight"),
}

# Format listing alphabetique global :
# Page 1 : /combattant.html
# Page N>1 : /combattant-page-{(N-1)*25}.html
# 25 fighters per page, ~315 pages in total

PROFILE_RE = re.compile(r'href="(/combattant-\d+\.html)"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://www.ufc-fr.com/",
}

DELAY = 0.6   # seconds between requests (polite)


# HTTP helper

def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        print(f"  [WARN] HTTP {r.status_code} -> {url}")
        return None
    except requests.RequestException as e:
        print(f"  [ERR] {url}: {e}")
        return None


def extract_profile_urls(html: str) -> list[str]:
    """Extract the /combattant-ID.html URLs from the HTML."""
    found = set()
    for path in PROFILE_RE.findall(html):
        # Exclude listing pages (e.g. /combattant-division-130.html)
        if re.match(r"^/combattant-\d+\.html$", path):
            found.add(BASE + path)
    return sorted(found)


# Mode division

def collect_division(key: str, div_id: int, label: str, max_pages: int) -> list[str]:
    """Collect every URL for one UFC-FR division."""
    all_urls: set[str] = set()
    print(f"\n  [{label}]", end="", flush=True)

    for page in range(1, max_pages + 1):
        if page == 1:
            url = f"{BASE}/combattant-division-{div_id}.html"
        else:
            offset = (page - 1) * 20
            url = f"{BASE}/combattant-division-{div_id}-page-{offset}.html"

        html = fetch(url)
        if not html:
            break

        urls = extract_profile_urls(html)
        if not urls:
            break

        new = set(urls) - all_urls
        all_urls.update(new)
        print(f" p{page}({len(new)})", end="", flush=True)
        time.sleep(DELAY)

    print(f" -> {len(all_urls)} total")
    return sorted(all_urls)


# Mode global (listing alphabetique)

def collect_all_global(max_pages: int) -> list[str]:
    """
    Scrape the global alphabetical listing /combattant.html
    All promotions together (not only UFC).
    max_pages: maximum number of pages (default 315 = the whole site).
    """
    all_urls: set[str] = set()
    print(f"\n  [Listing global]", end="", flush=True)

    for page in range(1, max_pages + 1):
        if page == 1:
            url = f"{BASE}/combattant.html"
        else:
            offset = (page - 1) * 25
            url = f"{BASE}/combattant-page-{offset}.html"

        html = fetch(url)
        if not html:
            break

        urls = extract_profile_urls(html)
        if not urls:
            break

        new = set(urls) - all_urls
        all_urls.update(new)
        print(f" p{page}({len(new)})", end="", flush=True)
        if page % 10 == 0:
            print(f"\n    ... {len(all_urls)} URLs collectees jusqu'ici ...", end="", flush=True)
        time.sleep(DELAY)

    print(f"\n  -> {len(all_urls)} total")
    return sorted(all_urls)


# MAIN

def main():
    parser = argparse.ArgumentParser(description="Collect UFC-FR URLs")
    parser.add_argument("--pages", type=int, default=10,
                        help="Pages per division (20 fighters/page). Default: 10 (top 200)")
    parser.add_argument("--div", choices=list(DIVISIONS.keys()) + ["all"], default="all",
                        help="Specific division or 'all' (default)")
    parser.add_argument("--all", dest="global_mode", action="store_true",
                        help="Scrape the COMPLETE alphabetical listing (~7875 fighters, ~45 min)")
    parser.add_argument("--global-pages", type=int, default=315,
                        help="Number of pages of the global listing (default: 315 = everything)")
    parser.add_argument("--out", default="data/scraper_output/ufc_fr/collected_ufc_fr_urls.txt",
                        help="Output file")
    args = parser.parse_args()

    out_path = Path(args.out)

    print("=" * 60)
    print("  collect_ufc_fr_urls.py")
    if args.global_mode:
        est = args.global_pages * 0.6 / 60
        print(f"  Mode: global listing (~{args.global_pages * 25} fighters max)")
        print(f"  Estimate: ~{est:.0f} min")
    else:
        divs = list(DIVISIONS.items()) if args.div == "all" else [(args.div, DIVISIONS[args.div])]
        print(f"  Mode: per division ({len(divs)} divisions)")
        print(f"  Pages / division: {args.pages}  (~{args.pages * 20} fighters max)")
    print(f"  Output: {out_path}")
    print("=" * 60)

    if args.global_mode:
        # Global mode: scrape the whole alphabetical listing
        urls = collect_all_global(args.global_pages)
        all_collected = {"Listing global": urls}
    else:
        # Per-division mode
        divs_to_scrape = (
            list(DIVISIONS.items())
            if args.div == "all"
            else [(args.div, DIVISIONS[args.div])]
        )
        all_collected: dict[str, list[str]] = {}
        grand_total: set[str] = set()

        for key, (div_id, label) in divs_to_scrape:
            urls = collect_division(key, div_id, label, args.pages)
            all_collected[label] = urls
            grand_total.update(urls)
            time.sleep(1.0)

    # Ecriture
    grand_total = {u for urls in all_collected.values() for u in urls}
    with out_path.open("w", encoding="utf-8") as f:
        for label, urls in all_collected.items():
            f.write(f"# {label} ({len(urls)} fighters)\n")
            for u in urls:
                f.write(u + "\n")
            f.write("\n")

    # Recapitulatif
    print("\n" + "=" * 60)
    print("  RECAPITULATIF")
    print("=" * 60)
    for label, urls in all_collected.items():
        print(f"  {label:<35} {len(urls):>5} fighters")
    print("-" * 60)
    print(f"  TOTAL (deduplique)               {len(grand_total):>5} fighters")
    print(f"\n  File: {out_path.resolve()}")
    print("\n  NEXT STEP:")
    print(f"  py scrape_batch.py --file {out_path.name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
