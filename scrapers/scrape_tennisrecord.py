"""
scrapers/scrape_tennisrecord.py

Fetch NTRP ratings and estimated dynamic ratings from tennisrecord.com and
update data/players.json in place.

Sources:
  Ratings page: https://www.tennisrecord.com/adult/ratings.aspx?...
    - "Current NTRP" column  -> ntrp_rating (e.g. "3.0 C", "3.5 S")
    - "2026 Estimated Dynamic" column -> used for matching only, NOT for baseline
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
from urllib.parse import urlencode, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.tennisrecord.com"
DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"
REGIONS_JSON = DATA_DIR / "regions.json"

# Legacy default (NV)
RATINGS_URL = (
    f"{BASE_URL}/adult/ratings.aspx?"
    "sectionname=Intermountain&districtname=Nevada&areaname=Area"
    "&gender=F&orderby=NTRPRating"
)


def _build_ratings_url(section: str = "Intermountain",
                       district: str = "Nevada",
                       area: str = "Area",
                       gender: str = "F") -> str:
    """Build tennisrecord ratings table URL for a given region."""
    params = {
        "sectionname": section,
        "districtname": district,
        "areaname": area,
        "gender": gender,
        "orderby": "NTRPRating",
    }
    return f"{BASE_URL}/adult/ratings.aspx?{urlencode(params)}"


def _load_regions() -> dict:
    if REGIONS_JSON.exists():
        return json.loads(REGIONS_JSON.read_text())
    return {}


def _get_state_config(state_code: str) -> dict:
    regions = _load_regions()
    cfg = regions.get("states", {}).get(state_code)
    if not cfg:
        raise ValueError(f"No config for state {state_code!r} in {REGIONS_JSON}")
    cfg["_section"] = regions.get("section", "Intermountain")
    cfg["_state_code"] = state_code
    return cfg

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

def fetch_ratings_table(url: str | None = None) -> list[dict]:
    """
    Download the full ratings table and return a list of:
      {name, name_norm, ntrp_rating, dynamic_rating, s_id, profile_url}
    """
    url = url or RATINGS_URL
    print(f"  Fetching ratings table from {url} …")
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=(10, 300))
            resp.raise_for_status()
            break
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            if attempt < 2:
                wait = 5 * (attempt + 1)
                print(f"    [retry] attempt {attempt+1} failed ({e.__class__.__name__}), waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"    [error] all 3 attempts failed for {url}")
                return []

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
# Step 1b – search for a player by name and get their s_id + NTRP + dynamic
# ---------------------------------------------------------------------------

_search_session = None

def search_player(name: str, state_hint: str = "") -> dict | None:
    """
    Search tennisrecord.com for a player by name (POST form) and return their info.
    Returns {name, s_id, ntrp_rating, profile_url} or None.
    """
    global _search_session
    search_url = f"{BASE_URL}/adult/search.aspx"

    try:
        if _search_session is None:
            _search_session = requests.Session()

        resp = _search_session.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        form_data = {}
        for inp in soup.find_all("input"):
            n = inp.get("name", "")
            if n:
                form_data[n] = inp.get("value", "")

        parts = name.strip().rsplit(" ", 1)
        form_data["firstname"] = parts[0] if len(parts) > 1 else ""
        form_data["lastname"] = parts[-1]

        resp2 = _search_session.post(search_url, data=form_data, headers=HEADERS, timeout=15)
        resp2.raise_for_status()
        soup2 = BeautifulSoup(resp2.text, "html.parser")
    except Exception as e:
        print(f"    [warn] search failed for {name}: {e}")
        return None

    candidates = []
    for row in soup2.find_all("tr"):
        link = row.find("a")
        if not link or "profile.aspx" not in link.get("href", ""):
            continue
        rname = link.get_text(strip=True)
        if _norm(rname) != _norm(name):
            continue

        href = link["href"]
        qs = parse_qs(urlparse(href).query)
        s_id = qs["s"][0] if "s" in qs else None

        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        location = cells[1] if len(cells) > 1 else ""
        ntrp = cells[3] if len(cells) > 3 else ""

        candidates.append({
            "name": rname,
            "s_id": s_id,
            "ntrp_rating": ntrp,
            "location": location,
            "profile_url": f"{BASE_URL}{href}",
        })

    if not candidates:
        return None

    if state_hint and len(candidates) > 1:
        st = state_hint.upper()
        state_matches = [c for c in candidates
                         if f", {st}" in c.get("location", "").upper()
                         or state_hint.lower() in c.get("location", "").lower()]
        if state_matches:
            return state_matches[0]

    return candidates[0]


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

def _ntrp_compatible(player_div: str, tr_ntrp: str) -> bool:
    """Check if a tennisrecord NTRP rating is plausible for a player's division.
    A non-D player's NTRP number must equal their division level (they can play
    up to 0.5 above, so a 3.0 C plays in 3.0 or 3.5). "D" means moved down one
    level: 3.5 D plays 3.0, 4.0 D plays 3.5. Players never play below their level
    unless they have a D designation."""
    if not tr_ntrp:
        return True
    parts = tr_ntrp.split()
    try:
        ntrp_num = float(parts[0])
    except (ValueError, IndexError):
        return True
    letter = parts[1].upper() if len(parts) > 1 else ""
    try:
        div_num = float(player_div[:3])
    except (ValueError, IndexError):
        return True
    if letter == "D":
        # D player's effective level is one step below their number
        effective = ntrp_num - 0.5
        return effective <= div_num and effective >= div_num - 0.5
    # Non-D: NTRP number is their base level, can play up 0.5
    return ntrp_num >= div_num - 0.5 and ntrp_num <= div_num


def update_players(records: list[dict], state_code: str | None = None):
    players = _load_json(PLAYERS_JSON, [])

    # Build lookup: name_norm -> list of rating records
    by_name: dict[str, list] = {}
    for r in records:
        by_name.setdefault(r["name_norm"], []).append(r)

    updated = 0
    skipped_no_match = 0
    skipped_collision = 0
    skipped_ntrp = 0
    profile_fetches = 0

    for p in players:
        if state_code and p.get("state") != state_code:
            continue
        pname = _norm(p.get("name", ""))
        if not pname:
            continue

        matches = by_name.get(pname, [])

        if not matches:
            skipped_no_match += 1
            continue

        player_div = p.get("division", "") or ""

        chosen = None

        if len(matches) == 1:
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

            # (b) Pre-filter to NTRP-compatible candidates
            if not chosen:
                compatible = [m for m in matches
                              if _ntrp_compatible(player_div, m.get("ntrp_rating", ""))]
                if len(compatible) == 1:
                    chosen = compatible[0]
                elif len(compatible) > 1:
                    # Fetch profiles and match by division + team
                    player_ntrp_prefix = player_div[:3]  # "3.0" or "3.5"
                    player_team_norm = _norm(p.get("team", ""))
                    best = None
                    best_score = -1

                    for m in compatible:
                        time.sleep(DELAY)
                        profile_fetches += 1
                        info = fetch_profile_info(m["profile_url"])

                        score = 0
                        for div in info["divisions"]:
                            if player_ntrp_prefix and player_ntrp_prefix in div:
                                score += 2
                        if player_team_norm and info["team"]:
                            if player_team_norm in _norm(info["team"]) or \
                               _norm(info["team"]) in player_team_norm:
                                score += 3

                        if score > best_score:
                            best_score = score
                            best = m

                    if best and best_score > 0:
                        chosen = best
                        if best.get("s_id") and not p.get("tennisrecord_id"):
                            p["tennisrecord_id"] = best["s_id"]
                    else:
                        skipped_collision += 1
                        continue
                else:
                    skipped_collision += 1
                    continue

        if chosen and not _ntrp_compatible(player_div, chosen.get("ntrp_rating", "")):
            skipped_ntrp += 1
            continue

        # Apply the chosen record's ratings
        if chosen:
            if chosen.get("ntrp_rating"):
                p["ntrp_rating"] = chosen["ntrp_rating"]
            if chosen.get("s_id") and not p.get("tennisrecord_id"):
                p["tennisrecord_id"] = chosen["s_id"]
            p.pop("pending_tennisrecord_lookup", None)
            updated += 1

    _save_json(PLAYERS_JSON, players)
    print(
        f"  Updated: {updated}  |  No match: {skipped_no_match}  "
        f"|  Collision skipped: {skipped_collision}  "
        f"|  NTRP rejected: {skipped_ntrp}  "
        f"|  Profile fetches: {profile_fetches}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def search_and_update_baselines(state_code: str | None = None,
                                 only_sectionals: bool = False):
    """
    For players still pending tennisrecord lookup, search by name to find
    their s_id, then scrape match history for pre-2026 baseline.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from scrape_baselines import get_baseline

    players = _load_json(PLAYERS_JSON, [])

    if only_sectionals:
        qualified = _load_json(DATA_DIR / "sectionals_qualified.json", {})
        q_teams = set()
        for t in qualified.get("qualified_teams", []):
            q_teams.add(t["team"].lower().strip())
        targets = [p for p in players
                   if p.get("pending_tennisrecord_lookup")
                   and ((p.get("team_30") or "").lower().strip() in q_teams
                        or (p.get("team") or "").lower().strip() in q_teams)]
    elif state_code:
        targets = [p for p in players
                   if p.get("state") == state_code
                   and p.get("pending_tennisrecord_lookup")]
    else:
        targets = [p for p in players if p.get("pending_tennisrecord_lookup")]

    print(f"=== Search + baseline scrape for {len(targets)} players ===")

    updated = 0
    not_found = 0
    no_baseline = 0

    for i, p in enumerate(targets):
        name = p.get("name", "")
        state = p.get("state", "")
        if not name:
            continue

        time.sleep(DELAY)
        result = search_player(name, state)

        if not result:
            not_found += 1
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(targets)}] {updated} updated, {not_found} not found")
            continue

        s_id = result.get("s_id")
        if s_id:
            p["tennisrecord_id"] = s_id
        p["profile_url"] = result.get("profile_url", "")

        if result.get("ntrp_rating"):
            p["ntrp_rating"] = result["ntrp_rating"]

        time.sleep(DELAY)
        date_str, baseline, err = get_baseline(name, s_id)

        if baseline is not None:
            p["dynamic_rating_baseline"] = baseline
            p["baseline_source"] = "oldest_fallback" if err == "oldest_fallback" else "history"
            p.pop("pending_tennisrecord_lookup", None)
            updated += 1
        else:
            no_baseline += 1
            p.pop("pending_tennisrecord_lookup", None)

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(targets)}] {updated} updated, {not_found} not found, {no_baseline} no baseline")
            _save_json(PLAYERS_JSON, players)

    _save_json(PLAYERS_JSON, players)
    print(f"  Done: {updated} updated, {not_found} not found, {no_baseline} no baseline")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scrape tennisrecord.com ratings")
    parser.add_argument("--state", default="NV", help="State code (NV, CO, UT, ID)")
    parser.add_argument("--search-baselines", action="store_true",
                        help="Search + match history baseline scrape for pending players")
    parser.add_argument("--sectionals-only", action="store_true",
                        help="Only process sectionals-qualified players")
    args = parser.parse_args()

    state_code = args.state.upper()

    if args.search_baselines:
        search_and_update_baselines(state_code, only_sectionals=args.sectionals_only)
        return

    cfg = _get_state_config(state_code)

    section = cfg["_section"]
    district = cfg.get("tennisrecord_district", cfg["district"])

    tr_areas = cfg.get("tennisrecord_areas") or []
    single_area = cfg.get("tennisrecord_area", "")

    if tr_areas:
        all_records = []
        for area_name in tr_areas:
            url = _build_ratings_url(section, district, area_name)
            print(f"=== Scraping tennisrecord.com ratings for {state_code} / {area_name} ===")
            records = fetch_ratings_table(url)
            all_records.extend(records)
        print(f"  Total: {len(all_records)} records across {len(tr_areas)} areas")
        update_players(all_records, state_code)
    else:
        url = _build_ratings_url(section, district, single_area or "")
        print(f"=== Scraping tennisrecord.com ratings for {state_code} ({district}) ===")
        records = fetch_ratings_table(url)
        update_players(records, state_code)

    print("Done.")


if __name__ == "__main__":
    main()
