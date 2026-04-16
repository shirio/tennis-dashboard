#!/usr/bin/env python3
"""Add per-subflight high-level summaries to the standings JSONs."""
import json, re
from pathlib import Path

DATA = Path("data")

# Manually authored summaries reflecting the actual state of each subflight.
# Update these as the season progresses.
SUMMARIES = {
    "30_A": (
        "DTC #3 (4-0) is the only unbeaten team, powered by Shi Oskooi (2.76→3.03, 4-0) "
        "and Darian McCauley (2.98→3.11, 3-0) as the D1 pair that upset Shirey (3.18) + "
        "Frazier (3.03) in W1. TPC and Anthem CC both at 3-1 are the main challengers. "
        "TPC crushed DTC #2 4-1 in their delayed W4 match — Jennifer Wang won S1, Amy "
        "Arbeli won S2, and TPC's 2D/3D pairs both won. DTC #2's only win came from "
        "Shirey + Frazier at D1 (6-2 6-3). Anthem CC has Chelsi Grady "
        "(2.86→3.00) and Crystal Ziebarth (2.83→2.97) emerging. Summerlin Arbors (2-2) "
        "has Tayoni Coleman (3-0 at S1) and Brittany Carlson (2.71→2.86, 3-1) as risers. "
        "DTC #2 collapsed to 1-3 after the TPC loss — their 1# Doubles pair going down "
        "is a structural problem. Red Rock CC, Life Time Fitness/GV, and Southern "
        "Highlands round out the bottom at 1-3."
    ),
    "30_B": (
        "Four teams tied at 3-1 at the top — Spanish Trail, Desert Palm, Whitney Mesa Park, "
        "and DTC #4 — with nothing separating them yet. Spanish Trail leads on tiebreakers, "
        "built on sirina Shouldis (2.87→2.94) and Chelsie Hawkinson (2.83→2.87). Desert "
        "Palm's ratings are flat despite wins — Kristyl Addison (3.06, 4-0) is ceiling-capped "
        "because every opponent has been below baseline. Nourjan Bonney (2.97→3.07, 3-0 at "
        "S1) is Desert Palm's singles force. Whitney Mesa Park draws from cross-listed 3.5 "
        "players. DTC #1 (2-2) and Lake Las Vegas Sports Club, Spanish Oaks (both 1-3) fill "
        "the middle. Club Ridges (0-4) is winless across all lines."
    ),
    "35_A": (
        "Summerlin Arbors (3-1) leads thanks to Rika Cook (3.54→3.81, 3-1) and Nancy "
        "Holland (3.47→3.64) as the D1 anchor. DTC #2 (3-1) is the other top team with "
        "Seda Sargsyan (3.92) handling S1. Life Time Fitness/GV and Anthem CC both at 2-2 — "
        "LTF has Joann Komanowski (3.54→3.29, -0.25) significantly overrated, which drags "
        "their depth. Red Rock CC #1 (1-3) has the division's biggest riser in Yan Uyeno "
        "(3.55→3.83, 4-0) — her wins are the main reason Red Rock is relevant at all. "
        "Spanish Trail #2 (1-3) relies heavily on cross-listed 3.0 players and is generally "
        "overmatched."
    ),
    "35_B": (
        "Red Rock CC #2 (4-0) is the class of the subflight with Mary Lind (3.64, 4-0 "
        "across all lines) leading a deep roster. Dragonridge CC and DTC #1 sit at 2-1, "
        "TPC at 2-2. TPC has the division's most underrated player in Arika Carrier "
        "(3.60→3.74, 3-0 at D1). Dragonridge has Melanie Isbell (3-0 at S1/S2) — a singles "
        "problem for everyone. DTC #1 has solid D1 depth in Kiyono + Hernandez but no "
        "dominant singles threat. Spanish Trail #1 (1-3) and Desert Palm (1-2) sit in "
        "the middle. DTC #3 (0-3) is the weakest team — their roster is built from "
        "cross-listed 3.0 players with negative deltas across the board."
    ),
}

for fname, sfx in [("standings_women_30.json", "30"), ("standings_women_35.json", "35")]:
    path = DATA / fname
    data = json.loads(path.read_text())
    for sf in data.get("subflights", []):
        lbl = sf.get("flight_label", "")
        key = f"{sfx}_{lbl}"
        if key in SUMMARIES:
            sf["subflight_summary"] = SUMMARIES[key]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Updated {fname}")
