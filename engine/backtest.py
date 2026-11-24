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
    args = ap.parse_args()
    res = run(R._load(R.PLAYERS_JSON, []))
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
