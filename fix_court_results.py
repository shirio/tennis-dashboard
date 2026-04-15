#!/usr/bin/env python3
"""
fix_court_results.py

Re-fetch per-court winner data for all completed matches that have a
tl_match_id, then recompute player stats and rebuild dashboards.

The per-court winner is determined by the `mark.gif` image on the
StatsAndStandings.aspx?t=7&par1=MATCHID page:
  - img id ends with "imgHomePlayer"    → Home team won that court
  - img id ends with "imgVisitorPlayer" → Visiting team won that court

Usage:
    python3 fix_court_results.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from scrapers.scrape_tennislink import (
    login,
    _compute_player_stats_from_scorecards,
    OUTPUT_STANDINGS_30,
    OUTPUT_STANDINGS_35,
    DELAY,
    sleep,
)
from engine.build_html import build_dashboards

BASE_URL = "https://tennislink.usta.com"
MATCH_VIEW_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?t=7&par1={{mid}}&par2=0&par3=0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"  [saved] {path}")


def _read_court_winners_from_html(html: str) -> list[str]:
    """
    Parse the StatsAndStandings.aspx?t=7 page HTML and return per-court winners.

    Winners are indicated by a 'mark.gif' image whose ID ends with:
      - 'imgHomePlayer'    → home team won
      - 'imgVisitorPlayer' → visitor team won

    The images appear in scorecard repeater items (ctl00, ctl01, ...) in court order.
    Returns list of 'home'/'away'/'' per court.
    """
    soup = BeautifulSoup(html, "html.parser")
    mark_imgs = soup.find_all(
        "img",
        src=re.compile(r"mark\.gif", re.I),
        id=re.compile(r"rptScoreCard", re.I),
    )

    if not mark_imgs:
        return []

    # Sort by repeater index (ctl00, ctl01, ...) to get courts in order
    def _court_idx(img):
        m = re.search(r"ctl(\d+)_img", img.get("id", ""), re.I)
        return int(m.group(1)) if m else 999

    mark_imgs_sorted = sorted(mark_imgs, key=_court_idx)

    courts: list[str] = []
    for img in mark_imgs_sorted:
        img_id = (img.get("id", "") or "").lower()
        if "homeplayer" in img_id:
            courts.append("home")
        elif "visitorplayer" in img_id:
            courts.append("away")
        else:
            courts.append("")

    return courts


def _page_has_scorecard(html: str) -> bool:
    """Return True if the page has actual scorecard content (not empty)."""
    return bool(re.search(r"mark\.gif", html, re.I)) or bool(
        re.search(r"rptScoreCard.*imgHomePlayer|rptScoreCard.*imgVisitorPlayer", html, re.I)
    )


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def fetch_and_apply_results(page, standings_data: dict) -> int:
    """
    For every completed match with a tl_match_id, fetch the match scorecard view
    from StatsAndStandings.aspx?t=7 and update per-court result fields.
    Returns number of matches updated.
    """
    n_updated = 0

    for sf in standings_data.get("subflights", []):
        for match in sf.get("matches", []):
            if match.get("pending"):
                continue
            tl_mid = match.get("tl_match_id")
            if not tl_mid:
                continue
            lines = match.get("lines", [])
            if not lines:
                continue

            home = match.get("home_team", "?")
            away = match.get("away_team", "?")
            date = match.get("date", "?")
            expected_score = match.get("score", "?")

            url = MATCH_VIEW_URL.format(mid=tl_mid)
            print(f"  {date} {home} vs {away} (ID={tl_mid})")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                sleep(1.0)
            except Exception as e:
                print(f"    [warn] load failed: {e}")
                continue

            html = page.content()

            if not _page_has_scorecard(html):
                print(f"    [warn] no scorecard data found on page – skipping")
                continue

            winners = _read_court_winners_from_html(html)
            if not winners:
                print(f"    [warn] no mark.gif images found on page")
                continue

            # Check if home/away orientation is flipped vs our data.
            # TennisLink's t=7 page uses TL's official home/away assignment,
            # but some matches in our data have home/away swapped.
            # Use team_wins_home / team_wins_away as ground truth.
            court_home_wins = winners.count("home")
            court_away_wins = winners.count("away")
            team_h = match.get("team_wins_home")
            team_v = match.get("team_wins_away")
            flipped = False
            if team_h is not None and team_v is not None:
                if court_home_wins == team_v and court_away_wins == team_h and team_h != team_v:
                    # Orientation is reversed – flip all results
                    winners = [
                        "away" if w == "home" else ("home" if w == "away" else "")
                        for w in winners
                    ]
                    flipped = True
                    print(f"    [flip] orientation reversed (TL: {court_home_wins}H-{court_away_wins}V, our data: {team_h}H-{team_v}V) – flipping")

            # Validate winner count vs match score
            home_wins = winners.count("home")
            away_wins = winners.count("away")
            print(f"    → winners: {winners}  ({home_wins}H-{away_wins}V, expected {expected_score})")

            if len(winners) != len(lines):
                print(f"    [warn] winners({len(winners)}) ≠ lines({len(lines)}) – applying partial")

            # Apply winners
            n = min(len(winners), len(lines))
            changed = 0
            for i in range(n):
                if winners[i] and lines[i].get("result") != winners[i]:
                    lines[i]["result"] = winners[i]
                    changed += 1

            if changed:
                n_updated += 1
                print(f"    → updated {changed} court result(s)")
            else:
                print(f"    → all results already correct")

            sleep(DELAY * 0.3)

    return n_updated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    print("\n=== fix_court_results.py ===")
    standings_30 = load_json(OUTPUT_STANDINGS_30, {})
    standings_35 = load_json(OUTPUT_STANDINGS_35, {})

    n_sc_30 = sum(
        1 for sf in standings_30.get("subflights", [])
        for m in sf.get("matches", [])
        if m.get("tl_match_id") and not m.get("pending") and m.get("lines")
    )
    n_sc_35 = sum(
        1 for sf in standings_35.get("subflights", [])
        for m in sf.get("matches", [])
        if m.get("tl_match_id") and not m.get("pending") and m.get("lines")
    )
    print(f"Matches with TL IDs: 3.0={n_sc_30}, 3.5={n_sc_35}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            print("\nLogging into TennisLink...")
            login(page, username, password)
            print("Login successful!")

            # Quick sanity check
            test_url = MATCH_VIEW_URL.format(mid="1011779853")
            page.goto(test_url, wait_until="domcontentloaded", timeout=25_000)
            sleep(1.5)
            test_html = page.content()
            test_winners = _read_court_winners_from_html(test_html)
            print(f"Sanity check (DRAGONRIDGE vs TPC 3/21): {test_winners}")
            # Expected: home, home, away, home, away (DRAGONRIDGE won 3, TPC won 2)

            print("\n=== Updating 3.5 Women court results ===")
            n35 = fetch_and_apply_results(page, standings_35)
            print(f"Updated {n35} matches in 3.5 Women")
            save_json(OUTPUT_STANDINGS_35, standings_35)

            print("\n=== Updating 3.0 Women court results ===")
            n30 = fetch_and_apply_results(page, standings_30)
            print(f"Updated {n30} matches in 3.0 Women")
            save_json(OUTPUT_STANDINGS_30, standings_30)

        finally:
            context.close()
            browser.close()

    total = n35 + n30
    if total > 0:
        print(f"\n=== Recomputing player stats ({total} matches updated) ===")
        standings_30_r = load_json(OUTPUT_STANDINGS_30, {})
        standings_35_r = load_json(OUTPUT_STANDINGS_35, {})
        _compute_player_stats_from_scorecards([
            ("3.0", standings_30_r.get("subflights", [])),
            ("3.5", standings_35_r.get("subflights", [])),
        ])

        print("\n=== Rebuilding HTML dashboards ===")
        build_dashboards()
        print("\n✓ Done! Court results corrected and dashboards rebuilt.")
    else:
        print("\nNo matches updated.")


if __name__ == "__main__":
    main()
