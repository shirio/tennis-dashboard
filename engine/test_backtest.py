"""Unit tests for engine/backtest.py scoring helpers.

Run:
    python3 -m unittest engine.test_backtest -v
"""
import unittest

from engine.backtest import Score, win_prob_for_winner


class TestBacktestScore(unittest.TestCase):
    def test_even_match_is_coin_flip(self):
        self.assertAlmostEqual(win_prob_for_winner([3.0], [3.0]), 0.50)

    def test_upset_scores_as_miss(self):
        sc = Score()
        sc.add(win_prob_for_winner([3.0], [3.3]))   # underdog won
        s = sc.summary()
        self.assertEqual(s["accuracy"], 0.0)
        self.assertEqual(s["calibration"][0]["actual"], 0.0)

    def test_favourite_win_counts(self):
        sc = Score()
        sc.add(0.75)
        sc.add(0.75)
        s = sc.summary()
        self.assertEqual(s["accuracy"], 1.0)
        self.assertAlmostEqual(s["brier"], 0.0625)

    def test_doubles_uses_cross_pair(self):
        p = win_prob_for_winner([3.2, 3.0], [3.0, 3.0])
        self.assertTrue(0.5 < p < 0.68)


if __name__ == "__main__":
    unittest.main()
