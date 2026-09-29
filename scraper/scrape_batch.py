"""
scrape_batch.py
BATCH multi-fighter / multi-source scraper for PostgreSQL / Supabase.

WHY THIS SCRIPT EXISTS
Doing 1 fighter at a time with their 4 URLs (1 per site) is slow:
you have to look for the fighter's profile on EACH site individually.
Yet each site has a RANKING (top 100/300/500 of a category) that already
lists the best fighters. It is MUCH faster to scrape a whole ranking at
once, then start again from the next site's ranking.

Sources scraped (1 URL = 1 fighter, the scraper detects the site by domain):
  - FightMatrix  : https://www.fightmatrix.com/fighter-profile/<Name>/<id>
  - Tapology     : https://www.tapology.com/fightcenter/fighters/<id>-<slug>
  - Underground  : https://fighters.mixedmartialarts.com/<Name>:<HEX>
  - UFC-FR       : https://www.ufc-fr.com/combattant-<id>.html

REAL WORKFLOW
1. On FightMatrix, open the Flyweight ranking (top 300).
2. Open all the profiles in tabs (in batches of 100, for example).
3. Copy the URLs (Ctrl+L on each tab) -> paste EVERYTHING into the console.
4. Empty line -> the script INSERTs the 300 fighters into the database.
5. Later (hours, days, weeks later, it does not matter):
   same thing with the Tapology Flyweight ranking (top 300).
   -> For each Tapology URL, the script finds the fighter already in the
      database and does an UPDATE (fills the empty columns, refreshes those
      in ALWAYS_REFRESH). NO duplicate is created.
6. Repeat with Underground, then UFC-FR. THE ORDER DOES NOT MATTER.
   You can even mix 1 site today + another one in 8 days:
   the same fighters get enriched as you go.

ANTI-DUPLICATE MATCHING
On every scrape, we look for the existing fighter in the database through:
  1. normalized name (strip accents + smart quotes + parentheses + lowercase
     + collapse whitespace)
  2. cross-check with date_of_birth if available (cast to date on both sides
     to avoid the str vs datetime.date trap)

5 possible statuses (see the lookup() function):
  - match_name_dob   : perfect match                        -> UPDATE
  - match_name_only  : name-only match (DOB NULL on one side)  -> UPDATE
  - new              : new fighter                          -> INSERT
  - new_homonyme     : namesake with a different DOB        -> INSERT
  - ambiguous        : several candidates and no DOB to decide -> SKIP
                       (we refuse to merge blindly)

Every failed / SKIP / CONFLICT URL is logged in failed_urls.txt
for a manual retry.

USAGE
    py scrape_batch.py
    -> paste all your URLs (1 per line, or several per line)
    -> EMPTY line to validate and start the batch
    -> Q (or quit) to cancel before starting
"""

import sys, time, logging, unicodedata, re
from pathlib import Path

import psycopg2

ROOT = Path(__file__).parent.parent  # repository root
SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))

from scrape_mma import scrape, detect_source
from scrape_mma_supabase import (
    DB_COLUMNS, ALWAYS_REFRESH, _prepare_row,
    # Shared utilities (single source of truth in scrape_mma_supabase.py)
    _to_date, _merge_data_sources, AUTHORITATIVE_SOURCES,
    _recompute_quality_score, _recompute_record_other,
    _recompute_record_total, _recompute_total_fights,
    _recompute_record_from_history, _fh_should_overwrite,
)
from db_connection import get_connection

LOG = logging.getLogger("scrape_many")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

FAILED_LOG    = ROOT / "data/scraper_output/errors/failed_urls.txt"
SLEEP_BETWEEN = 0.5   # sec between each fighter (the scraper already has its own internal sleep)

# Circuit breaker: when Tapology/Cloudflare bans the IP, ALL the following
# requests fall into 503 -> empty name. No point absorbing 400 of them (1h lost):
# beyond N CONSECUTIVE fetch failures, the batch is stopped. Reset as soon as a fetch
# reussit. Override via --max-consecutive-fails (0 = desactive).
MAX_CONSECUTIVE_FAILS = 3


# NAME normalization (batch-specific: in-memory matching)
# the shared utilities (_to_date, authoritative_sources)
# are imported from scrape_mma_supabase.py (single source of truth).

def normalize_name(s: str) -> str:
    """
    Bring all the cosmetic variations of the same name down to a common key.
    Without this, every typo divergence between 2 sources creates a duplicate in the database.

    HANDLED CASES:
      Parens/quotes :    'Tatsuro Taira ("The Best")'  -> "tatsuro taira"
      Apostrophes :      "Sean O'Malley" / "Sean OMalley" / "Sean O'Malley"
                            -> "sean omalley"   (all apostrophe variants)
      Initials :         "T.J. Dillashaw" / "TJ Dillashaw" / "T. J. Dillashaw"
                            -> "tj dillashaw"
      Dot/hyphen :       "Georges St. Pierre" / "Georges St-Pierre"
                            -> "georges st pierre"
      Saint/St :         "Saint Preux" / "St Preux" / "St. Preux"
                            -> "st preux"
      Accents :          "Jose Aldo" / "Jose Aldo" -> "jose aldo"
      Suffix (via DOB):  "Jose Aldo Jr" vs "Jose Aldo" -> different keys but
                         the DOB-subset fallback matches {jose,aldo} in {jose,aldo,jr}

    NOT HANDLED (intentional):
      - Transliterations (Khabib/Habib, Khamzat/Hamzat): too ambiguous, false positives
      - "Sean O Malley" with a literal space: rare, we accept logging a duplicate
      - Suffixes in the key (Jr/Junior/Sr): left to the DOB fallback
    """
    if not s:
        return ""
    # 1) Remove the content between parentheses: "(...)"
    s = re.sub(r"\s*\([^()]*\)\s*", " ", s)
    # 2) Remove the content between paired quotes (ASCII + smart): '"..." / "..." / '...'
    s = re.sub(r"\s*[\"“‘’”«»][^\"“‘’”«»]*[\"“‘’”«»]\s*", " ", s)
    # 3) Remove the remaining apostrophes (all Unicode variants).
    # "Sean O'Malley" / "Sean OMalley" / "Sean Oʼmalley" -> same key.
    #    '=' ASCII, ‘=‘ ’=’ smart, ʼ=ʼ modifier, `=` ´=´
    s = re.sub(r"['‘’ʼ`´]", "", s)
    # 4) Initials with dots: "T.J." / "T. J." / "A.J." -> "TJ" / "AJ"
    # Must run BEFORE the generic replacement of dots with spaces.
    # \s* between the initials to handle "T. J. Dillashaw" (space in the middle).
    s = re.sub(
        r"(?:\b\w\.\s*){2,}",
        lambda m: m.group(0).replace(".", "").replace(" ", "") + " ",
        s,
    )
    # 5) Ponctuation interne restante (point, tiret) -> espace.
    #    Resout "St. Pierre" / "St-Pierre" / "Anne-Marie" / "Jr."
    s = re.sub(r"[.\-]", " ", s)
    # 6) Saint -> St (abbreviation variant, common in MMA).
    # After step 5, "St. Preux" is already "St Preux": only "Saint" is left to normalize.
    s = re.sub(r"\bsaint\b", "st", s, flags=re.IGNORECASE)
    # 7) Strip accents + lowercase + collapse whitespace
    nfkd       = unicodedata.normalize("NFKD", s)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", no_accents.lower().strip())


# in-memory INDEX of the existing fighters

def load_existing_index(cur) -> dict:
    """
    Build THREE indexes:
      - by_name    : {norm_name: [(fighter_id, dob, raw_name), ...]}
      - by_dob     : {date: [(fighter_id, norm_name), ...]}
      - by_src_url : {"fm:157387": fighter_id, "tap:125404": fighter_id, ...}

    by_src_url is the anti-duplicate safety net when FM/Tapology have a DOB that is
    off by a year: the name matches but the DOB differs -> lookup() returns new_homonyme
    and a duplicate is inserted. The URL index catches that case.
    """
    cur.execute("SELECT id, name, date_of_birth FROM fighters")
    by_name: dict = {}
    by_dob: dict = {}
    for fid, name, dob in cur.fetchall():
        if not name:
            continue
        nkey = normalize_name(name)
        if nkey:
            by_name.setdefault(nkey, []).append((fid, dob, name))
        d = _to_date(dob)
        if d:
            by_dob.setdefault(d, []).append((fid, nkey))

    by_src_url: dict = {}
    cur.execute("SELECT id, data_source FROM fighters WHERE data_source IS NOT NULL AND data_source != ''")
    for fid, dsrc in cur.fetchall():
        for part in (dsrc or "").split("|"):
            part = part.strip()
            m = re.search(r"fightmatrix\.com/fighter-profile/[^/]+/(\d+)", part)
            if m:
                by_src_url[f"fm:{m.group(1)}"] = fid
            m = re.search(r"tapology\.com/fightcenter/fighters/(\d+)", part)
            if m:
                by_src_url[f"tap:{m.group(1)}"] = fid
            m = re.search(r"ufc-fr\.com/combattant-(\d+)", part)
            if m:
                by_src_url[f"ufr:{m.group(1)}"] = fid

    return {"by_name": by_name, "by_dob": by_dob, "by_src_url": by_src_url}


def _dob_fallback_match(by_dob: dict, key: str, dob_norm) -> int | None:
    """
    Look for a fighter with the SAME DOB AND a name whose tokens are a subset
    (or superset) of the incoming name. Solves:
      - FM "Jesus Santos Aguilar" vs UFC-FR "Jesus Aguilar"  -> same fighter
      - "Jose Aldo" vs "Jose Aldo Jr"                        -> same fighter
    Does NOT match if the intersection is empty or if only one token is shared
    (e.g. 2 different "Jesus" with a hypothetical same DOB -> stay 2 fighters).
    """
    if not dob_norm or not key:
        return None
    new_tokens = set(key.split())
    if len(new_tokens) < 2:
        return None  # name too short (e.g. a single word) -> too ambiguous
    for fid, cname_key in by_dob.get(dob_norm, []):
        if not cname_key:
            continue
        existing_tokens = set(cname_key.split())
        if len(existing_tokens) < 2:
            continue
        # Match if one is a subset of the other AND at least 2 tokens in common
        common = new_tokens & existing_tokens
        if len(common) >= 2 and (
            new_tokens.issubset(existing_tokens) or
            existing_tokens.issubset(new_tokens)
        ):
            return fid
    return None


def lookup_by_url(index: dict, url: str) -> int | None:
    """
    Look for an existing fighter through its profile URL (FM/tapology/UFC-FR).
    Fallback when name+DOB does not match (wrong DOB on the source side, e.g. FM off by 1 year).
    Return fighter_id or None.
    """
    by_src_url = index.get("by_src_url", {})
    if not url or not by_src_url:
        return None
    for part in url.split("|"):
        part = part.strip()
        m = re.search(r"fightmatrix\.com/fighter-profile/[^/]+/(\d+)", part)
        if m:
            fid = by_src_url.get(f"fm:{m.group(1)}")
            if fid:
                return fid
        m = re.search(r"tapology\.com/fightcenter/fighters/(\d+)", part)
        if m:
            fid = by_src_url.get(f"tap:{m.group(1)}")
            if fid:
                return fid
        m = re.search(r"ufc-fr\.com/combattant-(\d+)", part)
        if m:
            fid = by_src_url.get(f"ufr:{m.group(1)}")
            if fid:
                return fid
    return None


def lookup(index: dict, name: str, dob) -> tuple[int | None, str]:
    """
    Look for an existing fighter through the normalized name + DOB.

    Return (fighter_id, status):
      - (id, "match_name_dob")        -> perfect name+dob match,       UPDATE
      - (id, "match_name_only")       -> name-only match (DOB NULL),   UPDATE
      - (id, "match_name_close_dob")  -> name OK, dob within ±90d,     UPDATE
      - (id, "match_dob_subset")      -> dob OK, name is a subset,     UPDATE
      - (None, "new")                 -> no candidate,                 INSERT
      - (None, "new_homonyme")        -> namesake with a different DOB, INSERT
      - (None, "ambiguous")           -> 2 candidates without a DOB,   SKIP
    """
    by_name = index["by_name"]
    by_dob  = index["by_dob"]

    key = normalize_name(name)
    candidates = by_name.get(key, [])
    dob_norm = _to_date(dob)

    if not candidates:
        # DOB fallback: covers the "Jesus Santos Aguilar" / "Jesus Aguilar" variants
        fid_subset = _dob_fallback_match(by_dob, key, dob_norm)
        if fid_subset is not None:
            return fid_subset, "match_dob_subset"
        return None, "new"

    if dob_norm is not None:
        # 1. exact name+dob match (both sides cast to `date` to avoid str vs date)
        for fid, cdob, _cname in candidates:
            if _to_date(cdob) == dob_norm:
                return fid, "match_name_dob"

        # 2. no dob match -> split off the candidates without a DOB
        candidates_no_dob = [(fid, cdob) for fid, cdob, _ in candidates if cdob is None]

        if candidates_no_dob:
            if len(candidates_no_dob) == 1:
                return candidates_no_dob[0][0], "match_name_only"
            return None, "ambiguous"

        # 3. all the candidates have a DOB, none matches exactly.
        # Fuzzy fallback: same year + gap <= 90 days -> same fighter.
        for fid, cdob, _cname in candidates:
            cdob_norm = _to_date(cdob)
            if cdob_norm and cdob_norm.year == dob_norm.year:
                delta = abs((cdob_norm - dob_norm).days)
                if delta <= 90:
                    return fid, "match_name_close_dob"

        return None, "new_homonyme"

    # No DOB on the incoming side
    if len(candidates) == 1:
        return candidates[0][0], "match_name_only"
    return None, "ambiguous"


def add_to_index(index: dict, name: str, fid: int, dob, data_source: str = ""):
    """Add the fighter we have just INSERTed to the three indexes, to prevent a
    duplicate WITHIN THE SAME BATCH from being inserted again."""
    key = normalize_name(name)
    if key:
        index["by_name"].setdefault(key, []).append((fid, dob, name))
    d = _to_date(dob)
    if d:
        index["by_dob"].setdefault(d, []).append((fid, key))
    # Also index the URL for the URL-based fallback lookup
    by_src_url = index.setdefault("by_src_url", {})
    for part in (data_source or "").split("|"):
        part = part.strip()
        m = re.search(r"fightmatrix\.com/fighter-profile/[^/]+/(\d+)", part)
        if m:
            by_src_url.setdefault(f"fm:{m.group(1)}", fid)
        m = re.search(r"tapology\.com/fightcenter/fighters/(\d+)", part)
        if m:
            by_src_url.setdefault(f"tap:{m.group(1)}", fid)
        m = re.search(r"ufc-fr\.com/combattant-(\d+)", part)
        if m:
            by_src_url.setdefault(f"ufr:{m.group(1)}", fid)


#  upsert

def upsert_fighter(conn, row: dict, index: dict, current_source: str) -> tuple[str, int | None]:
    """
    Insert or update a fighter using the in-memory index.

    `current_source`: code of the site that has just been scraped
                      ('fightmatrix', 'tapology', 'underground', 'ufc_fr', 'ufc').
                      Used to decide, for the "soft" fields (first_sport, stance,
                      nickname, photo, etc.), whether the incoming value is allowed
                      to overwrite an existing value (see AUTHORITATIVE_SOURCES).

    Return (result_str, fighter_id).
    """
    name = (row.get("name") or "").strip()
    if not name:
        return "SKIP (no name)", None

    # Convention: ufc_id / fightmatrix_id / mma_com_id aligned on id
    # after INSERT (not filled from the URLs).
    row["ufc_id"]         = None
    row["fightmatrix_id"] = None
    row["mma_com_id"]     = None

    typed = _prepare_row(row)
    dob   = typed.get("date_of_birth")

    fid, status = lookup(index, name, dob)

    # URL fallback: when the name does not match (wrong DOB or abbreviated/different name
    # depending on the source, e.g. "Paul Dena" UFC-FR vs "Paul Denis Navero" Tapology).
    # We check whether the profile URL is already in the data_source of an existing fighter.
    if status in ("new_homonyme", "new"):
        src_url = typed.get("data_source") or ""
        fid_url = lookup_by_url(index, src_url)
        if fid_url is not None:
            LOG.info("  URL-match: %s -> id=%d (different name corrected)", name, fid_url)
            fid, status = fid_url, "match_url"

    if status == "ambiguous":
        return f"SKIP (ambigu: {name} has several namesakes in the DB and no DOB to decide)", None

    cur = conn.cursor()
    try:
        if fid is not None:
            # mise a jour
            # For each column:
            # 1) name / created_at: never touched
            # 2) notes: merge + dedupe (always authoritative)
            # 3) ALWAYS_REFRESH (records, ranks, ...): OVERWRITE
            # 4) AUTHORITATIVE_SOURCES[col] contains current_source: OVERWRITE
            # 5) Otherwise: COALESCE (fill if empty, keep otherwise)
            always_set, always_vals, fill_set, fill_vals = [], [], [], []

            # Special case "notes" + "data_source": merged with the existing value + dedupe.
            # Without this, data_source (in ALWAYS_REFRESH) would overwrite the history
            # of the URLs scraped in previous sessions.
            cur.execute(
                "SELECT notes, data_source, fight_history FROM fighters WHERE id = %s",
                (fid,),
            )
            existing_row = cur.fetchone()
            existing_notes = existing_row[0] if existing_row else None
            existing_dsrc  = existing_row[1] if existing_row else None
            existing_fh    = existing_row[2] if existing_row else None

            new_notes = typed.get("notes")
            if new_notes:
                # notes is exclusive to UFC-FR -> ALWAYS_REFRESH (no merge)
                if new_notes != existing_notes:
                    always_set.append("notes = %s")
                    always_vals.append(new_notes)

            new_dsrc = typed.get("data_source")
            if new_dsrc:
                merged_dsrc = _merge_data_sources(existing_dsrc, new_dsrc)
                if merged_dsrc != existing_dsrc:
                    always_set.append("data_source = %s")
                    always_vals.append(merged_dsrc)

            # fight_history: arbitrated by SOURCE HIERARCHY (Tapology > UFC-FR=FM
            # > UG). The stored history is only overwritten if the incoming source is
            # at least as reliable -> prevents an FM/UFC-FR re-scrape from overwriting Tapology.
            new_fh = typed.get("fight_history")
            if new_fh and _fh_should_overwrite(existing_fh, new_fh):
                always_set.append("fight_history = %s")
                always_vals.append(new_fh)

            for col in DB_COLUMNS:
                if col in ("name", "created_at", "notes", "data_source", "fight_history"):
                    continue
                v = typed.get(col)
                if v is None:
                    continue

                is_authoritative = (
                    col in AUTHORITATIVE_SOURCES
                    and current_source in AUTHORITATIVE_SOURCES[col]
                )

                if col == "ufc_title_match_win":
                    # MAX semantics: never overwrite a higher value.
                    # Each URL is scraped independently; underground/tapology/ufc_fr
                    # ignore title fights and set 0 by default through _fill().
                    # Without GREATEST, the underground scrape (URL 2) would overwrite the 2
                    # set by FightMatrix (URL 1) just before.
                    # coalesce({col}, 0) protects against NULL in the database.
                    always_set.append(f"{col} = GREATEST(COALESCE({col}, 0), %s)")
                    always_vals.append(v)
                elif col in ALWAYS_REFRESH or is_authoritative:
                    always_set.append(f"{col} = %s")
                    always_vals.append(v)
                else:
                    fill_set.append(f"{col} = COALESCE({col}, %s)")
                    fill_vals.append(v)

            if always_set or fill_set:
                set_parts = always_set + fill_set
                vals      = always_vals + fill_vals + [fid]
                cur.execute(
                    f"UPDATE fighters SET {', '.join(set_parts)} WHERE id = %s",
                    vals,
                )
            _recompute_record_from_history(cur, fid)
            _recompute_record_total(cur, fid)
            _recompute_record_other(cur, fid)
            _recompute_total_fights(cur, fid)
            score = _recompute_quality_score(cur, fid)
            conn.commit()
            return f"UPDATE id={fid} ({status}, src={current_source}, qs={score})", fid

        # insertion
        cols         = [c for c in DB_COLUMNS if typed.get(c) is not None]
        placeholders = ", ".join(["%s"] * len(cols))
        vals         = [typed[c] for c in cols]
        cur.execute(
            f"INSERT INTO fighters ({', '.join(cols)}) "
            f"VALUES ({placeholders}) RETURNING id",
            vals,
        )
        new_id = cur.fetchone()[0]
        # Align the external IDs on the sequential id (convention)
        cur.execute(
            "UPDATE fighters SET ufc_id=%s, fightmatrix_id=%s, mma_com_id=%s "
            "WHERE id=%s",
            (str(new_id), str(new_id), str(new_id), new_id),
        )
        _recompute_record_from_history(cur, new_id)
        _recompute_record_total(cur, new_id)
        _recompute_record_other(cur, new_id)
        _recompute_total_fights(cur, new_id)
        score = _recompute_quality_score(cur, new_id)
        conn.commit()
        add_to_index(index, name, new_id, dob, typed.get("data_source") or "")
        return f"INSERT id={new_id} ({status}, qs={score})", new_id

    except psycopg2.IntegrityError as e:
        conn.rollback()
        msg = e.diag.message_primary if e.diag else str(e)
        return f"CONFLICT: {msg}", None
    except Exception as e:
        conn.rollback()
        LOG.exception(f"Upsert error for {name}")
        return f"ERROR: {e}", None
    finally:
        cur.close()


# URL collection  (multi-line paste)

def collect_urls(from_file: str | None = None) -> list[str]:
    """
    Collect the URLs to scrape from:
      - a text file (--file path): each line with an http URL.
        Lines starting with '#' are ignored (comments).
      - interactive input (default mode): paste the URLs then an empty line.
    """
    raw_lines: list[str] = []

    if from_file:
        try:
            path = Path(from_file)
            raw_lines = path.read_text(encoding="utf-8").splitlines()
            print(f"\n  -> Reading {path.resolve()} ({len(raw_lines)} lines)")
        except FileNotFoundError:
            print(f"  /!\\ File not found: {from_file}")
            return []
    else:
        print("\n" + "=" * 60)
        print("  Paste your URLs (1 per line, or several on the same line).")
        print("  Press an EMPTY Enter to validate and start.")
        print("  Tip: you can also use --file collected_fm_urls.txt")
        print("=" * 60)
        while True:
            try:
                line = input().strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                if raw_lines:
                    break
                print("  (empty) -> paste at least one URL, or Ctrl+C to quit")
                continue
            raw_lines.append(line)

    # Extract only the http URLs (ignore # comments and empty lines)
    urls = []
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in line.split():
            if part.startswith("http"):
                urls.append(part)

    # Deduplicate while keeping order
    seen, unique = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def log_failure(url: str, reason: str):
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{url}\t{reason}\n")


#  MAIN

def main():
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--file", default=None,
                        help="Text file containing the URLs (1 per line).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of URLs to process (e.g. --limit 1000)")
    parser.add_argument("--offset", type=int, default=0,
                        help="Skip the first N URLs (e.g. --offset 1000 for the 1001-2000 slice)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Start without confirmation")
    parser.add_argument("--max-consecutive-fails", type=int, default=MAX_CONSECUTIVE_FAILS,
                        help="Stop the batch after N CONSECUTIVE fetch failures "
                             "(IP probably banned by Cloudflare). 0 = disabled.")
    parser.add_argument("--proxy", default=None,
                        help="Proxy/VPN to use for all the HTTP requests. "
                             "Examples: socks5://127.0.0.1:1080  "
                             "or http://user:pass@host:port. "
                             "Essential if the IP is banned by Tapology/Cloudflare. "
                             "Equivalent: SCRAPER_PROXY env variable.")
    parser.add_argument("--resume", action="store_true",
                        help="Resume where we stopped: skips the URLs already processed "
                             "(progress file). Survives reboots / Cloudflare bans. "
                             "URLs with a fetch failure (503) are NOT marked -> retried.")
    parser.add_argument("--progress", default=None,
                        help="Progress file for --resume "
                             "(default: <urls-file>.progress, next to the --file).")
    args, _ = parser.parse_known_args()

    # proxy: --proxy CLI > SCRAPER_PROXY env > none
    import os as _os
    if args.proxy:
        _os.environ["SCRAPER_PROXY"] = args.proxy
    proxy_active = _os.environ.get("SCRAPER_PROXY")

    print("=" * 60)
    print("  scrape_batch")
    print("  Batch scrape -> Supabase, robust anti-duplicate matching.")
    if proxy_active:
        print(f"  Proxy: {proxy_active}")
    print("=" * 60)

    all_urls = collect_urls(from_file=args.file)
    if not all_urls:
        print("  No URL. Goodbye.")
        return

    # --resume: remove the URLs already processed in previous sessions.
    # The progress file accumulates (append) the URLs that got a real
    # response (INSERT/UPDATE/SKIP/CONFLICT). Fetch failures (Cloudflare 503)
    # are NOT marked -> they are automatically retried on the next run.
    if args.progress:
        progress_path = Path(args.progress)
    elif args.file:
        progress_path = Path(args.file + ".progress")
    else:
        progress_path = ROOT / "data/scraper_output/errors/scrape_batch_progress.txt"

    if args.resume and progress_path.exists():
        done_urls = set(progress_path.read_text(encoding="utf-8").split())
        before = len(all_urls)
        all_urls = [u for u in all_urls if u not in done_urls]
        print(f"\n  -> RESUME: {len(done_urls)} URLs already done, "
              f"{len(all_urls)}/{before} remaining  ({progress_path.name})")

    # Apply offset + limit to process in slices
    total_in_file = len(all_urls)
    urls = all_urls[args.offset:]
    if args.limit:
        urls = urls[:args.limit]

    sources = sorted(set(detect_source(u) for u in urls))
    if args.offset or args.limit:
        print(f"\n  -> Slice: URLs {args.offset + 1} to {args.offset + len(urls)} / {total_in_file} total")
    print(f"  -> {len(urls)} URL(s) a scraper")
    print(f"  -> Source(s): {', '.join(sources)}")
    if "unknown" in sources:
        print("  /!\\ Some URLs do not match any known site.")

    # Estimate: ~4s per fighter (fetch + parse + DB + sleep)
    est_min = len(urls) * 4 / 60
    print(f"\n  -> Estimate: ~{est_min:.0f} min")

    if args.offset or args.limit:
        reste = total_in_file - args.offset - len(urls)
        next_offset = args.offset + len(urls)
        if reste > 0:
            print(f"  -> After this batch, run: --offset {next_offset} --limit {args.limit or reste}")
    if not args.yes:
        try:
            confirm = input("  Start? (Enter = yes, anything else = cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Annule.")
            return
        if confirm:
            print("  Cancelled.")
            return

    # Reset the failure log for this batch
    if FAILED_LOG.exists():
        FAILED_LOG.unlink()

    # Connection + index loading
    conn = get_connection()
    if not conn:
        print("  /!\\ Supabase connection impossible (check .env)")
        return

    cur = conn.cursor()
    print("\n  -> Loading the index of existing fighters...")
    index = load_existing_index(cur)
    cur.close()
    # Count the unique fighters (by ID) from the by_name index.
    # NB: sum(len(v) for v in index.values()) is WRONG because the index has 2 keys
    # (by_name + by_dob) and counted their sum (~2x the real figure).
    total_existing = len({
        fid
        for entries in index["by_name"].values()
        for fid, _, _ in entries
    })
    print(f"  -> {total_existing} fighters already in the database")

    stats   = {"INSERT": 0, "UPDATE": 0, "SKIP": 0, "CONFLICT": 0, "FAIL": 0}
    t_start = time.time()
    consecutive_fails = 0   # circuit-breaker : echecs fetch d'affilee (503 Cloudflare)
    # Progress file (append): only opened in --resume mode.
    prog_f = progress_path.open("a", encoding="utf-8") if args.resume else None

    def _breaker_tripped(n: int) -> bool:
        """Pause if too many consecutive failures (IP banned). Wait for the user to
        reconnect the VPN then press Enter to continue. Return True
        only if the user wants to stop (Q)."""
        nonlocal consecutive_fails
        if not args.max_consecutive_fails or n < args.max_consecutive_fails:
            return False
        print(f"\n  /!\\ {n} CONSECUTIVE fetch failures -> IP probably banned by Cloudflare.")
        print(f"      The failed URLs are in {FAILED_LOG.name} (retried on --resume).")
        print(f"\n  >>> Reconnect the VPN / change IP, then:")
        print(f"      - Press ENTER to continue the batch")
        print(f"      - Type Q then ENTER to stop")
        try:
            rep = input("  Waiting... ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return True
        if rep in ("q", "quit", "exit"):
            return True
        consecutive_fails = 0  # start again from zero after the VPN reconnection
        return False

    def _ensure_conn():
        """Reconnect if the Supabase connection has been dropped (server timeout)."""
        nonlocal conn
        try:
            conn.cursor().execute("SELECT 1")
        except Exception:
            print("  [RECONNECTION] Connection lost, reconnecting...")
            try:
                conn.close()
            except Exception:
                pass
            conn = get_connection()
            if not conn:
                raise RuntimeError("Unable to reconnect to Supabase")
            print("  [RECONNECTION] OK")

    try:
        for i, url in enumerate(urls, 1):
            elapsed = time.time() - t_start
            if i > 1:
                avg       = elapsed / (i - 1)
                eta       = avg * (len(urls) - i + 1)
                time_info = f" | avg {avg:.1f}s | ETA {eta/60:.1f} min"
            else:
                time_info = ""
            print(f"\n[{i}/{len(urls)}]  {url}{time_info}")

            current_source = detect_source(url)

            try:
                row = scrape([url])
            except Exception as e:
                LOG.exception(f"Scrape error: {e}")
                log_failure(url, f"scrape_error: {e}")
                stats["FAIL"] += 1
                consecutive_fails += 1
                if _breaker_tripped(consecutive_fails):
                    break
                continue

            if not row.get("name"):
                print(f"  -> empty name after scrape, ignored")
                log_failure(url, "no_name_after_scrape")
                stats["FAIL"] += 1
                consecutive_fails += 1
                if _breaker_tripped(consecutive_fails):
                    break
                continue

            # Fetch succeeded (we have a name) -> reset the circuit breaker
            consecutive_fails = 0

            # Automatic reconnection if Supabase cut the connection
            _ensure_conn()

            result, _ = upsert_fighter(conn, row, index, current_source)
            print(f"  -> {result}  name={row.get('name')}")

            if   result.startswith("INSERT"):    stats["INSERT"]   += 1
            elif result.startswith("UPDATE"):    stats["UPDATE"]   += 1
            elif result.startswith("SKIP"):
                stats["SKIP"] += 1
                log_failure(url, result)
            elif result.startswith("CONFLICT"):
                stats["CONFLICT"] += 1
                log_failure(url, result)
            else:
                stats["FAIL"] += 1
                log_failure(url, result)

            # Mark the URL as processed (real response). Database upsert errors and
            # fetch failures (503) are NOT marked -> retried on the next --resume.
            if prog_f is not None and result.startswith(("INSERT", "UPDATE", "SKIP", "CONFLICT")):
                prog_f.write(url + "\n"); prog_f.flush()

            time.sleep(SLEEP_BETWEEN)

    except KeyboardInterrupt:
        print("\n\n  /!\\ Interrupted by Ctrl+C. Partial summary below.")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if prog_f is not None:
            prog_f.close()

    elapsed = time.time() - t_start
    n_done  = stats["INSERT"] + stats["UPDATE"] + stats["SKIP"] + stats["CONFLICT"] + stats["FAIL"]
    per     = elapsed / max(n_done, 1)
    print("\n" + "=" * 60)
    print(f"  SUMMARY  ({elapsed/60:.1f} min, {per:.1f}s/fighter)")
    print(f"  INSERT   : {stats['INSERT']}")
    print(f"  UPDATE   : {stats['UPDATE']}")
    print(f"  SKIP     : {stats['SKIP']}")
    print(f"  CONFLICT: {stats['CONFLICT']}")
    print(f"  FAIL     : {stats['FAIL']}")
    print("=" * 60)
    if stats["FAIL"] or stats["CONFLICT"] or stats["SKIP"]:
        print(f"  Failure details: {FAILED_LOG.name}")


if __name__ == "__main__":
    main()
