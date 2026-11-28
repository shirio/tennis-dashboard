"""Unit tests for merging per-state crawl files (engine/crawl_events.merge_crawls).

Run:
    python3 -m unittest engine.test_crawl_merge -v
"""
import unittest

from engine.crawl_events import merge_crawls


class TestMergeCrawls(unittest.TestCase):
    def test_shared_records_merge_by_id_and_people_union_refs(self):
        nv = {"code": "nv", "year": 2026,
              "matches": {"m1": {"date": "1/1/2026"}, "m2": {"date": "2/1/2026"}},
              "teams": {"t1": {"name": "A"}},
              "people": {"p1": {"name": "Pat", "matches": [["m1", "#1 Doubles", "home"]], "teams": ["t1"]}}}
        ut = {"code": "ut", "year": 2026,
              "matches": {"m2": {"date": "2/1/2026"}, "m3": {"date": "3/1/2026"}},
              "teams": {"t2": {"name": "B"}},
              "people": {"p1": {"name": "Pat", "matches": [["m2", "#2 Doubles", "away"]], "teams": ["t2"]},
                         "p2": {"name": "Lee", "matches": [["m3", "#1 Singles", "home"]], "teams": ["t2"]}}}
        m = merge_crawls([nv, ut])
        self.assertEqual(sorted(m["matches"]), ["m1", "m2", "m3"])
        self.assertEqual(sorted(m["teams"]), ["t1", "t2"])
        self.assertEqual([r[0] for r in m["people"]["p1"]["matches"]], ["m1", "m2"])
        self.assertEqual(m["people"]["p1"]["teams"], ["t1", "t2"])
        self.assertEqual(m["states"], ["nv", "ut"])
        # inputs are not modified
        self.assertEqual(len(nv["people"]["p1"]["matches"]), 1)

    def test_duplicate_ref_not_doubled(self):
        a = {"code": "nv", "people": {"p": {"matches": [["m1", "#1", "home"]], "teams": []}}}
        b = {"code": "ut", "people": {"p": {"matches": [["m1", "#1", "home"]], "teams": []}}}
        self.assertEqual(len(merge_crawls([a, b])["people"]["p"]["matches"]), 1)


if __name__ == "__main__":
    unittest.main()
