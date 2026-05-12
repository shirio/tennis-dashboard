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

Use this after changing the ratings algorithm or HTML templates —
no scraping needed. All input data (players.json, matches_all_players.json)
is read as-is from the data/ directory.

Usage:
    python3 rebuild.py
"""
import json
from pathlib import Path

from scrapers.scrape_tennislink import _compute_player_stats_from_scorecards
from engine.ratings import run_ratings
from engine.build_html import build_dashboards

DATA_DIR = Path("data")

# Recompute per-player lines/W-L/team from scorecards before ratings,
# so manually entered matches (e.g. W7 entered without scraping) are
# reflected in the roster stats.
s30 = json.loads((DATA_DIR / "standings_women_30.json").read_text())
s35 = json.loads((DATA_DIR / "standings_women_35.json").read_text())
all_ntrp = [
    ("3.0", s30.get("subflights", [])),
    ("3.5", s35.get("subflights", [])),
]
_compute_player_stats_from_scorecards(all_ntrp)

run_ratings()
build_dashboards()
print("\n✓ Done! Dashboards rebuilt. Don't forget to commit + push.")
