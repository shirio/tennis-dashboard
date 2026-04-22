"""
scrapers/scrape_tennislink.py
Playwright-based scraper for USTA TennisLink.

Usage:
  # Mode 1 – standings + match results for 3.0/3.5 Women A/B subflights
  python3 scrapers/scrape_tennislink.py --mode 1

  # Mode 2 – cross-league match history for every player in data/players.json
  python3 scrapers/scrape_tennislink.py --mode 2

  # Both modes in sequence
  python3 scrapers/scrape_tennislink.py --mode all

Credentials are read from .env (TENNISLINK_USER, TENNISLINK_PASS).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://tennislink.usta.com"
LOGIN_URL = f"{BASE_URL}/Dashboard/Main/Login.aspx"

# TennisLink standings search URL (SearchType=2 = Teams/Flights/Leagues panel)
STANDINGS_SEARCH_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=2"

# Search criteria for NV Area F women's leagues
SEARCH_SECTION  = "Intermountain"
SEARCH_DISTRICT = "Nevada"
SEARCH_AREA     = "Area F"   # also seen as "Area" or "NV Area F"

DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"

OUTPUT_STANDINGS_30 = DATA_DIR / "standings_women_30.json"
OUTPUT_STANDINGS_35 = DATA_DIR / "standings_women_35.json"
OUTPUT_MATCHES_ALL = DATA_DIR / "matches_all_players.json"

DELAY = 1.2   # seconds between page loads

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"  [saved] {path}  ({len(data) if isinstance(data, list) else 'object'})")


def sleep(secs: float = DELAY):
    time.sleep(secs)


def abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return urljoin(BASE_URL, href)


def match_id_from_url(url: str) -> Optional[str]:
    """Extract a stable match identifier from a TennisLink match-detail URL."""
    qs = parse_qs(urlparse(url).query)
    # MatchID is the most reliable key; fall back to combination of params
    if "MatchID" in qs:
        return qs["MatchID"][0]
    # Build a composite key from whatever unique params are present
    keys = sorted(k for k in qs if k.lower() not in ("sessionid",))
    return "&".join(f"{k}={qs[k][0]}" for k in keys) or None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(page: Page, username: str, password: str):
    """
    Log in to TennisLink via Auth0 (account.usta.com).

    Flow:
      1. Navigate to LOGIN_URL  → redirects to account.usta.com/authorize?...
      2. Auth0 shows a universal login form (may have Google/Apple social buttons).
         We must NOT click social buttons.
      3. Fill username → press Enter (submits Auth0 email form, avoids social buttons).
      4. Auth0 may go to a password screen OR land directly (if single-page).
      5. Fill password → press Enter.
      6. Wait for redirect back to tennislink.usta.com.
    """
    print("  [login] navigating to TennisLink login …")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
    sleep(2)

    # We should now be on account.usta.com
    print(f"    on: {page.url}")

    # ── Step 1: fill username/email ──────────────────────────────────────────
    username_sel = None
    for sel in [
        "input[name='username']",
        "input[name='email']",
        "input[type='email']",
        "#username",
        "#email",
    ]:
        try:
            page.wait_for_selector(sel, timeout=3_000)
            page.fill(sel, username, timeout=3_000)
            username_sel = sel
            print(f"    filled username ({sel})")
            break
        except Exception:
            pass

    if not username_sel:
        raise RuntimeError(f"Could not find username field on {page.url}")

    # ── Step 2: advance to password screen ──────────────────────────────────
    # Use Enter to submit the Auth0 email form.
    # This avoids accidentally clicking "Continue with Google" buttons.
    pw_visible = False
    for sel in ["input[type='password']", "#password"]:
        try:
            if page.is_visible(sel, timeout=500):
                pw_visible = True
                break
        except Exception:
            pass

    if not pw_visible:
        page.keyboard.press("Enter")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except PWTimeoutError:
            pass
        sleep(1.5)

    # ── Step 3: fill password ────────────────────────────────────────────────
    pw_sel = None
    for sel in ["input[type='password']", "input[name='password']", "#password"]:
        try:
            page.wait_for_selector(sel, timeout=4_000)
            page.fill(sel, password, timeout=3_000)
            pw_sel = sel
            print(f"    filled password ({sel})")
            break
        except Exception:
            pass

    if not pw_sel:
        # Might be a single-screen form where password appeared after Enter above
        # Try filling again after a short wait
        sleep(1)
        for sel in ["input[type='password']", "input[name='password']"]:
            try:
                page.fill(sel, password, timeout=3_000)
                pw_sel = sel
                print(f"    filled password retry ({sel})")
                break
            except Exception:
                pass

    if not pw_sel:
        raise RuntimeError(f"Could not find password field on {page.url}")

    # ── Step 4: submit ────────────────────────────────────────────────────────
    page.keyboard.press("Enter")

    # Wait for redirect back to tennislink.usta.com
    try:
        page.wait_for_url("**/tennislink.usta.com/**", timeout=25_000)
    except PWTimeoutError:
        pass

    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PWTimeoutError:
        pass
    sleep(2)

    current = page.url
    if (
        "account.usta.com" in current
        or "accounts.google.com" in current
        or ("login" in current.lower() and "tennislink" not in current.lower())
    ):
        raise RuntimeError(
            f"Login failed – still at: {current}\n"
            "Check TENNISLINK_USER / TENNISLINK_PASS in .env\n"
            "Note: if your USTA account uses Google login, set TENNISLINK_USER "
            "to your USTA email (not Google email) and TENNISLINK_PASS to your "
            "USTA password."
        )
    print(f"  [login] success  (at: {current})")


# ---------------------------------------------------------------------------
# MODE 1 helpers (USTA# navigation approach)
# ---------------------------------------------------------------------------

SCORECARD_BASE_URL = f"{BASE_URL}/Leagues/Scorecard/printscorecard.aspx"
USTA_NUMBER = "2019825517"   # Player USTA# used as navigation entry point
USTA_SEARCH_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"

# Navigation link texts to skip when looking for team links on subflight pages
_NAV_SKIP_EXACT = {
    "A", "B", "Summary", "Team Standings", "Match Summary", "Match Schedule",
    "Player Roster", "Player Counts", "Send To Excel", "Print Report",
    "Link to this Page", "Send to Excel", "> Stats & Standings",
}
# Substrings that indicate breadcrumb/nav links (not team names)
_NAV_SKIP_KEYWORDS = ("USTA ADULT", "LEAGUE 18", "& OVER", "WOMEN-", "MEN-")


def _is_nav_link(txt: str) -> bool:
    """Return True if txt looks like a navigation breadcrumb rather than a team name."""
    if txt in _NAV_SKIP_EXACT:
        return True
    upper = txt.upper()
    return any(kw in upper for kw in _NAV_SKIP_KEYWORDS)


def _wait_for_network(page: Page, timeout: int = 10_000):
    """Wait for ASP.NET postback/AJAX to settle."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeoutError:
        pass
    sleep(0.6)


def _navigate_to_my_team(page: Page, ntrp: str, year: int) -> bool:
    """Search by USTA# and click the matching Women's Adult team for given ntrp/year."""
    page.goto(USTA_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    sleep(DELAY)
    page.fill("#ctl00_mainContent_txtUSTANum", USTA_NUMBER)
    page.click("#ctl00_mainContent_btnSearchStatsAndStandings")
    _wait_for_network(page, 12_000)
    sleep(1)

    for tbl in page.query_selector_all("table.CommonTable.Segmented"):
        for tr in tbl.query_selector_all("tbody tr"):
            tds = tr.query_selector_all("td")
            if len(tds) < 5:
                continue
            team_a = tds[0].query_selector("a")
            if not team_a:
                continue
            league = tds[3].inner_text().strip()
            flight = tds[4].inner_text().strip()
            if (str(year) in league and ntrp in flight
                    and "WOMEN" in flight.upper() and "ADULT" in league.upper()):
                print(f"    Clicking team: {team_a.inner_text().strip()} | {flight}")
                team_a.click()
                _wait_for_network(page, 12_000)
                sleep(2)
                return True
    return False


def _extract_standings_from_page(page: Page) -> list[dict]:
    """Extract team standings from #TeamSummary (13-column table)."""
    standings = []
    tbl = page.query_selector("#TeamSummary")
    if not tbl:
        return standings
    for tr in tbl.query_selector_all("tr"):
        cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
        if len(cells) < 6 or not cells[0] or not re.match(r'\d+', cells[1]):
            continue
        standings.append({
            "team_name":      cells[0],
            "team_wins":      _safe_int(cells[1]),
            "matches_played": _safe_int(cells[2]),
            "team_losses":    _safe_int(cells[5]),
            "indiv_wins":     _safe_int(cells[6]) if len(cells) > 6 else None,
            "indiv_losses":   _safe_int(cells[7]) if len(cells) > 7 else None,
            "sets_won":       _safe_int(cells[8]) if len(cells) > 8 else None,
            "sets_lost":      _safe_int(cells[9]) if len(cells) > 9 else None,
            "games_won":      _safe_int(cells[10]) if len(cells) > 10 else None,
            "games_lost":     _safe_int(cells[11]) if len(cells) > 11 else None,
            "games_won_pct":  cells[12] if len(cells) > 12 else (cells[-1] if cells else None),
        })
    return standings


def _tl_match_id_from_el(cell_el) -> Optional[str]:
    """Extract TennisLink numeric match ID from a result table cell (looks for links/onclick)."""
    if cell_el is None:
        return None
    # Check direct onclick on the cell
    for attr_el in [cell_el] + list(cell_el.query_selector_all("a, span, input")):
        for attr in ("onclick", "href"):
            try:
                val = attr_el.get_attribute(attr) or ""
            except Exception:
                continue
            # ViewScore(1234567, ...) or matchnum=1234567
            m = re.search(r'ViewScore\((\d{7,})', val)
            if m:
                return m.group(1)
            m = re.search(r'matchnum[=,\s]+(\d{7,})', val, re.I)
            if m:
                return m.group(1)
            # __doPostBack with numeric arg
            m = re.search(r"__doPostBack\([^,]+,\s*'?(\d{7,})", val)
            if m:
                return m.group(1)
    return None


def _extract_team_matches(page: Page, team_name: str) -> list[dict]:
    """
    Extract a team's match schedule from the 2-per-row table.
    Header: [Date, '', Opponent, Result, Date, '', Opponent, Result]
    Also captures TennisLink numeric match ID from result cell links.
    """
    matches = []
    for tbl in page.query_selector_all("table"):
        rows = tbl.query_selector_all("tr")
        if not rows:
            continue
        header = [td.inner_text().strip()
                  for td in rows[0].query_selector_all(":scope > td, :scope > th")]
        if header != ["Date", "", "Opponent", "Result", "Date", "", "Opponent", "Result"]:
            continue
        for tr in rows[1:]:
            cell_els = tr.query_selector_all(":scope > td")
            cells = [td.inner_text().strip().replace('\n', ' ') for td in cell_els]
            if len(cells) < 4:
                continue
            if cells[0] and cells[2] and re.match(r'\d+/\d+/\d+', cells[0]):
                tl_id = _tl_match_id_from_el(cell_els[3] if len(cell_els) > 3 else None)
                # Capture date link element for detail page navigation
                date_link_href = None
                if len(cell_els) > 0:
                    dl = cell_els[0].query_selector("a[href*='doPostBack']")
                    if dl:
                        date_link_href = dl.get_attribute("href")
                matches.append({"date": cells[0], "team": team_name,
                                "opponent": cells[2], "result": cells[3],
                                "tl_match_id": tl_id,
                                "_date_link_href": date_link_href})
            if (len(cells) >= 8 and cells[4] and cells[6]
                    and re.match(r'\d+/\d+/\d+', cells[4])):
                tl_id = _tl_match_id_from_el(cell_els[7] if len(cell_els) > 7 else None)
                date_link_href = None
                if len(cell_els) > 4:
                    dl = cell_els[4].query_selector("a[href*='doPostBack']")
                    if dl:
                        date_link_href = dl.get_attribute("href")
                matches.append({"date": cells[4], "team": team_name,
                                "opponent": cells[6], "result": cells[7],
                                "tl_match_id": tl_id,
                                "_date_link_href": date_link_href})
        if matches:
            break
    return matches


def _parse_match_detail_page(page: Page) -> list[dict]:
    """
    Parse line-by-line results from the TennisLink match detail page
    (reached by clicking the date link in the team schedule table).

    Returns list of dicts: {line, players_home, players_away, score, result}
    where line is e.g. "1# Singles", result is "home" or "away".

    Winner detection uses the radio button checked state (strWinner=Home/Visitor)
    rather than score parsing, because TennisLink displays scores winner-first
    (not home-first), making score-based detection unreliable.
    """
    lines = []
    body = page.query_selector("body")
    if not body:
        return lines
    text = body.inner_text()

    # --- Per-court winner via radio buttons ---
    # Parse the raw HTML (server-rendered ASP.NET WebForms) to find the `checked`
    # attribute on strWinner radio buttons.  We use page.content() + BeautifulSoup
    # rather than page.evaluate() because the attribute is set server-side and
    # evaluate() only sees the JS `.checked` property which may not reflect the
    # HTML attribute on disabled/readonly inputs.
    try:
        from bs4 import BeautifulSoup as _BS4
        _html = page.content()
        _soup = _BS4(_html, "html.parser")
        _radio_inputs = _soup.find_all("input", {"type": "radio", "name": "strWinner"})
        # Build list of {value, checked}
        radio_data = [
            {
                "value": inp.get("value", ""),
                "checked": inp.has_attr("checked"),
            }
            for inp in _radio_inputs
        ]
    except Exception:
        radio_data = []

    # Build court_winners list: one entry per court in DOM order.
    # Each court has a consecutive Home + Visitor radio pair.
    court_winners: list[str] = []
    i = 0
    while i + 1 < len(radio_data):
        pair = radio_data[i: i + 2]
        home_btn = next((r for r in pair if (r.get("value") or "").lower() == "home"), None)
        vis_btn  = next((r for r in pair if (r.get("value") or "").lower() in ("visitor", "away")), None)
        if home_btn is None or vis_btn is None:
            i += 1
            continue
        if home_btn.get("checked"):
            court_winners.append("home")
        elif vis_btn.get("checked"):
            court_winners.append("away")
        else:
            court_winners.append("")
        i += 2

    # --- Home/away team names ---
    home_team = ""
    away_team = ""
    ht_m = re.search(r'(\S[^\n]+?)\s+\(Home Team\)', text)
    at_m = re.search(r'(\S[^\n]+?)\s+\(Visiting Team\)', text)
    if ht_m:
        home_team = ht_m.group(1).strip()
    if at_m:
        away_team = at_m.group(1).strip()

    # Truncate at "TOTAL TEAM SCORE" to avoid parsing the summary section as player names
    cutoff = text.find("TOTAL TEAM SCORE")
    if cutoff != -1:
        text = text[:cutoff]

    # Split into line sections by pattern like "1# Singles", "2# Singles", "1# Doubles"
    line_sections = re.split(r'\n(?=\d+#\s+(?:Singles|Doubles))', text)
    court_idx = 0
    for section in line_sections:
        # Match "1# Singles" or "2# Doubles"
        lm = re.match(r'^(\d+)#\s+(Singles|Doubles)', section.strip())
        if not lm:
            continue
        line_num = int(lm.group(1))
        line_type = lm.group(2)
        line_label = f"{line_num}# {line_type}"

        # Extract player names and score from section
        section_body = section.strip()
        lines_in_section = [l.strip().replace('\xa0', '').strip() for l in section_body.split('\n') if l.strip().replace('\xa0', '').strip()]

        # Filter out noise tokens
        _noise = {'completed', 'not played', 'default', 'retired',
                  '2:00 pm', '3:00 pm', '4:00 pm', '10:00 am', '11:00 am', '12:00 pm',
                  'am', 'pm', 'n/a'}

        home_players: list[str] = []
        away_players: list[str] = []
        scores: list[str] = []
        in_away = False

        for raw_token in lines_in_section[1:]:  # skip header
            t = raw_token.strip().replace('\xa0', '').strip()
            tl = t.lower()

            if not t:
                continue

            # Time line may have player appended after a tab: "2:00 PM\tElsy Flores Rojas"
            if re.match(r'^\d+:\d+\s*(am|pm)', tl, re.I):
                parts = t.split('\t', 1)
                if len(parts) == 2:
                    player_part = parts[1].strip().replace('\xa0', '').strip()
                    if player_part and re.search(r'[a-zA-Z]', player_part) and len(player_part) > 1:
                        (away_players if in_away else home_players).append(player_part)
                continue

            if tl in _noise:
                continue
            if tl == 'vs.':
                in_away = True
                continue
            if re.match(r'^\d+-\d+$', t):
                scores.append(t)
                continue
            if tl in ('n/a', 'n/a / n/a', 'not available', 'default'):
                continue
            t = t.split('\t')[0].strip()
            tl = t.lower()
            if re.search(r'[a-zA-Z]', t) and len(t) > 1:
                (away_players if in_away else home_players).append(t)

        score_str = " ".join(scores)

        # Winner: use radio button result if available; fall back to score parsing
        if court_idx < len(court_winners) and court_winners[court_idx]:
            result = court_winners[court_idx]
        else:
            home_games = away_games = 0
            for s in scores:
                parts = s.split('-')
                if len(parts) == 2:
                    try:
                        h, a = int(parts[0]), int(parts[1])
                        if h > a:
                            home_games += 1
                        elif a > h:
                            away_games += 1
                    except Exception:
                        pass
            result = "home" if home_games > away_games else ("away" if away_games > home_games else "")

        court_idx += 1

        if home_players or away_players:
            lines.append({
                "line": line_label,
                "players_home": " / ".join(home_players),
                "players_away": " / ".join(away_players),
                "score": score_str,
                "result": result,
            })

    return lines


def _click_date_link_and_scrape_lines(page: Page, date_link_href: str) -> list[dict]:
    """
    Click the date link (doPostBack) on the team schedule page to navigate
    to the match detail page, scrape line data, then go back.
    Returns list of line dicts or [] on failure.
    """
    if not date_link_href:
        return []
    try:
        # Extract the doPostBack arguments and trigger it
        m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", date_link_href)
        if not m:
            return []
        event_target, event_argument = m.group(1), m.group(2)
        page.evaluate(
            f"__doPostBack('{event_target}', '{event_argument}')"
        )
        _wait_for_network(page, 12_000)
        sleep(1.5)

        lines = _parse_match_detail_page(page)

        # Navigate back to the team page
        page.go_back(wait_until="domcontentloaded", timeout=15_000)
        _wait_for_network(page, 10_000)
        sleep(1)

        return lines
    except Exception as e:
        print(f"          [warn] match detail scrape failed: {e}")
        # Try to recover by going back
        try:
            page.go_back(wait_until="domcontentloaded", timeout=10_000)
            sleep(1)
        except Exception:
            pass
        return []


def _extract_roster(page: Page) -> list[dict]:
    """Extract roster from 3-per-row Player Name / NTRP table."""
    for tbl in page.query_selector_all("table"):
        rows_all = []
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip()
                     for td in tr.query_selector_all(":scope > td, :scope > th")]
            if cells:
                rows_all.append(cells)
        if not rows_all or "Player Name" not in rows_all[0]:
            continue
        roster = []
        for row in rows_all[1:]:
            for i in range(0, len(row) - 1, 2):
                name = row[i].strip()
                ntrp_val = row[i + 1].strip() if i + 1 < len(row) else ""
                if name and name not in ("Player Name", "NTRP", ""):
                    roster.append({"name": name, "ntrp": ntrp_val})
        if roster:
            return roster
    return []


def _click_team_in_standings(page: Page, team_name: str) -> bool:
    """Click a team's doPostBack link in #TeamSummary to navigate to that team's page."""
    tbl = page.query_selector("#TeamSummary")
    if not tbl:
        return False
    for a in tbl.query_selector_all("a"):
        txt = (a.inner_text() or "").strip()
        href = a.get_attribute("href") or ""
        if txt == team_name and "javascript:__doPostBack" in href:
            a.click()
            _wait_for_network(page, 12_000)
            sleep(2)
            return True
    return False


def _parse_match_result(result: str, team_name: str, opponent: str) -> dict:
    """
    Parse a match result string e.g. "Won 5-0 Confirmed", "Lost 2-3 Confirmed",
    "Not Played", "Default - Won". Returns a partial match dict.
    The team_name is treated as home for consistency.
    """
    r = result.strip()
    pending = not r or r.lower() in ("not played", "tbd", "")
    team_wins = opp_wins = None

    if not pending:
        m = re.search(r'(\d+)\s*-\s*(\d+)', r)
        if m:
            team_wins, opp_wins = int(m.group(1)), int(m.group(2))

    score = f"{team_wins}-{opp_wins}" if (team_wins is not None and opp_wins is not None) else ""
    return {
        "home_team":      team_name,
        "away_team":      opponent,
        "team_wins_home": team_wins,
        "team_wins_away": opp_wins,
        "score":          score,
        "status":         r,
        "pending":        pending,
        "lines":          [],
    }


def _match_key(date: str, team_a: str, team_b: str) -> str:
    """Stable deduplication key for a match."""
    import hashlib
    teams = sorted([team_a.lower().strip(), team_b.lower().strip()])
    combined = f"{date.strip()}|{teams[0]}|{teams[1]}"
    return hashlib.md5(combined.encode()).hexdigest()[:16]


def _go_to_flight_page(page: Page) -> bool:
    """From a team page, click the Flight link to reach the flight-level page."""
    flight_link = page.query_selector("#ctl00_mainContent_lnkFlightForTeams")
    if not flight_link:
        for a in page.query_selector_all("a"):
            try:
                href = a.get_attribute("href") or ""
                txt = a.inner_text().strip()
                if "WOMEN" in txt.upper() and "lnkFlight" in href:
                    flight_link = a
                    break
            except Exception:
                pass
    if not flight_link:
        return False
    print(f"    Clicking Flight link: {flight_link.inner_text().strip()!r}")
    flight_link.click()
    _wait_for_network(page, 12_000)
    sleep(2)
    return True


def _discover_subflight_labels(page: Page, ntrp: str, year: int) -> list[str]:
    """
    Navigate to the flight page for this ntrp/year and return all subflight labels
    (e.g. ["A", "B"]). Labels are the single-letter doPostBack links on the flight page.
    """
    if not _navigate_to_my_team(page, ntrp, year):
        return []
    if not _go_to_flight_page(page):
        return []
    labels = []
    for a in page.query_selector_all("a"):
        try:
            txt = a.inner_text().strip()
            href = a.get_attribute("href") or ""
            if txt in ("A", "B", "C", "D", "E") and "javascript:__doPostBack" in href:
                if txt not in labels:
                    labels.append(txt)
        except Exception:
            pass
    return labels


def _navigate_to_subflight(page: Page, ntrp: str, year: int, label: str) -> bool:
    """
    Navigate to a specific subflight label (e.g. "A" or "B") and click the first team.
    Flow: my team → Flight page → subflight label link → first team link.
    Returns True if successfully on a team page within that subflight.
    """
    if not _navigate_to_my_team(page, ntrp, year):
        return False
    if not _go_to_flight_page(page):
        return False

    # Click the correct subflight link
    sf_link = None
    for a in page.query_selector_all("a"):
        try:
            txt = a.inner_text().strip()
            href = a.get_attribute("href") or ""
            if txt == label and "javascript:__doPostBack" in href:
                sf_link = a
                break
        except Exception:
            pass
    if not sf_link:
        print(f"    [warn] subflight {label!r} link not found on flight page")
        return False

    print(f"    Clicking subflight {label!r} link")
    sf_link.click()
    _wait_for_network(page, 12_000)
    sleep(2)

    # Click the first real team link on the subflight page
    for tbl in page.query_selector_all("table"):
        if not tbl.is_visible():
            continue
        for a in tbl.query_selector_all("a"):
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if "javascript:__doPostBack" in href and txt and not _is_nav_link(txt):
                    print(f"    Clicking first team in subflight {label}: {txt!r}")
                    a.click()
                    _wait_for_network(page, 12_000)
                    sleep(2)
                    return True
            except Exception:
                pass

    print(f"    [warn] no team links found on subflight {label!r} page")
    return False


def _scrape_subflight(page: Page, label: str) -> dict:
    """
    Scrape all teams in the current subflight.
    Assumes we're already on a team page within this subflight.
    Returns {"flight_label", "teams", "matches", "rosters"}.
    """
    print(f"  [subflight {label}] extracting standings ...")
    standings = _extract_standings_from_page(page)
    team_names = [s["team_name"] for s in standings]
    print(f"    {len(team_names)} teams: {team_names}")

    all_matches: dict[str, dict] = {}
    all_rosters: dict[str, list] = {}

    for i, team in enumerate(team_names):
        print(f"    [{i+1}/{len(team_names)}] {team!r}")
        # Always navigate explicitly — we may have landed on a different team's page
        # (e.g. the first team clicked to enter the subflight may differ from standings[0])
        if not _click_team_in_standings(page, team):
            print(f"      [warn] could not navigate to {team!r}, skipping")
            continue

        team_matches = _extract_team_matches(page, team)
        print(f"      {len(team_matches)} match entries")
        n_lines_scraped = 0
        for tm in team_matches:
            key = _match_key(tm["date"], tm["team"], tm["opponent"])
            parsed = _parse_match_result(tm["result"], tm["team"], tm["opponent"])
            tl_id = tm.get("tl_match_id")
            sc_url = f"{SCORECARD_BASE_URL}?matchnum={tl_id}" if tl_id else None

            if key not in all_matches:
                all_matches[key] = {
                    "match_id": key, "date": tm["date"],
                    "tl_match_id": tl_id, "scorecard_url": sc_url,
                    **parsed,
                    "lines": [],
                }
            else:
                if tl_id and not all_matches[key].get("tl_match_id"):
                    all_matches[key]["tl_match_id"] = tl_id
                    all_matches[key]["scorecard_url"] = sc_url

            # Scrape line details for confirmed matches (only if not yet fetched)
            if (not parsed.get("pending")
                    and not all_matches[key].get("lines")
                    and tm.get("_date_link_href")):
                lines = _click_date_link_and_scrape_lines(page, tm["_date_link_href"])
                if lines:
                    all_matches[key]["lines"] = lines
                    n_lines_scraped += 1

        if n_lines_scraped:
            print(f"      {n_lines_scraped} match detail page(s) scraped for line data")

        roster = _extract_roster(page)
        if roster:
            all_rosters[team] = roster
            print(f"      {len(roster)} roster players")

    matches_list = sorted(all_matches.values(), key=lambda m: m["date"])
    return {
        "flight_label": label,
        "teams": standings,
        "matches": matches_list,
        "rosters": all_rosters,
    }


def _fetch_tl_match_ids_for_subflight(page: Page, ntrp: str, year: int,
                                       label: str) -> dict[str, str]:
    """
    Navigate from the current team page to the subflight's Match Summary tab
    and extract a mapping of {match_dedup_key: tl_match_id}.
    Assumes we are currently on a page within the target subflight.
    """
    result: dict[str, str] = {}

    # Step 1: navigate to the flight overview page
    if not _go_to_flight_page(page):
        print(f"      [warn] could not reach flight page for match ID lookup")
        return result
    sleep(DELAY)

    # Step 2: click the subflight label link on the flight page
    # Note: do NOT use _is_nav_link here — single letters like 'A','B' are in NAV_SKIP_EXACT
    # but here we specifically WANT to click them.
    sf_link = None
    for a in page.query_selector_all("a"):
        try:
            href = a.get_attribute("href") or ""
            txt  = (a.inner_text() or "").strip()
            if "javascript:__doPostBack" in href and txt == label:
                sf_link = a
                break
        except Exception:
            continue

    if sf_link:
        sf_link.click()
        _wait_for_network(page, 12_000)
        sleep(DELAY)
    else:
        print(f"      [warn] subflight {label!r} link not found on flight page (match ID lookup)")
        # We're on the flight page — Match Summary may still be available
        pass

    # Step 3: click Match Summary / Match Schedule tab
    clicked_tab = False
    for tab_label in ("Match Summary", "Match Schedule"):
        for a in page.query_selector_all("a"):
            try:
                if tab_label in (a.inner_text() or ""):
                    a.click()
                    _wait_for_network(page, 10_000)
                    sleep(DELAY)
                    clicked_tab = True
                    break
            except Exception:
                pass
        if clicked_tab:
            break

    if not clicked_tab:
        print(f"      [warn] Match Summary tab not found for subflight {label!r}")
        return result

    # Step 4: parse match IDs
    summary_rows = _parse_match_summary_table(page)

    # DEBUG: dump all raw team names so we can see mismatches
    import pathlib as _pl
    _dbg = _pl.Path(f"/tmp/tl_match_summary_{ntrp.replace('.','')}_sf{label}.json")
    try:
        import json as _json
        _dbg.write_text(_json.dumps(summary_rows, indent=2))
    except Exception:
        pass

    for row in summary_rows:
        mid = row.get("match_id", "")
        date = row.get("date", "")
        ht = row.get("home_team", "")
        at = row.get("away_team", "")
        if mid and date and ht and at:
            key = _match_key(date, ht, at)
            result[key] = mid

    print(f"      [match IDs] {len(result)} TennisLink match IDs fetched for subflight {label}")
    return result


def _scrape_scorecard(page: Page, scorecard_url: str) -> list[dict]:
    """
    Fetch a TennisLink print-scorecard page and extract per-line details.
    Returns list of dicts: {line, players_home, players_away, score, result}
    where result is "home" or "away".
    """
    try:
        page.goto(scorecard_url, wait_until="domcontentloaded", timeout=25_000)
        sleep(1)
    except Exception as e:
        print(f"        [warn] scorecard load failed: {e}")
        return []

    lines_out = []
    body_text = ""
    body = page.query_selector("body")
    if body:
        body_text = body.inner_text()

    # ── Strategy 1: parse tables on the page ─────────────────────────────
    for tbl in page.query_selector_all("table"):
        text = tbl.inner_text() or ""
        # Only interested in tables that have line/court data
        if not re.search(r'Line\s*\d|Court\s*\d|Singles|Doubles', text, re.I):
            continue
        rows = tbl.query_selector_all("tr")
        current_line: Optional[dict] = None
        for tr in rows:
            cells = [td.inner_text().strip() for td in tr.query_selector_all("td, th")]
            row_text = " ".join(cells)
            # Detect "Line N" or "Court N" header row
            lm = re.search(r'(?:Line|Court)\s*(\d)', row_text, re.I)
            if lm:
                if current_line and current_line.get("players_home"):
                    lines_out.append(current_line)
                current_line = {
                    "line": int(lm.group(1)),
                    "players_home": "",
                    "players_away": "",
                    "score": "",
                    "result": "",
                }
                continue
            if current_line is None:
                continue
            # Score cell: looks like "6-3, 6-2" or "6-2 6-3"
            score_m = re.search(r'(\d-\d[\s,]+\d-\d(?:[\s,]+\d-\d)?)', row_text)
            if score_m and not current_line["score"]:
                current_line["score"] = re.sub(r'\s+', ' ', score_m.group(1).strip(", "))
            # Players – grab non-empty cells that look like names (not scores or labels)
            for c in cells:
                if not c or re.match(r'^\d-\d', c):
                    continue
                if re.search(r'\b(Winner|Result|Home|Away|Team|Score|Set)\b', c, re.I):
                    if re.search(r'\bHome\b', c, re.I) and "home" in c.lower():
                        current_line["result"] = "home"
                    elif re.search(r'\bAway\b', c, re.I) and "away" in c.lower():
                        current_line["result"] = "away"
                    continue
                if len(c) > 2 and not re.match(r'^\d+$', c):
                    if not current_line["players_home"]:
                        current_line["players_home"] = c
                    elif not current_line["players_away"]:
                        current_line["players_away"] = c
        if current_line and current_line.get("players_home"):
            lines_out.append(current_line)
        if lines_out:
            break

    # ── Strategy 2: parse body text if table parsing got nothing ─────────
    if not lines_out and body_text:
        lines_out = _parse_scorecard_text(body_text)

    return lines_out


def _parse_scorecard_text(text: str) -> list[dict]:
    """
    Fallback: parse scorecard body text for line-by-line data.
    Handles TennisLink's plain-text scorecard format.
    """
    lines_out = []
    # Split into sections by "Line N" markers
    sections = re.split(r'\n(?=(?:Line|Court)\s*\d)', text, flags=re.I)
    for section in sections:
        lm = re.match(r'(?:Line|Court)\s*(\d)', section.strip(), re.I)
        if not lm:
            continue
        line_num = int(lm.group(1))
        entry = {"line": line_num, "players_home": "", "players_away": "",
                 "score": "", "result": ""}

        # Score pattern
        score_m = re.search(r'(\d-\d(?:[,\s]+\d-\d)+)', section)
        if score_m:
            entry["score"] = re.sub(r'\s+', ' ', score_m.group(1).strip())

        # Player names: look for lines with actual names
        name_lines = []
        for line in section.split('\n'):
            stripped = line.strip()
            # Skip blank, score-like, or label lines
            if not stripped:
                continue
            if re.match(r'^(?:Line|Court|Set|Score|Winner|Result|Singles|Doubles)', stripped, re.I):
                continue
            if re.match(r'^[\d\-,\s]+$', stripped):
                continue
            if len(stripped) > 3:
                name_lines.append(stripped)

        if len(name_lines) >= 1:
            entry["players_home"] = name_lines[0]
        if len(name_lines) >= 2:
            entry["players_away"] = name_lines[1]

        # Winner
        winner_m = re.search(r'Winner[:\s]+(Home|Away)', section, re.I)
        if winner_m:
            entry["result"] = winner_m.group(1).lower()
        else:
            # Infer from score: whoever wins more sets
            sets_home = sets_away = 0
            for sm in re.finditer(r'(\d)-(\d)', entry["score"]):
                h, a = int(sm.group(1)), int(sm.group(2))
                if h > a:
                    sets_home += 1
                elif a > h:
                    sets_away += 1
            if sets_home > sets_away:
                entry["result"] = "home"
            elif sets_away > sets_home:
                entry["result"] = "away"

        if entry["players_home"]:
            lines_out.append(entry)

    return lines_out


def _scrape_all_scorecards(page: Page, all_standings: list[dict]) -> None:
    """
    For every completed match that has a scorecard_url, fetch the scorecard
    and fill in match["lines"]. Modifies all_standings in place.
    """
    # Collect all matches needing scorecard data
    to_fetch: list[tuple[dict, dict]] = []  # (subflight_dict, match_dict)
    for sf_data in all_standings:
        for m in sf_data.get("matches", []):
            if not m.get("pending") and m.get("scorecard_url") and not m.get("lines"):
                to_fetch.append((sf_data, m))

    if not to_fetch:
        print(f"  [scorecards] No scorecards to fetch.")
        return

    print(f"  [scorecards] Fetching {len(to_fetch)} scorecard(s) ...")
    fetched = 0
    for sf_data, m in to_fetch:
        url = m["scorecard_url"]
        mid = m.get("tl_match_id", m["match_id"])
        print(f"    [{fetched+1}/{len(to_fetch)}] match {mid} ({m.get('date','?')}: "
              f"{m.get('home_team','?')} vs {m.get('away_team','?')})")
        lines = _scrape_scorecard(page, url)
        m["lines"] = lines
        if lines:
            print(f"      → {len(lines)} line(s) parsed")
        else:
            print(f"      → no line data found")
        fetched += 1
        sleep(DELAY)


def _line_label_short(lnum: str) -> str:
    """Convert '1# Singles' -> 'S1', '2# Doubles' -> 'D2', etc."""
    m = re.match(r'^(\d+)#\s+(Singles|Doubles)', (lnum or "").strip())
    if not m:
        return lnum or ""
    prefix = "S" if m.group(2) == "Singles" else "D"
    return f"{prefix}{m.group(1)}"


def _score_winner(score: str) -> str:
    """
    Parse a score like '6-3 6-4' or '3-6 6-4 10-5' to determine
    who won from the home team's perspective.
    Returns 'home', 'away', or '' if unable to determine.
    """
    if not score:
        return ""
    sets = score.strip().split()
    home_sets, away_sets = 0, 0
    for s in sets:
        m = re.match(r'^(\d+)-(\d+)$', s)
        if m:
            h, a = int(m.group(1)), int(m.group(2))
            if h > a:
                home_sets += 1
            elif a > h:
                away_sets += 1
    if home_sets > away_sets:
        return "home"
    elif away_sets > home_sets:
        return "away"
    return ""


def _compute_player_stats_from_scorecards(all_ntrp_standings: list[tuple[str, list]]):
    """
    Walk all match lines across all subflights, compute per-player per-division:
      - lines_played_30 / lines_played_35  (list of court labels with counts, e.g. ["D1x2","D2","S2"])
      - wl_record_30 / wl_record_35        ("W-L" string)
      - team_30 / team_35                  (team the player actually played for in that division)

    W/L is determined by the per-court result field (set by radio button detection in
    _parse_match_detail_page). Falls back to match-level team result for lines without
    a court-level result.
    """
    from collections import defaultdict, Counter
    players = load_json(PLAYERS_JSON, [])
    by_name: dict[str, dict] = {p["name"].lower().strip(): p for p in players}

    # Build player-name → registered team lookup (normalized to uppercase for comparison)
    player_team: dict[str, str] = {
        p["name"].lower().strip(): (p.get("team") or "").upper().strip()
        for p in players
    }

    # Per-ntrp accumulators
    wins:         dict[str, dict] = defaultdict(lambda: defaultdict(int))
    losses:       dict[str, dict] = defaultdict(lambda: defaultdict(int))
    defaults_w:   dict[str, dict] = defaultdict(lambda: defaultdict(int))  # default wins
    lines_count:  dict[str, dict] = defaultdict(lambda: defaultdict(Counter))  # court label counts
    match_teams:  dict[str, dict] = defaultdict(dict)  # which team player played for per division

    def _split_players(field: str) -> list[str]:
        cleaned = re.sub(r",?\s*\d+-\d+.*$", "", field).strip()
        return [n.strip() for n in re.split(r"\s*/\s*", cleaned) if n.strip()]

    def _team_norm(t: str) -> str:
        return (t or "").upper().strip()

    for ntrp, subflights in all_ntrp_standings:
        for sf in subflights:
            for m in sf.get("matches", []):
                if m.get("pending") or not m.get("lines"):
                    continue

                # Match-level result (fallback for courts without per-court result)
                hw = m.get("team_wins_home") or 0
                aw = m.get("team_wins_away") or 0
                match_home_won = hw > aw
                match_away_won = aw > hw

                match_home = _team_norm(m.get("home_team", ""))
                match_away = _team_norm(m.get("away_team", ""))

                # --- Swap detection ---
                # TennisLink scorecards sometimes list the away team players on the
                # "home" side in the text and vice versa. Detect this by scanning all
                # lines: if more players from match_home appear in players_away (or
                # match_away players appear in players_home), the scorecard is swapped.
                normal_votes = 0
                swap_votes   = 0
                for _ln in m["lines"]:
                    for _pn in _split_players(_ln.get("players_home", "")):
                        _pt = player_team.get(_pn.lower().strip(), "")
                        if _pt == match_home:  normal_votes += 1
                        elif _pt == match_away: swap_votes  += 1
                    for _pn in _split_players(_ln.get("players_away", "")):
                        _pt = player_team.get(_pn.lower().strip(), "")
                        if _pt == match_away:  normal_votes += 1
                        elif _pt == match_home: swap_votes  += 1
                is_swapped = swap_votes > normal_votes

                for ln in m["lines"]:
                    court_label = _line_label_short(ln.get("line", ""))
                    # Per-court winner from radio buttons (set during scraping)
                    court_result = (ln.get("result") or "").lower()  # "home", "away", or ""

                    # Detect default/walkover lines — one side has no players listed
                    # (empty string or literal "N/A" / "N/A / N/A").
                    # Defaults DO count toward W/L records and lines-played (they show in
                    # the roster exactly like real matches). They are tracked separately via
                    # the defaults_NNN counter so analysis can distinguish competitive wins.
                    def _is_default_side(s: str) -> bool:
                        s = (s or "").strip().upper()
                        return not s or s in ("N/A", "N/A / N/A", "DEFAULT", "NOT AVAILABLE")
                    _line_is_default = (
                        _is_default_side(ln.get("players_home", "")) or
                        _is_default_side(ln.get("players_away", ""))
                    )

                    def _process(pname: str, parsed_is_home: bool):
                        key = pname.lower().strip()
                        if not key:
                            return
                        if court_label:
                            lines_count[ntrp][key][court_label] += 1

                        # Determine which side this player is actually on:
                        # 1. If registered team matches one of the match teams → use that
                        # 2. Otherwise, use parsed side but flip it if the scorecard is swapped
                        pteam = player_team.get(key, "")
                        if pteam and match_home and match_away:
                            if pteam == match_home:
                                actual_home = True
                            elif pteam == match_away:
                                actual_home = False
                            else:
                                # Unknown team: flip if scorecard is swapped
                                actual_home = (not parsed_is_home) if is_swapped else parsed_is_home
                        else:
                            actual_home = (not parsed_is_home) if is_swapped else parsed_is_home

                        # Record which team they played for in this division
                        if match_home and match_away:
                            match_teams[ntrp][key] = match_home if actual_home else match_away

                        # Determine W/L using per-court result if available,
                        # otherwise fall back to match-level result
                        if court_result in ("home", "away"):
                            won = (court_result == "home") if actual_home else (court_result == "away")
                        elif match_home_won or match_away_won:
                            won = match_home_won if actual_home else match_away_won
                        else:
                            return  # no result available; skip

                        if won:
                            wins[ntrp][key] += 1
                            if _line_is_default:
                                defaults_w[ntrp][key] += 1
                        else:
                            losses[ntrp][key] += 1

                    for pname in _split_players(ln.get("players_home", "")):
                        _process(pname, parsed_is_home=True)
                    for pname in _split_players(ln.get("players_away", "")):
                        _process(pname, parsed_is_home=False)

    # Map ntrp string to field suffix
    def _suffix(ntrp_str: str) -> str:
        return ntrp_str.replace(".", "")   # "3.0" -> "30", "3.5" -> "35"

    # Collect all (ntrp, player_key) pairs that appeared in any accumulator
    all_keys: dict[str, set] = defaultdict(set)
    for ntrp_key in lines_count:
        all_keys[ntrp_key].update(lines_count[ntrp_key].keys())
    for ntrp_key in wins:
        all_keys[ntrp_key].update(wins[ntrp_key].keys())

    updated = 0
    for ntrp_key, player_keys in all_keys.items():
        sfx = _suffix(ntrp_key)
        for key in player_keys:
            p = by_name.get(key)
            if not p:
                continue
            # Build sorted court list with multiplicity: ["D1x2", "D2", "S2"]
            counter = lines_count[ntrp_key].get(key, Counter())
            sorted_courts = []
            for label in sorted(counter, key=lambda x: (x[0], int(x[1:]) if x[1:].isdigit() else 0)):
                cnt = counter[label]
                sorted_courts.append(f"{label}x{cnt}" if cnt > 1 else label)
            w = wins[ntrp_key].get(key, 0)
            l = losses[ntrp_key].get(key, 0)
            dw = defaults_w[ntrp_key].get(key, 0)
            p[f"lines_played_{sfx}"] = sorted_courts
            p[f"wl_record_{sfx}"] = f"{w}-{l}"
            # Track default wins separately so analysis can show competitive-only record.
            # Only write the field when non-zero to keep the JSON lean.
            if dw:
                p[f"default_wins_{sfx}"] = dw
            elif f"default_wins_{sfx}" in p:
                del p[f"default_wins_{sfx}"]
            # Store the team they played for in this division (used for roster placement)
            team = match_teams[ntrp_key].get(key, "")
            if team:
                p[f"team_{sfx}"] = team
            updated += 1

    save_json(PLAYERS_JSON, players)
    print(f"  [player stats] Updated per-division lines/W-L/team for {updated} player(s)")


def _update_players_from_rosters(rosters_a: dict, rosters_b: dict, ntrp: str):
    """Add new players or update ntrp_rating from scraped rosters into players.json."""
    players = load_json(PLAYERS_JSON, [])
    existing_by_name = {p["name"].lower().strip(): p for p in players}

    added = 0
    for is_b, rosters in [(False, rosters_a), (True, rosters_b)]:
        division = f"{ntrp} Women {'B' if is_b else 'A'}"
        for team_name, roster in rosters.items():
            for player in roster:
                name = player["name"].strip()
                ntrp_val = player.get("ntrp", "").strip()
                key = name.lower().strip()
                if key in existing_by_name:
                    p = existing_by_name[key]
                    if not p.get("ntrp_rating") and ntrp_val:
                        p["ntrp_rating"] = ntrp_val
                    if not p.get("team"):
                        p["team"] = team_name
                else:
                    try:
                        from scrape_players import slugify
                        pid = slugify(name)
                    except Exception:
                        import re as _re
                        pid = _re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
                    stub = {
                        "id": pid,
                        "name": name,
                        "tennislink_id": None,
                        "profile_url": None,
                        "team": team_name,
                        "division": division,
                        "dynamic_rating_baseline": None,
                        "current_division_rating": None,
                        "global_rating": None,
                        "ntrp_rating": ntrp_val or None,
                        "wl_record": None,
                        "lines_played": None,
                        "lines_html": None,
                        "notes": None,
                        "pending_tennisrecord_lookup": True,
                    }
                    players.append(stub)
                    existing_by_name[key] = stub
                    added += 1

    save_json(PLAYERS_JSON, players)
    print(f"  [players] {added} new player(s) added from rosters")


# ---------------------------------------------------------------------------
# MODE 1 main
# ---------------------------------------------------------------------------

def run_mode1(page: Page, year: int):
    print("\n=== MODE 1: Standings + match results (USTA# navigation) ===")

    for ntrp, out_path in [("3.0", OUTPUT_STANDINGS_30), ("3.5", OUTPUT_STANDINGS_35)]:
        print(f"\n--- {ntrp} Women ---")

        # Discover all subflight labels (A, B, ...) from the flight page
        print(f"  Discovering subflights for {ntrp} Women {year} ...")
        labels = _discover_subflight_labels(page, ntrp, year)
        if not labels:
            print(f"  [warn] no subflights found for {ntrp} Women – skipping")
            continue
        print(f"  Found subflights: {labels}")

        all_subflights = []
        all_rosters: dict[str, list] = {}

        for label in labels:
            print(f"  Navigating to subflight {label!r} ...")
            ok = _navigate_to_subflight(page, ntrp, year, label)
            if not ok:
                print(f"  [warn] could not navigate to subflight {label!r}")
                continue
            sf_data = _scrape_subflight(page, label)

            # Enrich matches with TennisLink numeric match IDs (from Match Summary tab)
            print(f"  Fetching TennisLink match IDs for subflight {label!r} ...")
            tl_id_map = _fetch_tl_match_ids_for_subflight(page, ntrp, year, label)
            n_enriched = 0
            for m in sf_data["matches"]:
                key = m["match_id"]
                if key in tl_id_map and not m.get("tl_match_id"):
                    tl_id = tl_id_map[key]
                    m["tl_match_id"] = tl_id
                    m["scorecard_url"] = f"{SCORECARD_BASE_URL}?matchnum={tl_id}"
                    n_enriched += 1
            print(f"    {n_enriched}/{len(sf_data['matches'])} matches enriched with TL IDs")

            all_subflights.append(sf_data)
            all_rosters.update(sf_data.get("rosters", {}))

        if not all_subflights:
            print(f"  [warn] no subflights scraped for {ntrp} Women")
            continue

        # Persist player roster data into players.json
        # Split rosters by subflight for correct division labeling
        for sf in all_subflights:
            sf_label = sf["flight_label"]
            sf_rosters = sf.get("rosters", {})
            is_b = sf_label != labels[0]  # first label = A equivalent
            _update_players_from_rosters(
                {} if is_b else sf_rosters,
                sf_rosters if is_b else {},
                ntrp,
            )

        # Save standings file (strip rosters – stored in players.json)
        total_matches = sum(len(sf["matches"]) for sf in all_subflights)
        result = {
            "ntrp": ntrp,
            "year": year,
            "subflights": [{k: v for k, v in sf.items() if k != "rosters"}
                           for sf in all_subflights],
        }
        save_json(out_path, result)
        for sf in all_subflights:
            n_with_lines = sum(1 for m in sf["matches"]
                               if not m.get("pending") and m.get("lines"))
            print(f"  {ntrp} subflight {sf['flight_label']}: "
                  f"{len(sf['teams'])} teams  {len(sf['matches'])} matches  "
                  f"{n_with_lines} with line data")
        print(f"  Total matches: {total_matches}")


# LEGACY helpers kept for potential reuse / import compatibility
SUBFLIGHT_URL_CACHE = DATA_DIR / "subflight_urls.json"


def get_subflight_links(page: Page, ntrp: str, year: int) -> list[dict]:
    """
    Find NV Area F subflight URLs for the given NTRP level and year.

    Strategy:
    1. Check cache in data/subflight_urls.json for previously discovered URLs.
    2. If not cached, discover via player search:
       - Search for a known player from each subflight (A and B)
       - Click their team link
       - Click the subflight link
       - Extract the stable "Link to this Page" URL
    """
    print(f"  [standings] finding subflight URLs for {ntrp} Women {year} …")

    cache = load_json(SUBFLIGHT_URL_CACHE, {})
    cache_key = f"{ntrp}_{year}"
    if cache.get(cache_key):
        print(f"    using cached subflight URLs ({len(cache[cache_key])} found)")
        return cache[cache_key]

    players_data = load_json(PLAYERS_JSON, [])
    results: list[dict] = []

    for subflight_label in ["A", "B"]:
        div_name = f"{ntrp} Women {subflight_label}"
        player = next(
            (p for p in players_data
             if p.get("division") == div_name and p.get("team")),
            None
        )
        if not player:
            print(f"    [warn] no players found for {div_name} in players.json")
            continue

        url = _find_subflight_url_via_player(page, player["name"], player["team"])
        if url:
            print(f"    found {div_name} URL: {url}")
            results.append({"flight_label": div_name, "url": url})
        else:
            print(f"    [warn] could not find subflight URL for {div_name} (player: {player['name']})")

    if results:
        cache[cache_key] = results
        save_json(SUBFLIGHT_URL_CACHE, cache)

    print(f"    found {len(results)} subflight(s)")
    return results


def _find_subflight_url_via_player(page: Page, player_name: str, team_name: str) -> Optional[str]:
    """
    Search for player, click their NV/Las Vegas team row's team link,
    then click the subflight link, and return the canonical subflight URL.
    """
    parts = player_name.strip().split()
    first = parts[0] if parts else player_name
    last  = " ".join(parts[1:]) if len(parts) > 1 else ""

    try:
        page.goto(PLAYER_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
        sleep(DELAY)
        page.fill("#ctl00_mainContent_txtFirstName", first, timeout=3_000)
        page.fill("#ctl00_mainContent_txtLastName",  last,  timeout=3_000)
        page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
        _wait_for_network(page, 12_000)
        sleep(0.5)
    except Exception as e:
        print(f"    [warn] player search failed for {player_name!r}: {e}")
        return None

    # Find the NV row matching the expected team_name
    team_a = _find_nv_team_link(page, team_name)
    if not team_a:
        # Fallback: any NV row
        team_a = _find_nv_team_link(page, "")
    if not team_a:
        return None

    try:
        team_a.click()
        _wait_for_network(page, 12_000)
        sleep(DELAY)
    except Exception as e:
        print(f"    [warn] click team link failed: {e}")
        return None

    # Click the SubFlight link (not Flight, to get to the subflight-level page)
    sub_link = page.query_selector("#ctl00_mainContent_lnkSubFlightForTeams")
    if sub_link:
        try:
            sub_link.click()
            _wait_for_network(page, 12_000)
            sleep(DELAY)
        except Exception as e:
            print(f"    [warn] click subflight link failed: {e}")
            return None

    # Extract "Link to this Page" URL
    link_el = page.query_selector("a.share-link, #ctl00_mainContent_lnkShare")
    if link_el:
        href = link_el.get_attribute("href") or ""
        if href:
            return urljoin(BASE_URL + "/Leagues/Main/", href)

    return None


def _find_nv_team_link(page: Page, team_name: str):
    """Return the first team <a> element in a NV/Las Vegas row matching team_name."""
    for tbl in page.query_selector_all("table.CommonTable.Segmented"):
        for tr in tbl.query_selector_all("tbody tr"):
            tds = tr.query_selector_all("td")
            if len(tds) < 3:
                continue
            city_state = (tds[1].inner_text() or "").strip()
            if ",NV" not in city_state and "Las Vegas" not in city_state:
                continue
            team_a = tds[2].query_selector("a")
            if not team_a:
                continue
            if not team_name or team_name.lower() in (team_a.inner_text() or "").lower():
                return team_a
    return None


def scrape_subflight(page: Page, flight: dict) -> dict:
    """
    Scrape one subflight's standings and match summary.
    Uses the direct subflight URL (t=6&par1=...&par2=year&par3=0).

    Standings: parsed from Team Standings tab.
    Matches: parsed from Match Summary tab — includes match_id, date, home/visitor,
             team wins, match score, status.
    Scorecard: for each match, records the printscorecard URL for later use.
    """
    print(f"  [subflight] {flight['flight_label']} → {flight['url']}")
    page.goto(flight["url"], wait_until="domcontentloaded", timeout=30_000)
    sleep(DELAY)

    # ── Team Standings tab ───────────────────────────────────────────────────
    teams = []
    try:
        _click_tab_text(page, "Team Standings")
        sleep(DELAY)
        teams = _parse_team_standings_table(page)
    except Exception as e:
        print(f"    [warn] standings parse error: {e}")

    # ── Match Summary tab ────────────────────────────────────────────────────
    matches = []
    try:
        page.goto(flight["url"], wait_until="domcontentloaded", timeout=30_000)
        sleep(DELAY)
        _click_tab_text(page, "Match Summary")
        sleep(DELAY)
        matches = _parse_match_summary_table(page)
    except Exception as e:
        print(f"    [warn] match summary parse error: {e}")

    # Add scorecard URLs to each match
    for m in matches:
        mid = m.get("match_id")
        if mid:
            m["scorecard_url"] = f"{SCORECARD_BASE_URL}?matchnum={mid}"

    print(f"    {len(teams)} teams, {len(matches)} matches")
    return {
        "flight_label": flight["flight_label"],
        "flight_url": flight["url"],
        "teams": teams,
        "matches": matches,
    }


def _click_tab_text(page: Page, label: str):
    """Click a tab link/button by its text label."""
    for a in page.query_selector_all("a"):
        if label in (a.inner_text() or ""):
            a.click()
            return
    for btn in page.query_selector_all("button"):
        if label in (btn.inner_text() or ""):
            btn.click()
            return


def _parse_team_standings_table(page: Page) -> list[dict]:
    """
    Parse the Team Standings tab table.

    Discovered column layout (from live TennisLink inspection):
    [0]=Team ID, [1]=Team Name, [2]=Matches Played, [3]=Games Won*, [4]=Points*,
    [5]=Team Score Wins, [6]=Team Score Losses, [7]=Indiv Wins, [8]=Indiv Losses,
    ... (hidden cols) ... [16]=Games Won %
    """
    teams = []
    for tbl in page.query_selector_all("table"):
        txt = tbl.inner_text() or ""
        if "Team Name" not in txt and "team name" not in txt.lower():
            continue
        for tr in tbl.query_selector_all("tr"):
            # Use direct td children only (avoid nested tooltip table cells)
            cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
            if len(cells) < 3:
                continue
            # Data rows have '*****' in the Team ID column (first col)
            if cells[0] == "*****" and cells[1]:
                teams.append({
                    "team_name":      cells[1],
                    "matches_played": _safe_int(cells[2]) if len(cells) > 2 else None,
                    "team_wins":      _safe_int(cells[5]) if len(cells) > 5 else None,
                    "team_losses":    _safe_int(cells[6]) if len(cells) > 6 else None,
                    "indiv_wins":     _safe_int(cells[7]) if len(cells) > 7 else None,
                    "indiv_losses":   _safe_int(cells[8]) if len(cells) > 8 else None,
                    "games_won_pct":  cells[-1] if len(cells) > 0 else None,
                })
        if teams:
            break
    return teams


def _parse_match_summary_table(page: Page) -> list[dict]:
    """
    Parse the Match Summary tab table.

    Discovered column layout (using :scope > td to exclude tooltip nested cells):
    [0]=Match ID (link text), [1]=Schedule Date, [2]=Schedule Time,
    [3]=Home Team, [4]=Team ID, [5]=Visiting Team, [6]=Team ID,
    [7]=Team Wins Home, [8]=Team Wins Visitor,
    [9]=Match Score Home, [10]=Match Score Visitor,
    [11]=Match Status
    """
    rows = []
    seen_ids: set[str] = set()
    for tbl in page.query_selector_all("table"):
        txt = tbl.inner_text() or ""
        # Match summary tables have "Match ID" header and numeric match IDs
        if "Match ID" not in txt:
            continue
        tbl_rows: list[dict] = []
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
            if len(cells) < 8:
                continue
            # First cell should be a numeric match ID
            match_id = re.sub(r'\D', '', cells[0])
            if not match_id or len(match_id) < 7:
                continue
            if match_id in seen_ids:
                continue
            seen_ids.add(match_id)
            def _norm_team(s: str) -> str:
                """Collapse newlines/tabs and multiple spaces in a team name."""
                return re.sub(r"\s+", " ", s.replace("\n", " ").replace("\t", " ")).strip()

            row = {
                "match_id":         match_id,
                "date":             cells[1] if len(cells) > 1 else "",
                "time":             cells[2] if len(cells) > 2 else "",
                "home_team":        _norm_team(cells[3]) if len(cells) > 3 else "",
                "away_team":        _norm_team(cells[5]) if len(cells) > 5 else "",
                "team_wins_home":   _safe_int(cells[7]) if len(cells) > 7 else None,
                "team_wins_away":   _safe_int(cells[8]) if len(cells) > 8 else None,
                "score_home":       _safe_int(cells[9]) if len(cells) > 9 else None,
                "score_away":       _safe_int(cells[10]) if len(cells) > 10 else None,
                "status":           cells[11] if len(cells) > 11 else "",
            }
            # Pending = no result yet (both scores 0 or blank)
            row["pending"] = (
                row["team_wins_home"] is None and row["team_wins_away"] is None
            ) or (
                (row["team_wins_home"] == 0 and row["team_wins_away"] == 0)
                and not row["status"]
            )
            tbl_rows.append(row)
        rows.extend(tbl_rows)
        # Don't break — TL sometimes renders sub-groups in separate tables on the same page
    return rows


def _is_pending(score: str) -> bool:
    if not score or score.strip() in ("", "0-0", "0 - 0", "TBD", "N/A"):
        return True
    parts = re.split(r"[,;]", score)
    if all(re.fullmatch(r"\s*0\s*-\s*0\s*", p) for p in parts):
        return True
    return False


def recheck_pending(existing_matches: list[dict], page: Page) -> list[dict]:
    """Re-fetch scorecard pages for matches previously marked pending."""
    updated = []
    for m in existing_matches:
        if m.get("pending") and m.get("scorecard_url"):
            print(f"    [recheck pending] {m['match_id']}")
            # For now, just check if scorecard now has results
            try:
                page.goto(m["scorecard_url"], wait_until="domcontentloaded", timeout=20_000)
                sleep(DELAY * 0.5)
                # Check if page has actual results (name filled in)
                content = page.content()
                if "Winner" in content and re.search(r"Name:\s+\S", content):
                    m["pending"] = False
            except Exception:
                pass
        updated.append(m)
    return updated


# ---------------------------------------------------------------------------
# MODE 2 helpers
# ---------------------------------------------------------------------------

PLAYER_SEARCH_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"

# Kept for compatibility; gets replaced per-player after lookup
PLAYER_STATS_URL = PLAYER_SEARCH_URL


def find_tennislink_id(page: Page, player_name: str) -> Optional[str]:
    """
    Search TennisLink for a player and return a stable ID.
    Since TennisLink uses __doPostBack links (not URL params), we use the
    player's link element ID as the tennislink_id (e.g. the button postback target).
    Returns None if not found.
    """
    parts = player_name.strip().split()
    first = parts[0] if parts else player_name
    last  = " ".join(parts[1:]) if len(parts) > 1 else ""

    try:
        page.goto(PLAYER_SEARCH_URL, wait_until="domcontentloaded", timeout=20_000)
        sleep(DELAY * 0.5)
        page.fill("#ctl00_mainContent_txtFirstName", first, timeout=2_000)
        page.fill("#ctl00_mainContent_txtLastName",  last,  timeout=2_000)
        page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
        _wait_for_network(page, 12_000)
        sleep(0.5)
    except Exception:
        return None

    name_lower = player_name.lower()
    parts_last = last.lower()

    # Look in the result tables for an NV row matching this player
    for tbl in page.query_selector_all("table.CommonTable.Segmented"):
        for tr in tbl.query_selector_all("tbody tr"):
            tds = tr.query_selector_all("td")
            if len(tds) < 3:
                continue
            name_a = tds[0].query_selector("a")
            if not name_a:
                continue
            aname = (name_a.inner_text() or "").strip().lower()
            if aname != name_lower:
                continue
            city_state = (tds[1].inner_text() or "").strip()
            if ",NV" in city_state or "Las Vegas" in city_state:
                # Use the element's id as the tennislink_id (stable within session)
                el_id = name_a.get_attribute("id") or ""
                if el_id:
                    return el_id
                # Fallback: use a hash of name + city
                return _stable_id(player_name, city_state)
    return None


def _search_player_and_get_nv_row(page: Page, player_name: str):
    """
    Search for a player and return (name_a_element, team_a_element) for their NV row.
    Returns (None, None) if not found.
    """
    parts = player_name.strip().split()
    first = parts[0] if parts else player_name
    last  = " ".join(parts[1:]) if len(parts) > 1 else ""

    try:
        page.goto(PLAYER_SEARCH_URL, wait_until="domcontentloaded", timeout=20_000)
        sleep(DELAY * 0.5)
        page.fill("#ctl00_mainContent_txtFirstName", first, timeout=2_000)
        page.fill("#ctl00_mainContent_txtLastName",  last,  timeout=2_000)
        page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
        _wait_for_network(page, 12_000)
        sleep(0.5)
    except Exception:
        return None, None

    name_lower = player_name.lower()
    for tbl in page.query_selector_all("table.CommonTable.Segmented"):
        for tr in tbl.query_selector_all("tbody tr"):
            tds = tr.query_selector_all("td")
            if len(tds) < 3:
                continue
            name_a = tds[0].query_selector("a")
            if not name_a:
                continue
            if name_a.inner_text().strip().lower() != name_lower:
                continue
            city_state = (tds[1].inner_text() or "").strip()
            if ",NV" not in city_state and "Las Vegas" not in city_state:
                continue
            team_a = tds[2].query_selector("a")
            return name_a, team_a
    return None, None


def scrape_player_leagues(page: Page, tennislink_id: str, player_name: str) -> list[dict]:
    """
    Navigate to a player's match history via player search → click player name.
    Collects all league records shown on the player's history page.
    Returns list of {match_id, date, home_team, away_team, ...} records.
    """
    name_a, _ = _search_player_and_get_nv_row(page, player_name)
    if not name_a:
        return []

    try:
        name_a.click()
        _wait_for_network(page, 12_000)
        sleep(DELAY)
    except Exception as e:
        return []

    # Player history page: parse league rows from the result tables
    # Each row shows: team, league, match date, opponent, score
    matches = []
    try:
        matches = _parse_player_history_page(page, player_name)
    except Exception as e:
        print(f"    [warn] player history parse error for {player_name}: {e}")

    return matches


def _parse_player_history_page(page: Page, player_name: str) -> list[dict]:
    """
    Parse the player history/stats page after clicking a player's name link.
    Returns list of match records.
    """
    matches = []
    # The page shows match data in tables with team/opponent/score columns
    for tbl in page.query_selector_all("table"):
        txt = tbl.inner_text() or ""
        # Look for tables with match-related content
        if not any(kw in txt for kw in ["W/L", "Score", "Match", "Date"]):
            continue
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
            if len(cells) < 3:
                continue
            # Try to parse as a match row
            # Format varies, but we look for date-like patterns and scores
            row = {}
            for i, cell in enumerate(cells):
                if re.match(r"\d{1,2}/\d{1,2}/\d{4}", cell):
                    row["date"] = cell
                elif re.match(r"\d+-\d+", cell):
                    row["score"] = cell
                elif i == 0 and not row.get("team"):
                    row["team"] = cell
            if row.get("date") and (row.get("score") or row.get("team")):
                row["player"] = player_name
                row["match_id"] = _stable_id(player_name, row.get("date",""), row.get("team",""))
                matches.append(row)
    return matches




def _find_player_in_lines(player_name: str, lines: list[dict]) -> list[str]:
    """Return list of player names from the tracked set that appear in match lines."""
    name_lower = player_name.lower()
    found = []
    for line in lines:
        combined = " ".join([
            line.get("players_home", ""),
            line.get("players_away", ""),
            line.get("player", ""),
        ]).lower()
        if name_lower in combined:
            found.append(player_name)
            break
    return found


# ---------------------------------------------------------------------------
# MODE 2 main
# ---------------------------------------------------------------------------

def run_mode2(page: Page):
    print("\n=== MODE 2: Cross-league player match history ===")

    players: list[dict] = load_json(PLAYERS_JSON, [])
    if not players:
        print("  ERROR: data/players.json is empty or missing")
        return

    existing_all: list[dict] = load_json(OUTPUT_MATCHES_ALL, [])
    existing_by_id: dict[str, dict] = {
        m["match_id"]: m for m in existing_all if m.get("match_id")
    }

    # Index players who need TennisLink IDs resolved
    players_needing_id = [p for p in players if not p.get("tennislink_id")]
    print(f"  {len(players)} players, {len(players_needing_id)} need tennislink_id lookup")

    # Step 1: Resolve missing tennislink_ids
    for i, p in enumerate(players):
        if p.get("tennislink_id"):
            continue
        print(f"  [id lookup {i+1}/{len(players)}] {p['name']}")
        tid = find_tennislink_id(page, p["name"])
        if tid:
            p["tennislink_id"] = tid
            print(f"    → {tid}")
        sleep(DELAY)

    # Persist updated IDs back to players.json
    save_json(PLAYERS_JSON, players)

    # Step 2: Scrape match history for every player with a tennislink_id
    all_new_matches: list[dict] = []
    players_with_id = [p for p in players if p.get("tennislink_id")]
    print(f"  Scraping match history for {len(players_with_id)} players …")

    for i, p in enumerate(players_with_id):
        print(f"  [{i+1}/{len(players_with_id)}] {p['name']} (id={p['tennislink_id']})")
        matches = scrape_player_leagues(page, p["tennislink_id"], p["name"])
        for m in matches:
            mid = m.get("match_id")
            if mid and mid not in existing_by_id:
                existing_by_id[mid] = m
                all_new_matches.append(m)
            elif mid:
                # Merge tracked_players list
                prev = existing_by_id[mid]
                tracked = list(set(prev.get("tracked_players", []) + m.get("tracked_players", [])))
                prev["tracked_players"] = tracked
        sleep(DELAY * 0.3)

    # Re-check still-pending matches
    pending = [m for m in existing_by_id.values() if m.get("pending")]
    print(f"  Rechecking {len(pending)} pending matches …")
    recheck_pending(pending, page)

    final = list(existing_by_id.values())
    save_json(OUTPUT_MATCHES_ALL, final)
    print(f"  Total matches in output: {len(final)}  (new this run: {len(all_new_matches)})")


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def _safe_int(v: str) -> Optional[int]:
    try:
        return int(v.replace(",", "").strip())
    except Exception:
        return None


def _safe_float(v: str) -> Optional[float]:
    try:
        return float(v.replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def _stable_id(*parts: str) -> str:
    import hashlib
    combined = "|".join(str(p).lower().strip() for p in parts)
    return hashlib.md5(combined.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="TennisLink Playwright scraper")
    parser.add_argument(
        "--mode",
        choices=["1", "2", "all"],
        required=True,
        help="1=standings/results, 2=player history, all=both",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2025,
        help="League year (default: 2025)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run browser in headless mode (default: headed so you can observe/debug)",
    )
    args = parser.parse_args()

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env (see .env.example)")
        sys.exit(1)

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

            if args.mode in ("1", "all"):
                run_mode1(page, args.year)

            if args.mode in ("2", "all"):
                run_mode2(page)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
