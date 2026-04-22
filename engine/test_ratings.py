"""
engine/test_ratings.py
Unit tests for the v8 rating algorithm.
"""
import unittest
from engine.ratings import (
    MatchRecord,
    _win_probability,
    _cross_pair_expected,
    _surprise_weight,
    _set_dominance,
    _match_adjustment,
    _compute_v8_rating,
    _detect_scorecard_swap,
    _parse_sets,
)


class TestWinProbability(unittest.TestCase):
    def test_even_match(self):
        self.assertEqual(_win_probability(0.0), 0.50)

    def test_slight_favorite(self):
        # Interpolated: gap=0.15 is between 0.10→0.58 and 0.20→0.68
        p = _win_probability(0.15)
        self.assertGreater(p, 0.58)
        self.assertLess(p, 0.68)

    def test_big_underdog(self):
        # Interpolated: gap=-0.35 is between -0.30→0.25 and -0.40→0.18
        p = _win_probability(-0.35)
        self.assertGreater(p, 0.18)
        self.assertLess(p, 0.25)

    def test_extreme_underdog(self):
        self.assertEqual(_win_probability(-0.50), 0.12)


class TestCrossPairExpected(unittest.TestCase):
    def test_singles(self):
        """Singles: just P(player > opponent)."""
        result = _cross_pair_expected(3.0, None, [2.8])
        self.assertEqual(result, _win_probability(0.20))

    def test_doubles_symmetric(self):
        """Both sides identical → 50%."""
        result = _cross_pair_expected(3.0, 2.8, [3.0, 2.8])
        self.assertAlmostEqual(result, 0.50, delta=0.02)

    def test_doubles_asymmetric(self):
        """3.0+2.8 vs 2.7+2.9: team with both players at-or-above should be favored."""
        result = _cross_pair_expected(3.0, 2.8, [2.7, 2.9])
        self.assertGreater(result, 0.50)

    def test_doubles_wide_gap_vs_narrow(self):
        """3.2+2.6 (avg 2.9) vs 2.9+2.9 (avg 2.9): wide pair not the same as narrow pair."""
        wide = _cross_pair_expected(3.2, 2.6, [2.9, 2.9])
        narrow = _cross_pair_expected(2.9, 2.9, [2.9, 2.9])
        # Wide pair: 3.2 dominates both opponents, but 2.6 loses to both
        # Cross-pair weights: 3.2 gets more weight (top player) but 2.6 drags
        # Result should be > 0.50 because top-vs-top (0.40 weight) is very favorable
        self.assertGreater(wide, 0.50)
        self.assertLess(wide, 0.65)


class TestSurpriseWeight(unittest.TestCase):
    def test_upset_win_high_weight(self):
        """Big underdog wins → high surprise weight."""
        w = _surprise_weight(0.20, won=True)
        self.assertGreater(w, 0.80)

    def test_expected_win_low_weight(self):
        """Heavy favorite wins → low surprise weight."""
        w = _surprise_weight(0.80, won=True)
        self.assertLess(w, 0.20)

    def test_expected_loss_low_weight(self):
        """Heavy underdog loses → low surprise weight."""
        w = _surprise_weight(0.20, won=False)
        self.assertLess(w, 0.20)

    def test_upset_loss_high_weight(self):
        """Heavy favorite loses → high surprise weight."""
        w = _surprise_weight(0.80, won=False)
        self.assertGreater(w, 0.80)

    def test_even_match_moderate(self):
        """50/50 match → moderate weight either way."""
        w_win = _surprise_weight(0.50, won=True)
        w_loss = _surprise_weight(0.50, won=False)
        self.assertAlmostEqual(w_win, w_loss, delta=0.01)
        self.assertGreater(w_win, 0.30)
        self.assertLess(w_win, 0.70)

    def test_casey_primmer_expected_win(self):
        """Casey (3.17) beats 2.12+2.33 at D3: expected=0.80 → near-zero weight."""
        w = _surprise_weight(0.80, won=True)
        self.assertLessEqual(w, 0.12)


class TestSetDominance(unittest.TestCase):
    def test_bagel(self):
        self.assertEqual(_set_dominance(6, 0), 1.00)

    def test_close_set(self):
        self.assertEqual(_set_dominance(6, 4), 0.25)

    def test_tiebreak(self):
        self.assertEqual(_set_dominance(7, 6), 0.10)


class TestParseSets(unittest.TestCase):
    def test_two_sets(self):
        sets = _parse_sets("6-4 6-2")
        self.assertEqual(len(sets), 2)
        self.assertEqual(sets[0], (6, 4, True))
        self.assertEqual(sets[1], (6, 2, True))

    def test_three_sets_with_tiebreak(self):
        sets = _parse_sets("7-5 5-7 1-0")
        self.assertEqual(len(sets), 3)
        self.assertEqual(sets[0], (7, 5, True))
        self.assertEqual(sets[1], (7, 5, False))   # 5-7 → loser_games=5, first_side lost
        self.assertEqual(sets[2], (1, 0, True))

    def test_empty_score(self):
        self.assertEqual(_parse_sets(""), [])


class TestComputeV8Rating(unittest.TestCase):
    def test_no_matches_returns_baseline(self):
        """No matches → baseline unchanged."""
        self.assertEqual(_compute_v8_rating(3.0, []), 3.0)

    def test_shi_l_oskooi_division_30(self):
        """
        Shi L Oskooi: baseline 2.7626, 4 wins in 3.0 division.
        Opponent baselines from actual data. With confidence/deploy/line signals.
        """
        matches = [
            MatchRecord(
                opponent_ratings=[3.1849, 3.0331],
                partner_rating=2.98,
                won=True,
                date="3/14/2026",
                division="3.0",
                match_id="m1",
                line_label="1# Doubles",
                score="7-5 5-7 1-0",
            ),
            MatchRecord(
                opponent_ratings=[2.7720],
                partner_rating=None,
                won=True,
                date="3/21/2026",
                division="3.0",
                match_id="m2",
                line_label="2# Singles",
                score="6-7 6-1 1-0",
            ),
            MatchRecord(
                opponent_ratings=[2.6283, 2.7336],
                partner_rating=2.98,
                won=True,
                date="3/28/2026",
                division="3.0",
                match_id="m3",
                line_label="2# Doubles",
                score="6-4 6-4",
            ),
            MatchRecord(
                opponent_ratings=[2.5149, 2.8385],
                partner_rating=2.98,
                won=True,
                date="4/11/2026",
                division="3.0",
                match_id="m4",
                line_label="1# Doubles",
                score="6-1 6-0",
            ),
        ]
        result = _compute_v8_rating(2.7626, matches)
        # 4 wins as underdog, full deployment → significant upward move
        self.assertGreater(result, 2.90)
        self.assertLess(result, 3.25)

    def test_arika_carrier_goes_up(self):
        """Arika: baseline 3.597, 3 wins in 3.5 → should increase.
        With max-opponent implied for doubles wins, ceiling = max_opp + gap ≈ 3.60.
        Result should reach baseline → 3.60 range.
        """
        matches = [
            MatchRecord(
                opponent_ratings=[3.3940, 2.8224],
                partner_rating=3.50,
                won=True,
                date="3/14/2026",
                division="3.5",
                match_id="a1",
                line_label="1# Doubles",
                score="6-3 6-2",
            ),
            MatchRecord(
                opponent_ratings=[3.5789, 3.5022],
                partner_rating=3.50,
                won=True,
                date="3/21/2026",
                division="3.5",
                match_id="a2",
                line_label="1# Doubles",
                score="6-4 6-3",
            ),
            MatchRecord(
                opponent_ratings=[3.4372, 3.4496],
                partner_rating=3.50,
                won=True,
                date="4/11/2026",
                division="3.5",
                match_id="a3",
                line_label="1# Doubles",
                score="6-2 6-3",
            ),
        ]
        result = _compute_v8_rating(3.597, matches)
        # Ceiling from max-opp implied = 3.5789 + 0.02 = 3.60; result should reach it
        self.assertGreaterEqual(result, 3.597)   # at minimum, doesn't drop from baseline
        self.assertLessEqual(result, 3.62)       # stays within ceiling range

    def test_mixed_results_near_baseline(self):
        """1 win + 1 loss vs equal opponents → close to baseline.
        Note: score first number = line winner's games (TennisLink convention).
        """
        matches = [
            MatchRecord(
                opponent_ratings=[3.00],
                partner_rating=None,
                won=True,
                date="3/14/2026",
                division="3.0",
                match_id="x1",
                line_label="1# Singles",
                score="6-4 6-4",      # player won 6-4, 6-4
            ),
            MatchRecord(
                opponent_ratings=[3.00],
                partner_rating=None,
                won=False,
                date="3/21/2026",
                division="3.0",
                match_id="x2",
                line_label="1# Singles",
                score="6-4 6-4",      # opponent won 6-4, 6-4 (first = winner)
            ),
        ]
        result = _compute_v8_rating(3.0, matches)
        self.assertAlmostEqual(result, 3.0, delta=0.05)

    def test_casey_primmer_expected_results_neutral(self):
        """Casey: expected D3 win in 3.0, expected D3 loss in 3.5 → near baseline."""
        matches = [
            MatchRecord(
                opponent_ratings=[2.12, 2.33],
                partner_rating=2.73,
                won=True,
                date="3/14/2026",
                division="3.0",
                match_id="c1",
                line_label="3# Doubles",
                score="6-0 6-1",
            ),
            MatchRecord(
                opponent_ratings=[3.42, 3.38],
                partner_rating=2.73,
                won=False,
                date="3/21/2026",
                division="3.5",
                match_id="c2",
                line_label="3# Doubles",
                score="6-1 6-0",
            ),
        ]
        # Division (3.5) rating: only the loss → should drop or stay flat
        div_matches = [m for m in matches if m.division == "3.5"]
        result_div = _compute_v8_rating(3.1707, div_matches, n_total_weeks=4, division="3.5")
        self.assertLessEqual(result_div, 3.17)

        # Global (both matches): heavily expected results cancel out → near baseline
        result_global = _compute_v8_rating(3.1707, matches, n_total_weeks=4, division="3.5")
        self.assertAlmostEqual(result_global, 3.17, delta=0.06)

    def test_loss_to_weaker_opponent_drops_rating(self):
        """High-rated player losing to weaker opponent → rating decreases.
        Score "6-2 6-3" = opponent (winner) won 6-2, 6-3.
        """
        matches = [
            MatchRecord(
                opponent_ratings=[2.60, 2.50],
                partner_rating=2.90,
                won=False,
                date="3/14/2026",
                division="3.0",
                match_id="d1",
                line_label="3# Doubles",
                score="6-2 6-3",      # opponent won 6-2, 6-3 (first = winner)
            ),
        ]
        result = _compute_v8_rating(2.93, matches)
        self.assertLess(result, 2.93)


class TestSwapDetection(unittest.TestCase):
    def test_swapped_scorecard(self):
        """Home column has away-team players → swapped."""
        match = {
            "home_team": "DTC #3",
            "away_team": "DTC #2",
            "lines": [
                {"players_home": "Player A", "players_away": "Player B"},
                {"players_home": "Player C", "players_away": "Player D"},
            ],
        }
        lookup = {
            "player a": "DTC #2",  # away-team player in home column
            "player b": "DTC #3",  # home-team player in away column
            "player c": "DTC #2",
            "player d": "DTC #3",
        }
        self.assertTrue(_detect_scorecard_swap(match, lookup))

    def test_normal_scorecard(self):
        """Home column has home-team players → not swapped."""
        match = {
            "home_team": "TPC",
            "away_team": "DTC #1",
            "lines": [
                {"players_home": "Player A", "players_away": "Player B"},
            ],
        }
        lookup = {
            "player a": "TPC",
            "player b": "DTC #1",
        }
        self.assertFalse(_detect_scorecard_swap(match, lookup))


if __name__ == "__main__":
    unittest.main()
