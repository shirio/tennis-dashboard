"""Unit tests for engine/bump.py (Bump indicator).

Run:
    python3 -m unittest engine.test_bump -v
"""
import unittest

from engine import bump
from engine.ratings import CourtEvent


def ev(w, l, score, line="1# Doubles"):
    return CourtEvent(date="3/1/2026", match_id="m", line_label=line, division="3.0",
                      winner_keys=w, loser_keys=l, score=score)


class TestBump(unittest.TestCase):
    def test_parse_level(self):
        self.assertEqual(bump.parse_level("3.0 C"), (3.0, "C"))
        self.assertEqual(bump.parse_level(None), (None, ""))

    def test_start_rating_clamped_into_level(self):
        self.assertAlmostEqual(bump.start_rating(2.40, 3.0, "C"), 2.51)
        self.assertAlmostEqual(bump.start_rating(3.10, 3.0, "C"), 3.0)
        self.assertAlmostEqual(bump.start_rating(2.40, 3.0, "A"), 2.75)

    def test_one_match_moves_a_quarter_at_most(self):
        bl = {"a": 2.9, "b": 2.9}
        st = bump.run_bump([ev(["a"], ["b"], "6-0 6-0", "1# Singles")], bl, {})
        move = st["a"].history[-1] - 2.9
        self.assertGreater(move, 0)
        self.assertLessEqual(move, bump.MAX_GAP / bump.WINDOW + 1e-9)

    def test_partners_get_equal_credit(self):
        bl = {"a": 3.0, "b": 2.7, "c": 2.85, "d": 2.85}
        st = bump.run_bump([ev(["a", "b"], ["c", "d"], "6-3 6-3")], bl, {})
        self.assertAlmostEqual(st["a"].history[-1] - 3.0, st["b"].history[-1] - 2.7)

    def test_too_few_matches(self):
        bl = {"a": 2.98, "b": 2.6}
        st = bump.run_bump([ev(["a"], ["b"], "6-0 6-0", "1# Singles")], bl, {})
        self.assertEqual(bump.year_end_call(st["a"], 3.0, "C")["call"], "few_matches")

    def test_bump_up_after_strong_season(self):
        bl = {"a": 2.95, "b": 2.95, "c": 2.95}
        events = [ev(["a"], [o], "6-1 6-1", "1# Singles") for o in ["b", "c"] * 3]
        st = bump.run_bump(events, bl, {})
        call = bump.year_end_call(st["a"], 3.0, "C")
        self.assertIn(call["call"], ("up", "lean_up"))
        self.assertGreater(call["estimate"], 3.0)

    def test_strikes_only_for_self_rated(self):
        opps = [f"o{i}" for i in range(8)]
        bl = {"a": 3.2, "b": 3.0, **{o: 3.0 for o in opps}}
        events = [ev(["a"], [o], "6-0 6-0", "1# Singles") for o in opps]
        events.append(ev(["b"], ["o0"], "6-0 6-0", "1# Singles"))
        levels = {"a": (3.0, "S"), "b": (3.0, "C")}
        st = bump.run_bump(events, bl, levels)
        self.assertGreater(st["a"].strikes, 0)
        self.assertNotIn("strikes", bump.year_end_call(st["b"], 3.0, "C"))


if __name__ == "__main__":
    unittest.main()
