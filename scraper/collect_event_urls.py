"""
collect_event_urls.py
Collect Tapology EVENT URLs from the official SITEMAPS.

Why sitemaps and not the fightcenter?
  - /fightcenter?page=N is rendered in JS and IGNORES the page parameter
    (it always returns the same ~33 events). Pagination is impossible.
  - Tapology exposes ~46 event sitemaps: https://www.tapology.com/events/sitemap_N.xml
    Each one lists ~1000-3700 event URLs in the form
        https://www.tapology.com/fightcenter/events/<id-slug>
    The sitemaps are CHRONOLOGICAL: sitemap.xml = oldest (2008),
    the highest non-empty number = most recent + upcoming.

Strategy (--newest, default):
  1. Probe downward from --probe-max (default 60) to find the first
     NON-EMPTY sitemap (= the most recent one).
  2. Then walk down sitemap by sitemap (recent -> old), accumulating
     URLs until --max-urls is reached.

Playwright (Chromium) is used because the sitemaps return 403 to direct
requests (bot protection) but 200 through browser navigation.

USAGE
    # ~10k most recent events (multi-league, past + upcoming)
    py scraper/collect_event_urls.py --max-urls 10000

    # Explicit sitemap range
    py scraper/collect_event_urls.py --from-sitemap 30 --to-sitemap 37

    # Everything (~50k)
    py scraper/collect_event_urls.py --max-urls 100000

OUTPUT
    data/scraper_output/tapology/event_urls.txt   (1 URL per line, deduplicated)

NEXT STEP
    py scraper/scrape_events.py --file data/scraper_output/tapology/event_urls.txt --commit --yes
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "scraper_output" / "tapology" / "event_urls.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SITEMAP_TMPL = "https://www.tapology.com/events/sitemap{suffix}.xml"
EVENT_RE = re.compile(r"https?://www\.tapology\.com/fightcenter/events/[^\s<>\"]+")


def sitemap_url(n: int) -> str:
    """sitemap.xml for n=1, sitemap_N.xml otherwise."""
    return SITEMAP_TMPL.format(suffix="" if n <= 1 else f"_{n}")


def fetch_sitemap_urls(page, n: int) -> list[str]:
    """Return the list of event URLs of a sitemap (empty if missing/empty)."""
    sm = sitemap_url(n)
    try:
        page.goto(sm, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(1.3)  # let the XML viewer render
        body = page.inner_text("body")
    except Exception as e:  # noqa: BLE001
        print(f"  [sitemap_{n}] ERR {type(e).__name__}: {e}")
        return []
    urls = EVENT_RE.findall(body)
    # deduplicate while keeping order
    seen, out = set(), []
    for u in urls:
        u = u.rstrip("/")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def find_newest_sitemap(page, probe_max: int) -> int:
    """Probe downward from probe_max and return the first NON-EMPTY sitemap number."""
    print(f"  Looking for the most recent sitemap (probing from {probe_max} -> 1)...")
    for n in range(probe_max, 0, -1):
        urls = fetch_sitemap_urls(page, n)
        if urls:
            print(f"  -> sitemap_{n} non-empty ({len(urls)} urls) = the most recent")
            return n, urls
        print(f"  -> sitemap_{n} empty")
    return 0, []


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect Tapology event URLs (sitemaps)")
    ap.add_argument("--max-urls", type=int, default=10_000,
                    help="Max number of URLs to collect (default 10000)")
    ap.add_argument("--probe-max", type=int, default=60,
                    help="Max sitemap number to probe to find the most recent one")
    ap.add_argument("--from-sitemap", type=int, default=None,
                    help="Force a range: lowest number (oldest)")
    ap.add_argument("--to-sitemap", type=int, default=None,
                    help="Force a range: highest number (most recent)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output file")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: pip install playwright && py -m playwright install chromium")
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 62)
    print("  collect_event_urls.py -- sitemaps Tapology")
    print(f"  Target: ~{args.max_urls} URLs (from most recent to oldest)")
    print("=" * 62)

    # Filter: only URLs with a numeric ID at the start of the slug
    # Ex : /fightcenter/events/140934-ufc-fight-night-... → OK
    # Ex : /fightcenter/events/ufc-93-franklin-... → SKIP (redirect/bloque)
    import re as _re
    NUMERIC_SLUG = _re.compile(r"/fightcenter/events/\d+-.+")

    all_urls: list[str] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA,
                                  extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        page = ctx.new_page()

        if args.from_sitemap and args.to_sitemap:
            order = list(range(args.to_sitemap, args.from_sitemap - 1, -1))  # recent -> ancien
        else:
            newest, first_urls = find_newest_sitemap(page, args.probe_max)
            if not newest:
                print("  No non-empty sitemap found."); browser.close(); return 1
            # We already have the URLs of the most recent one (filtered)
            for u in first_urls:
                if NUMERIC_SLUG.search(u) and u not in seen:
                    seen.add(u); all_urls.append(u)
            print(f"  [sitemap_{newest}] +{len(all_urls)} valid / {len(first_urls)} raw (total {len(all_urls)})")
            order = list(range(newest - 1, 0, -1))

        for n in order:
            if len(all_urls) >= args.max_urls:
                break
            urls = fetch_sitemap_urls(page, n)
            added = 0
            for u in urls:
                if NUMERIC_SLUG.search(u) and u not in seen:
                    seen.add(u); all_urls.append(u); added += 1
            print(f"  [sitemap_{n}] +{added} valid / {len(urls)} raw (total {len(all_urls)})")
            time.sleep(1.0)

        browser.close()

    all_urls = all_urls[:args.max_urls]
    with out_path.open("w", encoding="utf-8") as f:
        for u in all_urls:
            f.write(u + "\n")

    print("\n" + "=" * 62)
    print(f"  {len(all_urls)} URLs written -> {out_path}")
    print("=" * 62)
    print("\n  NEXT STEP:")
    print(f"  py scraper/scrape_events.py --file {out_path} --commit --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
