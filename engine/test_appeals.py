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
        a = appeal_call("3.0", "3.0", {"last": {"dyn": 2.50, "date": "12/09/2025"}})
        self.assertEqual((a["dir"], a["close"]), ("up", True))
        a = appeal_call("3.0", None, {"last": {"dyn": 2.80, "date": "12/09/2025"}})
        self.assertTrue(a["close"])  # inside the band

    def test_no_data(self):
        self.assertIsNone(appeal_call(None, None, None))
        self.assertIsNone(appeal_call("3.0", None, {})["dir"])


if __name__ == "__main__":
    unittest.main()
