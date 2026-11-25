import unittest

from engine.build_directory import _self_rate_after_gap


class SelfRateAfterGapTest(unittest.TestCase):
    def test_two_blank_seasons_make_s(self):
        # Darian McCauley: 3.0 in 2022, nothing in 2023-24, back in 2025 -> 3.0 S
        b = {2022: {"level": 3.0}, 2023: {}, 2024: {}, 2025: {"level": 3.0}}
        _self_rate_after_gap(b)
        self.assertEqual(b[2025].get("rating_type"), "S")

    def test_one_blank_season_keeps_no_letter(self):
        b = {2022: {}, 2023: {"level": 3.0}, 2024: {}, 2025: {"level": 3.0}}
        _self_rate_after_gap(b)
        self.assertIsNone(b[2025].get("rating_type"))

    def test_no_earlier_play_unknown(self):
        b = {2022: {}, 2023: {}, 2024: {}, 2025: {"level": 3.0}}
        _self_rate_after_gap(b)
        self.assertIsNone(b[2025].get("rating_type"))

    def test_list_letter_kept(self):
        b = {2022: {"level": 3.0}, 2023: {}, 2024: {}, 2025: {"level": 3.0, "rating_type": "C"}}
        _self_rate_after_gap(b)
        self.assertEqual(b[2025]["rating_type"], "C")


if __name__ == "__main__":
    unittest.main()
