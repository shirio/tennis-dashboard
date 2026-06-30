#!/usr/bin/env python3
"""
Scrape Tennis Record Rating History pages for baseline ratings.
Finds the last black (non-red) Post-rating before 2026.
"""

import json
import time
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime

import requests as _requests


class RatingHistoryParser(HTMLParser):
    """Parse the rating history table from Tennis Record."""

    def __init__(self):
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.cell_index = 0
        self.current_row = []
        self.current_cell_text = ""
        self.current_cell_is_red = False
        self.rows = []  # list of (date_str, post_rating_float, is_black)
        self.row_data = []  # accumulate cells per row
        self.in_span_red = False
        self.header_found = False
        self.skip_row = False  # skip header rows

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "table":
            self.in_table = True

        if self.in_table and tag == "tr":
            self.in_row = True
            self.row_data = []
            self.skip_row = False

        if self.in_row and tag == "td":
            self.in_cell = True
            self.current_cell_text = ""
            self.current_cell_is_red = False

        if self.in_row and tag == "th":
            self.skip_row = True

        if self.in_cell and tag == "span":
            style = attrs_dict.get("style", "")
            if "DD0000" in style or "dd0000" in style:
                self.in_span_red = True
                self.current_cell_is_red = True

    def handle_endtag(self, tag):
        if tag == "span":
            self.in_span_red = False

        if self.in_row and tag == "td":
            self.row_data.append({
                "text": self.current_cell_text.strip(),
                "is_red": self.current_cell_is_red
            })
            self.in_cell = False

        if tag == "tr":
            if self.in_row and not self.skip_row and len(self.row_data) >= 10:
                # Columns: Date(0), League(1), Team(2), Line(3), Partner(4),
                #          Opponents(5-7 or combined), W/L, Score, Pre-rating, Post-rating
                # Post-rating is the last column (index -1), Pre-rating is second to last
                date_cell = self.row_data[0]
                post_cell = self.row_data[-1]

                date_str = date_cell["text"]
                post_text = post_cell["text"]
                is_black = not post_cell["is_red"]

                if date_str and post_text:
                    try:
                        # Parse the date
                        dt = datetime.strptime(date_str, "%m/%d/%Y")
                        # Parse rating
                        rating = float(post_text)
                        self.rows.append((date_str, dt, rating, is_black))
                    except (ValueError, TypeError):
                        pass

            self.in_row = False
            self.row_data = []

        if tag == "table":
            self.in_table = False

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell_text += data


def get_baseline(name, s_param):
    """Fetch the rating history page and extract the baseline rating."""
    encoded_name = urllib.parse.quote(name)
    url = f"https://www.tennisrecord.com/adult/matchhistory.aspx?year=Rating&playername={encoded_name}"
    if s_param:
        url += f"&s={s_param}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    html = None
    last_err = None
    for attempt in range(3):
        try:
            # (connect_timeout, read_timeout) — hard cap per attempt, not per chunk
            resp = _requests.get(url, headers=headers, timeout=(5, 10))
            resp.raise_for_status()
            html = resp.text
            break
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(2)
    if html is None:
        return None, None, last_err

    parser = RatingHistoryParser()
    parser.feed(html)

    rows = parser.rows
    if not rows:
        return None, None, "no_rows"

    # Filter to only black ratings
    black_rows = [(ds, dt, rating) for ds, dt, rating, is_black in rows if is_black]

    if not black_rows:
        return None, None, "no_black_ratings"

    # Find the last black rating before 2026
    pre_2026 = [(ds, dt, rating) for ds, dt, rating in black_rows if dt.year <= 2025]

    if pre_2026:
        # Rows are newest-first; take the first one (most recent before 2026)
        target = pre_2026[0]
        return target[0], target[2], None
    else:
        # No pre-2026 black ratings — take the oldest black rating on the page
        # Rows are newest-first so oldest is last
        target = black_rows[-1]
        return target[0], target[2], "oldest_fallback"


def main():
    import argparse as _ap
    _parser = _ap.ArgumentParser(description="Scrape pre-2026 baseline ratings from tennisrecord.com")
    _parser.add_argument("--input",  default="/Users/shirinoskooi/Documents/tennis-dashboard/data/players_for_baseline_scrape.json")
    _parser.add_argument("--output", default="/Users/shirinoskooi/Documents/tennis-dashboard/data/baseline_results.json")
    _args = _parser.parse_args()
    input_path  = _args.input
    output_path = _args.output

    with open(input_path) as f:
        players = json.load(f)

    total = len(players)

    # Load existing results to resume
    try:
        with open(output_path) as f:
            results = json.load(f)
        print(f"Resuming from {len(results)} already processed...")
    except FileNotFoundError:
        results = {}

    processed_names = set(results.keys())
    remaining = [p for p in players if p["name"] not in processed_names]
    print(f"Processing {len(remaining)} remaining players (of {total} total)...")

    errors = {}

    for i, player in enumerate(remaining):
        name = player["name"]
        s = player["s"]

        date_str, baseline, err = get_baseline(name, s)

        if date_str is not None and baseline is not None:
            results[name] = {"date": date_str, "baseline": baseline}
        else:
            results[name] = {"date": None, "baseline": None}
            if err:
                errors[name] = err

        # Progress update every 25 and save intermediate results
        if (i + 1) % 25 == 0:
            found = sum(1 for v in results.values() if v["date"] is not None)
            print(f"  [{len(results)}/{total}] {found} found so far...", flush=True)
            with open(output_path, "w") as f:
                json.dump(results, f, indent=2)

        # Small delay to be polite
        time.sleep(0.2)

    # Final save
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    found = sum(1 for v in results.values() if v["date"] is not None)
    print(f"\nDone! {found}/{total} players have baseline ratings.")

    if errors:
        print(f"\nError summary ({len(errors)} players with issues):")
        err_counts = {}
        for n, e in errors.items():
            err_counts[e] = err_counts.get(e, 0) + 1
        for e, c in sorted(err_counts.items(), key=lambda x: -x[1]):
            print(f"  {e}: {c}")

    print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
