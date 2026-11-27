"""Unit tests for engine/ratings_v9.py (ShiQ v9).

Run:
    python3 -m unittest engine.test_ratings_v9 -v
"""
import unittest

from engine import ratings_v9 as V9
from engine.ratings import CourtEvent


def ev(w, l, score, date="3/1/2026", line="1# Doubles"):
    return CourtEvent(date=date, match_id="m", line_label=line, division="3.0",
                      winner_keys=w, loser_keys=l, score=score)


class TestGames(unittest.TestCase):
    def test_straight_sets(self):
        self.assertEqual(V9.games_won("6-4 6-2"), (12, 6))

    def test_three_sets_with_match_tiebreak(self):
        self.assertEqual(V9.games_won("6-4 3-6 1-0"), (10, 10))


class TestV9(unittest.TestCase):
    def test_close_loss_to_stronger_pair_raises_loser(self):
        bl = {"a": 3.0, "b": 3.0, "c": 3.4, "d": 3.4}
        r = V9.run_v9([ev(["c", "d"], ["a", "b"], "6-4 6-4")], bl)
        self.assertGreater(r.rating["a"], 3.0)
        self.assertLess(r.rating["c"], 3.4)

    def test_blowout_of_weak_side_barely_moves(self):
        bl = {"a": 3.4, "b": 2.6}
        r = V9.run_v9([ev(["a"], ["b"], "6-0 6-0", line="1# Singles")], bl)
        even = V9.run_v9([ev(["a"], ["b"], "6-0 6-0", line="1# Singles")], {"a": 3.0, "b": 3.0})
        # A blowout of a much weaker side moves far less than one over an equal.
        self.assertLess(abs(r.rating["a"] - 3.4), 0.5 * abs(even.rating["a"] - 3.0))

    def test_partners_get_equal_credit(self):
        bl = {"a": 3.3, "b": 2.9, "c": 3.1, "d": 3.1}
        r = V9.run_v9([ev(["a", "b"], ["c", "d"], "6-1 6-1")], bl)
        self.assertAlmostEqual(r.rating["a"] - 3.3, r.rating["b"] - 2.9)

    def test_singles_offset_only_moves_on_singles(self):
        bl = {"a": 3.0, "b": 3.0, "c": 3.0, "d": 3.0}
        r = V9.run_v9([ev(["a", "b"], ["c", "d"], "6-0 6-0")], bl)
        self.assertEqual(r.singles_offset.get("a", 0.0), 0.0)
        r = V9.run_v9([ev(["a"], ["c"], "6-0 6-0", line="1# Singles")], bl)
        self.assertGreater(r.singles_offset["a"], 0.0)
        self.assertGreater(r.singles("a"), r.doubles("a"))

    def test_provisional_player_learns_faster(self):
        bl = {"new": 2.60, "est": 2.63, "x": 2.6, "y": 2.6}
        r1 = V9.run_v9([ev(["new"], ["x"], "6-2 6-2", line="1# Singles")], bl)
        r2 = V9.run_v9([ev(["est"], ["y"], "6-2 6-2", line="1# Singles")], bl)
        self.assertGreater(r1.rating["new"] - 2.60, r2.rating["est"] - 2.63)

    def test_win_probability_symmetric(self):
        self.assertAlmostEqual(V9.win_probability(0.2) + V9.win_probability(-0.2), 1.0)


if __name__ == "__main__":
    unittest.main()


class TestTimelines(unittest.TestCase):
    def test_pre_and_post_by_date(self):
        bl = {"a": 3.0, "b": 3.0, "c": 3.0}
        events = [
            ev(["a"], ["b"], "6-2 6-2", date="3/1/2026", line="1# Singles"),
            ev(["a"], ["c"], "6-2 6-2", date="3/1/2026", line="2# Singles"),
            ev(["b"], ["a"], "6-4 6-4", date="3/8/2026", line="1# Singles"),
        ]
        r = V9.run_v9(events, bl, timelines=True)
        pre, post = r.timeline["a"][("3.0", "3/1/2026")]
        self.assertEqual(pre, 3.0)
        pre2, post2 = r.timeline["a"][("3.0", "3/8/2026")]
        self.assertEqual(pre2, post)          # rating carried into next date
        self.assertAlmostEqual(post2, r.rating["a"])
        self.assertEqual(r.singles_matches["a"], 3)

    def test_side_win_probability_is_average_gap(self):
        self.assertAlmostEqual(V9.side_win_probability([3.2, 3.0], [3.1, 3.1]), 0.5)
        self.assertGreater(V9.side_win_probability([3.3], [3.0]), 0.65)  # slope 3, fit 2026-11-27


def test_line_timeline_two_matches_same_day():
    from engine import ratings as R
    from engine.ratings_v9 import run_v9
    ev = [R.CourtEvent(date="11/6/2026", match_id="C1", line_label="#3 Doubles", division="3.0",
                       winner_keys=["a", "b"], loser_keys=["c", "d"], score="6-4 6-1"),
          R.CourtEvent(date="11/6/2026", match_id="C2", line_label="#3 Doubles", division="3.0",
                       winner_keys=["a", "b"], loser_keys=["e", "f"], score="6-0 6-0")]
    res = run_v9(ev, {k: 3.0 for k in "abcdef"}, timelines=True)
    l1, l2 = res.line_timeline["a"]["C1|#3 Doubles"], res.line_timeline["a"]["C2|#3 Doubles"]
    assert l1[1] == l2[0]                      # second match starts where the first ended
    assert l1[0] < l1[1] < l2[1]
    day = res.timeline["a"][("3.0", "11/6/2026")]
    assert day == [l1[0], l2[1]]
