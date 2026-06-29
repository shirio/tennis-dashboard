#!/usr/bin/env python3
"""
Re-scrape correct pre-2026 baselines for all players.

Rules:
  1. Last black (non-red) post-rating with date <= 2025 from TennisRecord history
  2. If no pre-2026 black rating, oldest black rating on the page
  3. If no TennisRecord data at all, NTRP default:
     2.5 -> 2.10, 3.0 -> 2.60, 3.5 -> 3.10, 4.0 -> 3.60
  4. Last resort: 3.0

Steps:
  1. Fetch ratings tables for all states to get s_id for every player
  2. Match ratings table records to players.json entries
  3. For each player with an s_id, scrape their rating history page
  4. Apply the rules above
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrapers.scrape_tennisrecord import (
    fetch_ratings_table, _build_ratings_url, _load_regions, _norm,
    PLAYERS_JSON, HEADERS,
)
from scrape_baselines import get_baseline

NTRP_DEFAULTS = {
    "2.5": 2.10,
    "3.0": 2.60,
    "3.5": 3.10,
    "4.0": 3.60,
}
LAST_RESORT = 3.0


def _ntrp_level(ntrp_str: str) -> str:
    """Extract level from NTRP string like '3.0 C' -> '3.0'."""
    m = re.match(r"(\d\.\d)", ntrp_str or "")
    return m.group(1) if m else ""


def _player_division(p: dict) -> str:
    """Get the player's primary division."""
    for key in ("ntrp_rating", "division"):
        v = p.get(key, "")
        lvl = _ntrp_level(v)
        if lvl:
            return lvl
    for key in ("team_30", "team_35"):
        if p.get(key):
            return "3.0" if "30" in key else "3.5"
    return ""


def ntrp_default(division: str) -> float:
    return NTRP_DEFAULTS.get(division, LAST_RESORT)


def main():
    parser = argparse.ArgumentParser(description="Re-scrape correct baselines for all players")
    parser.add_argument("--state", default=None, help="Only process one state (NV, CO, UT, ID)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write players.json")
    parser.add_argument("--save-every", type=int, default=50)
    args = parser.parse_args()

    players = json.loads(PLAYERS_JSON.read_text())
    regions = _load_regions()

    # Step 1: Fetch ratings tables to build name -> s_id mapping
    print("=== Step 1: Fetching ratings tables for s_id mapping ===")
    name_to_sid: dict[str, str] = {}
    name_to_record: dict[str, dict] = {}
    in_ratings_table: set[str] = set()

    # Also populate from existing tennisrecord_id in players.json
    for p in players:
        tid = p.get("tennisrecord_id")
        if tid:
            name_to_sid[_norm(p.get("name", ""))] = tid

    all_states = regions.get("states", regions)
    states_to_process = [args.state.upper()] if args.state else list(all_states.keys())

    for state_code in states_to_process:
        state_cfg = all_states.get(state_code, {})
        district = state_cfg.get("tennisrecord_district", state_cfg.get("district", ""))
        # Single area or multiple areas
        single_area = state_cfg.get("tennisrecord_area")
        multi_areas = state_cfg.get("tennisrecord_areas", [])
        areas = [single_area] if single_area else multi_areas
        section = "Intermountain"

        if not district or not areas:
            print(f"  Skipping {state_code}: no district or areas configured")
            continue

        for area_name in areas:
            url = _build_ratings_url(section, district, area_name, "F")
            records = fetch_ratings_table(url)
            for r in records:
                nn = _norm(r["name"])
                in_ratings_table.add(nn)
                if r.get("s_id"):
                    name_to_sid[nn] = r["s_id"]
                name_to_record[nn] = r
            time.sleep(1)

    print(f"  Total s_id mappings: {len(name_to_sid)}")

    # Step 2: Filter players to process
    if args.state:
        targets = [p for p in players if p.get("state") == args.state.upper()]
    else:
        targets = players

    print(f"\n=== Step 2: Scraping baselines for {len(targets)} players ===")

    updated = 0
    from_history = 0
    from_default = 0
    errors = 0
    unchanged = 0
    oldest_fallbacks = 0

    for i, p in enumerate(targets):
        name = p.get("name", "")
        nn = _norm(name)
        s_id = name_to_sid.get(nn) or p.get("tennisrecord_id")

        old_baseline = p.get("dynamic_rating_baseline")
        division = _player_division(p)

        can_scrape = s_id or nn in in_ratings_table
        if can_scrape:
            time.sleep(0.15)
            date_str, baseline, err = get_baseline(name, s_id)

            if baseline is not None:
                p["dynamic_rating_baseline"] = round(baseline, 2)
                if s_id:
                    p["tennisrecord_id"] = s_id
                p["baseline_source"] = "oldest_fallback" if err == "oldest_fallback" else "history"
                from_history += 1
                if err == "oldest_fallback":
                    oldest_fallbacks += 1
                if old_baseline != p["dynamic_rating_baseline"]:
                    updated += 1
                else:
                    unchanged += 1
            elif err in ("no_rows", "no_black_ratings"):
                p["dynamic_rating_baseline"] = ntrp_default(division)
                p["baseline_source"] = "ntrp_default"
                from_default += 1
                if old_baseline != p["dynamic_rating_baseline"]:
                    updated += 1
                else:
                    unchanged += 1
            else:
                # Scrape error — keep existing or set default
                if old_baseline and old_baseline not in NTRP_DEFAULTS.values() and old_baseline != LAST_RESORT:
                    unchanged += 1
                else:
                    p["dynamic_rating_baseline"] = ntrp_default(division)
                    p["baseline_source"] = "ntrp_default"
                    from_default += 1
                    updated += 1
                errors += 1
        else:
            # Not in ratings table — use NTRP default
            default = ntrp_default(division)
            if old_baseline != default:
                p["dynamic_rating_baseline"] = default
                p["baseline_source"] = "ntrp_default"
                updated += 1
            else:
                unchanged += 1
            from_default += 1

        if (i + 1) % args.save_every == 0:
            print(f"  [{i+1}/{len(targets)}] {updated} changed, {from_history} from history, "
                  f"{from_default} defaults, {errors} errors, {oldest_fallbacks} oldest-fallback")
            if not args.dry_run:
                PLAYERS_JSON.write_text(json.dumps(players, indent=2, ensure_ascii=False))

    if not args.dry_run:
        PLAYERS_JSON.write_text(json.dumps(players, indent=2, ensure_ascii=False))

    print(f"\n=== Done ===")
    print(f"  Total processed: {len(targets)}")
    print(f"  Changed: {updated}")
    print(f"  From history page: {from_history} ({oldest_fallbacks} oldest-fallback)")
    print(f"  From NTRP default: {from_default}")
    print(f"  Errors: {errors}")
    print(f"  Unchanged: {unchanged}")

    # Show examples of big changes
    print(f"\n=== Biggest baseline changes (sample) ===")
    changes = []
    for p in targets:
        src = p.get("baseline_source", "")
        if src == "history":
            old = None
            for r in name_to_record.values():
                if _norm(r["name"]) == _norm(p.get("name", "")):
                    old = r.get("dynamic_rating")
                    break
            if old and abs(old - p["dynamic_rating_baseline"]) > 0.1:
                changes.append((p["name"], old, p["dynamic_rating_baseline"], old - p["dynamic_rating_baseline"]))
    changes.sort(key=lambda x: abs(x[3]), reverse=True)
    for name, old_dyn, new_base, diff in changes[:15]:
        print(f"  {name}: 2026_est={old_dyn:.2f} -> baseline={new_base:.2f} (diff={diff:+.2f})")


if __name__ == "__main__":
    main()
