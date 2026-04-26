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
STANDINGS_30 = DATA_DIR / "standings_women_30.json"
STANDINGS_35 = DATA_DIR / "standings_women_35.json"


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
# Division rating floor (approx lowest dynamic baseline in each division)
_DIV_FLOOR: dict[str, float] = {"3.0": 2.50, "3.5": 3.00}

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


def _match_adjustment(player_rating: float, record: MatchRecord,
                      scaling: float = SCALING, cap: float = CAP) -> float:
    """
    Compute the rating adjustment for a single match.
    Surprise weighting amplifies results that defy rating expectations (symmetrically
    for wins and losses) since upsets in either direction usually indicate ratings
    are off, not luck. Score dominance captures margin of victory within the match.
    """
    expected = _cross_pair_expected(
        player_rating, record.partner_rating, record.opponent_ratings
    )
    sw = _surprise_weight(expected, record.won)

    sets = _parse_sets(record.score)
    if not sets:
        surprise = (1.0 if record.won else 0.0) - expected
        return max(-cap, min(cap, surprise * sw * scaling))

    # --- Pre-compute per-set dominance and player-won-set flags ---
    player_won_set_arr: list[bool] = []
    dom_arr: list[float] = []
    for winner_games, loser_games, first_side_won in sets:
        player_won_set_arr.append(first_side_won == record.won)
        dom_arr.append(_set_dominance(winner_games, loser_games))

    # --- 3-set tiebreak Scenario 2 attenuation ---
    # Scenario 2: Player LOST the match, won set 1 more dominantly than the
    # opponent won set 2. Going to a tiebreak reveals the dominant set 1 win
    # overstated the player's advantage — attenuate set 1's weight so it
    # contributes no more than set 2 did (scale factor = dom_set2 / dom_set1).
    # Example: Kim Knotts wins set 1 6-1 (dom=0.75) but loses set 2 6-4
    # (dom=0.25) and tiebreak → set 1 weight becomes 0.25/0.75 ≈ 0.33,
    # so effective dom_set1 = 0.75 × 0.33 = 0.25 — same scale as set 2.
    set_dom_weights = [1.0] * len(sets)
    if len(sets) == 3 and not record.won:
        pws0, pws1, pws2 = player_won_set_arr[0], player_won_set_arr[1], player_won_set_arr[2]
        dom0, dom1 = dom_arr[0], dom_arr[1]
        if pws0 and not pws1 and not pws2 and dom0 > dom1 and dom0 > 0:
            set_dom_weights[0] = dom1 / dom0

    # Set-by-set signal weighted by score dominance — no SW here.
    # Score margins are direct performance evidence: Yarisbel winning 6-1 6-3 is
    # a real signal regardless of whether the win was expected. SW would zero out
    # dominant wins against weaker opponents, masking genuine performance quality.
    total_surprise = 0.0
    total_dominance = 0.0
    for i, (winner_games, loser_games, first_side_won) in enumerate(sets):
        player_won_set = player_won_set_arr[i]
        actual_set = 1.0 if player_won_set else 0.0
        base_surprise = actual_set - expected
        dom = dom_arr[i] * set_dom_weights[i]
        total_surprise += base_surprise * dom       # ← no SW on score signal
        total_dominance += dom

    # Match-outcome signal: SW applies here. The outcome (win/loss) is where luck
    # lives — an upset outcome is a sign ratings are off, an expected outcome is
    # weak evidence. Score margins within a match are less luck-dependent.
    # Singles matches carry a 1.25× bonus: a 1v1 result is a cleaner signal than
    # doubles (no partner contribution to mask individual ability).
    match_outcome_weight = 0.15 if len(sets) >= 3 else 0.30
    if record.partner_rating is None:   # singles — boost outcome weight
        match_outcome_weight *= 1.25
    match_surprise = (1.0 if record.won else 0.0) - expected
    total_surprise += match_surprise * match_outcome_weight * sw
    total_dominance += match_outcome_weight

    if total_dominance > 0:
        adj = (total_surprise / total_dominance) * scaling * total_dominance
    else:
        adj = 0.0

    return max(-cap, min(cap, adj))


# ---------------------------------------------------------------------------
# Score-gap mapping: how much rating gap does this score dominance imply?
# ---------------------------------------------------------------------------

_SCORE_GAP = {
    0: 0.10,   # 6-0 → dominant (bagel vs 2.95 implies ~3.05, not 3.17)
    1: 0.07,   # 6-1 → strong
    2: 0.05,   # 6-2 → solid
    3: 0.03,   # 6-3 → moderate
    4: 0.01,   # 6-4 → slight
    5: 0.00,   # 7-5 → essentially even
    6: 0.00,   # 7-6 → tiebreak, even
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
    FLOOR_BLEND = 0.50

    # Floor: strong win proves a lower bound — blend up toward implied_floor.
    if implied_floor is not None and surprise_rating < implied_floor:
        gap = implied_floor - surprise_rating
        surprise_rating += gap * FLOOR_BLEND

    # Win ceiling: hard cap at what wins prove you're capable of.
    if effective_win_ceil is not None and surprise_rating > effective_win_ceil:
        surprise_rating = effective_win_ceil

    # --- Per-loss soft pulls ---
    # Each loss independently blends the rating toward that loss's implied value.
    # More unexpected the loss (bigger upset), the harder the pull.
    #
    # Unlike the old hard ceiling, no single loss completely overrides win evidence.
    # Unlike the old threshold filter, upset losses aren't ignored — they pull harder
    # than near-peer losses, because losing to a much weaker team is more damning
    # than a close loss to a near-peer (variance on a bad day).
    #
    # Pull strength: base 30% blend, up to 65% for extreme upsets (opponent 0.40+
    # below baseline). Losses processed worst-first (lowest implied) so the most
    # damning loss has first impact; later losses may not apply if rating already
    # pulled below their implied.
    LOSS_BLEND_BASE  = 0.30   # pull toward loss_implied for any loss
    LOSS_BLEND_UPSET = 0.35   # additional blend at max upset severity (0.40+ below baseline)

    for loss_val in sorted(loss_implied):          # ascending = worst upset first
        if surprise_rating > loss_val:
            gap = surprise_rating - loss_val
            # upset_severity: 0 for near-peer (loss_val ≈ baseline), 1 for big upset
            upset_severity = min(1.0, max(0.0, (baseline - loss_val) / 0.40))
            blend = LOSS_BLEND_BASE + LOSS_BLEND_UPSET * upset_severity
            surprise_rating -= gap * blend

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
        data = _load(path, {})
        ntrp = data.get("ntrp", f"{div_suffix[0]}.{div_suffix[1]}")   # "3.0" or "3.5"
        default_opp = DEFAULT_OPP_RATING_30 if "30" in div_suffix else DEFAULT_OPP_RATING_35

        # Determine which field to use for opponent ratings in this division
        live_field = (opponent_rating_fields or {}).get(ntrp)   # None → use baseline

        def _rating_or_default(name: str, _live=live_field, _def=default_opp) -> float:
            p = players_by_name.get(_name_key(name))
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

                # Detect scorecard swap once per match (new-format lines only)
                _swap = _detect_scorecard_swap(match, team_lookup)

                for ln in match.get("lines", []):
                    # Defaults/walkovers: one side has no players listed.
                    # These are a total no-op for ratings — skip immediately.
                    # Represented as: empty string ("") or literal "N/A" / "N/A / N/A".
                    def _is_default_side(s: str) -> bool:
                        s = (s or "").strip().upper()
                        return not s or s in ("N/A", "N/A / N/A", "DEFAULT", "NOT AVAILABLE")
                    _ph = ln.get("players_home") or ln.get("winners") or ""
                    _pa = ln.get("players_away") or ln.get("losers") or ""
                    if _is_default_side(_ph) or _is_default_side(_pa):
                        continue

                    # Support both old format (winners/losers) and new scraper
                    # format (players_home / players_away / result: "home"|"away")
                    w_raw = ln.get("winners", "")
                    l_raw = ln.get("losers", "")
                    if not w_raw or not l_raw:
                        home_raw   = ln.get("players_home", "")
                        away_raw   = ln.get("players_away", "")
                        result_raw = ln.get("result", "").strip().lower()
                        if not home_raw or not away_raw or result_raw not in ("home", "away"):
                            continue
                        # If columns are swapped, flip home↔away before applying result
                        if _swap:
                            home_raw, away_raw = away_raw, home_raw
                        if result_raw == "home":
                            w_raw, l_raw = home_raw, away_raw
                        else:
                            w_raw, l_raw = away_raw, home_raw
                    # Skip walkovers
                    if w_raw.strip().upper() == "N/A" or l_raw.strip().upper() == "N/A":
                        continue
                    if not w_raw.strip() or not l_raw.strip():
                        continue

                    winner_names = _parse_player_names(w_raw)
                    loser_names = _parse_player_names(l_raw)
                    if not winner_names or not loser_names:
                        continue

                    line_label = ln.get("line", "")
                    score = ln.get("score", "")

                    # Create records for winners
                    for pname in winner_names:
                        pk = _name_key(pname)
                        if pk not in players_by_name:
                            continue
                        partners = [n for n in winner_names if _name_key(n) != pk]
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
                        pk = _name_key(pname)
                        if pk not in players_by_name:
                            continue
                        partners = [n for n in loser_names if _name_key(n) != pk]
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
        data = _load(path, {})
        ntrp = data.get("ntrp", f"{div_suffix[0]}.{div_suffix[1]}")

        for sf in data.get("subflights", []):
            for match in sf.get("matches", []):
                if match.get("pending"):
                    continue
                match_id = match.get("match_id", "")
                date = match.get("date", "")
                _swap = _detect_scorecard_swap(match, team_lookup)

                for ln in match.get("lines", []):
                    line_label = ln.get("line", "")
                    dedup_key = (match_id, line_label)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    def _is_default_side(s: str) -> bool:
                        s = (s or "").strip().upper()
                        return not s or s in ("N/A", "N/A / N/A", "DEFAULT", "NOT AVAILABLE")
                    if _is_default_side(ln.get("players_home", "")) or \
                       _is_default_side(ln.get("players_away", "")):
                        continue

                    w_raw = ln.get("winners", "")
                    l_raw = ln.get("losers", "")
                    if not w_raw or not l_raw:
                        home_raw = ln.get("players_home", "")
                        away_raw = ln.get("players_away", "")
                        result_raw = ln.get("result", "").strip().lower()
                        if not home_raw or not away_raw or result_raw not in ("home", "away"):
                            continue
                        if _swap:
                            home_raw, away_raw = away_raw, home_raw
                        w_raw, l_raw = (home_raw, away_raw) if result_raw == "home" else (away_raw, home_raw)

                    winner_keys = [
                        _name_key(n) for n in _parse_player_names(w_raw)
                        if _name_key(n) in players_by_name
                    ]
                    loser_keys = [
                        _name_key(n) for n in _parse_player_names(l_raw)
                        if _name_key(n) in players_by_name
                    ]
                    if not winner_keys and not loser_keys:
                        continue

                    events.append(CourtEvent(
                        date=date, match_id=match_id, line_label=line_label,
                        division=ntrp, winner_keys=winner_keys,
                        loser_keys=loser_keys, score=ln.get("score", ""),
                    ))

    return events


def _compute_global_sequential(
    court_events: list[CourtEvent],
    baselines: dict[str, float],
) -> dict[str, float]:
    """
    Compute global ratings by processing all court lines in chronological order,
    treating all divisions as one pool. Opponent strength at each step uses
    the running global rating (not the frozen baseline), so earlier results
    inform later ones.
    """
    def _date_sort_key(ev: CourtEvent) -> tuple:
        try:
            m, d, y = ev.date.split("/")
            return (int(y), int(m), int(d))
        except Exception:
            return (0, 0, 0)

    global_r: dict[str, float] = dict(baselines)

    for ev in sorted(court_events, key=_date_sort_key):
        all_keys = ev.winner_keys + ev.loser_keys
        # Snapshot current ratings for everyone in this court BEFORE any update
        # so both sides see consistent pre-match ratings.
        cur = {k: global_r.get(k, baselines.get(k, 3.0)) for k in all_keys}

        updates: dict[str, float] = {}

        for pk in ev.winner_keys:
            if pk not in baselines:
                continue
            partners = [k for k in ev.winner_keys if k != pk]
            partner_r = cur.get(partners[0]) if partners else None
            opp_ratings = [cur.get(k, 3.0) for k in ev.loser_keys] or [3.0]
            rec = MatchRecord(
                opponent_ratings=opp_ratings, partner_rating=partner_r,
                won=True, date=ev.date, division=ev.division,
                match_id=ev.match_id, line_label=ev.line_label, score=ev.score,
            )
            updates[pk] = cur[pk] + _match_adjustment(cur[pk], rec)

        for pk in ev.loser_keys:
            if pk not in baselines:
                continue
            partners = [k for k in ev.loser_keys if k != pk]
            partner_r = cur.get(partners[0]) if partners else None
            opp_ratings = [cur.get(k, 3.0) for k in ev.winner_keys] or [3.0]
            rec = MatchRecord(
                opponent_ratings=opp_ratings, partner_rating=partner_r,
                won=False, date=ev.date, division=ev.division,
                match_id=ev.match_id, line_label=ev.line_label, score=ev.score,
            )
            updates[pk] = cur[pk] + _match_adjustment(cur[pk], rec)

        global_r.update(updates)

    return global_r


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

    # Build name → player lookup
    players_by_name: dict[str, dict] = {}
    for p in players:
        k = _name_key(p.get("name", ""))
        if k:
            players_by_name[k] = p

    # Collect per-player match records from both divisions
    standings_files = [
        (STANDINGS_30, "30"),
        (STANDINGS_35, "35"),
    ]
    all_records = _collect_match_records(standings_files, players_by_name)

    # Collect unique court events for sequential global rating
    court_events = _collect_court_events(standings_files, players_by_name)

    # Pre-compute sequential global ratings (used below, replaces per-player batch call)
    baselines_all = {
        k: p["dynamic_rating_baseline"]
        for k, p in players_by_name.items()
        if p.get("dynamic_rating_baseline") is not None
    }
    global_sequential = _compute_global_sequential(court_events, baselines_all)

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

    for player in players:
        k = _name_key(player.get("name", ""))
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

        # Per-division ratings: SILOED, each based only on that division's matches
        for ntrp_key, sfx in [("3.0", "30"), ("3.5", "35")]:
            div_matches = [m for m in matches if m.division == ntrp_key]
            n_weeks = weeks_by_div.get(ntrp_key, 4)
            if div_matches:
                player[f"rating_{sfx}"] = _compute_v8_rating(
                    baseline, div_matches,
                    n_total_weeks=n_weeks, division=ntrp_key,
                )
            else:
                player[f"rating_{sfx}"] = baseline

        # current_division_rating = the primary division's siloed rating
        player["current_division_rating"] = player.get(f"rating_{primary_ntrp.replace('.','')}", baseline)

        # Global rating: sequential cross-division signal — only meaningful for players
        # who have matches in BOTH divisions. For single-division players, global = their
        # siloed rating (no cross-division data to add), so no diff will be displayed.
        has_30 = any(m.division == "3.0" for m in matches)
        has_35 = any(m.division == "3.5" for m in matches)
        if has_30 and has_35:
            player["global_rating"] = global_sequential.get(k, baseline)
        else:
            player["global_rating"] = player["current_division_rating"]

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

    players_by_name: dict[str, dict] = {}
    for p in players:
        k = _name_key(p.get("name", ""))
        if k:
            players_by_name[k] = p

    standings_files = [(STANDINGS_30, "30"), (STANDINGS_35, "35")]

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

    # Promote converged iterative ratings to primary fields.
    # The single-pass run_ratings() used frozen pre-season baselines for opponent
    # strength, which badly misrepresents mid-season matchups (e.g. a player with
    # baseline 2.18 who has already proved she's 2.74 by week 4 is treated as 2.18
    # in every single-pass match calculation — making Kim Knotts' even loss look
    # like a major upset). The iterative ratings converge to self-consistent values
    # where everyone's opponent strength reflects computed (not frozen) ratings.
    # These are the numbers we want displayed and used for all downstream logic.
    all_records_final = _collect_match_records(standings_files, players_by_name)
    active_keys = {k for k, ms in all_records_final.items() if ms}

    for player in players:
        k = _name_key(player.get("name", ""))
        if k not in active_keys:
            continue
        div = player.get("division", "")
        primary_ntrp = "3.5" if "3.5" in div else "3.0"

        r30 = player.get("iter_rating_30")
        r35 = player.get("iter_rating_35")
        if r30 is not None:
            player["rating_30"] = r30
        if r35 is not None:
            player["rating_35"] = r35
        # current_division_rating → primary division's iterative result
        if primary_ntrp == "3.0" and r30 is not None:
            player["current_division_rating"] = r30
        elif primary_ntrp == "3.5" and r35 is not None:
            player["current_division_rating"] = r35
        # global_rating → iterative global (cross-division players already have this)
        rg = player.get("iter_global_rating")
        if rg is not None:
            player["global_rating"] = rg

    # Persist everything (iter_rating_* fields + promoted primary fields)
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
