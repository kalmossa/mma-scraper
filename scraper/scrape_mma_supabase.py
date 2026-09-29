"""
scrape_mma_supabase.py
Same scraper as scrape_mma.py but writes into PostgreSQL / Supabase
instead of the "final tables - fighters.csv" CSV.

USAGE:
    py scrape_mma_supabase.py <fm_url> <tapo_url> <ug_url>
    py scrape_mma_supabase.py   (interactive mode)
"""

import sys, re, time, logging, json
from datetime import date, datetime
from pathlib import Path

import psycopg2

ROOT = Path(__file__).parent.parent  # repository root
SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))

from scrape_mma import (
    scrape, print_summary, ALWAYS_REFRESH, detect_source,
    SOURCE_PRIORITY, RECORD_AUTHORITATIVE_MIN,
)
from db_connection import get_connection


# shared utilities (also imported by the batch script)

def _to_date(v):
    """
    Normalize None / ISO str / datetime.date / datetime -> date | None.
    Essential: `_prepare_row` leaves date_of_birth as a STRING ("YYYY-MM-DD"),
    whereas Postgres returns a `datetime.date`. Without this cast, the comparison
    `existing_dob == new_dob` fails -> DOB matching breaks -> INSERT instead
    of UPDATE -> CONFLICT on uq_fighter_name_dob.
    """
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _merge_pipe_separated(
    existing: str | None,
    incoming: str | None,
    *,
    case_sensitive: bool = False,
) -> str | None:
    """
    Merge 2 strings separated by ' | ' while stripping duplicates.
    Keeps the order of appearance.

      case_sensitive=False -> compare in lowercase (useful for 'notes':
        "Team: X" and "team: x" are considered identical)
      case_sensitive=True  -> strict comparison (useful for 'data_source':
        URLs are case-sensitive, e.g. %20 vs %20)

    Examples:
      _merge_pipe_separated('A | B', 'B | C')                 -> 'A | B | C'
      _merge_pipe_separated('url1', 'url2 | url1', cs=True)   -> 'url1 | url2'
    """
    if not existing and not incoming:
        return None
    if not existing:
        return incoming
    if not incoming:
        return existing

    seen: set[str] = set()
    result: list[str] = []
    for chunk in (existing.split("|") + incoming.split("|")):
        part = chunk.strip()
        if not part:
            continue
        key = part if case_sensitive else part.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return " | ".join(result) if result else None


def _merge_notes(existing: str | None, incoming: str | None) -> str | None:
    """Merge of the notes: 'Tag: value | ...' with case-insensitive dedupe."""
    return _merge_pipe_separated(existing, incoming, case_sensitive=False)


def _merge_data_sources(existing: str | None, incoming: str | None) -> str | None:
    """
    Merge of data_source: accumulates all the URLs already scraped for this fighter,
    without duplicates. Case-sensitive dedupe (URLs are case-sensitive).
    Keeps a complete trace: 'urlA | urlB | urlC | urlD' after
    several scraping sessions spread over time.
    """
    return _merge_pipe_separated(existing, incoming, case_sensitive=True)


# "Soft" fields: COALESCE by default, but these sources may overwrite.
# See the batch script docstring for the justification of the hierarchy.
AUTHORITATIVE_SOURCES = {
    "first_sport":          {"ufc_fr", "ufc"},
    "stance":               {"ufc_fr", "ufc"},
    "nickname":             {"ufc_fr", "ufc", "tapology"},
    "date_of_birth":        {"tapology", "ufc_fr", "ufc"},
    # UFC-FR gives the COMPETITION nationality (the country the fighter fights for).
    # tapology gives the PLACE of birth (e.g. chimaev=russia/chechnya but fights for UAE).
    # -> only UFC-FR and UFC are authoritative; Tapology uses COALESCE (fills only if empty).
    "nationality":          {"ufc_fr", "ufc"},
    "height_inches":        {"ufc_fr", "tapology", "ufc"},
    "reach_inches":         {"ufc_fr", "tapology", "ufc"},
    "weight_class_current": {"ufc_fr", "fightmatrix"},
    "weight_class_origin":  {"fightmatrix"},
    "photo_url":            {"tapology"},   # design decision: photo_url <- Tapology only
    "photo_thumbnail_url":  {"ufc_fr"},     # design decision: round thumbnail <- UFC-FR only
    "career_debut_date":    {"fightmatrix", "tapology"},
}


# Fields that contribute to the data_quality_score (+ associated points).
# The score is capped at 100. An explicit formula rather than an SQL trigger
# to keep the logic in the Python code (easier to tweak).
_QUALITY_SCORE_FIELDS: list[tuple[str, int]] = [
    # Identite (essentiel)
    ("date_of_birth",              6),
    ("nationality",                3),
    ("nickname",                   2),
    # Bio physique
    ("height_inches",              5),
    ("reach_inches",               5),
    # Style / officiel UFC
    ("stance",                     4),   # only set by UFC-FR/UFC -> good proxy for a "UFC fighter"
    ("first_sport",                4),
    ("ufc_official_rank",          3),
    # Photos
    ("photo_url",                  2),   # Tapology
    ("photo_thumbnail_url",        2),   # UFC-FR
    # Carriere / historique
    ("fight_history",              6),
    ("career_debut_date",          2),
    ("last_fight_date",            2),
    # Metriques FightMatrix
    ("fightmatrix_rating_points",  5),
    ("fightmatrix_540_metric",     2),
    # Records detailles
    ("wins_by_ko_tko",             2),
]
_QUALITY_BASE        = 20    # Base score (the fighter exists with a name)
_QUALITY_PER_SOURCE  = 8     # Per unique source in data_source (4 sources max = +32)
_QUALITY_MAX_SOURCES = 4


def _recompute_record_other(cur, fighter_id: int) -> None:
    """
    Recompute record_other_* = record_total_* - record_ufc_* on the SQL side,
    on the state MERGED in the database after the upsert.

    Why on the SQL side and not in compute_derived()?
      compute_derived() runs on the row of a SINGLE scrape. If UFC-FR
      scrapes alone (without Tapology/FM which bring record_total), the row has
      record_total=0 but record_ufc=8 -> compute_derived gives other=-8
      clamped to 0, then the UPDATE (ALWAYS_REFRESH) OVERWRITES the good values
      previously scraped.

      By recomputing on the SQL side, we work on the FINAL state in the database
      where record_total and record_ufc are both up to date, no matter
      which source set them and in which order.

    E.g. Charles Johnson in the database after 4 scrapes
      record_total = 19-8-0, record_ufc = 8-6-0
      -> record_other = 11-2-0  (and no longer 0-0-0)
    """
    cur.execute(
        '''
        UPDATE fighters
        SET
            record_other_wins   = GREATEST(COALESCE(record_total_wins, 0)   - COALESCE(record_ufc_wins, 0),   0),
            record_other_losses = GREATEST(COALESCE(record_total_losses, 0) - COALESCE(record_ufc_losses, 0), 0),
            record_other_draws  = GREATEST(COALESCE(record_total_draws, 0)  - COALESCE(record_ufc_draws, 0),  0)
        WHERE id = %s
        ''',
        (fighter_id,),
    )


def _fh_meta(fh_raw) -> tuple[int, int]:
    """(source_priority, number_of_fights) of a fight_history JSON. (-1, 0) if empty/unreadable."""
    if not fh_raw:
        return (-1, 0)
    try:
        fh = json.loads(fh_raw) if isinstance(fh_raw, str) else fh_raw
    except (json.JSONDecodeError, TypeError):
        return (-1, 0)
    if not isinstance(fh, dict):
        return (-1, 0)
    prio = SOURCE_PRIORITY.get(fh.get("source", ""), 0)
    fights = fh.get("fights") or []
    return (prio, len(fights))


def _fh_source(fh_raw) -> str:
    """Source name (lowercase) of a fight_history JSON, or '' if unreadable."""
    if not fh_raw:
        return ""
    try:
        fh = json.loads(fh_raw) if isinstance(fh_raw, str) else fh_raw
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(fh, dict):
        return ""
    return (fh.get("source") or "").strip().lower()


def _fh_should_overwrite(existing_fh, new_fh) -> bool:
    """
    True if the incoming history must overwrite the stored one according to the HIERARCHY:
    the more reliable source wins; for the same source, the more complete one wins.

    EXCEPTION (design decision): a RE-SCRAPE of the SAME source ALWAYS
    overwrites the old one, EVEN if it has FEWER fights. It is a freshness
    correction (e.g. amateur/kickboxing fights were cleaned up -> 34 polluted
    fights -> 24 pro MMA fights). Without it, the old polluted history would be
    judged "more complete" and would block the correction.
    """
    new_src = _fh_source(new_fh)
    old_src = _fh_source(existing_fh)
    if new_src and new_src == old_src:
        return True  # re-scrape of the same source = correction -> always overwrite

    new_prio, new_count = _fh_meta(new_fh)
    old_prio, old_count = _fh_meta(existing_fh)
    if new_prio > old_prio:
        return True
    if new_prio == old_prio:
        return new_count >= old_count
    return False


def _record_from_fight_history(fh_raw) -> tuple[int, int, int, int] | None:
    """
    Count (W, L, D, NC) from a fight_history JSON, ONLY if its source
    is AUTHORITATIVE (priority >= RECORD_AUTHORITATIVE_MIN: tapology, ufcstats,
    event_scrape). Return None otherwise (fightmatrix/underground/unreadable/empty)
    -> the caller keeps the scraped value ("MAX" safety net).
    """
    if not fh_raw:
        return None
    try:
        fh = json.loads(fh_raw) if isinstance(fh_raw, str) else fh_raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(fh, dict):
        return None
    if SOURCE_PRIORITY.get((fh.get("source") or "").strip().lower(), 0) < RECORD_AUTHORITATIVE_MIN:
        return None
    fights = fh.get("fights") or []
    if not fights:
        return None
    w = l = d = nc = 0
    for f in fights:
        if not isinstance(f, dict):
            continue
        r = (f.get("result") or "").strip().upper()
        if r == "W":
            w += 1
        elif r == "L":
            l += 1
        elif r == "D":
            d += 1
        elif r in ("NC", "N/C"):
            nc += 1
    return (w, l, d, nc)


def _recompute_record_from_history(cur, fighter_id: int) -> None:
    """
    The fight_history of an AUTHORITATIVE source (tapology / ufcstats / event_scrape,
    see RECORD_AUTHORITATIVE_MIN) is authoritative on the record, ONLY if its count
    is <= the total already stored in the database.

    Targeted case: FM stored 15-1 with a phantom fight, Tapology says 14-1
    -> fight_history count(15) <= stored(16) -> we correct to 14-1.

    Case NOT to correct: the Tapology fight_history includes non-MMA fights
    (boxing/kickboxing) -> count(22) > stored(17) -> we do not overwrite the official
    16-1 header scraped from the Tapology page.

    For a FightMatrix/Underground history (or none) -> no action.
    To be called BEFORE _recompute_record_total (which does GREATEST(total, ufc)).
    """
    cur.execute(
        "SELECT fight_history, record_total_wins, record_total_losses, "
        "record_total_draws, record_total_nc FROM fighters WHERE id = %s",
        (fighter_id,),
    )
    row = cur.fetchone()
    if not row:
        return
    rec = _record_from_fight_history(row[0])
    if rec is None:
        return
    w, l, d, nc = rec
    fh_total = w + l + d + nc
    stored_total = (
        (row[1] or 0) + (row[2] or 0) + (row[3] or 0) + (row[4] or 0)
    )
    # Only apply if the fight_history corrects downward (phantom fight)
    # or is equal. If fight_history > stored, it includes non-MMA fights.
    if stored_total > 0 and fh_total > stored_total:
        return
    cur.execute(
        '''
        UPDATE fighters
        SET record_total_wins = %s, record_total_losses = %s,
            record_total_draws = %s, record_total_nc = %s
        WHERE id = %s
        ''',
        (w, l, d, nc, fighter_id),
    )


def _recompute_record_total(cur, fighter_id: int) -> None:
    """
    Safeguard: if record_ufc_* > record_total_* (logically impossible),
    force record_total_* = record_ufc_*. Typical case: UFC-FR scraped alone
    brings record_ufc=9 but not record_total -> total stays at 0.

    After this correction, _recompute_record_other recomputes other from
    total and ufc -> consistent.
    """
    cur.execute(
        '''
        UPDATE fighters
        SET
            record_total_wins   = GREATEST(COALESCE(record_total_wins, 0),   COALESCE(record_ufc_wins, 0)),
            record_total_losses = GREATEST(COALESCE(record_total_losses, 0), COALESCE(record_ufc_losses, 0)),
            record_total_draws  = GREATEST(COALESCE(record_total_draws, 0),  COALESCE(record_ufc_draws, 0))
        WHERE id = %s
        ''',
        (fighter_id,),
    )


def _recompute_total_fights(cur, fighter_id: int) -> None:
    """
    Safeguard: recompute total_fights = wins + losses + draws.

    The SQL trigger update_total_fights only fires on INSERT or
    UPDATE of the record_total_* columns. If for some reason (psycopg2,
    identical value not re-set, etc.) the trigger does not fire, we can
    end up with total_fights=0 while the records are filled.
    Systematic recompute after every upsert.
    """
    cur.execute(
        '''
        UPDATE fighters
        SET total_fights = COALESCE(record_total_wins,   0)
                         + COALESCE(record_total_losses, 0)
                         + COALESCE(record_total_draws,  0)
        WHERE id = %s
        ''',
        (fighter_id,),
    )


def _recompute_quality_score(cur, fighter_id: int) -> int:
    """
    Recompute data_quality_score from the CURRENT state in the database after the upsert.

    Formula:
        20 (base)
      + 8 * min(number_of_unique_sources_in_data_source, 4)        # max +32
      + sum of the points per filled critical field (see _QUALITY_SCORE_FIELDS)
      = capped at 100

    Needed because compute_derived() only computes the score at the 1st insertion:
    a fighter scraped from 1 source then 3 others stayed stuck at its initial score.
    This function is called at the end of every upsert (INSERT or UPDATE).
    """
    cols = ["data_source"] + [c for c, _ in _QUALITY_SCORE_FIELDS]
    cur.execute(
        f"SELECT {', '.join(cols)} FROM fighters WHERE id = %s",
        (fighter_id,),
    )
    r = cur.fetchone()
    if not r:
        return 0

    # Count unique sources (deduplicated per site, not per URL, so as not to count
    # twice if the user re-scrapes the same URL with a slightly different parameter)
    data_source = r[0] or ""
    unique_sites: set[str] = set()
    for u in data_source.split("|"):
        src = detect_source(u.strip())
        if src and src != "unknown":
            unique_sites.add(src)

    score = _QUALITY_BASE + _QUALITY_PER_SOURCE * min(len(unique_sites), _QUALITY_MAX_SOURCES)

    for i, (_col, points) in enumerate(_QUALITY_SCORE_FIELDS, start=1):
        v = r[i]
        if v is None:
            continue
        # Avoid counting "default" zeros (e.g. ufc_official_rank=NULL vs 0=champion: we accept 0)
        # Empty strings are rejected
        if isinstance(v, str) and not v.strip():
            continue
        score += points

    score = min(score, 100)
    cur.execute(
        "UPDATE fighters SET data_quality_score = %s WHERE id = %s",
        (score, fighter_id),
    )
    return score


def _sources_in_row(row: dict) -> set[str]:
    """
    Extract the list of scraped sites for this row, from row['data_source']
    (which contains the URLs separated by ' | ').
      'https://www.tapology.com/... | https://www.ufc-fr.com/...' -> {'tapology', 'ufc_fr'}
    Used to decide, in upsert_supabase, whether an authoritative source contributed
    to the current scrape (in which case it may overwrite the soft field).
    """
    sources: set[str] = set()
    raw = row.get("data_source") or ""
    for u in str(raw).split("|"):
        u = u.strip()
        if not u:
            continue
        src = detect_source(u)
        if src and src != "unknown":
            sources.add(src)
    return sources


# extraction of the external ids from the scraper URLs

def extract_external_ids(data_source: str) -> dict:
    """
    Parse data_source (URLs separated by ' | ') and extract:
      - ufc_id          (from ufc-fr.com or ufc.com)
      - fightmatrix_id  (from fightmatrix.com)
      - mma_com_id      (from mixedmartialarts.com)
    """
    ids = {"ufc_id": None, "fightmatrix_id": None, "mma_com_id": None}
    if not data_source:
        return ids

    for url in str(data_source).split("|"):
        u = url.strip()
        if not u:
            continue
        # FightMatrix : /fighter-profile/Petr%20Yan/141553
        m = re.search(r"fightmatrix\.com/fighter-profile/[^/]+/(\d+)", u, re.I)
        if m:
            ids["fightmatrix_id"] = m.group(1)
            continue
        # Tapology: /fighters/90075-petr-yan (the Tapology ID can also serve as a fallback ufc_id)
        m = re.search(r"tapology\.com/fightcenter/fighters/(\d+)", u, re.I)
        if m and not ids["ufc_id"]:
            ids["ufc_id"] = m.group(1)
            continue
        # underground : /petr-yan:64903cebf920bc6c
        m = re.search(r"mixedmartialarts\.com/[^/]*:([A-F0-9]{8,})", u, re.I)
        if m:
            ids["mma_com_id"] = m.group(1)
            continue
        # UFC-FR: combattant-1802.html (overrides ufc_id with the official UFC ID)
        m = re.search(r"ufc-fr\.com/combattant-(\d+)", u, re.I)
        if m:
            ids["ufc_id"] = m.group(1)
            continue
        # UFC.com : /athlete/petr-yan
        m = re.search(r"ufc\.com/(?:[a-z-]+/)?athlete/([\w-]+)", u, re.I)
        if m and not ids["ufc_id"]:
            ids["ufc_id"] = m.group(1)

    return ids

LOG = logging.getLogger("scrape_mma_supabase")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")

# columns that go into the database - exact order of the CSV (62 columns)
# Excluded: misc (internal to the CSV workflow), id (auto-generated by PostgreSQL)
# Excluded: country_code, flag_emoji, weight_class_history_text (dropped by design)
DB_COLUMNS = [
    "ufc_id", "fightmatrix_id", "mma_com_id",
    "name", "nickname", "gender", "date_of_birth", "age", "nationality",
    "photo_url", "photo_thumbnail_url",
    "weight_class_current", "weight_class_origin",
    "height_inches", "reach_inches", "stance", "first_sport",
    "record_total_wins", "record_total_losses", "record_total_draws", "record_total_nc",
    "record_ufc_wins", "record_ufc_losses", "record_ufc_draws",
    "record_other_wins", "record_other_losses", "record_other_draws",
    "wins_by_ko_tko", "wins_by_submission", "wins_by_decision",
    "losses_by_ko_tko", "losses_by_submission", "losses_by_decision",
    "split_decision_wins", "split_decision_losses",
    "career_debut_date", "last_fight_date", "days_inactive",
    "is_active", "current_league", "is_ufc_champion", "ufc_title_match_win", "total_fights",
    "win_percentage_int", "finish_rate_int", "current_streak", "last_5_results",
        "fightmatrix_rating_points", "fightmatrix_big_league_record",
    "fightmatrix_540_metric", "fightmatrix_quality_perf_pct",
    "ufc_official_rank", "ufc_p4p_rank",
    "created_at", "updated_at", "last_scraped_at",
    "data_source", "data_quality_score", "is_verified",
    "notes", "fight_history",
]

INT_COLS = {
    "age", "record_total_wins", "record_total_losses", "record_total_draws",
    "record_total_nc", "record_ufc_wins", "record_ufc_losses", "record_ufc_draws",
    "record_other_wins", "record_other_losses", "record_other_draws",
    "wins_by_ko_tko", "wins_by_submission", "wins_by_decision",
    "losses_by_ko_tko", "losses_by_submission", "losses_by_decision",
    "split_decision_wins", "split_decision_losses",
    "days_inactive", "ufc_title_match_win", "total_fights",
    "win_percentage_int", "finish_rate_int", "ufc_official_rank", "ufc_p4p_rank",
    "data_quality_score",
}
DECIMAL_COLS = {"fightmatrix_rating_points", "fightmatrix_540_metric", "fightmatrix_quality_perf_pct"}
BOOL_COLS = {"is_active", "is_ufc_champion", "is_verified"}
# Columns that store a URL: we reject data: URIs there (inline base64
# blobs, sometimes >100 KB) that used to crash the INSERT (varchar 700) and
# have no business in the database. Real long URLs pass (TEXT columns,
# migration 018).
URL_COLS = {"photo_url", "photo_thumbnail_url"}


def _cast(col, val):
    if val is None or str(val).strip() == "":
        return None
    val = str(val).strip()
    if col in URL_COLS and val.lower().startswith("data:"):
        return None
    if col in BOOL_COLS:
        return val.upper() in ("TRUE", "1", "YES", "T")
    if col in INT_COLS:
        try: return int(float(val))
        except: return None
    if col in DECIMAL_COLS:
        try: return float(val)
        except: return None
    return val


_VARCHAR_LIMITS = {
    "weight_class_current": 50,
    "weight_class_origin":  50,
    "current_league":       50,
    "nationality":          60,
    "current_streak":       15,
    "last_5_results":       30,
    "fightmatrix_big_league_record": 30,
}

def _prepare_row(row):
    out = {col: _cast(col, row.get(col, "")) for col in DB_COLUMNS}
    for col, limit in _VARCHAR_LIMITS.items():
        if col in out and isinstance(out[col], str) and len(out[col]) > limit:
            out[col] = out[col][:limit]
    return out


def _norm_tokens(name: str) -> set[str]:
    """Tokenize a normalized name (lowercase, no accents, no punctuation)."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_name = re.sub(r"[^a-z0-9 ]", " ", ascii_name.lower())
    return {t for t in ascii_name.split() if len(t) > 1}


def _name_divergences(typed: dict, db_wc, db_wins, db_nat) -> int:
    """
    Count the divergences between an incoming row and a database fighter found by name alone.
    >= 2 divergences = probable namesake -> we refuse the match.
    """
    count = 0
    inc_wc = typed.get("weight_class_current")
    inc_wins = typed.get("record_total_wins")
    inc_nat = typed.get("nationality")

    if inc_wc and db_wc:
        # Normalize (remove "Women's" to compare the raw weight)
        norm = lambda s: s.lower().replace("women's ", "").strip()
        if norm(inc_wc) != norm(db_wc):
            count += 1

    if inc_wins is not None and db_wins is not None:
        try:
            if abs(int(inc_wins) - int(db_wins)) > 3:
                count += 1
        except (TypeError, ValueError):
            pass

    if inc_nat and db_nat and inc_nat.lower() != db_nat.lower():
        count += 1

    return count


def _find_existing(cur, typed: dict) -> tuple[int, str] | tuple[None, None]:
    """
    Look for an existing fighter:
      1. (name, DOB) - discriminating pair if the DOB is known on both sides
      2. name alone + plausibility - match only if <= 1 divergence
         (weight_class, wins +/-3, nationality). Otherwise SKIP (probable namesake).
      3. DOB + partial tokens - "Samuel Chavarria" -> "Jesus Samuel Chavarria"
         (same DOB, tokens of the short name in tokens of the long name, >= 2 common tokens)

    Return (fighter_id, match_method) or (None, None) if new/ambiguous.
    """
    name = typed.get("name")
    dob = typed.get("date_of_birth")

    if name and dob:
        cur.execute(
            "SELECT id FROM fighters WHERE LOWER(name) = LOWER(%s) AND date_of_birth = %s",
            (name, dob),
        )
        r = cur.fetchone()
        if r:
            return r[0], "name+dob"

    if name:
        cur.execute(
            "SELECT id, date_of_birth, weight_class_current, record_total_wins, nationality "
            "FROM fighters WHERE LOWER(name) = LOWER(%s)",
            (name,),
        )
        r = cur.fetchone()
        if r:
            fid, db_dob, db_wc, db_wins, db_nat = r
            # DOBs known on both sides and different = certain namesake
            if dob and db_dob and _to_date(dob) != _to_date(db_dob):
                logging.warning(
                    f"  [AMBIGUOUS] '{name}': different DOBs "
                    f"({dob} vs {db_dob}) -> homonyme, SKIP"
                )
                return None, None
            # Without a DOB to decide: check plausibility
            div = _name_divergences(typed, db_wc, db_wins, db_nat)
            if div >= 2:
                logging.warning(
                    f"  [AMBIGUOUS] '{name}': name-only match but {div} divergences "
                    f"(wc={typed.get('weight_class_current')} vs {db_wc}, "
                    f"wins={typed.get('record_total_wins')} vs {db_wins}, "
                    f"nat={typed.get('nationality')} vs {db_nat}) -> SKIP"
                )
                return None, None
            return fid, "name"

    # Fallback: same DOB + flexible name match
    # Couvre :
    #   - "Samuel Chavarria" vs "Jesus Samuel Chavarria"  (subset)
    # - "Chris Reyes"      vs "Christopher Reyes"       (prefix)
    # - "Dan Henderson"    vs "Daniel Henderson"         (prefix)
    if name and dob:
        cur.execute(
            "SELECT id, name FROM fighters WHERE date_of_birth = %s",
            (dob,),
        )
        rows = cur.fetchall()
        if rows:
            new_tokens = _norm_tokens(name)
            for fid, fname in rows:
                existing_tokens = _norm_tokens(fname or "")
                if _tokens_match_flex(new_tokens, existing_tokens):
                    return fid, "dob+name_flex"

    # Step 4: flexible name match WITHOUT a reliable DOB, confirmed by corroboration.
    # Resolves "Leo Xavier" / "Leonardo Xavier" (no DOB) by relying on
    # the common fight history or identical fight dates.
    if name:
        cand = _find_flex_corroborated(cur, typed)
        if cand is not None:
            return cand, "name_flex+corrob"

    return None, None


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for ca in a:
        curr = [prev[0] + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def _tokens_match_flex(a: set[str], b: set[str]) -> bool:
    """
    Check whether two token sets (same DOB guaranteed) match the same fighter.

    1. Subset  - "Samuel Chavarria" in "Jesus Samuel Chavarria"
    2. Prefix  - "Chris" ~ "Christopher" (one token is a prefix of the other)
    3. Typo    - "Bandoui" ~ "Bandaoui" (edit distance <= 1, token >= 4 chars)
       All the other tokens must be identical in the 3 cases.
    """
    if not a or not b:
        return False

    # Strategy 1: subset
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    common = shorter & longer
    if len(common) >= 2 and shorter == common:
        return True

    # Strategies 2 & 3: a single token differs, all the others identical
    only_in_a = a - b
    only_in_b = b - a
    if len(only_in_a) == 1 and len(only_in_b) == 1:
        t_a = next(iter(only_in_a))
        t_b = next(iter(only_in_b))
        # Prefix (Chris/Christopher)
        if min(len(t_a), len(t_b)) >= 3 and (t_a.startswith(t_b) or t_b.startswith(t_a)):
            return True
        # Typo / transliteration (Bandoui/Bandaoui): edit distance <= 1
        if min(len(t_a), len(t_b)) >= 4 and _levenshtein(t_a, t_b) <= 1:
            return True

    return False


def _norm_opponent(s: str) -> set[str]:
    """
    Normalize an opponent name into a token set: remove the FightMatrix rank/category
    suffix ("#450 Welterweight"), accents and punctuation.
    """
    if not s:
        return set()
    s = re.split(r"#\d+", s)[0]  # coupe "#450 Welterweight"
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in nfkd if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return {t for t in s.split() if len(t) >= 3}


def _fh_opponents(fh_raw) -> list[set]:
    """Extract the list of opponents (token sets) from a fight_history JSON."""
    if not fh_raw:
        return []
    try:
        data = json.loads(fh_raw) if isinstance(fh_raw, str) else fh_raw
    except (ValueError, TypeError):
        return []
    out = []
    for f in data.get("fights", []):
        toks = _norm_opponent(f.get("opponent", ""))
        if toks:
            out.append(toks)
    return out


def _opp_same(ta: set, tb: set) -> bool:
    """Two identical opponents: token subset, >= 2 in common, or one token with a typo."""
    common = ta & tb
    if len(common) >= 2:
        return True
    shorter = ta if len(ta) <= len(tb) else tb
    if common and shorter == common:
        return True
    only_a, only_b = ta - tb, tb - ta
    if common and len(only_a) == 1 and len(only_b) == 1:
        x, y = next(iter(only_a)), next(iter(only_b))
        if min(len(x), len(y)) >= 4 and _levenshtein(x, y) <= 1:
            return True
    return False


def _opponents_overlap(a_opps: list[set], b_opps: list[set]) -> int:
    """Number of opponents of a that match an opponent of b."""
    count, used = 0, set()
    for ta in a_opps:
        for j, tb in enumerate(b_opps):
            if j not in used and _opp_same(ta, tb):
                count += 1
                used.add(j)
                break
    return count


def _find_flex_corroborated(cur, typed: dict):
    """
    Look for a fighter with a flexibly close name WITHOUT relying on the DOB, but requiring
    STRONG corroboration to avoid false positives (namesakes):
      - >= 2 common opponents in the history  (the most reliable signal)
      - OR same last-fight date AND same career start date
      - OR same DOB known on both sides
    Return the id of the best candidate, or None.
    """
    name = typed.get("name")
    inc_dob = _to_date(typed.get("date_of_birth"))
    inc_tokens = _norm_tokens(name)
    sig_tokens = [t for t in inc_tokens if len(t) >= 4]
    if not sig_tokens:
        return None

    # Candidates: fighters sharing at least one significant name token
    clauses = " OR ".join(["name ILIKE %s"] * len(sig_tokens))
    params = [f"%{t}%" for t in sig_tokens]
    cur.execute(
        f"SELECT id, name, date_of_birth, last_fight_date, career_debut_date, "
        f"fight_history FROM fighters WHERE {clauses} LIMIT 400",
        params,
    )
    rows = cur.fetchall()
    if not rows:
        return None

    inc_opps = _fh_opponents(typed.get("fight_history"))
    inc_lf  = _to_date(typed.get("last_fight_date"))
    inc_deb = _to_date(typed.get("career_debut_date"))

    best, best_score = None, 0
    for fid, fname, fdob, flf, fdeb, ffh in rows:
        if not _tokens_match_flex(inc_tokens, _norm_tokens(fname or "")):
            continue
        fdob = _to_date(fdob)
        # Contradiction: two different known DOBs = certain namesake
        if inc_dob and fdob and inc_dob != fdob:
            continue

        overlap = _opponents_overlap(inc_opps, _fh_opponents(ffh)) if inc_opps and ffh else 0
        dates_match = bool(inc_lf and inc_deb
                           and inc_lf == _to_date(flf) and inc_deb == _to_date(fdeb))
        same_dob = bool(inc_dob and fdob and inc_dob == fdob)

        if overlap >= 2 or dates_match or same_dob:
            score = overlap * 10 + (5 if dates_match else 0) + (3 if same_dob else 0)
            if score > best_score:
                best, best_score = fid, score

    return best


def upsert_supabase(row: dict) -> str:
    """
    Insert or update a fighter in the database.

    - Smart lookup by external ID (more robust than a plain LOWER(name))
    - ALWAYS_REFRESH overwrites, other fields: COALESCE (filled if empty)
    - DB errors are caught and reported cleanly (rollback + retry possible)
    """
    name = str(row.get("name", "")).strip()
    if not name:
        return "/!\\ No name -> row ignored"

    # We do NOT fill ufc_id / fightmatrix_id / mma_com_id from the URLs.
    # They are updated after the INSERT so that they all equal id.
    row["ufc_id"] = None
    row["fightmatrix_id"] = None
    row["mma_com_id"] = None

    typed = _prepare_row(row)

    conn = get_connection()
    if not conn:
        return "/!\\ Supabase connection failed (check .env)"

    try:
        cur = conn.cursor()
        fighter_id, match_by = _find_existing(cur, typed)

        # Which sources contributed to this row? (needed for the
        # AUTHORITATIVE_SOURCES logic: a "soft" field is only overwritten
        # if a source authoritative for that field actually scraped).
        scraped_sources = _sources_in_row(row)

        if fighter_id is not None:
            # mise a jour
            # For each column:
            # 1) name / created_at: never touched
            # 2) notes: merge + dedupe with the existing value (always authoritative)
            # 3) ALWAYS_REFRESH (records, ranks, ...): OVERWRITE
            # 4) AUTHORITATIVE_SOURCES[col] intersected with scraped_sources non-empty: OVERWRITE
            # (at least one authoritative source contributed to the scrape)
            # 5) Otherwise: COALESCE (fills if empty, keeps otherwise)
            always_set, always_vals, fill_set, fill_vals = [], [], [], []

            # Special case "notes" + "data_source": merged with the existing value + dedupe.
            # Without this, data_source in ALWAYS_REFRESH would overwrite the history of the URLs
            # scraped in previous sessions.
            cur.execute(
                "SELECT notes, data_source, fight_history FROM fighters WHERE id = %s",
                (fighter_id,),
            )
            existing_row = cur.fetchone()
            existing_notes  = existing_row[0] if existing_row else None
            existing_dsrc   = existing_row[1] if existing_row else None
            existing_fh     = existing_row[2] if existing_row else None

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

            # fight_history: arbitrated by SOURCE HIERARCHY (see SOURCE_PRIORITY).
            # The stored history is only overwritten if the incoming source is AT LEAST
            # as reliable (Tapology > UFC-FR=FM > UG). Without this safeguard, a later FM
            # or UFC-FR scrape overwrote the Tapology history -> wrong records.
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
                    and bool(scraped_sources & AUTHORITATIVE_SOURCES[col])
                )

                if col in ALWAYS_REFRESH or is_authoritative:
                    always_set.append(f"{col} = %s")
                    always_vals.append(v)
                else:
                    fill_set.append(f"{col} = COALESCE({col}, %s)")
                    fill_vals.append(v)

            # NOTE: no force-NULL for ufc_official_rank when champion=TRUE,
            # because the current convention is ufc_official_rank=0 for a champion
            # (already set by parse_ufc_fr_html). The old code created a
            # "multiple assignments to same column" conflict on the UPDATE.

            if always_set or fill_set:
                set_parts = always_set + fill_set
                vals = always_vals + fill_vals + [fighter_id]
                cur.execute(
                    f"UPDATE fighters SET {', '.join(set_parts)} WHERE id = %s", vals
                )
            # Order matters: Tapology is authoritative (record from history),
            # then total >= ufc, then other = total-ufc, then total_fights = sum.
            _recompute_record_from_history(cur, fighter_id)
            _recompute_record_total(cur, fighter_id)
            _recompute_record_other(cur, fighter_id)
            _recompute_total_fights(cur, fighter_id)
            score = _recompute_quality_score(cur, fighter_id)
            conn.commit()
            srcs_str = ",".join(sorted(scraped_sources)) if scraped_sources else "?"
            action = f"UPDATE  id={fighter_id}  (match: {match_by}, srcs: {srcs_str}, qs={score})"
        else:
            # insertion
            cols = [c for c in DB_COLUMNS if typed.get(c) is not None]
            placeholders = ", ".join(["%s"] * len(cols))
            vals = [typed[c] for c in cols]
            cur.execute(
                f"INSERT INTO fighters ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING id",
                vals,
            )
            fighter_id = cur.fetchone()[0]
            # Align ufc_id / fightmatrix_id / mma_com_id on the sequential id
            cur.execute(
                "UPDATE fighters SET ufc_id = %s, fightmatrix_id = %s, mma_com_id = %s "
                "WHERE id = %s",
                (str(fighter_id), str(fighter_id), str(fighter_id), fighter_id),
            )
            _recompute_record_from_history(cur, fighter_id)
            _recompute_record_total(cur, fighter_id)
            _recompute_record_other(cur, fighter_id)
            _recompute_total_fights(cur, fighter_id)
            score = _recompute_quality_score(cur, fighter_id)
            conn.commit()
            action = f"INSERT  id={fighter_id}  (qs={score})"

        cur.close()
        conn.close()
        return f"[Supabase] {action}  name={name}"

    except psycopg2.IntegrityError as e:
        conn.rollback()
        conn.close()
        # UNIQUE conflict: another fighter already has this external ID
        # or this (name, dob). We report it clearly.
        return f"/!\\ Uniqueness conflict ({name}) : {e.diag.message_primary if e.diag else e}"
    except Exception as e:
        conn.rollback()
        conn.close()
        LOG.exception(f"Error upsert_supabase for {name}")
        return f"/!\\ DB error ({name}) : {e}"


def main():
    args = [a for a in sys.argv[1:] if a.strip()]

    if args:
        row = scrape(args)
        print_summary(row)
        print("\n" + upsert_supabase(row))
        return

    print("=" * 60)
    print("  scrape_mma_supabase  (writing into Supabase)")
    print("  1 URL per fighter. Q to quit.")
    print("=" * 60)

    fighter_num = 1
    while True:
        print()
        try:
            url = input(f"  Fighter #{fighter_num} - URL (or Q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if url.lower() in ("q", "quit", "exit", ""):
            print("  Goodbye.")
            break
        if not url:
            continue

        row = scrape([url])
        print_summary(row)
        print(f"\n  {upsert_supabase(row)}")
        fighter_num += 1
        time.sleep(0.5)


if __name__ == "__main__":
    main()