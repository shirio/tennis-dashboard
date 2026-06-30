#!/usr/bin/env python3
"""
Re-scrape CO court winners from TennisLink t=7 scorecard pages.

CO 3.0 has 249 orientation mismatches; CO 3.5 has 241. Root cause: the
original scraper stored court_winner as "home"/"away" from the page's
perspective without verifying whether that page's home matches our stored
home_team. This script re-fetches every match's t=7 page, detects the
orientation via player name matching (same approach as rescrape_nv35_courts.py),
then corrects winner_team / loser_team / winner_names / loser_names.

Usage:
    python3 scripts/rescrape_co_courts.py              # both 3.0 + 3.5
    python3 scripts/rescrape_co_courts.py --div 30     # just 3.0
    python3 scripts/rescrape_co_courts.py --dry-run    # show diffs, no save
    python3 scripts/rescrape_co_courts.py --headless
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
PATHS = {
    "30": DATA / "standings_co_30.json",
    "35": DATA / "standings_co_35.json",
}


def _last_name(raw: str) -> str:
    raw = raw.strip().lower()
    if "," in raw:
        return raw.split(",")[0].strip()
    parts = raw.split()
    return parts[-1] if parts else raw


def _name_set(raw: str) -> set[str]:
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
    """Compare scraped player names against stored to detect home/away orientation flip.
    Returns True if flipped, False if normal, None if inconclusive."""
    label_map: dict[str, dict] = {}
    for sl in stored_lines:
        key = re.sub(r"\s+", "", sl.get("line", "")).lower()
        label_map[key] = sl

    normal = flipped = 0
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
        normal += len(sc_home & st_home) + len(sc_away & st_away)
        flipped += len(sc_home & st_away) + len(sc_away & st_home)

    if normal > flipped:
        return False
    elif flipped > normal:
        return True
    return None


def rescrape_file(page, path: Path, dry_run: bool) -> tuple[int, int, int]:
    """Rescrape a single standings file. Returns (matches_changed, courts_changed, skipped)."""
    standings = json.loads(path.read_text())
    all_matches: list[tuple[dict, dict]] = []
    for sf in standings.get("subflights", []):
        for m in sf.get("matches", []):
            if m.get("tl_match_id") and not m.get("pending"):
                all_matches.append((sf, m))

    print(f"\n  {path.name}: {len(all_matches)} matches to check")
    changes = total_court_changes = skipped = 0

    for i, (sf, match) in enumerate(all_matches):
        tl_id = match["tl_match_id"]
        date = match.get("date", "?")
        ht = match.get("home_team", "?")
        at = match.get("away_team", "?")
        stored_lines = match.get("lines", [])

        scraped = _fetch_match_details(page, tl_id)
        if not scraped:
            if i % 20 == 0:
                print(f"    [{i+1}/{len(all_matches)}] {date} {ht} vs {at}: SKIP (no data)")
            continue

        flipped = _detect_flip(scraped, stored_lines)
        if flipped is None:
            skipped += 1
            continue

        if flipped:
            for sc in scraped:
                sc["result"] = "away" if sc.get("result") == "home" else (
                    "home" if sc.get("result") == "away" else sc.get("result", ""))
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
                court_changes.append(f"    {stored_ln['line']}: {old_cw} → {new_result}")
                if not dry_run:
                    stored_ln["court_winner"] = new_result
                    ph = _clean_names(stored_ln.get("players_home", ""))
                    pa = _clean_names(stored_ln.get("players_away", ""))
                    if new_result == "home":
                        stored_ln["winner_team"] = ht
                        stored_ln["loser_team"] = at
                        stored_ln["winner_names"] = ph
                        stored_ln["loser_names"] = pa
                    else:
                        stored_ln["winner_team"] = at
                        stored_ln["loser_team"] = ht
                        stored_ln["winner_names"] = pa
                        stored_ln["loser_names"] = ph
                match_changed = True

        if court_changes:
            changes += 1
            total_court_changes += len(court_changes)
            flip_label = " [FLIPPED]" if flipped else ""
            print(f"    [{i+1}/{len(all_matches)}] {date} {ht} vs {at}: "
                  f"{len(court_changes)} court(s){flip_label}")
            for c in court_changes:
                print(c)

            if not dry_run:
                lines = match.get("lines", [])
                match["team_wins_home"] = sum(1 for ln in lines if ln.get("court_winner") == "home")
                match["team_wins_away"] = sum(1 for ln in lines if ln.get("court_winner") == "away")
        elif i % 25 == 0:
            print(f"    [{i+1}/{len(all_matches)}] {date} {ht} vs {at}: OK")

        sleep(DELAY * 0.3)

    if not dry_run and changes > 0:
        path.write_text(json.dumps(standings, indent=2))
        print(f"  Saved {path.name} ({changes} matches changed, {total_court_changes} courts)")

    return changes, total_court_changes, skipped


def main():
    parser = argparse.ArgumentParser(description="Re-scrape CO court winners from TL t=7 pages")
    parser.add_argument("--div", choices=["30", "35"], default=None,
                        help="Only process this division (default: both)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("[error] TENNISLINK_USER / TENNISLINK_PASS not set in .env")
        sys.exit(1)

    divs = [args.div] if args.div else ["30", "35"]
    paths = [PATHS[d] for d in divs]

    total_changes = total_courts = total_skipped = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()
        try:
            login(page, username, password)
            for p in paths:
                ch, co, sk = rescrape_file(page, p, args.dry_run)
                total_changes += ch
                total_courts += co
                total_skipped += sk
        finally:
            browser.close()

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Total: {total_changes} match(es) changed, "
          f"{total_courts} court(s) updated, {total_skipped} inconclusive skipped")

    if not args.dry_run and total_changes > 0:
        print("\nRunning normalize + rebuild for CO...")
        import subprocess
        subprocess.run([sys.executable, "rebuild.py", "--state", "CO"], check=True)
        subprocess.run(["git", "add",
                        "data/standings_co_30.json", "data/standings_co_35.json",
                        "women_co_30.html", "women_co_35.html",
                        "matchups_co_30.html", "matchups_co_35.html"])
        subprocess.run(["git", "commit", "-m",
                        f"CO: fix {total_courts} flipped court winners via t=7 rescrape"])
        subprocess.run(["git", "push"])
        print("Done.")


if __name__ == "__main__":
    main()
