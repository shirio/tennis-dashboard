#!/usr/bin/env python3
"""
Re-scrape UT regular season data cleanly using the subflight-level scraper
(_scrape_subflight + _match_key dedup). Preserves championship subflights
which are already clean and have 100% match ID coverage.

Root cause of old data problems:
  - Matches scraped per-team independently → no dedup → 2× duplicates
  - "Sports Mall vs Sports Mall" self-match entries
  - Only ~35% of matches had TL match IDs

Fix: run_mode1 uses _scrape_subflight which deduplicates via _match_key
(sorted team names + date hash), so each A-vs-B match appears exactly once.

Usage:
    python3 scripts/rescrape_ut.py
    python3 scripts/rescrape_ut.py --dry-run    # scrape only, don't save
    python3 scripts/rescrape_ut.py --headless
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
from scrapers.scrape_tennislink import (
    login, run_mode1, DELAY,
)

DATA_DIR = Path("data")
UT30_PATH = DATA_DIR / "standings_ut_30.json"
UT35_PATH = DATA_DIR / "standings_ut_35.json"


def _load_championship_subflights(path: Path) -> list[dict]:
    """Extract championship subflights from current data — these are clean."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    champ_sfs = [
        sf for sf in data.get("subflights", [])
        if "champ" in sf.get("flight_label", "").lower()
    ]
    return champ_sfs


def _merge_championships(path: Path, champ_sfs: list[dict]):
    """Append championship subflights to the freshly-scraped regular season data."""
    if not champ_sfs or not path.exists():
        return
    data = json.loads(path.read_text())
    # Avoid double-inserting if run_mode1 already picked them up
    existing_labels = {sf["flight_label"] for sf in data.get("subflights", [])}
    new_sfs = [sf for sf in champ_sfs if sf["flight_label"] not in existing_labels]
    if new_sfs:
        data["subflights"].extend(new_sfs)
        path.write_text(json.dumps(data, indent=2))
        print(f"  Merged {len(new_sfs)} championship subflight(s) back into {path.name}")
    else:
        print(f"  Championship subflights already present in {path.name}")


def _print_summary(path: Path):
    if not path.exists():
        print(f"  {path.name}: not found")
        return
    data = json.loads(path.read_text())
    sfs = data.get("subflights", [])
    total_matches = sum(len(sf.get("matches", [])) for sf in sfs)
    total_with_ids = sum(
        sum(1 for m in sf.get("matches", []) if m.get("tl_match_id"))
        for sf in sfs
    )
    print(f"\n  {path.name}: {len(sfs)} subflights, {total_matches} matches, "
          f"{total_with_ids}/{total_matches} with TL match IDs")
    for sf in sfs:
        matches = sf.get("matches", [])
        with_ids = sum(1 for m in matches if m.get("tl_match_id"))
        with_lines = sum(1 for m in matches if m.get("lines"))
        is_champ = "champ" in sf.get("flight_label", "").lower()
        tag = " [CHAMPIONSHIPS]" if is_champ else ""
        print(f"    {sf['flight_label']}: {len(matches)} matches, "
              f"{with_ids} IDs, {with_lines} w/lines{tag}")


def main():
    parser = argparse.ArgumentParser(description="Re-scrape UT regular season data")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and print summary but don't rebuild dashboards")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("[error] TENNISLINK_USER / TENNISLINK_PASS not set in .env")
        sys.exit(1)

    # 1. Save championship subflights before overwriting
    print("Saving championship subflights from current data...")
    champ_30 = _load_championship_subflights(UT30_PATH)
    champ_35 = _load_championship_subflights(UT35_PATH)
    print(f"  3.0: {len(champ_30)} championship subflight(s)")
    print(f"  3.5: {len(champ_35)} championship subflight(s)")

    # 2. Back up current files
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for path in [UT30_PATH, UT35_PATH]:
        if path.exists():
            bak = path.with_suffix(f".{ts}.bak.json")
            shutil.copy2(path, bak)
            print(f"  Backed up {path.name} → {bak.name}")

    # 3. Re-scrape regular season via run_mode1 (uses _scrape_subflight + _match_key dedup)
    print("\nStarting TennisLink re-scrape for UT...")
    print("  Uses _scrape_subflight which deduplicates via sorted-team-names+date hash")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        try:
            login(page, username, password)
            run_mode1(page, 2026, "UT")
        finally:
            browser.close()

    # 4. Merge championship subflights back in
    print("\nMerging championship subflights back...")
    _merge_championships(UT30_PATH, champ_30)
    _merge_championships(UT35_PATH, champ_35)

    # 5. Print summary
    print("\n=== Post-scrape summary ===")
    _print_summary(UT30_PATH)
    _print_summary(UT35_PATH)

    if args.dry_run:
        print("\n[dry-run] Skipping rebuild. Review data files before rebuilding.")
        return

    # 6. Rebuild + push
    print("\nRebuilding dashboards...")
    result = subprocess.run([sys.executable, "rebuild.py", "--state", "UT"],
                            cwd=str(Path.cwd()))
    if result.returncode != 0:
        print("[warn] rebuild.py exited non-zero — review output before pushing")
        return

    print("\nCommitting and pushing...")
    subprocess.run(["git", "add",
                    "data/standings_ut_30.json", "data/standings_ut_35.json",
                    "women_ut_30.html", "women_ut_35.html"],
                   cwd=str(Path.cwd()))
    subprocess.run(["git", "commit", "-m",
                    "UT: re-scrape regular season with proper dedup via _scrape_subflight\n\n"
                    "Fixes duplicate matches, missing TL match IDs, and corrupted entries\n"
                    "(Sports Mall vs Sports Mall) from old per-team scraping approach."],
                   cwd=str(Path.cwd()))
    subprocess.run(["git", "push"], cwd=str(Path.cwd()))
    print("\nDone.")


if __name__ == "__main__":
    main()
