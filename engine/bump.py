"""
engine/bump.py
Bump indicator: an estimate of where USTA's own year-end NTRP calculation
will land for each player, and whether that moves them up or down a level.

This is deliberately NOT ShiQ. ShiQ (engine/ratings_v9.py) tries to measure
true strength. The Bump indicator copies the publicly reverse-engineered
shape of USTA's math (r/10s analysis, updated Nov 2026), even where we think
that math is a worse measure of strength.

Year-end (fit_year_end):
* Each match is scored on share of games won against the strength of the
  other side; past a margin cap, extra games add nothing, so a blowout of a
  much weaker player can even pull you down.
* Every match of the rating year counts; recent ones only a little more
  (last 5 = 26% of the year-end, 6-10 = 23%, 11-15 = 15%, 16-20 = 17%,
  older = 19%). Playoff (Championships) matches count about 2.5x.
* Opponents and partners are judged where they FINISH the year, so all
  players are solved together.
* Both doubles partners get the same score-driven credit.
* What a computer-rated player carried in still matters; a self-rate or
  appeal level fades within a few matches.
* Fewer than 3 matches: the rating carries forward.
* Call: above the level's top (3.00 for a 3.0) = up, at or below its floor
  (2.50) = down.

Strikes (run_bump): self-rated / appealed players are DQ'd mid-year after
three matches that leave their RUNNING rating (this match averaged with the
previous three) at level + 0.30 or more. Strikes do not feed the year-end.

Starting point is the Dec 2025 TennisRecord estimate of each player's USTA
dynamic rating, kept inside the player's current level (start_rating), so
every call inherits that estimate's error. Calls are checked against the
real year-end ratings after they are published (Dec 2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from engine import ratings as R
from engine.ratings_v9 import games_won

K = 1.2                  # same game-share curve as ShiQ v9
SHARE_CEILING = 0.92     # game shares beyond this count as this (margin cap)
MAX_GAP = 0.90           # a blowout "plays like" at most this far above the opponents (tuned to base rates, 2026-11-25)
WINDOW = 4               # this match + previous 3 running ratings
MIN_MATCHES = 3
STRIKE_MARGIN = 0.30
LEVEL_WIDTH = 0.50

# Category thresholds on (year-end estimate - level top) / floor distance.
LOWEST_LEVEL = 2.5        # adult leagues bottom out here
CLEAR_MARGIN = 0.05      # this far past the line = "likely"
LEAN_DOWN_ZONE = 0.10    # this close above the down line = "Leaning down". Down calls are
                         # strict (84% right) but catch under half of real downs, and most
                         # misses sat within 0.10 of the line (Shirin, 2026-11-26).
BUMPED_UP_DOWN_ZONE = 0.10  # bumped up last year and this close above the down line = predicted down.
                            # Near the line they went down 12 of 25 times vs 20 of 143 for players
                            # at the same level two years running (2025 backtest: downs caught
                            # 34 -> 42 of 72, Shirin 2026-11-26).
DOWN_LINE_SHIFT = {4.0: 0.03}  # 4.0 pools sit low; downs there were caught only 4 of 16.
PLAY_UP_ZONE = 0.05      # this close under the up line, and ...
PLAY_UP_SHARE = 0.90     # ... this share of matches on higher-level flights = "Leaning up".
                         # Near the line, players who mostly play up went up far more often
                         # (2025 backtest: ups caught 68 -> 70 of 95, calls still 82% right).


@dataclass
class BumpState:
    history: list[float] = field(default_factory=list)   # running ratings, newest last
    matches: int = 0
    strikes: int = 0
    peak: float = 0.0


def parse_level(ntrp_rating: Optional[str]) -> tuple[Optional[float], str]:
    """'3.0 C' -> (3.0, 'C'). Unknown -> (None, '')."""
    if not ntrp_rating:
        return None, ""
    parts = ntrp_rating.split()
    try:
        level = float(parts[0])
    except (ValueError, IndexError):
        return None, ""
    return level, (parts[1] if len(parts) > 1 else "")


def start_rating(baseline: Optional[float], level: Optional[float],
                 letter: str = "") -> Optional[float]:
    """Starting point for the year, kept inside the player's current level.

    A player's current NTRP level was set by USTA's own year-end number, so
    that number must have been inside the level (2.51-3.00 for a 3.0). When
    the TennisRecord estimate falls outside it (common for appealed and
    self-rated players, who moved levels), pull it to the nearest edge.

    Self-rated and appealed players (S, A) have no USTA year-end number at
    this level yet, and TennisRecord's estimate for them is usually their old
    level or a default. They start no lower than the middle of the level.
    """
    if baseline is None or level is None:
        return baseline
    lo = level - LEVEL_WIDTH / 2 if letter in ("S", "A") else level - LEVEL_WIDTH + 0.01
    return min(max(baseline, lo), level)


# Where each player starts the rating year. "mid" = the middle of their
# current level (level - 0.25). On the 2025 -> 2026 backtest (383 core NV
# players) mid-level starts scored 93% against 88% for TennisRecord's
# December numbers, which start too many players near a level edge.
START_MODE = "mid"


def mid_level_start(level: Optional[float], fallback: Optional[float] = None) -> Optional[float]:
    """Middle of the player's level; ``fallback`` when the level is unknown."""
    return fallback if level is None else level - LEVEL_WIDTH / 2


def implied_gap(share: float) -> float:
    """Strength gap that would make this game share the expected one."""
    s = min(max(share, 1.0 - SHARE_CEILING), SHARE_CEILING)
    g = math.atanh(2.0 * s - 1.0) / K
    return max(-MAX_GAP, min(MAX_GAP, g))


def run_bump(events: list[R.CourtEvent], baselines: dict[str, float],
             levels: dict[str, tuple[Optional[float], str]]) -> dict[str, BumpState]:
    st: dict[str, BumpState] = {}

    def state(k: str) -> BumpState:
        if k not in st:
            b = baselines.get(k) or 3.0
            st[k] = BumpState(history=[b] * (WINDOW - 1), peak=b)
        return st[k]

    def current(k: str) -> float:
        return state(k).history[-1]

    for e in events:
        if not e.winner_keys or len(e.winner_keys) != len(e.loser_keys):
            continue
        w, l = games_won(e.score)
        if w + l == 0:
            continue
        sw = sum(current(k) for k in e.winner_keys) / len(e.winner_keys)
        sl = sum(current(k) for k in e.loser_keys) / len(e.loser_keys)
        # How far the result says the winning side out-played the rating gap.
        surplus = implied_gap(w / (w + l)) - (sw - sl)
        for k, sign in [(k, 1.0) for k in e.winner_keys] + [(k, -1.0) for k in e.loser_keys]:
            s = state(k)
            perf = s.history[-1] + sign * surplus          # equal credit for partners
            new = (perf + sum(s.history[-(WINDOW - 1):])) / WINDOW
            s.history.append(new)
            s.matches += 1
            s.peak = max(s.peak, new)
            level, letter = levels.get(k, (None, ""))
            if level is not None and letter in ("S", "A") and new > level + STRIKE_MARGIN:
                s.strikes += 1
    return st


# ---------------------------------------------------------------------------
# Year-end model (v2, after the Nov 2026 update to the published analysis)
# ---------------------------------------------------------------------------
# The year-end is NOT where the running average lands. It is a re-scoring of
# the whole rating year:
#   * every match counts; recent ones only a little more. Share of the
#     year-end by match recency (newest first):
#       last 5: 26%, 6-10: 23%, 11-15: 15%, 16-20: 17%, older: 19%
#   * playoff (Championships) matches carry ~2.5x a regular match
#   * each match is judged against where opponents and partners FINISH the
#     year, not where they stood that day (solved jointly, below)
#   * what a player carried in from last year still matters for computer-
#     rated players; a self-rate / appeal level fades within 3-4 matches.
RECENCY_SHARE = [(5, 0.26), (5, 0.23), (5, 0.15), (5, 0.17)]
OLDER_SHARE = 0.19
PLAYOFF_WEIGHT = 2.5
PRIOR_WEIGHT_COMPUTER = 1.0   # carry-in, in "matches" (tuned to base rates, 2026-11-25)
PRIOR_WEIGHT_SELF = 0.5       # S (new, self-rated): their results take over sooner. In the
                              # 2025 backtest new players near the line went up 1 in 3 times,
                              # and halving this caught 62 -> 68 of 95 ups (calls 78% -> 82% right).
                              # A (appeal) players are returning, so they use the computer weight.
FIT_ITERATIONS = 60
FIT_DAMPING = 0.5


def recency_weights(n: int) -> list[float]:
    """Per-match weights, oldest first, for a player with n counted matches.

    Scaled so a match in the newest block weighs about 1.
    """
    w_rev: list[float] = []
    for size, share in RECENCY_SHARE:
        w_rev += [share / size * 5 / 1.3] * size
    if n > len(w_rev):
        older = n - len(w_rev)
        w_rev += [OLDER_SHARE * 5 / 1.3 / older] * older
    return list(reversed(w_rev[:n]))


def playoff_match_ids() -> set[str]:
    """match_ids from Championships flights in the standings files."""
    ids: set[str] = set()
    for path, _ in R._discover_standings_files():
        data = R._load(path, {})
        for sf in data.get("subflights", []):
            if "championship" in (sf.get("flight_label") or "").lower():
                ids.update(m.get("match_id", "") for m in sf.get("matches", []))
    return ids


def fit_year_end(events: list[R.CourtEvent], start: dict[str, float],
                 letters: dict[str, str], playoffs: set[str] = frozenset(),
                 known_through: Optional[dict[str, str]] = None
                 ) -> tuple[dict[str, float], dict[str, int]]:
    """Jointly solve every player's year-end number.

    Each player's year-end Y balances the pull of their prior (start rating,
    weight PRIOR_WEIGHT_*) against the weighted surplus of each match:
        surplus = implied_gap(game share) - (Y_own_side - Y_other_side)
    using everyone's year-end Y, so opponents are judged where they finish.
    Partners get the same surplus (equal credit).

    ``known_through`` maps a player to the date of their start rating. Events
    flagged ``prior_crawl`` (late matches from last year's crawl) on or before
    that date are already in the start, so they don't move or count for that
    player; the player still counts as a partner/opponent for everyone else.
    """
    known_through = known_through or {}
    k_date = R._date_str_sort_key

    def already_known(k: str, e) -> bool:
        d = known_through.get(k)
        return bool(d) and getattr(e, "prior_crawl", False) and k_date(e.date) <= k_date(d)

    scored = []
    per_player: dict[str, list[int]] = {}
    for e in events:
        if not e.winner_keys or len(e.winner_keys) != len(e.loser_keys):
            continue
        w, l = games_won(e.score)
        if w + l == 0:
            continue
        idx = len(scored)
        scored.append((e, implied_gap(w / (w + l)), e.match_id in playoffs))
        for k in e.winner_keys + e.loser_keys:
            if not already_known(k, e):
                per_player.setdefault(k, []).append(idx)

    weight: dict[tuple[str, int], float] = {}
    for k, idxs in per_player.items():
        for i, rw in zip(idxs, recency_weights(len(idxs))):
            weight[(k, i)] = rw * (PLAYOFF_WEIGHT if scored[i][2] else 1.0)

    Y = {k: (start.get(k) or 3.0) for e, _, _ in scored
         for k in e.winner_keys + e.loser_keys}
    prior_w = {k: (PRIOR_WEIGHT_SELF if letters.get(k) == "S"
                   else PRIOR_WEIGHT_COMPUTER) for k in per_player}
    for _ in range(FIT_ITERATIONS):
        push = {k: 0.0 for k in Y}
        for i, (e, gap, _) in enumerate(scored):
            sw = sum(Y[k] for k in e.winner_keys) / len(e.winner_keys)
            sl = sum(Y[k] for k in e.loser_keys) / len(e.loser_keys)
            surplus = gap - (sw - sl)
            for k in e.winner_keys:
                push[k] += weight.get((k, i), 0.0) * surplus
            for k in e.loser_keys:
                push[k] -= weight.get((k, i), 0.0) * surplus
        for k in per_player:
            total_w = prior_w[k] + sum(weight[(k, i)] for i in per_player[k])
            step = (push[k] - prior_w[k] * (Y[k] - (start.get(k) or 3.0))) / total_w
            Y[k] += FIT_DAMPING * step
    return ({k: Y[k] for k in per_player},
            {k: len(v) for k, v in per_player.items()})


RECENTER_MIN_PLAYERS = 20   # a level needs this many NV players to re-center


def recenter_levels(year_end: dict[str, float], counts: dict[str, int],
                    levels: dict[str, tuple[Optional[float], str]],
                    is_nv) -> dict[str, float]:
    """Shift every player at a level by the same amount so that level's Nevada
    players (MIN_MATCHES+) average the middle of the band again.

    Everyone starts mid-level, but the higher pools drift low over a season
    (losses to strong out-of-state playoff teams and to the level above), which
    pushed players near the bottom edge into false "down" calls. Order within a
    level is unchanged. 2025 -> 2026 backtest on 973 NV women: false downs
    27 -> 7, 90.3% -> 91.6% right (Shirin approved 2026-11-26)."""
    by_level: dict[float, list[float]] = {}
    for k, y in year_end.items():
        lv = (levels.get(k) or (None,))[0]
        if lv is not None and counts.get(k, 0) >= MIN_MATCHES and is_nv(k):
            by_level.setdefault(lv, []).append(y - lv)
    shift = {lv: -LEVEL_WIDTH / 2 - sum(d) / len(d)
             for lv, d in by_level.items() if len(d) >= RECENTER_MIN_PLAYERS}
    return {k: y + shift.get((levels.get(k) or (None,))[0], 0.0)
            for k, y in year_end.items()}


def year_end_call(estimate: Optional[float], matches: int, level: Optional[float],
                  letter: str = "", strikes: Optional[int] = None,
                  play_up: float = 0.0, bumped_up: bool = False) -> dict:
    """Summarise one player's bump outlook.

    Returns {estimate, margin, call, label, matches, strikes}. ``call`` is one
    of up / lean_up / hold / lean_down / down / few_matches / unknown.
    ``play_up`` is the share of counted matches on higher-level flights.
    ``bumped_up``: the player's level went up at last year's year-end.
    ``direction`` (up / hold / down) is the backtested prediction: up when the
    estimate is past the top (or the play-up tiebreak applies), down when at or
    under the floor.
    """
    if level is None:
        return {"call": "unknown", "label": "No NTRP level"}
    if estimate is None or matches < MIN_MATCHES:
        return {"call": "few_matches", "label": f"Too few matches ({matches})",
                "matches": matches}
    est = estimate
    top, floor = level, level - LEVEL_WIDTH + DOWN_LINE_SHIFT.get(level, 0.0)
    if est > top:
        margin = est - top
        call = "up" if margin >= CLEAR_MARGIN else "lean_up"
    elif est <= floor:
        margin = est - floor
        call = "down" if -margin >= CLEAR_MARGIN else "lean_down"
    else:
        # Inside the level: distance to the nearer edge, signed toward it.
        margin = est - top if (top - est) < (est - floor) else est - floor
        call = ("lean_up" if est > top - CLEAR_MARGIN / 2 else
                "lean_down" if est <= floor + LEAN_DOWN_ZONE else "hold")
    tiebreak = (floor < est <= top and top - est < PLAY_UP_ZONE
                and play_up >= PLAY_UP_SHARE)
    if tiebreak:
        call = "lean_up"
    slide_back = (bumped_up and level > LOWEST_LEVEL and not tiebreak
                  and floor < est <= floor + BUMPED_UP_DOWN_ZONE)
    if level <= LOWEST_LEVEL and call in ("down", "lean_down"):
        # Nobody drops below 2.5 (no NV player is rated 2.0); in the 2025
        # backtest all 13 "down" calls at 2.5 held.
        call = "hold"
    out = {
        "estimate": round(est, 3),
        "margin": round(margin, 3),
        "call": call,
        "label": {
            "up": "Likely up", "lean_up": "Leaning up", "hold": "Holds",
            "lean_down": "Leaning down", "down": "Likely down",
        }[call],
        "matches": matches,
        "direction": ("up" if est > top or tiebreak else
                      "down" if (est <= floor and level > LOWEST_LEVEL) or slide_back
                      else "hold"),
    }
    if slide_back:
        out["label"] = "Leaning down (bumped up last year)"
    if tiebreak:
        out["label"] = "Leaning up (plays up)"
        out["play_up"] = round(play_up, 2)
    # Past the top of the NEXT level too: a two-level move (e.g. 3.5 -> 4.5).
    if call == "up" and est > top + LEVEL_WIDTH + CLEAR_MARGIN:
        out["levels"] = 2
        out["label"] = "Likely up two levels"
    elif call == "down" and est <= floor - LEVEL_WIDTH - CLEAR_MARGIN:
        out["levels"] = 2
        out["label"] = "Likely down two levels"
    if letter in ("S", "A") and strikes is not None:
        out["strikes"] = strikes
    return out


# Which matches USTA counts toward a year-end rating (Shirin, 2026-11-25):
# every adult league except summer and mixed leagues, played after the previous year's
# cutoff and on or before this year's. The cutoff is about a month before the
# year-end ratings come out; later matches count toward the next year.
RATING_YEAR_WINDOWS = {
    2025: ("11/10/2024", "11/9/2025"),   # cutoff assumed Nov 9 (Shirin, unconfirmed)
    2026: ("11/9/2025", "11/8/2026"),
}
CURRENT_RATING_YEAR = 2026


def counts_toward_year_end(date: str, league: str = "",
                           year: int = CURRENT_RATING_YEAR) -> bool:
    lg = (league or "").upper()
    if "SUMMER" in lg or "MIXED" in lg:   # mixed never counts toward NTRP (Shirin)
        return False
    lo, hi = RATING_YEAR_WINDOWS[year]
    k = R._date_str_sort_key
    return k(lo) <= k(date) <= k(hi)


TR_STARTS: set[str] = set()   # filled by year_end_inputs


PRIOR_LEVELS: dict[str, float] = {}   # last year's level, from last year's crawl rosters
TL_AREA: dict[str, str] = {}   # crawl-only ("tl::") key -> TennisLink area, filled by year_end_inputs


def is_nv_crawl_key(k: str) -> bool:
    """A crawl-only key for a Nevada person. Uses the person's area, not the
    key text: crawl person ids are TennisLink player ids, not name|AREA|n."""
    return k.startswith("tl::") and (TL_AREA.get(k) == "NEVADA" or "|NEVADA|" in k)


def year_end_inputs(players: list[dict], pbn: dict, year: int = CURRENT_RATING_YEAR):
    """(events, starts, letters, playoffs) for the year-end fit: standings
    data plus the TennisLink team crawl, filtered to matches USTA counts."""
    from engine import crawl_events as C
    events = R._collect_court_events(R._discover_standings_files(), pbn)
    playoffs = playoff_match_ids()
    levels = {k: parse_level(p.get("ntrp_rating")) for k, p in pbn.items()}
    starts = {k: start_rating(p.get("dynamic_rating_baseline"), *levels[k])
              for k, p in pbn.items()}
    crawl = C.load_crawl(year)
    if crawl:
        extra, keys = C.crawl_events(crawl, pbn, events)
        matches = crawl.get("matches", {})
        events += [e for e in extra
                   if counts_toward_year_end(e.date, matches[e.match_id].get("league", ""), year)]
        playoffs |= {mid for mid, m in matches.items() if m.get("kind") == "champ"}
        for pid, k in keys.items():
            person = crawl["people"][pid]
            if k.startswith("tl::"):
                TL_AREA.setdefault(k, person.get("area") or "")
                levels[k] = parse_level(person.get("level"))
                starts[k] = start_rating(C.baseline_for(person, matches), *levels[k])
            elif person.get("area") != "NEVADA" and C.tr_baseline(person) is not None:
                # A CO/UT/ID playoff opponent linked to their own season: start at
                # the hand-collected TennisRecord rating (see compute_bumps).
                starts[k] = start_rating(C.tr_baseline(person), *levels[k])
                TR_STARTS.add(k)
    # The rating year starts in the previous November, so its first weeks
    # (fall-league finals, Tri-Level Sectionals and Nationals) are in LAST
    # year's crawl. People are matched across years by their TennisLink
    # history link, NV players by players.json key.
    prev = C.load_crawl(year - 1)
    if prev:
        by_url = {}
        if crawl:
            by_url = {v.get("history_url"): keys[pid] for pid, v in crawl.get("people", {}).items()
                      if v.get("history_url") and pid in keys}
        extra, prev_keys = C.crawl_events(prev, pbn, events)
        remap = {pid: by_url.get(prev["people"][pid].get("history_url")) or k
                 for pid, k in prev_keys.items()}
        key_map = {prev_keys[pid]: k for pid, k in remap.items()}
        pm = prev.get("matches", {})
        for e in extra:
            if not counts_toward_year_end(e.date, pm[e.match_id].get("league", ""), year):
                continue
            e.winner_keys = [key_map.get(x, x) for x in e.winner_keys]
            e.loser_keys = [key_map.get(x, x) for x in e.loser_keys]
            e.prior_crawl = True
            events.append(e)
        playoffs |= {mid for mid, m in pm.items() if m.get("kind") == "champ"}
        for pid, k in remap.items():
            lv_prev = parse_level(prev["people"][pid].get("ntrp"))[0]
            if lv_prev is not None:
                PRIOR_LEVELS.setdefault(k, lv_prev)
            if k.startswith("tl::"):
                TL_AREA.setdefault(k, prev["people"][pid].get("area") or "")
            if k.startswith("tl::") and k not in levels:
                person = prev["people"][pid]
                levels[k] = parse_level(person.get("level"))
                starts[k] = start_rating(C.baseline_for(person, pm), *levels[k])
    events = [e for e in events if counts_toward_year_end(e.date, "", year)]
    events.sort(key=R._date_sort_key)
    letters = {k: lv[1] for k, lv in levels.items()}
    # A TennisRecord start dated in December already includes late-fall matches.
    known_through = {k: p["baseline_date"] for k, p in pbn.items()
                     if p.get("baseline_date") and p.get("dynamic_rating_baseline") is not None}
    return events, starts, letters, playoffs, levels, known_through


def play_up_shares(events, levels) -> dict[str, float]:
    """Share of each player's matches played on a single-level flight above
    their own level (combo flights like 7.0 don't count as playing up)."""
    total: dict[str, int] = {}
    up: dict[str, int] = {}
    for e in events:
        try:
            flight = float(e.division)
        except (TypeError, ValueError):
            flight = None
        for k in e.winner_keys + e.loser_keys:
            total[k] = total.get(k, 0) + 1
            lv = (levels.get(k) or (None,))[0]
            if flight is not None and flight <= 5.5 and lv is not None and flight > lv:
                up[k] = up.get(k, 0) + 1
    return {k: up.get(k, 0) / n for k, n in total.items()}


def little_known_playoffs(events: list, playoffs: set[str]) -> set[str]:
    """Playoff matches with an out-of-state player we know little about: no
    season of her own in our data and no hand-collected TennisRecord start.
    These count like regular matches (1x) instead of PLAYOFF_WEIGHT, because a
    loss to an opponent we underrate looks like a real drop at 2.5x. 2025 ->
    2026 backtest: 90.3% -> 91.5% right, mostly fewer false downs (Shirin
    approved 2026-11-26). Nevada-only playoffs keep the extra weight."""
    def little_known(k: str) -> bool:
        return k.startswith("tl::") and not is_nv_crawl_key(k) and k not in TR_STARTS
    return {e.match_id for e in events if e.match_id in playoffs
            and any(little_known(k) for k in e.winner_keys + e.loser_keys)}


def compute_bumps(players: list[dict], events: Optional[list] = None,
                  playoffs: Optional[set[str]] = None,
                  include_crawl: bool = False) -> dict[str, dict]:
    """{player_key: year_end_call(...)} for every player in players.json.

    With no ``events``, uses the standings plus the team crawl (see
    year_end_inputs). ``include_crawl`` adds calls for crawled Nevada people
    who aren't in players.json (keys ``tl::<crawl person id>``), so every
    player on the NV list, men included, gets a Bump."""
    pbn = R._build_players_by_name(players)
    if events is None:
        events, starts, letters, auto_playoffs, levels, known = year_end_inputs(players, pbn)
        if playoffs is None:
            playoffs = auto_playoffs - little_known_playoffs(events, auto_playoffs)
    else:
        if playoffs is None:
            playoffs = playoff_match_ids()
        levels = {k: parse_level(p.get("ntrp_rating")) for k, p in pbn.items()}
        starts = {k: start_rating(p.get("dynamic_rating_baseline"), *levels[k])
                  for k, p in pbn.items()}
        letters = {k: levels[k][1] for k in pbn}
        known = {}
    if START_MODE == "mid":
        # Nothing is baked into a mid-level start, so every counted match counts.
        # Out-of-state ("tl::") players already start mid-level, or at their
        # hand-collected TennisRecord rating when we have one (baseline_for).
        starts = {k: s if k.startswith("tl::") or k in TR_STARTS else
                  mid_level_start((levels.get(k) or (None,))[0], s)
                  for k, s in starts.items()}
        known = {}
    year_end, counts = fit_year_end(events, starts, letters, playoffs,
                                    known_through=known)
    def is_nv(k: str) -> bool:
        if k.startswith("tl::"):
            return is_nv_crawl_key(k)
        return (pbn[k].get("state") or "NV").upper() == "NV" if k in pbn else False
    year_end = recenter_levels(year_end, counts, levels, is_nv)
    strikes = run_bump(events, starts, levels)    # running rating: strikes only
    play_up = play_up_shares(events, levels)
    out = {}
    keys = list(pbn)
    if include_crawl:
        keys += [k for k in levels if is_nv_crawl_key(k)]
    for k in keys:
        st = strikes.get(k)
        out[k] = year_end_call(year_end.get(k), counts.get(k, 0), *levels[k],
                               strikes=st.strikes if st else None,
                               play_up=play_up.get(k, 0.0),
                               bumped_up=(levels[k][0] is not None and k in PRIOR_LEVELS
                                          and PRIOR_LEVELS[k] < levels[k][0]))
    return out


def run_bumps() -> int:
    """Write a ``bump`` dict onto every player in players.json. Returns count."""
    players = R._load(R.PLAYERS_JSON, [])
    if not players:
        return 0
    pbn = R._build_players_by_name(players)
    calls = compute_bumps(players)
    for k, p in pbn.items():
        p["bump"] = calls[k]
    R._save(R.PLAYERS_JSON, players)
    return len(calls)


if __name__ == "__main__":
    from collections import Counter
    players = R._load(R.PLAYERS_JSON, [])
    calls = compute_bumps(players)
    print(Counter(c["call"] for c in calls.values()))
