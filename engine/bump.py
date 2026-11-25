"""
engine/bump.py
Bump indicator: an estimate of where USTA's own year-end NTRP calculation
will land for each player, and whether that moves them up or down a level.

This is deliberately NOT ShiQ. ShiQ (engine/ratings_v9.py) tries to measure
true strength. The Bump indicator copies the publicly reverse-engineered
shape of USTA's math (r/10s analysis, updated Nov 2026), even where we think
that math is a worse measure of strength.

Year-end (fit_year_end):
* Each match is scored on share of games won against the strength of the
  other side; past a margin cap, extra games add nothing, so a blowout of a
  much weaker player can even pull you down.
* Every match of the rating year counts; recent ones only a little more
  (last 5 = 26% of the year-end, 6-10 = 23%, 11-15 = 15%, 16-20 = 17%,
  older = 19%). Playoff (Championships) matches count about 2.5x.
* Opponents and partners are judged where they FINISH the year, so all
  players are solved together.
* Both doubles partners get the same score-driven credit.
* What a computer-rated player carried in still matters; a self-rate or
  appeal level fades within a few matches.
* Fewer than 3 matches: the rating carries forward.
* Call: above the level's top (3.00 for a 3.0) = up, at or below its floor
  (2.50) = down.

Strikes (run_bump): self-rated / appealed players are DQ'd mid-year after
three matches that leave their RUNNING rating (this match averaged with the
previous three) at level + 0.30 or more. Strikes do not feed the year-end.

Starting point is the Dec 2025 TennisRecord estimate of each player's USTA
dynamic rating, kept inside the player's current level (start_rating), so
every call inherits that estimate's error. Calls are checked against the
real year-end ratings after they are published (Dec 2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine import ratings as R
from engine.ratings_v9 import games_won

K = 1.2                  # same game-share curve as ShiQ v9
SHARE_CEILING = 0.92     # game shares beyond this count as this (margin cap)
MAX_GAP = 0.50           # a blowout "plays like" at most this far above the opponents (tuned to base rates)
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


# ---------------------------------------------------------------------------
# Year-end model (v2, after the Nov 2026 update to the published analysis)
# ---------------------------------------------------------------------------
# The year-end is NOT where the running average lands. It is a re-scoring of
# the whole rating year:
#   * every match counts; recent ones only a little more. Share of the
#     year-end by match recency (newest first):
#       last 5: 26%, 6-10: 23%, 11-15: 15%, 16-20: 17%, older: 19%
#   * playoff (Championships) matches carry ~2.5x a regular match
#   * each match is judged against where opponents and partners FINISH the
#     year, not where they stood that day (solved jointly, below)
#   * what a player carried in from last year still matters for computer-
#     rated players; a self-rate / appeal level fades within 3-4 matches.
RECENCY_SHARE = [(5, 0.26), (5, 0.23), (5, 0.15), (5, 0.17)]
OLDER_SHARE = 0.19
PLAYOFF_WEIGHT = 2.5
PRIOR_WEIGHT_COMPUTER = 2.0   # carry-in, in "matches" (tuned to base rates)
PRIOR_WEIGHT_SELF = 1.0       # S / A: fades within a few matches
FIT_ITERATIONS = 60
FIT_DAMPING = 0.5


def recency_weights(n: int) -> list[float]:
    """Per-match weights, oldest first, for a player with n counted matches.

    Scaled so a match in the newest block weighs about 1.
    """
    w_rev: list[float] = []
    for size, share in RECENCY_SHARE:
        w_rev += [share / size * 5 / 1.3] * size
    if n > len(w_rev):
        older = n - len(w_rev)
        w_rev += [OLDER_SHARE * 5 / 1.3 / older] * older
    return list(reversed(w_rev[:n]))


def playoff_match_ids() -> set[str]:
    """match_ids from Championships flights in the standings files."""
    ids: set[str] = set()
    for path, _ in R._discover_standings_files():
        data = R._load(path, {})
        for sf in data.get("subflights", []):
            if "championship" in (sf.get("flight_label") or "").lower():
                ids.update(m.get("match_id", "") for m in sf.get("matches", []))
    return ids


def fit_year_end(events: list[R.CourtEvent], start: dict[str, float],
                 letters: dict[str, str], playoffs: set[str] = frozenset()
                 ) -> tuple[dict[str, float], dict[str, int]]:
    """Jointly solve every player's year-end number.

    Each player's year-end Y balances the pull of their prior (start rating,
    weight PRIOR_WEIGHT_*) against the weighted surplus of each match:
        surplus = implied_gap(game share) - (Y_own_side - Y_other_side)
    using everyone's year-end Y, so opponents are judged where they finish.
    Partners get the same surplus (equal credit).
    """
    scored = []
    per_player: dict[str, list[int]] = {}
    for e in events:
        if not e.winner_keys or len(e.winner_keys) != len(e.loser_keys):
            continue
        w, l = games_won(e.score)
        if w + l == 0:
            continue
        idx = len(scored)
        scored.append((e, implied_gap(w / (w + l)), e.match_id in playoffs))
        for k in e.winner_keys + e.loser_keys:
            per_player.setdefault(k, []).append(idx)

    weight: dict[tuple[str, int], float] = {}
    for k, idxs in per_player.items():
        for i, rw in zip(idxs, recency_weights(len(idxs))):
            weight[(k, i)] = rw * (PLAYOFF_WEIGHT if scored[i][2] else 1.0)

    Y = {k: (start.get(k) or 3.0) for k in per_player}
    prior_w = {k: (PRIOR_WEIGHT_SELF if letters.get(k) in ("S", "A")
                   else PRIOR_WEIGHT_COMPUTER) for k in per_player}
    for _ in range(FIT_ITERATIONS):
        push = {k: 0.0 for k in Y}
        for i, (e, gap, _) in enumerate(scored):
            sw = sum(Y[k] for k in e.winner_keys) / len(e.winner_keys)
            sl = sum(Y[k] for k in e.loser_keys) / len(e.loser_keys)
            surplus = gap - (sw - sl)
            for k in e.winner_keys:
                push[k] += weight[(k, i)] * surplus
            for k in e.loser_keys:
                push[k] -= weight[(k, i)] * surplus
        for k in Y:
            total_w = prior_w[k] + sum(weight[(k, i)] for i in per_player[k])
            step = (push[k] - prior_w[k] * (Y[k] - (start.get(k) or 3.0))) / total_w
            Y[k] += FIT_DAMPING * step
    return Y, {k: len(v) for k, v in per_player.items()}


def year_end_call(estimate: Optional[float], matches: int, level: Optional[float],
                  letter: str = "", strikes: Optional[int] = None) -> dict:
    """Summarise one player's bump outlook.

    Returns {estimate, margin, call, label, matches, strikes}. ``call`` is one
    of up / lean_up / hold / lean_down / down / few_matches / unknown.
    """
    if level is None:
        return {"call": "unknown", "label": "No NTRP level"}
    if estimate is None or matches < MIN_MATCHES:
        return {"call": "few_matches", "label": f"Too few matches ({matches})",
                "matches": matches}
    est = estimate
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
        "matches": matches,
    }
    if letter in ("S", "A") and strikes is not None:
        out["strikes"] = strikes
    return out


def compute_bumps(players: list[dict], events: Optional[list] = None,
                  playoffs: Optional[set[str]] = None) -> dict[str, dict]:
    """{player_key: year_end_call(...)} for every player in players.json."""
    pbn = R._build_players_by_name(players)
    if events is None:
        events = R._collect_court_events(R._discover_standings_files(), pbn)
        events.sort(key=R._date_sort_key)
    if playoffs is None:
        playoffs = playoff_match_ids()
    levels = {k: parse_level(p.get("ntrp_rating")) for k, p in pbn.items()}
    starts = {k: start_rating(p.get("dynamic_rating_baseline"), *levels[k])
              for k, p in pbn.items()}
    letters = {k: levels[k][1] for k in pbn}
    year_end, counts = fit_year_end(events, starts, letters, playoffs)
    strikes = run_bump(events, starts, levels)    # running rating: strikes only
    out = {}
    for k in pbn:
        st = strikes.get(k)
        out[k] = year_end_call(year_end.get(k), counts.get(k, 0), *levels[k],
                               strikes=st.strikes if st else None)
    return out


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
