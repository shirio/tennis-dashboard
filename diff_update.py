#!/usr/bin/env python3
# ===========================================================================
# IMPORTANT NOTES — read before editing or running this file
# ===========================================================================
# 1. This is the CORRECT script for all weekly result updates.
#    When the user asks to check for new matches, ALWAYS run this — never update.py.
# 2. After running, commit and push all changed files (data/, HTML files).
# 3. These notes must be preserved unless the user explicitly says to remove them.
# ===========================================================================
"""
Diff-based TennisLink updater.

Instead of re-scraping every team page from scratch, this:
  1. Fetches only the Match Summary table per subflight (1 page per subflight).
  2. Compares aggregate scores + pending status against our stored data.
  3. For matches that actually changed, fetches the single scorecard page
     (StatsAndStandings.aspx?t=7&par1=MATCHID) — which has per-line player
     names, set scores, and winner mark.gif.
  4. Updates standings JSONs in place.
  5. Runs normalize, stats recompute, ratings, and rebuilds dashboards.

Usage:
    python3 diff_update.py              # do the diff scrape
    python3 diff_update.py --dry-run    # show diffs but don't fetch details
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path
from typing import Optional
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from scrapers.scrape_tennislink import (
    login, _navigate_to_my_team, _go_to_flight_page,
    _parse_match_summary_table, _match_key, _wait_for_network,
    DELAY, sleep, BASE_URL,
)

DATA = Path("data")
STANDINGS_30 = DATA / "standings_women_30.json"
STANDINGS_35 = DATA / "standings_women_35.json"

MATCH_VIEW_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?t=7&par1={{mid}}&par2=0&par3=0"


# ---------------------------------------------------------------------------
# t=7 page parser
# ---------------------------------------------------------------------------

def _parse_scorecard_from_t7(html: str) -> list[dict]:
    """
    Parse the StatsAndStandings?t=7 scorecard page.
    Returns list of {line, players_home, players_away, score, result}.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find each rptScoreCard_ctlNN block — typically 5 courts (ctl00..ctl04)
    # Strategy: find all elements with id matching the pattern, group by ctl index.
    results: list[dict] = []

    # Build an ordered list of ctl indices present on the page
    ctl_indices: list[str] = sorted(set(
        m.group(1)
        for el in soup.find_all(id=re.compile(r"rptScoreCard_ctl\d+_"))
        for m in [re.search(r"rptScoreCard_ctl(\d+)_", el.get("id", ""))]
        if m
    ), key=int)

    # We need an anchor row for each court. The simplest approach: find the
    # home-player-1 anchor, walk up to its containing <tr>, and collect from there.
    for idx in ctl_indices:
        home1 = soup.find(id=f"ctl00_mainContent_rptScoreCard_ctl{idx}_lnkHomePlayer1")
        if home1 is None:
            continue
        # The row holds: line label, home players + mark, vs, away players + mark, score
        # Walk up to the nearest <tr>
        row = home1.find_parent("tr")
        if row is None:
            continue

        # Line label: look in first few td's — text like "1# Singles"
        line_label = ""
        for td in row.find_all("td"):
            txt = td.get_text(" ", strip=True)
            m = re.search(r"(\d+#\s*(?:Singles|Doubles))", txt)
            if m:
                line_label = m.group(1)
                break

        # Home players
        home_names = []
        for suffix in ("lnkHomePlayer1", "lnkHomePlayer2"):
            el = soup.find(id=f"ctl00_mainContent_rptScoreCard_ctl{idx}_{suffix}")
            if el:
                n = el.get_text(strip=True)
                if n:
                    home_names.append(n)

        # Away players
        away_names = []
        for suffix in ("lnkVisitorPlayer1", "lnkVisitorPlayer2"):
            el = soup.find(id=f"ctl00_mainContent_rptScoreCard_ctl{idx}_{suffix}")
            if el:
                n = el.get_text(strip=True)
                if n:
                    away_names.append(n)

        # Mark.gif winner: either imgHomePlayer or imgVisitorPlayer exists
        home_mark = soup.find(id=f"ctl00_mainContent_rptScoreCard_ctl{idx}_imgHomePlayer")
        away_mark = soup.find(id=f"ctl00_mainContent_rptScoreCard_ctl{idx}_imgVisitorPlayer")
        if home_mark is not None:
            result = "home"
        elif away_mark is not None:
            result = "away"
        else:
            result = ""

        # Score: find the TD in this row containing digit-hyphen-digit patterns
        score_str = ""
        for td in row.find_all("td"):
            text = td.get_text(" ", strip=True).replace("\xa0", " ")
            if re.search(r"\d+-\d+", text):
                # Normalize whitespace — multiple spaces become single
                score_str = re.sub(r"\s+", " ", text).strip()
                # Only keep digit-hyphen-digit tokens
                sets = re.findall(r"\d+-\d+", score_str)
                score_str = " ".join(sets)
                break

        if not home_names and not away_names:
            continue

        results.append({
            "line": line_label,
            "players_home": " / ".join(home_names) if home_names else "N/A",
            "players_away": " / ".join(away_names) if away_names else "N/A",
            "score": score_str,
            "result": result,
        })

    return results


def _fetch_match_details(page, tl_match_id: str) -> Optional[list[dict]]:
    """Navigate to the t=7 page and parse the scorecard."""
    url = MATCH_VIEW_URL.format(mid=tl_match_id)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        sleep(DELAY * 0.4)
    except Exception as e:
        print(f"    [warn] failed to load match {tl_match_id}: {e}")
        return None
    html = page.content()
    return _parse_scorecard_from_t7(html)


# ---------------------------------------------------------------------------
# Diff detection
# ---------------------------------------------------------------------------

def _find_match_diffs(stored_standings: dict, summary_rows: list[dict]) -> list[dict]:
    """
    Compare Match Summary rows to stored matches (by match_id).
    Returns list of diff records: {kind, tl_match_id, stored_match, new_row, subflight_obj}
    """
    # Build stored_by_id lookup across all subflights in this standings object
    stored_by_id: dict[str, tuple[dict, dict]] = {}
    for sf in stored_standings.get("subflights", []):
        for m in sf.get("matches", []):
            tl = m.get("tl_match_id")
            if tl:
                stored_by_id[tl] = (sf, m)

    diffs = []
    for row in summary_rows:
        tl_id = row.get("match_id", "")
        if not tl_id:
            continue

        entry = stored_by_id.get(tl_id)
        if entry is None:
            # Match we don't have at all (shouldn't happen if we're in sync)
            diffs.append({
                "kind": "new",
                "tl_match_id": tl_id,
                "stored_match": None,
                "new_row": row,
                "subflight_obj": None,
            })
            continue

        sf_obj, m_obj = entry
        stored_home_team = (m_obj.get("home_team") or "").strip().lower()
        new_home_team = (row.get("home_team") or "").strip().lower()
        stored_home = m_obj.get("team_wins_home")
        stored_away = m_obj.get("team_wins_away")
        new_home = row.get("team_wins_home")
        new_away = row.get("team_wins_away")
        stored_pending = bool(m_obj.get("pending"))
        new_pending = bool(row.get("pending"))

        # Future/unplayed match heuristic: no team wins AND no games played.
        # TL sometimes reports 0/0 for unplayed matches with a status like
        # "No Team Reported" so we can't just trust the pending flag.
        def _is_unplayed(wins_h, wins_a, score_h, score_a):
            if wins_h in (None, 0) and wins_a in (None, 0):
                if score_h in (None, 0) and score_a in (None, 0):
                    return True
            return False

        stored_unplayed = stored_pending or _is_unplayed(
            stored_home, stored_away,
            m_obj.get("score_home"), m_obj.get("score_away"),
        )
        new_unplayed = new_pending or _is_unplayed(
            new_home, new_away,
            row.get("score_home"), row.get("score_away"),
        )
        if stored_unplayed and new_unplayed:
            continue  # both unplayed — no change

        # Handle home/away flip: if our stored orientation differs from TL's,
        # normalize before comparing scores.
        if stored_home_team == new_home_team:
            eh, ea = stored_home, stored_away
        else:
            # orientations reversed — compare our stored scores in flipped order
            eh, ea = stored_away, stored_home

        if eh == new_home and ea == new_away and stored_pending == new_pending:
            continue  # no change (accounting for possible orientation flip)

        diffs.append({
            "kind": "changed",
            "tl_match_id": tl_id,
            "stored_match": m_obj,
            "new_row": row,
            "subflight_obj": sf_obj,
        })

    return diffs


# ---------------------------------------------------------------------------
# Standings teams recompute
# ---------------------------------------------------------------------------

def _normalize_lines_inline():
    """
    For each match line that has result='home'/'away' but is missing
    winner_team/loser_team, derive those fields from the match's home/away teams.
    Writes updated standings back to disk.
    """
    for path in (STANDINGS_30, STANDINGS_35):
        data = json.loads(path.read_text())
        changed = False
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                ht = m.get("home_team", "")
                at = m.get("away_team", "")
                for ln in m.get("lines", []):
                    result = ln.get("result", "")
                    if result and not ln.get("winner_team"):
                        if result == "home":
                            ln["winner_team"] = ht
                            ln["loser_team"] = at
                        elif result == "away":
                            ln["winner_team"] = at
                            ln["loser_team"] = ht
                        changed = True
        if changed:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"  [normalize] {path.name} lines normalized")


# ---------------------------------------------------------------------------

def _recompute_standings_teams():
    """
    Recompute the `teams` array for every subflight from actual match results.
    Keeps standings W-L, individual line wins, sets, and games in sync after
    diff updates (the TennisLink scrape only populates these on full scrapes).
    """
    for path in (STANDINGS_30, STANDINGS_35):
        data = json.loads(path.read_text())
        changed = False
        for sf in data.get("subflights", []):
            teams: dict[str, dict] = {}
            for m in sf.get("matches", []):
                if m.get("pending"):
                    continue
                ht = m.get("home_team", "")
                at = m.get("away_team", "")
                hw = m.get("team_wins_home")
                aw = m.get("team_wins_away")
                if hw is None or aw is None:
                    continue
                for team in (ht, at):
                    if team not in teams:
                        teams[team] = {
                            "team_name": team,
                            "team_wins": 0, "team_losses": 0,
                            "matches_played": 0,
                            "indiv_wins": 0, "indiv_losses": 0,
                            "sets_won": 0, "sets_lost": 0,
                            "games_won": 0, "games_lost": 0,
                        }
                winner_team = ht if (hw > aw) else at
                loser_team = at if (hw > aw) else ht
                teams[winner_team]["team_wins"] += 1
                teams[loser_team]["team_losses"] += 1
                teams[ht]["matches_played"] += 1
                teams[at]["matches_played"] += 1

                # Line-level stats
                for ln in m.get("lines", []):
                    wt = ln.get("winner_team", "")
                    lt = ln.get("loser_team", "")
                    score = ln.get("score", "")
                    if not wt or not lt:
                        continue
                    for t in (wt, lt):
                        if t not in teams:
                            teams[t] = {
                                "team_name": t,
                                "team_wins": 0, "team_losses": 0,
                                "matches_played": 0,
                                "indiv_wins": 0, "indiv_losses": 0,
                                "sets_won": 0, "sets_lost": 0,
                                "games_won": 0, "games_lost": 0,
                            }
                    teams[wt]["indiv_wins"] += 1
                    teams[lt]["indiv_losses"] += 1

                    # Parse sets/games from winner-first score
                    for part in score.strip().split():
                        nums = part.split("-")
                        if len(nums) != 2:
                            continue
                        try:
                            wg, lg = int(nums[0]), int(nums[1])
                        except ValueError:
                            continue
                        teams[wt]["games_won"] += wg
                        teams[wt]["games_lost"] += lg
                        teams[lt]["games_won"] += lg
                        teams[lt]["games_lost"] += wg
                        # Count sets (tiebreak 1-0 counts as a set)
                        if wg > lg:
                            teams[wt]["sets_won"] += 1
                            teams[lt]["sets_lost"] += 1
                        else:
                            teams[lt]["sets_won"] += 1
                            teams[wt]["sets_lost"] += 1

            if not teams:
                continue

            # Add games_won_pct
            for t in teams.values():
                total_games = t["games_won"] + t["games_lost"]
                t["games_won_pct"] = (
                    f"{t['games_won'] / total_games * 100:.2f}%"
                    if total_games > 0 else "0.00%"
                )

            # Sort: wins desc, then indiv_wins desc, then games_won_pct desc
            sorted_teams = sorted(
                teams.values(),
                key=lambda t: (
                    -t["team_wins"],
                    -t["indiv_wins"],
                    -t["games_won"],
                ),
            )

            # Preserve any extra fields from the old teams array (e.g. notes)
            old_by_name = {t.get("team_name", ""): t
                           for t in sf.get("teams", [])}
            for t in sorted_teams:
                old = old_by_name.get(t["team_name"], {})
                for k in ("notes",):
                    if k in old:
                        t[k] = old[k]

            sf["teams"] = sorted_teams
            changed = True

        if changed:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"  [standings] {path.name} teams table updated")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> int:
    """
    Returns number of matches updated.
    """
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("  [error] TENNISLINK_USER / TENNISLINK_PASS not set in .env")
        return 0

    standings_30 = json.loads(STANDINGS_30.read_text())
    standings_35 = json.loads(STANDINGS_35.read_text())

    total_updated = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()

        try:
            login(page, username, password)
            print()

            for ntrp, standings in [("3.0", standings_30), ("3.5", standings_35)]:
                print(f"=== {ntrp} Women ===")
                # Navigate to flight page for this NTRP
                ok = _navigate_to_my_team(page, ntrp, 2026)
                if not ok:
                    print(f"  [warn] could not navigate to {ntrp} team")
                    continue
                if not _go_to_flight_page(page):
                    print(f"  [warn] could not reach flight page")
                    continue
                sleep(DELAY)

                # For each subflight: click label, click Match Summary, parse
                all_diffs: list[dict] = []
                for sf_label in sorted({sf.get("flight_label", "") for sf in standings.get("subflights", [])}):
                    if not sf_label:
                        continue

                    # Click subflight label
                    sf_link = None
                    for a in page.query_selector_all("a"):
                        try:
                            href = a.get_attribute("href") or ""
                            txt = (a.inner_text() or "").strip()
                            if "javascript:__doPostBack" in href and txt == sf_label:
                                sf_link = a
                                break
                        except Exception:
                            continue
                    if sf_link:
                        sf_link.click()
                        _wait_for_network(page, 12_000)
                        sleep(DELAY)

                    # Click Match Summary
                    clicked = False
                    for tab_label in ("Match Summary", "Match Schedule"):
                        for a in page.query_selector_all("a"):
                            try:
                                if tab_label in (a.inner_text() or ""):
                                    a.click()
                                    _wait_for_network(page, 10_000)
                                    sleep(DELAY)
                                    clicked = True
                                    break
                            except Exception:
                                pass
                        if clicked:
                            break

                    if not clicked:
                        print(f"  [warn] Match Summary tab not found for subflight {sf_label}")
                    summary_rows = _parse_match_summary_table(page)
                    print(f"  Subflight {sf_label}: {len(summary_rows)} matches in summary")

                    diffs = _find_match_diffs(standings, summary_rows)
                    n_diff = len(diffs)
                    if n_diff == 0:
                        print(f"    → no diffs")
                    else:
                        print(f"    → {n_diff} match(es) changed:")
                        for d in diffs:
                            r = d["new_row"]
                            stored = d["stored_match"]
                            if d["kind"] == "new":
                                print(f"      NEW: {r['date']} {r['home_team']} vs {r['away_team']} ({r.get('team_wins_home')}-{r.get('team_wins_away')})")
                            else:
                                sh, sa = stored.get("team_wins_home"), stored.get("team_wins_away")
                                nh, na = r.get("team_wins_home"), r.get("team_wins_away")
                                print(f"      CHANGED: {r['date']} {r['home_team']} vs {r['away_team']}: {sh}-{sa} → {nh}-{na}")
                    all_diffs.extend(diffs)

                    # Back to flight page for next subflight
                    _go_to_flight_page(page)
                    sleep(DELAY)

                # Process diffs: fetch scorecard details for each changed match
                if all_diffs and not dry_run:
                    print(f"  Fetching scorecard details for {len(all_diffs)} match(es)...")
                    for d in all_diffs:
                        tl_id = d["tl_match_id"]
                        row = d["new_row"]
                        stored = d["stored_match"]

                        lines = _fetch_match_details(page, tl_id)
                        if not lines:
                            print(f"    [skip] {tl_id} — no scorecard data")
                            continue

                        if stored is None:
                            # Shouldn't happen if scraping is kept in sync
                            print(f"    [warn] new match {tl_id} not in stored data; skipping")
                            continue

                        # Update stored match object in place.
                        # Detect home/away orientation flip: Match Summary may list
                        # teams in opposite order from what's stored (from original scrape).
                        # When flipped, swap the scores AND invert result on each line so
                        # normalize_lines assigns winner_team correctly.
                        stored_home = (stored.get("home_team") or "").strip().lower()
                        row_home = (row.get("home_team") or "").strip().lower()
                        orientation_flipped = (stored_home != row_home and bool(stored_home))
                        if orientation_flipped:
                            stored["team_wins_home"] = row.get("team_wins_away")
                            stored["team_wins_away"] = row.get("team_wins_home")
                            for ln in lines:
                                if ln.get("result") == "home":
                                    ln["result"] = "away"
                                elif ln.get("result") == "away":
                                    ln["result"] = "home"
                        else:
                            stored["team_wins_home"] = row.get("team_wins_home")
                            stored["team_wins_away"] = row.get("team_wins_away")
                        stored["pending"] = row.get("pending", False)
                        stored["lines"] = lines
                        print(f"    ✓ {row['date']} {row['home_team']} vs {row['away_team']}: "
                              f"{row.get('team_wins_home')}-{row.get('team_wins_away')}, "
                              f"{len(lines)} lines")
                        total_updated += 1

                # Save after each NTRP
                if not dry_run:
                    path = STANDINGS_30 if ntrp == "3.0" else STANDINGS_35
                    path.write_text(json.dumps(standings, indent=2, ensure_ascii=False))
                    print(f"  [saved] {path.name}")
                print()
        finally:
            ctx.close()
            browser.close()

    return total_updated


PLAYERS_JSON = DATA / "players.json"
TR_PROFILE_URL = "https://www.tennisrecord.com/adult/profile.aspx?playername={name}"


def _lookup_and_add_unknown_players(standings_30: dict, standings_35: dict) -> int:
    """
    Scan match scorecard lines for player names that aren't in players.json.
    For each unknown player, fetch their tennisrecord.com profile to get their
    dynamic baseline and NTRP, then add them to players.json.

    Returns the number of new players added.
    """
    import requests
    from urllib.parse import quote
    from scrape_players import parse_player_profile

    players: list[dict] = json.loads(PLAYERS_JSON.read_text()) if PLAYERS_JSON.exists() else []
    known_names = {p["name"].strip().lower() for p in players}

    # Collect every player name that appears in any match scorecard line
    names_in_matches: set[str] = set()
    for standings, ntrp, team_field in [
        (standings_30, "3.0", "team_30"),
        (standings_35, "3.5", "team_35"),
    ]:
        for sf in standings.get("subflights", []):
            for match in sf.get("matches", []):
                if match.get("pending"):
                    continue
                for ln in match.get("lines", []):
                    for field in ("players_home", "players_away"):
                        raw = ln.get(field, "")
                        for part in re.split(r"\s*/\s*", raw):
                            name = part.strip()
                            if name and name.upper() not in ("N/A", "DEFAULT", "NOT AVAILABLE"):
                                names_in_matches.add(name)

    unknown = [n for n in sorted(names_in_matches) if n.lower() not in known_names]
    if not unknown:
        return 0

    print(f"\n[player lookup] {len(unknown)} unknown player(s) in match data: {unknown}")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; tennis-dashboard)"})
    added = 0

    for name in unknown:
        url = TR_PROFILE_URL.format(name=quote(name))
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code != 200:
                print(f"  [skip] {name}: HTTP {resp.status_code}")
                continue
            profile = parse_player_profile(resp.text)
            dynamic = profile.get("dynamic_rating")
            ntrp_full = profile.get("ntrp_full") or ""
            ntrp_level = ntrp_full.split()[0] if ntrp_full else None

            if dynamic is None:
                print(f"  [skip] {name}: no dynamic rating found on tennisrecord.com")
                continue

            # Determine which division(s) this player plays in based on match data
            ntrp_divs: set[str] = set()
            for standings, ntrp in [(standings_30, "3.0"), (standings_35, "3.5")]:
                for sf in standings.get("subflights", []):
                    for match in sf.get("matches", []):
                        for ln in match.get("lines", []):
                            for field in ("players_home", "players_away"):
                                if name in re.split(r"\s*/\s*", ln.get(field, "")):
                                    ntrp_divs.add(ntrp)

            primary_div = sorted(ntrp_divs)[0] if ntrp_divs else (ntrp_level or "3.0")
            new_entry: dict = {
                "name": name,
                "dynamic_rating_baseline": round(dynamic, 4),
                "ntrp_rating": ntrp_level or primary_div,
                "division": f"{primary_div} Women",
            }
            if "3.0" in ntrp_divs:
                new_entry["team_30"] = None   # team unknown; will be filled by roster scrape
            if "3.5" in ntrp_divs:
                new_entry["team_35"] = None

            players.append(new_entry)
            known_names.add(name.lower())
            added += 1
            print(f"  [added] {name}: baseline={dynamic:.4f}  ntrp={ntrp_full}  div={ntrp_divs}")

        except Exception as e:
            print(f"  [error] {name}: {e}")

    if added:
        PLAYERS_JSON.write_text(json.dumps(players, indent=2, ensure_ascii=False))
        print(f"[player lookup] {added} new player(s) added to players.json")

    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Show diffs but don't fetch details")
    args = ap.parse_args()

    n_updated = run(dry_run=args.dry_run)

    if args.dry_run:
        print(f"Dry run complete.")
        return

    if n_updated == 0:
        print("No changes detected. Nothing to do.")
        return

    print(f"\n=== Post-processing ({n_updated} matches updated) ===")

    # Re-normalize winner/loser fields inline (convert result:"home"/"away" → winner_team/loser_team)
    import subprocess
    _normalize_lines_inline()

    # Recompute standings teams table (W-L, indiv wins, sets, games) from match results
    _recompute_standings_teams()

    # Recompute player stats from updated scorecards
    from scrapers.scrape_tennislink import _compute_player_stats_from_scorecards
    s30 = json.loads(STANDINGS_30.read_text())
    s35 = json.loads(STANDINGS_35.read_text())
    all_ntrp = []
    for ntrp, st in [("3.0", s30), ("3.5", s35)]:
        lst = []
        for sf in st.get("subflights", []):
            lst.append(sf)
        all_ntrp.append((ntrp, lst))
    _compute_player_stats_from_scorecards(all_ntrp)

    # Look up any player names in match data that aren't yet in players.json
    n_new = _lookup_and_add_unknown_players(s30, s35)

    # If new players were added, recompute their per-division stats (W-L, lines_played)
    # so they appear correctly in the dashboard on first match
    if n_new:
        _compute_player_stats_from_scorecards(all_ntrp)

    # Run ratings + rebuild dashboards (same steps as rebuild.py)
    from engine.ratings import run_ratings
    from engine.build_html import build_dashboards
    run_ratings()

    # Regenerate notes and subflight summaries (generate_notes.py handles both)
    subprocess.run(["python3", "generate_notes.py"], check=True)

    build_dashboards()

    print("\n✓ Done! Dashboards rebuilt. Don't forget to commit + push.")


if __name__ == "__main__":
    main()
