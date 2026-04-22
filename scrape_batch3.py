#!/usr/bin/env python3
"""Scrape Tennis Record Rating History pages for pre-season baselines (batch 3)."""

import json
import re
import time
import urllib.request
import urllib.parse


def parse_year(date_str):
    """Return year int from 'MM/DD/YYYY' string, or None."""
    parts = date_str.strip().split("/")
    if len(parts) == 3:
        try:
            return int(parts[2])
        except ValueError:
            return None
    return None


def date_sort_key(date_str):
    parts = date_str.strip().split("/")
    if len(parts) == 3:
        try:
            return (int(parts[2]), int(parts[0]), int(parts[1]))
        except ValueError:
            return (0, 0, 0)
    return (0, 0, 0)


def find_baseline(html):
    """
    Parse html and return (date_str, baseline_float) or (None, None).

    Table columns (0-indexed, 10 cols per data row):
      0=Date, 1=League, 2=Team, 3=Court, 4=Partner, 5=Opponent(s),
      6=W/L, 7=Result, 8=Match(pre), 9=Rating(post)

    Black rating: td[9] contains plain number (no span with DD0000).
    Red rating: td[9] wraps value in <span style="color:#DD0000">.
    """
    # Extract all <tr>...</tr> blocks (non-greedy)
    rows = re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', html)

    candidates_before_2026 = []  # (date_str, post_val)
    all_black = []               # (date_str, year, post_val)

    for row in rows:
        tds = re.findall(r'<td[^>]*>([\s\S]*?)</td>', row)
        if len(tds) != 10:
            continue

        date_raw = tds[0]
        post_raw = tds[9]

        # Extract date text
        date_str = re.sub(r'<[^>]+>', '', date_raw).strip()
        if not re.match(r'\d{1,2}/\d{1,2}/\d{4}', date_str):
            continue

        year = parse_year(date_str)
        if year is None:
            continue

        # Determine if post-rating is red
        is_red = bool(re.search(r'DD0000', post_raw, re.IGNORECASE))

        if is_red:
            continue  # skip red

        # Extract post-rating value
        post_text = re.sub(r'<[^>]+>', '', post_raw).strip()
        try:
            post_val = float(post_text)
        except ValueError:
            continue

        # Black rating found
        all_black.append((date_str, year, post_val))
        if year <= 2025:
            candidates_before_2026.append((date_str, post_val))

    if candidates_before_2026:
        # Sort by date, take the LAST (most recent) before 2026
        candidates_before_2026.sort(key=lambda x: date_sort_key(x[0]))
        last = candidates_before_2026[-1]
        return last[0], last[1]
    elif all_black:
        # Take the OLDEST black rating on page
        all_black.sort(key=lambda x: date_sort_key(x[0]))
        oldest = all_black[0]
        return oldest[0], oldest[2]
    else:
        return None, None


def scrape_player(name, s_param):
    encoded_name = urllib.parse.quote(name, safe="")
    base_url = f"https://www.tennisrecord.com/adult/matchhistory.aspx?year=Rating&playername={encoded_name}"
    if s_param:
        url = base_url + f"&s={s_param}"
    else:
        url = base_url

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; scraper/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ERROR fetching {name}: {e}")
        return {"date": None, "baseline": None}

    if status != 200:
        return {"date": None, "baseline": None}

    date_str, baseline = find_baseline(html)
    return {"date": date_str, "baseline": baseline}


def main():
    input_path = "/Users/shirinoskooi/Documents/tennis-dashboard/data/scrape_batch_3.json"
    output_path = "/Users/shirinoskooi/Documents/tennis-dashboard/data/baseline_results_batch3.json"

    with open(input_path) as f:
        players = json.load(f)

    results = {}
    total = len(players)

    for i, player in enumerate(players):
        name = player["name"]
        s = player["s"]
        print(f"[{i+1}/{total}] {name} (s={s!r})")
        result = scrape_player(name, s)
        results[name] = result
        print(f"  -> {result}")
        # Polite rate limiting
        time.sleep(0.3)

    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nDone. Results written to {output_path}")
    nulls = sum(1 for v in results.values() if v["baseline"] is None)
    print(f"  Total: {total}, Nulls: {nulls}, Found: {total - nulls}")


if __name__ == "__main__":
    main()
