"""
fill the gaps in the database using the ESPN API (JSON, fast, no Cloudflare)

Why: Tapology goes through a real Chrome (2-4 pages/minute, captchas) whereas
ESPN serves profiles as JSON at ~10 per second, with no blocking. An ESPN profile
gives: date of birth, height, reach, stance, nationality, photo and record.
That is exactly what is missing the most (birth date and reach mostly).

What it does NOT do:
  - it never touches the record (record_total_*) nor the history: the source
    hierarchy stays Tapology > UFC Stats > the rest.
    Record discrepancies are only REPORTED.
  - it only fills EMPTY columns, it never overwrites anything.
  - it writes nothing without --commit.

Usage:
    py scraper/espn_enrichir.py                      # UFC, dry run (writes nothing)
    py scraper/espn_enrichir.py --commit             # apply
    py scraper/espn_enrichir.py --league all         # all 38,000 ESPN MMA athletes
    py scraper/espn_enrichir.py --new                # list those we do not have yet
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

import db_connection as db   # type: ignore  # noqa: E402

CORE = "http://sports.core.api.espn.com/v2/sports/mma"
LIGUES = {"ufc": f"{CORE}/leagues/ufc/athletes",
          "pfl": f"{CORE}/leagues/pfl/athletes",
          "bellator": f"{CORE}/leagues/bellator/athletes",
          "all": f"{CORE}/athletes"}
# our nationality vocabulary (ESPN gives short codes)
PAYS = {
    "USA": "United States", "United States": "United States", "BRA": "Brazil", "Brazil": "Brazil",
    "RUS": "Russia", "Russia": "Russia", "GBR": "United Kingdom", "England": "United Kingdom",
    "CAN": "Canada", "Canada": "Canada", "MEX": "Mexico", "Mexico": "Mexico",
    "FRA": "France", "France": "France", "JPN": "Japan", "Japan": "Japan",
    "POL": "Poland", "Poland": "Poland", "AUS": "Australia", "Australia": "Australia",
    "IRL": "Ireland", "Ireland": "Ireland", "NZL": "New Zealand", "New Zealand": "New Zealand",
    "CHN": "China", "China": "China", "KOR": "South Korea", "South Korea": "South Korea",
    "GEO": "Georgia", "Georgia": "Georgia", "UKR": "Ukraine", "Ukraine": "Ukraine",
    "KAZ": "Kazakhstan", "Kazakhstan": "Kazakhstan", "NGA": "Nigeria", "Nigeria": "Nigeria",
    "NLD": "Netherlands", "Netherlands": "Netherlands", "SWE": "Sweden", "Sweden": "Sweden",
    "ESP": "Spain", "Spain": "Spain", "ITA": "Italy", "Italy": "Italy",
    "GER": "Germany", "Germany": "Germany", "ARG": "Argentina", "Argentina": "Argentina",
}

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (compatible; mma-scraper/1.0)"


def cle(nom) -> str:
    s = unicodedata.normalize("NFKD", str(nom or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def get(url, essais=3):
    for i in range(essais):
        try:
            r = S.get(url, timeout=25)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(0.4 * (i + 1))
    return None


def liste_athletes(ligue: str, limite: int | None):
    """all the profiles of an ESPN league (or all MMA), in parallel."""
    base = LIGUES[ligue]
    premiere = get(f"{base}?limit=1000")
    if not premiere:
        return []
    pages = premiere.get("pageCount") or 1
    refs = [it["$ref"] for it in premiere.get("items") or []]
    for p in range(2, pages + 1):
        d = get(f"{base}?limit=1000&page={p}")
        refs += [it["$ref"] for it in (d or {}).get("items") or []]
        if limite and len(refs) >= limite:
            break
    if limite:
        refs = refs[:limite]
    print(f"   {len(refs)} profiles to read from ESPN ({ligue})...")
    fiches = []
    with ThreadPoolExecutor(max_workers=6) as pool:   # 6: fast without hammering ESPN
        for i, f in enumerate(pool.map(get, refs), 1):
            if f:
                fiches.append(f)
            if i % 500 == 0:
                print(f"      {i}/{len(refs)}")
    return fiches


def pouces_vers_pieds(p) -> str | None:
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    return f"{int(p // 12)}'{int(round(p % 12))}\""


def date_naissance(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def propositions(fiche, row, avec_stance: bool) -> dict:
    """what we can fill in WITHOUT overwriting anything."""
    p = {}
    dob = date_naissance(fiche.get("dateOfBirth"))
    if dob and not row["date_of_birth"]:
        p["date_of_birth"] = dob
    if fiche.get("height") and not row["height_inches"]:
        p["height_inches"] = pouces_vers_pieds(fiche["height"])
    if fiche.get("reach") and not row["reach_inches"]:
        p["reach_inches"] = f'{float(fiche["reach"]):.1f}"'
    if avec_stance and (fiche.get("stance") or {}).get("text") and not row["stance"]:
        p["stance"] = fiche["stance"]["text"]
    pays = PAYS.get(str(fiche.get("citizenship") or "").strip())
    if pays and not row["nationality"]:
        p["nationality"] = pays
    photo = (fiche.get("headshot") or {}).get("href")
    if photo and not row["photo_url"] and not row["photo_thumbnail_url"]:
        p["photo_url"] = photo
    return {k: v for k, v in p.items() if v}


def main():
    ap = argparse.ArgumentParser(description="Fill the gaps of the database from the ESPN API")
    ap.add_argument("--league", default="ufc", choices=list(LIGUES), help="ufc (default), pfl, bellator, all")
    ap.add_argument("--limit", type=int, default=None, help="max number of ESPN profiles to read")
    ap.add_argument("--commit", action="store_true", help="actually write to the database")
    ap.add_argument("--with-stance", action="store_true",
                    help="also fill the stance (warning: the stance column already contains STYLES from UFC-FR)")
    ap.add_argument("--new", action="store_true", help="list the ESPN athletes missing from our database")
    ap.add_argument("--records", action="store_true",
                    help="report the fighters for whom ESPN knows MORE wins than we do (1 extra request each)")
    args = ap.parse_args()

    print("=" * 62)
    print("  enrichment through the ESPN API")
    print("=" * 62)

    conn = db.get_connection()
    if not conn:
        print("No database connection (check .env)")
        return 1
    cur = db.get_cursor(conn)
    cur.execute("""SELECT id, name, date_of_birth, height_inches, reach_inches, stance,
                          nationality, photo_url, photo_thumbnail_url, data_source,
                          record_total_wins, record_total_losses
                     FROM fighters""")
    rows = cur.fetchall()
    par_nom = {}
    for r in rows:
        par_nom.setdefault(cle(r["name"]), []).append(r)
    print(f"\n1) {len(rows)} fighters in the database")

    print(f"\n2) reading ESPN ({args.league})...")
    t0 = time.time()
    fiches = liste_athletes(args.league, args.limit)
    print(f"   {len(fiches)} profiles read in {time.time() - t0:.0f}s")

    maj, inconnus, ambiguous, ecarts_bilan = [], [], 0, []
    compte = {}
    for f in fiches:
        nom = f.get("displayName") or f.get("fullName")
        cands = par_nom.get(cle(nom), [])
        if not cands:
            inconnus.append(f)
            continue
        dob = date_naissance(f.get("dateOfBirth"))
        if len(cands) > 1:
            # namesakes: settle it by date of birth if we have it on both sides
            exacts = [c for c in cands if c["date_of_birth"] and dob and c["date_of_birth"] == dob]
            if len(exacts) != 1:
                ambiguous += 1
                continue
            cands = exacts
        row = cands[0]
        # same name but date of birth more than a year apart: not the same person
        if row["date_of_birth"] and dob and abs((row["date_of_birth"] - dob).days) > 366:
            ambiguous += 1
            continue
        p = propositions(f, row, args.with_stance)
        if p:
            maj.append((row["id"], row["name"], p, f.get("$ref")))
            for k in p:
                compte[k] = compte.get(k, 0) + 1
        # The record is NEVER written (source hierarchy).
        # Note: the ESPN record is the LEAGUE's (UFC), not the career's
        # -> it is normally SMALLER than ours. So we only report the abnormal case:
        # ESPN counts more wins than we do,
        # which means we are missing fights.
        if args.records and f.get("records") and row["record_total_wins"] is not None:
            rec = get((f.get("records") or {}).get("$ref", ""))
            for it in ((rec or {}).get("items") or [])[:1]:
                stats = {x.get("name"): x.get("value") for x in it.get("stats") or []}
                w, l = stats.get("wins"), stats.get("losses")
                if w is not None and int(w) > (row["record_total_wins"] or 0):
                    ecarts_bilan.append((row["name"], f"{row['record_total_wins']}-{row['record_total_losses']}",
                                         f"{int(w)}-{int(l or 0)}"))

    print(f"\n3) matches: {len(maj)} fighters to complete | {len(inconnus)} unknown to us | {ambigus} ambiguous")
    for k, n in sorted(compte.items(), key=lambda kv: -kv[1]):
        print(f"      {k:22s} {n:5d}")
    if ecarts_bilan:
        print(f"\n   {len(ecarts_bilan)} fighters for whom ESPN knows MORE wins than we do "
              f"(we are missing fights; nothing is written) - examples:")
        for nom, a, b in ecarts_bilan[:8]:
            print(f"      {nom:28s} base {a:8s} espn {b}")

    if args.new:
        sortie = ROOT / "data" / "scraper_output" / "reports" / "espn_new.txt"
        sortie.parent.mkdir(parents=True, exist_ok=True)
        with open(sortie, "w", encoding="utf-8") as fh:
            fh.write("# ESPN athletes missing from our database (name | ESPN profile)\n")
            for f in inconnus:
                fh.write(f"{f.get('displayName')} | {f.get('$ref', '').split('?')[0]}\n")
        print(f"\n   -> {sortie.relative_to(ROOT)} ({len(inconnus)} lines)")

    if not args.commit:
        print("\n4) DRY RUN (--commit not specified): nothing is written. Examples:")
        for _, nom, p, _ in maj[:8]:
            print(f"      {nom:28s} {p}")
        conn.close()
        return 0

    print(f"\n4) writing {len(maj)} fighters...")
    n = 0
    for fid, _nom, p, ref in maj:
        sets = ", ".join(f"{k} = %s" for k in p)
        vals = list(p.values())
        # keep track of the source
        cur.execute(f"UPDATE fighters SET {sets}, data_source = "
                    f"CASE WHEN data_source IS NULL THEN %s WHEN data_source LIKE %s THEN data_source "
                    f"ELSE data_source || ' | ' || %s END WHERE id = %s",
                    vals + [ref, "%espn%", ref, fid])
        n += 1
    conn.commit()
    conn.close()
    print(f"   OK COMMIT: {n} fighters completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
