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
        self.assertEqual(bump.year_end_call(None, st["a"].matches, 3.0, "C")["call"],
                         "few_matches")

    def test_bump_up_after_strong_season(self):
        bl = {"a": 2.95, "b": 2.95, "c": 2.95}
        events = [ev(["a"], [o], "6-1 6-1", "1# Singles") for o in ["b", "c"] * 3]
        y, n = bump.fit_year_end(events, bl, {})
        call = bump.year_end_call(y["a"], n["a"], 3.0, "C")
        self.assertIn(call["call"], ("up", "lean_up"))
        self.assertGreater(call["estimate"], 3.0)

    def test_recency_weights_shape(self):
        w = bump.recency_weights(25)
        self.assertEqual(len(w), 25)
        self.assertGreater(w[-1], w[0])             # newest heavier than oldest
        last5, older = sum(w[-5:]), sum(w[:5])
        self.assertAlmostEqual(last5 / older, 0.26 / 0.19, places=6)

    def test_playoff_match_counts_more(self):
        bl = {"a": 2.8, "b": 2.8, "c": 2.8}
        reg = ev(["a"], ["b"], "6-2 6-2", "1# Singles")
        po = CourtEvent(date="6/1/2026", match_id="p", line_label="1# Singles",
                        division="3.0", winner_keys=["a"], loser_keys=["c"],
                        score="6-2 6-2")
        loss = CourtEvent(date="6/2/2026", match_id="q", line_label="1# Singles",
                          division="3.0", winner_keys=["c"], loser_keys=["a"],
                          score="6-2 6-2")
        y0, _ = bump.fit_year_end([reg, po, loss], bl, {}, set())
        y1, _ = bump.fit_year_end([reg, po, loss], bl, {}, {"p"})
        self.assertGreater(y1["a"], y0["a"])

    def test_opponents_judged_where_they_finish(self):
        # a beats b early; b then beats strong players. a's year-end should
        # rise with b's, even though a never played again.
        bl = {"a": 2.8, "b": 2.8, "x": 3.0, "y": 3.0}
        early = ev(["a"], ["b"], "6-4 6-4", "1# Singles")
        later = [ev(["b"], [o], "6-1 6-1", "1# Singles") for o in ["x", "y", "x"]]
        y_with, _ = bump.fit_year_end([early] + later, bl, {})
        y_alone, _ = bump.fit_year_end([early], bl, {})
        self.assertGreater(y_with["a"], y_alone["a"])

    def test_late_fall_match_already_in_baseline_not_counted_twice(self):
        bl = {"a": 2.9, "b": 2.9}
        late = CourtEvent(date="11/20/2025", match_id="f", line_label="1# Singles",
                          division="3.0", winner_keys=["a"], loser_keys=["b"],
                          score="6-0 6-0")
        late.prior_crawl = True
        y0, n0 = bump.fit_year_end([late], bl, {})
        y1, n1 = bump.fit_year_end([late], bl, {}, known_through={"a": "12/10/2025"})
        self.assertGreater(y0["a"], 2.9)
        self.assertNotIn("a", n1)              # a's baseline already has it
        self.assertEqual(n1["b"], 1)           # b's doesn't: b still moves
        self.assertLess(y1["b"], 2.9)
        y2, n2 = bump.fit_year_end([late], bl, {}, known_through={"a": "11/15/2025"})
        self.assertEqual(n2["a"], 1)           # played after the baseline: counts

    def test_blowout_of_much_weaker_player_can_lower_rating(self):
        bl = {"a": 3.0, "b": 1.9}   # gap beyond MAX_GAP
        y, _ = bump.fit_year_end([ev(["a"], ["b"], "6-0 6-0", "1# Singles")], bl, {})
        self.assertLess(y["a"], 3.0)

    def test_strikes_only_for_self_rated(self):
        opps = [f"o{i}" for i in range(8)]
        bl = {"a": 3.2, "b": 3.0, **{o: 3.0 for o in opps}}
        events = [ev(["a"], [o], "6-0 6-0", "1# Singles") for o in opps]
        events.append(ev(["b"], ["o0"], "6-0 6-0", "1# Singles"))
        levels = {"a": (3.0, "S"), "b": (3.0, "C")}
        st = bump.run_bump(events, bl, levels)
        self.assertGreater(st["a"].strikes, 0)
        self.assertNotIn("strikes", bump.year_end_call(3.0, 5, 3.0, "C", strikes=st["b"].strikes))


if __name__ == "__main__":
    unittest.main()


class TestYearEndWindow(unittest.TestCase):
    def test_summer_and_late_matches_do_not_count(self):
        self.assertTrue(bump.counts_toward_year_end("6/16/2026", "2026 USTA ADULT LEAGUE 18 & OVER"))
        self.assertFalse(bump.counts_toward_year_end("7/14/2026", "2026 NTA SUMMER LEAGUE"))
        self.assertFalse(bump.counts_toward_year_end("11/14/2026", "2026 NTA ADULT FALL LEAGUE"))
        self.assertTrue(bump.counts_toward_year_end("11/8/2026", "2026 NTA ADULT FALL LEAGUE"))
        self.assertTrue(bump.counts_toward_year_end("12/6/2025", ""))
        self.assertFalse(bump.counts_toward_year_end("6/1/2026", "2026 USTA ADULT MIXED 18 & OVER"))

    def test_two_level_call(self):
        c = bump.year_end_call(4.09, 30, 3.5, "C")
        self.assertEqual((c["call"], c.get("levels")), ("up", 2))
        self.assertEqual(bump.year_end_call(3.7, 30, 3.5, "C").get("levels"), None)


def test_no_down_call_below_lowest_level():
    from engine.bump import year_end_call
    c = year_end_call(1.90, 10, 2.5, "C")
    assert c["call"] == "hold" and c["estimate"] == 1.9
    assert "levels" not in c
    assert year_end_call(2.40, 10, 3.0, "C")["call"] == "down"


def test_mid_level_start():
    assert bump.mid_level_start(3.0) == 2.75
    assert bump.mid_level_start(3.5, 3.4) == 3.25
    assert bump.mid_level_start(None, 3.1) == 3.1   # unknown level: keep the fallback
    assert bump.START_MODE == "mid"


def test_out_of_state_player_without_level_starts_at_level_played():
    from engine import crawl_events as C
    matches = {
        "M1": {"league": "18 & OVER TRI-LEVEL 3.0/3.5/4.0W",
               "lines": [{"line": "#2 Doubles", "level": "3.5"}]},
        "M2": {"league": "18 & Over Adult 4.0W", "lines": [{"line": "#1 Doubles"}]},
        "M3": {"league": "55 & Over 7.0W", "lines": [{"line": "#1 Doubles"}]},
    }
    person = {"level": None, "ntrp": None,
              "matches": [["M1", "#2 Doubles", "away"], ["M2", "#1 Doubles", "home"]]}
    assert C.baseline_for(person) is None            # no match table: unknown
    assert C.baseline_for(person, matches) == 3.25   # lowest level played, mid
    combo = {"level": None, "matches": [["M3", "#1 Doubles", "home"]]}
    assert C.baseline_for(combo, matches) is None    # combo leagues say nothing
    assert C.baseline_for({"level": "3.0C"}, matches) == 2.75


def test_recenter_levels_moves_each_level_to_its_middle():
    from engine import bump as B
    ye = {f"p{i}": 3.60 + 0.01 * i for i in range(25)}          # 4.0 pool averages 3.72
    ye.update({f"q{i}": 2.75 + 0.01 * (i - 12) for i in range(25)})  # 3.0 pool already mid
    levels = {k: (4.0, "C") if k[0] == "p" else (3.0, "C") for k in ye}
    out = B.recenter_levels(ye, {k: 5 for k in ye}, levels, lambda k: True)
    p = [out[k] for k in ye if k[0] == "p"]
    assert abs(sum(p) / len(p) - 3.75) < 1e-9
    assert abs(out["p1"] - out["p0"] - 0.01) < 1e-9             # order kept
    assert abs(out["q0"] - ye["q0"]) < 1e-9


def test_playoffs_with_little_known_out_of_state_players_count_1x():
    from engine import bump as B, ratings as R
    e1 = R.CourtEvent(date="8/1/2026", match_id="C1", line_label="#1 Doubles", division="3.0",
                      winner_keys=["a", "b"], loser_keys=["tl::x|UTAH|1", "tl::y|UTAH|2"], score="6-4 6-4")
    e2 = R.CourtEvent(date="8/1/2026", match_id="C2", line_label="#1 Doubles", division="3.0",
                      winner_keys=["a", "b"], loser_keys=["tl::z|NEVADA|3", "c"], score="6-4 6-4")
    assert B.little_known_playoffs([e1, e2], {"C1", "C2"}) == {"C1"}


def test_play_up_tiebreak_leans_up_just_under_the_line():
    from engine.bump import year_end_call
    c = year_end_call(2.96, 20, 3.0, "C", play_up=0.95)
    assert c["call"] == "lean_up" and c["direction"] == "up"
    assert c["label"] == "Leaning up (plays up)"
    assert year_end_call(2.96, 20, 3.0, "C", play_up=0.5)["direction"] == "hold"
    assert year_end_call(2.90, 20, 3.0, "C", play_up=1.0)["direction"] == "hold"


def test_play_up_shares_ignore_combo_flights():
    from engine.bump import play_up_shares
    from engine.ratings import CourtEvent
    ev = [CourtEvent("1/1/2026", "m1", "D1", "3.5", ["a", "b"], ["c", "d"], "6-1 6-1"),
          CourtEvent("1/2/2026", "m2", "D1", "7.0", ["a", "b"], ["c", "d"], "6-1 6-1")]
    sh = play_up_shares(ev, {"a": (3.0, "C"), "c": (3.5, "C")})
    assert sh["a"] == 0.5 and sh["c"] == 0.0


def test_bumped_up_last_year_near_down_line_predicts_down():
    from engine.bump import year_end_call
    c = year_end_call(3.07, 20, 3.5, "C", bumped_up=True)
    assert c["direction"] == "down" and c["label"] == "Leaning down (bumped up last year)"
    assert year_end_call(3.07, 20, 3.5, "C")["direction"] == "hold"
    assert year_end_call(3.15, 20, 3.5, "C", bumped_up=True)["direction"] == "hold"


def test_4_0_down_line_sits_a_little_higher():
    from engine.bump import year_end_call
    assert year_end_call(3.52, 20, 4.0, "C")["direction"] == "down"
    assert year_end_call(3.02, 20, 3.5, "C")["direction"] == "hold"


def test_nv_crawl_key_uses_area_not_key_text():
    from engine import bump as B
    B.TL_AREA.clear()
    B.TL_AREA["tl::iuYTg1LXponzeVRzgkfLHg=="] = "NEVADA"
    B.TL_AREA["tl::x:abc123def456"] = "UTAH"
    assert B.is_nv_crawl_key("tl::iuYTg1LXponzeVRzgkfLHg==")
    assert not B.is_nv_crawl_key("tl::x:abc123def456")
    assert B.is_nv_crawl_key("tl::kara-gaston|NEVADA|0")   # old-style keys still work
    assert not B.is_nv_crawl_key("Kara Gaston")
    B.TL_AREA.clear()
