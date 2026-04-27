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
    _sequential_match_adj,
    _compute_v8_rating,
    _detect_scorecard_swap,
    _parse_sets,
    _scenario_signal,
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
        # With larger _SCORE_GAP calibration, 3 solid wins against 3.54-3.58 opponents
        # push the implied floor to ~3.72; result should be above baseline and capped
        # well below that ceiling (the win-floor blend only moves 50% of the gap).
        self.assertGreaterEqual(result, 3.597)   # at minimum, doesn't drop from baseline
        self.assertLessEqual(result, 3.75)       # stays within ceiling range

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


class TestScenarioSignal(unittest.TestCase):
    """
    Storyline reviewer: asserts that scenario signal values respect the intended
    rank ordering.  These tests act as a guard against future changes to the
    signal table that would violate the match-narrative hierarchy.

    Helper: _sig(score, won) parses the score and calls _scenario_signal.
    """

    def _sig(self, score: str, won: bool) -> float:
        sets = _parse_sets(score)
        return _scenario_signal(sets, won)

    # ---- Straight-set win ordering ----------------------------------------

    def test_straight_win_rank_order(self):
        """Rout+Rout > Even+Rout > Rout+Even > Even+Even
        Note: 6-3 is a rout (dom=0.40 >= threshold). Use 6-4 (dom=0.25) for "even".
        """
        rout_rout = self._sig("6-1 6-2", won=True)    # rank 1: Rout S1 + Rout S2
        even_rout = self._sig("6-4 6-2", won=True)    # rank 2: Even S1 + Rout S2
        rout_even = self._sig("6-1 6-4", won=True)    # rank 3: Rout S1 + Even S2
        even_even = self._sig("6-4 6-4", won=True)    # rank 4: Even S1 + Even S2

        self.assertGreater(rout_rout, even_rout, "Rout+Rout should beat Even+Rout")
        self.assertGreater(even_rout, rout_even, "Even+Rout should beat Rout+Even (finishing dominant)")
        self.assertGreater(rout_even, even_even, "Rout+Even should beat Even+Even")

    def test_straight_win_values(self):
        self.assertAlmostEqual(self._sig("6-0 6-1", won=True),  +1.00)
        self.assertAlmostEqual(self._sig("6-4 6-2", won=True),  +0.85)   # Even S1 + Rout S2
        self.assertAlmostEqual(self._sig("6-2 6-4", won=True),  +0.75)   # Rout S1 + Even S2
        self.assertAlmostEqual(self._sig("6-4 6-4", won=True),  +0.60)   # Even S1 + Even S2

    # ---- Straight-set loss ordering ----------------------------------------

    def test_straight_loss_rank_order(self):
        """Rout+Rout < Even+Rout < Rout+Even < Even+Even (all negative)
        Note: 6-3 is a rout (dom=0.40 >= threshold). Use 6-4 (dom=0.25) for "even".
        """
        rout_rout = self._sig("6-1 6-2", won=False)   # rank 1: Rout loss + Rout loss
        even_rout = self._sig("6-4 6-2", won=False)   # rank 2: Even loss S1 + Rout loss S2
        rout_even = self._sig("6-2 6-4", won=False)   # rank 3: Rout loss S1 + Even loss S2
        even_even = self._sig("6-4 6-4", won=False)   # rank 4: Even loss + Even loss

        self.assertLess(rout_rout, even_rout, "Rout+Rout loss worse than Even+Rout loss")
        self.assertLess(even_rout, rout_even, "Even+Rout loss worse than Rout+Even loss (fell apart at end)")
        self.assertLess(rout_even, even_even, "Rout+Even loss worse than Even+Even loss")

    def test_straight_loss_values(self):
        self.assertAlmostEqual(self._sig("6-0 6-1", won=False), -1.00)
        self.assertAlmostEqual(self._sig("6-4 6-2", won=False), -0.85)   # Even loss S1 + Rout loss S2
        self.assertAlmostEqual(self._sig("6-2 6-4", won=False), -0.75)   # Rout loss S1 + Even loss S2
        self.assertAlmostEqual(self._sig("6-4 6-4", won=False), -0.60)   # Even loss + Even loss

    # ---- 3-set tiebreak win ordering (ranks 1-8) ---------------------------

    def test_3set_win_rank_order(self):
        """Top 4 wins (by rank) each better than the one below."""
        # Rank 1: Even loss S1 + Rout win S2 → "7-6 6-1 1-0" from first side
        r1 = self._sig("7-6 6-1 1-0", won=True)   # s1: 7-6 even loss for winner? No...
        # Wait: in "7-6 6-1 1-0" with won=True, first side is the match winner.
        # s1=(7,6,True): first_side won set1 → player (first side) won set1 EVENLY
        # That's Even WIN in S1, not Even LOSS. We need Even LOSS S1 + Rout WIN S2 for rank 1.
        # Even LOSS S1 means player LOST s1. Since score is from winner's perspective (first number = winner's games in each set),
        # "lost set 1 evenly" from player perspective means SECOND side won set 1 evenly → "6-7" prefix
        # "6-7 6-1 1-0": s1=(7,6,False) — second side won s1 evenly. If player won match (won=True),
        # player is first side → first_side_won=False → player LOST s1. dom(7,6)=0.10<0.40 → Even loss ✓
        # s2=(6,1,True) — first side won s2. player won s2. dom(6,1)=0.75≥0.40 → Rout win ✓
        r1 = self._sig("6-7 6-1 1-0", won=True)   # Even loss S1 + Rout win S2

        # Rank 2: Rout win S1 + Even loss S2 → "6-1 7-6 1-0" with won=True
        r2 = self._sig("6-1 6-7 1-0", won=True)   # Rout win S1 + Even loss S2

        # Rank 3: Rout loss S1 + Rout win S2 → "6-7 ... wait, rout loss means player lost 6-1 style
        # "1-6 6-1 1-0": s1=(6,1,False) second side won; player is first side (won=True): first_side_won=False → player lost s1. dom(6,1)=0.75≥0.40 → Rout loss ✓. s2=(6,1,True): first side won → player won s2. dom=0.75 → Rout win ✓
        r3 = self._sig("1-6 6-1 1-0", won=True)   # Rout loss S1 + Rout win S2

        # Rank 5: Rout loss S1 + Even win S2 → "1-6 6-4 1-0"
        r5 = self._sig("1-6 6-4 1-0", won=True)   # Rout loss S1 + Even win S2

        # Rank 8: Even win S1 + Rout loss S2 → "6-4 1-6 1-0"
        r8 = self._sig("6-4 1-6 1-0", won=True)   # Even win S1 + Rout loss S2

        self.assertGreater(r1, r2, "rank1 > rank2 in 3-set wins")
        self.assertGreater(r2, r3, "rank2 > rank3 in 3-set wins")
        self.assertGreater(r3, r5, "rank3 > rank5 in 3-set wins")
        self.assertGreater(r5, r8, "rank5 > rank8 in 3-set wins")

    def test_3set_win_positive(self):
        """All 3-set tiebreak wins produce positive signals."""
        scores_won = [
            "6-7 6-1 1-0",   # rank 1
            "6-1 6-7 1-0",   # rank 2
            "1-6 6-1 1-0",   # rank 3
            "6-7 6-4 1-0",   # rank 4: Even loss S1 + Even win S2
            "1-6 6-4 1-0",   # rank 5
            "6-4 6-7 1-0",   # rank 6: Even win S1 + Even loss S2
            "6-1 1-6 1-0",   # rank 7: Rout win S1 + Rout loss S2
            "6-4 1-6 1-0",   # rank 8
        ]
        for sc in scores_won:
            sig = self._sig(sc, won=True)
            self.assertGreater(sig, 0, f"Expected positive signal for win: {sc}, got {sig}")

    # ---- 3-set tiebreak loss ordering (ranks 1-8) --------------------------

    def test_3set_loss_rank_order(self):
        """Top ranks (most negative) are worse than bottom ranks."""
        # Loss rank 1: Even win S1 + Rout loss S2 → "6-4 6-1 0-1" — player is LOSER
        # Score stored as winner's perspective: winner got "6-4 6-1 1-0"
        # For player who LOST (won=False): first_side=winner, so "6-4 6-1 1-0":
        # s1=(6,4,True): first side won. Player is second side (won=False). Player_won_s1=(True==False)=False. Even loss (dom=0.25). s2=(6,1,True): player lost. dom=0.75 → Rout loss.
        # That's Even loss S1 + Rout loss S2 for the LOSER — but we want "Even win S1 + Rout loss S2" for the loser.
        # "Even WIN S1" means player WON set 1 evenly. Player is second side (won=False), so player wins set 1 when first_side_won=False.
        # Score: "6-7 6-1 1-0" — from winner's view: winner lost s1 (6-7), won s2 (6-1), won tiebreak.
        # For loser (won=False): s1=(7,6,False): first_side_won=False → player_won_s1=(False==False)=True. dom(7,6)=0.10 → Even win ✓. s2=(6,1,True): player_won_s2=(True==False)=False. dom=0.75 → Rout loss ✓.
        l1 = self._sig("6-7 6-1 1-0", won=False)   # Even win S1 + Rout loss S2 for loser

        # Loss rank 2: Rout win S1 + Rout loss S2 → loser won s1 by rout, lost s2 by rout
        # "1-6 6-1 1-0" from winner's view: winner lost s1 (1-6), won s2 (6-1), won tiebreak.
        # Loser (won=False): s1=(6,1,False): first_side_won=False → player_won=(False==False)=True. dom=0.75 → Rout win ✓. s2=(6,1,True): player_won=(True==False)=False. dom=0.75 → Rout loss ✓.
        l2 = self._sig("1-6 6-1 1-0", won=False)   # Rout win S1 + Rout loss S2 for loser

        # Loss rank 8: Even loss S1 + Rout win S2 for loser → opponent won s1 evenly, loser won s2 by rout
        # "6-4 1-6 1-0": winner won s1 (6-4), lost s2 (1-6), won tiebreak.
        # Loser: s1=(6,4,True): player_won=(True==False)=False. dom(6,4)=0.25 → Even loss ✓. s2=(6,1,False): player_won=(False==False)=True. dom=0.75 → Rout win ✓.
        l8 = self._sig("6-4 1-6 1-0", won=False)   # Even loss S1 + Rout win S2 for loser

        self.assertLess(l1, l2, "loss rank1 more negative than rank2")
        self.assertLess(l2, l8, "loss rank2 more negative than rank8")

    def test_3set_loss_negative(self):
        """All 3-set tiebreak losses produce negative signals."""
        scores_lost = [
            "6-7 6-1 1-0",   # Even win S1 + Rout loss S2 for loser (rank 1 most negative)
            "1-6 6-1 1-0",   # Rout win S1 + Rout loss S2 for loser
            "6-1 6-4 1-0",   # Rout loss S1 + Even win S2 for loser
            "6-7 6-4 1-0",   # Even win S1 + Even loss S2 for loser
            "6-4 6-7 1-0",   # Even loss S1 + Even win S2 for loser
            "6-1 6-7 1-0",   # Rout loss S1 + Even... wait, winner won s1 6-1, lost s2 6-7
                              # loser: s1=(6,1,True) player_won=(True==False)=False dom=0.75 Rout loss
                              #        s2=(7,6,False) player_won=(False==False)=True dom=0.10 Even win
                              # Rout loss S1 + Even win S2 → already covered above but different key
            "1-6 6-7 1-0",   # Rout win S1 + Even loss S2 for loser
            "6-4 1-6 1-0",   # Even loss S1 + Rout win S2 for loser (rank 8 least negative)
        ]
        for sc in scores_lost:
            sig = self._sig(sc, won=False)
            self.assertLess(sig, 0, f"Expected negative signal for loss: {sc}, got {sig}")

    # ---- Symmetry: wins always > losses for matched scenarios ----------------

    def test_wins_always_positive_losses_always_negative(self):
        """Any win signal > 0, any loss signal < 0."""
        for score, is_rout in [("6-0 6-1", True), ("6-4 6-3", False)]:
            self.assertGreater(self._sig(score, won=True), 0)
            self.assertLess(self._sig(score, won=False), 0)

    def test_dominant_win_better_than_close_win(self):
        """6-0 6-1 win signals higher than 6-4 6-3 win."""
        self.assertGreater(
            self._sig("6-0 6-1", won=True),
            self._sig("6-4 6-3", won=True),
        )


class TestUnderdogFavorite(unittest.TestCase):
    """
    Guard against rating inflation from expected wins, and verify that underdogs
    are always rewarded for upsets or close losses.

    Rules being tested:
      1. Underdog upset win → always positive sequential adj.
      2. Underdog close loss → never negative sequential adj (0 or positive).
      3. Heavy favorite crushing underdog → tiny adj (< 0.01 per match).
      4. Heavy favorite loses any match → always negative adj.
      5. Even match winner → moderate positive adj (~0.025).
    """

    _HEAVY_FAV  = 3.10    # heavy favourite
    _WEAK_OPP   = 2.70    # weaker opponent (gap=0.40 → ~82% favourite)
    _NEAR_OPP   = 2.92    # closer but still below (gap=0.18 → ~68% favourite)
    SEQ_CAP = 0.05

    def _rec(self, player_r, opp_r, won, score, partner_r=None):
        return MatchRecord(
            opponent_ratings=[opp_r], partner_rating=partner_r,
            won=won, date="1/1/2026", division="3.0",
            match_id="t", line_label="1# Singles", score=score,
        )

    # ------------------------------------------------------------------
    # Rule 1: Underdog upset win → always positive
    # ------------------------------------------------------------------

    def test_underdog_upset_win_rout_positive(self):
        """Underdog (2.70) beats favourite (3.10) 6-1 6-2 → positive adj."""
        rec = self._rec(2.70, self._HEAVY_FAV, won=True, score="6-1 6-2")
        adj = _sequential_match_adj(2.70, rec)
        self.assertGreater(adj, 0, "Underdog upset win must produce a positive adj")

    def test_underdog_upset_win_even_positive(self):
        """Underdog (2.70) beats favourite (3.10) 6-4 6-4 → positive adj."""
        rec = self._rec(2.70, self._HEAVY_FAV, won=True, score="6-4 6-4")
        adj = _sequential_match_adj(2.70, rec)
        self.assertGreater(adj, 0, "Underdog close upset win must produce a positive adj")

    def test_underdog_upset_win_near_full_cap(self):
        """Underdog upset win earns well above half the SEQ_CAP (> 60% of cap).
        win_cap = SEQ_CAP × (1 − 0.18)² ≈ 0.101 (67% of cap).
        """
        rec = self._rec(2.70, self._HEAVY_FAV, won=True, score="6-2 6-3")
        adj = _sequential_match_adj(2.70, rec)
        self.assertGreater(adj, self.SEQ_CAP * 0.60,
                           "Upset win should earn well above half the sequential cap")

    # ------------------------------------------------------------------
    # Rule 2: Underdog close loss → never negative
    # ------------------------------------------------------------------

    def test_underdog_close_loss_not_penalized(self):
        """Underdog (2.70) loses 6-4 6-4 to favourite (3.10) → adj ≥ 0."""
        rec = self._rec(2.70, self._HEAVY_FAV, won=False, score="6-4 6-4")
        adj = _sequential_match_adj(2.70, rec)
        self.assertGreaterEqual(adj, 0,
                                "Underdog losing as expected must not be penalized")

    def test_underdog_rout_loss_not_penalized(self):
        """Underdog (2.70) gets bageled 6-0 6-1 → adj still ≥ 0."""
        rec = self._rec(2.70, self._HEAVY_FAV, won=False, score="6-0 6-1")
        adj = _sequential_match_adj(2.70, rec)
        self.assertGreaterEqual(adj, 0,
                                "Underdog rout loss must not be penalized (expected loss)")

    def test_underdog_very_close_loss_positive(self):
        """Underdog losing a tight tiebreak match can get a positive adj (overperformed expectation)."""
        # Even loss S1 + Even win S2 → 3-set tiebreak: rank-5 loss signal = -0.35
        # But expected_signal for big underdog is very negative → surprise is positive
        rec = self._rec(2.70, self._HEAVY_FAV, won=False, score="6-4 1-6 0-1")
        adj = _sequential_match_adj(2.70, rec)
        self.assertGreaterEqual(adj, 0,
                                "Underdog who pushes to a tiebreak should get neutral or positive adj")

    # ------------------------------------------------------------------
    # Rule 3: Heavy favourite crushing underdog → tiny adj
    # ------------------------------------------------------------------

    def test_heavy_favourite_rout_win_small(self):
        """3.10 crushing 2.70 with 6-0 6-1 → small adj (< 0.03).
        win_cap = 0.15 × (1 − 0.82) = 0.027 — dominant win earns something
        but is kept small by the scaled cap.
        """
        rec = self._rec(self._HEAVY_FAV, self._WEAK_OPP, won=True, score="6-0 6-1")
        adj = _sequential_match_adj(self._HEAVY_FAV, rec)
        self.assertGreater(adj, 0,
                           "Dominant win clears the surprise gate — earns a small positive")
        self.assertLess(adj, 0.03,
                        "But win_cap keeps it small when opponent is much weaker")

    def test_heavy_favourite_wins_even_drops(self):
        """3.10 beats 2.70 with 6-4 6-4 → adj < 0 (underperformed).
        Even+even win by an 82% favourite: raw_signal(0.60) < expected_signal(0.64)
        → negative surprise → slight rating drop.  Winning CAN hurt you if you
        won less impressively than your level implies.
        """
        rec = self._rec(self._HEAVY_FAV, self._WEAK_OPP, won=True, score="6-4 6-4")
        adj = _sequential_match_adj(self._HEAVY_FAV, rec)
        self.assertLess(adj, 0.0,
                        "Heavy favourite winning barely-evenly against weak opponent should slightly drop")

    def test_favourite_near_opp_win_limited(self):
        """3.10 beats 2.92 (gap=0.18, ~68% fav) → adj < 0.06.
        win_cap = 0.15 × (1 − 0.68) = 0.048 — moderate cap for a near-peer win.
        """
        rec = self._rec(self._HEAVY_FAV, self._NEAR_OPP, won=True, score="6-2 6-3")
        adj = _sequential_match_adj(self._HEAVY_FAV, rec)
        self.assertGreater(adj, 0,
                           "Dominant win against near-peer clears the surprise gate")
        self.assertLess(adj, 0.06,
                        "win_cap keeps it modest when opponent is somewhat weaker")

    def test_yarisbel_scenario_barely_moves(self):
        """
        Regression: 5 wins by 3.10 over 2.72–2.97 opponents should produce
        only tiny gains — total < 0.08, landing near 3.15.
        win_cap = SEQ_CAP × (1 − expected)² makes expected wins small:
          expected=0.82 → win_cap ≈ 0.005
          expected=0.64 → win_cap ≈ 0.019
        She's way above everyone she's beaten — it tells us nothing of her ceiling.
        """
        matches = [
            ("6-4 6-3", [2.78]),   # even+rout, expected≈0.76 → win_cap≈0.009
            ("6-3 6-1", [2.72]),   # rout+rout, expected≈0.82 → win_cap≈0.005
            ("6-1 6-3", [2.81]),   # rout+rout, expected≈0.76 → win_cap≈0.009
            ("6-1 6-3", [2.97]),   # rout+rout, expected≈0.64 → win_cap≈0.019
            ("6-2 6-2", [2.72]),   # rout+rout, expected≈0.82 → win_cap≈0.005
        ]
        rating = self._HEAVY_FAV   # 3.10
        total_gain = 0.0
        for score, opp_r in matches:
            rec = MatchRecord(
                opponent_ratings=opp_r, partner_rating=None,
                won=True, date="1/1/2026", division="3.0",
                match_id="t", line_label="1# Singles", score=score,
            )
            adj = _sequential_match_adj(rating, rec)
            self.assertGreater(adj, 0, "Dominant win still earns something positive")
            total_gain += adj
            rating += adj
        self.assertLess(total_gain, 0.08,
                        f"5 wins over much-weaker opponents: total gain must be < 0.08, got {total_gain:.4f}")
        self.assertGreater(rating, self._HEAVY_FAV,
                           "Final rating should be above starting point after 5 wins")

    def test_favourite_barely_wins_tiebreak_penalized(self):
        """
        Julie Brown scenario: 3.0+2.82 barely beats 2.82+2.78 in a 3-set tiebreak
        (score 6-4 4-6 1-0). Julie was a ~61% favourite; match was totally even.
        Surplus = 0.07 < gate (0.15) → below-gate penalty: adj < 0.
        "You won, but performed at the opponents' level rather than your own."
        """
        rec = MatchRecord(
            opponent_ratings=[2.82, 2.78], partner_rating=2.82,
            won=True, date="3/21/2026", division="3.0",
            match_id="w2", line_label="1# Doubles", score="6-4 4-6 1-0",
        )
        adj = _sequential_match_adj(3.0, rec)
        self.assertLess(adj, 0.0,
                        "Favourite barely winning a 3-set tiebreak should produce a small negative adj")
        self.assertGreater(adj, -0.05,
                           "Below-gate penalty is small — should not match a full upset loss")

    def test_favourite_wins_convincingly_earns_something(self):
        """
        A favourite who DOMINATES earns a positive adj.
        3.0 beats 2.80 with 6-1 6-2 (rout+rout): surplus >> 0.15 gate.
        win_cap = 0.15 × (1 − expected) keeps it proportional.
        """
        rec = self._rec(3.0, 2.80, won=True, score="6-1 6-2")
        adj = _sequential_match_adj(3.0, rec)
        self.assertGreater(adj, 0.0,
                           "Favourite crushing opponent convincingly earns a positive adj")
        self.assertLess(adj, 0.06,
                        "win_cap keeps it proportional to how unexpected the win was")

    # ------------------------------------------------------------------
    # Rule 4: Heavy favourite loses → always negative adj
    # ------------------------------------------------------------------

    def test_favourite_loses_rout_drops(self):
        """3.10 loses 1-6 1-6 to 2.70 → negative adj."""
        rec = self._rec(self._HEAVY_FAV, self._WEAK_OPP, won=False, score="6-1 6-1")
        adj = _sequential_match_adj(self._HEAVY_FAV, rec)
        self.assertLess(adj, 0, "Favourite losing must produce a negative adj")

    def test_favourite_loses_close_drops(self):
        """3.10 loses 4-6 4-6 to 2.70 → still negative."""
        rec = self._rec(self._HEAVY_FAV, self._WEAK_OPP, won=False, score="6-4 6-4")
        adj = _sequential_match_adj(self._HEAVY_FAV, rec)
        self.assertLess(adj, 0, "Favourite losing a close match must still produce negative adj")

    # ------------------------------------------------------------------
    # Rule 5: Even match winner → moderate positive adj
    # ------------------------------------------------------------------

    def test_even_match_win_moderate(self):
        """3.0 beats 3.0 evenly → adj in (0.02, 0.06).
        win_cap = 0.15 × (0.50)² = 0.0375 for a true 50/50 match.
        """
        rec = self._rec(3.0, 3.0, won=True, score="7-5 6-4")
        adj = _sequential_match_adj(3.0, rec)
        self.assertGreater(adj, 0.02,
                           "Even match win earns a moderate positive adj")
        self.assertLess(adj, 0.06,
                        "Even match win stays well below full SEQ_CAP")

    def test_even_match_loss_drops(self):
        """3.0 loses 4-6 3-6 to 3.0 → negative adj."""
        rec = self._rec(3.0, 3.0, won=False, score="6-4 6-3")
        adj = _sequential_match_adj(3.0, rec)
        self.assertLess(adj, 0, "Even match loss must produce a negative adj")


if __name__ == "__main__":
    unittest.main()
