"""
scrapers/test_retired_scorecard.py

USTA quirk: on a retirement, TennisLink prints "Retired" under the WINNER's
name, next to the green checkmark, even though the OTHER player retired and
lost. The parser must take the winner from the checkmark and must not treat
"Retired" as a player name or as a sign of who lost.

Run:
    python3 -m unittest scrapers.test_retired_scorecard -v
"""
from __future__ import annotations

import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

# Minimal scorecard shaped like TennisLink's: Sandra Mier (home) has the green
# checkmark and "Retired" under her name; Shi L Oskooi (visitor) retired.
SCORECARD = """
<html><body>
<div>DTC #3 (Home Team)</div>
<div>RED ROCK CC (Visiting Team)</div>
<table>
<tr><td>#1 Singles</td></tr>
<tr><td>2:00 PM</td>
    <td>Sandra Mier<br>Retired
        <img id="ctl00_mainContent_rptScoreCard_ctl00_imgHomePlayer" src="/images/mark.gif" width="16" height="16"></td>
    <td>Vs.</td>
    <td>Shi L Oskooi</td>
    <td>4-4</td></tr>
</table>
<div>TOTAL TEAM SCORE</div>
</body></html>
"""


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class RetiredScorecardTest(unittest.TestCase):
    def test_checkmark_marks_winner_and_retired_is_ignored(self):
        from scrapers.scrape_tennislink import _parse_match_detail_page
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.set_content(SCORECARD)
            lines = _parse_match_detail_page(page)
            browser.close()
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertEqual(line["line"], "1# Singles")
        self.assertEqual(line["result"], "home")          # checkmark side won
        self.assertEqual(line["players_home"], "Sandra Mier")
        self.assertEqual(line["players_away"], "Shi L Oskooi")
        self.assertNotIn("retired", (line["players_home"] + line["players_away"]).lower())


if __name__ == "__main__":
    unittest.main()
