"""
fix_duplicates.py
Diagnose and merge duplicate fighters in the database.

Context: the scraper logs "CONFLICT: duplicate key value violates unique
constraint uq_fighter_name_dob" when lookup() does not find an existing
fighter through name normalization, but the (name, dob) constraint
blocks the INSERT. This happens when two rows have the same (name, dob)
under different IDs, or when the name is slightly different (e.g.
"Li Jingliang" vs "Jingliang Li") but the DOB links them.

USAGE
    # List the duplicates without touching anything
    py fix_duplicates.py

    # Include near-duplicates (same normalized name, different DOB)
    py fix_duplicates.py --include-name-only

    # Apply the automatic merges (safe: keeps the best row)
    py fix_duplicates.py --commit

    # Re-scrape the conflicting URLs after the fix
    py fix_duplicates.py --commit && py scrape_batch.py --file failed_urls.txt --yes

MERGE LOGIC
For each group of duplicates:
  - Keep the row with the highest data_quality_score (or the most URLs)
  - Merge the data_sources (union of URLs)
  - Fill the winner's empty fields with the loser's values
  - Delete the duplicate(s)
"""

import sys
import re
import unicodedata
import argparse
from pathlib import Path
from collections import defaultdict

SCRAPERS = Path(__file__).parent  # scraper/ directory (cross imports)
if str(SCRAPERS) not in sys.path:
    sys.path.insert(0, str(SCRAPERS))
from db_connection import get_connection


# Normalization (same logic as the batch scraper)

def normalize_name(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[‘’ʼ']", "", s)    # smart quotes / apostrophes
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)          # "(nickname)" -> espace
    s = re.sub(r"[^a-zA-Z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def merge_data_sources(*args: str | None) -> str:
    seen, parts = set(), []
    for ds in args:
        for p in str(ds or "").split("|"):
            p = p.strip().rstrip("/")
            if p and p not in seen:
                seen.add(p)
                parts.append(p)
    return " | ".join(parts) if parts else ""


# Fields to merge (take the loser's non-NULL value if the winner is NULL)

MERGE_FIELDS = [
    "nickname", "gender", "date_of_birth", "nationality",
    "photo_url", "photo_thumbnail_url",
    "weight_class_current", "weight_class_origin",
    "height_inches", "reach_inches", "stance", "first_sport",
    "record_total_wins", "record_total_losses", "record_total_draws", "record_total_nc",
    "record_ufc_wins", "record_ufc_losses", "record_ufc_draws",
    "wins_by_ko_tko", "wins_by_submission", "wins_by_decision",
    "losses_by_ko_tko", "losses_by_submission", "losses_by_decision",
    "fightmatrix_rating_points",
    "fightmatrix_big_league_record", "fightmatrix_540_metric", "fightmatrix_quality_perf_pct",
    "current_league", "is_active", "is_ufc_champion",
    "ufc_official_rank", "ufc_p4p_rank",
    "notes", "fight_history",
]

INT_FIELDS = {
    "record_total_wins", "record_total_losses", "record_total_draws", "record_total_nc",
    "record_ufc_wins", "record_ufc_losses", "record_ufc_draws",
}


def merge_int(a, b):
    """Take the MAX of the two integer values (the most complete one)."""
    ia = int(a) if a not in (None, "") else 0
    ib = int(b) if b not in (None, "") else 0
    return max(ia, ib) or None


def normalize_sorted(name: str) -> str:
    """Normalized name with SORTED tokens -> insensitive to first/last name order.
    'Michael Chandler' and 'Chandler Michael' both give 'chandler michael'."""
    nn = normalize_name(name)
    return " ".join(sorted(nn.split())) if nn else ""


def clean_empty_placeholders(cur, conn, rows, commit=False):
    """Delete EMPTY fighters (shells) whose name (sorted tokens) matches
    a REAL fighter. Safe: only rows without any data are deleted.

    rows: tuples (id, name, dob, ds, qs, wins, losses, gender, wc, fh, notes)
    """
    # idx : 0 id, 1 name, 2 dob, 5 wins, 6 losses, 9 fight_history
    def is_empty(r):
        return (not r[9]) and (not r[2]) and (r[5] or 0) == 0 and (r[6] or 0) == 0

    real_by_sorted: dict[str, list] = defaultdict(list)
    for r in rows:
        if not is_empty(r):
            real_by_sorted[normalize_sorted(r[1])].append(r)

    to_delete = []   # (empty_row, winner_row)
    for r in rows:
        if not is_empty(r):
            continue
        key = normalize_sorted(r[1])
        if key and key in real_by_sorted:
            # matches a real fighter (we take the first one as the indicative "winner")
            to_delete.append((r, real_by_sorted[key][0]))

    print(f"\n  Empty shells to delete: {len(to_delete)}")
    for empty_r, winner_r in to_delete:
        print(f"   DELETE id={empty_r[0]:6d} '{empty_r[1]}'  (empty)"
              f"   ->  keep id={winner_r[0]:6d} '{winner_r[1]}'")

    if not to_delete:
        print("  Nothing to clean.")
        return

    if not commit:
        print(f"\n  [DRY-RUN] {len(to_delete)} shells would be deleted. "
              f"Run again with --commit.")
        return

    deleted = 0
    for empty_r, _winner in to_delete:
        try:
            cur.execute("SAVEPOINT del_empty")
            cur.execute("DELETE FROM fighters WHERE id = %s", (empty_r[0],))
            cur.execute("RELEASE SAVEPOINT del_empty")
            deleted += 1
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT del_empty")
            cur.execute("RELEASE SAVEPOINT del_empty")
            print(f"  SKIP id={empty_r[0]} ({e!s:.100})")
    conn.commit()
    print(f"\n  [OK] {deleted} empty shells deleted.")


_EXTRA_MERGE_FIELDS = [
    "name",
    "last_fight_date", "days_inactive", "total_fights",
    "win_percentage_int", "finish_rate_int",
    "current_streak", "last_5_results",
    "record_other_wins", "record_other_losses", "record_other_draws",
    "split_decision_wins", "split_decision_losses",
    "career_debut_date",
]

# Values considered "empty" for a field (in addition to None/""/0)
_EMPTY_VALS = {None, "", 0, "Unknown", "unknown"}


def merge_by_ids(cur, conn, keep_id: int, discard_id: int, commit: bool):
    """Merge two fighters by explicit IDs: KEEP keeps its data + DISCARD's,
    DISCARD is deleted. Tapology remains the priority for fight_history and the name."""
    all_fields = ["id", "name", "data_source", "data_quality_score"] + MERGE_FIELDS + _EXTRA_MERGE_FIELDS
    # Deduplicate while keeping order
    seen_f, col_list_fields = set(), []
    for f in all_fields:
        if f not in seen_f:
            seen_f.add(f); col_list_fields.append(f)
    col_list = ", ".join(col_list_fields)

    cur.execute(f"SELECT {col_list} FROM fighters WHERE id = ANY(%s)", ([keep_id, discard_id],))
    detail_rows = {r[0]: r for r in cur.fetchall()}
    if keep_id not in detail_rows:
        print(f"  ERROR: id={keep_id} not found in the database"); return
    if discard_id not in detail_rows:
        print(f"  ERROR: id={discard_id} not found in the database"); return

    cols = col_list_fields
    winner_row = dict(zip(cols, detail_rows[keep_id]))
    loser_row  = dict(zip(cols, detail_rows[discard_id]))

    print(f"\n  KEEP    id={keep_id}    {winner_row['name']!r}  qs={winner_row['data_quality_score']}  src={winner_row['data_source']}")
    print(f"  DELETE  id={discard_id}  {loser_row['name']!r}  qs={loser_row['data_quality_score']}  src={loser_row['data_source']}")

    updates = {"data_source": merge_data_sources(winner_row["data_source"], loser_row["data_source"])}

    SOURCE_PRIO = {"tapology": 4, "ufcstats": 3, "event_scrape": 3,
                   "ufc_fr": 2, "ufc": 2, "fightmatrix": 2, "underground": 1}

    def _fh_source(fh_json):
        if not fh_json: return ""
        try:
            import json as _json; return _json.loads(fh_json).get("source", "") or ""
        except Exception: return ""

    def _src_prio(ds: str | None) -> int:
        ds = ds or ""
        if "tapology.com" in ds: return 4
        if "ufcstats" in ds or "event_scrape" in ds: return 3
        if "ufc-fr.com" in ds or "ufc.com" in ds: return 2
        if "fightmatrix" in ds: return 2
        return 0

    # fight_history: most reliable source
    best_fh, best_prio = None, -1
    for row in [winner_row, loser_row]:
        fh = row.get("fight_history")
        if not fh: continue
        prio = SOURCE_PRIO.get(_fh_source(fh), 0)
        if prio > best_prio:
            best_prio, best_fh = prio, fh
    if best_fh:
        updates["fight_history"] = best_fh

    # name: if DISCARD comes from Tapology (the most reliable source for names),
    # take its name unless KEEP already has that same name.
    loser_ds = loser_row.get("data_source") or ""
    if "tapology.com" in loser_ds and loser_row.get("name") and loser_row["name"] != winner_row.get("name"):
        updates["name"] = loser_row["name"]

    # all other fields: KEEP if non-empty, otherwise DISCARD
    merge_all = [f for f in cols if f not in ("id", "name", "data_source", "data_quality_score", "fight_history")]
    for field in merge_all:
        winner_val = winner_row.get(field)
        loser_val  = loser_row.get(field)
        if winner_val in _EMPTY_VALS and loser_val not in _EMPTY_VALS:
            updates[field] = loser_val
        if field in INT_FIELDS:
            mx = merge_int(winner_val, loser_val)
            if mx: updates[field] = mx

    print(f"\n  Fields to update on id={keep_id} :")
    for k, v in updates.items():
        val_str = str(v)[:80] + ("…" if len(str(v)) > 80 else "")
        print(f"   {k} = {val_str}")

    if not commit:
        print(f"\n  [DRY-RUN] Run again with --commit to apply.")
        return

    try:
        cur.execute("SAVEPOINT merge_manual")
        if updates:
            set_parts = [f"{k} = %s" for k in updates]
            cur.execute(
                f"UPDATE fighters SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = %s",
                list(updates.values()) + [keep_id]
            )
        cur.execute("DELETE FROM fighters WHERE id = %s", (discard_id,))
        cur.execute("RELEASE SAVEPOINT merge_manual")
        conn.commit()
        print(f"\n  [OK] id={keep_id} enriched, id={discard_id} deleted.")
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT merge_manual")
        cur.execute("RELEASE SAVEPOINT merge_manual")
        conn.rollback()
        print(f"\n  ERROR during merge: {e}")


def main():
    parser = argparse.ArgumentParser(description="Diagnose and merge duplicate fighters")
    parser.add_argument("--include-name-only", action="store_true",
                        help="Include name-only duplicates (different or NULL DOB on both sides)")
    parser.add_argument("--empty-placeholders", action="store_true",
                        help="Delete EMPTY shells (0-0, no history nor DOB) "
                             "whose name (sorted tokens) matches a real fighter. Handles "
                             "inverted names like 'Chandler Michael' vs 'Michael Chandler'.")
    parser.add_argument("--merge-ids", nargs=2, type=int, metavar=("KEEP_ID", "DISCARD_ID"),
                        help="Merge two fighters by explicit IDs: KEEP keeps its data "
                             "+ DISCARD's, DISCARD is deleted.")
    parser.add_argument("--commit", action="store_true",
                        help="Apply the merges (otherwise dry-run)")
    parser.add_argument("--failed-log", default="failed_urls.txt",
                        help="Log file of the conflicting URLs")
    args = parser.parse_args()

    conn = get_connection()
    if not conn:
        print("ERROR: unable to connect to the database")
        sys.exit(1)

    cur = conn.cursor()

    # -- Manual merge mode by IDs --
    if args.merge_ids:
        keep_id, discard_id = args.merge_ids
        merge_by_ids(cur, conn, keep_id, discard_id, commit=args.commit)
        cur.close(); conn.close(); return

    # 1. Load all the fighters
    print("  Loading the fighters...", end=" ", flush=True)
    cur.execute("""
        SELECT id, name, date_of_birth, data_source, data_quality_score,
               record_total_wins, record_total_losses, gender,
               weight_class_current, fight_history, notes
        FROM fighters
        ORDER BY id
    """)
    rows = cur.fetchall()
    print(f"{len(rows)} fighters")

    # -- Dedicated mode: delete EMPTY shells with an inverted/duplicated name --
    if args.empty_placeholders:
        clean_empty_placeholders(cur, conn, rows, commit=args.commit)
        cur.close()
        conn.close()
        return

    # 2. Detect duplicates by (normalized name, DOB)
    # Group 1: same (name_norm, dob) - certain duplicates
    # Group 2: same name_norm only - probable duplicates (with --include-name-only)

    by_name_dob:  dict[tuple, list] = defaultdict(list)
    by_name_only: dict[str, list]   = defaultdict(list)

    for row in rows:
        fid, name, dob, ds, qs, wins, losses, gender, wc, fh, notes = row
        nn = normalize_name(name)
        key_full = (nn, str(dob) if dob else None)
        by_name_dob[key_full].append(row)
        by_name_only[nn].append(row)

    dupes_exact = {k: v for k, v in by_name_dob.items() if len(v) >= 2}
    dupes_name  = {k: v for k, v in by_name_only.items()
                   if len(v) >= 2 and k not in {kk[0] for kk in dupes_exact}}

    print(f"\n  Exact duplicates (same name + DOB)      : {len(dupes_exact)} groupes")
    print(f"  Near-duplicates (name only, different DOB): {len(dupes_name)} groupes")

    # 3. Affichage
    groups_to_fix = list(dupes_exact.items())
    if args.include_name_only:
        groups_to_fix += [(k, v) for k, v in dupes_name.items()]

    if not groups_to_fix:
        print("\n  No duplicate found! Database is clean.")
        cur.close()
        conn.close()
        return

    def extract_fm_ids(data_source: str | None) -> set[str]:
        """Extract the numeric FM IDs from data_source."""
        ids = set()
        for part in str(data_source or "").split("|"):
            m = re.search(r"fightmatrix\.com/fighter-profile/[^/]+/(\d+)", part)
            if m:
                ids.add(m.group(1))
        return ids

    merges:  list[tuple[int, list[int]]] = []   # (id_gagnant, [ids_a_supprimer])
    skipped: list[tuple]                 = []   # groupes homonyms

    safe_count = 0
    homo_count = 0

    for key, group in groups_to_fix:
        # Detect whether several different FM IDs are in the group -> namesakes
        all_fm_ids: set[str] = set()
        for row in group:
            all_fm_ids.update(extract_fm_ids(row[3]))
        is_homonym = len(all_fm_ids) >= 2

        # Detect whether several different UFC-FR IDs are in the group
        ufr_ids: set[str] = set()
        for row in group:
            for part in str(row[3] or "").split("|"):
                m = re.search(r"ufc-fr\.com/combattant-(\d+)", part)
                if m:
                    ufr_ids.add(m.group(1))
        if len(ufr_ids) >= 2:
            is_homonym = True

        # Readable normalized name (key can be a tuple or a str)
        if isinstance(key, tuple):
            display_name, display_dob = key
        else:
            display_name, display_dob = key, "?"

        if is_homonym:
            homo_count += 1
            skipped.append((display_name, group))
            continue

        # Sort: Tapology > quality_score > number of sources > wins
        # Tapology is always preferred as the winner because it provides the source
        # autoritaire (fight_history complet, photo, bio).
        def sort_key(r):
            has_tap = 1 if "tapology.com" in (r[3] or "") else 0
            qs      = r[4] or 0
            n_src   = len([p for p in str(r[3] or "").split("|") if p.strip()])
            wins    = r[5] or 0
            return (has_tap, qs, n_src, wins)
        group_sorted = sorted(group, key=sort_key, reverse=True)
        winner = group_sorted[0]
        losers = group_sorted[1:]
        safe_count += 1

        print(f"  [{safe_count}] {display_name}")
        print(f" ├ KEEP   id={winner[0]:6d} {winner[1]:<30} qs={winner[4]} src={winner[3]}")
        for loser in losers:
            print(f" └ DELETE id={loser[0]:6d} {loser[1]:<30} qs={loser[4]} src={loser[3]}")
        print()

        merges.append((winner[0], [l[0] for l in losers]))

    if skipped:
        print(f"\n  {'='*60}")
        print(f"  HOMONYMS IGNORED ({homo_count}) - different FM IDs or UFC-FR IDs")
        print(f"  {'='*60}")
        for display_name, group in skipped:
            print(f"\n  ⚠  {display_name}")
            for row in group:
                print(f"     id={row[0]:6d}  {row[1]:<30}  qs={row[4]}  src={row[3]}")
        print()

    if not args.commit:
        print(f"  [DRY-RUN] {len(merges)} merges would be applied ({homo_count} homonyms ignored).")
        print("  Run again with --commit to apply.")
        cur.close()
        conn.close()
        return

    # 4. Confirmation before execution
    print(f"\n  {'='*60}")
    print(f"  {len(merges)} merges to apply, {homo_count} homonyms ignored.")
    print(f"  {'='*60}")
    try:
        rep = input("  Confirm these merges? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        rep = "n"
    if rep != "y":
        print("  Cancelled.")
        cur.close(); conn.close(); return

    # 5. Apply the merges
    print(f"\n  Applying {len(merges)} merges...")

    # Reload all the fields for each row to merge
    col_list = ", ".join(["id", "name", "data_source", "data_quality_score"] + MERGE_FIELDS)
    merged_count = 0
    deleted_count = 0

    for winner_id, loser_ids in merges:
        all_ids = [winner_id] + loser_ids
        cur.execute(
            f"SELECT {col_list} FROM fighters WHERE id = ANY(%s)",
            (all_ids,)
        )
        detail_rows = {r[0]: r for r in cur.fetchall()}
        cols = ["id", "name", "data_source", "data_quality_score"] + MERGE_FIELDS
        col_idx = {c: i for i, c in enumerate(cols)}

        winner_row = dict(zip(cols, detail_rows[winner_id]))

        # Merger data_source
        all_ds = [detail_rows[i][col_idx["data_source"]] for i in all_ids]
        merged_ds = merge_data_sources(*all_ds)
        winner_row["data_source"] = merged_ds

        # For each mergeable field: take the winner's value if non-null, otherwise the loser's
        # Exception for fight_history: always prefer the Tapology source (the most
        # complete and authoritative), even if the winner already has an FM history.
        updates = {"data_source": merged_ds}

        def _fh_source(fh_json: str | None) -> str:
            """Extract the 'source' field of the fight_history JSON ('' if missing/invalid)."""
            if not fh_json:
                return ""
            try:
                import json as _json
                return _json.loads(fh_json).get("source", "") or ""
            except Exception:
                return ""

        TAP_SOURCES = {"tapology", "ufcstats", "event_scrape"}   # priorite >= 3

        for field in MERGE_FIELDS:
            winner_val = winner_row.get(field)

            # fight_history: take the most reliable source of the whole group
            if field == "fight_history":
                best_fh, best_prio = None, -1
                SOURCE_PRIO = {"tapology": 4, "ufcstats": 3, "event_scrape": 3,
                               "ufc_fr": 2, "ufc": 2, "fightmatrix": 2,
                               "underground": 1}
                for row_id in all_ids:
                    row_d = dict(zip(cols, detail_rows[row_id]))
                    fh = row_d.get("fight_history")
                    if not fh:
                        continue
                    prio = SOURCE_PRIO.get(_fh_source(fh), 0)
                    if prio > best_prio:
                        best_prio, best_fh = prio, fh
                if best_fh and best_fh != winner_row.get("fight_history"):
                    updates["fight_history"] = best_fh
                continue

            if winner_val in (None, "", 0):
                for lid in loser_ids:
                    loser_row = dict(zip(cols, detail_rows[lid]))
                    loser_val = loser_row.get(field)
                    if loser_val not in (None, "", 0):
                        updates[field] = loser_val
                        break
            # For INT fields: take the MAX (most complete record)
            if field in INT_FIELDS:
                vals = [detail_rows[i][col_idx[field]] for i in all_ids]
                mx = merge_int(*vals[:2]) if len(vals) >= 2 else vals[0]
                for v in vals[2:]:
                    mx = merge_int(mx, v)
                if mx:
                    updates[field] = mx

        # update the winner + DELETE the losers inside a savepoint
        # (if UniqueViolation on this specific merge: roll back to the savepoint
        # and skip it, without cancelling the other merges already validated)
        try:
            cur.execute("SAVEPOINT merge_one")
            if updates:
                set_parts = [f"{k} = %s" for k in updates]
                vals = list(updates.values()) + [winner_id]
                cur.execute(
                    f"UPDATE fighters SET {', '.join(set_parts)} WHERE id = %s",
                    vals
                )
                merged_count += 1

            for lid in loser_ids:
                cur.execute("DELETE FROM fighters WHERE id = %s", (lid,))
                deleted_count += 1
                print(f"  DELETE id={lid}  merged into id={winner_id}")

            cur.execute("RELEASE SAVEPOINT merge_one")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT merge_one")
            cur.execute("RELEASE SAVEPOINT merge_one")
            print(f"  SKIP id={winner_id} (conflict: {e!s:.120})")

    conn.commit()
    print(f"\n  [OK] {merged_count} fighters enriched, {deleted_count} duplicates deleted")

    # 5. Report the URLs to re-scrape from failed_urls.txt
    failed_path = Path(args.failed_log)
    if failed_path.exists():
        lines = failed_path.read_text(encoding="utf-8").splitlines()
        conflict_urls = [
            l.split("\t")[0].strip()
            for l in lines
            if "CONFLICT" in l and l.strip() and not l.startswith("#")
        ]
        if conflict_urls:
            retry_path = Path("conflict_retry_urls.txt")
            with retry_path.open("w", encoding="utf-8") as f:
                f.write("# URLs to re-scrape after fixing the duplicates\n")
                for u in conflict_urls:
                    f.write(u + "\n")
            print(f"\n  URLs a re-scraper : {retry_path.resolve()}")
            print(f"  ({len(conflict_urls)} URLs)")
            print(f"\n  NEXT STEP:")
            print(f"  py scrape_batch.py --file conflict_retry_urls.txt --yes")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
