#!/usr/bin/env python3
"""
Orchestrate full scraping pipeline for all states (or a single state).

Steps per state:
  1. Discover areas (if not cached in regions.json)
  2. Scrape TennisLink league matches (3.0 + 3.5)
  3. Scrape TennisLink districts (if completed for that state)
  4. Scrape tennisrecord.com baseline ratings
  5. Compute player stats

After all states:
  6. Run unified cross-state ratings
  7. Build all dashboards + sectionals page

Usage:
    python3 scrape_all.py                     # all states with districts
    python3 scrape_all.py --state CO          # just Colorado
    python3 scrape_all.py --skip-tennislink   # skip TennisLink, just baselines + rebuild
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path("data")
REGIONS_JSON = DATA_DIR / "regions.json"


def run_cmd(cmd: list[str], desc: str):
    print(f"\n{'='*60}")
    print(f"  {desc}")
    print(f"  $ {' '.join(cmd)}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, cwd=str(Path.cwd()))
    if result.returncode != 0:
        print(f"  [warn] Command exited with code {result.returncode}")
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Full scrape pipeline for all states")
    parser.add_argument("--state", default=None,
                        help="Process only this state (e.g. CO, UT)")
    parser.add_argument("--skip-tennislink", action="store_true",
                        help="Skip TennisLink scraping (just baselines + rebuild)")
    parser.add_argument("--skip-baselines", action="store_true",
                        help="Skip tennisrecord baseline scraping")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    regions = json.loads(REGIONS_JSON.read_text()) if REGIONS_JSON.exists() else {}
    all_states = list(regions.get("states", {}).keys())

    if args.state:
        states = [args.state.upper()]
    else:
        states = [s for s in all_states if regions["states"][s].get("has_districts", False)]

    print(f"Processing states: {', '.join(states)}")

    # Per-state scraping
    for state_code in states:
        cfg = regions.get("states", {}).get(state_code, {})
        print(f"\n{'#'*60}")
        print(f"  STATE: {state_code} ({cfg.get('label', state_code)})")
        print(f"{'#'*60}")

        if not args.skip_tennislink:
            # Discover areas if not cached
            if not cfg.get("areas"):
                run_cmd([sys.executable, "scrapers/scrape_tennislink.py",
                         "--mode", "discover-areas",
                         "--state", state_code,
                         "--year", str(args.year)] +
                        (["--headless"] if args.headless else []),
                        f"Discovering areas for {state_code}")
                # Reload regions after discovery
                regions = json.loads(REGIONS_JSON.read_text())

            # Scrape league matches (3.0 + 3.5)
            run_cmd([sys.executable, "scrapers/scrape_tennislink.py",
                     "--mode", "1",
                     "--state", state_code,
                     "--year", str(args.year)] +
                    (["--headless"] if args.headless else []),
                    f"Scraping {state_code} league data")

            # Scrape districts
            if cfg.get("has_districts", False):
                run_cmd([sys.executable, "scrapers/scrape_tennislink.py",
                         "--mode", "districts",
                         "--state", state_code,
                         "--year", str(args.year)] +
                        (["--headless"] if args.headless else []),
                        f"Scraping {state_code} districts")

        # Scrape tennisrecord baselines
        if not args.skip_baselines:
            run_cmd([sys.executable, "scrapers/scrape_tennisrecord.py",
                     "--state", state_code],
                    f"Scraping tennisrecord baselines for {state_code}")

    # Rebuild everything
    run_cmd([sys.executable, "rebuild.py"],
            "Rebuilding ratings + dashboards for all states")

    print(f"\nAll done! Processed: {', '.join(states)}")


if __name__ == "__main__":
    main()
