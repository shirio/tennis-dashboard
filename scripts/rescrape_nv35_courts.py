#!/usr/bin/env python3
"""
Re-scrape all NV 3.5 individual match scorecards from TennisLink to get
authoritative per-court winner data.

Navigates to each match's t=7 scorecard page, re-reads mark.gif court
winners, detects if the page's home/away orientation differs from what
we have stored (and flips accordingly), then updates standings_nv_35.json.

Usage:
    python3 scripts/rescrape_nv35_courts.py
    python3 scripts/rescrape_nv35_courts.py --dry-run   # show diffs only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from time import sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
from diff_update import login, _fetch_match_details, _parse_scorecard_from_t7, DELAY

DATA = Path("data")
NV35_PATH = DATA / "standings_nv_35.json"


def _last_name(raw: str) -> str:
    """Extract last name from 'Smith, John' or 'John Smith'."""
    raw = raw.strip().lower()
    if "," in raw:
        return raw.split(",")[0].strip()
    parts = raw.split()
    return parts[-1] if parts else raw


def _name_set(raw: str) -> set[str]:
    """Get set of last names from a players string like 'Smith, John / Doe, Jane'."""
    if not raw or raw.strip().upper() in ("N/A", "NA", ""):
        return set()
    names = set()
    for part in raw.split("/"):
        part = part.strip()
        if part and part.upper() not in ("N/A", "NA"):
            names.add(_last_name(part))
    return names


def _clean_names(raw: str) -> list[str]:
    if not raw:
        return []
    s = raw.strip().upper()
    if s in ("N/A", "NA", ""):
        return []
    return [n.strip() for n in raw.split("/") if n.strip() and n.strip().upper() not in ("N/A", "NA")]


def _detect_flip(scraped_courts: list[dict], stored_lines: list[dict]) -> bool | None:
    """
    Compares player names from the t=7 page against stored players to determine
    whether the page's 'home' corresponds to our JSON's 'home' or 'away'.

    Returns True if flipped, False if normal, None if inconclusive.
    """
    label_map: dict[str, dict] = {}
    for sl in stored_lines:
        key = re.sub(r"\s+", "", sl.get("line", "")).lower()
        label_map[key] = sl

    normal = 0
    flipped = 0

    for sc in scraped_courts:
        key = re.sub(r"\s+", "", sc.get("line", "")).lower()
        stored = label_map.get(key)
        if not stored:
            continue

        sc_home = _name_set(sc.get("players_home", ""))
        sc_away = _name_set(sc.get("players_away", ""))
        st_home = _name_set(stored.get("players_home", ""))
        st_away = _name_set(stored.get("players_away", ""))

        if not sc_home and not sc_away:
            continue

        # Overlap score: how many names match in the expected direction
        overlap_normal = len(sc_home & st_home) + len(sc_away & st_away)
        overlap_flipped = len(sc_home & st_away) + len(sc_away & st_home)

        normal += overlap_normal
        flipped += overlap_flipped

    if normal > flipped:
        return False
    elif flipped > normal:
        return True
    else:
        return None  # inconclusive — skip this match


def rescrape(dry_run: bool = False) -> int:
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("  [error] TENNISLINK_USER / TENNISLINK_PASS not set in .env")
        return 0

    standings = json.loads(NV35_PATH.read_text())

    all_matches: list[tuple[dict, dict]] = []
    for sf in standings.get("subflights", []):
        for m in sf.get("matches", []):
            if m.get("tl_match_id") and not m.get("pending"):
                all_matches.append((sf, m))

    print(f"Found {len(all_matches)} NV 3.5 matches to re-scrape")

    changes = 0
    total_court_changes = 0
    skipped_inconclusive = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()

        try:
            login(page, username, password)
            print()

            for i, (sf, match) in enumerate(all_matches):
                tl_id = match["tl_match_id"]
                date = match.get("date", "?")
                ht = match.get("home_team", "?")
                at = match.get("away_team", "?")
                stored_lines = match.get("lines", [])

                scraped = _fetch_match_details(page, tl_id)
                if not scraped:
                    print(f"  [{i+1}/{len(all_matches)}] {date} {ht} vs {at}: SKIP (no data)")
                    continue

                # Detect if t=7 page home/away is flipped relative to our JSON
                flipped = _detect_flip(scraped, stored_lines)
                if flipped is None:
                    print(f"  [{i+1}/{len(all_matches)}] {date} {ht} vs {at}: SKIP (inconclusive orientation)")
                    skipped_inconclusive += 1
                    continue

                if flipped:
                    # Invert all result values and swap home/away player fields
                    for sc in scraped:
                        sc["result"] = "away" if sc.get("result") == "home" else (
                            "home" if sc.get("result") == "away" else sc.get("result", "")
                        )
                        sc["players_home"], sc["players_away"] = sc.get("players_away", ""), sc.get("players_home", "")

                def _norm(s): return re.sub(r"\s+", "", s).lower()
                scraped_by_line = {_norm(ln["line"]): ln for ln in scraped}

                match_changed = False
                court_changes = []

                for stored_ln in stored_lines:
                    label_key = _norm(stored_ln.get("line", ""))
                    scraped_ln = scraped_by_line.get(label_key)
                    if not scraped_ln:
                        continue

                    new_result = scraped_ln.get("result", "")
                    old_cw = stored_ln.get("court_winner")

                    if new_result and new_result != old_cw:
                        court_changes.append(
                            f"    {stored_ln['line']}: {old_cw} → {new_result}"
                        )
                        if not dry_run:
                            stored_ln["court_winner"] = new_result
                            ph = _clean_names(stored_ln.get("players_home", ""))
                            pa = _clean_names(stored_ln.get("players_away", ""))
                            if new_result == "home":
                                stored_ln["winner_names"] = ph
                                stored_ln["loser_names"] = pa
                                stored_ln["winner_team"] = ht
                                stored_ln["loser_team"] = at
                            else:
                                stored_ln["winner_names"] = pa
                                stored_ln["loser_names"] = ph
                                stored_ln["winner_team"] = at
                                stored_ln["loser_team"] = ht
                        match_changed = True

                if court_changes:
                    changes += 1
                    total_court_changes += len(court_changes)
                    flip_label = " [FLIPPED]" if flipped else ""
                    print(f"  [{i+1}/{len(all_matches)}] {date} {ht} vs {at}: {len(court_changes)} court(s) changed{flip_label}")
                    for c in court_changes:
                        print(c)

                    if not dry_run:
                        lines = match.get("lines", [])
                        new_h = sum(1 for ln in lines if ln.get("court_winner") == "home")
                        new_a = sum(1 for ln in lines if ln.get("court_winner") == "away")
                        match["team_wins_home"] = new_h
                        match["team_wins_away"] = new_a
                else:
                    if i % 10 == 0:
                        print(f"  [{i+1}/{len(all_matches)}] {date} {ht} vs {at}: OK")

                sleep(DELAY * 0.3)

        finally:
            browser.close()

    print(f"\n{'DRY RUN — ' if dry_run else ''}Results: {changes} match(es) changed, "
          f"{total_court_changes} court(s) updated, {skipped_inconclusive} inconclusive skipped")

    if not dry_run and changes > 0:
        NV35_PATH.write_text(json.dumps(standings, indent=2))
        print(f"Saved {NV35_PATH}")

    return changes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    n = rescrape(dry_run=args.dry_run)

    if not args.dry_run and n > 0:
        print("\nRunning normalize + rebuild...")
        import subprocess
        subprocess.run([sys.executable, "rebuild.py", "--state", "NV"], check=True)
        print("\nDone. Review VALIDATION warnings before committing.")
