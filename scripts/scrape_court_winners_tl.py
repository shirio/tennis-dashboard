#!/usr/bin/env python3
"""
Targeted TennisLink re-scrape to populate per-court winners using mark.gif detection.

Navigates to each subflight → team → match detail page (via date link postback),
and parses the green checkmark (mark.gif) to determine per-court winners.

Only visits teams/matches that have unknown court_winner values.

Usage:
    python3 scripts/scrape_court_winners_tl.py --state UT
    python3 scripts/scrape_court_winners_tl.py --state CO --headless
    python3 scripts/scrape_court_winners_tl.py  # all states
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

DATA_DIR = Path("data")


def _needs_court_winners(match: dict) -> bool:
    lines = match.get("lines", [])
    if not lines:
        return False
    return any(ln.get("court_winner") is None for ln in lines)


def _merge_court_winners(existing_lines: list[dict], scraped_lines: list[dict]):
    """Merge scraped court winner results into existing match lines."""
    for el in existing_lines:
        if el.get("court_winner") is not None:
            continue
        line_label = el.get("line", "")
        for sl in scraped_lines:
            if sl.get("line") == line_label and sl.get("result"):
                el["court_winner"] = sl["result"]
                break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=None, help="State code (UT, CO, ID)")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.headed:
        args.headless = False

    states = [args.state.upper()] if args.state else ["UT", "CO", "ID"]

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    # Load standings and identify work
    work_items = []
    for state in states:
        for div in ["30", "35"]:
            path = DATA_DIR / f"standings_{state.lower()}_{div}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            unknown = sum(
                1 for sf in data.get("subflights", [])
                for m in sf.get("matches", [])
                for ln in m.get("lines", [])
                if ln.get("court_winner") is None
            )
            if unknown > 0:
                work_items.append((state, div, path, data, unknown))
                print(f"{path.name}: {unknown} courts need winners")

    if not work_items:
        print("All courts already have winners!")
        return

    if args.dry_run:
        print("\n[dry-run] Would scrape the above. Exiting.")
        return

    from playwright.sync_api import sync_playwright
    from scrapers.scrape_tennislink import (
        login, _wait_for_network, _parse_match_detail_page,
        _get_state_config, _navigate_via_league_search,
        _click_team_in_standings, _extract_team_matches,
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
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
            login(page, username, password)

            total_applied = 0

            for state, div, path, data, unknown_count in work_items:
                ntrp = f"{div[0]}.{div[1]}"
                print(f"\n=== {state} {ntrp} ({unknown_count} unknown courts) ===")

                state_cfg = _get_state_config(state)
                areas = state_cfg.get("areas", [])
                if not areas:
                    areas = [{"area": ""}]

                for sf in data.get("subflights", []):
                    sf_label = sf.get("flight_label", "?")

                    # Find teams with unknown courts
                    teams_needing = set()
                    matches_needing = {}
                    for m in sf.get("matches", []):
                        if _needs_court_winners(m):
                            teams_needing.add(m.get("home_team", ""))
                            teams_needing.add(m.get("away_team", ""))
                            key = f"{m.get('date')}|{m.get('home_team')}|{m.get('away_team')}"
                            matches_needing[key] = m

                    if not teams_needing:
                        continue

                    print(f"\n  Subflight {sf_label}: {len(matches_needing)} matches need winners across {len(teams_needing)} teams")

                    # Navigate to this league's subflight
                    area_info = areas[0] if areas else {}
                    area_name = area_info.get("area", "")
                    navigated = _navigate_via_league_search(
                        page, state_cfg.get("_section", "Intermountain"),
                        state_cfg["district"], area_name, ntrp, 2026,
                    )
                    if not navigated:
                        print(f"    [error] Could not navigate to {state} {ntrp} {area_name}")
                        continue

                    # Try to reach the right subflight by clicking subflight links
                    # The league search puts us on a team page; try to find subflight nav
                    _try_navigate_subflight(page, sf_label)

                    teams_visited = set()
                    sf_applied = 0

                    for team in sorted(teams_needing):
                        if team in teams_visited:
                            continue
                        teams_visited.add(team)

                        if not _click_team_in_standings(page, team):
                            print(f"    [warn] Could not navigate to team {team!r}")
                            continue

                        team_matches = _extract_team_matches(page, team)
                        print(f"    {team}: {len(team_matches)} matches on schedule")

                        for tm in team_matches:
                            tm_date = tm["date"].strip()
                            tm_team = tm["team"].strip()
                            tm_opp = tm["opponent"].strip()

                            # Find matching standings match
                            match_obj = None
                            for m in sf.get("matches", []):
                                if not _needs_court_winners(m):
                                    continue
                                md = m.get("date", "").strip()
                                mh = m.get("home_team", "").strip()
                                ma = m.get("away_team", "").strip()
                                if md == tm_date and (
                                    (mh == tm_team and ma == tm_opp) or
                                    (mh == tm_opp and ma == tm_team)
                                ):
                                    match_obj = m
                                    break

                            if not match_obj:
                                continue
                            if not tm.get("_date_link_href"):
                                continue

                            # Click into the match detail page
                            href = tm["_date_link_href"]
                            try:
                                pb_m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
                                if not pb_m:
                                    continue
                                et, ea = pb_m.group(1), pb_m.group(2)
                                page.evaluate(f"__doPostBack('{et}', '{ea}')")
                                _wait_for_network(page, 12_000)
                                sleep(1.5)

                                lines = _parse_match_detail_page(page)

                                if lines:
                                    before = sum(1 for ln in match_obj.get("lines", [])
                                                 if ln.get("court_winner") is None)
                                    _merge_court_winners(match_obj.get("lines", []), lines)
                                    after = sum(1 for ln in match_obj.get("lines", [])
                                                if ln.get("court_winner") is None)
                                    applied = before - after
                                    if applied > 0:
                                        sf_applied += applied
                                        total_applied += applied

                                page.go_back(wait_until="domcontentloaded", timeout=15_000)
                                _wait_for_network(page, 10_000)
                                sleep(1)
                            except Exception as e:
                                print(f"      [error] {tm_date} {tm_opp}: {e}")
                                try:
                                    page.go_back(wait_until="domcontentloaded", timeout=10_000)
                                    sleep(1)
                                except Exception:
                                    pass

                    if sf_applied:
                        print(f"  Subflight {sf_label}: applied {sf_applied} court winners")

                # Save updated standings
                path.write_text(json.dumps(data, indent=2))
                remaining = sum(
                    1 for sf in data.get("subflights", [])
                    for m in sf.get("matches", [])
                    for ln in m.get("lines", [])
                    if ln.get("court_winner") is None
                )
                print(f"  Saved {path.name} ({remaining} still unknown)")

            print(f"\n=== Total court winners applied: {total_applied} ===")

        finally:
            context.close()
            browser.close()


def _try_navigate_subflight(page, sf_label: str):
    """Try to navigate to a specific subflight from the current page."""
    from scrapers.scrape_tennislink import _wait_for_network

    # Look for subflight links in the page
    for a in page.query_selector_all("a"):
        try:
            txt = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
            if txt == sf_label and "doPostBack" in href:
                a.click()
                _wait_for_network(page, 12_000)
                sleep(2)
                return True
        except Exception:
            continue
    return False


if __name__ == "__main__":
    main()
