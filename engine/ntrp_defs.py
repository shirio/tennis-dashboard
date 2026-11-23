"""
engine/ntrp_defs.py
Shared USTA NTRP definitions.

Rating-type letters come from the "Key to Type of Rating" on TennisLink's
Find NTRP Rating Info page. A full NTRP rating is a level plus one of these
letters, e.g. "3.0 C" or "3.5 S".
"""
from __future__ import annotations

import re

RATING_TYPES: dict[str, str] = {
    "S": "Self-rate or Medical Appeal",
    "A": "Appeal",
    "C": "Computer",
    "D": "Disqualification",
    "E": "Early Start League dynamic",
    "M": "Mixed Exclusive Year-end Rating",
    "T": "Tournament Exclusive Year End Rating",
}

# USTA scorecard quirk (see scrapers/scrape_tennislink.py): on a retirement,
# TennisLink prints "Retired" under the WINNER's name, next to the green
# checkmark, even though the OTHER player retired and lost. The checkmark is
# what marks the winner; the word "Retired" never identifies who retired.

_NTRP_RE = re.compile(r"^\s*(\d\.\d)\s*([A-Z])?\s*$")


def parse_ntrp(value: str | None) -> tuple[float | None, str | None]:
    """Split a full NTRP string into (level, letter): "3.0 C" -> (3.0, "C")."""
    if not value:
        return None, None
    m = _NTRP_RE.match(str(value))
    if not m:
        return None, None
    return float(m.group(1)), m.group(2)


def format_ntrp(level, letter: str | None) -> str:
    """(3.0, "C") -> "3.0 C"; (3.0, None) -> "3.0"; (None, _) -> ""."""
    if level in (None, ""):
        return ""
    s = f"{float(level):.1f}"
    return f"{s} {letter}" if letter else s
