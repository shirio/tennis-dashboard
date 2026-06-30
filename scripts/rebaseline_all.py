#!/usr/bin/env python3
"""
Re-scrape correct pre-2026 baselines for all players.

Rules:
  1. Last black (non-red) post-rating with date <= 2025 from TennisRecord history
  2. If no pre-2026 black rating, oldest black rating on the page
  3. If no TennisRecord data at all, NTRP default:
     2.5 -> 2.10, 3.0 -> 2.60, 3.5 -> 3.10, 4.0 -> 3.60
  4. Last resort: 3.0

Speed-up features:
  --skip-done     Skip players already baselined from history (don't re-scrape)
  --workers N     Parallel scraping threads (default 5)
  --no-sid-cache  Force re-fetch of ratings tables even if cache exists
  s_id cache:     data/tennisrecord_sids_{state}.json avoids re-fetching area tables
  s_id writeback: discovered s_ids are saved to players.json for future runs
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _ntrp_level(ntrp_str: str) -> str:
    m = re.match(r"(\d\.\d)", ntrp_str or "")
    return m.group(1) if m else ""


def _player_division(p: dict) -> str:
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


def _sid_cache_path(state_code: str) -> Path:
    return DATA_DIR / f"tennisrecord_sids_{state_code.lower()}.json"


def _load_sid_cache(state_code: str) -> dict[str, str] | None:
    path = _sid_cache_path(state_code)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            print(f"  Loaded s_id cache from {path.name} ({len(data)} entries)")
            return data
        except Exception:
            pass
    return None


def _save_sid_cache(state_code: str, mapping: dict[str, str]) -> None:
    path = _sid_cache_path(state_code)
    path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False))
    print(f"  Saved s_id cache to {path.name} ({len(mapping)} entries)")


def _scrape_one(item: tuple) -> tuple:
    """Worker function for parallel scraping. Returns (index, result_dict)."""
    i, p, s_id, division, old_baseline = item
    name = p.get("name", "")
    time.sleep(0.05)  # light throttle per worker
    date_str, baseline, err = get_baseline(name, s_id)
    return (i, {
        "baseline": baseline,
        "err": err,
        "s_id": s_id,
        "division": division,
        "old_baseline": old_baseline,
    })


def main():
    parser = argparse.ArgumentParser(description="Re-scrape correct baselines for all players")
    parser.add_argument("--state", default=None, help="Only process one state (NV, CO, UT, ID)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write players.json")
    parser.add_argument("--save-every", type=int, default=100)
    parser.add_argument("--workers", type=int, default=5, help="Parallel scraping threads")
    parser.add_argument("--skip-done", action="store_true",
                        help="Skip players already baselined from history (no re-scrape)")
    parser.add_argument("--no-sid-cache", action="store_true",
                        help="Force re-fetch of ratings tables even if cache exists")
    args = parser.parse_args()

    players = json.loads(PLAYERS_JSON.read_text())
    regions = _load_regions()

    # ── Step 1: Build name → s_id mapping ────────────────────────────────────
    print("=== Step 1: Building s_id mapping ===")
    name_to_sid: dict[str, str] = {}
    name_to_record: dict[str, dict] = {}
    in_ratings_table: set[str] = set()

    # Seed from existing tennisrecord_id in players.json
    for p in players:
        tid = p.get("tennisrecord_id")
        if tid:
            name_to_sid[_norm(p.get("name", ""))] = tid

    all_states = regions.get("states", regions)
    states_to_process = [args.state.upper()] if args.state else list(all_states.keys())

    newly_discovered_sids: dict[str, str] = {}  # norm_name -> s_id, for writeback

    for state_code in states_to_process:
        state_cfg = all_states.get(state_code, {})
        district = state_cfg.get("tennisrecord_district", state_cfg.get("district", ""))
        single_area = state_cfg.get("tennisrecord_area")
        multi_areas = state_cfg.get("tennisrecord_areas", [])
        areas = [single_area] if single_area else multi_areas
        section = "Intermountain"

        if not district or not areas:
            print(f"  Skipping {state_code}: no district/areas configured")
            continue

        # Try cache first
        cached = None if args.no_sid_cache else _load_sid_cache(state_code)
        if cached is not None:
            for nn, sid in cached.items():
                in_ratings_table.add(nn)
                if sid:
                    name_to_sid[nn] = sid
            continue

        # Cache miss — fetch from TennisRecord
        state_sids: dict[str, str] = {}
        for area_name in areas:
            url = _build_ratings_url(section, district, area_name, "F")
            records = fetch_ratings_table(url)
            for r in records:
                nn = _norm(r["name"])
                in_ratings_table.add(nn)
                sid = r.get("s_id", "")
                state_sids[nn] = sid
                if sid:
                    name_to_sid[nn] = sid
                    newly_discovered_sids[nn] = sid
                name_to_record[nn] = r
            time.sleep(1)

        _save_sid_cache(state_code, state_sids)

    print(f"  Total s_id mappings: {len(name_to_sid)}")

    # ── s_id writeback: save newly discovered s_ids into players.json ────────
    if newly_discovered_sids and not args.dry_run:
        written = 0
        for p in players:
            nn = _norm(p.get("name", ""))
            if not p.get("tennisrecord_id") and nn in newly_discovered_sids:
                p["tennisrecord_id"] = newly_discovered_sids[nn]
                written += 1
        if written:
            print(f"  Wrote {written} new tennisrecord_ids into players.json")
            PLAYERS_JSON.write_text(json.dumps(players, indent=2, ensure_ascii=False))

    # ── Step 2: Scrape baselines ──────────────────────────────────────────────
    if args.state:
        targets = [p for p in players if p.get("state") == args.state.upper()]
    else:
        targets = players

    # Skip players already baselined from history (improvement #4)
    if args.skip_done:
        _done = {"history", "oldest_fallback"}
        skipped = sum(1 for p in targets if p.get("baseline_source") in _done)
        targets = [p for p in targets if p.get("baseline_source") not in _done]
        print(f"\n=== Step 2: Scraping baselines for {len(targets)} players "
              f"({skipped} already done, skipped) ===")
    else:
        print(f"\n=== Step 2: Scraping baselines for {len(targets)} players ===")

    # Split targets into those that need scraping vs instant NTRP defaults
    scrape_targets = []
    default_targets = []
    for i, p in enumerate(targets):
        nn = _norm(p.get("name", ""))
        s_id = name_to_sid.get(nn) or p.get("tennisrecord_id")
        if s_id or nn in in_ratings_table:
            scrape_targets.append((i, p, s_id,
                                   _player_division(p),
                                   p.get("dynamic_rating_baseline")))
        else:
            default_targets.append((i, p))

    # Apply NTRP defaults instantly for players not in ratings table
    for _, p in default_targets:
        division = _player_division(p)
        default = ntrp_default(division)
        if p.get("dynamic_rating_baseline") != default:
            p["dynamic_rating_baseline"] = default
            p["baseline_source"] = "ntrp_default"

    print(f"  {len(scrape_targets)} need TennisRecord scrape, "
          f"{len(default_targets)} get NTRP defaults")

    # Counters (thread-safe)
    lock = threading.Lock()
    counters = {"updated": 0, "from_history": 0, "from_default": 0,
                "errors": 0, "unchanged": 0, "oldest_fallbacks": 0, "done": 0}

    def _apply_result(p: dict, res: dict) -> None:
        baseline = res["baseline"]
        err = res["err"]
        s_id = res["s_id"]
        division = res["division"]
        old_baseline = res["old_baseline"]

        with lock:
            if baseline is not None:
                p["dynamic_rating_baseline"] = round(baseline, 2)
                if s_id:
                    p["tennisrecord_id"] = s_id
                p["baseline_source"] = "oldest_fallback" if err == "oldest_fallback" else "history"
                counters["from_history"] += 1
                if err == "oldest_fallback":
                    counters["oldest_fallbacks"] += 1
                if old_baseline != p["dynamic_rating_baseline"]:
                    counters["updated"] += 1
                else:
                    counters["unchanged"] += 1
            elif err in ("no_rows", "no_black_ratings"):
                p["dynamic_rating_baseline"] = ntrp_default(division)
                p["baseline_source"] = "ntrp_default"
                counters["from_default"] += 1
                if old_baseline != p["dynamic_rating_baseline"]:
                    counters["updated"] += 1
                else:
                    counters["unchanged"] += 1
            else:
                if (old_baseline
                        and old_baseline not in NTRP_DEFAULTS.values()
                        and old_baseline != LAST_RESORT):
                    counters["unchanged"] += 1
                else:
                    p["dynamic_rating_baseline"] = ntrp_default(division)
                    p["baseline_source"] = "ntrp_default"
                    counters["from_default"] += 1
                    counters["updated"] += 1
                counters["errors"] += 1
            counters["done"] += 1

    save_lock = threading.Lock()
    last_save = [0]

    def _maybe_save(force: bool = False) -> None:
        with save_lock:
            done = counters["done"]
            if force or done - last_save[0] >= args.save_every:
                c = counters
                print(f"  [{done}/{len(scrape_targets)}] "
                      f"{c['updated']} changed, {c['from_history']} from history, "
                      f"{c['from_default']} defaults, {c['errors']} errors, "
                      f"{c['oldest_fallbacks']} oldest-fallback")
                if not args.dry_run:
                    PLAYERS_JSON.write_text(
                        json.dumps(players, indent=2, ensure_ascii=False))
                last_save[0] = done

    # Parallel scraping with ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_scrape_one, item): item for item in scrape_targets}
        for future in as_completed(futures):
            item = futures[future]
            _, p, _, _, _ = item
            try:
                _, res = future.result()
                _apply_result(p, res)
            except Exception as e:
                with lock:
                    counters["errors"] += 1
                    counters["done"] += 1
            _maybe_save()

    _maybe_save(force=True)

    if not args.dry_run:
        PLAYERS_JSON.write_text(json.dumps(players, indent=2, ensure_ascii=False))

    c = counters
    print(f"\n=== Done ===")
    print(f"  Total processed: {len(targets)}")
    print(f"  Changed: {c['updated']}")
    print(f"  From history page: {c['from_history']} ({c['oldest_fallbacks']} oldest-fallback)")
    print(f"  From NTRP default: {c['from_default']}")
    print(f"  Errors: {c['errors']}")
    print(f"  Unchanged: {c['unchanged']}")

    # Show biggest baseline changes
    if name_to_record:
        print(f"\n=== Biggest baseline changes (sample) ===")
        changes = []
        for p in targets:
            if p.get("baseline_source") == "history":
                nn = _norm(p.get("name", ""))
                r = name_to_record.get(nn)
                if r:
                    old = r.get("dynamic_rating")
                    new = p.get("dynamic_rating_baseline")
                    if old and new and abs(old - new) > 0.1:
                        changes.append((p["name"], old, new, old - new))
        changes.sort(key=lambda x: abs(x[3]), reverse=True)
        for name, old_dyn, new_base, diff in changes[:15]:
            print(f"  {name}: 2026_est={old_dyn:.2f} -> baseline={new_base:.2f} (diff={diff:+.2f})")


if __name__ == "__main__":
    main()
