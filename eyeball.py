#!/usr/bin/env python3
"""Print named-player ratings — used to eyeball lever-by-lever changes during reform."""
import json
from pathlib import Path

NAMED = [
    "Shi L Oskooi", "Darian McCauley", "Yarisbel Schieck",
    "Emmy Perez", "Prexy Tamayo", "Anna Clark",
    "Christine LaBarre", "Kara Gaston",
    "Lisa Schnitz", "Shawanna Johnson",
    "Leticia Schoff", "Kimberly Lippisch", "Arika Carrier",
    "Tina Shirey", "Irene Frazier",
    "Kellie Woods", "Kim Knotts", "Moon-Hui Choi",
    "Tayoni Coleman",  # singles-only example
]

players = json.loads((Path("data") / "players.json").read_text())
by_name = {p.get("name", "").lower(): p for p in players}

print(f"{'Name':<22} {'BL':>5} {'R30':>6} {'R35':>6} {'GLOB':>6}  W-L 3.0 / 3.5")
print("-" * 70)
for name in NAMED:
    p = by_name.get(name.lower()) or next(
        (v for k, v in by_name.items() if name.lower() in k), None)
    if not p:
        print(f"{name:<22}  [not found]")
        continue
    bl = p.get("dynamic_rating_baseline")
    r30 = p.get("rating_30")
    r35 = p.get("rating_35")
    gl = p.get("global_rating")
    wl30 = p.get("wl_record_30", "")
    wl35 = p.get("wl_record_35", "")
    print(
        f"{name:<22} "
        f"{bl if bl is not None else '-':>5} "
        f"{r30 if r30 is not None else '-':>6} "
        f"{r35 if r35 is not None else '-':>6} "
        f"{gl if gl is not None else '-':>6}  "
        f"{wl30:>5} / {wl35}"
    )
