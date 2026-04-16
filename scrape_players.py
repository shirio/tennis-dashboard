#!/usr/bin/env python3
"""
Scrape tennisrecord.com to bootstrap the full player universe
for women's USTA Adult 18+ divisions in NV Area F (Intermountain).

Crawl path:
  Entry page -> subflight links -> team roster pages -> player profile pages

Output: data/players.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://www.tennisrecord.com"
ENTRY_URL = (
    "https://www.tennisrecord.com/adult/league/leaguefind.aspx"
    "?year={year}&lt=1&sectionname=Intermountain"
    "&districtname=Nevada&areaname=Area&gender=F"
)
CACHE_DIR = Path("data/.cache")
OUTPUT_PATH = Path("data/players.json")
DEFAULT_DELAY = 1.5  # seconds between requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """anna-clark from 'Anna Clark'."""
    s = name.lower().strip()
    s = re.sub(r"[''`]", "", s)          # strip apostrophes
    s = re.sub(r"[^a-z0-9]+", "-", s)    # non-alphanum -> hyphen
    s = s.strip("-")
    return s


def cache_key_for_url(url: str) -> str:
    """Filesystem-safe cache filename from URL."""
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    # Also add a readable prefix from the path
    parsed = urlparse(url)
    prefix = parsed.path.split("/")[-1].split(".")[0][:30]
    return f"{prefix}_{h}.html"


def fetch(url: str, session: requests.Session, *,
          delay: float, use_cache: bool, cache_only: bool) -> str | None:
    """GET a URL with caching and rate-limiting. Returns HTML or None."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / cache_key_for_url(url)

    # Try cache first
    if use_cache and cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    if cache_only:
        print(f"  [cache-only] Not cached, skipping: {url}")
        return None

    # Fetch
    for attempt in range(3):
        try:
            time.sleep(delay)
            resp = session.get(url, timeout=30)
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"  [429] Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            html = resp.text
            # Cache it
            cache_path.write_text(html, encoding="utf-8")
            return html
        except requests.RequestException as e:
            print(f"  [error] Attempt {attempt+1}/3 failed for {url}: {e}")
            if attempt < 2:
                time.sleep(5)
    return None


def extract_s_param(href: str) -> str | None:
    """Extract the s= query parameter from a URL."""
    qs = parse_qs(urlparse(href).query)
    vals = qs.get("s", [])
    return vals[0] if vals else None


# ---------------------------------------------------------------------------
# Level 1: Parse league finder -> list of (division_name, url)
# ---------------------------------------------------------------------------

def parse_league_finder(html: str) -> list[dict]:
    """Parse the entry page to find subflight links.

    Returns list of dicts: {division, url, flight_name, subflight}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # The league data table is the one whose header row contains "Flight"
    for table in soup.find_all("table", class_="responsive14"):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue

            link = tr.find("a", class_="link")
            if not link:
                continue
            href = link.get("href", "")
            if "league.aspx" not in href:
                continue
            # Skip breadcrumb-style links (they point to leaguetype, leaguesection, etc.)
            if any(x in href for x in ["leaguetype", "leaguesection", "leaguedistrict",
                                        "leaguearea", "leaguegender", "leaguefind",
                                        "league/index"]):
                continue

            flight_text = tds[1].get_text(strip=True) if len(tds) > 1 else ""
            subflight_text = tds[2].get_text(strip=True) if len(tds) > 2 else ""

            # Parse NTRP level from flight name
            m = re.search(r"(\d\.\d)\s*WOMEN", flight_text, re.IGNORECASE)
            if not m:
                continue
            ntrp = m.group(1)

            # Build compact division name
            if subflight_text and subflight_text != "--------":
                division = f"{ntrp} Women {subflight_text}"
            else:
                division = f"{ntrp} Women"

            full_url = urljoin(BASE_URL, href)
            results.append({
                "division": division,
                "url": full_url,
                "flight_name": flight_text,
                "subflight": subflight_text,
            })

    return results


# ---------------------------------------------------------------------------
# Level 2: Parse team listing -> list of teams
# ---------------------------------------------------------------------------

def parse_team_listing(html: str) -> list[dict]:
    """Parse a subflight page to find team links.

    Returns list of dicts: {name, url, court_rating}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Find the table with team links
    for table in soup.find_all("table", class_="responsive14"):
        links = table.find_all("a", class_="link",
                               href=lambda h: h and "teamprofile.aspx" in h)
        if not links:
            continue

        for tr in table.find_all("tr"):
            link = tr.find("a", class_="link",
                           href=lambda h: h and "teamprofile.aspx" in h)
            if not link:
                continue

            team_name = link.get_text(strip=True)
            team_url = urljoin(BASE_URL, link["href"])

            # Court rating is typically the last td with a decimal number
            court_rating = None
            for td in reversed(tr.find_all("td")):
                txt = td.get_text(strip=True)
                if re.match(r"^\d\.\d+$", txt):
                    court_rating = float(txt)
                    break

            results.append({
                "name": team_name,
                "url": team_url,
                "court_rating": court_rating,
            })
        break  # only process the first table that has team links

    return results


# ---------------------------------------------------------------------------
# Level 3: Parse team roster -> list of player stubs
# ---------------------------------------------------------------------------

def parse_team_roster(html: str) -> list[dict]:
    """Parse a team profile page to find player links and basic info.

    Returns list of dicts: {name, url, tennisrecord_id, ntrp, current_rating}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Find the table containing profile.aspx links (the roster table)
    for table in soup.find_all("table", class_="responsive14"):
        links = table.find_all("a", class_="link",
                               href=lambda h: h and "profile.aspx" in h)
        if not links:
            continue

        for tr in table.find_all("tr"):
            link = tr.find("a", class_="link",
                           href=lambda h: h and "profile.aspx" in h)
            if not link:
                continue

            name = link.get_text(strip=True)
            href = link["href"]
            player_url = urljoin(BASE_URL, href)
            tr_id = extract_s_param(href)

            tds = tr.find_all("td")

            # NTRP is typically in the 3rd td (after name and location)
            ntrp = None
            for td in tds[1:]:
                txt = td.get_text(strip=True)
                if re.match(r"^\d\.\d$", txt):
                    ntrp = txt
                    break

            # Current rating: look for a decimal with 2+ decimal places
            # in the rightmost tds
            current_rating = None
            for td in reversed(tds):
                txt = td.get_text(strip=True)
                if re.match(r"^\d\.\d{2,}$", txt):
                    current_rating = float(txt)
                    break

            results.append({
                "name": name,
                "url": player_url,
                "tennisrecord_id": tr_id,  # s= param; None for most players
                "profile_url": link["href"],  # raw href for re-fetching
                "ntrp": ntrp,
                "current_rating": current_rating,
            })
        break  # only the roster table

    return results


# ---------------------------------------------------------------------------
# Level 4: Parse player profile -> enrichment data
# ---------------------------------------------------------------------------

def parse_player_profile(html: str) -> dict:
    """Parse a player profile page for dynamic rating and NTRP letter.

    Returns dict: {ntrp_full, dynamic_rating}
    """
    soup = BeautifulSoup(html, "html.parser")
    result = {"ntrp_full": None, "dynamic_rating": None}

    # Find the main profile table
    tables = soup.find_all("table", class_="responsive14")

    for table in tables:
        rows = table.find_all("tr")
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue

            left_text = tds[0].get_text(strip=True)

            # Look for NTRP rating with letter in the header row
            # It's in a bold span in the right column of the first row
            right_bold = tds[-1].find("span", style=lambda s: s and "bold" in s)
            if right_bold:
                bold_text = right_bold.get_text(strip=True)

                # NTRP + letter pattern (e.g., "3.0 C", "3.5 S")
                m = re.match(r"^(\d\.\d)\s*([A-Z])$", bold_text)
                if m and result["ntrp_full"] is None:
                    result["ntrp_full"] = f"{m.group(1)} {m.group(2)}"
                    continue

                # Dynamic rating pattern (e.g., "3.0991")
                if "dynamic" in left_text.lower() or "estimated" in left_text.lower():
                    m2 = re.match(r"^(\d\.\d+)$", bold_text)
                    if m2:
                        result["dynamic_rating"] = float(m2.group(1))
                        continue

    return result


# ---------------------------------------------------------------------------
# Level 5: Parse per-player match history for line data
# ---------------------------------------------------------------------------

def parse_match_history(html: str, ntrp_filter: str = "") -> dict:
    """
    Parse a tennisrecord.com /adult/matchhistory.aspx page.

    Returns:
      {
        "ntrp_full": "3.0 C" | None,
        "lines": [
          {
            "date": "04/01/2026",
            "court": "D2",          # S1/S2/D1/D2/D3
            "team": "DTC #3",
            "partner": "Maria De Lourdes Herrera",   # blank for singles
            "opponents": ["Kristina Runion", "Corrine Pearson"],
            "wl": "W",              # "W" or "L"
            "score": "6-0 6-2",
            "league": "Adult 18+",
            "ntrp": "2.5",
          },
          ...
        ]
      }
    """
    from bs4 import BeautifulSoup
    import re as _re

    soup = BeautifulSoup(html, "html.parser")
    result = {"ntrp_full": None, "lines": []}

    # ── NTRP letter from page header ────────────────────────────────────────
    for span in soup.find_all("span"):
        txt = span.get_text(strip=True)
        m = _re.match(r"^(\d\.\d)\s+([A-Z])$", txt)
        if m:
            result["ntrp_full"] = f"{m.group(1)} {m.group(2)}"
            break

    # ── Match history table ─────────────────────────────────────────────────
    # Headers: Match Date | League | Team | Court | Partner | Opponent(s) | W/L | Result | Match | Rating
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        header = [th.get_text(" ", strip=True).lower()
                  for th in rows[0].find_all(["th", "td"])]
        if "match date" not in header or "court" not in header:
            continue

        col = {h: i for i, h in enumerate(header)}
        date_col    = col.get("match date", 0)
        league_col  = col.get("league", 1)
        team_col    = col.get("team", 2)
        court_col   = col.get("court", 3)
        partner_col = col.get("partner", 4)
        opp_col     = col.get("opponent(s)", 5)
        wl_col      = col.get("w/l", 6)
        result_col  = col.get("result", 7)

        for tr in rows[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 7:
                continue
            date    = cells[date_col] if date_col < len(cells) else ""
            league  = cells[league_col] if league_col < len(cells) else ""
            team    = cells[team_col] if team_col < len(cells) else ""
            court   = cells[court_col] if court_col < len(cells) else ""
            partner = cells[partner_col] if partner_col < len(cells) else ""
            opp_raw = cells[opp_col] if opp_col < len(cells) else ""
            wl      = cells[wl_col] if wl_col < len(cells) else ""
            score   = cells[result_col] if result_col < len(cells) else ""

            # Only keep 18+ Adult league rows for our target NTRP
            if "adult 18+" not in league.lower():
                continue
            # Extract NTRP from league string e.g. "Adult 18+ 3.0"
            ntrp_m = _re.search(r"(\d\.\d)", league)
            ntrp = ntrp_m.group(1) if ntrp_m else ""
            if ntrp_filter and ntrp != ntrp_filter:
                continue

            # Strip rating suffixes from player names: "Maria Jones (2.50)" → "Maria Jones"
            def clean_name(s: str) -> str:
                return _re.sub(r"\s*\(\s*[-\d.]+\s*\)\s*", "", s).strip()

            # Team name: "DTC #3 Intermountain" → "DTC #3"
            team_clean = _re.sub(r"\s+Intermountain.*$", "", team, flags=_re.I).strip()

            partner_clean = clean_name(partner) if partner else ""

            # Opponents: one name for singles, two for doubles (space-separated in cell)
            # They appear as "FirstLast (rating) FirstLast2 (rating2)" for doubles
            opp_names = []
            for opp_part in _re.split(r"\s{2,}", opp_raw):
                c = clean_name(opp_part)
                if c and c != "-----":
                    opp_names.append(c)

            if not date or not court:
                continue

            result["lines"].append({
                "date": date,
                "court": court,
                "team": team_clean,
                "partner": partner_clean,
                "opponents": opp_names,
                "wl": wl.strip(),
                "score": score.strip(),
                "league": league.strip(),
                "ntrp": ntrp,
            })
        break  # only process first matching table

    return result


COURT_ORDER = {"S1": 1, "S2": 2, "S3": 3, "D1": 4, "D2": 5, "D3": 6}


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def load_existing(path: Path) -> list[dict]:
    """Load existing players.json if it exists."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[warn] Could not parse existing {path}: {e}")
    return []


def merge_players(existing: list[dict], new_players: list[dict]) -> list[dict]:
    """Merge new scraped players into existing list.

    Match by name (case-insensitive). Update scraped fields, preserve manual fields.
    """
    # Index existing by lowercase name
    by_name: dict[str, dict] = {}
    for p in existing:
        key = p.get("name", "").strip().lower()
        if key:
            by_name[key] = p

    merged = []
    seen_names = set()

    for np in new_players:
        key = np["name"].strip().lower()
        seen_names.add(key)

        if key in by_name:
            # Update existing record with scraped fields
            ep = by_name[key]
            ep["id"] = np.get("id", ep.get("id"))
            ep["tennisrecord_id"] = np.get("tennisrecord_id") or ep.get("tennisrecord_id")
            ep["profile_url"] = np.get("profile_url", ep.get("profile_url"))
            ep["team"] = np.get("team", ep.get("team"))
            ep["division"] = np.get("division", ep.get("division"))
            if np.get("dynamic_rating_baseline") is not None:
                ep["dynamic_rating_baseline"] = np["dynamic_rating_baseline"]
            if np.get("ntrp_rating") is not None:
                ep["ntrp_rating"] = np["ntrp_rating"]
            merged.append(ep)
        else:
            merged.append(np)

    # Keep existing players not found in scrape
    for p in existing:
        key = p.get("name", "").strip().lower()
        if key and key not in seen_names:
            merged.append(p)

    return merged


def sort_players(players: list[dict]) -> list[dict]:
    """Sort by division (2.5, 3.0, 3.5, 4.0) then by dynamic rating desc."""
    def sort_key(p):
        div = p.get("division", "")
        # Extract numeric NTRP from division string
        m = re.match(r"(\d\.\d)", div)
        ntrp_num = float(m.group(1)) if m else 9.0
        rating = p.get("dynamic_rating_baseline") or 0
        return (ntrp_num, -rating, p.get("name", ""))
    return sorted(players, key=sort_key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape tennisrecord.com player data for NV Area F women's divisions."
    )
    parser.add_argument("--profiles", action="store_true",
                        help="Also scrape individual player profile pages (slower)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore cached HTML, fetch everything fresh")
    parser.add_argument("--cache-only", action="store_true",
                        help="Only use cached pages, don't make network requests")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Seconds between requests (default: {DEFAULT_DELAY})")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH,
                        help=f"Output path (default: {OUTPUT_PATH})")
    parser.add_argument("--year", type=int, default=2026,
                        help="Season year (default: 2026)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and display results without writing output")
    args = parser.parse_args()

    use_cache = not args.no_cache
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "TennisDashboard/1.0 (player-data-bootstrap)"
    )

    all_players: list[dict] = []

    # -----------------------------------------------------------------------
    # Level 1: Fetch entry page -> subflight list
    # -----------------------------------------------------------------------
    entry_url = ENTRY_URL.format(year=args.year)
    print(f"[1/4] Fetching league finder: {entry_url}")
    html = fetch(entry_url, session, delay=args.delay,
                 use_cache=use_cache, cache_only=args.cache_only)
    if not html:
        print("[fatal] Could not fetch entry page.")
        sys.exit(1)

    subflights = parse_league_finder(html)
    print(f"       Found {len(subflights)} subflights:")
    for sf in subflights:
        print(f"         {sf['division']} -> {sf['url']}")

    # -----------------------------------------------------------------------
    # Level 2: For each subflight, fetch team listing
    # -----------------------------------------------------------------------
    print(f"\n[2/4] Scraping team listings...")
    division_teams: list[tuple[dict, list[dict]]] = []  # (subflight, teams)

    for i, sf in enumerate(subflights, 1):
        print(f"  [{i}/{len(subflights)}] {sf['division']}...")
        html = fetch(sf["url"], session, delay=args.delay,
                     use_cache=use_cache, cache_only=args.cache_only)
        if not html:
            print(f"    [skip] Could not fetch subflight page")
            continue

        teams = parse_team_listing(html)
        print(f"    Found {len(teams)} teams")
        division_teams.append((sf, teams))

    total_teams = sum(len(t) for _, t in division_teams)
    print(f"       Total: {total_teams} teams across {len(division_teams)} divisions")

    # -----------------------------------------------------------------------
    # Level 3: For each team, fetch roster
    # -----------------------------------------------------------------------
    print(f"\n[3/4] Scraping team rosters...")
    team_idx = 0

    for sf, teams in division_teams:
        for team in teams:
            team_idx += 1
            print(f"  [{team_idx}/{total_teams}] {sf['division']} / {team['name']}...",
                  end="", flush=True)
            html = fetch(team["url"], session, delay=args.delay,
                         use_cache=use_cache, cache_only=args.cache_only)
            if not html:
                print(" [skip]")
                continue

            players = parse_team_roster(html)
            print(f" {len(players)} players")

            for p in players:
                all_players.append({
                    "id": slugify(p["name"]),
                    "name": p["name"],
                    "tennisrecord_id": p["tennisrecord_id"],
                    "profile_url": p["profile_url"],
                    "team": team["name"],
                    "division": sf["division"],
                    "dynamic_rating_baseline": p["current_rating"],
                    "current_division_rating": None,
                    "global_rating": None,
                    "ntrp_rating": p["ntrp"],
                    "wl_record": None,
                    "lines_played": None,
                    "lines_html": None,
                    "notes": None,
                })

    print(f"       Total: {len(all_players)} player entries (before dedup)")

    # -----------------------------------------------------------------------
    # Level 4 (optional): Enrich with profile data
    # -----------------------------------------------------------------------
    if args.profiles:
        print(f"\n[4/4] Scraping player profiles...")
        # Deduplicate by tennisrecord_id before fetching profiles
        seen_ids = set()
        unique_players = []
        for p in all_players:
            pid = p["tennisrecord_id"]
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                unique_players.append(p)
            elif not pid:
                unique_players.append(p)

        for i, p in enumerate(unique_players, 1):
            if not p["tennisrecord_id"]:
                continue
            profile_url = (f"{BASE_URL}/adult/profile.aspx"
                           f"?playername={p['name']}&s={p['tennisrecord_id']}")
            print(f"  [{i}/{len(unique_players)}] {p['name']}...", end="", flush=True)
            html = fetch(profile_url, session, delay=args.delay,
                         use_cache=use_cache, cache_only=args.cache_only)
            if not html:
                print(" [skip]")
                continue

            profile = parse_player_profile(html)
            if profile["dynamic_rating"]:
                p["dynamic_rating_baseline"] = profile["dynamic_rating"]
            if profile["ntrp_full"]:
                p["ntrp_rating"] = profile["ntrp_full"]
            print(f" ntrp={profile['ntrp_full']} dyn={profile['dynamic_rating']}")

        # Apply profile data back to all_players (multiple entries for same player)
        profile_data = {p["tennisrecord_id"]: p for p in unique_players if p["tennisrecord_id"]}
        for p in all_players:
            pid = p["tennisrecord_id"]
            if pid and pid in profile_data:
                src = profile_data[pid]
                p["dynamic_rating_baseline"] = src["dynamic_rating_baseline"]
                p["ntrp_rating"] = src["ntrp_rating"]
    else:
        print(f"\n[4/4] Skipping profile scrape (use --profiles to enable)")

    # -----------------------------------------------------------------------
    # Dedup: keep one entry per player name (first occurrence wins for team)
    # -----------------------------------------------------------------------
    seen_names: set[str] = set()
    deduped: list[dict] = []
    for p in all_players:
        key = p["name"].strip().lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        deduped.append(p)

    print(f"\n       After dedup: {len(deduped)} unique players")

    # -----------------------------------------------------------------------
    # Merge with existing file
    # -----------------------------------------------------------------------
    existing = load_existing(args.output)
    if existing:
        print(f"       Merging with {len(existing)} existing entries in {args.output}")
        deduped = merge_players(existing, deduped)
        print(f"       After merge: {len(deduped)} players")

    # Sort
    final = sort_players(deduped)

    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------
    if args.dry_run:
        print(f"\n[dry-run] Would write {len(final)} players to {args.output}")
        for p in final[:10]:
            print(f"  {p['division']:15s} | {p['name']:25s} | "
                  f"dyn={p['dynamic_rating_baseline']} | ntrp={p['ntrp_rating']} | "
                  f"team={p['team']}")
        if len(final) > 10:
            print(f"  ... and {len(final) - 10} more")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(final, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )
    print(f"\nWrote {len(final)} players to {args.output}")

    # Summary by division
    divs: dict[str, int] = {}
    for p in final:
        d = p.get("division", "unknown")
        divs[d] = divs.get(d, 0) + 1
    print("\nBreakdown:")
    for d in sorted(divs):
        print(f"  {d}: {divs[d]} players")


if __name__ == "__main__":
    main()
