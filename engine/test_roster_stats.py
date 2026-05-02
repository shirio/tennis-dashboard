"""
engine/test_roster_stats.py

Validates that every player's wl_record_{ntrp} and lines_played_{ntrp}
in players.json match what the standings scorecard data actually shows.

Run:
    python3 -m unittest engine.test_roster_stats -v
"""
from __future__ import annotations

import json
import re
import unittest
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"
STANDINGS_30 = DATA_DIR / "standings_women_30.json"
STANDINGS_35 = DATA_DIR / "standings_women_35.json"


# ---------------------------------------------------------------------------
# Helpers (duplicated from scrape_tennislink to avoid import coupling)
# ---------------------------------------------------------------------------

def _line_label_short(lnum: str) -> str:
    m = re.match(r'^(\d+)#\s+(Singles|Doubles)', (lnum or "").strip())
    if not m:
        return lnum
    prefix = "S" if m.group(2) == "Singles" else "D"
    return f"{prefix}{m.group(1)}"


def _split_players(field: str) -> list[str]:
    cleaned = re.sub(r",?\s*\d+-\d+.*$", "", field).strip()
    return [n.strip() for n in re.split(r"\s*/\s*", cleaned) if n.strip()]


def _is_default_side(s: str) -> bool:
    s = (s or "").strip().upper()
    return not s or s in ("N/A", "N/A / N/A", "DEFAULT", "NOT AVAILABLE")


def _compute_expected_stats(
    players: list[dict],
    all_ntrp_standings: list[tuple[str, list]],
) -> dict[str, dict[str, dict]]:
    """
    Walk all scorecard lines and return:
      expected[ntrp][player_name_lower] = {
          "wins": int,
          "losses": int,
          "courts": Counter({label: count}),
      }
    """
    player_team: dict[str, str] = {
        p["name"].lower().strip(): (p.get("team") or "").upper().strip()
        for p in players
    }

    wins:        dict[str, dict] = defaultdict(lambda: defaultdict(int))
    losses:      dict[str, dict] = defaultdict(lambda: defaultdict(int))
    courts:      dict[str, dict] = defaultdict(lambda: defaultdict(Counter))

    def _team_norm(t: str) -> str:
        return (t or "").upper().strip()

    for ntrp, subflights in all_ntrp_standings:
        for sf in subflights:
            for m in sf.get("matches", []):
                if m.get("pending") or not m.get("lines"):
                    continue

                hw = m.get("team_wins_home") or 0
                aw = m.get("team_wins_away") or 0
                match_home_won = hw > aw
                match_away_won = aw > hw

                match_home = _team_norm(m.get("home_team", ""))
                match_away = _team_norm(m.get("away_team", ""))

                # Swap detection
                normal_votes = swap_votes = 0
                for _ln in m["lines"]:
                    for _pn in _split_players(_ln.get("players_home", "")):
                        _pt = player_team.get(_pn.lower().strip(), "")
                        if _pt == match_home:   normal_votes += 1
                        elif _pt == match_away: swap_votes   += 1
                    for _pn in _split_players(_ln.get("players_away", "")):
                        _pt = player_team.get(_pn.lower().strip(), "")
                        if _pt == match_away:  normal_votes += 1
                        elif _pt == match_home: swap_votes  += 1
                is_swapped = swap_votes > normal_votes

                for ln in m["lines"]:
                    court_label = _line_label_short(ln.get("line", ""))
                    court_result = (ln.get("result") or "").lower()

                    def _process(pname: str, parsed_is_home: bool):
                        key = pname.lower().strip()
                        if not key:
                            return
                        if court_label:
                            courts[ntrp][key][court_label] += 1

                        pteam = player_team.get(key, "")
                        if pteam and match_home and match_away:
                            if pteam == match_home:
                                actual_home = True
                            elif pteam == match_away:
                                actual_home = False
                            else:
                                actual_home = (not parsed_is_home) if is_swapped else parsed_is_home
                        else:
                            actual_home = (not parsed_is_home) if is_swapped else parsed_is_home

                        if court_result in ("home", "away"):
                            won = (court_result == "home") if actual_home else (court_result == "away")
                        elif match_home_won or match_away_won:
                            won = match_home_won if actual_home else match_away_won
                        else:
                            return

                        if won:
                            wins[ntrp][key] += 1
                        else:
                            losses[ntrp][key] += 1

                    for pname in _split_players(ln.get("players_home", "")):
                        _process(pname, parsed_is_home=True)
                    for pname in _split_players(ln.get("players_away", "")):
                        _process(pname, parsed_is_home=False)

    # Merge into result dict
    all_keys: dict[str, set] = defaultdict(set)
    for ntrp_key in courts:
        all_keys[ntrp_key].update(courts[ntrp_key].keys())
    for ntrp_key in wins:
        all_keys[ntrp_key].update(wins[ntrp_key].keys())

    result: dict[str, dict] = defaultdict(dict)
    for ntrp, keys in all_keys.items():
        for key in keys:
            result[ntrp][key] = {
                "wins":   wins[ntrp].get(key, 0),
                "losses": losses[ntrp].get(key, 0),
                "courts": courts[ntrp].get(key, Counter()),
            }
    return result


def _courts_to_str(counter: Counter) -> list[str]:
    """Same format as _compute_player_stats_from_scorecards."""
    out = []
    for label in sorted(counter, key=lambda x: (x[0], int(x[1:]) if x[1:].isdigit() else 0)):
        cnt = counter[label]
        out.append(f"{label}x{cnt}" if cnt > 1 else label)
    return out


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestRosterStats(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.players = json.loads(PLAYERS_JSON.read_text())
        s30 = json.loads(STANDINGS_30.read_text())
        s35 = json.loads(STANDINGS_35.read_text())
        cls.expected = _compute_expected_stats(
            cls.players,
            [
                ("3.0", s30.get("subflights", [])),
                ("3.5", s35.get("subflights", [])),
            ],
        )
        cls.by_name = {p["name"].lower().strip(): p for p in cls.players}

    def _check_division(self, ntrp: str, sfx: str):
        expected_div = self.expected.get(ntrp, {})
        mismatches = []

        for key, exp in expected_div.items():
            p = self.by_name.get(key)
            if not p:
                continue  # player in scorecard but not in players.json — separate concern

            exp_wl   = f"{exp['wins']}-{exp['losses']}"
            exp_lines = _courts_to_str(exp["courts"])

            actual_wl    = p.get(f"wl_record_{sfx}") or ""
            actual_lines = p.get(f"lines_played_{sfx}") or []

            if actual_wl != exp_wl:
                mismatches.append(
                    f"{p['name']}: wl_record_{sfx}={actual_wl!r} but scorecard says {exp_wl!r}"
                )
            if actual_lines != exp_lines:
                mismatches.append(
                    f"{p['name']}: lines_played_{sfx}={actual_lines!r} but scorecard says {exp_lines!r}"
                )

        if mismatches:
            self.fail(
                f"{len(mismatches)} roster stat mismatch(es) in {ntrp}:\n"
                + "\n".join(f"  • {m}" for m in mismatches)
            )

    def test_wl_and_lines_30(self):
        """All players' wl_record_30 and lines_played_30 match scorecard data."""
        self._check_division("3.0", "30")

    def test_wl_and_lines_35(self):
        """All players' wl_record_35 and lines_played_35 match scorecard data."""
        self._check_division("3.5", "35")


if __name__ == "__main__":
    unittest.main()
