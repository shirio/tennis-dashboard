"""
engine/appeals.py
Which way did an A (appeal) rating go?

- Prior-year level differs from the appealed level: certain (3.5 -> 3.0 A is down).
- Same level: use TennisRecord's estimated dynamic as of the last match before the
  year-end cutoff (Nov 8) of the appeal season (data/tr_appeal_dynamics.json, from
  scrapers/scrape_tr_appeals.py). Above the level -> the computer most likely bumped
  her up, so she appealed down. At or below it -> appealed up.
- Flagged for review: within 0.10 of the level's top edge, no matches before the
  cutoff, or more than a level below the band (possible wrong profile).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DYN_FILE = ROOT / "data" / "tr_appeal_dynamics.json"
CLOSE = 0.10  # within this of a level edge -> flag for review
UP_ZONE = 0.20  # TR within this of the band's bottom edge reads as an appeal up (Shirin)


def load_dynamics(path: Path = DYN_FILE) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {d["name"].lower(): d for d in json.loads(path.read_text()) if not d.get("unmatched")}


def appeal_call(level, prior_level, rec: dict | None, prior_year: int = 2025, earlier=None) -> dict | None:
    """{'dir': 'up'|'down', 'close': bool, 'why': str} or None when nothing is known.
    `earlier` = the level she played the season before `prior_level`, if known."""
    if level is None:
        return None
    lvl = float(level)
    if prior_level is not None and float(prior_level) != lvl:
        p = float(prior_level)
        d = "down" if p > lvl else "up"
        return {"dir": d, "close": False,
                "why": f"Appealed {d}: {p:.1f} in {prior_year}, {lvl:.1f} after the appeal"}
    if lvl <= 2.5:  # nothing below 2.5, so a 2.5 A can only be an appeal down (Shirin)
        return {"dir": "down", "close": False, "why": "Appealed down: there is no level below 2.5"}
    lo = lvl - 0.5
    last = (rec or {}).get("last")
    if last:
        x, src = float(last["dyn"]), f"TennisRecord estimate {float(last['dyn']):.2f} as of {last['date']}, the last match before the {prior_year} year-end cutoff"
    elif rec and rec.get("dyn_now") is not None:
        x, src = float(rec["dyn_now"]), f"no {prior_year} matches; current TennisRecord estimate {float(rec['dyn_now']):.2f}"
    else:
        return {"dir": None, "close": True, "why": "Appeal direction unknown: no match data"}
    # Shirin, 2026-11-25:
    # - Above the level: the computer bumped her up, so she appealed down. Any margin.
    # - Dropping a level takes a rating at or near the band's bottom edge (within
    #   UP_ZONE of it, e.g. under 3.70 for a 4.0), so that reads as an appeal up.
    # - In between, where she came from decides: a level higher the season before
    #   means she was bumped back up and appealed down, a level lower means up.
    # - Otherwise it's a real toss-up: lean to the nearer edge and flag it.
    far = x <= lo - 0.5  # more than a level below the band: odd, check the profile match
    hist_note = ""
    if x > lvl:
        d, close = "down", False
    elif x <= lo + UP_ZONE:
        d, close = "up", far
    elif earlier is not None and float(earlier) != lvl:
        d, close = ("down" if float(earlier) > lvl else "up"), False
        hist_note = f"; she played {float(earlier):.1f} the season before"
    else:
        d, close = ("down" if lvl - x < x - lo else "up"), True
    close = close or not last
    band = f"{lo:.2f}–{lvl:.2f}"
    return {"dir": d, "close": close, "x": round(x, 2),
            "why": f"{'Probably appealed' if close else 'Appealed'} {d}: {src}, {lvl:.1f} band is {band}{hist_note}"
                   + (" (more than a level away, needs review)" if far else " (close, needs review)" if close else "")}
