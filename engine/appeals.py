"""
engine/appeals.py
Which way did an A (appeal) rating go?

- Prior-year level differs from the appealed level: certain (3.5 -> 3.0 A is down).
- Same level: use TennisRecord's estimated dynamic after the player's last match of
  the prior year (data/tr_appeal_dynamics.json, from scrapers/scrape_tr_appeals.py).
  Above the level -> the computer most likely bumped her up, so she appealed down.
  At or below the bottom of the level's band -> she most likely dropped and appealed up.
- More than a level past the edge (odd), close to an edge, inside the band (the estimate disagrees with an appeal being
  needed at all), or no prior-year matches: we still pick a direction but flag it.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DYN_FILE = ROOT / "data" / "tr_appeal_dynamics.json"
CLOSE = 0.10  # within this of a level edge -> flag for review


def load_dynamics(path: Path = DYN_FILE) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {d["name"].lower(): d for d in json.loads(path.read_text()) if not d.get("unmatched")}


def appeal_call(level, prior_level, rec: dict | None, prior_year: int = 2025) -> dict | None:
    """{'dir': 'up'|'down', 'close': bool, 'why': str} or None when nothing is known."""
    if level is None:
        return None
    lvl = float(level)
    if prior_level is not None and float(prior_level) != lvl:
        p = float(prior_level)
        d = "down" if p > lvl else "up"
        return {"dir": d, "close": False,
                "why": f"Appealed {d}: {p:.1f} in {prior_year}, {lvl:.1f} after the appeal"}
    lo = lvl - 0.5
    last = (rec or {}).get("last")
    if last:
        x, src = float(last["dyn"]), f"TennisRecord estimate {float(last['dyn']):.2f} after the last {prior_year} match ({last['date']})"
    elif rec and rec.get("dyn_now") is not None:
        x, src = float(rec["dyn_now"]), f"no {prior_year} matches; current TennisRecord estimate {float(rec['dyn_now']):.2f}"
    else:
        return {"dir": None, "close": True, "why": "Appeal direction unknown: no match data"}
    if x > lvl:
        d, margin = "down", x - lvl
    elif x <= lo:
        d, margin = "up", lo - x
    else:  # inside the band: lean toward the nearer edge
        d, margin = ("down" if lvl - x < x - lo else "up"), 0.0
    far = margin >= 0.5  # more than a level past the edge: odd, check the profile match
    close = margin < CLOSE or not last or far
    band = f"{lo:.2f}–{lvl:.2f}"
    return {"dir": d, "close": close, "x": round(x, 2),
            "why": f"{'Probably appealed' if close else 'Appealed'} {d}: {src}, {lvl:.1f} band is {band}"
                   + (" (more than a level away, needs review)" if far else " (close, needs review)" if close else "")}
