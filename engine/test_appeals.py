import unittest

from engine.appeals import appeal_call


class AppealCallTest(unittest.TestCase):
    def test_level_change_is_certain(self):
        a = appeal_call("3.0", "3.5", None)
        self.assertEqual((a["dir"], a["close"]), ("down", False))
        a = appeal_call("3.5", "3.0", None)
        self.assertEqual((a["dir"], a["close"]), ("up", False))

    def test_same_level_uses_last_dynamic(self):
        rec = {"last": {"dyn": 3.21, "date": "12/09/2025"}}
        self.assertEqual(appeal_call("3.0", "3.0", rec)["dir"], "down")
        rec = {"last": {"dyn": 2.30, "date": "12/09/2025"}}
        a = appeal_call("3.0", "3.0", rec)
        self.assertEqual((a["dir"], a["close"]), ("up", False))

    def test_edge_is_close(self):
        # mid-band with no level change behind it: a toss-up
        a = appeal_call("3.0", "3.0", {"last": {"dyn": 2.95, "date": "11/04/2025"}}, earlier="3.0")
        self.assertEqual((a["dir"], a["close"]), ("down", True))
        # just over the line is still a clear appeal down (Treasure Nyame 4.09 at 4.0)
        a = appeal_call("4.0", "4.0", {"last": {"dyn": 4.09, "date": "09/29/2024"}}, earlier="4.0")
        self.assertEqual((a["dir"], a["close"]), ("down", False))

    def test_inside_band_is_up(self):
        # Maryam Kaveh: 2.57 (or even 2.80) at 3.0 can't be an appeal down from 3.5
        for x in (2.57, 2.80):
            a = appeal_call("3.0", "3.0", {"last": {"dyn": x, "date": "11/04/2025"}}, earlier="2.5")
            self.assertEqual((a["dir"], a["close"]), ("up", False))

    def test_inside_band_follows_earlier_level(self):
        # Nicole Reyes: 4.0 -> 3.5 -> 3.5 A with TR 3.46 = appeal down
        a = appeal_call("3.5", "3.5", {"last": {"dyn": 3.46, "date": "11/01/2025"}}, earlier="4.0")
        self.assertEqual((a["dir"], a["close"]), ("down", False))

    def test_25_is_always_down(self):
        a = appeal_call("2.5", "2.5", {"last": {"dyn": 2.30, "date": "11/04/2025"}})
        self.assertEqual((a["dir"], a["close"]), ("down", False))

    def test_no_data(self):
        self.assertIsNone(appeal_call(None, None, None))
        self.assertIsNone(appeal_call("3.0", None, {})["dir"])


if __name__ == "__main__":
    unittest.main()
