#!/usr/bin/env python3
"""
Discover ALL UT 3.0 championship levels by navigating to teams from
both AM and PM flights and reading Championship Advancement links.
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
    _go_to_flight_page, BASE_URL, DELAY,
)

NTRP = "3.0"
YEAR = 2026


def sleep(secs: float = DELAY):
    _sleep(secs)


def _click_postback(page: Page, href: str):
    m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
    if m:
        page.evaluate(f"__doPostBack('{m.group(1)}', '{m.group(2)}')")
        _wait_for_network(page, 15_000)
        sleep(2)


def _get_advancement_links(page: Page) -> list[dict]:
    return page.evaluate("""() => {
        const results = [];
        for (const a of document.querySelectorAll('a')) {
            const href = a.getAttribute('href') || '';
            if (href.includes('rptChampAdvancementForTeamSummary') && href.includes('doPostBack')) {
                let text = a.innerText.trim();
                const tr = a.closest('tr');
                let rowText = tr ? tr.innerText.trim() : '';
                results.push({text, href, rowText: rowText.substring(0, 200), id: a.id || ''});
            }
        }
        return results;
    }""")


def _navigate_to_team_in_subflight(page: Page, flight_suffix: str, subflight_name: str,
                                     team_substr: str) -> bool:
    """Navigate: league search → team page → flight page → subflight → specific team."""
    area = f"UT-{flight_suffix}"
    ok = _navigate_via_league_search(page, "Intermountain", "Utah", area, NTRP, YEAR)
    if not ok:
        print(f"  Failed to navigate via league search (area={area})")
        return False

    # Now on a team page in the right flight. Go to flight page.
    if not _go_to_flight_page(page):
        print(f"  Failed to go to flight page")
        return False

    # Click subflight
    sf_links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('rptSubFlightsForFlightSummary') && href.includes('__doPostBack');
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")

    clicked = False
    for sl in sf_links:
        if sl["text"] == subflight_name:
            _click_postback(page, sl["href"])
            clicked = True
            break
    if not clicked:
        print(f"  Could not find subflight {subflight_name!r} in {[s['text'] for s in sf_links]}")
        return False

    # Click team
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
            print(f"  Clicking team: {tl['text']}")
            _click_postback(page, tl["href"])
            return True

    print(f"  Could not find team matching '{team_substr}' in {[t['text'] for t in team_links[:10]]}")
    return False


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")

    # Check teams from each subflight (flight_suffix, subflight_name, team_substr)
    teams_to_check = [
        # AM teams
        ("AM", "3.0W AM Teal", "PC MARC"),
        ("AM", "3.0W AM Green", "Utah/Nebeker"),       # districts winner
        ("AM", "3.0W AM Gold", "Ivory Ridge-Supreme"),  # top Gold team
        # PM teams
        ("PM", "3.0W PM Pink", "Harvey"),
        ("PM", "3.0W PM Indigo", "Liberty Park-Kesler"),
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            login(page, username, password)

            all_adv = {}

            for flight_suffix, sf_name, team_substr in teams_to_check:
                print(f"\n{'='*60}")
                print(f"Checking {team_substr} in {sf_name}")
                print(f"{'='*60}")

                ok = _navigate_to_team_in_subflight(page, flight_suffix, sf_name, team_substr)
                if not ok:
                    continue

                links = _get_advancement_links(page)
                if links:
                    print(f"  Advancement links:")
                    for lnk in links:
                        label = lnk["text"] or "(empty)"
                        print(f"    text={label!r}  rowText={lnk['rowText']!r}")
                        if lnk["text"] and lnk["text"] not in all_adv:
                            all_adv[lnk["text"]] = {"href": lnk["href"], "team": team_substr}
                        elif not lnk["text"]:
                            # Empty text link — might be the "3.0W" final round
                            # Try to get text from row context
                            row = lnk["rowText"]
                            if row and row not in all_adv:
                                all_adv[f"(from row: {row[:50]})"] = {
                                    "href": lnk["href"], "team": team_substr
                                }
                else:
                    print(f"  No advancement links")
                    # Check if there's a "did not advance" message
                    champ_text = page.evaluate("""() => {
                        const body = document.body.innerText;
                        const idx = body.indexOf('Championship');
                        if (idx >= 0) return body.substring(idx, idx + 200);
                        return '';
                    }""")
                    if champ_text:
                        print(f"  Championship section: {champ_text[:150]}")

            print(f"\n{'='*60}")
            print(f"ALL DISCOVERED CHAMPIONSHIP LEVELS:")
            for text, info in sorted(all_adv.items()):
                print(f"  '{text}' (from {info['team']})")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
