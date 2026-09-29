#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_tapology_urls.py
Extract EVERY already-known Tapology URL from the `data_source` column
of the database (previous scrapes stored the scraped URLs there, pipe-separated).

WHY?
Records (W-L-D) were wrong for lack of a source hierarchy. The fix makes
Tapology authoritative, BUT a fighter whose stored history comes from
FightMatrix keeps its FM record until its Tapology page is (re)scraped.
This script generates the list of Tapology URLs to (re)scrape -> feed it to the batch.

PRIORITY: fighters whose fight_history is NOT already Tapology are written
FIRST (they are the ones with a potentially wrong record). The others
follow (freshness re-scrape). `--missing-only` keeps only the first group.

USAGE
    py scraper/extract_tapology_urls.py
    py scraper/extract_tapology_urls.py --missing-only
    py scraper/extract_tapology_urls.py --out data/scraper_output/tapology/rescrape.txt

OUTPUT (default)
    data/scraper_output/tapology/rescrape_tapology_urls.txt   (1 URL per line)

THEN (you) -- scrape behind Cloudflare WITHOUT a VPN using a real Chrome:
    set SCRAPER_SELENIUM=1                                  (Windows: cmd)
    $env:SCRAPER_SELENIUM = "1"                             (PowerShell)
    py scraper/scrape_batch.py --file <this_file>.txt --yes
    # solve the captcha BY HAND once, the script then carries on alone.
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
from db_connection import get_connection  # noqa: E402

# Capture a Tapology profile URL (not the search/listing pages).
TAP_URL_RE = re.compile(r"https?://(?:www\.)?tapology\.com/fightcenter/fighters/[^\s|]+", re.I)


def fh_is_tapology(fh_raw) -> bool:
    # True if the stored fight_history was scraped from Tapology
    if not fh_raw:
        return False
    try:
        fh = json.loads(fh_raw) if isinstance(fh_raw, str) else fh_raw
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(fh, dict) and (fh.get("source") or "").strip().lower() == "tapology"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "data" / "scraper_output" / "tapology" / "rescrape_tapology_urls.txt"))
    ap.add_argument("--missing-only", action="store_true", default=True,
                    help="Only keep the fighters whose record is NOT already Tapology (default)")
    ap.add_argument("--all", dest="missing_only", action="store_false",
                    help="Also include the fighters that already have Tapology (freshness re-scrape)")
    args = ap.parse_args()

    conn = get_connection()
    if not conn:
        print("/!\\ Database connection failed (check .env)")
        sys.exit(1)

    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name, data_source, fight_history
        FROM fighters
        WHERE data_source ILIKE %s
        ORDER BY id
        """,
        ("%tapology.com%",),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    priority, fresh = [], []     # priority = record not yet from Tapology
    seen = set()                  # avoid writing the same URL twice
    for _id, name, dsrc, fh in rows:
        # pick the Tapology profile URL from the data_source column
        m = TAP_URL_RE.search(dsrc or "")
        if not m:
            continue
        url = m.group(0).rstrip("/")
        if url in seen:
            continue
        seen.add(url)
        # if the history already comes from Tapology -> just freshness (fresh),
        # otherwise the record is potentially wrong -> re-scrape first
        (fresh if fh_is_tapology(fh) else priority).append(url)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    urls = priority if args.missing_only else priority + fresh
    out_path.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")

    print(f"  Fighters with a Tapology URL in data_source: {len(seen)}")
    print(f"  -> record NOT yet Tapology (priority)      : {len(priority)}")
    print(f"  -> already Tapology (freshness re-scrape)  : {len(fresh)}")
    print(f"  Wrote {len(urls)} URL(s) -> {out_path}")
    print()
    print("  Scrape (real Chrome, no VPN):")
    print('    PowerShell : $env:SCRAPER_SELENIUM = "1"')
    print(f"    py scraper/scrape_batch.py --file {out_path} --yes")


if __name__ == "__main__":
    main()
