"""
collect_tapology_selenium.py
Tapology collector using SELENIUM (a real Chrome).
Nearly unblockable: Tapology sees a genuine human browser.
Slower (~2-4 fighters/min) but 100% reliable.

REQUIREMENTS
    pip install selenium webdriver-manager
    (Chrome must be installed on the machine)

USAGE
    py collect_tapology_selenium.py                 # everyone, auto-resume
    py collect_tapology_selenium.py --limit 200
    py collect_tapology_selenium.py --headless      # no visible window
    py collect_tapology_selenium.py --resume

TIP: leave the Chrome window open, the script works inside it.
If Tapology asks for a captcha, solve it by hand ONCE, then
the script carries on (cookies are kept).

OUTPUT
    tapology_selenium_urls.txt
    tapology_selenium_progress.txt
"""

import re
import sys
import time
import random
import unicodedata
import argparse
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
except ImportError:
    print("ERROR: pip install selenium webdriver-manager")
    sys.exit(1)

ROOT = Path(__file__).parent.parent  # repository root
SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))
from db_connection import get_connection

BASE_TAP = "https://www.tapology.com"
PROGRESS_FILE = ROOT / "data/scraper_output/tapology/tapology_selenium_progress.txt"
URLS_FILE     = ROOT / "data/scraper_output/tapology/tapology_selenium_urls.txt"
NOMATCH_FILE  = ROOT / "data/scraper_output/tapology/tapology_selenium_nomatch.txt"

PROFILE_RE = re.compile(r'/fightcenter/fighters/(\d+)-([a-z0-9\-]+)')


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def name_score(name: str, slug: str) -> int:
    our_n, slug_n = normalize(name), normalize(slug)
    if our_n == slug_n:  return 100
    if our_n in slug_n:  return 90
    parts = name.strip().split()
    if len(parts) >= 2:
        first, last = normalize(parts[0]), normalize(parts[-1])
        if first in slug_n and last in slug_n: return 80
        if len(last) >= 4 and last in slug_n:  return 60
    return 0


def make_driver(headless: bool):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver


def search(driver, name: str, min_score: int) -> tuple[str | None, int]:
    term = name.replace(" ", "+")
    driver.get(f"{BASE_TAP}/search?term={term}&type=fighters")
    time.sleep(random.uniform(1.0, 2.0))
    html = driver.page_source
    best_url, best_score = None, 0
    for fid, slug in PROFILE_RE.findall(html):
        s = name_score(name, slug)
        if s > best_score:
            best_score = s
            best_url = f"{BASE_TAP}/fightcenter/fighters/{fid}-{slug}"
    return (best_url, best_score) if best_score >= min_score else (None, best_score)


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
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  collect_tapology_selenium.py")
    print("=" * 60)

    fighters = load_fighters(args.force)
    done = set()
    if args.resume and PROGRESS_FILE.exists():
        done = set(PROGRESS_FILE.read_text(encoding="utf-8").splitlines())
        print(f"  Resuming: {len(done)} already processed")
    fighters = [f for f in fighters if f["name"] not in done]
    if args.limit:
        fighters = fighters[:args.limit]
    print(f"  To process: {len(fighters)} fighters")

    driver = make_driver(args.headless)
    # Load the home page once to establish the cookies
    driver.get(BASE_TAP)
    time.sleep(3)

    found = nomatch = 0
    prog_f = PROGRESS_FILE.open("a", encoding="utf-8")
    urls_f = URLS_FILE.open("a", encoding="utf-8")
    nom_f  = NOMATCH_FILE.open("a", encoding="utf-8")

    try:
        for i, f in enumerate(fighters, 1):
            name = f["name"]
            try:
                url, score = search(driver, name, args.min_score)
            except Exception as e:
                print(f"  [ERR] {name}: {e}")
                continue

            prog_f.write(name + "\n"); prog_f.flush()
            if url:
                found += 1
                urls_f.write(f"{url}  # {name} (score={score}, id={f['id']})\n"); urls_f.flush()
                status = f"OK {score}"
            else:
                nomatch += 1
                nom_f.write(name + "\n"); nom_f.flush()
                status = "--"

            if i <= 10 or i % 20 == 0 or url:
                pct = i / len(fighters) * 100
                print(f"  [{i:5}/{len(fighters)} {pct:4.0f}%]  {status:8}  {name}")

            time.sleep(random.uniform(0.8, 2.0))
    except KeyboardInterrupt:
        print("\n  Interrupted. Run again with --resume.")
    finally:
        prog_f.close(); urls_f.close(); nom_f.close()
        driver.quit()

    print(f"\n  FOUND: {found}  |  NOMATCH: {nomatch}")
    print(f"  URLs: {URLS_FILE.resolve()}")
    print(f"\n  py scrape_batch.py --file tapology_selenium_urls.txt --yes")


if __name__ == "__main__":
    main()
