"""
collect_tapology_rankings.py
Collect Tapology URLs from the static RANKINGS PAGES.
These pages are server-side rendered and contain the fighter links directly.

URL pattern:
    https://www.tapology.com/rankings/mma-fighter-rankings-by-weight-division/
    weight-class-{slug}

Covers the top 100 fighters per division (4 pages x 25),
then matches them against the database by name.

USAGE
    py collect_tapology_rankings.py              # first 4 pages (~100/div)
    py collect_tapology_rankings.py --pages 8    # top ~200/div
    py collect_tapology_rankings.py --min-score 60  # more permissive matching

OUTPUT
    tapology_ranking_urls.txt
"""

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

# Tapology rankings pages (static, server-side rendered)
RANKING_BASE = BASE_TAP + "/rankings/mma-fighter-rankings-by-weight-division"

DIVISION_SLUGS = [
    ("heavyweight",              "Heavyweight"),
    ("light-heavyweight",        "Light Heavyweight"),
    ("middleweight",             "Middleweight"),
    ("welterweight",             "Welterweight"),
    ("lightweight",              "Lightweight"),
    ("featherweight",            "Featherweight"),
    ("bantamweight",             "Bantamweight"),
    ("flyweight",                "Flyweight"),
    ("strawweight",              "Strawweight"),
    ("atomweight",               "Atomweight"),
    ("super-heavyweight",        "Super Heavyweight"),
    ("womens-strawweight",       "Women's Strawweight"),
    ("womens-flyweight",         "Women's Flyweight"),
    ("womens-bantamweight",      "Women's Bantamweight"),
    ("womens-featherweight",     "Women's Featherweight"),
    ("womens-atomweight",        "Women's Atomweight"),
]

# Global rankings page (P4P, etc.)
EXTRA_PAGES = [
    BASE_TAP + "/rankings",
    BASE_TAP + "/rankings/mma-fighter-rankings-by-weight-division",
]

PROFILE_RE = re.compile(r'href="(/fightcenter/fighters/(\d+)-([^"]+))"')

# Regex to capture name + URL from the rankings
RANKING_ROW_RE = re.compile(
    r'href="(/fightcenter/fighters/(\d+)-([^"]+))"[^>]*>\s*([^<]{2,50}?)\s*</a>',
    re.DOTALL,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_TAP + "/rankings",
}

DELAY = 2.0
DELAY_DIV = 10.0


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[''ʼ']", "", s)
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)
    s = re.sub(r"[^a-zA-Z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def score_match(db_name: str, tap_name: str, slug: str) -> int:
    db_n  = normalize(db_name)
    tap_n = normalize(tap_name)
    sl_n  = normalize(slug.replace("-", " "))

    if db_n and db_n == tap_n:  return 100
    if db_n and db_n == sl_n:   return 100
    if tap_n and db_n in tap_n: return 90
    if sl_n  and db_n in sl_n:  return 90

    parts = db_name.strip().split()   # ex: "Beneil Dariush" -> ["Beneil", "Dariush"]
    if len(parts) >= 2:
        first = normalize(parts[0])
        last  = normalize(parts[-1])
        if tap_n and first in tap_n and last in tap_n: return 80
        if sl_n  and first in sl_n  and last in sl_n:  return 80
        if sl_n  and len(last) >= 4 and last in sl_n:  return 60

    return 0


def fetch(url: str) -> str | None:    
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.text
        print(f"  [HTTP {r.status_code}] {url}")
        return None
    except requests.RequestException as e:
        print(f"  [ERR] {e}")
        return None


def parse_page(html: str) -> list[tuple[str, str, str, str]]: 
    """Retourne (path, fid, slug, name)."""
    seen, results = set(), []
    for m in RANKING_ROW_RE.finditer(html):
        path, fid, slug, name = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        if fid not in seen and len(name) >= 2 and name.replace(" ", "").isalpha():
            seen.add(fid)
            results.append((path, fid, slug, name))
    # Fallback without a name
    if not results:
        for path, fid, slug in PROFILE_RE.findall(html):
            if fid not in seen:
                seen.add(fid)
                results.append((path, fid, slug, ""))
    return results


def load_db(force: bool) -> dict[str, list[dict]]:   # key = normalized name, value = list of {id, name}
    conn = get_connection()
    if not conn:
        sys.exit(1)
    cur = conn.cursor()
    cond = "" if force else "WHERE (data_source IS NULL OR data_source NOT ILIKE '%tapology.com%')"
    cur.execute(f"SELECT id, name FROM fighters {cond}")
    rows = cur.fetchall()
    cur.close(); conn.close()
    by_norm: dict[str, list[dict]] = defaultdict(list)
    for fid, name in rows:
        if name:
            by_norm[normalize(name)].append({"id": fid, "name": name})
    return by_norm


def main():   # collect + match + write
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages",     type=int, default=4,
                        help="Pages per division (25 fighters/page, default 4=top 100)")
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument("--force",     action="store_true")
    parser.add_argument("--out",       default="data/scraper_output/tapology/tapology_ranking_urls.txt")
    args = parser.parse_args()

    print("=" * 60)
    print("  collect_tapology_rankings.py")
    print(f"  Pages/division: {args.pages} (~{args.pages*25} fighters)")
    print(f"  Score min      : {args.min_score}")
    est = (len(DIVISION_SLUGS) * args.pages * DELAY + len(DIVISION_SLUGS) * DELAY_DIV) / 60
    print(f"  Estimate       : ~{est:.0f} min")
    print("=" * 60)

    print("\n  Loading the database...", end=" ", flush=True)
    db = load_db(args.force)
    print(f"{sum(len(v) for v in db.values())} fighters without a Tapology URL")

    all_tap: list[tuple[str, str, str, str]] = []
    seen_fids: set[str] = set()

    for slug, label in DIVISION_SLUGS:
        div_fighters = []
        print(f"\n  [{label}]", end="", flush=True)

        for page in range(1, args.pages + 1):
            if page == 1:
                url = f"{RANKING_BASE}/weight-class-{slug}"
            else:
                url = f"{RANKING_BASE}/weight-class-{slug}?page={page}"

            html = fetch(url)
            if not html:
                break

            fighters = parse_page(html)
            new = [(p, fid, sl, nm) for p, fid, sl, nm in fighters if fid not in seen_fids]
            for item in new:
                seen_fids.add(item[1])
            div_fighters.extend(new)
            all_tap.extend(new)

            print(f" p{page}({len(new)})", end="", flush=True)
            if len(new) == 0 and page > 1:
                break
            time.sleep(DELAY)

        print(f" -> {len(div_fighters)}")
        if slug != DIVISION_SLUGS[-1][0]:
            time.sleep(DELAY_DIV)

    print(f"\n  Unique Tapology fighters collected: {len(all_tap)}")

    # Matching
    print("\n  Matching against the database...")
    matched: dict[int, tuple] = {}

    for path, fid, slug, tap_name in all_tap:
        tap_url = BASE_TAP + path
        best_score, best_db = 0, None

        # Look up by exact name
        for key in [normalize(tap_name) if tap_name else "", normalize(slug.replace("-", " "))]:
            if not key:
                continue
            for db_f in db.get(key, []):
                s = score_match(db_f["name"], tap_name, slug)
                if s > best_score:
                    best_score, best_db = s, db_f

        # Fallback : scan partiel
        if not best_db:
            sl_n = normalize(slug.replace("-", " "))
            for db_key, db_list in db.items():
                if len(db_key) >= 4 and db_key in sl_n:
                    for db_f in db_list:
                        s = score_match(db_f["name"], tap_name, slug)
                        if s > best_score:
                            best_score, best_db = s, db_f
                    if best_score >= args.min_score:
                        break

        if best_score >= args.min_score and best_db:
            db_id = best_db["id"]
            if db_id not in matched or best_score > matched[db_id][3]:
                matched[db_id] = (db_id, best_db["name"], tap_url, best_score)

    matched_list = sorted(matched.values(), key=lambda x: -x[3])

    # Ecriture
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# Tapology ranking URLs — {len(matched_list)} fighters\n\n")
        for db_id, db_name, tap_url, score in matched_list:
            f.write(f"{tap_url}  # {db_name} (score={score}, id={db_id})\n")

    print(f"\n{'='*60}")   # summary
    print(f"  MATCHES (>= {args.min_score}) : {len(matched_list)}")  # ex: 80/100
    print(f"{'='*60}")
    print(f"\n  File: {out.resolve()}")
    if matched_list:
        print(f"\n  NEXT STEP:")
        print(f"  py scrape_batch.py --file {args.out} --yes")
        print(f"  Estimate: ~{len(matched_list)*2.8/60:.0f} min")


if __name__ == "__main__":
    main()
