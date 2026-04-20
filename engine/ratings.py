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
    Asymmetric weighting based on how surprising the result is.

    - Upset win (won=True, expected low): HIGH weight — meaningful signal.
    - Expected win (won=True, expected high): LOW weight — noisy / uninformative.
    - Upset loss (won=False, expected high): HIGH weight — meaningful signal.
    - Expected loss (won=False, expected low): LOW weight — noisy / uninformative.

    Returns a multiplier in [0.15, 1.0].
    """
    if won:
        # For a win, surprise = how unlikely the win was (1 - expected)
        surprise_level = 1.0 - expected
    else:
        # For a loss, surprise = how unlikely the loss was (expected)
        surprise_level = expected

    # Map surprise_level to weight: low surprise → low weight
    if surprise_level >= 0.70:
        return 1.00    # huge upset
    if surprise_level >= 0.55:
        return 0.75    # moderate upset
    if surprise_level >= 0.40:
        return 0.40    # slight upset or even match — modest signal
    if surprise_level >= 0.25:
        return 0.20    # expected result — weak signal
    return 0.10        # heavily expected result (barely a signal)


def _match_adjustment(player_rating: float, record: MatchRecord,
                      scaling: float = SCALING, cap: float = CAP) -> float:
    """
    Compute the v8 rating adjustment for a single match.
    Uses cross-pair expected probability, surprise weighting, and set-by-set scoring.
    """
    expected = _cross_pair_expected(
        player_rating, record.partner_rating, record.opponent_ratings
    )
    sw = _surprise_weight(expected, record.won)

    sets = _parse_sets(record.score)
    if not sets:
        # No parseable score → use simple win/loss surprise
        surprise = (1.0 if record.won else 0.0) - expected
        return max(-cap, min(cap, surprise * sw * scaling))

    # Set-by-set surprise accumulation
    total_surprise = 0.0
    total_dominance = 0.0
    for winner_games, loser_games, first_side_won in sets:
        # Did the player win this set?
        player_won_set = (first_side_won == record.won)
        actual_set = 1.0 if player_won_set else 0.0
        base_surprise = actual_set - expected
        dom = _set_dominance(winner_games, loser_games)
        total_surprise += base_surprise * dom * sw
        total_dominance += dom

    # Add match-outcome signal: the overall W/L result carries its own weight
    # beyond individual set scores. Straight-sets (2-set) wins are more decisive
    # and carry a stronger signal. 3-set matches were closely contested — the
    # outcome matters but the signal is muddier.
    match_outcome_weight = 0.15 if len(sets) >= 3 else 0.30
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
    0: 0.22,   # 6-0 → dominant (reduced: one bagel ≠ 0.40 rating gap)
    1: 0.15,   # 6-1 → strong
    2: 0.10,   # 6-2 → solid
    3: 0.07,   # 6-3 → moderate
    4: 0.03,   # 6-4 → slight
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
    # For doubles: we use opponent strength directly (not pair back-calculation)
    # because backing out individual rating from pair is too sensitive to partner quality.
    # Beating opponents rated X means YOU are approximately X + gap.
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

    # --- Step 1-3: match adjustments with diminishing returns ---
    adjustments = [_match_adjustment(baseline, m, scaling, cap) for m in matches]
    sorted_adj = sorted(adjustments, key=lambda x: abs(x), reverse=True)
    match_total = 0.0
    for i, adj in enumerate(sorted_adj):
        w = DIM_WEIGHTS[i] if i < len(DIM_WEIGHTS) else DIM_WEIGHTS[-1]
        match_total += adj * w

    # --- Step 4: asymmetric confidence scaling ---
    deploy_rate = n_matches / n_total_weeks if n_total_weeks > 0 else 0
    if match_total > 0:
        # Positive moves require evidence: need matches AND deployment
        confidence = (min(1.0, n_matches / MIN_MATCHES_FULL_CONFIDENCE)
                      * min(1.0, deploy_rate * 2))
    else:
        # Negative moves: losses always count at full weight
        confidence = 1.0
    match_total *= confidence

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

    # Ceiling from wins: you can't be rated much above what your wins prove.
    # If you only beat 2.85-average opponents, even dominantly, your max is ~3.05-3.10.
    # This prevents inflation from beating weak fields.
    implied_win_ceiling = max(win_implied) if win_implied else None

    # Ceiling from losses: losing to someone rated X means you're probably not above X.
    implied_loss_ceiling = min(loss_implied) if loss_implied else None

    # Use the tighter of the two ceilings
    implied_ceiling = None
    if implied_win_ceiling is not None and implied_loss_ceiling is not None:
        implied_ceiling = min(implied_win_ceiling, implied_loss_ceiling)
    elif implied_win_ceiling is not None:
        implied_ceiling = implied_win_ceiling
    elif implied_loss_ceiling is not None:
        implied_ceiling = implied_loss_ceiling

    # --- Implied-rating constraints ---
    # Floor: if you beat someone strong, you must be at least near their level.
    # Ceiling: you can't be rated above what your opponents prove. Hard cap.
    #   If every opponent was below your baseline, you have zero evidence
    #   of being above baseline — the ceiling IS your baseline.
    FLOOR_BLEND = 0.50

    if implied_ceiling is not None:
        implied_ceiling = max(implied_ceiling, baseline)

    if implied_floor is not None and surprise_rating < implied_floor:
        gap = implied_floor - surprise_rating
        surprise_rating += gap * FLOOR_BLEND

    # Hard ceiling: you cannot exceed your implied ceiling from wins.
    # No blending — if you only beat weak opponents, your rating is capped.
    if implied_ceiling is not None and surprise_rating > implied_ceiling:
        surprise_rating = implied_ceiling

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
) -> dict[str, list[MatchRecord]]:
    """
    Walk standings JSONs and extract per-player MatchRecords.

    standings_files: list of (path, division_suffix) tuples,
        e.g. [(STANDINGS_30, "30"), (STANDINGS_35, "35")]
    players_by_name: {name_key: player_dict} lookup
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

        def _rating_or_default(name: str) -> float:
            p = players_by_name.get(_name_key(name))
            if p:
                r = p.get("dynamic_rating_baseline")
                if r is not None:
                    return r
            return default_opp

        for sf in data.get("subflights", []):
            for match in sf.get("matches", []):
                if match.get("pending"):
                    continue
                match_id = match.get("match_id", "")
                date = match.get("date", "")

                for ln in match.get("lines", []):
                    # Use normalized winner/loser fields
                    w_raw = ln.get("winners", "")
                    l_raw = ln.get("losers", "")
                    if not w_raw or not l_raw:
                        continue
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

        # Global rating: ALL matches across all divisions
        n_weeks_global = max(weeks_by_div.values()) if weeks_by_div else 4
        player["global_rating"] = _compute_v8_rating(
            baseline, matches,
            n_total_weeks=n_weeks_global, division=primary_ntrp,
        )

        summary.players_updated += 1

    _save(PLAYERS_JSON, players)
    print(f"  [ratings] updated {summary.players_updated} players "
          f"({summary.players_skipped} skipped – no match data)")
    return summary


# ---------------------------------------------------------------------------
# Standalone entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_ratings()
