#!/usr/bin/env python3
"""
Recompute ratings and rebuild dashboards from existing match data.

Use this after changing the ratings algorithm or HTML templates —
no scraping needed. All input data (players.json, matches_all_players.json)
is read as-is from the data/ directory.

Usage:
    python3 rebuild.py
"""
from engine.ratings import run_ratings
from engine.build_html import build_dashboards

run_ratings()
build_dashboards()
print("\n✓ Done! Dashboards rebuilt. Don't forget to commit + push.")
