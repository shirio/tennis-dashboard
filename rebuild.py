#!/usr/bin/env python3
# ===========================================================================
# IMPORTANT NOTES — read before editing or running this file
# ===========================================================================
# 1. This script does NOT scrape — it only recomputes ratings and rebuilds
#    HTML from whatever is already in data/. Use diff_update.py to fetch new results.
# 2. Use this after changing the ratings algorithm or HTML templates.
# 3. generate_notes.py must be run separately — rebuild.py does NOT call it.
# 4. These notes must be preserved unless the user explicitly says to remove them.
# ===========================================================================
"""
Recompute ratings and rebuild dashboards from existing match data.

Supports multi-state: loads all states' standings from data/ directory,
runs unified cross-state ratings, then builds per-state dashboards
plus the sectionals comparison page.

Usage:
    python3 rebuild.py              # all states
    python3 rebuild.py --state NV   # just Nevada
"""
import argparse
import json
from pathlib import Path

from scrapers.scrape_tennislink import _compute_player_stats_from_scorecards
from engine.normalize import normalize_all
from engine.ratings import run_ratings
from engine.build_html import build_dashboards, build_sectionals_page

DATA_DIR = Path("data")
REGIONS_JSON = DATA_DIR / "regions.json"


def main():
    parser = argparse.ArgumentParser(description="Rebuild ratings + dashboards")
    parser.add_argument("--state", default=None,
                        help="Rebuild only this state (e.g. NV, CO). Default: all states")
    args = parser.parse_args()

    # Determine which states to process
    regions = json.loads(REGIONS_JSON.read_text()) if REGIONS_JSON.exists() else {}
    all_states = list(regions.get("states", {}).keys()) or ["NV"]
    states = [args.state.upper()] if args.state else all_states

    # Recompute per-player lines/W-L/team from scorecards for ALL states, even
    # when --state scopes the dashboard rebuild to one state. Otherwise
    # _compute_player_stats_from_scorecards (which clears every player's
    # per-division fields up front before recomputing only from what it's
    # given) would wipe team_30/35, wl_record_30/35, etc. for every OTHER
    # state's players — breaking cross-state pages like sectionals_30.html.
    all_ntrp = []
    for st in all_states:
        st_lower = st.lower()
        for ntrp_label, suffix in [("3.0", "30"), ("3.5", "35")]:
            path = DATA_DIR / f"standings_{st_lower}_{suffix}.json"
            if path.exists():
                data = json.loads(path.read_text())
                all_ntrp.append((ntrp_label, st, data.get("subflights", [])))
            # Also include districts matches
            d_path = DATA_DIR / f"districts_{st_lower}_{suffix}.json"
            if d_path.exists():
                d_data = json.loads(d_path.read_text())
                if d_data.get("matches"):
                    all_ntrp.append((ntrp_label, st, [{"flight_label": "Districts",
                                                    "teams": d_data.get("teams", []),
                                                    "matches": d_data.get("matches", [])}]))

    if all_ntrp:
        _compute_player_stats_from_scorecards(all_ntrp)

    # Normalize match results (canonical court_winner field)
    normalize_all()

    # Unified cross-state ratings
    run_ratings()

    # Build per-state dashboards
    build_dashboards(states)

    # Build sectionals comparison page
    build_sectionals_page()

    print(f"\nDone! Dashboards rebuilt for {', '.join(states)}.")


if __name__ == "__main__":
    main()
