"""
scrape_ufc_events.py
MMA events scraper using ESPN's public API (free, structured JSON).

Why ESPN rather than Tapology?
  - No Cloudflare, no captcha, no IP ban: nothing is worked around.
  - Everything needed: winner, method (KO/TKO/SUB/DEC), round, card segment
    (Main Card / Prelims), weight class.
  - Available a few minutes after the fight ends.
Warning: this is a public but UNDOCUMENTED API, not a licensed feed.
So keep to polite, low-volume usage.

EVERYTHING goes through the "core API":
  - events list: .../mma/leagues/{league}/events?dates=YEAR
  - event detail: .../mma/leagues/{league}/events/{id}/competitions
The scoreboard (site.api.espn.com) has returned 403 since the end of 2026, it is no longer used.

Organizations covered: UFC, PFL, Bellator (--league).

This scraper REUSES the scrape_events.py pipeline:
  upsert_event (cross-source dedup) + upsert_event_fights + update_fighters_from_event.
  -> An ESPN event is automatically merged with its Tapology/UFC-FR equivalent
     already in the database (same date + same fighters), and updates the
     fighters' records AND their fight_history (new fight inserted at the top).

WHY THIS IS THE RIGHT WAY TO UPDATE:
  Re-scraping the 12,000 profiles is pointless: height, reach or date of
  birth never change. What changes is that a fighter has fought.
  One call per event updates the two fighters involved -> a few requests
  a week instead of 12,000, with no VPN and no ban.

USAGE
    py scraper/scrape_ufc_events.py --recent               # last 30 days (dry run)
    py scraper/scrape_ufc_events.py --recent --commit      # write to the database
    py scraper/scrape_ufc_events.py --recent --league pfl --commit
    py scraper/scrape_ufc_events.py --since 2025-01-01 --until 2025-12-31 --commit
    py scraper/scrape_ufc_events.py --event 600058949 --commit   # 1 event by its ESPN ID
    py scraper/scrape_ufc_events.py --upcoming --commit    # upcoming cards (no results)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRAPERS = Path(__file__).parent
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))

import db_connection as db  # noqa: E402
# Reuse the whole existing pipeline (dedup, upsert, fighter updates)
import scrape_events as SE  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The scoreboard (site.api.espn.com) has returned 403 since the end of 2026: everything goes
# through the core API, which also lists the events and which does respond.
CORE = "https://sports.core.api.espn.com/v2/sports/mma/leagues/{ligue}"
CORE_EVENTS = CORE + "/events?dates={annee}&limit=1000"
CORE_EVENT = CORE + "/events/{eid}"
CORE_COMPS = CORE + "/events/{eid}/competitions"

# the organizations covered by ESPN that we know how to handle. the key is the one in
# the ESPN url, the value is what we write in the promotion column.
LIGUES = {"ufc": "UFC", "pfl": "PFL", "bellator": "BELLATOR"}

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _get(url: str, retries: int = 3) -> dict | None:
    """GET JSON with a light retry."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            if attempt == retries - 1:
                print(f"    [ERROR] GET {url[:70]}... : {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


# Mapping methode ESPN -> code interne (compatible upsert_event_fights)
def _map_method(result_name: str | None) -> str | None:
    if not result_name:
        return None
    n = result_name.lower()
    if "ko" in n or "tko" in n or "knockout" in n:
        return "KO/TKO"
    if "sub" in n:
        return "SUB"
    if "dec" in n:
        return "DEC"
    if "draw" in n:
        return "DRAW"
    if "dq" in n or "disqual" in n:
        return "DQ"
    if "no contest" in n or n == "nc":
        return "NC"
    return result_name.upper()


def _card_section(segment_name: str | None) -> str:
    if not segment_name:
        return "Main Card"
    s = segment_name.lower()
    if "early" in s:
        return "Early Prelims"
    if "prelim" in s:
        return "Prelims"
    return "Main Card"


def _wc_from_type(type_abbrev: str | None, type_id: str | None) -> tuple[str, int | None]:
    """'W Strawweight' / 'Lightweight' -> clean name. No reliable lbs via the ESPN type."""
    if not type_abbrev:
        return "", None
    wc = type_abbrev.replace("W ", "Women's ").strip()
    return wc, None


def fetch_event_ids(d1: date, d2: date, ligue: str = "ufc") -> list[dict]:
    """
    List the events between two dates, through the core API.

    The core API only filters by YEAR (?dates=2025), so we query each
    year of the range and then filter ourselves. Each element of the list is
    a simple link: we fetch the event's name and date behind it.

    The status is not given by the list: a past event is necessarily
    finished, a same-day or upcoming event is not yet (it will be picked up
    on the next run).
    """
    aujourdhui = date.today()
    out = []
    for annee in range(d1.year, d2.year + 1):
        data = _get(CORE_EVENTS.format(ligue=ligue, annee=annee))
        if not data:
            continue
        for item in data.get("items", []):
            ref = item.get("$ref")
            if not ref:
                continue
            ev = _get(ref.replace("http://", "https://"))
            if not ev:
                continue
            jour = (ev.get("date") or "")[:10]
            if not jour:
                continue
            try:
                j = date.fromisoformat(jour)
            except ValueError:
                continue
            if not (d1 <= j <= d2):
                continue
            out.append({
                "id":     str(ev.get("id") or ""),
                "name":   ev.get("name") or "",
                "date":   jour,
                "status": "STATUS_FINAL" if j < aujourdhui else "",
                "raw":    ev,
            })
    out.sort(key=lambda e: e["date"])
    return out


def parse_event_from_core(eid: str, ev_meta: dict, ligue: str = "ufc") -> tuple[dict, list[dict]]:
    """
    Build (meta, bouts) in the scrape_events pipeline format from the core API.
    The core API gives the precise method + cardSegment; we resolve the athletes
    through their $ref (1 request each, cached locally).
    """
    # We do NOT store the ESPN URL in tapology_url (UNIQUE field reserved for Tapology).
    # The espn-{eid} slug serves as the ESPN identifier; find_matching_event does the rest.
    meta = {
        "tapology_url": None,
        "slug":         f"espn-{eid}",
        "name":         ev_meta.get("name") or "",
        "date":         ev_meta.get("date"),
        "promotion":    LIGUES.get(ligue, ligue.upper()),
        "venue": None, "city": None, "country": None,
        "poster_url": None,
    }
    is_final = ev_meta.get("status") == "STATUS_FINAL"
    meta["status"] = "completed" if is_final else "upcoming"

    # If the date is missing (--event mode), fetch it through the core API event.
    if not meta.get("date"):
        ev_core = _get(CORE_EVENT.format(ligue=ligue, eid=eid)) or {}
        if ev_core.get("date"):
            meta["date"] = ev_core["date"][:10]
        if not meta.get("name") and ev_core.get("name"):
            meta["name"] = ev_core["name"]
        # status through the core if available
        if ev_core.get("status", {}).get("$ref"):
            stc = _get(ev_core["status"]["$ref"]) or {}
            if stc.get("type", {}).get("name") == "STATUS_FINAL":
                meta["status"] = "completed"

    comps_data = _get(CORE_COMPS.format(ligue=ligue, eid=eid))
    if not comps_data:
        return meta, []

    athlete_cache: dict[str, str] = {}

    def _athlete_name(ref: str) -> str:
        if ref in athlete_cache:
            return athlete_cache[ref]
        a = _get(ref) or {}
        nm = a.get("displayName") or a.get("fullName") or ""
        athlete_cache[ref] = nm
        return nm

    bouts: list[dict] = []
    items = comps_data.get("items", [])
    # The core API lists the fights from the last (prelim 1) to the first (main event).
    # We reverse to get the main event first (bout_order 0).
    for comp_ref in reversed(items):
        comp = _get(comp_ref["$ref"]) if "$ref" in comp_ref else comp_ref
        if not comp:
            continue

        seg = (comp.get("cardSegment") or {}).get("description") \
            or (comp.get("cardSegment") or {}).get("name")
        card_section = _card_section(seg)

        type_info = comp.get("type") or {}
        wc, wc_lbs = _wc_from_type(type_info.get("abbreviation"), type_info.get("id"))

        # Status + methode
        method = None
        round_num = None
        bout_status = "upcoming"
        st_ref = (comp.get("status") or {}).get("$ref")
        if st_ref:
            st = _get(st_ref) or {}
            stype = st.get("type", {})
            if stype.get("name") == "STATUS_FINAL":
                bout_status = "completed"
            result = st.get("result") or {}
            method = _map_method(result.get("displayName") or result.get("name"))
            round_num = st.get("period") or None

        # Competitors
        f1_name = f2_name = ""
        winner_name = None
        comp_list = comp.get("competitors", [])
        # order 1 = "red" corner/favorite, 2 = "blue". We map order->slot.
        ordered = sorted(comp_list, key=lambda c: c.get("order", 99))
        for slot, cm in enumerate(ordered):
            ath_ref = (cm.get("athlete") or {}).get("$ref")
            name = _athlete_name(ath_ref) if ath_ref else ""
            if slot == 0:
                f1_name = name
            elif slot == 1:
                f2_name = name
            if cm.get("winner"):
                winner_name = name

        if not f1_name or not f2_name:
            continue

        bouts.append({
            "card_section":      card_section,
            "bout_label":        card_section,
            "bout_order":        len(bouts),
            "fighter1_name":     f1_name,
            "fighter2_name":     f2_name,
            "fighter1_record":   None,
            "fighter2_record":   None,
            "fighter1_ufc_rank": None,
            "fighter2_ufc_rank": None,
            "weight_class":      wc,
            "weight_lbs":        wc_lbs,
            "rounds":            None,
            "is_title_fight":    "title" in (type_info.get("abbreviation") or "").lower(),
            "title_text":        "",
            "status":            bout_status if bout_status != "upcoming" else meta["status"],
            "winner_name":       winner_name,
            "method":            method,
            "method_detail":     None,
            "round_num":         round_num,
            "time_str":          None,
        })

    return meta, bouts


def main() -> int:
    p = argparse.ArgumentParser(description="Scraper events UFC via API officielle ESPN")
    p.add_argument("--commit", action="store_true", help="Write to the database (default: dry-run)")
    p.add_argument("--verbose", "-v", action="store_true", help="Bout details")
    p.add_argument("--recent", action="store_true", help="Events of the last 30 days")
    p.add_argument("--upcoming", action="store_true", help="Events of the next 60 days")
    p.add_argument("--since", help="Date debut (YYYY-MM-DD)")
    p.add_argument("--until", help="End date (YYYY-MM-DD)")
    p.add_argument("--event", help="A single event by its ESPN ID")
    p.add_argument("--league", default="ufc", choices=sorted(LIGUES),
                   help="Organization to scrape (default: ufc)")
    args = p.parse_args()

    print("=" * 62)
    print("  Scraper Events via l'API publique ESPN")
    print(f"  Organization: {LIGUES[args.league]}")
    print(f"  Mode : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("=" * 62)

    # Determine the date range
    if args.event:
        events = [{"id": args.event, "name": None, "date": None, "status": None}]
        # Let parse_event_from_core deduce what it can.
    else:
        if args.recent:
            d2 = date.today()
            d1 = d2 - timedelta(days=30)
        elif args.upcoming:
            d1 = date.today()
            d2 = d1 + timedelta(days=60)
        elif args.since:
            d1 = date.fromisoformat(args.since)
            d2 = date.fromisoformat(args.until) if args.until else date.today()
        else:
            print("  Specify --recent, --upcoming, --since, or --event. (--help)")
            return 1
        print(f"\n  {LIGUES[args.league]} from {d1} to {d2}")
        events = fetch_event_ids(d1, d2, args.league)

    if not events:
        print("  No event found."); return 0
    print(f"  {len(events)} events to process\n")

    conn = db.get_connection()
    if not conn:
        print("  [ERROR] Database connection impossible."); return 1
    SE._ensure_event_fights_date_column(conn)

    print("  Chargement index fighters...")
    idx = SE.load_fighter_index(conn)
    print(f"  {len(idx)} fighters indexes\n")

    stats = {"INSERT": 0, "UPDATE": 0, "SKIP": 0, "FAIL": 0}
    total_fighters_upd = 0
    t0 = time.time()

    for i, ev in enumerate(events, 1):
        eid = ev["id"]
        meta, bouts = parse_event_from_core(eid, ev, args.league)
        # If the date is unknown (--event mode), deduce it from the 1st bout via the scoreboard?
        if not meta.get("date") and bouts:
            pass  # ESPN core does not give the event date here; stays None -> skip the update

        n_results = sum(1 for b in bouts if b.get("winner_name"))
        print(f"[{i}/{len(events)}] {meta.get('name') or eid} "
              f"({meta.get('date') or '?'}) : {len(bouts)} bouts, {n_results} results")

        if args.verbose:
            for b in bouts:
                w = f" -> {b['winner_name']}" if b.get("winner_name") else ""
                m = f" ({b['method']})" if b.get("method") else ""
                print(f"     [{b['card_section']:12s}] {b['fighter1_name']} vs {b['fighter2_name']}{w}{m}")

        if not args.commit:
            stats["SKIP"] += 1
            continue

        if not meta.get("date") or not bouts:
            stats["FAIL"] += 1
            continue

        try:
            event_id, op = SE.upsert_event(conn, meta, [], bouts=bouts)
            SE.upsert_event_fights(conn, event_id, bouts, idx, event_date=meta.get("date"))
            n_upd, _ = SE.update_fighters_from_event(conn, bouts, meta, commit=True)
            conn.commit()
            total_fighters_upd += n_upd
            stats[op] = stats.get(op, 0) + 1
            print(f"     -> {op} event_id={event_id} | {n_upd} fighters updated")
        except Exception as e:  # noqa: BLE001
            print(f"     [ERROR] {e}")
            conn.rollback()
            stats["FAIL"] += 1
        time.sleep(0.3)  # politesse API

    conn.close()
    print("\n" + "=" * 62)
    print(f"  SUMMARY ({time.time()-t0:.0f}s) | fighters updated (cumulative): {total_fighters_upd}")
    for k, v in stats.items():
        if v:
            print(f"   {k:8s} : {v}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
