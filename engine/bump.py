"""
engine/bump.py
Bump indicator: an estimate of where USTA's own year-end NTRP calculation
will land for each player, and whether that moves them up or down a level.

This is deliberately NOT ShiQ. ShiQ (engine/ratings_v9.py) tries to measure
true strength. The Bump indicator copies the publicly reverse-engineered
shape of USTA's year-end math, even where we think that math is a worse
measure of strength:

* Each match produces a performance number from the share of games won
  against the strength of the opponents faced. Past a margin ceiling, extra
  games add nothing.
* Both doubles partners get exactly the same score-driven credit.
* The running rating after a match is the average of that match's
  performance and the previous three running ratings, so one match moves a
  player about a quarter of the way.
* Fewer than 3 matches in the rating year: the rating carries forward.
* Year-end call: above the level's top (3.00 for a 3.0) is a bump up, below
  its floor (2.50 for a 3.0) is a bump down.
* Self-rated / appealed players (S, A) can also be disqualified mid-year:
  three "strikes", each a running rating above level + 0.30 after a match.

Starting point is the Dec 2025 TennisRecord estimate of each player's USTA
dynamic rating (dynamic_rating_baseline), kept inside the player's current
level (see start_rating), so every call inherits that
estimate's error. Calls are checked against the real year-end ratings after
they are published (Dec 2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine import ratings as R
from engine.ratings_v9 import games_won

K = 1.2                  # same game-share curve as ShiQ v9
SHARE_CEILING = 0.92     # game shares beyond this count as this (margin cap)
MAX_GAP = 0.35           # a blowout "plays like" at most this far above the opponents (fits the post's base rates)
WINDOW = 4               # this match + previous 3 running ratings
MIN_MATCHES = 3
STRIKE_MARGIN = 0.30
LEVEL_WIDTH = 0.50

# Category thresholds on (year-end estimate - level top) / floor distance.
CLEAR_MARGIN = 0.05      # this far past the line = "likely"


@dataclass
class BumpState:
    history: list[float] = field(default_factory=list)   # running ratings, newest last
    matches: int = 0
    strikes: int = 0
    peak: float = 0.0


def parse_level(ntrp_rating: Optional[str]) -> tuple[Optional[float], str]:
    """'3.0 C' -> (3.0, 'C'). Unknown -> (None, '')."""
    if not ntrp_rating:
        return None, ""
    parts = ntrp_rating.split()
    try:
        level = float(parts[0])
    except (ValueError, IndexError):
        return None, ""
    return level, (parts[1] if len(parts) > 1 else "")


def start_rating(baseline: Optional[float], level: Optional[float],
                 letter: str = "") -> Optional[float]:
    """Starting point for the year, kept inside the player's current level.

    A player's current NTRP level was set by USTA's own year-end number, so
    that number must have been inside the level (2.51-3.00 for a 3.0). When
    the TennisRecord estimate falls outside it (common for appealed and
    self-rated players, who moved levels), pull it to the nearest edge.

    Self-rated and appealed players (S, A) have no USTA year-end number at
    this level yet, and TennisRecord's estimate for them is usually their old
    level or a default. They start no lower than the middle of the level.
    """
    if baseline is None or level is None:
        return baseline
    lo = level - LEVEL_WIDTH / 2 if letter in ("S", "A") else level - LEVEL_WIDTH + 0.01
    return min(max(baseline, lo), level)


def implied_gap(share: float) -> float:
    """Strength gap that would make this game share the expected one."""
    s = min(max(share, 1.0 - SHARE_CEILING), SHARE_CEILING)
    g = math.atanh(2.0 * s - 1.0) / K
    return max(-MAX_GAP, min(MAX_GAP, g))


def run_bump(events: list[R.CourtEvent], baselines: dict[str, float],
             levels: dict[str, tuple[Optional[float], str]]) -> dict[str, BumpState]:
    st: dict[str, BumpState] = {}

    def state(k: str) -> BumpState:
        if k not in st:
            b = baselines.get(k) or 3.0
            st[k] = BumpState(history=[b] * (WINDOW - 1), peak=b)
        return st[k]

    def current(k: str) -> float:
        return state(k).history[-1]

    for e in events:
        if not e.winner_keys or len(e.winner_keys) != len(e.loser_keys):
            continue
        w, l = games_won(e.score)
        if w + l == 0:
            continue
        sw = sum(current(k) for k in e.winner_keys) / len(e.winner_keys)
        sl = sum(current(k) for k in e.loser_keys) / len(e.loser_keys)
        # How far the result says the winning side out-played the rating gap.
        surplus = implied_gap(w / (w + l)) - (sw - sl)
        for k, sign in [(k, 1.0) for k in e.winner_keys] + [(k, -1.0) for k in e.loser_keys]:
            s = state(k)
            perf = s.history[-1] + sign * surplus          # equal credit for partners
            new = (perf + sum(s.history[-(WINDOW - 1):])) / WINDOW
            s.history.append(new)
            s.matches += 1
            s.peak = max(s.peak, new)
            level, letter = levels.get(k, (None, ""))
            if level is not None and letter in ("S", "A") and new > level + STRIKE_MARGIN:
                s.strikes += 1
    return st


def year_end_call(state: Optional[BumpState], level: Optional[float],
                  letter: str = "") -> dict:
    """Summarise one player's bump outlook.

    Returns {estimate, margin, call, label, matches, strikes}. ``call`` is one
    of up / lean_up / hold / lean_down / down / few_matches / unknown.
    """
    if level is None:
        return {"call": "unknown", "label": "No NTRP level"}
    if state is None or state.matches < MIN_MATCHES:
        n = state.matches if state else 0
        return {"call": "few_matches", "label": f"Too few matches ({n})",
                "matches": n}
    est = state.history[-1]
    top, floor = level, level - LEVEL_WIDTH
    if est > top:
        margin = est - top
        call = "up" if margin >= CLEAR_MARGIN else "lean_up"
    elif est <= floor:
        margin = est - floor
        call = "down" if -margin >= CLEAR_MARGIN else "lean_down"
    else:
        # Inside the level: distance to the nearer edge, signed toward it.
        margin = est - top if (top - est) < (est - floor) else est - floor
        call = ("lean_up" if est > top - CLEAR_MARGIN / 2 else
                "lean_down" if est < floor + CLEAR_MARGIN / 2 else "hold")
    out = {
        "estimate": round(est, 3),
        "margin": round(margin, 3),
        "call": call,
        "label": {
            "up": "Likely up", "lean_up": "Leaning up", "hold": "Holds",
            "lean_down": "Leaning down", "down": "Likely down",
        }[call],
        "matches": state.matches,
    }
    if letter in ("S", "A"):
        out["strikes"] = state.strikes
    return out


def compute_bumps(players: list[dict], events: Optional[list] = None) -> dict[str, dict]:
    """{player_key: year_end_call(...)} for every player in players.json."""
    pbn = R._build_players_by_name(players)
    if events is None:
        events = R._collect_court_events(R._discover_standings_files(), pbn)
        events.sort(key=R._date_sort_key)
    levels = {k: parse_level(p.get("ntrp_rating")) for k, p in pbn.items()}
    baselines = {k: start_rating(p.get("dynamic_rating_baseline"), *levels[k])
                 for k, p in pbn.items()}
    states = run_bump(events, baselines, levels)
    return {k: year_end_call(states.get(k), *levels[k]) for k in pbn}


def run_bumps() -> int:
    """Write a ``bump`` dict onto every player in players.json. Returns count."""
    players = R._load(R.PLAYERS_JSON, [])
    if not players:
        return 0
    pbn = R._build_players_by_name(players)
    calls = compute_bumps(players)
    for k, p in pbn.items():
        p["bump"] = calls[k]
    R._save(R.PLAYERS_JSON, players)
    return len(calls)


if __name__ == "__main__":
    from collections import Counter
    players = R._load(R.PLAYERS_JSON, [])
    calls = compute_bumps(players)
    print(Counter(c["call"] for c in calls.values()))
