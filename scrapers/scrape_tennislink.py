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

DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"
REGIONS_JSON = DATA_DIR / "regions.json"

OUTPUT_MATCHES_ALL = DATA_DIR / "matches_all_players.json"

# Legacy output paths (NV default — kept for backward compat)
OUTPUT_STANDINGS_30 = DATA_DIR / "standings_women_30.json"
OUTPUT_STANDINGS_35 = DATA_DIR / "standings_women_35.json"


def _load_regions() -> dict:
    """Load regions config from data/regions.json."""
    return json.loads(REGIONS_JSON.read_text()) if REGIONS_JSON.exists() else {}


def _get_state_config(state_code: str) -> dict:
    """Return config for a state code (e.g. 'NV', 'CO')."""
    regions = _load_regions()
    cfg = regions.get("states", {}).get(state_code)
    if not cfg:
        raise ValueError(f"No config found for state {state_code!r} in {REGIONS_JSON}")
    cfg["_section"] = regions.get("section", "Intermountain")
    cfg["_state_code"] = state_code
    return cfg


def _output_path(state_code: str, ntrp: str, kind: str = "standings") -> Path:
    """Return the data file path for a state/ntrp combo.
    kind: 'standings' or 'districts'
    """
    sfx = ntrp.replace(".", "")  # "3.0" -> "30"
    return DATA_DIR / f"{kind}_{state_code.lower()}_{sfx}.json"

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
USTA_SEARCH_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"

# Navigation link texts to skip when looking for team links on subflight pages
_NAV_SKIP_EXACT = {
    "A", "B", "Summary", "Team Standings", "Match Summary", "Match Schedule",
    "Player Roster", "Player Counts", "Send To Excel", "Print Report",
    "Link to this Page", "Send to Excel", "> Stats & Standings",
}
# Substrings that indicate breadcrumb/nav links (not team names)
_NAV_SKIP_KEYWORDS = ("USTA ADULT", "LEAGUE 18", "& OVER", "WOMEN-", "MEN-")


_FLIGHT_NAME_RE = re.compile(r"^/?\ ?\d\.\d[WM]\b", re.IGNORECASE)

def _is_nav_link(txt: str) -> bool:
    """Return True if txt looks like a navigation breadcrumb rather than a team name."""
    if txt in _NAV_SKIP_EXACT:
        return True
    upper = txt.upper()
    if any(kw in upper for kw in _NAV_SKIP_KEYWORDS):
        return True
    if _FLIGHT_NAME_RE.match(txt):
        return True
    if txt.startswith("/"):
        return True
    return False


def _wait_for_network(page: Page, timeout: int = 10_000):
    """Wait for ASP.NET postback/AJAX to settle."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeoutError:
        pass
    sleep(0.6)


def _navigate_to_my_team(page: Page, ntrp: str, year: int,
                         usta_number: str = "2019825517") -> bool:
    """Search by USTA# and click the matching Women's Adult team for given ntrp/year."""
    page.goto(USTA_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    sleep(DELAY)
    page.fill("#ctl00_mainContent_txtUSTANum", usta_number)
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


def _extract_champ_standings(page: Page) -> list[dict]:
    """Extract team standings from #tblCPTeamStanding (championship page format).
    Columns: Team ID | Team Name | Matches Played | Games Won* | Points* |
             Team Wins | Team Losses | Indiv Wins | Indiv Losses |
             Sets Won | Sets Lost | Games Won | Games Lost | Game Win%"""
    standings = []
    tbl = page.query_selector("#tblCPTeamStanding")
    if not tbl:
        return standings
    for tr in tbl.query_selector_all("tr"):
        cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
        if len(cells) < 7:
            continue
        # Skip header/disclaimer rows — data rows have a team name in cells[1]
        # and a numeric matches_played in cells[2]
        if not cells[1] or not re.match(r'\d+', cells[2]):
            continue
        standings.append({
            "team_name":      cells[1],
            "team_wins":      _safe_int(cells[5]),
            "matches_played": _safe_int(cells[2]),
            "team_losses":    _safe_int(cells[6]),
            "indiv_wins":     _safe_int(cells[7]) if len(cells) > 7 else None,
            "indiv_losses":   _safe_int(cells[8]) if len(cells) > 8 else None,
            "sets_won":       _safe_int(cells[9]) if len(cells) > 9 else None,
            "sets_lost":      _safe_int(cells[10]) if len(cells) > 10 else None,
            "games_won":      _safe_int(cells[11]) if len(cells) > 11 else None,
            "games_lost":     _safe_int(cells[12]) if len(cells) > 12 else None,
            "games_won_pct":  cells[13] if len(cells) > 13 else None,
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

    # --- Per-court winner detection ---
    # Three detection methods, tried in order:
    # 1. Radio buttons via JS .checked property (NV TennisLink)
    # 2. Radio buttons via HTML checked attribute (NV fallback)
    # 3. Green checkmark images (mark.gif) — used by UT/CO/ID scorecards
    #    imgHomePlayer = home won, imgVisitorPlayer = visitor won
    court_winners: list[str] = []
    try:
        radio_data = page.evaluate("""() => {
            const radios = document.querySelectorAll('input[type="radio"][name="strWinner"]');
            return Array.from(radios).map(r => ({
                value: (r.value || '').toLowerCase(),
                checked: r.checked
            }));
        }""")
    except Exception:
        radio_data = []

    if not any(r.get("checked") for r in radio_data):
        try:
            from bs4 import BeautifulSoup as _BS4
            _html = page.content()
            _soup = _BS4(_html, "html.parser")
            _radio_inputs = _soup.find_all("input", {"type": "radio", "name": "strWinner"})
            radio_data = [
                {
                    "value": (inp.get("value") or "").lower(),
                    "checked": inp.has_attr("checked"),
                }
                for inp in _radio_inputs
            ]
        except Exception:
            radio_data = []

    i = 0
    while i + 1 < len(radio_data):
        pair = radio_data[i: i + 2]
        home_btn = next((r for r in pair if r.get("value") == "home"), None)
        vis_btn  = next((r for r in pair if r.get("value") in ("visitor", "away")), None)
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

    # Fallback: green checkmark images (mark.gif) on UT/CO/ID/NV scorecards
    if not any(court_winners):
        try:
            mark_data = page.evaluate("""() => {
                const marks = document.querySelectorAll('img[src*="mark.gif"]');
                return Array.from(marks).map(img => ({
                    id: img.id || '',
                    visible: img.offsetWidth > 0 && img.offsetHeight > 0
                }));
            }""")
        except Exception:
            mark_data = []
        if mark_data:
            court_indices = [
                int(re.search(r'rptScoreCard_ctl(\d+)', m["id"]).group(1))
                for m in mark_data
                if re.search(r'rptScoreCard_ctl(\d+)', m["id"])
            ]
            if not court_indices:
                mark_data = []
        if mark_data:
            num_courts = max(court_indices) + 1
            court_winners = [""] * num_courts
            for m in mark_data:
                idx_m = re.search(r'rptScoreCard_ctl(\d+)', m["id"])
                if not idx_m:
                    continue
                court_idx = int(idx_m.group(1))
                if court_idx >= len(court_winners):
                    continue
                if "imgHomePlayer" in m["id"]:
                    court_winners[court_idx] = "home"
                elif "imgVisitorPlayer" in m["id"] or "imgVisitPlayer" in m["id"]:
                    court_winners[court_idx] = "away"

    # Fallback: Championship scorecard format — mark.gif in table column
    # position (col 2 = home indicator, col 5 = visitor indicator).
    # Used by NV/UT/CO/ID Championship pages where IDs lack rptScoreCard prefix.
    if not any(court_winners):
        try:
            champ_marks = page.evaluate("""() => {
                const panel = document.getElementById('ctl00_mainContent_Panel1')
                    || document.getElementById('ctl00_mainContent_pnlCPScorecard');
                if (!panel) return [];
                const courts = [];
                for (const tr of panel.querySelectorAll('tr')) {
                    const tds = Array.from(tr.querySelectorAll('td'));
                    if (tds.length < 7) continue;
                    const label = tds[0].innerText.trim();
                    if (!/#\\d+ (Singles|Doubles)/.test(label)) continue;
                    let winner = '';
                    tds.forEach((td, i) => {
                        const imgs = td.querySelectorAll('img');
                        for (const img of imgs) {
                            if (img.src && img.src.includes('mark.gif')) {
                                if (i <= 2) winner = 'home';
                                else if (i >= 4) winner = 'away';
                            }
                        }
                    });
                    courts.push(winner);
                }
                return courts;
            }""")
        except Exception:
            champ_marks = []
        if champ_marks and any(champ_marks):
            court_winners = champ_marks

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

    # Split into line sections by pattern like "1# Singles", "#1 Singles"
    line_sections = re.split(r'\n(?=(?:\d+#|#\d+)\s+(?:Singles|Doubles))', text)
    court_idx = 0
    for section in line_sections:
        lm = re.match(r'^(?:(\d+)#|#(\d+))\s+(Singles|Doubles)', section.strip())
        if not lm:
            continue
        line_num = int(lm.group(1) or lm.group(2))
        line_type = lm.group(3)
        line_label = f"{line_num}# {line_type}"

        # Extract player names and score from section
        section_body = section.strip()
        lines_in_section = [l.strip().replace('\xa0', '').strip() for l in section_body.split('\n') if l.strip().replace('\xa0', '').strip()]

        # Filter out noise tokens
        _noise = {'completed', 'not played', 'default', 'retired',
                  '2:00 pm', '3:00 pm', '4:00 pm', '10:00 am', '11:00 am', '12:00 pm',
                  '12:00 midnight', '12:00 noon',
                  'am', 'pm', 'midnight', 'noon', 'n/a'}

        home_players: list[str] = []
        away_players: list[str] = []
        scores: list[str] = []
        in_away = False

        # Check if header line has player name after tab: "#1 Singles\tElena Bolha"
        header_line = lines_in_section[0] if lines_in_section else ""
        header_parts = header_line.split('\t', 1)
        if len(header_parts) == 2:
            p = header_parts[1].strip().replace('\xa0', '').strip()
            if p and re.search(r'[a-zA-Z]', p) and len(p) > 1 and p.lower() not in _noise:
                home_players.append(p)

        for raw_token in lines_in_section[1:]:  # skip header
            t = raw_token.strip().replace('\xa0', '').strip()
            tl = t.lower()

            if not t:
                continue

            # Expand tab-separated tokens (championship format: "Vs.\tPlayer Name")
            tab_parts = [p.strip() for p in t.split('\t') if p.strip()]
            tokens_to_process = []
            for tp in tab_parts:
                tokens_to_process.append(tp)

            for t in tokens_to_process:
                tl = t.lower().replace('\xa0', '').strip()
                t = t.replace('\xa0', '').strip()

                if not t:
                    continue

                # Time line may have player appended: "2:00 PM", "12:00 Midnight"
                if re.match(r'^\d+:\d+\s*(am|pm|midnight|noon)$', tl, re.I):
                    continue

                if tl in _noise:
                    continue
                if tl == 'vs.':
                    in_away = True
                    continue
                if re.match(r'^[\d]+-[\d]+(\s+[\d]+-[\d]+)*$', t):
                    scores.extend(re.findall(r'\d+-\d+', t))
                    continue
                if tl in ('n/a', 'n/a / n/a', 'not available', 'default'):
                    continue
                if re.match(r'3rd set tie-break', tl):
                    continue
                if re.search(r'[a-zA-Z]', t) and len(t) > 1:
                    (away_players if in_away else home_players).append(t)

        score_str = " ".join(scores)

        # Winner: use radio button result if available. Score-based fallback is
        # unreliable because TennisLink displays scores winner-first (the larger
        # number always comes first regardless of home/away), so score parsing
        # always yields "home". Leave result empty when radio buttons aren't found.
        if court_idx < len(court_winners) and court_winners[court_idx]:
            result = court_winners[court_idx]
        else:
            result = ""

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


# ---------------------------------------------------------------------------
# League search navigation (for states without a USTA pivot number)
# ---------------------------------------------------------------------------

def _navigate_via_league_search(page: Page, section: str, district: str, area: str,
                                ntrp: str, year: int, gender: str = "Female") -> bool:
    """
    Navigate to a team page using the league search form (SearchType=2).
    Flow: set section/district/area dropdowns → search → find matching league → click team.
    Returns True if we land on a team page within the target league.
    """
    page.goto(STANDINGS_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    sleep(DELAY)

    # ASP.NET form with IDs: ddlChampYear, ddlDivisionForTeams, ddlSection,
    # ddlNTRPLevel, ddlGender. Dropdowns may be inside collapsed panels so we
    # always use JS to set values and __doPostBack for server round-trips.

    def _aspnet_set(short_id, value, postback=False):
        """Set a dropdown value via JS. If postback=True, trigger __doPostBack."""
        full = f"ctl00_mainContent_{short_id}"
        page.evaluate(f"""(() => {{
            const el = document.getElementById('{full}');
            if (!el) return;
            el.value = '{value}';
        }})()""")
        if postback:
            name = f"ctl00$mainContent${short_id}"
            page.evaluate(f"__doPostBack('{name}', '')")
            _wait_for_network(page, 8_000)
            sleep(1)

    def _aspnet_set_by_text(short_id, match_text, postback=False):
        """Set a dropdown by matching option text. Returns the matched value."""
        full = f"ctl00_mainContent_{short_id}"
        matched = page.evaluate(f"""(() => {{
            const el = document.getElementById('{full}');
            if (!el) return null;
            const target = '{match_text}'.toLowerCase();
            for (const opt of el.options) {{
                if (opt.text.toLowerCase().includes(target)) {{
                    el.value = opt.value;
                    return opt.value;
                }}
            }}
            return null;
        }})()""")
        if matched is not None and postback:
            name = f"ctl00$mainContent${short_id}"
            page.evaluate(f"__doPostBack('{name}', '')")
            _wait_for_network(page, 8_000)
            sleep(1)
        return matched

    # ASP.NET requires postbacks for EventValidation. We set Division and
    # Section via JS+postback (so the server registers them), then set the
    # remaining filters, and submit.

    # Division = Adult 18&Over (postback)
    _aspnet_set_by_text("ddlDivisionForTeams", "adult 18", postback=True)

    # Section = Intermountain (postback — server registers it in EventValidation)
    _aspnet_set_by_text("ddlSection", "intermountain", postback=True)

    # After Section postback, re-set Division (may have been reset)
    _aspnet_set_by_text("ddlDivisionForTeams", "adult 18")

    # Set remaining filters (no postback needed)
    _aspnet_set("ddlChampYear", str(year))
    _aspnet_set_by_text("ddlNTRPLevel", ntrp)
    _aspnet_set_by_text("ddlGender", "female")

    # Check for district/area dropdowns that appeared after Section postback
    for maybe_id in ["ddlDistrict", "ddlDistrictForTeams"]:
        exists = page.evaluate(
            f"!!document.getElementById('ctl00_mainContent_{maybe_id}')")
        if exists:
            _aspnet_set_by_text(maybe_id, district.lower())
            break

    # Verify
    vals = page.evaluate("""(() => {
        const g = id => {
            const e = document.getElementById('ctl00_mainContent_' + id);
            return e ? e.options[e.selectedIndex]?.text || e.value : 'N/A';
        };
        return g('ddlDivisionForTeams') + ' | ' + g('ddlSection') + ' | ' +
               g('ddlNTRPLevel') + ' | ' + g('ddlGender');
    })()""")
    print(f"    Form values: {vals}")

    # Submit via Find Teams button postback
    page.evaluate("__doPostBack('ctl00$mainContent$btnSearchTeamByName', '')")
    _wait_for_network(page, 15_000)
    sleep(3)

    body_text = page.inner_text("body")[:3000] if page.query_selector("body") else ""
    body_upper = body_text.upper()

    teams_found = re.search(r"(\d+)\s+Teams?\s+found", body_text)
    if teams_found:
        print(f"    Search returned {teams_found.group(1)} teams")

    # Load flight_suffix from regions.json for area-specific matching
    _regions = json.loads(Path("data/regions.json").read_text()) if Path("data/regions.json").exists() else {}
    flight_suffix = None
    _found_suffix_config = False
    for st_cfg in _regions.get("states", {}).values():
        for a_cfg in st_cfg.get("areas", []):
            if a_cfg.get("area", "").upper() == area.upper():
                flight_suffix = a_cfg.get("flight_suffix")
                _found_suffix_config = True
                break
        if _found_suffix_config:
            break

    # Collect all sibling suffixes for the same state (to exclude them for the default area)
    sibling_suffixes = []
    if _found_suffix_config and flight_suffix is None:
        state_prefix = area.split("-")[0] + "-" if "-" in area else ""
        for st_cfg in _regions.get("states", {}).values():
            for a_cfg in st_cfg.get("areas", []):
                a_name = a_cfg.get("area", "").upper()
                if state_prefix and a_name.startswith(state_prefix) and a_cfg.get("flight_suffix"):
                    sibling_suffixes.append(a_cfg["flight_suffix"].upper())

    # Legacy fallback: strip state prefix from area name
    area_match = area.upper()
    for prefix in ("CO-", "UT-", "ID-", "NV-"):
        area_match = area_match.replace(prefix, "")

    # Extract team rows from the results table via JS
    rows = page.evaluate("""(() => {
        const results = [];
        document.querySelectorAll('table tr').forEach(tr => {
            const tds = Array.from(tr.querySelectorAll('td'));
            if (tds.length >= 4) {
                const link = tds[0].querySelector('a');
                if (link && link.innerText.trim().length > 2) {
                    results.push({
                        team: link.innerText.trim(),
                        flight: tds[4] ? tds[4].innerText.trim() : '',
                        href: link.getAttribute('href') || ''
                    });
                }
            }
        });
        return results;
    })()""")

    def _flight_matches_area(flight_upper):
        """Check if a flight string matches our target area."""
        if ntrp not in flight_upper:
            return False
        if _found_suffix_config:
            if flight_suffix:
                return flight_suffix.upper() in flight_upper
            else:
                # Default area (e.g. Denver Metro): match flights WITHOUT any sibling suffix
                return not any(s in flight_upper for s in sibling_suffixes)
        # Legacy fallback: match area name in flight text
        return area_match in flight_upper

    # Also filter by district column if available
    district_upper = district.upper()

    # First pass: team in target area with matching NTRP and district
    for r in rows:
        flight_upper = r.get("flight", "").upper()
        row_district = r.get("district", "").upper()
        if row_district and district_upper not in row_district:
            continue
        if _flight_matches_area(flight_upper):
            print(f"    Found team in target area: {r['team']} ({r['flight']})")
            href = r["href"]
            if href.startswith("javascript:"):
                page.evaluate(href)
            else:
                page.goto(abs_url(href), wait_until="domcontentloaded", timeout=30_000)
            _wait_for_network(page, 12_000)
            sleep(2)
            return True

    # Fallback: any team matching NTRP in district
    for r in rows:
        flight_upper = r.get("flight", "").upper()
        row_district = r.get("district", "").upper()
        if row_district and district_upper not in row_district:
            continue
        if ntrp in flight_upper:
            print(f"    Fallback team: {r['team']} ({r['flight']})")
            href = r["href"]
            if href.startswith("javascript:"):
                page.evaluate(href)
            else:
                page.goto(abs_url(href), wait_until="domcontentloaded", timeout=30_000)
            _wait_for_network(page, 12_000)
            sleep(2)
            return True

    print(f"    [debug] Found {len(rows)} row(s), body preview: {body_text[:300]}")
    print(f"    [warn] no matching {ntrp} Women's league found for {district}/{area}")
    return False


def discover_areas(page: Page, section: str, district: str) -> list[str]:
    """
    Navigate to the league search form and enumerate available Area options
    for the given section/district.
    """
    page.goto(STANDINGS_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    sleep(DELAY)

    # Set Section
    section_sel = page.query_selector("#ctl00_mainContent_ddlSection")
    if section_sel:
        for opt in section_sel.query_selector_all("option"):
            if section.lower() in opt.inner_text().lower():
                page.select_option("#ctl00_mainContent_ddlSection", opt.get_attribute("value"))
                _wait_for_network(page, 8_000)
                break

    # Set District
    dist_sel = page.query_selector("#ctl00_mainContent_ddlDistrict")
    if dist_sel:
        for opt in dist_sel.query_selector_all("option"):
            if district.lower() in opt.inner_text().lower():
                page.select_option("#ctl00_mainContent_ddlDistrict", opt.get_attribute("value"))
                _wait_for_network(page, 8_000)
                break

    # Read Area dropdown options
    areas = []
    area_sel = page.query_selector("#ctl00_mainContent_ddlArea")
    if area_sel:
        for opt in area_sel.query_selector_all("option"):
            val = opt.get_attribute("value") or ""
            txt = opt.inner_text().strip()
            if val and txt and txt.lower() not in ("", "select", "all", "-- select --"):
                areas.append(txt)

    print(f"  Areas for {district}: {areas}")
    return areas


# ---------------------------------------------------------------------------
# Championships/Districts scraper
# ---------------------------------------------------------------------------

CHAMP_SEARCH_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx"


def _dismiss_cookie_banner(page: Page):
    """Remove OneTrust cookie consent banner that blocks clicks."""
    page.evaluate("""() => {
        const b = document.querySelector('#onetrust-consent-sdk');
        if (b) b.remove();
    }""")


def _pw_select_by_label(page: Page, sel_id: str, label: str) -> str:
    """Select a dropdown option using Playwright's native select_option (preserves ViewState)."""
    sel = f"#ctl00_mainContent_{sel_id}"
    try:
        page.select_option(sel, label=label)
        return f"OK:{label}"
    except Exception:
        pass
    # Partial match fallback
    try:
        val = page.evaluate(f"""(() => {{
            const el = document.querySelector('{sel}');
            if (!el) return null;
            const target = '{label.lower()}';
            for (const o of el.options) {{
                if (o.text.toLowerCase().includes(target)) return o.value;
            }}
            return null;
        }})()""")
        if val is not None:
            page.select_option(sel, value=str(val))
            return f"OK:{label}(partial)"
    except Exception:
        pass
    return f"FAIL:{sel_id}"


def _make_champ_form_visible(page: Page):
    """Force the championship search form elements visible so Playwright can interact."""
    page.evaluate("""() => {
        // Force all championship panel elements visible
        const ids = ['ddlCYear','ddlDivision','ddlNTRPlevelChampionlevel',
                     'ddlGenderChampion','ddlClevel','ddlSection',
                     'ddlDistrict','btnSearchMatch'];
        for (const id of ids) {
            const el = document.getElementById('ctl00_mainContent_' + id);
            if (!el) continue;
            // Walk up and force all ancestors visible
            let p = el;
            while (p && p !== document.body) {
                if (p.style) {
                    p.style.display = '';
                    p.style.visibility = 'visible';
                    p.style.height = 'auto';
                    p.style.overflow = 'visible';
                    p.style.opacity = '1';
                    p.style.position = 'static';
                }
                // Remove jQuery UI accordion hidden class
                if (p.classList) p.classList.remove('ui-helper-hidden');
                // Handle aria-hidden
                if (p.getAttribute('aria-hidden') === 'true')
                    p.removeAttribute('aria-hidden');
                p = p.parentElement;
            }
        }
    }""")
    sleep(0.5)


def _js_set_select(page: Page, sel_id: str, match_text: str) -> str:
    """Set a select value via JS text match without triggering change/postback."""
    P = "#ctl00_mainContent_"
    return page.evaluate(f"""(() => {{
        const el = document.querySelector('{P}{sel_id}');
        if (!el) return 'NOT_FOUND';
        const target = '{match_text}'.toLowerCase();
        for (const o of el.options) {{
            if (o.text.toLowerCase().includes(target)) {{
                el.value = o.value;
                return 'OK:' + o.text.trim();
            }}
        }}
        return 'NO_MATCH:' + Array.from(el.options).map(o => o.text.trim()).join('|');
    }})()""")


def _click_tab(page: Page, tab_name: str) -> bool:
    """Click a tab link (e.g. 'Team Standings', 'Match Summary') on the current page."""
    for a in page.query_selector_all("a"):
        try:
            txt = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
            if txt == tab_name and "javascript:__doPostBack" in href:
                print(f"  Clicking '{tab_name}' tab")
                a.click()
                _wait_for_network(page, 12_000)
                sleep(2)
                return True
        except Exception:
            pass
    return False


def _find_champ_advancement_links(page: Page) -> list[tuple[str, str]]:
    """Find all Championship Advancements doPostBack links on the current page.
    Returns list of (link_text, href) tuples, with district-level links first."""
    links = []
    for a in page.query_selector_all("a"):
        try:
            href = a.get_attribute("href") or ""
            if "rptChampAdvancementForTeamSummary" in href and "doPostBack" in href:
                txt = (a.inner_text() or "").strip()
                links.append((txt, href))
        except Exception:
            pass
    links.sort(key=lambda x: (0 if "district" in x[0].lower() else 1, -len(x[0])))
    return links


def _find_champ_advancement_link(page: Page) -> Optional[str]:
    """Look for a Championship Advancements doPostBack link on the current team page.
    Returns the doPostBack href string if found, None otherwise."""
    links = _find_champ_advancement_links(page)
    if links:
        txt, href = links[0]
        print(f"    Found championship advancement link: {txt!r}")
        if len(links) > 1:
            print(f"    (also found: {', '.join(repr(t) for t, _ in links[1:])})")
        return href
    return None


def _click_champ_advancement(page: Page) -> bool:
    """If the current page has a championship advancement link, click it and return True."""
    href = _find_champ_advancement_link(page)
    if not href:
        return False
    m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
    if not m:
        return False
    page.evaluate(f"__doPostBack('{m.group(1)}', '{m.group(2)}')")
    _wait_for_network(page, 15_000)
    sleep(2)
    return True


def _navigate_to_championships(page: Page, state_code: str, ntrp: str,
                                year: int = 2026) -> bool:
    """
    Navigate to district championships results via Championship Advancements.
    Flow: navigate to team pages in the league → find one with a Championship
    Advancements link → click it → land on district championship page.
    """
    cfg = _get_state_config(state_code)

    # Navigate to any team page first
    if not _navigate_to_team_page(page, ntrp, year, cfg):
        print(f"    [warn] could not navigate to team page")
        return False

    # Check the current team page for championship advancement
    if _click_champ_advancement(page):
        return True

    # Go to flight page and iterate through subflights on the page
    if not _go_to_flight_page(page):
        print(f"    [warn] could not navigate to flight page")
        return False

    # Discover subflight links on the flight page
    sf_links_info = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('rptSubFlightsForFlightSummary')
                   && href.includes('__doPostBack');
        }).map(a => ({
            text: a.innerText.trim(),
            href: a.getAttribute('href')
        }));
    }""")
    # Also check for single-letter subflights (NV style)
    if not sf_links_info:
        sf_links_info = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a')).filter(a => {
                const txt = a.innerText.trim();
                const href = a.getAttribute('href') || '';
                return /^[A-E]$/.test(txt) && href.includes('__doPostBack');
            }).map(a => ({
                text: a.innerText.trim(),
                href: a.getAttribute('href')
            }));
        }""")

    print(f"    Found {len(sf_links_info)} subflights on flight page")

    for sf_info in sf_links_info:
        sf_text = sf_info["text"]

        # Navigate to team page → flight page → click this subflight
        if not _navigate_to_team_page(page, ntrp, year, cfg):
            break
        if not _go_to_flight_page(page):
            break

        # Click this subflight
        clicked = False
        for a in page.query_selector_all("a"):
            try:
                txt = a.inner_text().strip()
                href = a.get_attribute("href") or ""
                if txt == sf_text and "doPostBack" in href:
                    print(f"    Clicking subflight {sf_text!r}")
                    a.click()
                    _wait_for_network(page, 12_000)
                    sleep(1)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            continue

        # Find team links — try #TeamSummary first, then scan for team-like links
        tbl = page.query_selector("#TeamSummary")
        team_links = tbl.query_selector_all("a") if tbl else []

        if not team_links:
            # On flight page, teams may be in the subflight expansion, not in #TeamSummary.
            # Look for doPostBack links that look like team names (ALL CAPS, no nav links)
            _nav_texts = {"Home", "LOGOUT", "MY TENNIS", "Team Standings", "Match Summary",
                          "Player Roster", "USTA LEAGUE", "TOURNAMENTS", sf_text}
            team_links = []
            for a in page.query_selector_all("a"):
                try:
                    txt = (a.inner_text() or "").strip()
                    href = a.get_attribute("href") or ""
                    if (txt and "doPostBack" in href and len(txt) > 2
                            and txt not in _nav_texts
                            and "rptTeams" in href):
                        team_links.append(a)
                except Exception:
                    pass

        first_team = None
        for a in team_links[:1]:
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if txt and "doPostBack" in href:
                    first_team = txt
                    print(f"    Clicking first team in subflight {sf_text!r}: {txt!r}")
                    a.click()
                    _wait_for_network(page, 12_000)
                    sleep(2)
                    break
            except Exception:
                pass
        if not first_team:
            continue

        if _click_champ_advancement(page):
            return True

    print(f"    [warn] no team has championship advancement links")
    return False


def _safe_postback(page: Page, event_target: str, event_arg: str = ""):
    """Trigger ASP.NET __doPostBack without strict-mode issues."""
    page.evaluate(f"""
        (function() {{
            document.getElementById('__EVENTTARGET').value = '{event_target}';
            document.getElementById('__EVENTARGUMENT').value = '{event_arg}';
            document.forms[0].submit();
        }})()
    """)


def _navigate_to_champ_listing(page: Page, cfg: dict, ntrp: str, year: int,
                                ntrp_tags: list[str],
                                _matches_ntrp) -> bool:
    """Navigate to the championship listing page via the championship search form.
    This shows ALL championships (Flight A/B/C, Final Rounds, District, etc.)
    rather than just one team's specific championship.

    After finding the listing, clicks into the best championship
    (Final Rounds > District > Flight).
    """
    # Navigate to the Stats & Standings page
    page.goto(f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx",
              wait_until="domcontentloaded", timeout=30_000)
    _wait_for_network(page, 10_000)
    sleep(2)

    # Make championship form visible (it's hidden in an accordion panel)
    _make_champ_form_visible(page)
    sleep(1)

    P = "#ctl00_mainContent_"

    # Set all championship search form values at once (no intermediate postbacks)
    form_vals = {}
    form_vals["ddlCYear"] = _js_set_select(page, "ddlCYear", str(year))
    form_vals["ddlDivision"] = _js_set_select(page, "ddlDivision", "Adult 18")
    form_vals["ddlNTRPlevelChampionlevel"] = _js_set_select(page, "ddlNTRPlevelChampionlevel", ntrp)
    form_vals["ddlGenderChampion"] = _js_set_select(page, "ddlGenderChampion", "Female")
    form_vals["ddlClevel"] = _js_set_select(page, "ddlClevel", "District")
    form_vals["ddlSection"] = _js_set_select(page, "ddlSection", "Intermountain")

    # Trigger Section postback to load District dropdown (cascading)
    section_el = page.query_selector(f"{P}ddlSection")
    if section_el:
        section_el.dispatch_event("change")
        _wait_for_network(page, 10_000)
        sleep(2)
        _make_champ_form_visible(page)

    form_vals["ddlDistrict"] = _js_set_select(page, "ddlDistrict", cfg["district"])

    for k, v in form_vals.items():
        print(f"    {k}: {v}")

    has_critical = any("NOT_FOUND" in form_vals.get(k, "") for k in
                       ["ddlCYear", "ddlDivision", "ddlNTRPlevelChampionlevel",
                        "ddlGenderChampion", "ddlClevel", "ddlSection"])
    if has_critical:
        print(f"  [warn] championship search form: critical fields missing")
        return False

    # Use Playwright's native select_option for proper ASP.NET interaction
    # First trigger Section postback properly using native selection
    try:
        page.select_option(f"{P}ddlSection", label="USTA/INTERMOUNTAIN")
        _wait_for_network(page, 10_000)
        sleep(2)
        _make_champ_form_visible(page)

        # Now set district
        form_vals["ddlDistrict"] = _js_set_select(page, "ddlDistrict", cfg["district"])
        print(f"    ddlDistrict (retry): {form_vals['ddlDistrict']}")

        # Click search via native click
        btn = page.query_selector(f"{P}btnSearchMatch")
        if btn:
            print(f"  Clicking championship search button...")
            btn.click()
            _wait_for_network(page, 15_000)
            sleep(3)
    except Exception as e:
        print(f"  [warn] native form interaction failed: {e}")

    # We should now be on the championship listing page
    body_preview = page.evaluate("document.body.innerText.substring(0, 3000)")
    print(f"  Championship listing: {body_preview[:1200]}")

    # Find all championship links
    champ_entries = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            const txt = a.innerText.trim();
            return href.includes('doPostBack') && txt.length > 10 && txt.length < 200
                && /W\s*3\.\d|flight|final|district|playoff|round|championship/i.test(txt);
        }).map(a => ({
            text: a.innerText.trim(),
            href: a.getAttribute('href').substring(0, 180)
        }));
    }""")
    if champ_entries:
        print(f"  Championship entries: {[e['text'] for e in champ_entries]}")
        # Click best: Final > District > any
        best = None
        for entry in champ_entries:
            txt_upper = entry["text"].upper()
            if "FINAL" in txt_upper:
                best = entry
                break
            if "DISTRICT" in txt_upper:
                best = entry
                break
        if not best:
            best = champ_entries[-1]
        if best:
            print(f"  Clicking: {best['text']!r}")
            m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", best["href"])
            if m:
                _safe_postback(page, m.group(1), m.group(2))
                _wait_for_network(page, 15_000)
                sleep(2)
                return True
    else:
        print(f"  [info] no championship entries found on listing page")

    return False


def _scrape_championship_page(page: Page) -> tuple[list, list] | None:
    """Scrape standings and matches from the current championship reports page.
    Returns (standings, matches_list) or None if no data found."""

    _click_tab(page, "Team Standings")

    champ_table = page.evaluate("""() => {
        const t = document.getElementById('tblCPTeamStanding');
        if (!t) return null;
        const rows = [];
        t.querySelectorAll('tr').forEach(tr => {
            const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.innerText.trim());
            rows.push(cells);
        });
        return rows;
    }""")
    if champ_table:
        print(f"    Standings table ({len(champ_table)} rows):")
        for r in champ_table[:8]:
            print(f"      {r[:8]}")

    standings = _extract_standings_from_page(page)
    if not standings:
        standings = _extract_champ_standings(page)

    if not standings:
        _click_tab(page, "Match Summary")
        card_teams = page.evaluate("""() => {
            const teams = new Set();
            const tbl = document.getElementById('tblCPMatchSummary');
            if (tbl) {
                tbl.querySelectorAll('tr').forEach(tr => {
                    const tds = Array.from(tr.querySelectorAll('td'));
                    if (tds.length < 15) return;
                    const texts = tds.map(td => td.innerText.trim());
                    if (!/^\\d{5,}$/.test(texts[0])) return;
                    const home = (texts[11] || '').replace(/\\n/g, ' ').trim();
                    const away = (texts[13] || '').replace(/\\n/g, ' ').trim();
                    if (home) teams.add(home);
                    if (away) teams.add(away);
                });
            }
            if (teams.size > 0) return Array.from(teams);
            const body = document.body.innerText;
            const matches = body.matchAll(/#(\\d{5,})\\s+\\d{2}\\/\\d{2}\\/\\d{4}/g);
            for (const m of matches) {
                const idx = body.indexOf(m[0]);
                const chunk = body.substring(idx, idx + 200);
                const lines = chunk.split('\\n').map(l => l.trim()).filter(Boolean);
                for (const line of lines.slice(1)) {
                    if (line.length > 2 && line.length < 50
                        && !/^\\d/.test(line) && !/^#/.test(line)
                        && !line.includes('Date:') && !line.includes('Team:')
                        && !line.includes('Opponent:') && !line.includes('Action:')
                        && !line.includes('View Score') && !line.includes('AM')
                        && !line.includes('PM')
                        && !/^\\d{2}\\/\\d{2}/.test(line))
                        teams.add(line);
                }
            }
            return Array.from(teams);
        }""")
        if card_teams:
            print(f"    Bracket format: teams from match cards: {card_teams}")
            standings = [{"team_name": t, "team_wins": 0, "team_losses": 0,
                          "matches_played": 0} for t in card_teams]

    team_names = [s["team_name"] for s in standings]
    print(f"    Found {len(team_names)} teams: {team_names}")

    if not team_names:
        print(f"    [info] no teams found — championship may not have been played yet")
        return None

    all_matches: dict[str, dict] = {}

    _click_tab(page, "Match Summary")

    match_rows = page.evaluate("""() => {
        const tbl = document.getElementById('tblCPMatchSummary');
        if (!tbl) return [];
        const rows = [];
        tbl.querySelectorAll('tr').forEach(tr => {
            const tds = Array.from(tr.querySelectorAll('td'));
            if (tds.length < 15) return;
            const texts = tds.map(td => td.innerText.trim());
            if (!/^\\d{5,}$/.test(texts[0])) return;
            let viewScoreAction = null;
            tds.forEach(td => {
                td.querySelectorAll('a').forEach(a => {
                    const t = a.innerText.trim();
                    const h = a.getAttribute('href') || '';
                    const oc = a.getAttribute('onclick') || '';
                    if (t === 'View Score' || oc.includes('ViewScore')) {
                        viewScoreAction = oc || h;
                    }
                });
            });
            rows.push({
                matchId: texts[0],
                date: texts[9] || texts[2],
                homeTeam: (texts[11] || texts[4]).replace(/\\n/g, ' '),
                awayTeam: (texts[13] || texts[6]).replace(/\\n/g, ' '),
                homeWins: texts[15] || '',
                awayWins: texts[16] || '',
                status: texts[19] || '',
                facility: texts[20] || '',
                viewScoreAction: viewScoreAction
            });
        });
        return rows;
    }""")

    print(f"    Match Summary: {len(match_rows)} matches")
    for mr in match_rows[:3]:
        print(f"      {mr['date']} {mr['homeTeam']} vs {mr['awayTeam']} "
              f"({mr['homeWins']}-{mr['awayWins']})")

    for mr in match_rows:
        tl_id = mr.get("matchId")
        date_str = mr.get("date", "")
        home = mr.get("homeTeam", "")
        away = mr.get("awayTeam", "")
        if not date_str or not home or not away:
            continue

        h_wins = _safe_int(mr.get("homeWins", ""))
        a_wins = _safe_int(mr.get("awayWins", ""))
        score = f"{h_wins}-{a_wins}" if h_wins is not None and a_wins is not None else ""
        status = mr.get("status", "")
        sc_url = f"{SCORECARD_BASE_URL}?matchnum={tl_id}" if tl_id else None

        key = _match_key(date_str, home, away)
        if key not in all_matches:
            all_matches[key] = {
                "match_id": key, "date": date_str,
                "tl_match_id": tl_id, "scorecard_url": sc_url,
                "home_team": home, "away_team": away,
                "team_wins_home": h_wins, "team_wins_away": a_wins,
                "score": score, "status": status,
                "pending": not score, "lines": [],
                "_view_score_action": mr.get("viewScoreAction"),
            }

    n_sc = 0
    matches_needing_lines = [
        (key, m) for key, m in all_matches.items()
        if not m.get("lines") and m.get("_view_score_action")
    ]

    if matches_needing_lines:
        vs_source = page.evaluate("""() => {
            if (typeof ViewScore === 'function') return ViewScore.toString();
            return 'NOT_FOUND';
        }""")
        print(f"    ViewScore function: {vs_source[:80]}")

        for key, match in matches_needing_lines:
            action = match.get("_view_score_action", "")
            tl_id = match.get("tl_match_id", "")
            if not action:
                continue

            exec_action = re.sub(r'^return\s+', '', action.strip())
            try:
                page.evaluate(exec_action)
                _wait_for_network(page, 15_000)
                sleep(1.5)

                lines = _parse_match_detail_page(page)
                if not lines:
                    body = page.query_selector("body")
                    body_text = body.inner_text() if body else ""
                    lines = _parse_scorecard_text(body_text) if body_text else []

                if lines:
                    match["lines"] = lines
                    n_sc += 1

                page.go_back(wait_until="domcontentloaded", timeout=15_000)
                _wait_for_network(page, 10_000)
                sleep(1)
            except Exception as e:
                print(f"      [warn] ViewScore for {tl_id} failed: {e}")
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=10_000)
                    sleep(1)
                except Exception:
                    pass

    if n_sc:
        print(f"    Scraped {n_sc}/{len(matches_needing_lines)} match line details")

    for key, match in all_matches.items():
        match.pop("_view_score_action", None)

    matches_list = sorted(all_matches.values(), key=lambda m: m.get("date", ""))
    return standings, matches_list


def _champ_entry_label(raw_text: str) -> str:
    """Derive a short subflight label from a championship entry/level text.
    Handles both entry link text ('W 3.0 Flight A Playoff 2026...')
    and page title text ('2026 USTA ADULT 18 & OVER - DISTRICT CHAMPIONSHIPS - W 3.0 - FLIGHT A').
    """
    t = raw_text.strip()
    # Extract flight name from dash-separated page titles
    # e.g. "... - W 3.0 - FLIGHT A" → "FLIGHT A"
    parts = [p.strip() for p in t.split(" - ")]
    # Find the most specific part (last meaningful segment)
    for part in reversed(parts):
        pu = part.upper()
        if re.search(r'FLIGHT\s+[A-Z]', pu) or "FINAL" in pu:
            t = part
            break
    # Clean up
    t = re.sub(r'^\d{4}\s*', '', t).strip()
    t = re.sub(r'\s*\d{4}\s*$', '', t).strip()
    t = re.sub(r'^.*?USTA\s+ADULT.*?-\s*', '', t, flags=re.I).strip()
    t = re.sub(r'^.*?DISTRICT\s+CHAMPIONSHIPS?\s*-?\s*', '', t, flags=re.I).strip()
    t = re.sub(r'^[WM]\s*\d\.\d\s*-?\s*', '', t).strip()
    t = re.sub(r'\s*(Playoff|District|Championship)s?\s*$', '', t, flags=re.I).strip()
    if not t:
        return "Championships"
    return f"Championships {t.title()}"


def _get_champ_listing_entries(page: Page, cfg: dict, ntrp: str,
                                year: int = 2026) -> list[dict]:
    """Navigate to the championship search form and return all entries."""
    page.goto(f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx",
              wait_until="domcontentloaded", timeout=30_000)
    _wait_for_network(page, 10_000)
    sleep(2)

    _make_champ_form_visible(page)
    sleep(1)

    P = "#ctl00_mainContent_"
    fv = {}
    fv["year"] = _js_set_select(page, "ddlCYear", str(year))
    fv["div"] = _js_set_select(page, "ddlDivision", "Adult 18")
    fv["ntrp"] = _js_set_select(page, "ddlNTRPlevelChampionlevel", ntrp)
    fv["gender"] = _js_set_select(page, "ddlGenderChampion", "Female")
    fv["level"] = _js_set_select(page, "ddlClevel", "District")
    fv["section"] = _js_set_select(page, "ddlSection", "Intermountain")
    print(f"    Champ form (pre-postback): {fv}")

    try:
        page.select_option(f"{P}ddlSection", label="USTA/INTERMOUNTAIN")
        _wait_for_network(page, 10_000)
        sleep(2)
        _make_champ_form_visible(page)

        fv["district"] = _js_set_select(page, "ddlDistrict", cfg["district"])
        print(f"    Champ form district: {fv['district']}")

        btn = page.query_selector(f"{P}btnSearchMatch")
        if btn:
            print(f"    Clicking championship search button...")
            btn.click()
            _wait_for_network(page, 15_000)
            sleep(3)
        else:
            print(f"    [warn] search button not found")
            return []
    except Exception as e:
        print(f"    [warn] championship search form failed: {e}")
        return []

    body_preview = page.evaluate("document.body.innerText.substring(0, 1500)")
    print(f"    Listing page preview: {body_preview[:500]}")

    entries = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            const txt = a.innerText.trim();
            return href.includes('doPostBack') && txt.length > 10 && txt.length < 200
                && /W\\s*3\\.\\d|flight|final|district|playoff|round|championship/i.test(txt);
        }).map(a => ({
            text: a.innerText.trim(),
            href: a.getAttribute('href').substring(0, 180)
        }));
    }""")
    if entries:
        print(f"    Found {len(entries)} championship entries on listing page")
    else:
        all_links = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a')).filter(a => {
                const href = a.getAttribute('href') || '';
                const txt = a.innerText.trim();
                return href.includes('doPostBack') && txt.length > 10 && txt.length < 200;
            }).map(a => a.innerText.trim()).slice(0, 20);
        }""")
        print(f"    [debug] No matching entries. All postback links: {all_links}")
    return entries or []


def scrape_all_districts(page: Page, state_code: str, ntrp: str, year: int = 2026):
    """Scrape ALL championship levels (flight playoffs + final rounds) for a state.
    All levels are combined into one Championships subflight."""
    cfg = _get_state_config(state_code)
    print(f"\n=== DISTRICTS (all levels): {state_code} {ntrp} Women ===")

    if not cfg.get("has_districts", False):
        print(f"  [skip] {state_code} does not have districts yet")
        return

    entries = _get_champ_listing_entries(page, cfg, ntrp, year)
    if not entries:
        print(f"  [warn] no championship entries found for {cfg['district']}")
        print(f"  Falling back to single-level scraper...")
        scrape_districts(page, state_code, ntrp, year)
        return

    print(f"  Found {len(entries)} championship entries:")
    for e in entries:
        print(f"    - {e['text']}")

    ntrp_short = ntrp.replace(".", "")
    standings_path = _output_path(state_code, ntrp_short)
    if standings_path.exists():
        existing = json.loads(standings_path.read_text())
    else:
        existing = {"ntrp": ntrp, "year": year, "subflights": []}

    existing["subflights"] = [
        sf for sf in existing.get("subflights", [])
        if not sf.get("flight_label", "").startswith("Championships")
    ]

    champ_subflights = []

    for i, entry in enumerate(entries):
        print(f"\n  --- Entry {i+1}/{len(entries)}: {entry['text']!r} ---")

        if i > 0:
            fresh_entries = _get_champ_listing_entries(page, cfg, ntrp, year)
            if not fresh_entries:
                print(f"    [warn] could not re-navigate to championship listing")
                break
            fresh_match = None
            for fe in fresh_entries:
                if fe["text"] == entry["text"]:
                    fresh_match = fe
                    break
            if not fresh_match:
                print(f"    [warn] entry {entry['text']!r} not found on re-navigation")
                continue
            entry = fresh_match

        m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", entry["href"])
        if not m:
            print(f"    [skip] no postback in href")
            continue

        _safe_postback(page, m.group(1), m.group(2))
        _wait_for_network(page, 15_000)
        sleep(2)

        result = _scrape_championship_page(page)
        if not result:
            continue

        standings, matches = result

        # Derive a short label from the entry text
        raw = entry["text"].strip()
        label = _champ_entry_label(raw)

        print(f"    Scraped: {len(standings)} teams, {len(matches)} matches "
              f"from {raw!r} → {label!r}")

        champ_subflights.append({
            "flight_label": label,
            "teams": standings,
            "matches": sorted(matches, key=lambda m_item: m_item.get("date", "")),
        })

    if not champ_subflights:
        print(f"  [warn] no championship data scraped")
        return

    for csf in champ_subflights:
        existing["subflights"].append(csf)

    save_json(standings_path, existing)
    total_teams = sum(len(sf["teams"]) for sf in champ_subflights)
    total_matches = sum(len(sf["matches"]) for sf in champ_subflights)
    print(f"\n  Saved {len(champ_subflights)} championship flights "
          f"({total_teams} teams, {total_matches} matches) to {standings_path}")


def scrape_districts(page: Page, state_code: str, ntrp: str, year: int = 2026):
    """
    Scrape district championship matches for a given state/ntrp.
    Uses Championship Advancements on team pages to navigate to the
    district championship page, then discovers ALL flights (A, B, C, ...)
    and Final Rounds by following team advancement links from Final Rounds.
    Each flight is saved as a separate subflight.
    """
    cfg = _get_state_config(state_code)
    print(f"\n=== DISTRICTS: {state_code} {ntrp} Women ===")

    if not cfg.get("has_districts", False):
        print(f"  [skip] {state_code} does not have districts yet")
        return

    if not _navigate_to_championships(page, state_code, ntrp, year):
        print(f"  [warn] could not navigate to {cfg['district']} championships")
        return

    champ_subflights: list[dict] = []

    def _detect_champ_level():
        return page.evaluate("""() => {
            const rows = document.querySelectorAll('table tr');
            for (const tr of rows) {
                const tds = tr.querySelectorAll('td');
                for (const td of tds) {
                    const t = td.innerText.trim().toUpperCase();
                    if (t.includes('FLIGHT PLAYOFF') || t.includes('FINAL ROUND')
                        || t.includes('DISTRICT CHAMP')) return t;
                }
            }
            const anchor = document.getElementById('ctl00_mainContent_tblCPFlightAnchor');
            if (anchor) {
                const t = anchor.innerText.toUpperCase();
                if (t.includes('FLIGHT') || t.includes('FINAL') || t.includes('DISTRICT'))
                    return t;
            }
            return '';
        }""").upper()

    def _scrape_and_save_level(level_name):
        print(f"  Scraping level: {level_name!r}")
        result = _scrape_championship_page(page)
        if result:
            teams, matches = result
            label = _champ_entry_label(level_name)
            champ_subflights.append({
                "flight_label": label,
                "teams": teams,
                "matches": sorted(matches, key=lambda m: m.get("date", "")),
            })
            print(f"    Got {len(teams)} teams, {len(matches)} matches → {label!r}")
            return teams
        return []

    champ_text = _detect_champ_level()
    is_flight_playoff = ("FLIGHT PLAYOFF" in champ_text
                         and "FINAL" not in champ_text
                         and "DISTRICT CHAMP" not in champ_text)
    print(f"  Championship text: {champ_text[:120]!r} → flight_playoff={is_flight_playoff}")

    if not is_flight_playoff:
        _scrape_and_save_level(champ_text)

    if is_flight_playoff:
        print(f"  [info] on flight playoff page — looking for teams to find "
              f"higher championship levels...")
        team_links_on_page = page.evaluate("""() => {
            const navTexts = new Set(['Home', 'LOGOUT', 'MY TENNIS', 'Team Standings',
                'Match Summary', 'Player Roster', 'Team Standings By Champion',
                'Team Standings By Championship Report',
                'Send to Excel', 'Print Report', 'Link to this Page',
                'View Score', 'USTA LEAGUE', 'TOURNAMENTS',
                'JUNIOR TEAM TENNIS', 'USTA FLEX LEAGUES', 'NET GENERATION',
                'TENNISLINK', 'NATIONAL CAMPUS', 'NATIONAL TENNIS CENTER',
                'PLAYER DEVELOPMENT', 'USTA FOUNDATION', 'USTA COACHING',
                'RED BALL TENNIS', 'VIEW MAP', 'CAREERS', 'INTERNSHIPS',
                'CONTACT US', 'Enable accessibility']);
            return Array.from(document.querySelectorAll('a')).filter(a => {
                const href = a.getAttribute('href') || '';
                const txt = a.innerText.trim();
                const u = txt.toUpperCase();
                return href.includes('__doPostBack') && txt.length > 2
                    && txt.length < 50 && !navTexts.has(txt) && !navTexts.has(u)
                    && !/^\\d/.test(txt) && !/^#/.test(txt)
                    && !u.includes('USTA ADULT') && !u.includes('USTA/')
                    && !u.includes('FLIGHT') && !u.includes('CHAMPIONSHIP')
                    && !txt.includes('http')
                    && !href.includes('lnkSection') && !href.includes('lnkDistrict')
                    && !href.includes('lnkArea') && !href.includes('lnkFlight')
                    && !href.includes('lnkLeague');
            }).map(a => ({
                text: a.innerText.trim(),
                href: a.getAttribute('href')
            }));
        }""")
        seen_teams_set = set()
        unique_teams = []
        for tl in team_links_on_page:
            if tl["text"] not in seen_teams_set:
                seen_teams_set.add(tl["text"])
                unique_teams.append(tl)
        print(f"  Team links found: {[t['text'] for t in unique_teams]}")

        current_champ_text = champ_text
        found_higher = False
        _nav_prefixes = ("USTA/", "COLORADO", "CO-", "UT-", "NV-", "ID-",
                         "UTAH", "NEVADA", "IDAHO", "INTERMOUNTAIN",
                         "WOMEN", "3.0", "3.5")
        for team_info in unique_teams:
            if any(team_info["text"].upper().startswith(p)
                   for p in _nav_prefixes):
                continue
            print(f"  Checking team {team_info['text']!r} for higher advancement...")
            m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)",
                          team_info["href"])
            if not m:
                continue
            page.evaluate(f"__doPostBack('{m.group(1)}', '{m.group(2)}')")
            _wait_for_network(page, 12_000)
            sleep(2)

            adv_links = _find_champ_advancement_links(page)
            adv_texts = [t for t, _ in adv_links]
            print(f"    Advancement links: {adv_texts}")

            for link_text, link_href in adv_links:
                if not link_text.strip():
                    continue
                lt = link_text.upper()
                if lt in current_champ_text or current_champ_text in lt:
                    continue
                if any(lt.startswith(s) for s in
                       ("SOUTH", "NORTH", "CENTRAL", "WEST", "EAST", "SOUTHEAST")):
                    continue
                print(f"  >> Following advancement: {link_text!r}")
                m2 = re.search(r"__doPostBack\('([^']+)','([^']*)'\)",
                               link_href)
                if m2:
                    page.evaluate(
                        f"__doPostBack('{m2.group(1)}', '{m2.group(2)}')")
                    _wait_for_network(page, 15_000)
                    sleep(2)
                    found_higher = True
                break
            if found_higher:
                break
            page.go_back()
            _wait_for_network(page, 10_000)
            sleep(1)

        # Follow chain until Final Rounds, scraping each level as its own subflight
        if found_higher:
            for _depth in range(5):
                new_champ = _detect_champ_level()
                print(f"  Now on: {new_champ[:120]!r}")
                _scrape_and_save_level(new_champ[:120])
                if "FINAL" in new_champ.upper():
                    print(f"  >> Reached Final Rounds!")
                    break
                _click_tab(page, "Team Standings")
                _lvl_standings = _extract_champ_standings(page)
                if not _lvl_standings:
                    print(f"  [warn] no standings at this level")
                    break
                _lvl_standings.sort(key=lambda s: (
                    -s.get("team_wins", 0), s.get("team_losses", 99)))
                _winner = _lvl_standings[0]
                print(f"  Winner: {_winner['team_name']!r} "
                      f"({_winner.get('team_wins', '?')}-"
                      f"{_winner.get('team_losses', '?')})")
                clicked = False
                for a in page.query_selector_all("a"):
                    try:
                        txt = (a.inner_text() or "").strip()
                        href = a.get_attribute("href") or ""
                        if txt == _winner["team_name"] and "doPostBack" in href:
                            a.click()
                            _wait_for_network(page, 12_000)
                            sleep(2)
                            clicked = True
                            break
                    except Exception:
                        pass
                if not clicked:
                    break
                adv = _find_champ_advancement_links(page)
                print(f"  {_winner['team_name']} adv: "
                      f"{[t for t, _ in adv]}")
                advanced = False
                for lt2, lh2 in adv:
                    if "FINAL" in lt2.upper():
                        m3 = re.search(
                            r"__doPostBack\('([^']+)','([^']*)'\)", lh2)
                        if m3:
                            print(f"  >> Advancing to: {lt2!r}")
                            page.evaluate(
                                f"__doPostBack('{m3.group(1)}', "
                                f"'{m3.group(2)}')")
                            _wait_for_network(page, 15_000)
                            sleep(2)
                            advanced = True
                            break
                if not advanced:
                    nu = new_champ.upper()
                    for lt2, lh2 in adv:
                        ltu = lt2.upper().strip()
                        if (not ltu or "FLIGHT PLAYOFF" in ltu
                                or nu[:15] in ltu):
                            continue
                        m3 = re.search(
                            r"__doPostBack\('([^']+)','([^']*)'\)", lh2)
                        if m3:
                            print(f"  >> Advancing to: {lt2!r}")
                            page.evaluate(
                                f"__doPostBack('{m3.group(1)}', "
                                f"'{m3.group(2)}')")
                            _wait_for_network(page, 15_000)
                            sleep(2)
                            advanced = True
                            break
                if not advanced:
                    break

    # Discover sibling flights we didn't traverse.
    # Final Rounds teams come from different flights — click each team,
    # follow their flight advancement link, scrape, then return to Final Rounds.
    final_sf = next((sf for sf in champ_subflights
                     if "Final" in sf["flight_label"]), None)
    scraped_labels = {sf["flight_label"] for sf in champ_subflights}
    if final_sf:
        print(f"\n  Discovering sibling flights from Final Rounds teams...")
        for team in final_sf["teams"]:
            tn = team.get("team_name", "")
            if not tn:
                continue
            # Click team name on the Final Rounds championship page
            clicked = False
            for a in page.query_selector_all("a"):
                try:
                    txt = (a.inner_text() or "").strip()
                    href = a.get_attribute("href") or ""
                    if txt == tn and "doPostBack" in href:
                        a.click()
                        _wait_for_network(page, 12_000)
                        sleep(2)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                continue
            adv_links = _find_champ_advancement_links(page)
            # Find a flight link we haven't scraped yet
            target_link = None
            target_label = None
            final_link = None
            for lt, lh in adv_links:
                lt_clean = lt.strip().lstrip("- ").strip()
                lt_upper = lt_clean.upper()
                if "FINAL" in lt_upper:
                    final_link = lh
                if (not lt_clean or "FLIGHT PLAYOFF" in lt_upper
                        or "FINAL" in lt_upper or "W 3" in lt_upper):
                    continue
                label = _champ_entry_label(lt_clean)
                if label not in scraped_labels:
                    target_link = lh
                    target_label = label
            if not target_link:
                # This team's flight is already scraped — go back to Final Rounds
                if final_link:
                    m_fr = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", final_link)
                    if m_fr:
                        page.evaluate(f"__doPostBack('{m_fr.group(1)}', '{m_fr.group(2)}')")
                        _wait_for_network(page, 12_000)
                        sleep(2)
                        continue
                page.go_back()
                _wait_for_network(page, 10_000)
                sleep(1)
                continue

            # Navigate directly to the new flight from this team page
            print(f"    {tn} → navigating to {target_label!r}")
            m_fl = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", target_link)
            if not m_fl:
                page.go_back()
                _wait_for_network(page, 10_000)
                sleep(1)
                continue
            page.evaluate(f"__doPostBack('{m_fl.group(1)}', '{m_fl.group(2)}')")
            _wait_for_network(page, 15_000)
            sleep(2)

            # Scrape this flight
            result = _scrape_championship_page(page)
            if result:
                teams_found, matches_found = result
                champ_subflights.append({
                    "flight_label": target_label,
                    "teams": teams_found,
                    "matches": sorted(matches_found, key=lambda m_item: m_item.get("date", "")),
                })
                scraped_labels.add(target_label)
                print(f"    Got {len(teams_found)} teams, {len(matches_found)} matches")

            # Return to Final Rounds: click a known Final Rounds team on
            # this flight page, then follow their Final Rounds advancement link
            _returned = False
            final_team_names = {t.get("team_name", "") for t in final_sf["teams"]}
            for _tab_name in ["Match Summary", "Team Standings"]:
                if _returned:
                    break
                _click_tab(page, _tab_name)
                for a in page.query_selector_all("a"):
                    try:
                        txt = (a.inner_text() or "").strip()
                        href = a.get_attribute("href") or ""
                        if txt in final_team_names and "doPostBack" in href:
                            a.click()
                            _wait_for_network(page, 12_000)
                            sleep(1)
                            adv2 = _find_champ_advancement_links(page)
                            for lt2, lh2 in adv2:
                                if "FINAL" in lt2.upper():
                                    m_ret = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", lh2)
                                    if m_ret:
                                        page.evaluate(f"__doPostBack('{m_ret.group(1)}', '{m_ret.group(2)}')")
                                        _wait_for_network(page, 12_000)
                                        sleep(2)
                                        _returned = True
                                    break
                            if _returned:
                                break
                            page.go_back()
                            _wait_for_network(page, 8_000)
                            sleep(1)
                    except Exception:
                        pass
            if not _returned:
                print(f"    [warn] could not return to Final Rounds, stopping sibling discovery")
                break

    # If we haven't scraped anything, try the current page as fallback
    if not champ_subflights:
        result = _scrape_championship_page(page)
        if result:
            teams, matches = result
            champ_subflights.append({
                "flight_label": "Championships",
                "teams": teams,
                "matches": sorted(matches, key=lambda m_item: m_item.get("date", "")),
            })

    if not champ_subflights:
        print(f"  [warn] no championship data found")
        return

    ntrp_short = ntrp.replace(".", "")
    standings_path = _output_path(state_code, ntrp_short)
    if standings_path.exists():
        existing = json.loads(standings_path.read_text())
    else:
        existing = {"ntrp": ntrp, "year": year, "subflights": []}

    existing["subflights"] = [
        sf for sf in existing.get("subflights", [])
        if not sf.get("flight_label", "").startswith("Championships")
    ]

    for csf in champ_subflights:
        existing["subflights"].append(csf)

    save_json(standings_path, existing)
    total_teams = sum(len(sf["teams"]) for sf in champ_subflights)
    total_matches = sum(len(sf["matches"]) for sf in champ_subflights)
    print(f"\n  Saved {len(champ_subflights)} championship flights "
          f"({total_teams} teams, {total_matches} matches) to {standings_path}")


# ---------------------------------------------------------------------------
# Flight page navigation
# ---------------------------------------------------------------------------

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


def _navigate_to_team_page(page: Page, ntrp: str, year: int,
                           state_cfg: dict | None = None) -> bool:
    """Navigate to any team page for the given ntrp/year.
    Uses USTA# pivot if available in state_cfg, else league search form."""
    if state_cfg:
        # Try USTA pivot first
        areas = state_cfg.get("areas", [])
        for area_info in areas:
            pivot = area_info.get("usta_pivot")
            if pivot:
                if _navigate_to_my_team(page, ntrp, year, usta_number=pivot):
                    return True
        # Fall back to league search
        section = state_cfg.get("_section", "Intermountain")
        district = state_cfg["district"]
        area = areas[0]["area"] if areas else ""
        return _navigate_via_league_search(page, section, district, area, ntrp, year)
    return _navigate_to_my_team(page, ntrp, year)


def _discover_subflight_labels(page: Page, ntrp: str, year: int,
                               state_cfg: dict | None = None) -> list[str]:
    """
    Navigate to the flight page for this ntrp/year and return all subflight labels
    (e.g. ["A", "B"]). Labels are the single-letter doPostBack links on the flight page.
    """
    if not _navigate_to_team_page(page, ntrp, year, state_cfg):
        return []
    if not _go_to_flight_page(page):
        return []
    labels = []
    for a in page.query_selector_all("a"):
        try:
            txt = a.inner_text().strip()
            href = a.get_attribute("href") or ""
            if not href or "javascript:__doPostBack" not in href:
                continue
            # Single-letter subflights (NV style: A, B, C)
            if txt in ("A", "B", "C", "D", "E"):
                if txt not in labels:
                    labels.append(txt)
            # Named subflights (CO Denver Metro style: SOUTH I, CENTRAL III, etc.)
            elif "rptSubFlightsForFlightSummary" in href and 1 < len(txt) < 30:
                if txt not in labels:
                    labels.append(txt)
        except Exception:
            pass
    return labels


def _navigate_to_subflight(page: Page, ntrp: str, year: int, label: str,
                           state_cfg: dict | None = None) -> bool:
    """
    Navigate to a specific subflight label (e.g. "A" or "B") and click the first team.
    Flow: team page → Flight page → subflight label link → first team link.
    Returns True if successfully on a team page within that subflight.
    """
    if not _navigate_to_team_page(page, ntrp, year, state_cfg):
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

    # Some states land on subflight Summary tab after clicking a subflight label.
    # Try clicking "Team Standings" tab, then look for team links.
    # Also look for team links in summary view (rptTeamsForSubFlightSummary).
    for a in page.query_selector_all("a"):
        try:
            txt = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
            if txt == "Team Standings" and "javascript:__doPostBack" in href:
                print(f"    Clicking 'Team Standings' tab")
                a.click()
                _wait_for_network(page, 12_000)
                sleep(2)
                break
        except Exception:
            pass

    # Click the first real team link — prefer links in known team repeaters
    def _find_team_link():
        # First: look for links in team standings/summary repeaters (most reliable)
        for a in page.query_selector_all("a"):
            try:
                href = a.get_attribute("href") or ""
                txt = (a.inner_text() or "").strip()
                if not txt or not href or "javascript:__doPostBack" not in href:
                    continue
                if ("rptTeamStandings" in href or "rptTeamsForSubFlight" in href):
                    if not _is_nav_link(txt) and len(txt) > 2:
                        return a, txt
            except Exception:
                pass
        # Fallback: any non-nav link in a visible table
        for tbl in page.query_selector_all("table"):
            if not tbl.is_visible():
                continue
            for a in tbl.query_selector_all("a"):
                try:
                    txt = (a.inner_text() or "").strip()
                    href = a.get_attribute("href") or ""
                    if "javascript:__doPostBack" in href and txt and not _is_nav_link(txt) and len(txt) > 2:
                        return a, txt
                except Exception:
                    pass
        return None, None

    team_link, team_txt = _find_team_link()
    if team_link:
        print(f"    Clicking first team in subflight {label}: {team_txt!r}")
        team_link.click()
        _wait_for_network(page, 12_000)
        sleep(2)
        return True

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

        # Winner — only trust explicit "Winner: Home/Away" text.
        # Do NOT infer from score: TennisLink displays scores winner-first
        # (larger number always first), so score-based inference always
        # yields "home" regardless of who actually won.
        winner_m = re.search(r'Winner[:\s]+(Home|Away)', section, re.I)
        if winner_m:
            entry["result"] = winner_m.group(1).lower()

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


def _compute_player_stats_from_scorecards(all_ntrp_standings: list[tuple]):
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

    # Build name → player(s) lookup. For same-name players in different states,
    # we keep a list so team-based disambiguation can pick the right one.
    # Clear all per-division stats fields before recomputing from scratch.
    # This prevents stale values from persisting when disambiguation logic changes.
    _div_fields = ("lines_played_30", "wl_record_30", "team_30", "default_wins_30",
                   "lines_played_35", "wl_record_35", "team_35", "default_wins_35")
    for p in players:
        for f in _div_fields:
            p.pop(f, None)

    _by_name_list: dict[str, list] = {}
    for p in players:
        k = p["name"].lower().strip()
        _by_name_list.setdefault(k, []).append(p)
    _ambiguous_names = {k for k, ps in _by_name_list.items() if len(ps) > 1}

    def _pick_player(name_key: str, match_teams_norm: set) -> dict | None:
        """Return the player dict for name_key, using team to disambiguate if needed."""
        candidates = _by_name_list.get(name_key, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Multiple same-name players: pick by team match
        for p in candidates:
            t = (p.get("team") or "").upper().strip()
            if t and t in match_teams_norm:
                return p
        return candidates[0]  # fallback

    # For backward compat (swap detection uses player_team by name key, single-value)
    # For ambiguous names we just use the first candidate's team — swap detection
    # operates over all lines so individual errors here are minor.
    by_name: dict[str, dict] = {}
    for k, ps in _by_name_list.items():
        by_name[k] = ps[0]

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
    # Team votes per (ntrp, player): Counter of team_name -> match count. Using a
    # majority vote (not last-write-wins) avoids misattributing a player to a team
    # they played for in just one match (e.g. a single mislabeled scorecard, or a
    # later-processed subflight overwriting an earlier one with far more matches).
    match_teams:  dict[str, dict] = defaultdict(lambda: defaultdict(Counter))

    def _split_players(field: str) -> list[str]:
        cleaned = re.sub(r",?\s*\d+-\d+.*$", "", field).strip()
        names = [n.strip() for n in re.split(r"\s*/\s*", cleaned) if n.strip()]
        # Strip TennisLink DQ annotations e.g. "Name - (DQ)*" or "(DQ)* - note text"
        stripped = []
        for n in names:
            n = re.sub(r"\s*-\s*\(DQ\)\*?.*$", "", n, flags=re.IGNORECASE).strip()
            n = re.sub(r"^\(DQ\)\*?\s*-\s*.*$", "", n, flags=re.IGNORECASE).strip()
            if n:
                stripped.append(n)
        return stripped

    def _team_norm(t: str) -> str:
        return (t or "").upper().strip()

    for entry in all_ntrp_standings:
        # Support both old (ntrp, subflights) and new (ntrp, state_code, subflights) tuples
        if len(entry) == 3:
            ntrp, _entry_state, subflights = entry
        else:
            ntrp, subflights = entry
            _entry_state = ""
        _entry_state = (_entry_state or "").lower()
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
                    # Authoritative per-court winner: court_winner (set by
                    # engine/normalize.py, matches what ratings.py trusts) —
                    # NOT the raw scraped "result" field, which can go stale
                    # after a rescrape corrects court_winner without touching
                    # the original result value, silently corrupting displayed
                    # W-L records (e.g. Carina Lambert showed 4-3 instead of
                    # the correct 5-2 for exactly this reason).
                    court_result = (ln.get("court_winner") or "").lower()  # "home", "away", or ""

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

                        # Determine which side this player is actually on.
                        # Trust scorecard position + swap detection exclusively.
                        #
                        # Previously this used a team-name override (if player's
                        # primary team == match_home → actual_home = True), but that
                        # breaks for cross-division players (e.g. Kristyl Addison is
                        # DESERT PALM in 3.0 but DTC#3 in 3.5 — in a DESERT PALM vs
                        # DTC#3 3.5 match the override incorrectly places her on the
                        # DESERT PALM side). Swap detection operates over all lines in
                        # the match (5 lines × 2 players = up to 10 votes) and is
                        # robust enough without the per-player override.
                        actual_home = (not parsed_is_home) if is_swapped else parsed_is_home

                        # For same-name players in different states, qualify the
                        # accumulator key with state to keep their stats separate.
                        player_team_here = (match_home if actual_home else match_away) if (match_home and match_away) else ""
                        acc_key = f"{_entry_state}::{key}" if (key in _ambiguous_names and _entry_state) else key

                        if court_label:
                            lines_count[ntrp][acc_key][court_label] += 1

                        # Record which team they played for in this division (one
                        # vote per match they appear in; majority wins at the end)
                        if match_home and match_away:
                            match_teams[ntrp][acc_key][player_team_here] += 1

                        # Determine W/L using per-court result if available,
                        # otherwise fall back to match-level result
                        if court_result in ("home", "away"):
                            won = (court_result == "home") if actual_home else (court_result == "away")
                        elif match_home_won or match_away_won:
                            won = match_home_won if actual_home else match_away_won
                        else:
                            return  # no result available; skip

                        if won:
                            wins[ntrp][acc_key] += 1
                            if _line_is_default:
                                defaults_w[ntrp][acc_key] += 1
                        else:
                            losses[ntrp][acc_key] += 1

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
        for acc_key in player_keys:
            # Decode state-qualified key for same-name players (e.g. "ut::tina taylor")
            if "::" in acc_key:
                state_part, name_part = acc_key.split("::", 1)
                candidates = _by_name_list.get(name_part, [])
                p = next((c for c in candidates if (c.get("state") or "").lower() == state_part), None)
                if not p:
                    p = candidates[0] if candidates else None
            else:
                seen_teams = set(match_teams[ntrp_key].get(acc_key, Counter()).keys())
                p = _pick_player(acc_key, seen_teams)
            key = acc_key  # keep for accumulator lookups below
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
            # Store the team they played for in this division (used for roster
            # placement). Majority vote across all matches — not the last team
            # processed — so a single mislabeled/cross-rostered match doesn't
            # override the team a player actually plays for most of the time.
            team_votes = match_teams[ntrp_key].get(key, Counter())
            if team_votes:
                team = team_votes.most_common(1)[0][0]
                if team:
                    p[f"team_{sfx}"] = team
            updated += 1

    save_json(PLAYERS_JSON, players)
    print(f"  [player stats] Updated per-division lines/W-L/team for {updated} player(s)")


def _update_players_from_rosters(rosters_a: dict, rosters_b: dict, ntrp: str,
                                 state_code: str = "NV"):
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
                        p["state"] = state_code
                    elif p.get("team") == team_name:
                        p["state"] = state_code
                    elif not p.get("state"):
                        p["state"] = state_code
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
                        "state": state_code,
                        "pending_tennisrecord_lookup": True,
                    }
                    players.append(stub)
                    existing_by_name[key] = stub
                    added += 1

    save_json(PLAYERS_JSON, players)
    print(f"  [players] {added} new player(s) added from rosters ({state_code})")


# ---------------------------------------------------------------------------
# MODE 1 main
# ---------------------------------------------------------------------------

def _scrape_area_subflights(page: Page, ntrp: str, year: int,
                            state_cfg: dict, area_info: dict,
                            state_code: str) -> list[dict]:
    """Scrape all subflights for a single area within a state. Returns list of subflight dicts."""
    area_name = area_info.get("area", "")
    area_cfg = {**state_cfg, "areas": [area_info]}
    print(f"\n  --- Area: {area_name} ---")

    _skip_nav_for_first = False
    labels = _discover_subflight_labels(page, ntrp, year, area_cfg)
    if not labels:
        # No A/B/C subflight labels — treat entire flight as a single subflight.
        # We're on the flight page after _discover_subflight_labels navigated there.
        # Click the first team link on the flight page and scrape as subflight "A".
        print(f"    No subflight labels for {area_name}; treating flight as single subflight")
        if not _navigate_to_team_page(page, ntrp, year, area_cfg):
            print(f"    [warn] could not navigate to team page for {area_name}")
            return []
        if not _go_to_flight_page(page):
            print(f"    [warn] could not navigate to flight page for {area_name}")
            return []
        # Click Team Standings tab if available (some states land on Summary)
        for a in page.query_selector_all("a"):
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if txt == "Team Standings" and "javascript:__doPostBack" in href:
                    a.click()
                    _wait_for_network(page, 12_000)
                    sleep(2)
                    break
            except Exception:
                pass
        # Click first team — prefer links in team repeaters
        first_team_clicked = False
        for a in page.query_selector_all("a"):
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if (txt and "javascript:__doPostBack" in href
                        and ("rptTeamStandings" in href or "rptTeamsForSubFlight" in href
                             or "rptTeamsForFlight" in href)
                        and not _is_nav_link(txt) and len(txt) > 2):
                    print(f"    Clicking first team in flight: {txt!r}")
                    a.click()
                    _wait_for_network(page, 12_000)
                    sleep(2)
                    first_team_clicked = True
                    break
            except Exception:
                pass
        if not first_team_clicked:
            # Fallback: any non-nav link in a visible table
            for tbl in page.query_selector_all("table"):
                if not tbl.is_visible():
                    continue
                for a in tbl.query_selector_all("a"):
                    try:
                        txt = (a.inner_text() or "").strip()
                        href = a.get_attribute("href") or ""
                        if "javascript:__doPostBack" in href and txt and not _is_nav_link(txt) and len(txt) > 2:
                            print(f"    Clicking first team in flight: {txt!r}")
                            a.click()
                            _wait_for_network(page, 12_000)
                            sleep(2)
                            first_team_clicked = True
                            break
                    except Exception:
                        pass
                if first_team_clicked:
                    break
        if not first_team_clicked:
            print(f"    [warn] no team links on flight page for {area_name}")
            return []
        labels = ["A"]
        _skip_nav_for_first = True
        # Now on a team page — fall through to normal scraping
    print(f"    Subflights: {labels}")

    area_subflights = []
    for i_sf, label in enumerate(labels):
        sf_label_full = f"{area_name} {label}" if len(state_cfg.get("areas", [])) > 1 else label
        if _skip_nav_for_first:
            _skip_nav_for_first = False
            ok = True
            print(f"    Already on team page for single-flight area")
        elif i_sf == 0:
            # First subflight: full navigation (search → team → flight → subflight)
            print(f"    Navigating to subflight {label!r} ...")
            ok = _navigate_to_subflight(page, ntrp, year, label, area_cfg)
        else:
            # Subsequent subflights: go back to flight page from current team page
            print(f"    Navigating to subflight {label!r} (via flight page) ...")
            ok = False
            if _go_to_flight_page(page):
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
                if sf_link:
                    sf_link.click()
                    _wait_for_network(page, 12_000)
                    sleep(2)
                    # Click Team Standings tab if we landed on Summary
                    for a2 in page.query_selector_all("a"):
                        try:
                            t2 = (a2.inner_text() or "").strip()
                            h2 = a2.get_attribute("href") or ""
                            if t2 == "Team Standings" and "javascript:__doPostBack" in h2:
                                a2.click()
                                _wait_for_network(page, 12_000)
                                sleep(2)
                                break
                        except Exception:
                            pass
                    # Click first team — prefer links in team repeaters
                    for a in page.query_selector_all("a"):
                        try:
                            txt = (a.inner_text() or "").strip()
                            href = a.get_attribute("href") or ""
                            if (txt and "javascript:__doPostBack" in href
                                    and ("rptTeamStandings" in href or "rptTeamsForSubFlight" in href)
                                    and not _is_nav_link(txt) and len(txt) > 2):
                                print(f"    Clicking first team in subflight {label}: {txt!r}")
                                a.click()
                                _wait_for_network(page, 12_000)
                                sleep(2)
                                ok = True
                                break
                        except Exception:
                            pass
                        if ok:
                            break
            if not ok:
                # Fallback: full re-navigation
                print(f"    [warn] fast nav failed, falling back to full search")
                ok = _navigate_to_subflight(page, ntrp, year, label, area_cfg)
        if not ok:
            print(f"    [warn] could not navigate to subflight {label!r}")
            continue
        sf_data = _scrape_subflight(page, label)
        if len(state_cfg.get("areas", [])) > 1:
            sf_data["flight_label"] = sf_label_full

        print(f"    Fetching TennisLink match IDs for subflight {label!r} ...")
        tl_id_map = _fetch_tl_match_ids_for_subflight(page, ntrp, year, label)
        n_enriched = 0
        for m in sf_data["matches"]:
            key = m["match_id"]
            if key in tl_id_map and not m.get("tl_match_id"):
                tl_id = tl_id_map[key]
                m["tl_match_id"] = tl_id
                m["scorecard_url"] = f"{SCORECARD_BASE_URL}?matchnum={tl_id}"
                n_enriched += 1
        print(f"      {n_enriched}/{len(sf_data['matches'])} matches enriched with TL IDs")

        # After TL ID fetch, page may be on subflight Match Summary tab.
        # Navigate back to a team page so next iteration can use _go_to_flight_page.
        if i_sf < len(labels) - 1:
            _nav_back = False
            for tab_txt in ("Team Standings",):
                for a in page.query_selector_all("a"):
                    try:
                        if tab_txt in (a.inner_text() or ""):
                            a.click()
                            _wait_for_network(page, 10_000)
                            sleep(1)
                            _nav_back = True
                            break
                    except Exception:
                        pass
                if _nav_back:
                    break
            if _nav_back:
                _found_team = False
                for a in page.query_selector_all("a"):
                    try:
                        txt = (a.inner_text() or "").strip()
                        href = a.get_attribute("href") or ""
                        if (txt and len(txt) > 2
                                and "javascript:__doPostBack" in href
                                and ("rptTeamStandings" in href or "rptTeamsForSubFlight" in href)
                                and not _is_nav_link(txt)):
                            a.click()
                            _wait_for_network(page, 10_000)
                            sleep(1)
                            _found_team = True
                            break
                    except Exception:
                        pass
                if not _found_team:
                    for tbl in page.query_selector_all("table"):
                        if not tbl.is_visible():
                            continue
                        for a in tbl.query_selector_all("a"):
                            try:
                                txt = (a.inner_text() or "").strip()
                                href = a.get_attribute("href") or ""
                                if "javascript:__doPostBack" in href and txt and len(txt) > 2 and not _is_nav_link(txt):
                                    a.click()
                                    _wait_for_network(page, 10_000)
                                    sleep(1)
                                    _found_team = True
                                    break
                            except Exception:
                                pass
                        if _found_team:
                            break

        sf_rosters = sf_data.get("rosters", {})
        is_b = label != labels[0]
        _update_players_from_rosters(
            {} if is_b else sf_rosters,
            sf_rosters if is_b else {},
            ntrp,
            state_code=state_code,
        )

        area_subflights.append(sf_data)

    return area_subflights


def run_mode1(page: Page, year: int, state_code: str = "NV"):
    """Scrape standings + match results for a given state."""
    state_cfg = _get_state_config(state_code)
    print(f"\n=== MODE 1: Standings + match results ({state_code} – {state_cfg['label']}) ===")

    areas = state_cfg.get("areas", [])
    if not areas:
        areas = [{"area": ""}]

    for ntrp in ["3.0", "3.5"]:
        out_path = _output_path(state_code, ntrp)
        print(f"\n--- {state_code} {ntrp} Women ---")

        all_subflights = []

        if len(areas) <= 1:
            all_subflights = _scrape_area_subflights(
                page, ntrp, year, state_cfg, areas[0] if areas else {}, state_code)
        else:
            for area_info in areas:
                area_sfs = _scrape_area_subflights(
                    page, ntrp, year, state_cfg, area_info, state_code)
                all_subflights.extend(area_sfs)

        if not all_subflights:
            print(f"  [warn] no subflights scraped for {ntrp} Women")
            continue

        total_matches = sum(len(sf["matches"]) for sf in all_subflights)
        result = {
            "ntrp": ntrp,
            "year": year,
            "state": state_code,
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
        choices=["1", "2", "all", "districts", "discover-areas"],
        required=True,
        help="1=standings/results, 2=player history, all=both, "
             "districts=championship results, discover-areas=list area options",
    )
    parser.add_argument(
        "--state",
        default="NV",
        help="State code (NV, CO, UT, ID). Default: NV",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2026,
        help="League year (default: 2026)",
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

    state_code = args.state.upper()

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

            if args.mode == "discover-areas":
                cfg = _get_state_config(state_code)
                areas = discover_areas(page, cfg["_section"], cfg["district"])
                print(f"\nAreas for {state_code} ({cfg['district']}): {areas}")
                # Update regions.json with discovered areas
                regions = _load_regions()
                state_obj = regions["states"][state_code]
                state_obj["areas"] = [{"area": a} for a in areas]
                save_json(REGIONS_JSON, regions)

            if args.mode in ("1", "all"):
                run_mode1(page, args.year, state_code)

            if args.mode in ("2", "all"):
                run_mode2(page)

            if args.mode in ("districts", "all"):
                for ntrp in ["3.0"]:
                    scrape_all_districts(page, state_code, ntrp, args.year)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
