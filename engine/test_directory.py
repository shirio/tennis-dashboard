"""
engine/test_directory.py

Unit tests for the Nevada directory: USTA results parsing, name matching,
year-to-year linking and record splits.

Run:
    python3 -m unittest engine.test_directory -v
"""
from __future__ import annotations

import unittest

from engine.build_directory import (
    compute_records, first_last_key, link_usta_to_db, link_years, match_key,
)
from engine.ntrp_defs import RATING_TYPES, format_ntrp, parse_ntrp
from scrapers.ntrp_list import display_name, parse_results_html

RESULTS_HTML = """
<table><tr><td>NTRP Information for:</td></tr></table>
<table>
<tr><th>Name</th><th>Gender</th><th>City</th><th>State</th>
    <th>Year End Rating Level</th><th>Year End Rating Date</th><th>Rating Type</th></tr>
<tr><td><a href="PlayerHistory.aspx?id=1">Abatangelo, Elizabeth</a></td><td>F</td>
    <td>Las Vegas</td><td>NV</td><td>3.0</td><td>12/31/2025</td><td>C</td></tr>
<tr><td><a href="PlayerHistory.aspx?id=2">AKERMAN, ELAN</a></td><td>M</td>
    <td>Henderson</td><td>NV</td><td>3.5</td><td>2/27/2026</td><td>S</td></tr>
<tr><td><a href="PlayerHistory.aspx?id=3">Afualo, Ui</a></td><td>F</td>
    <td>St George</td><td>UT</td><td>4.0</td><td>12/31/2025</td><td>C</td></tr>
</table>
"""


class ParseTest(unittest.TestCase):
    def test_parse_results(self):
        rows = parse_results_html(RESULTS_HTML)
        self.assertEqual(len(rows), 3)
        r = rows[0]
        self.assertEqual(r["name"], "Elizabeth Abatangelo")
        self.assertEqual((r["gender"], r["city"], r["state"]), ("F", "Las Vegas", "NV"))
        self.assertEqual((r["level"], r["rating_type"], r["rating_date"]), ("3.0", "C", "12/31/2025"))
        self.assertTrue(r["history_url"].endswith("/NTRP/PlayerHistory.aspx?id=1"))
        self.assertEqual(rows[1]["name"], "Elan Akerman")
        self.assertEqual(rows[2]["state"], "UT")   # out-of-state players are kept

    def test_display_name(self):
        self.assertEqual(display_name("Allen,  Maria"), "Maria Allen")
        self.assertEqual(display_name("Shi L Oskooi"), "Shi L Oskooi")


class DefsTest(unittest.TestCase):
    def test_rating_types(self):
        self.assertEqual(set(RATING_TYPES), set("SACDEMT"))

    def test_parse_format(self):
        self.assertEqual(parse_ntrp("3.0 C"), (3.0, "C"))
        self.assertEqual(parse_ntrp("2.5"), (2.5, None))
        self.assertEqual(parse_ntrp(None), (None, None))
        self.assertEqual(format_ntrp(3.5, "S"), "3.5 S")
        self.assertEqual(format_ntrp(None, "C"), "")


class MatchTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(match_key("Mary-Ann  O'Neil"), "mary ann oneil")
        self.assertEqual(first_last_key("Shi L Oskooi"), "shi oskooi")

    def test_link_unique_only(self):
        db = [{"id": "a", "name": "Shi L Oskooi"}, {"id": "b", "name": "Kim Lee"},
              {"id": "c", "name": "Kim Lee"}, {"id": "d", "name": "Pat Smith"}]
        usta = [{"name": "Shi Oskooi", "gender": "F"},     # middle initial missing -> fallback
                {"name": "Kim Lee", "gender": "F"},        # ambiguous in db -> unlinked
                {"name": "Pat Smith", "gender": "M"},      # db is women-only -> unlinked
                {"name": "Nobody Here", "gender": "F"}]
        links = link_usta_to_db(usta, db)
        self.assertEqual({i: p["id"] for i, p in links.items()}, {0: "a"})

    def test_link_years(self):
        a = [{"name": "Kim Lee", "city": "Reno", "gender": "F"},
             {"name": "Kim Lee", "city": "Las Vegas", "gender": "F"}]
        b = [{"name": "Kim Lee", "city": "Las Vegas", "gender": "F", "level": 3.5}]
        self.assertEqual(link_years(a, b), {1: b[0]})


class RecordsTest(unittest.TestCase):
    def test_splits(self):
        h = [
            {"date": "3/14/2026", "div": "3.0", "line": "1# Singles", "won": True},
            {"date": "3/21/2026", "div": "3.0", "line": "1# Doubles", "won": False},
            {"date": "4/11/2026", "div": "3.5", "line": "2# Doubles", "won": True},
            {"date": "5/9/2026", "div": "3.0", "line": "2# Singles", "won": True, "wko": True},
            {"date": "5/9/2025", "div": "3.5", "line": "1# Singles", "won": False},
        ]
        r = compute_records(h)
        self.assertEqual(r["overall"], [2, 2])                 # default excluded
        self.assertEqual(dict(r["by_year"]), {"2026": [2, 1], "2025": [0, 1]})
        self.assertEqual(r["by_div"]["3.0"], [1, 1])
        self.assertEqual(r["by_div_type"][("3.0", "singles")], [1, 0])
        self.assertEqual(r["singles"], [1, 1])
        self.assertEqual(r["doubles_by_div"]["3.5"], [1, 0])


if __name__ == "__main__":
    unittest.main()
