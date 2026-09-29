"""
collect_tapology_smart.py
ROBUST Tapology collector:
  - uses cloudscraper (bypasses Cloudflare / 503)
  - searches for each database fighter by name
  - INCREMENTAL SAVING: resumes where it stopped (--resume)
  - exponential backoff on 503 (waits longer and longer)
  - randomized delays (looks human)

REQUIREMENTS
    pip install cloudscraper

USAGE
    # Start / resume the collection (all fighters without a Tapology URL)
    py collect_tapology_smart.py

    # Resume after an interruption
    py collect_tapology_smart.py --resume

    # Limit for testing
    py collect_tapology_smart.py --limit 100

    # Minimum matching score
    py collect_tapology_smart.py --min-score 70

OUTPUT
    tapology_smart_urls.txt        : URLs found (appended as it goes)
    tapology_smart_progress.txt    : names already processed (for --resume)
    tapology_smart_nomatch.txt     : not found
"""

import re
import sys
import time
import random
import unicodedata
import argparse
from pathlib import Path

try:
    import cloudscraper
except ImportError:
    print("ERROR: pip install cloudscraper")
    sys.exit(1)

ROOT = Path(__file__).parent.parent  # repository root
SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))
from db_connection import get_connection

BASE_TAP = "https://www.tapology.com"
SEARCH_URL = BASE_TAP + "/search?term={term}&type=fighters"
PROFILE_RE = re.compile(r'href="(/fightcenter/fighters/(\d+)-([^"]+))"')

PROGRESS_FILE = ROOT / "data/scraper_output/tapology/tapology_smart_progress.txt"
URLS_FILE     = ROOT / "data/scraper_output/tapology/tapology_smart_urls.txt"
NOMATCH_FILE  = ROOT / "data/scraper_output/tapology/tapology_smart_nomatch.txt"

# Delays: random between MIN and MAX (looks human)
DELAY_MIN, DELAY_MAX = 1.5, 3.5
# backoff on 503: wait backoff_base * 2^n
BACKOFF_BASE = 10
MAX_BACKOFF  = 300


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def name_score(name: str, slug: str) -> int:
    our_n = normalize(name)
    slug_n = normalize(slug)
    if our_n == slug_n:  return 100
    if our_n in slug_n:  return 90
    parts = name.strip().split()
    if len(parts) >= 2:
        first, last = normalize(parts[0]), normalize(parts[-1])
        if first in slug_n and last in slug_n: return 80
        if len(last) >= 4 and last in slug_n:  return 60
    return 0


def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )


def search(scraper, name: str, min_score: int) -> tuple[str | None, int, bool]:
    """Retourne (url, score, was_503)."""
    term = name.replace(" ", "+")
    url = SEARCH_URL.format(term=term)
    try:
        r = scraper.get(url, timeout=20)
    except Exception:
        return None, 0, False

    if r.status_code == 503:
        return None, 0, True
    if r.status_code != 200:
        return None, 0, False

    results = list(dict.fromkeys(PROFILE_RE.findall(r.text)))
    best_url, best_score = None, 0
    for path, fid, slug in results:
        s = name_score(name, slug)
        if s > best_score:
            best_score, best_url = s, BASE_TAP + path
    return (best_url, best_score, False) if best_score >= min_score else (None, best_score, False)


def load_fighters(force: bool) -> list[dict]:
    conn = get_connection()
    if not conn:
        sys.exit(1)
    cur = conn.cursor()
    cond = "" if force else "WHERE (data_source IS NULL OR data_source NOT ILIKE '%tapology.com%')"
    cur.execute(f"""
        SELECT id, name FROM fighters {cond}
        ORDER BY fightmatrix_rating_points DESC NULLS LAST, record_total_wins DESC NULLS LAST
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"id": r[0], "name": r[1]} for r in rows if r[1]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="Resume the collection")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  collect_tapology_smart.py (cloudscraper)")
    print("=" * 60)

    fighters = load_fighters(args.force)

    # Resume: skip names already processed
    done = set()
    if args.resume and PROGRESS_FILE.exists():
        done = set(PROGRESS_FILE.read_text(encoding="utf-8").splitlines())
        print(f"  Resuming: {len(done)} fighters already processed")
    fighters = [f for f in fighters if f["name"] not in done]

    if args.limit:
        fighters = fighters[:args.limit]

    print(f"  To process: {len(fighters)} fighters\n")

    scraper = make_scraper()
    found = nomatch = consecutive_503 = 0

    prog_f = PROGRESS_FILE.open("a", encoding="utf-8")
    urls_f = URLS_FILE.open("a", encoding="utf-8")
    nom_f  = NOMATCH_FILE.open("a", encoding="utf-8")

    try:
        for i, f in enumerate(fighters, 1):
            name = f["name"]
            url, score, was_503 = search(scraper, name, args.min_score)

            if was_503:
                consecutive_503 += 1
                backoff = min(BACKOFF_BASE * (2 ** (consecutive_503 - 1)), MAX_BACKOFF)
                print(f"  [503] backoff {backoff}s (#{consecutive_503})... recreating scraper")
                time.sleep(backoff)
                scraper = make_scraper()   # nouveau scraper = nouveaux cookies
                # Retry this fighter
                url, score, was_503 = search(scraper, name, args.min_score)
                if was_503:
                    continue  # still blocked, move on (will be retried on the next --resume if not written)
            consecutive_503 = 0

            prog_f.write(name + "\n"); prog_f.flush()

            if url:
                found += 1
                urls_f.write(f"{url}  # {name} (score={score}, id={f['id']})\n"); urls_f.flush()
                status = f"OK {score}"
            else:
                nomatch += 1
                nom_f.write(name + "\n"); nom_f.flush()
                status = "--"

            if i <= 10 or i % 25 == 0 or url:
                pct = i / len(fighters) * 100
                print(f"  [{i:5}/{len(fighters)} {pct:4.0f}%]  {status:8}  {name}")

            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    except KeyboardInterrupt:
        print("\n  Interrupted. Run again with --resume to continue.")
    finally:
        prog_f.close(); urls_f.close(); nom_f.close()

    print(f"\n{'='*60}")
    print(f"  FOUND    : {found}")
    print(f"  NOMATCH  : {nomatch}")
    print(f"{'='*60}")
    print(f"\n  URLs: {URLS_FILE.resolve()}")
    print(f"\n  NEXT STEP:")
    print(f"  py scrape_batch.py --file tapology_smart_urls.txt --yes")


if __name__ == "__main__":
    main()
