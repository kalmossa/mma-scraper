"""
scrape_fm_full.py
Scrape FightMatrix through the ranking pages (static HTML).

MODE: collect
    Generates URLs from the FightMatrix rankings.
    --pages N = N pages per division (25 fighters/page).
    Example: --pages 50 = top 1250 per division (~16,000 URLs in total).
    Output: fm_deep_urls.txt

NOTE: the BFS mode (following opponent links from the profiles) does NOT
work with FightMatrix. The fight tables are loaded through
JavaScript/AJAX; the initial HTML contains no link to the opponents.
The BFS was therefore kept in the code but it is not useful (it always
produces 0 new fighters). Use the collect mode exclusively.

USAGE
    # Top 200 per division (8 pages x 25 = 200, ~5 min)
    py scrape_fm_full.py collect --pages 8

    # Top 500 per division (20 pages, ~12 min)
    py scrape_fm_full.py collect --pages 20

    # Top 1250 per division (50 pages, ~30 min) - recommended for retired fighters
    py scrape_fm_full.py collect --pages 50 --out fm_deep_urls.txt

    # Then scrape the collected URLs:
    py scrape_batch.py --file data/scraper_output/fightmatrix/fm_deep_urls.txt --yes

OUTPUT
    fm_deep_urls.txt   (collect mode)
"""

import re
import sys
import time
import argparse
import logging
from collections import deque
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

ROOT = Path(__file__).parent.parent  # repository root
SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))

LOG = logging.getLogger("scrape_fm_full")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

# FightMatrix config
FM_BASE    = "https://www.fightmatrix.com"
RANK_URL   = FM_BASE + "/mma-ranks/{slug}?PageNum={page}"
PROFILE_RE = re.compile(r'href="(/fighter-profile/[^"?#&\s]+)"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DIVISIONS = {
    "hvy":  "heavyweight-265-lbs",
    "lhw":  "light-heavyweight-185-205-lbs",
    "mw":   "middleweight",
    "ww":   "welterweight",
    "lw":   "lightweight",
    "fw":   "featherweight",
    "bw":   "bantamweight",
    "fly":  "flyweight",
    "w_fw": "womens-featheweight",   # intentional FM typo (missing 'r')
    "w_bw": "womens-bantamweight",
    "w_fl": "womens-flyweight",
    "w_sw": "womens-strawweight",
    "w_aw": "womens-atomweight",
}

DELAY_PAGE = 0.8    # between ranking pages
DELAY_PROF = 0.8    # between BFS profiles
DELAY_DIV  = 1.5    # between divisions


# helpers HTTP

def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        LOG.warning(f"HTTP {r.status_code} -> {url}")
        return None
    except requests.RequestException as e:
        LOG.error(f"Fetch error {url}: {e}")
        return None


def normalize_url(path_or_url: str) -> str:
    """Return an absolute FM URL without a trailing slash."""
    u = path_or_url.strip().rstrip("/")
    if u.startswith("/"):
        u = FM_BASE + u
    return u


def extract_profile_urls(html: str) -> list[str]:
    """Extract every /fighter-profile/ link from an FM HTML page."""
    return sorted({normalize_url(p) for p in PROFILE_RE.findall(html)})


# MODE 1: collect - FM rankings -> URL file

def run_collect(pages: int, divs: list[str], out_file: Path):
    """Collect the URLs from the FM rankings (up to pages*25 per division)."""
    print("=" * 60)
    print(f"  scrape_fm_full.py  -- MODE COLLECT")
    print(f"  Pages / division: {pages}  (~{pages * 25} fighters max)")
    print(f"  Divisions        : {len(divs)}")
    print(f"  Output           : {out_file}")
    print("=" * 60)

    collected: dict[str, list[str]] = {}
    grand_total: set[str] = set()

    for key in divs:
        slug  = DIVISIONS[key]
        label = key
        all_urls: set[str] = set()
        print(f"\n  [{label}]", end="", flush=True)

        for page in range(1, pages + 1):
            html = fetch(RANK_URL.format(slug=slug, page=page))
            if not html:
                break
            urls = extract_profile_urls(html)
            if not urls:
                break
            new = set(urls) - all_urls
            all_urls.update(new)
            print(f" p{page}({len(new)})", end="", flush=True)
            time.sleep(DELAY_PAGE)

        print(f" -> {len(all_urls)} total")
        time.sleep(DELAY_DIV)
        collected[label] = sorted(all_urls)
        grand_total.update(all_urls)

    # Ecriture
    with out_file.open("w", encoding="utf-8") as f:
        for label, urls in collected.items():
            f.write(f"# {label} ({len(urls)} fighters)\n")
            for u in urls:
                f.write(u + "\n")
            f.write("\n")

    print("\n" + "=" * 60)
    for label, urls in collected.items():
        print(f"  {label:<10}  {len(urls):>4} fighters")
    print("-" * 60)
    print(f"  TOTAL  {len(grand_total):>4} unique fighters")
    print(f"\n  -> File: {out_file.resolve()}")
    print(f"\n  NEXT STEP:")
    print(f"  py scrape_batch.py --file {out_file.name}")
    print("=" * 60)


# MODE 2: BFS - opponents of every fighter already in the database

def load_db_fm_urls() -> set[str]:
    """
    Extract the FightMatrix URLs from the data_source column (format: url1 | url2 | ...).
    Return an empty set without crashing if the connection fails.
    """
    try:
        from db_connection import get_connection
        conn = get_connection()
        if not conn:
            LOG.warning("Database connection impossible - use --seeds to provide the starting URLs")
            return set()
        cur = conn.cursor()
        cur.execute("SELECT data_source FROM fighters WHERE data_source IS NOT NULL")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        urls = set()
        for (data_source,) in rows:
            if not data_source:
                continue
            for part in str(data_source).split("|"):
                part = part.strip().rstrip("/")
                if "fightmatrix.com/fighter-profile/" in part:
                    urls.add(part)

        LOG.info(f"  {len(urls)} FM URLs found in data_source in the database")
        return urls

    except Exception as e:
        LOG.warning(f"Unable to load the URLs from the database ({e})")
        LOG.warning("  -> Run with --seeds data/scraper_output/fightmatrix/collected_fm_urls.txt to provide the seeds manually")
        return set()


def run_bfs(max_new: int, dry_run: bool, export_file: Path | None,
            extra_seeds: list[str]):
    """
    BFS starting from the FM fighters in the database.
    For each profile:
      - Fetch the FM HTML
      - Extract the opponent links
      - Enqueue the new ones
      - Scrape the fighter (except with --dry-run or --export)
    """
    print("=" * 60)
    print(f"  scrape_fm_full.py  -- MODE BFS")
    if dry_run:
        print("  DRY-RUN: display only, no scrape")
    elif export_file:
        print(f"  EXPORT: URLs saved in {export_file}")
    else:
        print("  LIVE: direct scrape into Supabase")
    print(f"  Max new fighters: {max_new}")
    print("=" * 60)

    # Seeds = fighters already in the database (rebuilt from fightmatrix_id + name)
    known_in_db = load_db_fm_urls()
    print(f"\n  -> {len(known_in_db)} FM URLs rebuilt from the database")

    # visited = everything already seen (in the database or already queued)
    # Note: the rebuilt URLs are "known" but we still have to
    # visit them to extract the opponents (inactive fighters, etc.)
    # known_norm = fighters already in the database (we VISIT them for their opponents
    # but we do not RE-SCRAPE them)
    known_norm = {normalize_url(u) for u in known_in_db}

    # visited = URLs already FETCHED (avoids infinite loops).
    # important: do NOT pre-fill it with known_norm!
    # If the 4306 database fighters are put in visited at the start, their opponents
    # (also among the 4306) would be marked "already seen" and never added to
    # queue -> +0 opponents -> nothing discovered.
    # The right logic: visited only grows when a page is actually FETCHED.
    visited: set[str] = set()

    # Initial queue = extra seeds (high priority) + database fighters (to extract opponents)
    bfs_queue: deque[str] = deque()
    queued: set[str] = set()   # what IS in the queue (avoids duplicates in the queue)

    # Extra seeds first (collected_fm_urls.txt) - processed first
    for u in extra_seeds:
        nu = normalize_url(u)
        if nu not in queued:
            bfs_queue.appendleft(nu)
            queued.add(nu)

    # Then the database fighters - we visit them to extract their opponents
    for u in known_in_db:
        nu = normalize_url(u)
        if nu not in queued:
            bfs_queue.append(nu)
            queued.add(nu)

    if not bfs_queue:
        print("\n  /!\\ Empty queue! Provide the seeds with --seeds data/scraper_output/fightmatrix/collected_fm_urls.txt")
        print("  Example: py scrape_fm_full.py bfs --seeds data/scraper_output/fightmatrix/collected_fm_urls.txt --export bfs_urls.txt")
        return

    print(f"  -> Initial queue: {len(bfs_queue)} profiles to visit")
    print(f"     ({len(extra_seeds)} from --seeds, {len(known_in_db)} from the database)")

    if dry_run:
        print(f"\n  (dry-run: an actual visit is needed to count exactly)")
        print(f"  Tip: run --bfs --seeds data/scraper_output/fightmatrix/collected_fm_urls.txt --export bfs_urls.txt")
        return

    # In export / live mode, prepare the scraper if needed
    if not dry_run and export_file is None:
        try:
            from scrape_mma import scrape, detect_source
            from scrape_batch import (
                load_existing_index, upsert_fighter,
            )
            from db_connection import get_connection
            conn_live = get_connection()
            cur_live  = conn_live.cursor()
            index     = load_existing_index(cur_live)
            cur_live.close()
        except ImportError as e:
            LOG.error(f"Import error: {e}")
            return

    discovered_new: list[str] = []   # URLs not in the database
    visited_count = 0

    print(f"\n  BFS start...")

    while bfs_queue:
        url = bfs_queue.popleft()
        visited_count += 1

        # A fighter is "known" if its normalized URL is in known_norm
        # (fighters rebuilt from fightmatrix_id + name in the database)
        # or if the exact URL was in the extra seeds (already scraped)
        is_in_db = url in known_norm
        prefix = f"[visited={visited_count} new={len(discovered_new)}/{max_new}]"

        if len(discovered_new) >= max_new:
            print(f"\n  Limite max_new={max_new} reached, stopping.")
            break

        # Mark this URL as fetched (avoids fetching it again)
        visited.add(url)

        # Fetch the page to extract the opponent links
        html = fetch(url)
        if not html:
            time.sleep(DELAY_PROF)
            continue

        # Extract the opponents -> enriches the queue
        # We use `queued` (not `visited`) so as not to block the database fighters
        # that have not been visited yet
        opp_urls = extract_profile_urls(html)
        added = 0
        for opp_url in opp_urls:
            if opp_url not in queued:
                bfs_queue.append(opp_url)
                queued.add(opp_url)
                added += 1

        # If this fighter is not in the database, it has to be scraped
        if not is_in_db:
            discovered_new.append(url)
            print(f"{prefix}  NEW  +{added} opponents  {url}")

            if not dry_run:
                if export_file is None:
                    # Scrape live
                    try:
                        row = scrape([url])
                        if row.get("name"):
                            result, _ = upsert_fighter(conn_live, row, index, "fightmatrix")
                            print(f"  -> {result}  name={row.get('name')}")
                        else:
                            print(f"  -> empty name, ignored")
                    except Exception as e:
                        LOG.error(f"  Scrape error: {e}")
        else:
            if visited_count % 50 == 0 or added > 0:
                print(f"{prefix}  already-in-DB +{added} new opponents queued  {url.split('/')[-2][:30]}")

        time.sleep(DELAY_PROF)

    # Save the export if requested
    if export_file is not None and discovered_new:
        with export_file.open("w", encoding="utf-8") as f:
            f.write(f"# BFS FightMatrix — {len(discovered_new)} new fighters\n")
            for u in discovered_new:
                f.write(u + "\n")
        print(f"\n  -> {len(discovered_new)} URLs exported to {export_file}")
        print(f"  NEXT STEP:")
        print(f"  py scrape_batch.py --file {export_file.name}")

    if not dry_run and export_file is None:
        conn_live.close()

    print(f"\n  BFS done: {visited_count} profiles visited, "
          f"{len(discovered_new)} new fighters processed")


#  MAIN

def main():
    parser = argparse.ArgumentParser(
        description="scrape_fm_full.py - deep FightMatrix collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="mode")

    # Mode collect
    pc = sub.add_parser("collect", help="Generate URLs from the FM rankings")
    pc.add_argument("--pages", type=int, default=20,
                    help="Pages per division (25 fighters/page). Default: 20 (top 500)")
    pc.add_argument("--div", choices=list(DIVISIONS.keys()) + ["all"], default="all",
                    help="Specific division or 'all' (default)")
    pc.add_argument("--out", default="data/scraper_output/fightmatrix/fm_deep_urls.txt",
                    help="Output file (default: fm_deep_urls.txt)")

    # Mode bfs
    pb = sub.add_parser("bfs", help="BFS from the fighters in the database")
    pb.add_argument("--max", type=int, default=5000,
                    help="Max number of new fighters to scrape (default: 5000)")
    pb.add_argument("--dry-run", action="store_true",
                    help="Display without scraping")
    pb.add_argument("--export", default=None,
                    help="Export the URLs into this file instead of scraping directly")
    pb.add_argument("--seeds", default=None,
                    help="Additional seed URL file (e.g. collected_fm_urls.txt)")

    # Compat shortcut: --collect and --bfs as direct flags
    parser.add_argument("--collect", action="store_true", help="Alias mode collect")
    parser.add_argument("--bfs",     action="store_true", help="Alias mode bfs")
    parser.add_argument("--pages",   type=int, default=20)
    parser.add_argument("--div",     choices=list(DIVISIONS.keys()) + ["all"], default="all")
    parser.add_argument("--out",     default="data/scraper_output/fightmatrix/fm_deep_urls.txt")
    parser.add_argument("--max",     type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--export",  default=None)
    parser.add_argument("--seeds",   default=None)

    args = parser.parse_args()

    # Determine the mode
    mode = args.mode
    if not mode:
        if args.collect:
            mode = "collect"
        elif args.bfs:
            mode = "bfs"
        else:
            parser.print_help()
            return

    if mode == "collect":
        divs = list(DIVISIONS.keys()) if args.div == "all" else [args.div]
        run_collect(
            pages    = args.pages,
            divs     = divs,
            out_file = Path(args.out),
        )

    elif mode == "bfs":
        extra_seeds = []
        if args.seeds:
            seed_path = Path(args.seeds)
            if seed_path.exists():
                for line in seed_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    for part in line.split():
                        if "fightmatrix.com" in part:
                            extra_seeds.append(part)
                print(f"  -> {len(extra_seeds)} extra seeds from {args.seeds}")
            else:
                print(f"  /!\\ Seeds file not found: {args.seeds}")

        run_bfs(
            max_new    = args.max,
            dry_run    = args.dry_run,
            export_file= Path(args.export) if args.export else None,
            extra_seeds= extra_seeds,
        )


if __name__ == "__main__":
    main()
