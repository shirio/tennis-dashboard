"""
scrapers/scrape_tennisrecord.py

Fetch NTRP ratings and estimated dynamic ratings from tennisrecord.com and
update data/players.json in place.

Sources:
  Ratings page: https://www.tennisrecord.com/adult/ratings.aspx?...
    - "Current NTRP" column  -> ntrp_rating (e.g. "3.0 C", "3.5 S")
    - "2026 Estimated Dynamic" column -> dynamic_rating_baseline (e.g. 2.98)
  Profile page: /adult/profile.aspx?playername=NAME&s=ID
    - Used to disambiguate players with the same name by checking division/team.

Usage:
  python3 scrapers/scrape_tennisrecord.py
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.tennisrecord.com"
RATINGS_URL = (
    f"{BASE_URL}/adult/ratings.aspx?"
    "sectionname=Intermountain&districtname=Nevada&areaname=Area"
    "&gender=F&orderby=NTRPRating"
)
DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
DELAY = 0.4  # seconds between profile fetches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Lowercase, strip, collapse spaces for name matching."""
    return re.sub(r"\s+", " ", (name or "").lower().strip())


def _load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))
    print(f"  [saved] {path}  ({len(data)} players)")


# ---------------------------------------------------------------------------
# Step 1 – scrape the ratings table
# ---------------------------------------------------------------------------

def fetch_ratings_table() -> list[dict]:
    """
    Download the full ratings table and return a list of:
      {name, name_norm, ntrp_rating, dynamic_rating, s_id, profile_url}
    """
    print(f"  Fetching ratings table …")
    resp = requests.get(RATINGS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    # The data table is the largest one (1574 data rows + 1 header)
    data_table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = data_table.find_all("tr")[1:]  # skip header

    records = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        link = row.find("a")
        if not link:
            continue
        name = link.get_text(strip=True)
        href = link.get("href", "")

        # Extract s_id from href (e.g. ?year=Rating&playername=John&s=42)
        s_id = None
        qs = parse_qs(urlparse(href).query)
        if "s" in qs:
            s_id = qs["s"][0]

        current_ntrp = cells[2].get_text(strip=True)  # "3.5 C", "3.0 S", etc.
        dynamic_str  = cells[4].get_text(strip=True)  # "3.4521"

        try:
            dynamic = float(dynamic_str)
        except (ValueError, TypeError):
            dynamic = None

        profile_url = f"{BASE_URL}/adult/profile.aspx?playername={name}" + (
            f"&s={s_id}" if s_id else ""
        )

        records.append({
            "name": name,
            "name_norm": _norm(name),
            "ntrp_rating": current_ntrp,
            "dynamic_rating": dynamic,
            "s_id": s_id,
            "profile_url": profile_url,
        })

    print(f"  Parsed {len(records)} records from ratings table.")
    return records


# ---------------------------------------------------------------------------
# Step 2 – fetch a profile page and extract division + team info
# ---------------------------------------------------------------------------

def fetch_profile_info(profile_url: str) -> dict:
    """
    Fetch a player profile page from tennisrecord.com and extract:
      - division keywords ("3.0 Women", "3.5 Women", "18+", etc.)
      - team name (most recent Adult 18+ team)
    Returns a dict with keys 'divisions' (list of strings) and 'team' (str or "").
    """
    try:
        resp = requests.get(profile_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [warn] profile fetch failed: {profile_url}: {e}")
        return {"divisions": [], "team": ""}

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Look for division markers in text
    divisions = re.findall(r"\b(?:3\.0|3\.5|4\.0|4\.5|5\.0)\s+Women\b", text)
    # Look for Adult 18+ team
    team_match = re.search(r"Adult 18\+.*?(?:team|Team)[^:]*:\s*([A-Z][^\n]+)", text)
    team = team_match.group(1).strip() if team_match else ""

    # Alternative: look for team name near "LIFE TIME" or similar
    if not team:
        # Try table rows with league/team info
        for row in soup.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            joined = " ".join(cells).lower()
            if "adult 18+" in joined or "adult" in joined:
                for cell in cells:
                    if len(cell) > 4 and not re.match(r"^\d", cell):
                        team = cell
                        break
                if team:
                    break

    return {"divisions": list(set(divisions)), "team": team.strip()}


# ---------------------------------------------------------------------------
# Step 3 – match and update players.json
# ---------------------------------------------------------------------------

def update_players(records: list[dict]):
    players = _load_json(PLAYERS_JSON, [])

    # Build lookup: name_norm -> list of rating records
    by_name: dict[str, list] = {}
    for r in records:
        by_name.setdefault(r["name_norm"], []).append(r)

    updated = 0
    skipped_no_match = 0
    skipped_collision = 0
    profile_fetches = 0

    for p in players:
        pname = _norm(p.get("name", ""))
        if not pname:
            continue

        matches = by_name.get(pname, [])

        if not matches:
            skipped_no_match += 1
            continue

        chosen = None

        if len(matches) == 1:
            # Unique name match → use directly
            chosen = matches[0]

        else:
            # Multiple records with same name → disambiguate

            # (a) If player already has tennisrecord_id, use s_id match
            if p.get("tennisrecord_id"):
                tid = str(p["tennisrecord_id"])
                for m in matches:
                    if m.get("s_id") == tid:
                        chosen = m
                        break

            # (b) If no tennisrecord_id, fetch profiles and match by division + team
            if not chosen:
                player_ntrp_prefix = (p.get("division", "") or "")[:3]  # "3.0" or "3.5"
                player_team_norm = _norm(p.get("team", ""))
                best = None
                best_score = -1

                for m in matches:
                    time.sleep(DELAY)
                    profile_fetches += 1
                    info = fetch_profile_info(m["profile_url"])

                    score = 0
                    # Division match
                    for div in info["divisions"]:
                        if player_ntrp_prefix and player_ntrp_prefix in div:
                            score += 2
                    # Team match (partial)
                    if player_team_norm and info["team"]:
                        if player_team_norm in _norm(info["team"]) or \
                           _norm(info["team"]) in player_team_norm:
                            score += 3

                    if score > best_score:
                        best_score = score
                        best = m

                if best and best_score > 0:
                    chosen = best
                    # Also store the s_id as tennisrecord_id if we found it
                    if best.get("s_id") and not p.get("tennisrecord_id"):
                        p["tennisrecord_id"] = best["s_id"]
                else:
                    skipped_collision += 1
                    continue

        # Apply the chosen record's ratings
        if chosen:
            if chosen.get("ntrp_rating"):
                p["ntrp_rating"] = chosen["ntrp_rating"]
            if chosen.get("dynamic_rating") is not None:
                p["dynamic_rating_baseline"] = chosen["dynamic_rating"]
            updated += 1

    _save_json(PLAYERS_JSON, players)
    print(
        f"  Updated: {updated}  |  No match: {skipped_no_match}  "
        f"|  Collision skipped: {skipped_collision}  "
        f"|  Profile fetches: {profile_fetches}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Scraping tennisrecord.com ratings ===")
    records = fetch_ratings_table()
    update_players(records)
    print("Done.")


if __name__ == "__main__":
    main()
