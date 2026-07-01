#!/usr/bin/env python3
"""
Scrape all CO 3.0 Women championship/playoff rounds from TennisLink, using
the team-page model (not the championship search form, which was flaky and
missed rounds — see scrape_ut_championships.py for the same approach).

Bracket structure discovered from a live TennisLink team page (not
previously captured): Flight Playoff (per Denver-Metro sub-area, e.g.
"DEN 3", "DEN 4", ...) -> Flight A/B/C -> Final Rounds. Our existing data
only has Flight A/B/C/Final Rounds; the earlier "Flight Playoff" round is
missing entirely.

Strategy: for each of the 12 teams we know reached Flight A/B/C (already
scraped), navigate to THEIR OWN regular-season team page and scan ALL
championship advancement links present there — a playoff-qualifying team's
page lists every round it advanced through, so we don't need to crawl every
regular-season team, just re-visit the ones we know made it.
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
    _parse_match_detail_page, _get_state_config,
    BASE_URL, DELAY,
)

NTRP = "3.0"
YEAR = 2026
DATA_DIR = Path("data")
STANDINGS_PATH = DATA_DIR / "standings_co_30.json"
SCORECARD_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx"


def sleep(secs: float = DELAY):
    _sleep(secs)


def _click_postback(page: Page, href: str):
    m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
    if m:
        page.evaluate(f"__doPostBack('{m.group(1)}', '{m.group(2)}')")
        _wait_for_network(page, 15_000)
        sleep(2)


def _area_for_subflight(sf_label: str) -> str:
    sl = sf_label.upper()
    if "MOUNTAINS" in sl:
        return "CO-MOUNTAINS"
    if "NORTHERN" in sl or "NOCO" in sl:
        return "CO-NORTHERN COLORADO"
    if "SOUTHERN" in sl or "SOCO" in sl:
        return "CO-SOUTHERN COLORADO"
    if "WESTERN" in sl or "WS " in sl:
        return "CO-WESTERN SLOPE"
    return "CO-DENVER METRO"


def _navigate_to_team(page: Page, area: str, subflight_name: str, team_name: str) -> bool:
    """Navigate to a specific team page: league search -> flight -> subflight -> team."""
    ok = _navigate_via_league_search(page, "Intermountain", "Colorado", area, NTRP, YEAR)
    if not ok:
        return False
    if not _go_to_flight_page(page):
        return False

    sf_links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('doPostBack');
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for sl in sf_links:
        # Match exact, or "SUBFLIGHT - SUFFIX" (e.g. "FLIGHT I - SOCO" for
        # subflight_name="FLIGHT I") — link text format varies by area.
        if sl["text"] == subflight_name or sl["text"].upper().startswith(subflight_name.upper() + " -"):
            _click_postback(page, sl["href"])
            break
    else:
        # Areas with only ONE subflight (e.g. CO-NORTHERN COLORADO "A",
        # CO-SOUTHERN COLORADO "FLIGHT I") don't show a subflight selector
        # link at all — the flight page IS the (only) subflight's team
        # listing already. Fall back to checking for the target team
        # directly on the current page before giving up.
        has_team_here = page.evaluate(f"""() => {{
            const tbl = document.getElementById('TeamSummary');
            const container = tbl || document;
            return Array.from(container.querySelectorAll('a')).some(a =>
                a.innerText.trim().toUpperCase() === {team_name.strip().upper()!r});
        }}""")
        if not has_team_here:
            print(f"    [warn] subflight {subflight_name!r} not found on flight page")
            return False
        print(f"    (single-subflight area — team found directly on flight page)")

    team_links = page.evaluate("""() => {
        const tbl = document.getElementById('TeamSummary');
        const container = tbl || document;
        return Array.from(container.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('doPostBack') && a.innerText.trim().length > 3;
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for tl in team_links:
        if tl["text"].strip().upper() == team_name.strip().upper():
            _click_postback(page, tl["href"])
            return True

    print(f"    [warn] team {team_name!r} not found. Available: "
          f"{[t['text'] for t in team_links]}")
    return False


def _find_advancement_links(page: Page) -> list[dict]:
    """Return ALL championship advancement links on the current team page.
    Filters out empty/whitespace-only link text and pure-NTRP labels like
    'W 3.0' (the level's own header link, not an actual round to follow)."""
    links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('rptChampAdvancementForTeamSummary') && href.includes('doPostBack');
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    out = []
    for lnk in links:
        text = lnk["text"].strip()
        if not text:
            continue
        if re.fullmatch(r'[WM]\s*\d\.\d', text, re.IGNORECASE):
            continue
        out.append(lnk)
    return out


def _raw_label_to_flight_label(raw_text: str) -> str:
    """Derive a stable subflight label from the raw advancement link text.
    Handles 'Flight A', 'Flight Playoff / W 3.0 - DEN 3', 'Final Rounds', etc.
    """
    t = raw_text.strip()
    # "Flight Playoff / W 3.0 - DEN 3" -> "Flight Playoff DEN 3"
    # "Flight Playoff / W 3.0 - SOCO 1" -> "Flight Playoff SOCO 1"
    # Generalized over any area code (DEN, SOCO, NOCO, WS, MOUNTAINS, ...).
    m = re.search(r'flight\s+playoff.*?-\s*([A-Za-z]+\s*\d+)\s*$', t, re.IGNORECASE)
    if m:
        return f"Championships Flight Playoff {m.group(1).upper()}"
    m = re.search(r'\bflight\s+([A-Za-z])\b', t, re.IGNORECASE)
    if m:
        return f"Championships Flight {m.group(1).upper()}"
    if "final" in t.lower():
        return "Championships Final Rounds"
    return f"Championships {t}"


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    existing = json.loads(STANDINGS_PATH.read_text())

    # Find the 12 teams already known to have reached Flight A/B/C, and their
    # home (regular-season) subflight — we revisit THEIR OWN team page to
    # discover every advancement link present (including any earlier round
    # like "Flight Playoff" that our prior scrape never captured).
    champ_team_names = set()
    for sf in existing["subflights"]:
        if sf.get("flight_label", "").startswith("Championships"):
            for t in sf.get("teams", []):
                if t.get("team_name"):
                    champ_team_names.add(t["team_name"])

    team_homes = []  # (area, subflight_name, team_name)
    for sf in existing["subflights"]:
        lbl = sf.get("flight_label", "")
        if lbl.startswith("Championships"):
            continue
        area = _area_for_subflight(lbl)
        # Subflight NAME as it appears on the flight page link (strip area prefix)
        sf_name = lbl
        for pfx in (area + " ",):
            if sf_name.startswith(pfx):
                sf_name = sf_name[len(pfx):]
        for t in sf.get("teams", []):
            tn = t.get("team_name", "")
            if tn in champ_team_names:
                team_homes.append((area, sf_name, tn))

    print(f"Found {len(team_homes)} championship-qualified teams to re-check:")
    for area, sf_name, tn in team_homes:
        print(f"  {tn}  ({area} / {sf_name})")

    discovered: dict[str, dict] = {}  # flight_label -> {teams, matches}
    seen_hrefs: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            print("\nLogging in...")
            login(page, username, password)
            print("OK\n")

            for area, sf_name, team_name in team_homes:
                print(f"\n{'='*60}\n{team_name}  ({area} / {sf_name})\n{'='*60}")
                if not _navigate_to_team(page, area, sf_name, team_name):
                    continue

                links = _find_advancement_links(page)
                print(f"  Advancement links found: {[l['text'] for l in links]}")

                for lnk in links:
                    flight_label = _raw_label_to_flight_label(lnk["text"])
                    if flight_label in discovered:
                        print(f"    (skip — already scraped {flight_label!r})")
                        continue

                    # Need to re-navigate since clicking a link moves us off this page
                    if not _navigate_to_team(page, area, sf_name, team_name):
                        continue
                    fresh_links = _find_advancement_links(page)
                    match = next((l for l in fresh_links if l["text"] == lnk["text"]), None)
                    if not match:
                        print(f"    [warn] link {lnk['text']!r} disappeared on re-nav")
                        continue

                    _click_postback(page, match["href"])
                    result = _scrape_championship_page(page)
                    if not result:
                        print(f"    [warn] no data scraped for {lnk['text']!r}")
                        continue
                    teams, matches = result
                    print(f"    Scraped {flight_label!r}: {len(teams)} teams, {len(matches)} matches")

                    matches_needing_lines = [m for m in matches if not m.get("lines") and m.get("tl_match_id")]
                    if matches_needing_lines:
                        print(f"    Scraping {len(matches_needing_lines)} scorecards...")
                        for m in matches_needing_lines:
                            tl_id = m["tl_match_id"]
                            url = f"{SCORECARD_URL}?t=12&par1={tl_id}&par2=0&par3=0"
                            try:
                                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                                _wait_for_network(page, 15_000)
                                sleep(1.5)
                                lines = _parse_match_detail_page(page)
                                if lines:
                                    m["lines"] = lines
                            except Exception as e:
                                print(f"      Error scraping scorecard {tl_id}: {e}")
                            sleep(0.8)

                    discovered[flight_label] = {
                        "flight_label": flight_label,
                        "teams": teams,
                        "matches": sorted(matches, key=lambda mm: mm.get("date", "")),
                    }

        finally:
            ctx.close()
            browser.close()

    if not discovered:
        print("\nNo championship data discovered.")
        return

    print(f"\n{'='*60}")
    print("DISCOVERED ROUNDS:")
    for label, sf in discovered.items():
        print(f"  {label}: {len(sf['teams'])} teams, {len(sf['matches'])} matches")

    # Merge: replace any existing subflight with the same label, add new ones
    existing["subflights"] = [
        sf for sf in existing["subflights"]
        if sf.get("flight_label") not in discovered
    ]
    for sf in discovered.values():
        existing["subflights"].append(sf)

    STANDINGS_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"\nSaved {STANDINGS_PATH}")


if __name__ == "__main__":
    main()
