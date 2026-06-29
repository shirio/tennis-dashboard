#!/usr/bin/env python3
"""
Scrape all UT 3.0 Women championship/playoff rounds from TennisLink.

Championship structure:
  Regular season subflights → Flight playoffs (A, B, C, D, E on 6/6) → Final 3.0W (6/19)

Strategy: navigate to known teams with advancement links, click each link
to reach the championship page, scrape standings + match details.
"""
from __future__ import annotations
import json, os, re, sys
from pathlib import Path
from time import sleep as _sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright, Page
from scrapers.scrape_tennislink import (
    login, _wait_for_network, _navigate_via_league_search,
    _go_to_flight_page, _scrape_championship_page,
    _parse_match_detail_page, _champ_entry_label,
    BASE_URL, DELAY,
)

NTRP = "3.0"
YEAR = 2026
DATA_DIR = Path("data")
STANDINGS_PATH = DATA_DIR / "standings_ut_30.json"
SECTIONALS_PATH = DATA_DIR / "sectionals_qualified.json"
SCORECARD_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx"


def sleep(secs: float = DELAY):
    _sleep(secs)


def _click_postback(page: Page, href: str):
    m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
    if m:
        page.evaluate(f"__doPostBack('{m.group(1)}', '{m.group(2)}')")
        _wait_for_network(page, 15_000)
        sleep(2)


def _navigate_to_team(page: Page, flight_suffix: str, subflight_name: str,
                      team_substr: str) -> bool:
    """Navigate to a specific team page: league search -> flight -> subflight -> team."""
    area = f"UT-{flight_suffix}"
    ok = _navigate_via_league_search(page, "Intermountain", "Utah", area, NTRP, YEAR)
    if not ok:
        return False
    if not _go_to_flight_page(page):
        return False

    sf_links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('rptSubFlightsForFlightSummary') && href.includes('__doPostBack');
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for sl in sf_links:
        if sl["text"] == subflight_name:
            _click_postback(page, sl["href"])
            break
    else:
        print(f"  [warn] subflight {subflight_name!r} not found")
        return False

    team_links = page.evaluate("""() => {
        const tbl = document.getElementById('TeamSummary');
        const container = tbl || document;
        return Array.from(container.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('doPostBack') && a.innerText.trim().length > 3;
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for tl in team_links:
        if team_substr.lower() in tl["text"].lower():
            _click_postback(page, tl["href"])
            return True

    print(f"  [warn] team {team_substr!r} not found")
    return False


def _find_advancement_link(page: Page, link_text: str) -> str | None:
    links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('rptChampAdvancementForTeamSummary') && href.includes('doPostBack');
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for lnk in links:
        if lnk["text"] == link_text:
            return lnk["href"]
    return None


def _scrape_champ_level(page: Page, flight_suffix: str, subflight_name: str,
                         team_substr: str, adv_link_text: str) -> dict | None:
    print(f"\n{'='*60}")
    print(f"Scraping: {adv_link_text} (via {team_substr} in {subflight_name})")
    print(f"{'='*60}")

    if not _navigate_to_team(page, flight_suffix, subflight_name, team_substr):
        return None

    href = _find_advancement_link(page, adv_link_text)
    if not href:
        print(f"  [warn] advancement link {adv_link_text!r} not found on team page")
        return None

    _click_postback(page, href)

    result = _scrape_championship_page(page)
    if not result:
        print(f"  [warn] no data from championship page")
        return None

    teams, matches = result
    label = _champ_entry_label(adv_link_text)
    print(f"  Got {len(teams)} teams, {len(matches)} matches -> {label!r}")

    matches_needing_lines = [m for m in matches if not m.get("lines") and m.get("tl_match_id")]
    if matches_needing_lines:
        print(f"  Scraping {len(matches_needing_lines)} scorecards...")
        for i, match in enumerate(matches_needing_lines):
            tl_id = match["tl_match_id"]
            url = f"{SCORECARD_URL}?t=12&par1={tl_id}&par2=0&par3=0"
            print(f"    [{i+1}/{len(matches_needing_lines)}] Match {tl_id}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                _wait_for_network(page, 15_000)
                sleep(2)
                lines = _parse_match_detail_page(page)
                if lines:
                    match["lines"] = lines
                    print(f"      {len(lines)} lines")
            except Exception as e:
                print(f"      Error: {e}")
            sleep(1)

    return {
        "flight_label": label,
        "teams": teams,
        "matches": sorted(matches, key=lambda m: m.get("date", "")),
    }


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    # Championship levels: (flight_suffix, subflight, team_substr, adv_link_text)
    champ_levels = [
        ("AM", "3.0W AM Teal",  "PC MARC",              "Flight E"),
        ("AM", "3.0W AM Green", "Utah/Nebeker",          "Flight A"),
        ("PM", "3.0W PM Indigo","Liberty Park-Kesler",   "Flight B"),
        ("PM", "3.0W PM Indigo","Wasatch Hills",         "Flight C"),
        ("AM", "3.0W AM Gold",  "Ivory Ridge-Supreme",   "Flight D"),
        # Final round
        ("AM", "3.0W AM Green", "Utah/Nebeker",          "3.0W"),
    ]

    existing = json.loads(STANDINGS_PATH.read_text()) if STANDINGS_PATH.exists() else {
        "ntrp": NTRP, "year": YEAR, "state": "UT", "subflights": []
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            print("Logging in...")
            login(page, username, password)
            print("OK\n")

            scraped = []
            seen_labels = set()

            for flight_suffix, sf_name, team_substr, adv_text in champ_levels:
                label = _champ_entry_label(adv_text)
                if label in seen_labels:
                    continue

                sf = _scrape_champ_level(page, flight_suffix, sf_name, team_substr, adv_text)
                if sf:
                    scraped.append(sf)
                    seen_labels.add(sf["flight_label"])

            if not scraped:
                print("\nNo championship data scraped.")
                return

            # Remove old championship subflights, add new
            existing["subflights"] = [
                sf for sf in existing.get("subflights", [])
                if not sf.get("flight_label", "").startswith("Championships")
            ]
            for sf in scraped:
                existing["subflights"].append(sf)

            STANDINGS_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))

            print(f"\n{'='*60}")
            print(f"SAVED {STANDINGS_PATH}")
            for sf in scraped:
                n_lines = sum(1 for m in sf["matches"] if m.get("lines"))
                print(f"  {sf['flight_label']}: {len(sf['teams'])} teams, "
                      f"{len(sf['matches'])} matches ({n_lines} with lines)")

            # Update sectionals winner from final 3.0W round
            final = next((sf for sf in scraped if "3.0w" in sf["flight_label"].lower()), None)
            if final:
                teams_sorted = sorted(
                    final["teams"],
                    key=lambda t: (-t.get("team_wins", 0), t.get("team_losses", 99)),
                )
                if teams_sorted and teams_sorted[0].get("team_wins", 0) > 0:
                    winner = teams_sorted[0]["team_name"]
                    print(f"\nDistricts winner: {winner}")

                    sect = json.loads(SECTIONALS_PATH.read_text()) if SECTIONALS_PATH.exists() else {
                        "year": YEAR, "ntrp": NTRP, "gender": "Female",
                        "level": "Sectionals", "section": "Intermountain",
                        "qualified_teams": [],
                    }
                    for qt in sect.get("qualified_teams", []):
                        if qt.get("state") == "UT":
                            old = qt.get("team")
                            qt["team"] = winner
                            qt["source"] = "districts_winner"
                            if old != winner:
                                print(f"  Updated: {old!r} -> {winner!r}")
                            break
                    else:
                        sect.setdefault("qualified_teams", []).append({
                            "state": "UT", "team": winner, "source": "districts_winner",
                        })

                    SECTIONALS_PATH.write_text(json.dumps(sect, indent=2, ensure_ascii=False) + "\n")
                    print(f"  Saved {SECTIONALS_PATH}")

        except Exception as e:
            import traceback
            traceback.print_exc()
            sys.exit(1)
        finally:
            ctx.close()
            browser.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
