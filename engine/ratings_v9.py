"""
engine/ratings_v9.py
ShiQ v9: games-based sequential rating with a per-player singles offset.

This is the ShiQ every page shows: engine/shiq.py runs it after the v8
engine (engine/ratings.py) and overwrites v8's rating fields with v9's.
v9 beat v8 on the backtest scorecard (engine/backtest.py --holdout).

How it works
------------
* Every player has a doubles rating ``r`` (starts at the Dec baseline) and a
  singles offset ``o`` (starts at 0). Doubles strength = r, singles = r + o.
  A doubles line moves both; a singles line moves singles fully and doubles
  75% as much (see LR_DOUBLES / LR_SINGLES / SINGLES_TO_DOUBLES).
* For each completed court line, oldest first, the engine predicts the share
  of games the winning side should take:
      expected = 0.5 + 0.5 * tanh(K * (side_A - side_B))
  where a side's strength is the average of its players.
* It then compares that with the share of games actually won and moves both
  sides by ``lr * (actual - expected)``: a 6-4 6-4 loss to a stronger pair
  can raise you, a 6-0 6-0 win over a weak pair barely moves you.
* Both partners get the same credit (no partner-asymmetric rule).
* Players with no real baseline (NTRP default) learn faster for their first
  few matches, because their starting number is a guess.
* Win probability for the matchup explorer / backtest is a logistic curve of
  the strength gap, ``floor + (1 - 2*floor) / (1 + exp(-slope * gap))``; the slope grows
  with how many matches the players have behind them (win_slope), fitted on our data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine import ratings as R

# ---------------------------------------------------------------------------
# Parameters (tuned on the first half of spring 2026, see engine/backtest.py)
# ---------------------------------------------------------------------------
# Retuned 2026-11-27 on the full 2024-2026 TennisLink crawl (tuned on 2025,
# checked on 2026's 26.9k lines): ratings move faster and the game-share curve
# is flatter. Brier 0.2137 -> 0.2026 with WIN_SLOPE refit (old: K 1.2, LR 0.20, slope 8).
K = 0.8                 # game-share sensitivity to strength gap (singles lines)
LR = 0.30               # (kept for reference; see the split below)
LR_SINGLES_OFFSET = 0.10  # (kept for reference; see the split below)
# Singles / doubles split (Shirin, 2026-11-27; tuned on 2025 lines, checked on
# 2026's 26.9k): the doubles rating r and the singles rating r + o.
#   doubles line: r moves LR_DOUBLES * miss; the singles rating moves with it.
#   singles line: the singles rating moves LR_SINGLES * miss, r moves
#                 SINGLES_TO_DOUBLES of that (the offset o takes the rest).
# Fully separate ratings were worse (Brier 0.2062): most players have few
# singles lines. This split: Brier 0.2027 -> 0.1995 (singles 0.1995 -> 0.1988,
# doubles 0.2038 -> 0.1998).
LR_DOUBLES = 0.50
LR_SINGLES = 0.60
SINGLES_TO_DOUBLES = 0.75
K_DOUBLES = 0.7         # doubles game shares follow the gap a bit more loosely
PROVISIONAL_BOOST = 2.0   # step multiplier for players starting on an NTRP default
PROVISIONAL_MATCHES = 5   # ... for this many matches
WIN_SLOPE = 3.0          # logistic slope when we don't know how much history the players have
# Confidence grows with history (Shirin 2026-11-28: the odds must match reality).
# One slope was too cautious for players with history (said 85%, won 92%) and
# too confident for new ones (said 90%+, won 78%). The slope now runs from
# WIN_SLOPE_NEW to WIN_SLOPE_SETTLED with the fewer side's prior same-type
# matches n:  slope = new + (settled - new) * (1 - exp(-n / WIN_SLOPE_TAU)).
# Tuned on half of 2026's lines, checked on the other half: Brier 0.2059 ->
# 0.2040 and 0.2038 -> 0.2020; the 90%+ group now wins about as often as predicted.
WIN_SLOPE_NEW = 2.0
WIN_SLOPE_SETTLED = 4.5
WIN_SLOPE_TAU = 3.0
UPSET_FLOOR = 0.04       # even a lopsided matchup is an upset ~4% of the time
# (refit 2026-11-25 on 13,852 lines: the plain curve said 97% where favourites
#  won 93%; slope 8 with a 4% floor tops out at 96% and matches actual rates)

# Baselines that are just the NTRP fallback (ratings._NTRP_DEFAULT_RATING)
_DEFAULT_BASELINES = {round(v, 2) for v in R._NTRP_DEFAULT_RATING.values()}


@dataclass
class Params:
    k: float = K
    k_doubles: float = K_DOUBLES
    lr_doubles: float = LR_DOUBLES
    lr_singles: float = LR_SINGLES
    singles_to_doubles: float = SINGLES_TO_DOUBLES
    provisional_boost: float = PROVISIONAL_BOOST
    provisional_matches: int = PROVISIONAL_MATCHES


@dataclass
class V9Result:
    rating: dict[str, float] = field(default_factory=dict)          # shared r
    singles_offset: dict[str, float] = field(default_factory=dict)  # o
    matches: dict[str, int] = field(default_factory=dict)
    singles_matches: dict[str, int] = field(default_factory=dict)
    # Filled when run_v9(timelines=True):
    # player -> {(division, date): (rating going in, rating after that date)}
    timeline: dict[str, dict[tuple[str, str], list[float]]] = field(default_factory=dict)
    # player -> {"match id|line": [rating going in, rating after]}, one per line
    line_timeline: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    # One entry per scored event, in processed order:
    # (event, pre-match strength diff winner-side minus loser-side)
    pre_match: list[tuple[R.CourtEvent, float]] = field(default_factory=list)
    # Parallel to pre_match: prior same-type matches of the line's least-experienced player
    pre_match_n: list[int] = field(default_factory=list)

    def singles(self, k: str) -> Optional[float]:
        if k not in self.rating:
            return None
        return self.rating[k] + self.singles_offset.get(k, 0.0)

    def doubles(self, k: str) -> Optional[float]:
        return self.rating.get(k)


def win_slope(matches: Optional[int]) -> float:
    """Logistic slope for a line whose less-experienced side has ``matches``
    prior same-type (singles or doubles) matches; WIN_SLOPE when unknown."""
    if matches is None:
        return WIN_SLOPE
    return WIN_SLOPE_NEW + (WIN_SLOPE_SETTLED - WIN_SLOPE_NEW) * (1.0 - math.exp(-matches / WIN_SLOPE_TAU))


def win_probability(gap: float, slope: Optional[float] = None,
                    floor: float = UPSET_FLOOR, matches: Optional[int] = None) -> float:
    """P(side wins) given strength gap (side minus opponents), kept within
    [floor, 1 - floor]. ``matches``: prior same-type matches of the side with
    fewer (sets the confidence); ``slope`` overrides it."""
    if slope is None:
        slope = win_slope(matches)
    return floor + (1.0 - 2.0 * floor) / (1.0 + math.exp(-slope * gap))


def side_win_probability(side: list[float], opp: list[float],
                         slope: Optional[float] = None, matches: Optional[int] = None) -> float:
    """P(side wins) for singles or doubles: a side's strength is its average."""
    return win_probability(sum(side) / len(side) - sum(opp) / len(opp), slope, matches=matches)


def expected_game_share(gap: float, k: float = K) -> float:
    return 0.5 + 0.5 * math.tanh(k * gap)


def games_won(score: str) -> tuple[int, int]:
    """(winner_games, loser_games) for a line score stored from the winner's side.

    A match tiebreak (1-0 / 0-1) counts as one game.
    """
    w = l = 0
    for a, b, first_won in R._parse_sets(score):
        if a <= 1 and b <= 1:
            a, b = 1, 0
        if first_won:
            w, l = w + a, l + b
        else:
            w, l = w + b, l + a
    return w, l


def is_provisional(baseline: Optional[float]) -> bool:
    return baseline is None or round(baseline, 2) in _DEFAULT_BASELINES


def line_ref(e) -> str:
    """Key of one court line: 'match id|line'. A standings line that the
    TennisLink crawl also has carries the crawl's key (crawl_ref), so the
    crawl-based player pages can find it."""
    return getattr(e, "crawl_ref", None) or f"{e.match_id}|{e.line_label}"


def run_v9(events: list[R.CourtEvent], baselines: dict[str, float],
           params: Params = Params(), timelines: bool = False,
           provisional_keys: frozenset = frozenset(),
           state: Optional[V9Result] = None) -> V9Result:
    """Process court events in the given (chronological) order.

    With ``timelines`` the result also records, per player and per
    (division, date), the shared rating going into that date's first match
    and after its last one. ``provisional_keys`` are players whose baseline
    is only a guess (e.g. crawled players placed mid-level), so they learn
    faster at first like NTRP-default players. ``state`` continues from an
    earlier run (prior seasons): ratings, singles offsets and match counts
    carry over; ``baselines`` only seeds players new to this run.
    """
    res = V9Result()
    if state is not None:
        res.rating = dict(state.rating)
        res.singles_offset = dict(state.singles_offset)
        res.matches = dict(state.matches)
        res.singles_matches = dict(state.singles_matches)
    provisional = {k: is_provisional(v) or k in provisional_keys for k, v in baselines.items()}

    def strength(k: str, singles: bool) -> float:
        if k not in res.rating:
            res.rating[k] = baselines.get(k) or 3.0
        return res.rating[k] + (res.singles_offset.get(k, 0.0) if singles else 0.0)

    for e in events:
        if not e.winner_keys or len(e.winner_keys) != len(e.loser_keys):
            continue   # a side has a player we can't identify
        w, l = games_won(e.score)
        if w + l == 0:
            continue
        singles = len(e.winner_keys) == 1
        sw = sum(strength(k, singles) for k in e.winner_keys) / len(e.winner_keys)
        sl = sum(strength(k, singles) for k in e.loser_keys) / len(e.loser_keys)
        gap = sw - sl
        res.pre_match.append((e, gap))
        res.pre_match_n.append(min(
            (res.singles_matches.get(k, 0) if singles
             else res.matches.get(k, 0) - res.singles_matches.get(k, 0))
            for k in e.winner_keys + e.loser_keys))
        if timelines:
            ref = line_ref(e)
            for k in e.winner_keys + e.loser_keys:
                res.timeline.setdefault(k, {}).setdefault(
                    (e.division, e.date), [res.rating[k], res.rating[k]])
                res.line_timeline.setdefault(k, {})[ref] = [res.rating[k], res.rating[k]]

        delta = w / (w + l) - expected_game_share(gap, params.k if singles else params.k_doubles)
        for k, sign in [(k, 1.0) for k in e.winner_keys] + [(k, -1.0) for k in e.loser_keys]:
            n = res.matches.get(k, 0)
            boost = (params.provisional_boost
                     if provisional.get(k, True) and n < params.provisional_matches else 1.0)
            step = sign * boost * delta
            if singles:
                shared = params.lr_singles * params.singles_to_doubles * step
                res.rating[k] += shared
                res.singles_offset[k] = (res.singles_offset.get(k, 0.0)
                                         + params.lr_singles * step - shared)
            else:
                res.rating[k] += params.lr_doubles * step
            res.matches[k] = n + 1
            if singles:
                res.singles_matches[k] = res.singles_matches.get(k, 0) + 1
            if timelines:
                res.timeline[k][(e.division, e.date)][1] = res.rating[k]
                res.line_timeline[k][ref][1] = res.rating[k]
    return res


def baselines_from_players(players: list[dict]) -> dict[str, float]:
    pbn = R._build_players_by_name(players)
    return {k: p.get("dynamic_rating_baseline") for k, p in pbn.items()}
