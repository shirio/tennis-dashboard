#!/usr/bin/env python3
"""Scrape Tennis Record Rating History pages and extract pre-season baselines."""

import json
import re
import urllib.request
import urllib.parse
from html.parser import HTMLParser


class RatingHistoryParser(HTMLParser):
    """Parse Tennis Record rating history page for match rows."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._in_table = False
        self._in_row = False
        self._tds = []
        self._current_td_data = []
        self._current_td_color = None
        self._in_td = False
        self._in_span = False
        self._span_color = None
        self._depth = 0
        self._table_depth = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self._depth += 1

        if tag == 'table':
            # Look for the ratings table
            self._table_depth = self._depth
            self._in_table = True

        if self._in_table and tag == 'tr':
            self._in_row = True
            self._tds = []

        if self._in_row and tag == 'td':
            self._in_td = True
            self._current_td_data = []
            self._current_td_color = None

        if self._in_td and tag == 'span':
            style = attrs_dict.get('style', '')
            if 'color:#DD0000' in style or 'color: #DD0000' in style:
                self._span_color = 'red'
            else:
                self._span_color = None
            self._in_span = True

    def handle_endtag(self, tag):
        if tag == 'tr' and self._in_row:
            if len(self._tds) >= 2:
                self.rows.append(self._tds[:])
            self._in_row = False
            self._tds = []

        if tag == 'td' and self._in_td:
            self._tds.append({
                'text': ''.join(self._current_td_data).strip(),
                'color': self._current_td_color
            })
            self._in_td = False
            self._current_td_data = []
            self._current_td_color = None

        if tag == 'span' and self._in_span:
            self._in_span = False
            self._span_color = None

        self._depth -= 1

    def handle_data(self, data):
        if self._in_td:
            self._current_td_data.append(data)
            if self._in_span and self._span_color == 'red':
                self._current_td_color = 'red'


def parse_date(date_str):
    """Return year from a date string like MM/DD/YYYY."""
    parts = date_str.strip().split('/')
    if len(parts) == 3:
        try:
            return int(parts[2])
        except ValueError:
            pass
    return None


def extract_baseline(html_content):
    """Extract baseline from HTML content.

    Rules:
    - Look at each row's last two TDs: Pre-rating and Post-rating
    - Black rating = plain number (no red color)
    - Red rating = has color:#DD0000 span
    - Find last black Post-rating with date before 2026 (year <= 2025)
    - If none, take oldest black rating
    - Return {"date": ..., "baseline": ...}
    """
    parser = RatingHistoryParser()
    parser.feed(html_content)

    # We need to find rows that have: date, pre-rating, post-rating
    # The page structure: each row typically has date in first td, ratings in last two
    # Let's collect valid data rows

    valid_entries = []  # (date_str, post_rating_val, post_rating_color, pre_rating_val, pre_rating_color)

    for row in parser.rows:
        if len(row) < 3:
            continue

        # Check if first td looks like a date MM/DD/YYYY
        first_td = row[0]['text']
        if not re.match(r'\d{1,2}/\d{1,2}/\d{4}', first_td):
            continue

        # Last two tds are pre-rating and post-rating
        pre_td = row[-2]
        post_td = row[-1]

        # Try to parse as numbers
        pre_text = pre_td['text'].strip()
        post_text = post_td['text'].strip()

        try:
            pre_val = float(pre_text)
        except (ValueError, TypeError):
            continue

        try:
            post_val = float(post_text)
        except (ValueError, TypeError):
            continue

        valid_entries.append({
            'date': first_td.strip(),
            'year': parse_date(first_td),
            'pre_val': pre_val,
            'pre_color': pre_td['color'],
            'post_val': post_val,
            'post_color': post_td['color'],
        })

    if not valid_entries:
        return {"date": None, "baseline": None}

    # Filter for black post-ratings (no red color)
    black_post_entries = [e for e in valid_entries if e['post_color'] != 'red']

    if not black_post_entries:
        # Try black pre-ratings
        black_pre_entries = [e for e in valid_entries if e['pre_color'] != 'red']
        if not black_pre_entries:
            return {"date": None, "baseline": None}
        # Use oldest black pre-rating
        oldest = black_pre_entries[-1]  # Assuming rows are in reverse chrono order
        return {"date": oldest['date'], "baseline": oldest['pre_val']}

    # Find last black post-rating with year <= 2025
    pre_2026 = [e for e in black_post_entries if e['year'] is not None and e['year'] <= 2025]

    if pre_2026:
        # Last one (most recent before 2026)
        target = pre_2026[0]  # Rows are in reverse chrono, so first is most recent
    else:
        # Take oldest black post-rating (last in list)
        target = black_post_entries[-1]

    return {"date": target['date'], "baseline": target['post_val']}


def fetch_player(name, s_param):
    """Fetch and parse a player's rating history page."""
    encoded_name = urllib.parse.quote(name, safe='')

    if s_param:
        url = f"https://www.tennisrecord.com/adult/matchhistory.aspx?year=Rating&playername={encoded_name}&s={s_param}"
    else:
        url = f"https://www.tennisrecord.com/adult/matchhistory.aspx?year=Rating&playername={encoded_name}"

    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36'
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                html = resp.read().decode('utf-8', errors='replace')
                return extract_baseline(html)
            else:
                return {"date": None, "baseline": None}
    except Exception as e:
        print(f"  ERROR fetching {name}: {e}")
        return {"date": None, "baseline": None}


def main():
    batch_file = '/Users/shirinoskooi/Documents/tennis-dashboard/data/scrape_batch_1.json'
    results_file = '/Users/shirinoskooi/Documents/tennis-dashboard/data/baseline_results.json'

    # Load batch
    with open(batch_file) as f:
        players = json.load(f)

    # Load existing results
    with open(results_file) as f:
        results = json.load(f)

    print(f"Loaded {len(players)} players to scrape")
    print(f"Existing results: {len(results)} entries")

    for i, player in enumerate(players):
        name = player['name']
        s = player.get('s', '')

        if name in results:
            print(f"[{i+1}/{len(players)}] SKIP (already exists): {name}")
            continue

        print(f"[{i+1}/{len(players)}] Fetching: {name} (s={s!r})")
        result = fetch_player(name, s)
        results[name] = result
        print(f"  -> {result}")

    # Save results
    with open(results_file, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\nDone! Total results: {len(results)} entries")


if __name__ == '__main__':
    main()
