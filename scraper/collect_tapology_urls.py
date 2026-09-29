"""
collect_tapology_urls.py
Automatically find the Tapology URL of every database fighter
using the Tapology search (/search?term=NAME&type=fighters).

Matching strategy (in order of confidence):
  1. Exact: URL slug contains the normalized first name + last name
  2. Partial: slug contains the last name + the first name
  3. If several results are close, take the first one
  4. If 0 match -> record it in a log for manual review

Automatic filter: fighters that already have a Tapology URL
in their data_source column are ignored.

USAGE
    # All fighters without a Tapology URL (default)
    py collect_tapology_urls.py

    # Limit to 500 fighters (test)
    py collect_tapology_urls.py --limit 500

    # Search again even for fighters that already have Tapology
    py collect_tapology_urls.py --force

    # Active fighters only (is_active = TRUE)
    py collect_tapology_urls.py --active-only

OUTPUT
    tapology_urls.txt     : URLs found, ready for the batch scraper
    tapology_nomatch.txt  : fighters for which no URL was found

NEXT STEP
    py scrape_batch.py --file tapology_urls.txt --yes
"""

import re
import sys
import time
import unicodedata
import argparse
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

BASE_TAP = "https://www.tapology.com"
SEARCH_URL = BASE_TAP + "/search?term={term}&type=fighters"
PROFILE_RE = re.compile(r'href="(/fightcenter/fighters/(\d+)-([^"]+))"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_TAP + "/",
}

DELAY = 0.8   # seconds between requests (polite)


# Helpers

def normalize(s: str) -> str:
    """Normalize a name for comparison: lowercase, no accents, alphanumeric."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def slug_to_name(slug: str) -> str:
    """Extract the readable name from a Tapology URL slug."""
    return slug.replace("-", " ").strip()


def name_match_score(our_name: str, our_nickname: str | None, slug: str) -> int:
    """
    0-100 matching score between our fighter and the Tapology slug.
    100 = exact, 0 = no match.
    """
    our_n = normalize(our_name)
    slug_n = normalize(slug)

    # Score 100: slug contains exactly the normalized name
    if our_n == slug_n:
        return 100

    # Score 90: slug contains the name as an exact substring
    if our_n in slug_n:
        return 90

    # Score 80: all the name tokens are in the slug
    tokens = [t for t in our_n.split() if len(t) > 1]
    if tokens and all(t in slug_n for t in tokens):
        return 80

    # Score 70: first name + last name in the slug (ignores middle name)
    parts = our_name.strip().split()
    if len(parts) >= 2:
        first = normalize(parts[0])
        last  = normalize(parts[-1])
        if first in slug_n and last in slug_n:
            return 70

    # Score 60: last name only (useful when the first name is very short)
    parts2 = our_name.strip().split()
    if len(parts2) >= 2:
        last2 = normalize(parts2[-1])
        if len(last2) >= 4 and last2 in slug_n:
            return 60

    # Score 50 : match via surnom
    if our_nickname:
        nick_n = normalize(our_nickname)
        if len(nick_n) >= 4 and nick_n in slug_n:
            return 50

    return 0


def search_tapology(name: str, nickname: str | None) -> tuple[str | None, int]:
    """
    Search for a fighter on Tapology.
    Return (url, score) of the best match, or (None, 0) if nothing.
    """
    term = requests.utils.quote(name)
    url = SEARCH_URL.format(term=term)

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None, 0
    except requests.RequestException:
        return None, 0

    results = list(dict.fromkeys(
        (path, fid, slug) for path, fid, slug in PROFILE_RE.findall(r.text)
    ))

    if not results:
        return None, 0

    best_url, best_score = None, 0
    for path, fid, slug in results:
        score = name_match_score(name, nickname, slug)
        if score > best_score:
            best_score = score
            best_url = BASE_TAP + path

    return best_url, best_score


# DB helpers

def load_fighters_from_db(active_only: bool, force: bool) -> list[dict]:
    """Load the database fighters that do not have a Tapology URL yet."""
    try:
        from db_connection import get_connection
        conn = get_connection()
        if not conn:
            print("ERROR: unable to connect to the database")
            sys.exit(1)

        cur = conn.cursor()
        conditions = []
        if not force:
            conditions.append("(data_source IS NULL OR data_source NOT ILIKE '%%tapology.com%%')")
        if active_only:
            conditions.append("is_active = TRUE")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(f"""
            SELECT id, name, nickname, weight_class_current, nationality
            FROM fighters
            {where}
            ORDER BY fightmatrix_rating_points DESC NULLS LAST, record_total_wins DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        fighters = [
            {"id": r[0], "name": r[1], "nickname": r[2],
             "weight_class": r[3], "nationality": r[4]}
            for r in rows if r[1]  # ignore empty name
        ]
        print(f"  -> {len(fighters)} fighters to process")
        return fighters

    except Exception as e:
        print(f"DB ERROR: {e}")
        sys.exit(1)


# MAIN

def main():
    parser = argparse.ArgumentParser(description="Collect Tapology URLs from the database")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit to N fighters (0 = all)")
    parser.add_argument("--force", action="store_true",
                        help="Search again even for fighters that already have Tapology")
    parser.add_argument("--active-only", action="store_true",
                        help="Active fighters only")
    parser.add_argument("--min-score", type=int, default=70,
                        help="Minimum confidence score (0-100, default 70)")
    parser.add_argument("--out", default="data/scraper_output/tapology/tapology_urls.txt")
    parser.add_argument("--nomatch", default="data/scraper_output/tapology/tapology_nomatch.txt")
    args = parser.parse_args()

    print("=" * 60)
    print("  collect_tapology_urls.py")
    print(f"  Min score : {args.min_score}/100")
    print(f"  Output    : {args.out}")
    print("=" * 60)

    fighters = load_fighters_from_db(args.active_only, args.force)
    if args.limit:
        fighters = fighters[:args.limit]
        print(f"  Limit applied: {args.limit} fighters")

    est = len(fighters) * DELAY / 60
    print(f"  Estimate: ~{est:.0f} min  ({len(fighters)} searches x {DELAY}s)")
    print()

    found:    list[tuple[str, str, int]] = []   # (fighter_name, tap_url, score)
    nomatch:  list[str] = []
    low_conf: list[tuple[str, str, int]] = []   # score < min but > 0

    for i, f in enumerate(fighters, 1):
        name = f["name"]
        nick = f.get("nickname")

        tap_url, score = search_tapology(name, nick)

        if score >= args.min_score and tap_url:
            found.append((name, tap_url, score))
            status = f"OK score={score}"
        elif tap_url and score > 0:
            low_conf.append((name, tap_url, score))
            status = f"~ score={score} (low)"
        else:
            nomatch.append(name)
            status = "-- not found"

        if i % 50 == 0 or i <= 5 or score >= args.min_score:
            pct = i / len(fighters) * 100
            print(f"  [{i:5}/{len(fighters)} {pct:4.0f}%]  {status:20}  {name}")

        time.sleep(DELAY)

    # Write the URLs file
    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# Tapology URLs collectees — {len(found)} fighters\n")
        f.write(f"# Minimum score: {args.min_score}/100\n")
        for name, url, score in found:
            f.write(f"{url}  # {name} (score={score})\n")

    # Ecriture no-match
    nomatch_path = Path(args.nomatch)
    with nomatch_path.open("w", encoding="utf-8") as f:
        f.write(f"# Fighters without a Tapology URL found ({len(nomatch)})\n")
        for name in nomatch:
            f.write(name + "\n")

    # Bilan
    print("\n" + "=" * 60)
    print(f"  FOUND (score >= {args.min_score})  : {len(found)}")
    print(f"  Confiance basse (score < {args.min_score}) : {len(low_conf)}")
    print(f"  Not found             : {len(nomatch)}")
    print(f"  Success rate          : {len(found)/len(fighters)*100:.1f}%%")
    print("=" * 60)
    print(f"\n  URLs: {out_path.resolve()}")
    print(f"  No-match: {nomatch_path.resolve()}")

    if found:
        est_scrape = len(found) * 2.8 / 60
        print(f"\n  NEXT STEP:")
        print(f"  py scrape_batch.py --file {args.out} --yes")
        print(f"  Scrape estimate: ~{est_scrape:.0f} min")

    if low_conf:
        print(f"\n  Note: {len(low_conf)} URLs with low confidence ignored.")
        print(f"  Run again with --min-score 50 to include them.")


if __name__ == "__main__":
    main()
