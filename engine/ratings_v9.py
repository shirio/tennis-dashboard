"""
engine/ratings_v9.py
ShiQ v9: games-based sequential rating with a per-player singles offset.

Runs BESIDE the v8 engine (engine/ratings.py) until it wins on the
backtest scorecard (engine/backtest.py). Nothing here changes v8 output.

How it works
------------
* Every player has one shared rating ``r`` (starts at the Dec baseline) and a
  singles offset ``o`` (starts at 0). Doubles strength = r, singles = r + o.
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
  the strength gap, ``1 / (1 + exp(-WIN_SLOPE * gap))``, fitted on our data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine import ratings as R

# ---------------------------------------------------------------------------
# Parameters (tuned on the first half of spring 2026, see engine/backtest.py)
# ---------------------------------------------------------------------------
K = 1.2                 # game-share sensitivity to strength gap
LR = 0.20               # step size for the shared rating
LR_SINGLES_OFFSET = 0.10  # extra step for the singles offset on singles lines
PROVISIONAL_BOOST = 2.0   # step multiplier for players starting on an NTRP default
PROVISIONAL_MATCHES = 5   # ... for this many matches
WIN_SLOPE = 7.0          # logistic slope for win probability

# Baselines that are just the NTRP fallback (ratings._NTRP_DEFAULT_RATING)
_DEFAULT_BASELINES = {round(v, 2) for v in R._NTRP_DEFAULT_RATING.values()}


@dataclass
class Params:
    k: float = K
    lr: float = LR
    lr_offset: float = LR_SINGLES_OFFSET
    provisional_boost: float = PROVISIONAL_BOOST
    provisional_matches: int = PROVISIONAL_MATCHES


@dataclass
class V9Result:
    rating: dict[str, float] = field(default_factory=dict)          # shared r
    singles_offset: dict[str, float] = field(default_factory=dict)  # o
    matches: dict[str, int] = field(default_factory=dict)
    # One entry per scored event, in processed order:
    # (event, pre-match strength diff winner-side minus loser-side)
    pre_match: list[tuple[R.CourtEvent, float]] = field(default_factory=list)

    def singles(self, k: str) -> Optional[float]:
        if k not in self.rating:
            return None
        return self.rating[k] + self.singles_offset.get(k, 0.0)

    def doubles(self, k: str) -> Optional[float]:
        return self.rating.get(k)


def win_probability(gap: float, slope: float = WIN_SLOPE) -> float:
    """P(side wins) given strength gap (side minus opponents)."""
    return 1.0 / (1.0 + math.exp(-slope * gap))


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


def run_v9(events: list[R.CourtEvent], baselines: dict[str, float],
           params: Params = Params()) -> V9Result:
    """Process court events in the given (chronological) order."""
    res = V9Result()
    provisional = {k: is_provisional(v) for k, v in baselines.items()}

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

        delta = w / (w + l) - expected_game_share(gap, params.k)
        for k, sign in [(k, 1.0) for k in e.winner_keys] + [(k, -1.0) for k in e.loser_keys]:
            n = res.matches.get(k, 0)
            boost = (params.provisional_boost
                     if provisional.get(k, True) and n < params.provisional_matches else 1.0)
            res.rating[k] += sign * params.lr * boost * delta
            if singles:
                res.singles_offset[k] = (res.singles_offset.get(k, 0.0)
                                         + sign * params.lr_offset * boost * delta)
            res.matches[k] = n + 1
    return res


def baselines_from_players(players: list[dict]) -> dict[str, float]:
    pbn = R._build_players_by_name(players)
    return {k: p.get("dynamic_rating_baseline") for k, p in pbn.items()}
