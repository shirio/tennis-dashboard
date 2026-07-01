# ===========================================================================
# IMPORTANT NOTES — read before editing this file
# ===========================================================================
# 1. This is the v8 sequential ratings engine. Changes here affect ALL player
#    ratings — run rebuild.py after any edit to recompute.
# 2. _win_probability() uses a stepped interpolation table (_WIN_PROB_TABLE).
#    The same logic is duplicated in build_html.py as _win_prob_gap() for the
#    matchup explorer — keep them in sync if the table changes.
# 3. These notes must be preserved unless the user explicitly says to remove them.
# ===========================================================================
"""
engine/ratings.py
v8 rating algorithm: cross-pair win probability, gap-significance weighting,
set-by-set surprise scoring, magnitude-sorted diminishing returns.

Callable as a module:
    from engine.ratings import run_ratings, RatingsSummary

Or standalone:
    python3 engine/ratings.py
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"
MATCHES_JSON = DATA_DIR / "matches_all_players.json"
REGIONS_JSON = DATA_DIR / "regions.json"

# Legacy single-state paths (kept for backward compat; symlinks to standings_nv_*.json)
STANDINGS_30 = DATA_DIR / "standings_women_30.json"
STANDINGS_35 = DATA_DIR / "standings_women_35.json"


def _discover_standings_files() -> list[tuple[Path, str]]:
    """
    Discover all standings + districts files across all states.
    Returns [(path, div_suffix), ...] e.g. [(standings_nv_30.json, "30"), ...].
    """
    files = []
    regions = json.loads(REGIONS_JSON.read_text()) if REGIONS_JSON.exists() else {}
    states = list(regions.get("states", {}).keys())
    if not states:
        # Fallback to legacy files
        return [(STANDINGS_30, "30"), (STANDINGS_35, "35")]

    for st in states:
        st_lower = st.lower()
        for ntrp_suffix in ["30", "35"]:
            # Regular season standings
            standings_path = DATA_DIR / f"standings_{st_lower}_{ntrp_suffix}.json"
            if standings_path.exists():
                files.append((standings_path, ntrp_suffix))
            # Districts
            districts_path = DATA_DIR / f"districts_{st_lower}_{ntrp_suffix}.json"
            if districts_path.exists():
                files.append((districts_path, ntrp_suffix))

    if not files:
        return [(STANDINGS_30, "30"), (STANDINGS_35, "35")]
    return files


# ---------------------------------------------------------------------------
# Constants & Parameters
# ---------------------------------------------------------------------------

SCALING = 0.40          # scales raw set-surprise aggregate to rating adjustment
CAP = 0.18             # max |adjustment| per match
DIM_WEIGHTS = [1.00, 0.65, 0.45, 0.35, 0.28]   # diminishing return weights by magnitude rank
DEFAULT_OPP_RATING_30 = 3.00   # fallback opponent rating when baseline unknown
DEFAULT_OPP_RATING_35 = 3.50
MIN_MATCHES_FULL_CONFIDENCE = 3   # matches needed for full confidence on positive moves
DEPLOY_WEIGHT = 0.04              # max ±0.02 additive for deployment rate signal
LINE_PLACE_WEIGHT = 0.01          # per-tier-gap multiplier for line placement signal

# Line tier values — higher = more competitive line
_LINE_TIER: dict[str, float] = {
    "1# Singles": 5.0, "1# Doubles": 4.5,
    "2# Singles": 4.0, "2# Doubles": 3.0,
    "3# Doubles": 1.5,
}

# Lever 1 — Per-match line-difficulty weighting.
# Replaces the old flat singles 1.25× multiplier. A win at S1 against a real
# opponent says more about your skill than a 3rd-doubles closeout win against
# a weaker pair. The multipliers below scale each match's adjustment.
# Singles still gets a small lift over equivalent doubles line because of
# its cleaner 1v1 signal, but doubles top lines now get rewarded too.
_LINE_DIFFICULTY_MULT: dict[str, float] = {
    "1# Singles": 1.30,
    "1# Doubles": 1.20,
    "2# Singles": 1.15,
    "2# Doubles": 1.00,
    "3# Doubles": 0.70,
}
_LINE_DIFFICULTY_DEFAULT = 1.0

# Lever 2 — Partner-asymmetric credit in doubles.
# When a doubles team's partner-gap (|player − partner|) is large, the credit/
# blame should be redistributed: a strong player who wins alongside a weaker
# partner gets MORE credit (carrying signal); the weaker partner gets LESS
# (they were carried). Inverse for losses: the stronger player takes LESS
# blame because the loss was partially their partner's contribution.
#
# Parameters:
#   gap_threshold:  ratings gap below which no asymmetry is applied (0.20)
#   gap_ceiling:    gap at which the multiplier hits its extreme (0.50)
#                   = threshold + 0.30 (linear ramp over that range)
#   win_boost:      max multiplier for stronger partner in a win   (+0.40)
#   win_damp:       symmetric dampen for weaker partner in a win   (−0.40)
#   loss_dampen:    multiplier scale-down for stronger partner's blame in a loss (0.30)
#                   → stronger pays 0.7× of normal at full ramp
#   loss_amplify:   multiplier scale-up for weaker partner's blame  (+0.30)
#                   → weaker pays 1.3× of normal at full ramp
_PARTNER_GAP_THRESHOLD = 0.20
_PARTNER_GAP_CEILING = 0.50
_PARTNER_WIN_BOOST = 0.40
_PARTNER_WIN_DAMP = 0.40
_PARTNER_LOSS_DAMPEN = 0.30
_PARTNER_LOSS_AMPLIFY = 0.30


def _partner_mult(player_r: float, partner_r: Optional[float], won: bool) -> float:
    """Compute the Lever 2 partner-asymmetric multiplier for a doubles match.

    Returns 1.0 for singles (partner_r is None) or when the partner-gap is
    below threshold. Otherwise scales linearly between threshold and ceiling.
    """
    if partner_r is None or player_r is None:
        return 1.0
    gap = abs(player_r - partner_r)
    if gap < _PARTNER_GAP_THRESHOLD:
        return 1.0
    ramp = min(1.0, (gap - _PARTNER_GAP_THRESHOLD)
               / (_PARTNER_GAP_CEILING - _PARTNER_GAP_THRESHOLD))
    is_stronger = player_r > partner_r
    if won:
        if is_stronger:
            return 1.0 + _PARTNER_WIN_BOOST * ramp     # up to 1.40×
        return 1.0 - _PARTNER_WIN_DAMP * ramp          # down to 0.60×
    # Loss
    if is_stronger:
        return 1.0 - _PARTNER_LOSS_DAMPEN * ramp       # down to 0.70× (less blame)
    return 1.0 + _PARTNER_LOSS_AMPLIFY * ramp          # up to 1.30× (more blame)
# Division rating floor (approx lowest dynamic baseline in each division)
_DIV_FLOOR: dict[str, float] = {"3.0": 2.50, "3.5": 3.00}

# Default baseline for players who have no dynamic_rating_baseline, keyed by NTRP prefix.
# Used when a player appears in match data but was never rated (e.g. late additions,
# sub players). Better than a flat 3.0 because it keeps opponent strength estimates
# consistent within each division.
_NTRP_DEFAULT_RATING: dict[str, float] = {
    "2.5": 2.10,
    "3.0": 2.60,
    "3.5": 3.10,
    "4.0": 3.60,
    "4.5": 4.10,
}


def ntrp_default(division: str) -> float:
    """Return the assumed baseline for a player with no dynamic_rating_baseline.

    Parses the NTRP level from the division string (e.g. '3.0 Women B' → 3.0)
    and returns the configured default for that level.
    """
    for prefix, default in _NTRP_DEFAULT_RATING.items():
        if division.startswith(prefix):
            return default
    return 3.0   # last-resort fallback for unrecognised division strings

# Stepped win-probability table: (gap_threshold, probability)
# gap = player_rating - opponent_rating
# Entries checked top-to-bottom; first matching threshold wins.
_WIN_PROB_TABLE = [
    (0.40, 0.82),
    (0.30, 0.75),
    (0.20, 0.68),
    (0.10, 0.58),
    (0.00, 0.50),
    (-0.10, 0.42),
    (-0.20, 0.32),
    (-0.30, 0.25),
    (-0.40, 0.18),
]
_WIN_PROB_FLOOR = 0.12   # extreme underdog (gap < -0.40)

# Gap-significance table: (|expected - 0.5| threshold, significance)
_GAP_SIG_TABLE = [
    (0.30, 1.00),
    (0.20, 0.85),
    (0.10, 0.65),
]
_GAP_SIG_FLOOR = 0.40

# Set dominance: (loser_games, dominance_score)
_SET_DOM_TABLE = {
    0: 1.00,   # 6-0
    1: 0.75,   # 6-1
    2: 0.50,   # 6-2
    3: 0.40,   # 6-3
    4: 0.25,   # 6-4
    5: 0.10,   # 7-5
    6: 0.10,   # 7-6
}

# Cross-pair weights for doubles (top=higher rated, bottom=lower rated)
_CP_TOP_TOP = 0.40
_CP_TOP_BOT = 0.25
_CP_BOT_TOP = 0.25
_CP_BOT_BOT = 0.10

# ---------------------------------------------------------------------------
# Scenario signal tables — "the story of the set scores"
# ---------------------------------------------------------------------------
# Key: (s1_player_won, s1_is_rout, s2_player_won, s2_is_rout)
# A set is a "rout" when _set_dominance(winner_games, loser_games) > 0.40
# (i.e. 6-0 / 6-1 / 6-2). All other sets are "even" (6-3, 6-4, 7-5, 7-6).
# Note: 6-3 has dominance exactly 0.40 — strict > means it is "even", not a rout.
# In straight sets the player always wins both (won=True) or loses both (won=False),
# so the match outcome is implied by the set outcomes — no need for a 5th key dimension.
_SIGNAL_2SET: dict[tuple, float] = {
    # Straight-set wins (player won both sets)
    (True,  True,  True,  True):  +1.00,   # Rout win  + Rout win
    (True,  False, True,  True):  +0.85,   # Even win  + Rout win  (finishing dominant)
    (True,  True,  True,  False): +0.75,   # Rout win  + Even win
    (True,  False, True,  False): +0.60,   # Even win  + Even win
    # Straight-set losses (player lost both sets)
    (False, True,  False, True):  -1.00,   # Rout loss + Rout loss
    (False, False, False, True):  -0.85,   # Even loss + Rout loss  (fell apart at the end)
    (False, True,  False, False): -0.75,   # Rout loss + Even loss
    (False, False, False, False): -0.60,   # Even loss + Even loss
}

# Key: (s1_player_won, s1_is_rout, s2_player_won, s2_is_rout, match_won)
# For 3-set tiebreaks: s1/s2 describe sets 1 & 2; the tiebreak outcome is the match result.
#
# Design: the tiebreak (whether a 10-point supertiebreak or a full third set) is
# treated as a near-coin-flip that resolves a tie — not a meaningful skill signal on
# its own. Only the S1/S2 story matters for magnitude; the TB result just determines
# sign. Signals are symmetric: winning the TB gives +M, losing it gives -M, where M
# depends on the S1/S2 dominance pattern. Range is compressed ~3× vs. straight-set
# signals so a 3-set win never overpowers a dominant 2-set result.
_SIGNAL_3SET: dict[tuple, float] = {
    # Won the tiebreak — S1/S2 story determines how much credit
    (False, False, True,  True,  True):  +0.25,  # Even S1 loss  + Rout S2 win  → won TB
    (True,  True,  False, False, True):  +0.20,  # Rout S1 win   + Even S2 loss → won TB
    (False, True,  True,  True,  True):  +0.15,  # Rout S1 loss  + Rout S2 win  → won TB
    (False, False, True,  False, True):  +0.13,  # Even S1 loss  + Even S2 win  → won TB
    (False, True,  True,  False, True):  +0.12,  # Rout S1 loss  + Even S2 win  → won TB
    (True,  False, False, False, True):  +0.10,  # Even S1 win   + Even S2 loss → won TB (even match)
    (True,  True,  False, True,  True):  +0.08,  # Rout S1 win   + Rout S2 loss → won TB
    (True,  False, False, True,  True):  +0.05,  # Even S1 win   + Rout S2 loss → won TB
    # Lost the tiebreak — symmetric: same S1/S2 magnitude, negated
    (False, False, True,  True,  False): -0.25,  # Even S1 loss  + Rout S2 win  → lost TB
    (True,  True,  False, False, False): -0.20,  # Rout S1 win   + Even S2 loss → lost TB
    (False, True,  True,  True,  False): -0.15,  # Rout S1 loss  + Rout S2 win  → lost TB
    (False, False, True,  False, False): -0.13,  # Even S1 loss  + Even S2 win  → lost TB
    (False, True,  True,  False, False): -0.12,  # Rout S1 loss  + Even S2 win  → lost TB
    (True,  False, False, False, False): -0.10,  # Even S1 win   + Even S2 loss → lost TB (even match)
    (True,  True,  False, True,  False): -0.08,  # Rout S1 win   + Rout S2 loss → lost TB
    (True,  False, False, True,  False): -0.05,  # Even S1 win   + Rout S2 loss → lost TB
}

_ROUT_THRESHOLD = 0.40   # strict > threshold: dom > 0.40 is a rout (6-0/6-1/6-2); 6-3 (dom=0.40) is even


@dataclass
class RatingsSummary:
    players_updated: int = 0
    players_skipped: int = 0   # no match data found


@dataclass
class MatchRecord:
    opponent_ratings: list[float]       # 1 for singles, 2 for doubles
    partner_rating: Optional[float]     # None for singles
    won: bool
    date: str                           # "M/DD/YYYY"
    division: str                       # "3.0" or "3.5"
    match_id: str
    line_label: str                     # e.g. "1# Doubles"
    score: str                          # e.g. "6-4 6-2"


@dataclass
class CourtEvent:
    """One unique court line across all divisions, used for sequential global rating."""
    date: str
    match_id: str
    line_label: str
    division: str
    winner_keys: list[str]              # name_key(s) of winning side
    loser_keys: list[str]               # name_key(s) of losing side
    score: str


# ---------------------------------------------------------------------------
# Helpers (kept from original)
# ---------------------------------------------------------------------------

def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def _build_players_by_name(players: list[dict]) -> dict[str, dict]:
    """Build name→player dict.

    Same-name players in different states (e.g. two 'Tina Taylor') get
    keyed as '{state}::{name_key}' so they never overwrite each other.
    Non-ambiguous players keep their plain name_key.
    """
    from collections import Counter
    counts: Counter = Counter(
        _name_key(p.get("name", "")) for p in players if _name_key(p.get("name", ""))
    )
    ambiguous = {k for k, c in counts.items() if c > 1}
    result: dict[str, dict] = {}
    for p in players:
        k = _name_key(p.get("name", ""))
        if not k:
            continue
        if k in ambiguous:
            st = (p.get("state") or "??").lower()
            result[f"{st}::{k}"] = p
        else:
            result[k] = p
    return result


def _resolve_key(name: str, file_state: str, players_by_name: dict[str, dict]) -> str:
    """Return the correct players_by_name key for *name*, preferring the
    state-qualified variant when it exists (disambiguates same-name players)."""
    nk = _name_key(name)
    if file_state:
        qk = f"{file_state}::{nk}"
        if qk in players_by_name:
            return qk
    return nk


def _parse_player_names(field_val: str) -> list[str]:
    """
    Extract individual player names from a line's player field.
    Handles:  "Anna Clark"  |  "Anna Clark / Jane Doe"
    Strips score suffixes like "  6-3" after a comma.
    """
    if not field_val:
        return []
    cleaned = re.sub(r",?\s*\d+-\d+.*$", "", field_val).strip()
    parts = re.split(r"\s*/\s*", cleaned)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# v8 Algorithm Components
# ---------------------------------------------------------------------------

def _win_probability(gap: float) -> float:
    """Interpolated win probability based on rating gap (player - opponent).
    Uses the stepped table as anchor points but linearly interpolates between them
    for smoother behavior at gap boundaries.
    """
    gap = round(gap, 4)  # avoid float boundary issues
    # Above highest threshold
    if gap >= _WIN_PROB_TABLE[0][0]:
        return _WIN_PROB_TABLE[0][1]
    # Below lowest threshold
    if gap < _WIN_PROB_TABLE[-1][0]:
        return _WIN_PROB_FLOOR
    # Find the two bracketing entries and interpolate
    for i in range(len(_WIN_PROB_TABLE) - 1):
        hi_gap, hi_prob = _WIN_PROB_TABLE[i]
        lo_gap, lo_prob = _WIN_PROB_TABLE[i + 1]
        if gap >= lo_gap:
            # Interpolate between lo and hi
            frac = (gap - lo_gap) / (hi_gap - lo_gap) if hi_gap != lo_gap else 0
            return lo_prob + frac * (hi_prob - lo_prob)
    return _WIN_PROB_FLOOR


def _cross_pair_expected(player_rating: float,
                         partner_rating: Optional[float],
                         opponent_ratings: list[float]) -> float:
    """
    Cross-pair expected win probability.
    Singles: P(player > opponent).
    Doubles: weighted combination of all 4 cross-pair matchups.
    """
    if len(opponent_ratings) == 1 or partner_rating is None:
        # Singles (or doubles where partner rating is unknown → treat as singles)
        opp_r = opponent_ratings[0] if opponent_ratings else player_rating
        return _win_probability(player_rating - opp_r)

    # Doubles: sort each side by rating
    our = sorted([player_rating, partner_rating], reverse=True)   # [top, bottom]
    their = sorted(opponent_ratings[:2], reverse=True)             # [top, bottom]

    tt = _win_probability(our[0] - their[0])
    tb = _win_probability(our[0] - their[1])
    bt = _win_probability(our[1] - their[0])
    bb = _win_probability(our[1] - their[1])

    return _CP_TOP_TOP * tt + _CP_TOP_BOT * tb + _CP_BOT_TOP * bt + _CP_BOT_BOT * bb


def _gap_significance(expected: float) -> float:
    """How informative is this matchup? Further from 50% = more meaningful."""
    d = abs(expected - 0.50)
    for threshold, sig in _GAP_SIG_TABLE:
        if d >= threshold:
            return sig
    return _GAP_SIG_FLOOR


def _set_dominance(winner_games: int, loser_games: int) -> float:
    """How dominant was the set result?"""
    # 10-point tiebreak (1-0 / 0-1): essentially a coin flip — minimal dominance
    if winner_games <= 1 and loser_games <= 1:
        return 0.05
    return _SET_DOM_TABLE.get(loser_games, 0.10)


def _parse_sets(score_str: str) -> list[tuple[int, int, bool]]:
    """
    Parse a score like '7-5 5-7 1-0' into list of (winner_games, loser_games, first_side_won_set).
    first_side_won_set=True means the first number was higher (or 1-0 tiebreak).
    """
    sets: list[tuple[int, int, bool]] = []
    for part in score_str.split():
        m = part.split("-")
        if len(m) != 2:
            continue
        try:
            a, b = int(m[0]), int(m[1])
        except (ValueError, TypeError):
            continue
        if a == 1 and b == 0:
            sets.append((1, 0, True))      # tiebreak set, first side won
        elif a == 0 and b == 1:
            sets.append((0, 1, False))     # tiebreak set, second side won
        elif a > b:
            sets.append((a, b, True))      # first side won set
        elif b > a:
            sets.append((b, a, False))     # second side won set
        # skip ties (shouldn't happen in tennis)
    return sets


def _surprise_weight(expected: float, won: bool) -> float:
    """
    How much does this result update our belief that ratings are correct?

    Computed as |actual - expected|: how far the outcome was from what ratings
    predicted. An underdog winning (upset) and a favourite losing (upset) are
    equivalent — both are strong signals that ratings are off. A favourite winning
    and an underdog losing are both weak signals — they just confirm the status quo.

    This is symmetric by construction: surprise_level for a win = 1 - expected,
    which equals expected for a loss when the match flips — both measure the same
    "distance from expectation" regardless of direction.

    Returns a multiplier in [0.10, 1.00].
    """
    surprise_level = abs((1.0 if won else 0.0) - expected)
    if surprise_level >= 0.70:
        return 1.00
    if surprise_level >= 0.55:
        return 0.75
    if surprise_level >= 0.40:
        return 0.40
    if surprise_level >= 0.25:
        return 0.20
    return 0.10


def _scenario_signal(sets: list[tuple[int, int, bool]], won: bool) -> float:
    """
    Map the match's set-by-set story to a raw signal in [-1.0, +1.0].

    Classifies set 1 and set 2 from the focal player's perspective (did they win
    the set, and was it a rout?).  A set is a rout when
    _set_dominance(winner_games, loser_games) > _ROUT_THRESHOLD (covers 6-0/6-1/6-2 only;
    6-3 has dominance exactly 0.40 and is classified as "even").

    For 2-set matches: looks up _SIGNAL_2SET.
    For 3-set tiebreaks: looks up _SIGNAL_3SET using sets 1 & 2 plus match outcome.
    Falls back to ±0.50 for any unrecognised scenario.
    """
    if len(sets) < 2:
        return 0.50 if won else -0.50

    s1, s2 = sets[0], sets[1]

    # Did the focal player win each of the first two sets?
    # Convention: score is stored from the LINE WINNER's perspective.
    # first_side_won=True means the first number in the score was larger (winner's side won that set).
    # The focal player is the "first side" when record.won=True.
    s1_pw = (s1[2] == won)
    s2_pw = (s2[2] == won)

    s1_rout = _set_dominance(s1[0], s1[1]) > _ROUT_THRESHOLD
    s2_rout = _set_dominance(s2[0], s2[1]) > _ROUT_THRESHOLD

    is_3set = len(sets) == 3
    if is_3set:
        key = (s1_pw, s1_rout, s2_pw, s2_rout, won)
        return _SIGNAL_3SET.get(key, 0.50 if won else -0.50)
    else:
        key = (s1_pw, s1_rout, s2_pw, s2_rout)
        return _SIGNAL_2SET.get(key, 0.50 if won else -0.50)


def _match_adjustment(player_rating: float, record: MatchRecord,
                      scaling: float = SCALING, cap: float = CAP) -> float:
    """
    Compute the rating adjustment for a single match.

    Uses a scenario-based set signal: the "story" of the set scores maps to a
    raw_signal in [-1, +1] (e.g. rout-win-both = +1.00, even-loss-both = -0.60).
    That signal is compared against the expected_signal derived from cross-pair
    win probability, so upsets produce large adjustments and expected outcomes
    produce small ones — in both directions.

    Underdog protection: a player expected to lose (expected < 0.50) never gets a
    negative adjustment from a loss — losing as expected is not evidence of
    being overrated.

    Singles bonus: singles matches carry a 1.25× multiplier because a 1v1 result
    is a cleaner signal than doubles (no partner contribution to mask individual ability).
    """
    expected = _cross_pair_expected(
        player_rating, record.partner_rating, record.opponent_ratings
    )

    sets = _parse_sets(record.score)
    if not sets:
        # No score available — fall back to match outcome only with surprise weighting
        sw = _surprise_weight(expected, record.won)
        surprise = (1.0 if record.won else 0.0) - expected
        adj = surprise * sw * scaling
        return max(-cap, min(cap, adj))

    raw_signal = _scenario_signal(sets, record.won)

    # Underdog tiebreak credit: an underdog (expected < 0.50) who loses in a
    # 3-set tiebreak has overperformed their expectation regardless of the
    # set-by-set story.  The tiebreak itself is a coin flip — they performed at
    # roughly 50% level when expected at <50%.  Floor the raw_signal at −0.15
    # (the "best" tiebreak-loss scenario in the table: even fight, narrowly lost)
    # so the scenario table's judgment can't bury a genuinely competitive showing.
    if not record.won and expected < 0.50 and len(sets) == 3:
        raw_signal = max(raw_signal, -0.15)

    # Map expected [0, 1] → [-1, +1] to match the raw_signal scale.
    # expected=0.50 → 0.0 (even match), expected=0.80 → +0.60 (favoured)
    expected_signal = 2.0 * expected - 1.0

    # Heavy underdog loss: the linear formula underestimates how bad a loss
    # is "expected" to look. A 13.6% underdog is expected to lose in routs
    # (Rout+Rout = −1.0), not at the −0.73 midpoint. Blend expected_signal
    # toward −1.0 as underdog severity increases so that any set where the
    # underdog holds their own reads as a genuine overperformance.
    #   expected=0.30 → no shift (boundary)
    #   expected=0.20 → expected_signal ≈ −0.73  (small shift)
    #   expected=0.13 → expected_signal ≈ −0.88  (meaningful shift)
    #   expected=0.00 → expected_signal = −1.00  (fully worst-case)
    if not record.won and expected < 0.30:
        underdog_factor = (0.30 - expected) / 0.30
        expected_signal = (
            expected_signal * (1.0 - underdog_factor) + (-1.0) * underdog_factor
        )

    surprise = raw_signal - expected_signal

    adj = surprise * scaling

    adj = max(-cap, min(cap, adj))

    # Directional enforcement: losses produce negative signals, wins positive.
    # Favorites (expected ≥ 0.50) who lose must always drop — no floor.
    if not record.won and expected >= 0.50:
        adj = min(adj, 0.0)   # favorite who loses must drop or stay flat

    # Underdog protection: any player expected to lose (expected < 0.50) never gets a
    # negative adjustment from a loss.  Going to a tiebreak against stronger opponents
    # is an overperformance regardless of the set-by-set story — the scenario table
    # is calibrated for neutral players and shouldn't override this floor.
    if not record.won and expected < 0.50:
        adj = max(adj, 0.0)   # underdog who lost: never penalised

    return adj


def _sequential_match_adj(current_rating: float, record: MatchRecord) -> float:
    """
    Per-match adjustment for the incremental sequential system.

    Two rules govern positive (win) adjustments:

    1. Minimum surprise gate: the win's scenario signal must exceed the
       expected signal by at least _MIN_WIN_SURPRISE (0.15). Wins that land
       within the expected range earn nothing — squeezing through a 3-set
       tiebreak as the heavy favourite is not evidence of a higher rating.

    2. Win cap — two-regime formula, continuous at expected=0.50:
         Underdogs (< 0.50): linear  SEQ_CAP × (1 − expected)
         Favourites (≥ 0.50): steep  SEQ_CAP × 2 × (1 − expected)²
         • 18% underdog upset  → win_cap ≈ 0.123
         • 50/50 match winner  → win_cap = 0.075
         • 68% favourite       → win_cap ≈ 0.031
         • 82% favourite       → win_cap ≈ 0.010  (tiny)

    Loss cap scaled by expected²: symmetrically to the win cap, a loss hurts you
    more when you were the heavy favourite and less when you were the underdog.
      • 82% favourite loses → loss_cap = 0.15 × 0.82² ≈ 0.101  (large penalty)
      • 50/50 match loser  → loss_cap = 0.15 × 0.50² = 0.038
      • 18% underdog loses → loss_cap = 0.15 × 0.18² ≈ 0.005  (nearly nothing)
    Underdogs expected to lose (expected < 0.50) are floored at 0 by
    _match_adjustment — they are never penalised for expected losses but CAN
    earn a positive adj for overperforming.
    """
    adj = _match_adjustment(current_rating, record)

    # SEQ_CAP: maximum a single match can move your sequential rating.
    # 0.15 lets one genuine outlier match (dominant upset, bad day) move
    # a rating meaningfully in a short season — rather than capping all
    # matches at ±0.05 and making real skill invisible for weeks.
    _SEQ_CAP = 0.15

    # Minimum surplus above expected_signal needed to earn any positive credit.
    _MIN_WIN_SURPRISE = 0.15

    # Scale for the below-gate penalty: wins that barely met expectations earn
    # a negative adj proportional to how far short of the gate they fell.
    # Max penalty = 0.15 × scale (when surplus = 0, i.e. win exactly at expectation).
    # 0.30 → max ≈ 0.045; at surplus=0.068 (Julie's case): penalty ≈ 0.025
    _BELOW_GATE_SCALE = 0.30

    # Lever 1 — per-match line-difficulty multiplier applied to the FINAL output
    # (after caps), so a D3 win moves you less and an S1 win moves you more.
    line_mult = _LINE_DIFFICULTY_MULT.get(record.line_label, _LINE_DIFFICULTY_DEFAULT)
    # Lever 2 — partner-asymmetric credit. The carrier gets more credit;
    # the carried partner gets less. For losses, the stronger partner takes
    # less blame.
    partner_mult = _partner_mult(current_rating, record.partner_rating, record.won)

    def _scale(x: float) -> float:
        return x * line_mult * partner_mult

    if record.won:
        expected = _cross_pair_expected(
            current_rating, record.partner_rating, record.opponent_ratings
        )
        sets = _parse_sets(record.score)
        if sets:
            raw_signal = _scenario_signal(sets, record.won)
            expected_signal = 2.0 * expected - 1.0
            surplus = raw_signal - expected_signal
            if surplus < _MIN_WIN_SURPRISE:
                # Below gate: win landed below expected quality.
                # Penalty is proportional to the shortfall but capped at
                # _MIN_WIN_SURPRISE * _BELOW_GATE_SCALE — a winner never drops
                # more than a small nudge regardless of how badly they won.
                # Without this cap, a heavy favorite who barely survives a
                # tiebreak (raw_signal << expected_signal) produces a deeply
                # negative adj, causing the winner to end up below the loser.
                capped_shortfall = min(_MIN_WIN_SURPRISE - surplus, _MIN_WIN_SURPRISE)
                penalty = capped_shortfall * _BELOW_GATE_SCALE
                return _scale(-penalty)
        # Cleared the gate (or no score data): scale win cap by how unexpected
        # the win was.
        #
        # For the win cap, use the focal player's INDIVIDUAL expected win
        # probability (average win_prob vs each opponent) when they are the
        # STRONGER partner in doubles. The cross-pair team expected is dragged
        # down by a weak partner, producing an inflated cap and then a 1.40×
        # partner_mult boost on top — a double effect that over-rewards the
        # strong player for every win regardless of opponent quality.
        # Individual expected correctly captures "how dominant was this player
        # vs those specific opponents" independent of who they were paired with.
        # Weak partners (cap_expected == expected): no change — they already
        # get the team-level expected and a 0.60× damp via partner_mult.
        cap_expected = expected
        if (record.partner_rating is not None
                and len(record.opponent_ratings) >= 2
                and current_rating > record.partner_rating):
            n = len(record.opponent_ratings)
            cap_expected = sum(
                _win_probability(current_rating - opp_r)
                for opp_r in record.opponent_ratings
            ) / n

        # Heavy underdogs (cap_expected < 0.30 — win prob ≤ 25%): linear
        #   win_cap = SEQ_CAP × (1 − cap_expected)
        # Everyone else (cap_expected ≥ 0.30): squared formula
        #   win_cap = SEQ_CAP × (1 − cap_expected)²
        if cap_expected < 0.30:
            win_cap = _SEQ_CAP * (1.0 - cap_expected)
        else:
            win_cap = _SEQ_CAP * (1.0 - cap_expected) ** 2
        return _scale(min(win_cap, max(adj, 0.0)))

    # Loss path.
    if not record.won:
        expected = _cross_pair_expected(
            current_rating, record.partner_rating, record.opponent_ratings
        )
        if adj > 0:
            # Underdog who overperformed (e.g. pushed to a tiebreak): treat like
            # a win for capping purposes — the gain should be meaningful but bounded
            # by how unexpected the result was, same formula as upset wins.
            if expected < 0.30:
                win_cap = _SEQ_CAP * (1.0 - expected)
            else:
                win_cap = _SEQ_CAP * (1.0 - expected) ** 2
            return _scale(min(win_cap, adj))
        # Normal loss: cap proportional to expected² so heavy underdogs are
        # barely penalised and favourites are appropriately penalised.
        loss_cap = _SEQ_CAP * expected ** 2
        return _scale(max(-loss_cap, adj))

    return _scale(max(-_SEQ_CAP, min(_SEQ_CAP, adj)))


# ---------------------------------------------------------------------------
# Score-gap mapping: how much rating gap does this score dominance imply?
# ---------------------------------------------------------------------------

_SCORE_GAP = {
    # What rating advantage (above opponent) is implied by winning a set with
    # this many loser-games?  Calibrated so that the most-likely explanation for
    # a 6-N score matches the win-probability table:
    #   6-0 bagel     → opponent won nothing  → ~0.45 gap  (82-88% win prob per game)
    #   6-1           → opponent won 1 game   → ~0.28 gap  (~75% win prob)
    #   6-2           → opponent won 2 games  → ~0.18 gap  (~68% win prob)
    #   6-3           → solid but contested   → ~0.10 gap  (~60% win prob)
    #   6-4           → moderate edge         → ~0.05 gap  (~55% win prob)
    #   7-5 / 7-6     → near-even, slight edge
    0: 0.45,
    1: 0.30,
    2: 0.18,
    3: 0.10,
    4: 0.05,
    5: 0.02,
    6: 0.00,   # tiebreak — treat as even
}


def _implied_rating_from_match(record: MatchRecord) -> Optional[float]:
    """
    Infer what rating the player must have been at to produce this result.
    For wins: implied = opponent_strength + score_gap
    For losses: implied = opponent_strength - score_gap
    For doubles: accounts for partner rating.
    Returns None if data insufficient.
    """
    if not record.opponent_ratings:
        return None

    # For doubles wins: use the STRONGEST opponent as the effective benchmark.
    # Winning a doubles match proves you (and your partner) can handle the best
    # that pair has. Using avg would understate the quality of the win when one
    # opponent is significantly stronger (Arika beating a 3.58+3.50 pair should
    # imply ~3.60, not ~3.56). For losses and singles: keep average (you lost
    # to the pair as a whole; avg is the right benchmark for the loss ceiling).
    #
    # EXCEPTION — lopsided opponent pair: if the opponents are very mismatched
    # (spread ≥ 0.35), a dominant score is more likely explained by the weak
    # partner than by the player beating the strong one. Use avg in this case
    # to avoid inflating the implied rating (Maryna Post 6-0 6-0 vs 3.01+1.96:
    # max would imply 3.11, but the bagel is just the 1.96 dragging the pair).
    is_doubles = record.partner_rating is not None and len(record.opponent_ratings) >= 2
    if is_doubles and record.won:
        opp_ratings = record.opponent_ratings[:2]
        spread = max(opp_ratings) - min(opp_ratings)
        if spread >= 0.35:
            opp_strength = sum(opp_ratings) / len(opp_ratings)   # lopsided pair → avg
        else:
            opp_strength = max(opp_ratings)                       # balanced pair → max
    else:
        opp_strength = sum(record.opponent_ratings) / len(record.opponent_ratings)

    # Parse sets to get average score gap
    sets = _parse_sets(record.score)
    if not sets:
        gap = 0.0
    else:
        gaps = []
        for wg, lg, first_won in sets:
            if wg <= 1 and lg <= 1:
                gaps.append(0.0)  # tiebreak
            else:
                player_won_set = (first_won == record.won)
                g = _SCORE_GAP.get(lg, 0.0)
                gaps.append(g if player_won_set else -g)
        gap = sum(gaps) / len(gaps) if gaps else 0.0

    # Implied rating = opponent strength ± score gap
    if record.won:
        return opp_strength + gap
    else:
        return opp_strength - gap


def _compute_v8_rating(baseline: float, matches: list[MatchRecord],
                       scaling: float = SCALING, cap: float = CAP,
                       n_total_weeks: int = 4,
                       division: str = "3.0") -> float:
    """
    Compute v8 rating from baseline using all match records.
    1. Compute per-match adjustments (all from baseline perspective).
    2. Sort by absolute magnitude (most impactful first).
    3. Apply diminishing-return weights.
    4. Apply asymmetric confidence scaling (positive moves attenuated by sample size).
    5. Add deployment rate prior (captain's selection signal).
    6. Add line placement signal (deployment context vs baseline).
    """
    if not matches or baseline is None:
        return baseline

    n_matches = len(matches)

    # --- Steps 1-4: adjustments, diminishing returns, confidence ---
    # Adjustments are sorted by magnitude so the most impactful match
    # gets the highest diminishing-return weight.
    # Positive (win) contributions and negative (loss) contributions are
    # accumulated separately so confidence can be applied independently —
    # this prevents a single large loss from "sign-flipping" the total and
    # bypassing the evidence requirement that governs positive moves.
    adjustments = [_match_adjustment(baseline, m, scaling, cap) for m in matches]
    sorted_adj = sorted(adjustments, key=lambda x: abs(x), reverse=True)
    pos_total = 0.0   # weighted sum of positive (win) adjustments
    neg_total = 0.0   # weighted sum of negative (loss) adjustments
    for i, adj in enumerate(sorted_adj):
        w = DIM_WEIGHTS[i] if i < len(DIM_WEIGHTS) else DIM_WEIGHTS[-1]
        if adj >= 0:
            pos_total += adj * w
        else:
            neg_total += adj * w  # remains negative

    deploy_rate = n_matches / n_total_weeks if n_total_weeks > 0 else 0
    evidence = (min(1.0, n_matches / MIN_MATCHES_FULL_CONFIDENCE)
                * min(1.0, deploy_rate * 2))

    # Both directions get the same evidence-based confidence.
    # The asymmetry between wins and losses is already handled upstream by
    # _surprise_weight: a surprising loss gets weight=1.0 while an expected
    # win gets weight=0.10 — a 10× difference built into the adjustment itself.
    # Adding a separate floor on losses here would be double-counting that asymmetry.
    match_total = pos_total * evidence + neg_total * evidence

    # --- Step 5: deployment rate prior ---
    deploy_prior = (deploy_rate - 0.5) * DEPLOY_WEIGHT
    deploy_prior = max(-0.02, min(0.02, deploy_prior))

    # --- Step 6: line placement signal ---
    div_floor = _DIV_FLOOR.get(division, 2.50)
    expected_tier = (baseline - div_floor) * 10
    tier_sum = 0.0
    tier_count = 0
    for m in matches:
        t = _LINE_TIER.get(m.line_label)
        if t is not None:
            tier_sum += t
            tier_count += 1
    if tier_count > 0:
        actual_avg_tier = tier_sum / tier_count
        line_adj = (actual_avg_tier - expected_tier) * LINE_PLACE_WEIGHT
    else:
        line_adj = 0.0

    surprise_rating = baseline + match_total + deploy_prior + line_adj

    # --- Step 7: implied-rating validation ---
    # Check that the computed rating is consistent with who the player beat/lost to.
    # Compute "implied rating" from each match: what rating would you need to produce
    # that result? Use the max-implied-from-wins as a floor and min-implied-from-losses
    # as a ceiling. Blend toward the implied range if the surprise-based rating is outside.
    implied = [_implied_rating_from_match(m) for m in matches]
    win_implied = [r for r, m in zip(implied, matches) if r is not None and m.won]
    loss_implied = [r for r, m in zip(implied, matches) if r is not None and not m.won]

    # Floor from wins: to beat someone rated X, you must be at least ~X.
    # The hardest win (highest implied) is the strongest evidence of ability.
    implied_floor = max(win_implied) if win_implied else None

    # Ceiling from wins: only meaningful when wins against STRONG opponents prove
    # an upper bound. If a player's best win implies 3.56 but their baseline is 3.60,
    # that win doesn't prove they CAN'T be 3.60 — it just proves a lower bound.
    # Win ceiling only applies when it exceeds baseline (i.e., wins against strong
    # opponents show you belong at a high level, limiting further inflation).
    implied_win_ceiling = max(win_implied) if win_implied else None

    # --- Win ceiling (Cases A / B / C) ---
    #
    # WIN CEILING:
    #   Case A — win_implied > baseline: wins against strong opponents → hard cap at
    #     win_implied (prevents inflation above what wins actually prove).
    #   Case B — win_implied is CLOSE to baseline (gap ≤ WIN_CEIL_GAP) AND player
    #     faced at least one opponent at/above their baseline: near-level wins →
    #     no ceiling (Arika beating 3.54 opponents at 3.597 proves she belongs there).
    #     Case B is BLOCKED when player individually outrated every opponent beaten:
    #     weak partner can make pair even-odds vs below-baseline opponents, artificially
    #     inflating adjustments (Kristin Stowe 2.63 with 2.36 partner vs 2.50 opps).
    #   Case C — wins far below baseline OR player outrated every opponent they beat:
    #     cap at baseline + small evidence-based nudge (+0.02 per win, max +0.08).
    WIN_CEIL_GAP = 0.05    # wins within 0.05 below baseline → near-level, no ceiling

    max_opp_beaten = max(
        (max(m.opponent_ratings) for m in matches if m.won and m.opponent_ratings),
        default=None,
    )
    player_outrated_all_opps = (
        max_opp_beaten is not None and max_opp_beaten < baseline
    )

    effective_win_ceil: Optional[float] = None
    if implied_win_ceiling is not None:
        if implied_win_ceiling > baseline:
            effective_win_ceil = implied_win_ceiling          # Case A
        elif baseline - implied_win_ceiling <= WIN_CEIL_GAP and not player_outrated_all_opps:
            effective_win_ceil = None                         # Case B — no cap
        else:
            n_wins = sum(1 for m in matches if m.won)
            evidence_nudge = min(0.08, n_wins * 0.02)
            effective_win_ceil = baseline + evidence_nudge    # Case C

    # --- Implied-rating constraints ---

    # Win ceiling: hard cap at what wins prove you're capable of.
    if effective_win_ceil is not None and surprise_rating > effective_win_ceil:
        surprise_rating = effective_win_ceil

    # --- Per-loss soft pulls ---
    # Each loss independently blends the rating toward that loss's implied value.
    # More unexpected the loss (bigger upset), the harder the pull.
    #
    # Pull strength: base 30% blend, up to 65% for extreme upsets (opponent 0.40+
    # below baseline). Losses processed worst-first (lowest implied) so the most
    # damning loss has first impact; later losses may not apply if rating already
    # pulled below their implied.
    LOSS_BLEND_BASE  = 0.30
    LOSS_BLEND_UPSET = 0.35

    for loss_val in sorted(loss_implied):          # ascending = worst upset first
        if surprise_rating > loss_val:
            gap = surprise_rating - loss_val
            upset_severity = min(1.0, max(0.0, (baseline - loss_val) / 0.40))
            blend = LOSS_BLEND_BASE + LOSS_BLEND_UPSET * upset_severity
            surprise_rating -= gap * blend

    # Win floor — enforced AFTER loss pulls.
    # Wins prove a hard lower bound on ability: if you beat someone rated X with
    # score Y, you've demonstrated you're at least X+Y level regardless of losses.
    # Applying this after loss pulls ensures dominant wins anchor the rating even
    # when prior losses in the sample would otherwise drag it below that proof.
    # (A 6-1 6-1 win against a 2.79 player proves ≥ 3.09; losses can't override that.)
    if implied_floor is not None and surprise_rating < implied_floor:
        surprise_rating = implied_floor

    return round(surprise_rating, 4)


# ---------------------------------------------------------------------------
# Scorecard swap detection
# ---------------------------------------------------------------------------

def _detect_scorecard_swap(match: dict, team_lookup: dict[str, str]) -> bool:
    """
    Vote-based detection: count identified players in home/away columns
    whose registered team matches the opposite side.
    Returns True if the scorecard columns are swapped vs official home/away.
    """
    home_team = match.get("home_team", "").lower()
    away_team = match.get("away_team", "").lower()
    if not home_team or not away_team:
        return False

    h_col_home = h_col_away = a_col_home = a_col_away = 0

    for ln in match.get("lines", []):
        for raw in re.split(r"\s*/\s*", ln.get("players_home", "")):
            t = team_lookup.get(_name_key(raw), "").lower()
            if t == home_team:
                h_col_home += 1
            elif t == away_team:
                h_col_away += 1
        for raw in re.split(r"\s*/\s*", ln.get("players_away", "")):
            t = team_lookup.get(_name_key(raw), "").lower()
            if t == home_team:
                a_col_home += 1
            elif t == away_team:
                a_col_away += 1

    swap_evidence = h_col_away + a_col_home
    normal_evidence = h_col_home + a_col_away
    return swap_evidence > normal_evidence


# ---------------------------------------------------------------------------
# Match record collection
# ---------------------------------------------------------------------------

def _collect_match_records(
    standings_files: list[tuple[Path, str]],
    players_by_name: dict[str, dict],
    opponent_rating_fields: Optional[dict[str, str]] = None,
) -> dict[str, list[MatchRecord]]:
    """
    Walk standings JSONs and extract per-player MatchRecords.

    standings_files: list of (path, division_suffix) tuples,
        e.g. [(STANDINGS_30, "30"), (STANDINGS_35, "35")]
    players_by_name: {name_key: player_dict} lookup
    opponent_rating_fields: optional dict mapping ntrp key ("3.0"/"3.5") to the player
        field to use for opponent strength, e.g. {"3.0": "iter_rating_30", "3.5": "iter_rating_35"}.
        If None (default), uses "dynamic_rating_baseline" for all divisions.
    Returns: {name_key: [MatchRecord, ...]}
    """
    records: dict[str, list[MatchRecord]] = {}

    # Build team lookup using division-specific team fields
    team_lookup: dict[str, str] = {}
    for k, p in players_by_name.items():
        team_lookup[k] = p.get("team", "")

    for path, div_suffix in standings_files:
        # Derive state from filename for same-name disambiguation
        _st_m = re.match(r"(?:standings|districts)_([a-z]+)_", path.name)
        _file_state = _st_m.group(1) if _st_m else ""

        data = _load(path, {})
        ntrp = data.get("ntrp", f"{div_suffix[0]}.{div_suffix[1]}")   # "3.0" or "3.5"
        default_opp = DEFAULT_OPP_RATING_30 if "30" in div_suffix else DEFAULT_OPP_RATING_35

        # Determine which field to use for opponent ratings in this division
        live_field = (opponent_rating_fields or {}).get(ntrp)   # None → use baseline

        def _rating_or_default(name: str, _live=live_field, _def=default_opp, _fs=_file_state) -> float:
            p = players_by_name.get(_resolve_key(name, _fs, players_by_name))
            if p:
                # Prefer the live iterative field if provided and populated
                if _live is not None:
                    r = p.get(_live)
                    if r is not None:
                        return r
                # Fall back to frozen baseline
                r = p.get("dynamic_rating_baseline")
                if r is not None:
                    return r
            return _def

        for sf in data.get("subflights", []):
            for match in sf.get("matches", []):
                if match.get("pending"):
                    continue
                match_id = match.get("match_id", "")
                date = match.get("date", "")

                for ln in match.get("lines", []):
                    # Use canonical winner/loser fields from normalization
                    cw = ln.get("court_winner")
                    if cw is None:
                        continue

                    w_names = ln.get("winner_names") or []
                    l_names = ln.get("loser_names") or []
                    if not w_names or not l_names:
                        continue

                    w_raw = " / ".join(w_names)
                    l_raw = " / ".join(l_names)

                    winner_names = _parse_player_names(w_raw)
                    loser_names = _parse_player_names(l_raw)
                    if not winner_names or not loser_names:
                        continue

                    line_label = ln.get("line", "")
                    score = ln.get("score", "")

                    # Create records for winners
                    for pname in winner_names:
                        pk = _resolve_key(pname, _file_state, players_by_name)
                        if pk not in players_by_name:
                            continue
                        partners = [n for n in winner_names if _resolve_key(n, _file_state, players_by_name) != pk]
                        partner_r = _rating_or_default(partners[0]) if partners else None
                        opp_ratings = [_rating_or_default(n) for n in loser_names]
                        if not opp_ratings:
                            opp_ratings = [default_opp]
                        records.setdefault(pk, []).append(MatchRecord(
                            opponent_ratings=opp_ratings, partner_rating=partner_r,
                            won=True, date=date, division=ntrp,
                            match_id=match_id, line_label=line_label, score=score,
                        ))

                    # Create records for losers
                    for pname in loser_names:
                        pk = _resolve_key(pname, _file_state, players_by_name)
                        if pk not in players_by_name:
                            continue
                        partners = [n for n in loser_names if _resolve_key(n, _file_state, players_by_name) != pk]
                        partner_r = _rating_or_default(partners[0]) if partners else None
                        opp_ratings = [_rating_or_default(n) for n in winner_names]
                        if not opp_ratings:
                            opp_ratings = [default_opp]
                        records.setdefault(pk, []).append(MatchRecord(
                            opponent_ratings=opp_ratings, partner_rating=partner_r,
                            won=False, date=date, division=ntrp,
                            match_id=match_id, line_label=line_label, score=score,
                        ))

    return records


def _collect_court_events(
    standings_files: list[tuple[Path, str]],
    players_by_name: dict[str, dict],
) -> list[CourtEvent]:
    """
    Collect one CourtEvent per unique court line across all standings files.
    Used for sequential global rating computation.
    """
    seen: set[tuple[str, str]] = set()   # (match_id, line_label)
    events: list[CourtEvent] = []
    team_lookup: dict[str, str] = {k: p.get("team", "") for k, p in players_by_name.items()}

    for path, div_suffix in standings_files:
        _st_m = re.match(r"(?:standings|districts)_([a-z]+)_", path.name)
        _file_state = _st_m.group(1) if _st_m else ""

        data = _load(path, {})
        ntrp = data.get("ntrp", f"{div_suffix[0]}.{div_suffix[1]}")

        def _rk(name: str, _fs: str = _file_state) -> str:
            return _resolve_key(name, _fs, players_by_name)

        for sf in data.get("subflights", []):
            for match in sf.get("matches", []):
                if match.get("pending"):
                    continue
                match_id = match.get("match_id", "")
                date = match.get("date", "")
                for ln in match.get("lines", []):
                    line_label = ln.get("line", "")
                    dedup_key = (match_id, line_label)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    cw = ln.get("court_winner")
                    if cw is None:
                        continue

                    w_names = ln.get("winner_names") or []
                    l_names = ln.get("loser_names") or []
                    if not w_names or not l_names:
                        continue

                    w_raw = " / ".join(w_names)
                    l_raw = " / ".join(l_names)

                    winner_keys = [
                        _rk(n) for n in _parse_player_names(w_raw)
                        if _rk(n) in players_by_name
                    ]
                    loser_keys = [
                        _rk(n) for n in _parse_player_names(l_raw)
                        if _rk(n) in players_by_name
                    ]
                    if not winner_keys and not loser_keys:
                        continue

                    events.append(CourtEvent(
                        date=date, match_id=match_id, line_label=line_label,
                        division=ntrp, winner_keys=winner_keys,
                        loser_keys=loser_keys, score=ln.get("score", ""),
                    ))

    return events


def _date_sort_key(ev: "CourtEvent") -> tuple:
    try:
        m, d, y = ev.date.split("/")
        return (int(y), int(m), int(d))
    except Exception:
        return (0, 0, 0)


def _date_str_sort_key(date_str: str) -> tuple:
    try:
        m, d, y = date_str.split("/")
        return (int(y), int(m), int(d))
    except Exception:
        return (0, 0, 0)


def _compute_division_sequential(
    court_events: list[CourtEvent],
    division: str,
    baselines: dict[str, float],
    n_total_weeks: int,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """
    Compute per-division ratings chronologically using incremental per-match updates.

    Each player starts at their baseline rating. After every match, _match_adjustment
    is called on the player's CURRENT running rating and the result is added directly
    to that running value. The baseline is NEVER used again after initialization —
    not even as an anchor for subsequent matches.

    Opponent ratings used inside each MatchRecord are snapshotted at the moment the
    match is played (pre-match running values), so earlier results inform later ones
    without circular dependencies.

    Returns:
        final_ratings:      {player_key: rating_after_all_matches}
        pre_match_timeline: {player_key: {date_str: rating_going_INTO_that_date}}
            This timeline is used to display "what was this player's rating at the
            time of this match" in the results tab.
    """
    from collections import defaultdict

    div_events = sorted(
        (ev for ev in court_events if ev.division == division),
        key=_date_sort_key,
    )

    events_by_date: dict[str, list[CourtEvent]] = defaultdict(list)
    for ev in div_events:
        events_by_date[ev.date].append(ev)

    dates_sorted = sorted(events_by_date.keys(), key=_date_str_sort_key)

    # Running ratings — start everyone at their baseline
    running: dict[str, float] = dict(baselines)

    # pre_match_timeline[player_key][date] = rating going INTO that date
    pre_match: dict[str, dict[str, float]] = {}

    # Lever 4 — detect "high-confidence under-rating" pattern per player.
    # Tracks running W-L and opponent quality. Criteria (all must hold):
    #   ≥3 matches; ≥65% win rate; avg opp baseline ≥ player baseline + 0.25.
    # The +0.25 gap is critical: it targets players who are clearly misclassified
    # (Lippisch BL 2.18 beating real 3.0 opponents BL ~2.85, gap 0.67; Arika
    # BL 3.16 dominating 3.5 opponents BL ~3.40+). It prevents normal 3.0
    # players with good records (Kristyl BL 2.91, avg opp ~2.91) from triggering
    # the doubled cap just because they have a winning season.
    _UNDERRATED_MIN_MATCHES = 3
    _UNDERRATED_WIN_RATE = 0.65
    _UNDERRATED_OPP_GAP = 0.25   # avg opp must be 0.25 above player baseline
    underrated_stats: dict[str, dict] = {}   # pk -> {wins, total, opp_sum, opp_n}

    def _is_underrated(pk: str) -> bool:
        s = underrated_stats.get(pk)
        if not s or s["total"] < _UNDERRATED_MIN_MATCHES:
            return False
        if s["wins"] / s["total"] < _UNDERRATED_WIN_RATE:
            return False
        if s["opp_n"] == 0:
            return False
        avg_opp = s["opp_sum"] / s["opp_n"]
        player_bl = baselines.get(pk, 0)
        return avg_opp >= player_bl + _UNDERRATED_OPP_GAP

    # Lever 6 — coach-trust line-deployment tracking.
    # We track each player's line distribution; after all matches a post-pass
    # rewards consistent top-line deployment (S1/D1) and penalizes D3-lock
    # for players whose baseline is high enough to expect better.
    line_deployment: dict[str, dict[str, int]] = {}   # pk -> {line_label: count}

    # Lever 5 — chronic-loser positive-move ceiling.
    # Track running W-L per player IN THIS DIVISION. Once a player has ≥3
    # matches and <30% win rate, any further LOSSES in this division cannot
    # contribute a positive adj (games-stolen-via-strong-partner inflation).
    # Wins still count normally — a chronic loser who actually wins gets the
    # full upside signal.
    _CHRONIC_MIN_MATCHES = 3
    _CHRONIC_WIN_RATE_CEILING = 0.30
    wl_counts: dict[str, list[int]] = {}   # pk -> [wins, losses]

    def _is_chronic_loser(pk: str) -> bool:
        w, l = wl_counts.get(pk, (0, 0))[0], wl_counts.get(pk, (0, 0))[1]
        n = w + l
        if n < _CHRONIC_MIN_MATCHES:
            return False
        return (w / n) < _CHRONIC_WIN_RATE_CEILING

    for date in dates_sorted:
        today = events_by_date[date]

        # Everyone playing today in this division
        involved: set[str] = set()
        for ev in today:
            involved.update(ev.winner_keys + ev.loser_keys)

        # Snapshot ratings before today's matches — this is what opponents
        # "look like" from the perspective of each match played on this date.
        # Baseline is ONLY used here to fill in players who have never played
        # before (i.e. their running[pk] hasn't been set yet).
        snap: dict[str, float] = {
            pk: running.get(pk, baselines.get(pk, ntrp_default("")))
            for pk in involved
        }

        # Record pre-match rating in the timeline
        for pk in involved:
            pre_match.setdefault(pk, {})[date] = snap[pk]

        # Independent per-player adjustments — no zero-sum coupling.
        # Every winner and loser is computed from their own current rating and
        # their own match context.  Cross-rating asymmetry is handled naturally:
        # a 3.0 beating a 4.0 produces a large positive surprise for the 3.0
        # and a large negative surprise for the 4.0 — independently, not mirrored.
        updates: dict[str, float] = {}

        for ev in today:
            # --- Winners ---
            for pk in ev.winner_keys:
                if pk not in baselines:
                    continue
                partners_w = [k for k in ev.winner_keys if k != pk]
                partner_r = snap.get(partners_w[0]) if partners_w else None
                opp_r = [snap.get(k, baselines.get(k, ntrp_default(""))) for k in ev.loser_keys] or [ntrp_default("")]
                rec = MatchRecord(
                    opponent_ratings=opp_r, partner_rating=partner_r,
                    won=True, date=date, division=division,
                    match_id=ev.match_id, line_label=ev.line_label, score=ev.score,
                )
                prev = updates.get(pk, snap[pk])
                adj = _sequential_match_adj(prev, rec)
                # Lever 4 — underrated-player aggressive uplift.
                # If pattern detected, scale up the adj by 2× (effectively
                # raising the seq cap from 0.15 → 0.30 for this match only).
                if adj > 0 and _is_underrated(pk):
                    adj = min(adj * 2.0, 0.30)
                updates[pk] = round(prev + adj, 4)
                # Lever 5 W-L bookkeeping
                wl_counts.setdefault(pk, [0, 0])[0] += 1
                # Lever 6 deployment bookkeeping
                _dep = line_deployment.setdefault(pk, {})
                _dep[ev.line_label] = _dep.get(ev.line_label, 0) + 1
                # Lever 4 bookkeeping (winners): record opp baselines
                _us = underrated_stats.setdefault(pk, {
                    "wins": 0, "total": 0, "opp_sum": 0.0, "opp_n": 0
                })
                _us["wins"] += 1
                _us["total"] += 1
                for ok in ev.loser_keys:
                    _opp_bl = baselines.get(ok)
                    if _opp_bl is not None:
                        _us["opp_sum"] += _opp_bl
                        _us["opp_n"] += 1

            # --- Losers ---
            for pk in ev.loser_keys:
                if pk not in baselines:
                    continue
                partners_l = [k for k in ev.loser_keys if k != pk]
                partner_r = snap.get(partners_l[0]) if partners_l else None
                opp_r = [snap.get(k, baselines.get(k, ntrp_default(""))) for k in ev.winner_keys] or [ntrp_default("")]
                rec = MatchRecord(
                    opponent_ratings=opp_r, partner_rating=partner_r,
                    won=False, date=date, division=division,
                    match_id=ev.match_id, line_label=ev.line_label, score=ev.score,
                )
                prev = updates.get(pk, snap[pk])
                adj = _sequential_match_adj(prev, rec)
                # Lever 5 — chronic loser: block positive contributions from
                # *losses* (games-stolen narrow-loss signals). Real wins still
                # count above; only positive adj from losses is suppressed.
                if adj > 0 and _is_chronic_loser(pk):
                    adj = 0.0
                updates[pk] = round(prev + adj, 4)
                # Lever 5 W-L bookkeeping
                wl_counts.setdefault(pk, [0, 0])[1] += 1
                # Lever 6 deployment bookkeeping
                _dep = line_deployment.setdefault(pk, {})
                _dep[ev.line_label] = _dep.get(ev.line_label, 0) + 1
                # Lever 4 bookkeeping (losers): only update total count
                _us = underrated_stats.setdefault(pk, {
                    "wins": 0, "total": 0, "opp_sum": 0.0, "opp_n": 0
                })
                _us["total"] += 1

        running.update(updates)

    # Lever 6 post-pass — coach-trust deployment signal.
    # Looking at each player's full-season line distribution:
    #   • ≥75% at top lines (S1 or D1) and ≥3 matches → +0.04 (coach trust)
    #   • ≥75% at D3 and ≥3 matches and baseline justifies higher → −0.03
    #     (D3 lock despite skill baseline = signal of weaker actual ability)
    _TOP_LINES = {"1# Singles", "1# Doubles"}
    _BOTTOM_LINES = {"3# Doubles"}
    _TOP_LOCK_FRAC = 0.75
    _BOTTOM_LOCK_FRAC = 0.75
    _TOP_LOCK_BONUS = 0.04
    _BOTTOM_LOCK_PENALTY = -0.03
    _BOTTOM_LOCK_BL_THRESHOLD = 0.30   # above div_floor + 0.30 → eligible for penalty

    div_floor = _DIV_FLOOR.get(division, 2.50)
    for pk, dist in line_deployment.items():
        total = sum(dist.values())
        if total < 3:
            continue
        top_frac = sum(dist.get(l, 0) for l in _TOP_LINES) / total
        bot_frac = sum(dist.get(l, 0) for l in _BOTTOM_LINES) / total
        baseline = baselines.get(pk, 0)
        if top_frac >= _TOP_LOCK_FRAC:
            running[pk] = round(running.get(pk, baseline) + _TOP_LOCK_BONUS, 4)
        elif bot_frac >= _BOTTOM_LOCK_FRAC and baseline >= div_floor + _BOTTOM_LOCK_BL_THRESHOLD:
            running[pk] = round(running.get(pk, baseline) + _BOTTOM_LOCK_PENALTY, 4)

    return running, pre_match


def _compute_earned_doubles(
    court_events: list[CourtEvent],
    baselines: dict[str, float],
) -> set[str]:
    """Lever 3 — flag players whose doubles record is *earned*, not partner-inflated.

    Criteria (any one qualifies):
      1. ≥ 3 doubles wins AND avg opp baseline in those wins ≥ player_bl − 0.05
         (won against opponents at or above the player's own level)
      2. ≥ 3 different doubles partners AND won with ≥ 2 of them
         (success doesn't depend on one specific strong partner)

    Used to decide whether a doubles-favored rating should be preserved
    (Tina/Irene case) or overridden by the singles-anchor (Lisa case).
    """
    from collections import defaultdict

    win_counts: dict[str, int] = defaultdict(int)
    opp_bl_sum: dict[str, float] = defaultdict(float)
    opp_bl_n: dict[str, int] = defaultdict(int)
    win_partners: dict[str, set[str]] = defaultdict(set)
    partners_seen: dict[str, set[str]] = defaultdict(set)

    for ev in court_events:
        if "Doubles" not in ev.line_label:
            continue
        # Track partners across all doubles matches (wins or losses)
        if len(ev.winner_keys) >= 2:
            for pk in ev.winner_keys:
                other = [k for k in ev.winner_keys if k != pk]
                if other:
                    partners_seen[pk].add(other[0])
                    win_partners[pk].add(other[0])
            for pk in ev.loser_keys:
                other = [k for k in ev.loser_keys if k != pk]
                if other:
                    partners_seen[pk].add(other[0])
            # Accumulate win stats
            for pk in ev.winner_keys:
                win_counts[pk] += 1
                for ok in ev.loser_keys:
                    bl = baselines.get(ok)
                    if bl is not None:
                        opp_bl_sum[pk] += bl
                        opp_bl_n[pk] += 1

    earned: set[str] = set()
    for pk, w in win_counts.items():
        if w < 3:
            continue
        player_bl = baselines.get(pk, 0)
        avg_opp_bl = opp_bl_sum[pk] / opp_bl_n[pk] if opp_bl_n[pk] > 0 else 0
        cond_quality = avg_opp_bl >= player_bl - 0.05
        cond_partners = (
            len(partners_seen.get(pk, set())) >= 3
            and len(win_partners.get(pk, set())) >= 2
        )
        if cond_quality or cond_partners:
            earned.add(pk)
    return earned


def _compute_global_sequential(
    court_events: list[CourtEvent],
    baselines: dict[str, float],
) -> tuple[dict[str, float], dict[str, dict[str, dict[str, float]]]]:
    """
    Compute global ratings by processing all court lines in chronological order,
    treating all divisions as one pool. Opponent strength at each step uses
    the running global rating (not the frozen baseline), so earlier results
    inform later ones.

    Returns:
        (global_r, global_timeline) where:
          global_r:        {player_key: final_rating}
          global_timeline: {player_key: {division: {date: pre_match_rating}}}
                           Records the running global rating going INTO each match.
                           Split by division so the results tab can display correct
                           point-in-time ratings per division page.

    Applies Levers 1-6 in the unified pass:
      • L1/L2 — embedded in _sequential_match_adj (per-match line/partner mult)
      • L4 — underrated-player aggressive uplift (≥3 matches, ≥65% wr, opp≥bl)
      • L5 — chronic-loser positive-move ceiling (≥3 matches, <30% wr)
      • L6 — coach-trust deployment post-pass (top-line bonus, D3-lock penalty)
    """
    global_r: dict[str, float] = dict(baselines)

    # Lever 4 — underrated pattern detector (global scope, cross-division)
    _UNDERRATED_MIN_MATCHES = 3
    _UNDERRATED_WIN_RATE = 0.65
    _UNDERRATED_OPP_GAP = 0.25   # avg opp must be 0.25 above player baseline
    underrated_stats: dict[str, dict] = {}

    def _is_underrated(pk: str) -> bool:
        s = underrated_stats.get(pk)
        if not s or s["total"] < _UNDERRATED_MIN_MATCHES:
            return False
        if s["wins"] / s["total"] < _UNDERRATED_WIN_RATE:
            return False
        if s["opp_n"] == 0:
            return False
        avg_opp = s["opp_sum"] / s["opp_n"]
        player_bl = baselines.get(pk, 0)
        return avg_opp >= player_bl + _UNDERRATED_OPP_GAP

    # Cross-division guard for Lever 4 (global sequential only).
    # wl_by_div is populated below as matches are processed; _is_underrated() reads
    # it so cross-division players are excluded BEFORE the first cross-div match is
    # processed — safe because `len(wl_by_div.get(pk, {})) > 1` is False until the
    # second division's first event fires.
    wl_by_div_for_l4: dict[str, set] = {}  # pk -> set of divisions seen

    def _is_underrated(pk: str) -> bool:
        s = underrated_stats.get(pk)
        if not s or s["total"] < _UNDERRATED_MIN_MATCHES:
            return False
        if s["wins"] / s["total"] < _UNDERRATED_WIN_RATE:
            return False
        if s["opp_n"] == 0:
            return False
        # Cross-division players (3.0 + 3.5) are never misclassified in this sense:
        # their 3.5 opponents always have higher baselines by design, which inflates
        # the avg-opp check and triggers false positives (e.g. a 3.0/3.5 player
        # whose 3.5 wins push avg_opp above threshold even though she's correctly
        # rated in 3.0). Lever 4 is only for single-division players.
        if len(wl_by_div_for_l4.get(pk, set())) > 1:
            return False
        avg_opp = s["opp_sum"] / s["opp_n"]
        player_bl = baselines.get(pk, 0)
        return avg_opp >= player_bl + _UNDERRATED_OPP_GAP

    # Lever 5 — chronic-loser tracker (global scope across all matches)
    _CHRONIC_MIN_MATCHES = 3
    _CHRONIC_WIN_RATE_CEILING = 0.30
    wl_counts: dict[str, list[int]] = {}

    def _is_chronic_loser(pk: str) -> bool:
        w, l = wl_counts.get(pk, [0, 0])
        n = w + l
        if n < _CHRONIC_MIN_MATCHES:
            return False
        return (w / n) < _CHRONIC_WIN_RATE_CEILING

    # Lever 6 — deployment tracker (per-division because tier semantics differ)
    line_deployment: dict[str, dict[str, dict[str, int]]] = {}   # pk -> div -> {line: n}

    # Lever 5b — per-division W-L for "struggling-in-higher-division" detection.
    # Tracks W-L per division so we can detect the Anna Clark / Leticia pattern:
    # cross-listed player who dominates lower division but loses in higher one.
    # Their unified rating is currently inflated by the lower-div success.
    wl_by_div: dict[str, dict[str, list[int]]] = {}   # pk -> div -> [w, l]
    # Lever 5b constants (also used in inline cap below and in post-pass safety net)
    _STRUGGLE_MIN_MATCHES = 4
    _STRUGGLE_WIN_RATE    = 0.30
    _STRUGGLE_CAP_DELTA   = 0.15

    # Two timelines, both split by division:
    #   global_timeline:      pre-match  (rating going INTO the first match of each day)
    #   global_post_timeline: post-match (rating AFTER all matches on each day)
    # The results tab uses pre-match for exact-date hits (showing what the player
    # was rated going in) and post-match for prior-date fallback (showing what an
    # opponent was rated *after* their last played week, not before it).
    global_timeline: dict[str, dict[str, dict[str, float]]] = {}       # pk->div->{date:pre}
    global_post_timeline: dict[str, dict[str, dict[str, float]]] = {}  # pk->div->{date:post}

    for ev in sorted(court_events, key=_date_sort_key):
        all_keys = ev.winner_keys + ev.loser_keys
        cur = {k: global_r.get(k, baselines.get(k, ntrp_default(""))) for k in all_keys}

        # Record pre-match snapshot for each known player before any updates
        for pk in all_keys:
            if pk in baselines:
                (global_timeline
                 .setdefault(pk, {})
                 .setdefault(ev.division, {})
                 .setdefault(ev.date, round(cur[pk], 4)))

        updates: dict[str, float] = {}

        # --- Winners ---
        for pk in ev.winner_keys:
            if pk not in baselines:
                continue
            partners_w = [k for k in ev.winner_keys if k != pk]
            partner_r = cur.get(partners_w[0]) if partners_w else None
            opp_ratings = [cur.get(k, baselines.get(k, ntrp_default(""))) for k in ev.loser_keys] or [ntrp_default("")]
            rec = MatchRecord(
                opponent_ratings=opp_ratings, partner_rating=partner_r,
                won=True, date=ev.date, division=ev.division,
                match_id=ev.match_id, line_label=ev.line_label, score=ev.score,
            )
            adj = _sequential_match_adj(cur[pk], rec)
            # Lever 4: lift cap when underrated pattern detected
            if adj > 0 and _is_underrated(pk):
                adj = min(adj * 2.0, 0.30)
            updates[pk] = round(cur[pk] + adj, 4)
            # Bookkeeping
            wl_counts.setdefault(pk, [0, 0])[0] += 1
            _us = underrated_stats.setdefault(pk, {
                "wins": 0, "total": 0, "opp_sum": 0.0, "opp_n": 0
            })
            _us["wins"] += 1
            _us["total"] += 1
            for ok in ev.loser_keys:
                _bl = baselines.get(ok)
                if _bl is not None:
                    _us["opp_sum"] += _bl
                    _us["opp_n"] += 1
            _div_dep = line_deployment.setdefault(pk, {}).setdefault(ev.division, {})
            _div_dep[ev.line_label] = _div_dep.get(ev.line_label, 0) + 1
            wl_by_div.setdefault(pk, {}).setdefault(ev.division, [0, 0])[0] += 1
            wl_by_div_for_l4.setdefault(pk, set()).add(ev.division)

        # --- Losers ---
        for pk in ev.loser_keys:
            if pk not in baselines:
                continue
            partners_l = [k for k in ev.loser_keys if k != pk]
            partner_r = cur.get(partners_l[0]) if partners_l else None
            opp_ratings = [cur.get(k, baselines.get(k, ntrp_default(""))) for k in ev.winner_keys] or [ntrp_default("")]
            rec = MatchRecord(
                opponent_ratings=opp_ratings, partner_rating=partner_r,
                won=False, date=ev.date, division=ev.division,
                match_id=ev.match_id, line_label=ev.line_label, score=ev.score,
            )
            adj = _sequential_match_adj(cur[pk], rec)
            # Lever 5: chronic loser → block positive bumps from losses
            if adj > 0 and _is_chronic_loser(pk):
                adj = 0.0
            updates[pk] = round(cur[pk] + adj, 4)
            # Bookkeeping
            wl_counts.setdefault(pk, [0, 0])[1] += 1
            _us = underrated_stats.setdefault(pk, {
                "wins": 0, "total": 0, "opp_sum": 0.0, "opp_n": 0
            })
            _us["total"] += 1
            _div_dep = line_deployment.setdefault(pk, {}).setdefault(ev.division, {})
            _div_dep[ev.line_label] = _div_dep.get(ev.line_label, 0) + 1
            wl_by_div.setdefault(pk, {}).setdefault(ev.division, [0, 0])[1] += 1
            wl_by_div_for_l4.setdefault(pk, set()).add(ev.division)

        global_r.update(updates)

        # Lever 5b inline cap — apply cross-division ceiling immediately after
        # each update so the timelines capture the capped values.  Without this,
        # the post-pass Lever 5b corrects the final global_r but the timelines
        # already contain the inflated values, causing the results tab to show
        # ratings like 3.40 for players whose roster rating is capped at 3.13.
        for pk in all_keys:
            if pk not in baselines:
                continue
            divs_pk = wl_by_div.get(pk, {})
            if len(divs_pk) < 2:
                continue   # not cross-listed yet
            higher_div_pk = max(divs_pk.keys(), key=lambda d: _DIV_FLOOR.get(d, 0))
            _w5b, _l5b = divs_pk[higher_div_pk]
            _n5b = _w5b + _l5b
            if _n5b < _STRUGGLE_MIN_MATCHES:
                continue
            if _w5b / _n5b >= _STRUGGLE_WIN_RATE:
                continue
            _cap5b = baselines.get(pk, 0) + _STRUGGLE_CAP_DELTA
            if global_r.get(pk, baselines.get(pk, 0)) > _cap5b:
                global_r[pk] = round(_cap5b, 4)

        # Record post-match snapshot for every player in this event (overwrites
        # on each subsequent event of the same day so the final value per day is
        # captured — used by _pit_rating fallback for opponent lookups).
        for pk in all_keys:
            if pk in baselines:
                (global_post_timeline
                 .setdefault(pk, {})
                 .setdefault(ev.division, {})[ev.date]) = round(global_r.get(pk, baselines.get(pk, 0)), 4)

    # Lever 5b post-pass safety net — catches any edge cases the inline cap
    # missed (e.g. the qualifying threshold crossed on the very last event).
    # MUST run before Lever 6 so the D3-lock penalty can push a capped player
    # below the ceiling (otherwise Lever 6 fires → Lever 5b snaps back to cap,
    # and the penalty is absorbed).  Example: Kara Gaston (BL 2.96) is capped
    # at 3.11 by Lever 5b; Lever 6 then applies -0.03 D3-lock → 3.08, which
    # is the intended result (below Darian McCauley at 3.09, who has no cap
    # and plays D1/D2 — a stronger deployment signal).
    # Higher division = the one with higher floor
    for pk, divs in wl_by_div.items():
        if len(divs) < 2:
            continue   # not cross-listed
        baseline = baselines.get(pk, 0)
        higher_div = max(divs.keys(),
                         key=lambda d: _DIV_FLOOR.get(d, 0))
        w, l = divs[higher_div]
        n = w + l
        if n < _STRUGGLE_MIN_MATCHES:
            continue
        if w / n >= _STRUGGLE_WIN_RATE:
            continue
        # Player struggles in higher division — cap unified rating.
        cap = baseline + _STRUGGLE_CAP_DELTA
        if global_r.get(pk, baseline) > cap:
            global_r[pk] = round(cap, 4)

    # Lever 6 post-pass — coach-trust deployment bonus / D3-lock penalty.
    # Runs AFTER Lever 5b ceiling so the penalty meaningfully shifts a capped
    # player downward (the D3-lock signal should override the hard cap boundary
    # and let the more nuanced deployment evidence pull the value lower).
    _TOP_LINES = {"1# Singles", "1# Doubles"}
    _BOTTOM_LINES = {"3# Doubles"}
    _TOP_LOCK_FRAC = 0.75
    _BOTTOM_LOCK_FRAC = 0.75
    _TOP_LOCK_BONUS = 0.04
    _BOTTOM_LOCK_PENALTY = -0.03
    _BOTTOM_LOCK_BL_THRESHOLD = 0.30

    for pk, by_div in line_deployment.items():
        baseline = baselines.get(pk, 0)
        adj_total = 0.0
        for div, dist in by_div.items():
            total = sum(dist.values())
            if total < 3:
                continue
            top_frac = sum(dist.get(l, 0) for l in _TOP_LINES) / total
            bot_frac = sum(dist.get(l, 0) for l in _BOTTOM_LINES) / total
            div_floor = _DIV_FLOOR.get(div, 2.50)
            if top_frac >= _TOP_LOCK_FRAC:
                adj_total += _TOP_LOCK_BONUS
            elif bot_frac >= _BOTTOM_LOCK_FRAC and baseline >= div_floor + _BOTTOM_LOCK_BL_THRESHOLD:
                adj_total += _BOTTOM_LOCK_PENALTY
        if adj_total != 0:
            global_r[pk] = round(global_r.get(pk, baseline) + adj_total, 4)

    return global_r, global_timeline, global_post_timeline


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_ratings() -> RatingsSummary:
    """
    Load all data, recompute ratings with v8 algorithm, write back to players.json.
    """
    summary = RatingsSummary()

    players: list[dict] = _load(PLAYERS_JSON, [])
    if not players:
        print("  [ratings] players.json is empty – skipping")
        return summary

    # Assign NTRP-based defaults to players with no dynamic_rating_baseline.
    # These players were added to the roster but never returned a rating from
    # Tennis Record (e.g. late subs, missing TennisRecord profiles).
    # Setting the default here means it is both used in all calculations AND
    # persisted to players.json so it shows up on the dashboard.
    for p in players:
        if p.get("dynamic_rating_baseline") is None and p.get("division"):
            p["dynamic_rating_baseline"] = ntrp_default(p["division"])

    # Build name → player lookup (state-qualified for same-name players in different states)
    players_by_name = _build_players_by_name(players)

    # Collect per-player match records from all states and divisions
    standings_files = _discover_standings_files()
    print(f"  [ratings] Loading {len(standings_files)} standings/districts files")
    all_records = _collect_match_records(standings_files, players_by_name)

    # Collect unique court events for sequential global rating
    court_events = _collect_court_events(standings_files, players_by_name)

    baselines_all = {
        k: p["dynamic_rating_baseline"]
           if p.get("dynamic_rating_baseline") is not None
           else ntrp_default(p.get("division", ""))
        for k, p in players_by_name.items()
    }

    # Count total match weeks per division (for deployment rate calculation)
    weeks_by_div: dict[str, int] = {}
    for path, div_suffix in standings_files:
        data = _load(path, {})
        ntrp = data.get("ntrp", f"{div_suffix[0]}.{div_suffix[1]}")
        dates: set[str] = set()
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if not m.get("pending") and m.get("date"):
                    dates.add(m["date"])
        weeks_by_div[ntrp] = len(dates)

    # --- Per-division sequential ratings ---
    # Chronological: each match sees opponents at their running rating AT THAT DATE,
    # not frozen baselines. The match history accumulates with locked-in opponent
    # ratings so _compute_v8_rating after each date is historically consistent.
    # Fully siloed: 3.0 events only feed 3.0 ratings; 3.5 events only feed 3.5 ratings.
    seq_30_finals, seq_30_timeline = _compute_division_sequential(
        court_events, "3.0", baselines_all, weeks_by_div.get("3.0", 4),
    )
    seq_35_finals, seq_35_timeline = _compute_division_sequential(
        court_events, "3.5", baselines_all, weeks_by_div.get("3.5", 4),
    )

    # --- Global sequential (cross-division base blend) ---
    global_sequential, global_timeline, global_post_timeline = _compute_global_sequential(court_events, baselines_all)

    # Snapshot of global_sequential before any post-pass adjustments (Lever 3,
    # Lever 5b floor).  After those levers modify global_sequential we will
    # compute the per-player delta and patch every timeline entry by the same
    # amount so the results-tab "running rating" tracks the final roster rating
    # rather than the raw sequential value.  Without this, a player boosted by
    # Lever 3 (e.g. Prexy, +0.12 singles-anchor) shows 2.99 "going into" their
    # last match even though the roster shows 3.25.
    _pre_lever_sequential: dict[str, float] = dict(global_sequential)

    # --- Lever 3: singles-anchored cross-division reconciliation ---
    # Compute singles-only and doubles-only sequential ratings, then apply
    # asymmetric override:
    #   • singles > doubles + 0.15  → use singles (Prexy ↗, exposes strong-singles players)
    #   • doubles > singles + 0.15  → check "earned doubles" badge:
    #       earned → keep blend (Tina/Irene preserved)
    #       not earned → use singles-anchor (Lisa ↘, strips partner-inflated rating)
    #   • within 0.15 → blend
    singles_events = [ev for ev in court_events if "Singles" in ev.line_label]
    doubles_events = [ev for ev in court_events if "Doubles" in ev.line_label]
    singles_only_rating, _, _ = _compute_global_sequential(singles_events, baselines_all)
    doubles_only_rating, _, _ = _compute_global_sequential(doubles_events, baselines_all)
    earned_doubles_set = _compute_earned_doubles(court_events, baselines_all)

    # Apply reconciliation as a post-pass adjustment to global_sequential
    _RECONCILE_GAP = 0.15
    def _has_singles(pk):
        return any(pk in ev.winner_keys + ev.loser_keys
                   for ev in singles_events)
    def _has_doubles(pk):
        return any(pk in ev.winner_keys + ev.loser_keys
                   for ev in doubles_events)

    for pk in list(global_sequential.keys()):
        s = singles_only_rating.get(pk)
        d = doubles_only_rating.get(pk)
        bl = baselines_all.get(pk)
        if bl is None:
            continue
        # Only meaningful if player has both kinds of matches
        if not (_has_singles(pk) and _has_doubles(pk)):
            continue
        if s is None or d is None:
            continue
        # Compare singles signal vs doubles signal as DELTAS from baseline,
        # not raw values — divisional levels differ so absolute comparison
        # isn't apples-to-apples.
        s_delta = s - bl
        d_delta = d - bl
        gap = s_delta - d_delta   # >0 means singles signal stronger
        if gap > _RECONCILE_GAP:
            # Singles much stronger than doubles → anchor up ONLY if singles
            # signal is genuinely positive (s_delta > 0.08).  Without this
            # gate, a player who wins all their (easy) singles matches while
            # losing their (hard) doubles matches triggers the upward anchor
            # from a near-zero s_delta — inflating their rating for noise.
            # Prexy (s_delta ≈ +0.17): passes.  Kim Springer (s_delta ≈ +0.02,
            # 3 easy wins over sub-2.6 opponents): blocked.
            if s_delta > 0.08:
                global_sequential[pk] = round(bl + s_delta, 4)
        elif gap < -_RECONCILE_GAP:
            # Doubles much stronger → only preserve if earned
            if pk not in earned_doubles_set:
                # Require at least 2 singles matches before anchoring down.
                # One singles win — even a dominant one (e.g. Bencini 6-1 6-3)
                # produces a tiny singles delta because it's one data point vs
                # a potentially weaker opponent.  That's not evidence of weak
                # singles; it's evidence of thin data.  Penalising a dominant
                # doubles player (8-1) for rarely playing singles is wrong.
                n_singles = sum(1 for ev in singles_events
                                if pk in ev.winner_keys + ev.loser_keys)
                if n_singles < 2:
                    pass  # not enough singles data to anchor down — leave blend
                else:
                    # Un-earned doubles inflation (Lisa) → anchor down
                    global_sequential[pk] = round(bl + s_delta, 4)
            # else: keep the blend (Tina/Irene case)

    # NOTE: There is deliberately no "Lever 5b floor" here. An earlier version
    # floored a cross-listed player's global rating at their lower-division
    # peak whenever they struggled in the higher division (<30% win rate,
    # >=4 matches) — e.g. Melissa Hicks, 6-1 in 3.0 but 0-4 in 3.5. That
    # inverted the entire point of tracking cross-division results: losing
    # badly in the higher division IS the signal that a player isn't ready
    # for it yet, regardless of how well they do in the lower one. The
    # existing in-line ceiling inside _compute_global_sequential (capping at
    # baseline+0.15 for the same struggling-in-higher-division pattern) is
    # the correct direction — it prevents over-inflation from lower-division
    # dominance without ever protecting a player from their higher-division
    # losses. A floor should not coexist with that ceiling.

    # --- Patch timelines to reflect post-pass adjustments (Lever 3) ---
    # Any player whose global_sequential value was changed by the post-pass
    # levers needs their timeline entries bumped by the same delta so the
    # results-tab running ratings are consistent with the final roster rating.
    #
    # Example: Prexy Tamayo — Lever 3 singles-anchor raises her from ~3.13
    # (raw sequential) to 3.25 (final).  Without this patch every result in
    # the 3.5 tab shows "2.93 → 2.95 → 2.99" while the roster says 3.25,
    # which the user correctly flags as "clearly wrong."
    #
    # The delta is added uniformly to ALL entries in pre- and post-match
    # timelines.  This preserves the relative shape of the season trajectory
    # (wins still show up-ticks, losses show down-ticks) while anchoring the
    # endpoint to the Lever 3/5b-adjusted final value.
    for pk in list(global_sequential.keys()):
        pre_val  = _pre_lever_sequential.get(pk)
        post_val = global_sequential.get(pk)
        if pre_val is None or post_val is None:
            continue
        delta = round(post_val - pre_val, 4)
        if abs(delta) < 0.001:
            continue  # no adjustment needed
        bl = baselines_all.get(pk, 0)
        for div_dict in global_timeline.get(pk, {}).values():
            for date in list(div_dict.keys()):
                div_dict[date] = round(div_dict[date] + delta, 4)
        for div_dict in global_post_timeline.get(pk, {}).values():
            for date in list(div_dict.keys()):
                div_dict[date] = round(div_dict[date] + delta, 4)

    # Clamp each player's FIRST pre-match timeline entry to at least their
    # baseline.  Large downward Lever 3 corrections (e.g. doubles-only player
    # like Bencini, –0.18 delta) can shift the opening entry below baseline,
    # which is always wrong — before match 1 the algorithm only knew baseline.
    for pk, div_map in global_timeline.items():
        bl = baselines_all.get(pk)
        if bl is None:
            continue
        for tl in div_map.values():
            if not tl:
                continue
            first_date = min(tl.keys(), key=_date_sort_key)
            if tl[first_date] < bl:
                tl[first_date] = round(bl, 4)

    for player in players:
        k = _name_key(player.get("name", ""))
        # Prefer state-qualified key for same-name players in different states
        st = (player.get("state") or "").lower()
        if f"{st}::{k}" in all_records:
            k = f"{st}::{k}"
        matches = all_records.get(k, [])

        baseline = player.get("dynamic_rating_baseline")
        if baseline is None:
            summary.players_skipped += 1
            continue

        if not matches:
            player["current_division_rating"] = baseline
            player["global_rating"] = baseline
            player["rating_30"] = baseline
            player["rating_35"] = baseline
            player["rating_timeline_30"] = {}
            player["rating_timeline_35"] = {}
            summary.players_skipped += 1
            continue

        # Determine player's primary division
        div = player.get("division", "")
        if "3.5" in div:
            primary_ntrp = "3.5"
        elif "3.0" in div:
            primary_ntrp = "3.0"
        elif "2.5" in div:
            primary_ntrp = "3.0"
        else:
            primary_ntrp = "3.0"

        # Lever 7 — unified global rating is the source of truth.
        # rating_30 / rating_35 become "the player's rating during this season"
        # filtered to players who played in that division. For cross-division
        # players, all three values converge — there's no longer a "different
        # rating for each level"; the global rating, informed by Levers 1-6
        # and the singles-anchored reconciliation (Lever 3), is the truth.
        unified = global_sequential.get(k, baseline)
        has_30 = any(m.division == "3.0" for m in matches)
        has_35 = any(m.division == "3.5" for m in matches)
        # Always store the unified global rating in rating_30/35 so the
        # correct value is displayed in each division's dashboard — even for
        # cross-listed players who have matches only in the other division.
        player["rating_30"] = unified
        player["rating_35"] = unified
        # Timelines show global sequential running values, split by division.
        # pre-match: rating going INTO each date (exact-hit display for active players).
        # post-match: rating AFTER all matches on each date (fallback for opponent lookups
        #             from future weeks — shows the updated value after any big upset win).
        _gtl  = global_timeline.get(k, {})
        _gptl = global_post_timeline.get(k, {})
        player["rating_timeline_30"]      = _gtl.get("3.0", {})
        player["rating_timeline_35"]      = _gtl.get("3.5", {})
        player["rating_post_timeline_30"] = _gptl.get("3.0", {})
        player["rating_post_timeline_35"] = _gptl.get("3.5", {})

        # current_division_rating = unified (their one true rating)
        player["current_division_rating"] = unified

        # global_rating = unified
        player["global_rating"] = unified

        summary.players_updated += 1

    _save(PLAYERS_JSON, players)
    print(f"  [ratings] updated {summary.players_updated} players "
          f"({summary.players_skipped} skipped – no match data)")
    return summary


# ---------------------------------------------------------------------------
# Iterative opponent ratings
# ---------------------------------------------------------------------------

def run_ratings_iterative(
    max_iterations: int = 12,
    epsilon: float = 0.001,
    damping: float = 0.65,
    verbose: bool = True,
) -> None:
    """
    Compute ratings iteratively so that opponent strengths reflect each player's
    computed rating from the previous pass rather than frozen pre-season baselines.

    Writes results to separate fields on each player dict:
        "iter_rating_30", "iter_rating_35", "iter_global_rating"
    Does NOT overwrite current_division_rating / rating_30 / rating_35 / global_rating.
    Call this after run_ratings() so that single-pass results are already present for
    comparison.

    damping: blend factor — each iteration uses (damping * computed + (1-damping) * prev).
        Values < 1.0 prevent 2-cycle oscillation in strongly coupled matchups.
    Convergence: stops when max |new - prev| across all players < epsilon.
    """
    players: list[dict] = _load(PLAYERS_JSON, [])
    if not players:
        return

    players_by_name = _build_players_by_name(players)

    standings_files = _discover_standings_files()

    # Count weeks per division (same as run_ratings)
    weeks_by_div: dict[str, int] = {}
    for path, div_suffix in standings_files:
        data = _load(path, {})
        ntrp = data.get("ntrp", f"{div_suffix[0]}.{div_suffix[1]}")
        dates: set[str] = set()
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if not m.get("pending") and m.get("date"):
                    dates.add(m["date"])
        weeks_by_div[ntrp] = len(dates)

    # Track per-iteration snapshots for verbose output
    # snapshots[iter_idx] = {name_key: (r30, r35, rglobal)}
    snapshots: list[dict[str, tuple[float, float, float]]] = []

    def _compute_all(opp_fields: Optional[dict[str, str]]) -> dict[str, tuple[float, float, float]]:
        """One full pass: collect records, compute ratings, return {nk: (r30, r35, rglobal)}."""
        all_records = _collect_match_records(standings_files, players_by_name, opp_fields)
        result: dict[str, tuple[float, float, float]] = {}
        for player in players:
            k = _name_key(player.get("name", ""))
            matches = all_records.get(k, [])
            baseline = player.get("dynamic_rating_baseline")
            if baseline is None:
                continue
            div = player.get("division", "")
            primary_ntrp = "3.5" if "3.5" in div else "3.0"
            r30 = r35 = rg = baseline
            for ntrp_key, sfx in [("3.0", "30"), ("3.5", "35")]:
                div_matches = [m for m in matches if m.division == ntrp_key]
                n_weeks = weeks_by_div.get(ntrp_key, 4)
                if div_matches:
                    r = _compute_v8_rating(baseline, div_matches,
                                           n_total_weeks=n_weeks, division=ntrp_key)
                    if ntrp_key == "3.0":
                        r30 = r
                    else:
                        r35 = r
            n_weeks_global = max(weeks_by_div.values()) if weeks_by_div else 4
            if matches:
                rg = _compute_v8_rating(baseline, matches,
                                        n_total_weeks=n_weeks_global, division=primary_ntrp)
            result[k] = (r30, r35, rg)
        return result

    def _store(ratings: dict[str, tuple[float, float, float]]) -> None:
        """Write iter_rating_* fields into players_by_name dicts."""
        for k, (r30, r35, rg) in ratings.items():
            p = players_by_name.get(k)
            if p:
                p["iter_rating_30"] = r30
                p["iter_rating_35"] = r35
                p["iter_global_rating"] = rg

    # Iteration 0: use baseline as opponent ratings (same as single-pass)
    prev = _compute_all(None)
    _store(prev)
    snapshots.append(prev)

    converged_at = 0
    for iteration in range(1, max_iterations + 1):
        opp_fields = {"3.0": "iter_rating_30", "3.5": "iter_rating_35"}
        raw = _compute_all(opp_fields)

        # Damped update: blend computed value toward previous to prevent oscillation.
        # Pure substitution (damping=1.0) can produce 2-cycles when A's rating depends
        # on B's, which depends on A's. Damping < 1.0 breaks the cycle.
        curr = {
            k: (
                round(damping * raw[k][0] + (1 - damping) * prev[k][0], 4),
                round(damping * raw[k][1] + (1 - damping) * prev[k][1], 4),
                round(damping * raw[k][2] + (1 - damping) * prev[k][2], 4),
            )
            for k in raw if k in prev
        }

        # Convergence check
        max_delta = max(
            max(abs(curr[k][i] - prev[k][i]) for i in range(3))
            for k in curr
        )
        _store(curr)
        snapshots.append(curr)
        prev = curr

        if max_delta < epsilon:
            converged_at = iteration
            break
    else:
        converged_at = max_iterations

    # Persist iter_rating_* fields to disk (diagnostic only — primary ratings
    # are set by _compute_division_sequential in run_ratings())
    _save(PLAYERS_JSON, players)

    if verbose:
        # Print comparison table for players with ≥1 match
        all_records_check = _collect_match_records(standings_files, players_by_name)
        active = {k for k, ms in all_records_check.items() if ms}

        print(f"\n  [iterative] converged after {converged_at} iteration(s)  "
              f"(epsilon={epsilon})\n")

        header = "%-25s %8s %8s" % ("Player", "Baseline", "Single")
        for i in range(len(snapshots)):
            header += " %7s" % f"Iter-{i}"
        header += "  %7s" % "ΔFinal"
        print(header)
        print("-" * len(header))

        # Sort by abs(final delta) descending
        rows = []
        for p in players:
            k = _name_key(p.get("name", ""))
            if k not in active:
                continue
            baseline = p.get("dynamic_rating_baseline")
            if baseline is None:
                continue
            single = p.get("current_division_rating", baseline)
            iters = [snapshots[i][k][0] if "3.0" in p.get("division","") else snapshots[i][k][1]
                     for i in range(len(snapshots)) if k in snapshots[i]]
            final = iters[-1] if iters else single
            rows.append((abs(final - baseline), p.get("name",""), baseline, single, iters, final))

        for _, name, baseline, single, iters, final in sorted(rows, reverse=True)[:40]:
            line = "%-25s %8.4f %8.4f" % (name, baseline, single)
            for r in iters:
                line += " %7.4f" % r
            line += "  %+7.4f" % (final - baseline)
            print(line)


# ---------------------------------------------------------------------------
# Standalone entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_ratings()
    run_ratings_iterative()
