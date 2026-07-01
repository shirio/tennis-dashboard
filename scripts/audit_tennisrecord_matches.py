#!/usr/bin/env python3
"""
Audit every player linked to a tennisrecord.com profile for a wrong-person
match. Tier 1 (cheap): fetch each profile page, extract "(City, State)" from
the header, and flag anyone whose profile state doesn't match our stored
player.state — a strong, fast signal (as with Elizabeth Anderson / Susan
Anderson / Laura Peterson). Doesn't require match-history parsing.

Usage:
    python3 scripts/audit_tennisrecord_matches.py                 # full run
    python3 scripts/audit_tennisrecord_matches.py --state CO      # one state
    python3 scripts/audit_tennisrecord_matches.py --limit 50      # smoke test
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests
from scrape_players import fetch

PLAYERS_JSON = Path("data/players.json")
OUT_JSON = Path("data/tennisrecord_audit_flagged.json")
BASE_URL = "https://www.tennisrecord.com"


def _full_url(url: str) -> str:
    """profile_url is stored as either a relative path or a full URL —
    normalize to full so requests.get doesn't choke on 'No scheme supplied'."""
    return url if url.startswith("http") else BASE_URL + url

# Player.state -> plausible tennisrecord location states. Not exhaustive —
# players can legitimately live just across a state line and play in a
# neighboring section, so this is a coarse first-pass filter, not a
# definitive verdict. Intermountain section covers these states.
_INTERMOUNTAIN_STATES = {"CO", "UT", "NV", "ID", "WY", "MT"}


def _extract_location(html: str) -> str | None:
    text = re.sub("<[^<]+?>", " ", html)
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"\(([^()]+,\s*[A-Z]{2})\)\s+(Male|Female)\b", text)
    return m.group(1).strip() if m else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    players = json.loads(PLAYERS_JSON.read_text())
    with_url = [p for p in players if p.get("profile_url")]
    id_only = [p for p in players if p.get("tennisrecord_id") and not p.get("profile_url")]
    for p in id_only:
        p["profile_url"] = (f"/adult/profile.aspx?playername={p['name']}"
                             f"&s={p['tennisrecord_id']}")
    linked = with_url + id_only
    if args.state:
        linked = [p for p in linked if p.get("state") == args.state.upper()]
    if args.limit:
        linked = linked[:args.limit]

    print(f"Auditing {len(linked)} players with a tennisrecord profile "
          f"({len(with_url)} via profile_url, {len(id_only)} via tennisrecord_id)")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })

    flagged = []
    checked = 0
    for p in linked:
        url = _full_url(p["profile_url"])
        html = fetch(url, session, delay=1.2, use_cache=True, cache_only=False)
        checked += 1
        if not html:
            print(f"  [{checked}/{len(linked)}] {p['name']}: fetch failed")
            continue
        loc = _extract_location(html)
        player_state = p.get("state", "")
        loc_state = loc.split(",")[-1].strip() if loc else None

        # Flag if the profile's location state is OUTSIDE the Intermountain
        # section entirely (the section our leagues are all in) — a strong
        # signal regardless of which specific state within it.
        suspicious = bool(loc_state) and loc_state not in _INTERMOUNTAIN_STATES
        marker = " [SUSPICIOUS]" if suspicious else ""
        if checked % 25 == 0 or suspicious:
            print(f"  [{checked}/{len(linked)}] {p['name']} ({player_state}, {p.get('team','?')}): "
                  f"profile location={loc!r}{marker}")

        if suspicious:
            flagged.append({
                "name": p["name"], "state": player_state, "team": p.get("team"),
                "division": p.get("division"), "profile_url": url,
                "profile_location": loc,
            })

    print(f"\nChecked {checked} players. Flagged {len(flagged)} as suspicious "
          f"(profile location outside Intermountain section entirely).")
    OUT_JSON.write_text(json.dumps(flagged, indent=2))
    print(f"Saved flagged list to {OUT_JSON}")


if __name__ == "__main__":
    main()
