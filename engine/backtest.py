"""
engine/backtest.py
Prediction scorecard for the ShiQ ratings engine.

For every completed court line in the standings data, predict the winner from
each player's rating going INTO that match (rating_timeline_* in players.json,
written by run_ratings) and compare with what actually happened. The same
lines are also scored with frozen Dec baselines, so any engine change can be
judged against "not updating at all".

Run (after rebuild.py, from the repo root):
    python3 -m engine.backtest
    python3 -m engine.backtest --json   # machine-readable
    python3 -m engine.backtest --holdout  # baseline vs v8 vs v9, second half
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from engine import ratings as R

RatingFn = Callable[[str, R.CourtEvent], Optional[float]]


@dataclass
class Score:
    n: int = 0
    correct: int = 0
    brier: float = 0.0
    logloss: float = 0.0
    # calibration buckets keyed by favourite probability (0.50, 0.55, ...)
    buckets: dict[float, list[float]] = field(default_factory=dict)

    def add(self, p_winner: float) -> None:
        self.n += 1
        self.correct += p_winner > 0.5
        self.brier += (1.0 - p_winner) ** 2
        self.logloss += -math.log(max(p_winner, 1e-6))
        fav_p, fav_won = (p_winner, 1.0) if p_winner >= 0.5 else (1.0 - p_winner, 0.0)
        b = round(min(math.floor(fav_p * 20) / 20, 0.95), 2)
        s = self.buckets.setdefault(b, [0, 0.0, 0.0])
        s[0] += 1
        s[1] += fav_won
        s[2] += fav_p

    def summary(self) -> dict:
        if not self.n:
            return {"n": 0}
        return {
            "n": self.n,
            "accuracy": round(self.correct / self.n, 4),
            "brier": round(self.brier / self.n, 4),
            "logloss": round(self.logloss / self.n, 4),
            "calibration": [
                {"predicted": round(ps / k, 3), "actual": round(w / k, 3), "n": k}
                for b, (k, w, ps) in sorted(self.buckets.items())
            ],
        }


def win_prob_for_winner(winner_r: list[float], loser_r: list[float]) -> float:
    """Engine's pre-match probability that the side that won would win."""
    if len(winner_r) == 1:
        return R._win_probability(winner_r[0] - loser_r[0])
    return R._cross_pair_expected(winner_r[0], winner_r[1], loser_r)


def load_events(players: list[dict]):
    pbn = R._build_players_by_name(players)
    events = R._collect_court_events(R._discover_standings_files(), pbn)
    events.sort(key=R._date_sort_key)
    return pbn, events


def score_events(events, rating_fn: RatingFn) -> Score:
    sc = Score()
    for e in events:
        if not e.winner_keys or len(e.winner_keys) != len(e.loser_keys):
            continue   # a side has an unknown player
        w = [rating_fn(k, e) for k in e.winner_keys]
        l = [rating_fn(k, e) for k in e.loser_keys]
        if None in w or None in l:
            continue
        sc.add(win_prob_for_winner(w, l))
    return sc


def fit_slope(gaps: list[float]) -> float:
    """Logistic slope a for P(win) = 1/(1+exp(-a*gap)), max likelihood by grid.

    ``gaps`` are winner-side minus loser-side strength differences.
    """
    best = (float("inf"), 1.0)
    for i in range(1, 81):
        a = i * 0.25
        ll = sum(math.log1p(math.exp(-a * g)) for g in gaps)
        best = min(best, (ll, a))
    return best[1]


def holdout(gaps_by_event: list[Optional[float]], n_events: int) -> dict:
    """Fit a win curve on the first half of the season, score the second half.

    Puts every model on the same probability footing, so the comparison is
    about how well each one RANKS players, not about its own win table.
    """
    half = n_events // 2
    train = [g for g in gaps_by_event[:half] if g is not None]
    test = [g for g in gaps_by_event[half:] if g is not None]
    a = fit_slope(train)
    sc = Score()
    for g in test:
        sc.add(1.0 / (1.0 + math.exp(-a * g)))
    out = sc.summary()
    out["slope"] = a
    return out


def _gap(e, rating_fn) -> Optional[float]:
    if not e.winner_keys or len(e.winner_keys) != len(e.loser_keys):
        return None
    w = [rating_fn(k, e) for k in e.winner_keys]
    l = [rating_fn(k, e) for k in e.loser_keys]
    if None in w or None in l:
        return None
    return sum(w) / len(w) - sum(l) / len(l)


def run_holdout(players: list[dict]) -> dict:
    """Second-half-of-season comparison: frozen baseline vs ShiQ v8 vs v9."""
    from engine import ratings_v9 as V9
    pbn, events = load_events(players)
    events = [e for e in events if sum(V9.games_won(e.score)) > 0]
    n = len(events)

    def baseline(k, e):
        return pbn[k].get("dynamic_rating_baseline")

    def shiq_pre(k, e):
        return pbn[k].get("rating_timeline_" + e.division.replace(".", ""), {}).get(e.date)

    v9 = V9.run_v9(events, V9.baselines_from_players(players))
    v9_gap = {id(e): g for e, g in v9.pre_match}
    res = {
        "baseline": holdout([_gap(e, baseline) for e in events], n),
        "shiq_v8": holdout([_gap(e, shiq_pre) for e in events], n),
        "shiq_v9": holdout([v9_gap.get(id(e)) for e in events], n),
    }
    for fmt, singles in (("singles", True), ("doubles", False)):
        idx = [i for i, e in enumerate(events) if (len(e.winner_keys) == 1) == singles]
        sub = [events[i] for i in idx]
        res[f"shiq_v8_{fmt}"] = holdout([_gap(e, shiq_pre) for e in sub], len(sub))
        res[f"shiq_v9_{fmt}"] = holdout([v9_gap.get(id(e)) for e in sub], len(sub))
    return res


def run(players: list[dict]) -> dict:
    pbn, events = load_events(players)

    def baseline(k, e):
        return pbn[k].get("dynamic_rating_baseline")

    def shiq_pre(k, e):
        tl = pbn[k].get("rating_timeline_" + e.division.replace(".", ""), {})
        return tl.get(e.date)

    return {
        "baseline": score_events(events, baseline).summary(),
        "shiq_pre_match": score_events(events, shiq_pre).summary(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--holdout", action="store_true",
                    help="compare baseline / v8 / v9 on the second half of the season")
    args = ap.parse_args()
    players = R._load(R.PLAYERS_JSON, [])
    if args.holdout:
        res = run_holdout(players)
        if args.json:
            print(json.dumps(res, indent=2))
            return
        print("Second half of season, win curve fitted on first half:")
        for name, s in res.items():
            print(f"  {name:18s} lines={s['n']:5d}  accuracy={s['accuracy']:.1%}  "
                  f"brier={s['brier']:.4f}  logloss={s['logloss']:.4f}")
        return
    res = run(players)
    if args.json:
        print(json.dumps(res, indent=2))
        return
    for name, s in res.items():
        if not s.get("n"):
            print(f"{name}: no scorable lines")
            continue
        print(f"{name:15s} lines={s['n']}  accuracy={s['accuracy']:.1%}  "
              f"brier={s['brier']:.4f}  logloss={s['logloss']:.4f}")
    print("\nCalibration (ShiQ pre-match, favourite's view):")
    for c in res["shiq_pre_match"].get("calibration", []):
        print(f"  predicted {c['predicted']:.0%}  actual {c['actual']:.0%}  (n={c['n']})")


if __name__ == "__main__":
    main()
