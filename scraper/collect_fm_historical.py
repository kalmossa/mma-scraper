"""
collect_fm_historical.py
Collect FightMatrix profile URLs from the quarterly HISTORICAL rankings
(Issues 1 to 146, from 01/01/1990 to today).

Covers retired fighters / former opponents missing from the current rankings.

URL pattern:
    https://www.fightmatrix.com/historical-mma-rankings/generated-historical-rankings/
    ?Issue={N}&Division={D}&Page={P}&RF=FM

USAGE
    # Recent issues (5 years, top 50 per division) - fast, ~10 min
    py collect_fm_historical.py

    # Since 2005 (Issue 60), top 75 per division - recommended, ~30 min
    py collect_fm_historical.py --from-issue 60 --pages 3

    # Everything since 2000 (Issue 40), top 100 - complete, ~60 min
    py collect_fm_historical.py --from-issue 40 --pages 4

    # EVERYTHING (1990->2026, top 25 only)
    py collect_fm_historical.py --from-issue 1 --pages 1

OUTPUT
    fm_historical_urls.txt   (unique URLs, new fighters not yet in the database)

NEXT STEP
    py scraper/scrape_batch.py --file fm_historical_urls.txt --resume --yes
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

BASE = "https://www.fightmatrix.com"
HIST_URL = BASE + "/historical-mma-rankings/generated-historical-rankings/"

# Divisions FM (id → label lisible)
DIVISIONS = {
    1:  "Heavyweight",
    2:  "Light Heavyweight",
    3:  "Middleweight",
    4:  "Welterweight",
    5:  "Lightweight",
    6:  "Featherweight",
    7:  "Bantamweight",
    8:  "Flyweight",
    9:  "Strawweight",
    12: "Women - Atomweight",
    13: "Women - Strawweight",
    14: "Women - Flyweight",
    15: "Women - Bantamweight",
    16: "Women - Featherweight+",
    # 11 = Division Dominance (meta-ranking, skipped)
    # 17 = Women - Division Dominance (skipped)
}

PROFILE_RE = re.compile(r'href="(/fighter-profile/[^"?#&\s]+)"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

DELAY = 0.7   # seconds between requests


def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text
        return None
    except requests.RequestException as e:
        print(f"  [ERR] {url}: {e}")
        return None


def normalize_url(path: str) -> str:
    u = path.strip().rstrip("/")
    if u.startswith("/"):
        u = BASE + u
    return u


def extract_urls(html: str) -> set[str]:
    return {normalize_url(p) for p in PROFILE_RE.findall(html)}


def load_db_urls() -> set[str]:
    """Load the FM URLs already in the database to avoid duplicates."""
    try:
        from db_connection import get_connection
        conn = get_connection()
        if not conn:
            return set()
        cur = conn.cursor()
        cur.execute("SELECT data_source FROM fighters WHERE data_source IS NOT NULL")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        known: set[str] = set()
        for (ds,) in rows:
            for part in str(ds).split("|"):
                part = part.strip().rstrip("/")
                if "fightmatrix.com/fighter-profile/" in part:
                    known.add(part)
        print(f"  -> {len(known)} FM URLs already in the database (will be ignored)")
        return known
    except Exception as e:
        print(f"  [WARN] Could not load the database: {e}")
        return set()


def get_last_page(html: str) -> int:
    """Extract the last page number from the '>>' link."""
    m = re.search(r'Page=(\d+)&RF=FM[^"]*"><b>>></b>', html)
    if m:
        return int(m.group(1))
    return 1


def collect_historical(from_issue: int, to_issue: int, max_pages: int,
                       out_file: Path, skip_db: bool) -> None:
    print("=" * 60)
    print("  collect_fm_historical.py")
    print(f"  Issues: {from_issue} → {to_issue}  ({to_issue - from_issue + 1} quarters)")
    print(f"  Max pages / division / issue: {max_pages}  (~{max_pages * 25} fighters)")
    print(f"  Divisions: {len(DIVISIONS)}")
    print(f"  Output: {out_file}")
    print("=" * 60)

    known_db = load_db_urls() if skip_db else set()

    all_urls:  set[str] = set()   # all collected URLs
    new_urls:  set[str] = set()   # URLs not yet in the database

    total_pages_fetched = 0
    errors = 0

    for issue in range(from_issue, to_issue + 1):
        for div_id, div_label in DIVISIONS.items():
            # Page 1: detect the total number of pages
            url1 = f"{HIST_URL}?Issue={issue}&Division={div_id}&Page=1&RF=FM"
            html1 = fetch(url1)
            total_pages_fetched += 1
            time.sleep(DELAY)

            if not html1:
                errors += 1
                continue

            last_page = get_last_page(html1)
            pages_to_scrape = min(max_pages, last_page)

            # Extraire page 1
            found = extract_urls(html1)
            all_urls.update(found)
            new = found - known_db
            new_urls.update(new)

            # Pages suivantes
            for page in range(2, pages_to_scrape + 1):
                url_p = f"{HIST_URL}?Issue={issue}&Division={div_id}&Page={page}&RF=FM"
                html_p = fetch(url_p)
                total_pages_fetched += 1
                time.sleep(DELAY)
                if not html_p:
                    break
                found_p = extract_urls(html_p)
                if not found_p:
                    break
                all_urls.update(found_p)
                new_urls.update(found_p - known_db)

            # Print every 10 issues
            if issue % 10 == 0 or issue == from_issue:
                print(f"  Issue {issue:3d} / {to_issue}  {div_label:<25} "
                      f"pages={pages_to_scrape}/{last_page}  "
                      f"total={len(all_urls)}  new={len(new_urls)}")

        # Pause inter-issue
        time.sleep(0.5)

    # Ecriture
    print("\n" + "=" * 60)
    print(f"  Pages fetched    : {total_pages_fetched}")
    print(f"  Total URLs       : {len(all_urls)} (with duplicates across issues)")
    print(f"  Already in DB    : {len(all_urls - new_urls)}")
    print(f"  NEW              : {len(new_urls)}")
    print("=" * 60)

    with out_file.open("w", encoding="utf-8") as f:
        f.write(f"# FM Historical Rankings — {len(new_urls)} new URLs\n")
        f.write(f"# Issues {from_issue}→{to_issue}, {max_pages} pages/div\n")
        for u in sorted(new_urls):
            f.write(u + "\n")

    print(f"\n  File: {out_file.resolve()}")
    if new_urls:
        est = len(new_urls) * 2.8 / 60
        print(f"\n  NEXT STEP:")
        print(f"  py scraper/scrape_batch.py --file {out_file} --resume --yes")
        print(f"  Scrape estimate: ~{est:.0f} min")
    else:
        print("\n  No new URL - all the historical fighters are already in the database.")


def main():
    # Fetch the current issue from the main page
    print("  Fetching the current issue...", end=" ", flush=True)
    latest_issue = 146  # fallback
    try:
        r = requests.get(HIST_URL, headers=HEADERS, timeout=10)
        m = re.search(r"value='(\d+)'[^>]*>04/01/2026<", r.text)
        if not m:
            m = re.search(r"selected[^>]*value='(\d+)'", r.text)
        if m:
            latest_issue = int(m.group(1))
    except Exception:
        pass
    print(f"Issue {latest_issue}")

    parser = argparse.ArgumentParser(description="Collect historical FM URLs")
    parser.add_argument("--from-issue", type=int, default=80,
                        help="Start issue (default 80 = ~2010). Issue 1=1990, 60=2005, 80=2010, 100=2015")
    parser.add_argument("--to-issue", type=int, default=latest_issue,
                        help=f"End issue (default {latest_issue} = current)")
    parser.add_argument("--pages", type=int, default=2,
                        help="Max pages per division per issue (default 2 = top 50). 4=top 100, 6=top 150")
    parser.add_argument("--out", default="data/scraper_output/fightmatrix/fm_historical_urls.txt",
                        help="Output file")
    parser.add_argument("--no-skip-db", action="store_true",
                        help="Also include the URLs already in the database (disables filtering)")
    args = parser.parse_args()

    # Estimation
    n_issues = args.to_issue - args.from_issue + 1
    n_divs = len(DIVISIONS)
    est_pages = n_issues * n_divs * args.pages
    est_min = est_pages * DELAY / 60

    print(f"\n  Issues: {args.from_issue} → {args.to_issue} ({n_issues} quarters)")
    print(f"  Pages : {args.pages}/div/issue → ~{est_pages} requetes")
    print(f"  Estimate: ~{est_min:.0f} min")

    collect_historical(
        from_issue=args.from_issue,
        to_issue=args.to_issue,
        max_pages=args.pages,
        out_file=Path(args.out),
        skip_db=not args.no_skip_db,
    )


if __name__ == "__main__":
    main()
