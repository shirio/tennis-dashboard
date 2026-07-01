# ===========================================================================
# IMPORTANT NOTES — read before editing this file
# ===========================================================================
# 1. Win detection in match lines MUST use winner_team/loser_team fields,
#    NOT the players_home/players_away column position. Scorecards are
#    sometimes swapped — the column position does not reliably indicate
#    which team's players are listed. _team_by_name lookup is the safe fallback.
# 2. This file generates both the main dashboards (women_30/35.html) AND
#    the matchup explorer pages (matchups_30/35.html). build_dashboards()
#    calls _generate_html() and _build_matchup_page() for each division.
# 3. _CSS and _JS are shared by the main dashboards.
#    _MATCHUP_CSS and _MATCHUP_JS are for the explorer pages only.
# 4. After editing, run: python3 rebuild.py  (ratings + HTML rebuild)
#    generate_notes.py must be run separately if notes logic changed.
# 5. These notes must be preserved unless the user explicitly says to remove them.
# ===========================================================================
"""
engine/build_html.py
Generate women_30.html and women_35.html from standings + player data.
Tabs: Standings | Team Rosters | All Players | All Results
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"
REGIONS_JSON = DATA_DIR / "regions.json"
STANDINGS_30 = DATA_DIR / "standings_women_30.json"
STANDINGS_35 = DATA_DIR / "standings_women_35.json"


def _load_regions() -> dict:
    if REGIONS_JSON.exists():
        return json.loads(REGIONS_JSON.read_text())
    return {}


def _get_state_config(state_code: str) -> dict:
    regions = _load_regions()
    cfg = regions.get("states", {}).get(state_code, {})
    cfg["_state_code"] = state_code
    return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def _esc(s) -> str:
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _date_sort_key(d: str) -> tuple:
    """Convert M/D/YYYY string to (YYYY, MM, DD) tuple for correct chronological sort.

    Plain string sort fails for dates like 5/2 vs 5/16 because '5/1...' < '5/2...'
    alphabetically, placing week 9 (5/16) before week 7 (5/2).
    """
    try:
        m, day, y = d.split("/")
        return (int(y), int(m), int(day))
    except Exception:
        return (0, 0, 0)


# ---------------------------------------------------------------------------
# Team name abbreviations
# ---------------------------------------------------------------------------

_TEAM_ABBREV = {
    "lake las vegas sports club": "Lake LV",
    "red rock cc":                "Red Rock",
    "red rock cc #1":             "Red Rock #1",
    "red rock cc #2":             "Red Rock #2",
    "life time fitness/gv":       "LTF",
    "life time fitness":          "LTF",
    "anthem cc":                  "Anthem",
    "club ridges":                "Ridges",
    "desert palm":                "D. Palm",
    "dragonridge cc":             "Dragonridge",
    "southern highlands":         "SoHi",
    "spanish oaks":               "S. Oaks",
    "spanish trail":              "S. Trail",
    "spanish trail #1":           "S. Trail #1",
    "spanish trail #2":           "S. Trail #2",
    "summerlin arbors":           "Summerlin",
    "whitney mesa park":          "Whitney",
}


def _abbrev_team(name: str) -> str:
    """Return abbreviated team name if one is defined, else original."""
    return _TEAM_ABBREV.get((name or "").lower().strip(), name)


def _simplify_subflight_labels(subflights: list[dict], ntrp: str = "") -> dict[str, tuple[str, str, str]]:
    """Build display names for subflight labels.

    Returns {raw_label: (tab_label, column_label, group_name)} where:
    - tab_label: shown on the subflight tab button (e.g. "A: South I")
    - column_label: shown in the SF column in All Players (e.g. "A")
    - group_name: grouping header for visual separation (e.g. "Denver Metro")
    """
    raw_labels = [sf.get("flight_label", "") for sf in subflights]
    mapping: dict[str, tuple[str, str, str]] = {}

    _champ_exact = {"championships", "districts", "playoffs"}

    def _is_champ(lbl):
        ll = lbl.lower().strip()
        return ll in _champ_exact or ll.startswith("championships ")

    for lbl in raw_labels:
        if _is_champ(lbl):
            ll = lbl.lower().strip()
            if ll in _champ_exact:
                mapping[lbl] = ("Districts", "Dist", "Districts")
            else:
                suffix = lbl[len("Championships"):].strip()
                short = re.sub(r'(?i)\s*(playoff|district|championship)s?\s*$', '', suffix).strip()
                tab = short if short else "Districts"
                mapping[lbl] = (tab, tab[:4], "Districts")
            continue

    regular = [l for l in raw_labels if not _is_champ(l)]
    if not regular:
        return mapping

    # Already simple labels (single word/letter like "A", "B")
    if all(len(l.split()) <= 1 and len(l) <= 3 for l in regular):
        for lbl in regular:
            mapping[lbl] = (lbl, lbl, "")
        return mapping

    # UT pattern: "UT-AM 3.0W AM Teal" → color is the last word
    _colors = r'\b(Teal|Green|Gold|Pink|Indigo|Yellow|Blue|White|Orange|Ivory|Red|Purple)\s*$'
    if all(re.search(_colors, l, re.IGNORECASE) for l in regular):
        for lbl in regular:
            color = lbl.rsplit(None, 1)[-1]
            mapping[lbl] = (color, color, "")
        return mapping

    # CO-style pattern: many subflights with region prefixes
    cleaned = {}
    for lbl in regular:
        cleaned[lbl] = re.sub(r'^[A-Z]{2}-', '', lbl)

    # Find the single dominant prefix shared by the most labels.
    # Prefer longer prefixes at the same count (e.g. "DENVER METRO" over "DENVER").
    best_prefix = ""
    best_count = 0
    for cl in cleaned.values():
        words = cl.split()
        for prefix_len in range(1, len(words)):
            pfx = " ".join(words[:prefix_len])
            cnt = sum(1 for c in cleaned.values() if c.startswith(pfx + " "))
            if cnt >= 3 and (cnt > best_count or (cnt == best_count and len(pfx) > len(best_prefix))):
                best_count = cnt
                best_prefix = pfx

    # Assign letter codes, grouped: dominant region first, then others
    dominant = sorted([l for l in regular if cleaned[l].startswith(best_prefix + " ")]) if best_prefix else []
    others = sorted([l for l in regular if l not in dominant])

    letter_idx = 0
    for lbl in dominant + others:
        letter = chr(ord('A') + letter_idx)
        letter_idx += 1
        clean = cleaned[lbl]
        suffix = clean[len(best_prefix):].strip() if lbl in dominant else clean
        # Determine group name
        if lbl in dominant:
            group_name = best_prefix.title()
        else:
            # Use the cleaned label as group, or "" if few non-dominant entries
            remaining = [l for l in regular if l not in dominant]
            group_name = "" if len(remaining) <= 2 else "Other Regions"
        tab_label = f"{letter}: {suffix}"
        mapping[lbl] = (tab_label, letter, group_name)

    return mapping


# ---------------------------------------------------------------------------
# Line label helpers
# ---------------------------------------------------------------------------

def _line_label_short(lnum: str) -> str:
    """Convert '1# Singles' -> 'S1', '2# Doubles' -> 'D2', etc."""
    m = re.match(r'^(\d+)#\s+(Singles|Doubles)', (lnum or "").strip())
    if not m:
        return lnum
    prefix = "S" if m.group(2) == "Singles" else "D"
    return f"{prefix}{m.group(1)}"


_LINE_PILL_COLORS = {
    "S1": "pill-s1", "S2": "pill-s2", "S3": "pill-s3",
    "D1": "pill-d1", "D2": "pill-d2", "D3": "pill-d3",
}


def _lines_pills_html(lines_played) -> str:
    """Render lines_played (list of court labels, possibly with 'xN' counts) as colored pills.

    Accepts entries like "D1", "D1x2", "S2x3" — the label is the base (D1/S2/etc.)
    and the optional xN suffix shows how many times that court was played.
    """
    if not lines_played:
        return "–"
    # Legacy: if it's a number, just show as plain text
    if isinstance(lines_played, (int, float)):
        return str(int(lines_played))
    if isinstance(lines_played, str):
        try:
            return str(int(lines_played))
        except ValueError:
            pass
    # It's a list like ["D1x2", "D2", "S2"]
    pills = ""
    for raw in lines_played:
        raw = str(raw).strip()
        # Parse "D1x2" or "S1" format
        m = re.match(r'^([A-Z]\d+)(x\d+)?$', raw)
        if m:
            label  = m.group(1)
            suffix = m.group(2) or ""
        else:
            label  = raw
            suffix = ""
        cls = _LINE_PILL_COLORS.get(label, "pill-d1")
        pills += f'<span class="line-pill {cls}">{_esc(label + suffix)}</span>'
    return pills or "–"


def _fmt_rating(v) -> str:
    if v is None:
        return "–"
    try:
        return f"{float(v):.2f}"
    except Exception:
        return str(v)


def _rating_span(curr, baseline, ntrp_str: str) -> str:
    """Return color-coded span for current rating."""
    s = _fmt_rating(curr)
    if curr is None:
        return f'<span class="rn">{s}</span>'
    try:
        c = float(curr)
        b = float(baseline) if baseline is not None else c
        if c > b + 0.005:
            return f'<span class="ru">{s}</span>'
        elif c < b - 0.005:
            return f'<span class="rd">{s}</span>'
        return f'<span class="rn">{s}</span>'
    except Exception:
        return f'<span class="rn">{s}</span>'


def _global_diff_span(glob, div_rating) -> str:
    """Show global-vs-division diff as a muted ±0.XX value. Blank if same or missing."""
    if glob is None or div_rating is None:
        return ""
    try:
        g, d = float(glob), float(div_rating)
        diff = g - d
        if abs(diff) < 0.005:
            return ""
        sign = "+" if diff > 0 else ""
        cls = "gdiff-up" if diff > 0 else "gdiff-dn"
        return f'<span class="{cls}">{sign}{diff:.2f}</span>'
    except Exception:
        return ""


def _baseline_diff_span(curr, baseline) -> tuple[str, str]:
    """
    Returns (html, sort_key) for the New-vs-Baseline diff column.
    html: coloured ±0.XX span (or '–' if missing).
    sort_key: numeric string for data-sort attribute.
    """
    if curr is None or baseline is None:
        return "–", "-999"
    try:
        c, b = float(curr), float(baseline)
        diff = c - b
        sort_key = f"{diff:.4f}"
        if abs(diff) < 0.005:
            return "<span class='gdiff-zero'>0</span>", sort_key
        sign = "+" if diff > 0 else ""
        cls = "gdiff-up" if diff > 0 else "gdiff-dn"
        return f'<span class="{cls}">{sign}{diff:.2f}</span>', sort_key
    except Exception:
        return "–", "-999"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _ntrp_division_compatible(division: str, ntrp_rating: str) -> bool:
    """Check if a player's tennisrecord/roster NTRP rating is plausible for
    the division they're actually competing in. Mirrors
    scrapers/scrape_tennisrecord.py's _ntrp_compatible — kept as a separate
    copy here since that check gates what tennisrecord data gets ACCEPTED
    into players.json, while this one VALIDATES data that already made it
    in (e.g. via the TennisLink roster path, which has no such gate) so a
    genuine rating/division mismatch (sandbagging signal, or a rating a
    full level above/below what's normal) surfaces as a warning instead of
    silently passing through unflagged.
    """
    if not division or not ntrp_rating:
        return True
    parts = ntrp_rating.split()
    try:
        ntrp_num = float(parts[0])
    except (ValueError, IndexError):
        return True
    letter = parts[1].upper() if len(parts) > 1 else ""
    try:
        div_num = float(division[:3])
    except (ValueError, IndexError):
        return True
    if letter == "D":
        effective = ntrp_num - 0.5
        return effective <= div_num and effective >= div_num - 0.5
    return ntrp_num >= div_num - 0.5 and ntrp_num <= div_num


def _validate_ntrp(players: list[dict]) -> list[str]:
    """Flag players whose ntrp_rating doesn't plausibly match the division
    they're competing in — either a full level below (possible sandbagging)
    or more than 0.5 above (unusual jump). Catches cases the tennisrecord.com
    ingestion gate (_ntrp_compatible) never sees, e.g. ntrp_rating set
    directly from a TennisLink roster page with no validation at all.
    """
    warnings = []
    for p in players:
        div = p.get("division", "")
        ntrp = p.get("ntrp_rating", "")
        if not _ntrp_division_compatible(div, ntrp):
            warnings.append(
                f"{p.get('name','?')} ({p.get('state','?')}, {p.get('team','?')}): "
                f"ntrp_rating {ntrp!r} is not plausible for division {div!r}"
            )
    return warnings


def _validate(subflights: list[dict]) -> list[str]:
    """
    Cross-check each team's W/L in the standings table against what the
    match results actually show. Returns a list of human-readable warnings.
    """
    warnings = []
    for sf in subflights:
        label = sf.get("flight_label", "?")
        wins: dict[str, int] = defaultdict(int)
        losses: dict[str, int] = defaultdict(int)
        for m in sf.get("matches", []):
            if m.get("pending"):
                continue
            hw = m.get("team_wins_home") or 0
            aw = m.get("team_wins_away") or 0
            status = (m.get("status") or "").lower()
            if hw > aw:
                wins[m["home_team"]] += 1
                losses[m["away_team"]] += 1
            elif aw > hw:
                wins[m["away_team"]] += 1
                losses[m["home_team"]] += 1
            elif "won" in status:
                wins[m["home_team"]] += 1
                losses[m["away_team"]] += 1
            elif "lost" in status:
                wins[m["away_team"]] += 1
                losses[m["home_team"]] += 1

        for t in sf.get("teams", []):
            name = t.get("team_name", "")
            sw, sl = (t.get("team_wins") or 0), (t.get("team_losses") or 0)
            rw, rl = wins.get(name, 0), losses.get(name, 0)
            if sw == 0 and sl == 0 and (rw > 0 or rl > 0):
                continue
            if sw == 0 and sl == 0 and (rw > 0 or rl > 0):
                continue
            if rw != sw or rl != sl:
                warnings.append(
                    f"Subflight {label} — {name}: "
                    f"standings shows {sw}W–{sl}L but match results count {rw}W–{rl}L"
                )
    return warnings


# ---------------------------------------------------------------------------
# Per-court winner detection helpers
# ---------------------------------------------------------------------------

def _match_winner_team(match: dict) -> str | None:
    """Return the team name that won the overall match, or None if unknown."""
    tw = match.get("team_wins_home")
    ta = match.get("team_wins_away")
    ht = (match.get("home_team") or "").strip()
    at = (match.get("away_team") or "").strip()
    if tw is not None and ta is not None and ht and at:
        if tw > ta:
            return ht
        if ta > tw:
            return at
    return None


# ---------------------------------------------------------------------------
# Badge / record helpers
# ---------------------------------------------------------------------------

def _badge_record(w, l) -> str:
    if w is None and l is None:
        return "–"
    w, l = int(w or 0), int(l or 0)
    rec = f"{w}–{l}"
    cls = "bw" if w > l else ("bl" if l > w else "bn")
    return f'<span class="badge {cls}">{rec}</span>'


def _team_result_for(matches: list[dict], team: str) -> list[dict]:
    """
    Build a per-team list of results (score/won from that team's perspective).
    """
    out = []
    for m in sorted(matches, key=lambda x: _date_sort_key(x.get("date", ""))):
        hw = m.get("team_wins_home")
        aw = m.get("team_wins_away")
        pending = m.get("pending", False)
        status = (m.get("status") or "").lower()
        if m.get("home_team") == team:
            opp = m.get("away_team", "")
            if not pending and hw is not None and aw is not None:
                won = hw > aw if hw != aw else ("won" in status)
                score = f"{hw}–{aw}"
            else:
                won, score = None, ""
        elif m.get("away_team") == team:
            opp = m.get("home_team", "")
            if not pending and hw is not None and aw is not None:
                won = aw > hw if hw != aw else ("lost" in status)
                score = f"{aw}–{hw}"
            else:
                won, score = None, ""
        else:
            continue
        out.append({
            "date": m.get("date", ""),
            "opponent": opp,
            "won": won,
            "score": score,
            "pending": pending,
            "lines": m.get("lines", []),
            # Whether per-court results have been verified via TL match page.
            # Without tl_match_id the court result field is unreliable (defaults
            # to "home" for every court) so we suppress per-court highlighting.
            "courts_verified": bool(m.get("tl_match_id")),
            # Official home/away team names so we can detect swapped scorecards.
            "home_team": m.get("home_team", ""),
            "away_team": m.get("away_team", ""),
            "is_tie": (hw == aw and hw is not None and not pending),
        })
    return out


def _result_badge(won, score, pending) -> str:
    if pending or won is None:
        return '<span class="badge bn">Pending</span>'
    if won:
        return f'<span class="badge bw">W&nbsp;{_esc(score)}</span>'
    return f'<span class="badge bl">L&nbsp;{_esc(score)}</span>'


def _tie_game_totals(lines: list[dict], team_a: str, team_b: str) -> tuple[int, int]:
    """Sum total games won by team_a and team_b across all courts for a 2-2 tie.

    Scores are stored winner-first (e.g. '6-4 6-3' means winner took 12, loser 7).
    Uses winner_team/loser_team fields — no orientation dependency.
    Returns (team_a_total, team_b_total); returns (0, 0) if data is insufficient.
    """
    ta = team_a.upper()
    tb = team_b.upper()
    ga = gb = 0
    for ln in lines:
        score = (ln.get("score") or "").strip()
        wt = (ln.get("winner_team") or "").upper()
        lt = (ln.get("loser_team") or "").upper()
        if not score or not wt:
            continue
        w_games = l_games = 0
        for part in score.split():
            m = re.match(r"^(\d+)-(\d+)$", part)
            if m:
                w_games += int(m.group(1))
                l_games += int(m.group(2))
        if wt == ta:
            ga += w_games
            gb += l_games
        elif wt == tb:
            gb += w_games
            ga += l_games
    return ga, gb


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------

def _wl_cell(w, l) -> str:
    """Render a W–L cell like '16–4', or '–' if both missing."""
    if w is None and l is None:
        return "–"
    return f"{int(w or 0)}–{int(l or 0)}"


def _standings_tab(subflights: list[dict], warnings: list[str],
                   sf_display: dict | None = None, id_prefix: str = "st",
                   show_nav_links: bool = True) -> str:
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in warnings)
        warn_html = f'<div class="warn-box">⚠ Data validation warnings:<ul>{items}</ul></div>'

    sf_display = sf_display or {}

    # Build subflight tabs with group separators
    sf_btns = ""
    prev_group = None
    for i, sf in enumerate(subflights):
        lbl = sf.get("flight_label", str(i))
        tab_lbl, _, group = sf_display.get(lbl, (lbl, lbl, ""))
        active = " on" if i == 0 else ""
        if group and group != prev_group:
            sf_btns += (f'<div class="sf-break"></div>' if group.lower() == "districts" else '') + f'<span class="sf-group-label{"" if group.lower() != "districts" else " sf-group-inline"}">{_esc(group)}</span>\n'
            prev_group = group
        elif not group and prev_group:
            prev_group = None
        sf_btns += (
            f'<button class="rtab sf-switcher{active}" '
            f'data-sf="{_esc(lbl)}" '
            f'onclick="filterStandingsSF(\'{_esc(lbl)}\',\'{id_prefix}\')">'
            f'{_esc(tab_lbl)}</button>\n'
        )

    panes = ""
    for i, sf in enumerate(subflights):
        sf_raw = sf.get("flight_label", "")
        tab_lbl, _, _ = sf_display.get(sf_raw, (sf_raw, sf_raw, ""))
        lbl    = _esc(tab_lbl)
        teams  = sf.get("teams", [])
        matches = sf.get("matches", [])
        summary = _esc(sf.get("subflight_summary", "") or "")
        visible = "" if i == 0 else ' style="display:none"'

        # Sort by scraped team records (authoritative — TL resolves tiebreakers
        # in the standings page that can't be derived from per-court data alone).
        teams.sort(key=lambda t: (-t.get("team_wins", 0), t.get("team_losses", 99)))

        rows = ""
        for j, t in enumerate(teams, 1):
            name = t.get("team_name", "")
            w = t.get("team_wins")
            l = t.get("team_losses")
            iw = t.get("indiv_wins")  if t.get("indiv_wins")  is not None else "–"
            il = t.get("indiv_losses") if t.get("indiv_losses") is not None else "–"
            sw = t.get("sets_won")    if t.get("sets_won")    is not None else "–"
            sl = t.get("sets_lost")   if t.get("sets_lost")   is not None else "–"
            gw = t.get("games_won")   if t.get("games_won")   is not None else "–"
            gl = t.get("games_lost")  if t.get("games_lost")  is not None else "–"
            slug   = _slug(name)
            sf_esc = _esc(sf_raw).replace("'", "\\'")
            notes = _esc(t.get("notes", "") or "")
            name_cell = (
                f"<a class='team-link' href='#' "
                f"onclick=\"goToRoster('{slug}','{sf_esc}'); return false;\">"
                f"{_esc(name)}</a>"
            ) if show_nav_links else _esc(name)
            record_cell = (
                f"<a class='team-link' href='#' "
                f"onclick=\"goToResult('{slug}','{sf_esc}'); return false;\">"
                f"{_badge_record(w, l)}</a>"
            ) if show_nav_links else _badge_record(w, l)
            rows += (
                f"<tr>"
                f"<td class='rank'>{j}</td>"
                f"<td class='tname'>{name_cell}</td>"
                f"<td>{record_cell}</td>"
                f"<td class='st-w'>{iw}</td><td class='st-l'>{il}</td>"
                f"<td class='st-w'>{sw}</td><td class='st-l'>{sl}</td>"
                f"<td class='st-w'>{gw}</td><td class='st-l'>{gl}</td>"
                f"<td class='notes-cell'>{notes}</td>"
                f"</tr>\n"
            )

        summary_html = (
            f'<p class="sf-summary">{summary}</p>' if summary else ""
        )
        panes += (
            f'<div class="st-pane" data-sf="{_esc(sf_raw)}" data-prefix="{id_prefix}"{visible}>'
            f'<p class="sf-header">Subflight {lbl}</p>'
            f'{summary_html}'
            f'<table class="st-table"><thead><tr>'
            f'<th style="width:2rem">#</th><th>Team</th>'
            f'<th>Record</th><th colspan="2">Courts</th><th colspan="2">Sets</th><th colspan="2">Games</th><th>Notes</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
            f'</div>\n'
        )

    return (
        warn_html
        + f'<div class="rtabs" id="{id_prefix}-sf-tabs">{sf_btns}</div>'
        + panes
    )


def _parse_score(score: str, player_is_home: bool) -> tuple[int, int, int, int]:
    """Parse a score string into (sets_won, sets_lost, games_won, games_lost).
    Score format is always home-away per set, e.g. "6-3 4-6 1-0".
    """
    sw = sl = gw = gl = 0
    for part in (score or "").split():
        if "-" not in part:
            continue
        a, _, b = part.partition("-")
        try:
            hg, ag = int(a), int(b)
        except ValueError:
            continue  # skip "?-?" tokens
        mg = hg if player_is_home else ag
        og = ag if player_is_home else hg
        gw += mg
        gl += og
        if mg > og:
            sw += 1
        else:
            sl += 1
    return sw, sl, gw, gl


def _abbrev_line(line_label: str) -> str:
    """Convert '1# Doubles' → 'D1', '2# Singles' → 'S2', etc."""
    import re as _re
    m = _re.match(r'(\d+)#\s*(Singles|Doubles)', line_label or "")
    if not m:
        return line_label or ""
    n, t = m.group(1), m.group(2)
    return ("S" if t == "Singles" else "D") + n


def _rosters_tab(subflights: list[dict], players: list[dict], ntrp: str = "",
                 sf_display: dict | None = None) -> str:
    # Field suffix for per-division stats ("3.0" -> "30", "3.5" -> "35")
    _sfx = ntrp.replace(".", "") if ntrp else ""

    # Build team set from standings so we know which teams belong to this NTRP level
    _standings_teams = {
        t.get("team_name", "")
        for sf in subflights
        for t in sf.get("teams", [])
    }
    _standings_lower = {tn.lower(): tn for tn in _standings_teams}

    def _resolve_team(name: str) -> str:
        """Match a team name to its canonical standings form (case-insensitive)."""
        if name in _standings_teams:
            return name
        return _standings_lower.get(name.lower(), name)

    # Include a player in this division's rosters if:
    #   (a) their registered division matches this NTRP level → place in their registered team
    #   (b) they have match stats for this NTRP level AND their division-specific team
    #       (team_{sfx}) is in this division's standings → place in that actual team
    #       (handles dual-division players registered in one level but also playing in another)
    by_team: dict[str, list] = defaultdict(list)
    for p in players:
        t   = p.get("team", "")            # registered team
        div = p.get("division", "")
        in_div = not ntrp or div.startswith(ntrp)

        # The team they actually played for in this division (set during stats computation)
        div_team = _resolve_team((p.get(f"team_{_sfx}", "") or "")) if _sfx else ""

        has_ntrp_stats = bool(
            _sfx and (p.get(f"lines_played_{_sfx}") or p.get(f"wl_record_{_sfx}"))
        )

        # Prefer the team derived from actual scorecard data (team_35 / team_30) when
        # available and valid — it is more reliable than the roster-scraped 'team' field.
        effective_team = (div_team if (div_team and div_team in _standings_teams) else t) if in_div else t

        if in_div and effective_team:
            by_team[effective_team].append(p)
        elif has_ntrp_stats and div_team and div_team in _standings_teams:
            by_team[div_team].append(p)

    # Build panes grouped by subflight
    sf_display = sf_display or {}
    sf_labels = [sf.get("flight_label", str(i)) for i, sf in enumerate(subflights)]
    first_sf = sf_labels[0] if sf_labels else ""

    sf_btns = ""
    prev_group = None
    for i, lbl in enumerate(sf_labels):
        tab_lbl, _, group = sf_display.get(lbl, (lbl, lbl, ""))
        active = " on" if i == 0 else ""
        if group and group != prev_group:
            sf_btns += (f'<div class="sf-break"></div>' if group.lower() == "districts" else '') + f'<span class="sf-group-label{"" if group.lower() != "districts" else " sf-group-inline"}">{_esc(group)}</span>\n'
            prev_group = group
        elif not group and prev_group:
            prev_group = None
        sf_btns += (
            f'<button class="rtab sf-switcher{active}" '
            f'data-sf="{_esc(lbl)}" '
            f'onclick="filterSF(\'{_esc(lbl)}\',this,\'ro-sf-tabs\',\'ro-tabs\',\'ro\')">'
            f'{_esc(tab_lbl)}</button>\n'
        )

    team_tabs, rpanes = "", ""
    first_seen = True
    for sf in subflights:
        sf_lbl = sf.get("flight_label", "")
        for t in sf.get("teams", []):
            tname = t.get("team_name", "")
            if not tname:
                continue
            tid = f"ro-{_slug(sf_lbl)}-{_slug(tname)}"
            # First tab of first subflight is active
            active = " on" if first_seen else ""
            visible = "" if sf_lbl == first_sf else ' style="display:none"'
            first_seen = False

            roster = sorted(
                by_team.get(tname, []),
                key=lambda p: -(p.get(f"rating_{_sfx}") or
                                p.get("current_division_rating") or
                                p.get("dynamic_rating_baseline") or 0)
            )
            rows = ""
            for p in roster:
                ntrp_r = p.get("ntrp_rating", "") or ""
                baseline = p.get("dynamic_rating_baseline")
                curr = p.get(f"rating_{_sfx}") or p.get("current_division_rating")
                # Use per-division stats only — legacy wl_record is a combined total across
                # all divisions and is wrong for cross-listed players.
                wl    = p.get(f"wl_record_{_sfx}") or "–"
                lines = p.get(f"lines_played_{_sfx}") or "–"
                pnotes = _esc(p.get(f"notes_{_sfx}", "") or "")
                dw = p.get(f"default_wins_{_sfx}", 0) or 0
                if dw:
                    dw_note = (f'<span class="default-win-badge">'
                               f'incl. {dw} walkover{"s" if dw > 1 else ""}</span>')
                    pnotes = (pnotes + " " if pnotes else "") + dw_note
                rows += (
                    f"<tr>"
                    f"<td>{_esc(p.get('name',''))}</td>"
                    f"<td>{_esc(ntrp_r)}</td>"
                    f"<td>{_esc(_fmt_rating(baseline))}</td>"
                    f"<td>{_rating_span(curr, baseline, ntrp_r)}</td>"
                    f"<td style='white-space:nowrap'>{_esc(str(wl))}</td>"
                    f"<td>{_lines_pills_html(lines)}</td>"
                    f"<td class='notes-cell'>{pnotes}</td>"
                    f"</tr>\n"
                )
            if not rows:
                rows = "<tr><td colspan='7' class='muted'>No players yet.</td></tr>"

            team_tabs += (
                f'<button class="rtab{active}" data-sf="{_esc(sf_lbl)}"{visible} '
                f'onclick="sr(\'{tid}\',this,\'ro-tabs\')">'
                f'{_esc(_abbrev_team(tname))}'
                f'</button>\n'
            )
            rpanes += (
                f'<div id="{tid}" class="rpane{active}">'
                f'<p class="sec-title">{_esc(tname)} &mdash; Subflight {_esc(sf_lbl)}</p>'
                f'<table><thead><tr>'
                f'<th class="sortable" onclick="sortRoster(0)">Player ↕</th>'
                f'<th class="sortable" onclick="sortRoster(1)">NTRP ↕</th>'
                f'<th class="sortable" onclick="sortRoster(2)">Base ↕</th>'
                f'<th class="sortable" onclick="sortRoster(3)">New ↕</th>'
                f'<th class="sortable" onclick="sortRoster(4)">W–L ↕</th><th>Lines</th><th>Notes</th>'
                f'</tr></thead><tbody>{rows}</tbody></table></div>\n'
            )

    return (
        f'<div class="rtabs" id="ro-sf-tabs">{sf_btns}</div>'
        f'<div class="rtabs scrollable" id="ro-tabs">{team_tabs}</div>'
        + rpanes
    )


def _nkey(n: str) -> str:
    # Strip TennisLink DQ/note annotations: "Name - (DQ)*" or "(DQ)* - This player..."
    s = re.sub(r"\s*-\s*\(DQ\)\*?.*$", "", n.strip(), flags=re.IGNORECASE)
    s = re.sub(r"^\(DQ\)\*?\s*-\s*", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s.strip().lower())


_NOISE_NAMES = frozenset({
    "12:00 midnight", "12:00 noon", "dbl. default", "default",
    "n/a", "na", "n", "a", "(dq)", "(dq)*", "note -",
})


def _is_noise_name(name: str) -> bool:
    s = name.strip().lower()
    if s in _NOISE_NAMES:
        return True
    if re.match(r"^\d+:\d+\s*(am|pm|midnight|noon)$", s, re.I):
        return True
    if len(s) <= 1 and not s.isalpha():
        return True
    return False


def _traverse_match_histories(
    player_pool: list[dict],
    ntrp: str,
    subflights: list[dict] | None,
    other_subflights: list[dict] | None,
    state_code: str = "",
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    other_ntrp = "3.5" if ntrp == "3.0" else "3.0"

    # Detect same-name players across states; key them as "{state}::{nk}"
    _name_counts: dict[str, int] = {}
    for p in player_pool:
        nk = _nkey(p.get("name", ""))
        if nk:
            _name_counts[nk] = _name_counts.get(nk, 0) + 1
    _ambiguous = {k for k, c in _name_counts.items() if c > 1}

    by_name: dict[str, dict] = {}
    for p in player_pool:
        nk = _nkey(p.get("name", ""))
        if not nk:
            continue
        if nk in _ambiguous:
            st = (p.get("state") or "").lower()
            by_name[f"{st}::{nk}"] = p
        else:
            by_name[nk] = p

    def _bname_key(name: str) -> str:
        """Return by_name key for name, preferring state-qualified variant."""
        nk = _nkey(name)
        if nk in _ambiguous and state_code:
            qk = f"{state_code.lower()}::{nk}"
            if qk in by_name:
                return qk
        return nk

    def _pit_r(nk: str, match_date: str, div: str) -> str:
        """Point-in-time rating for a player at match_date in given division."""
        p = by_name.get(nk)
        if not p:
            return ""
        sfx = div.replace(".", "")
        pre_tl = p.get(f"rating_timeline_{sfx}") or {}
        if match_date in pre_tl:
            return f"{float(pre_tl[match_date]):.2f}"
        post_tl = p.get(f"rating_post_timeline_{sfx}") or {}
        match_key = _date_sort_key(match_date)
        prior = [(k, v) for k, v in post_tl.items() if _date_sort_key(k) < match_key]
        if prior:
            prior.sort(key=lambda x: _date_sort_key(x[0]), reverse=True)
            return f"{float(prior[0][1]):.2f}"
        base = p.get("dynamic_rating_baseline")
        if base is not None:
            return f"{float(base):.2f}"
        final = p.get(f"rating_{sfx}")
        if final is not None:
            return f"{float(final):.2f}"
        return ""

    player_histories: dict[str, list[dict]] = {}
    player_stats: dict[str, dict] = {}
    _seen: set[tuple] = set()

    for div, sfs in [(ntrp, subflights or []), (other_ntrp, other_subflights or [])]:
        div_sfx = div.replace(".", "")
        for sf in sfs:
            for match in sf.get("matches", []):
                if match.get("pending"):
                    continue
                date = match.get("date", "")
                mid = match.get("match_id", "")
                m_home_team = (match.get("home_team") or "").strip()
                m_away_team = (match.get("away_team") or "").strip()
                _match_wt = _match_winner_team(match)
                for line in match.get("lines", []):
                    home_raw = line.get("players_home", "") or ""
                    away_raw = line.get("players_away", "") or ""
                    wt = (line.get("winner_team") or "").strip()
                    lt = (line.get("loser_team") or "").strip()
                    score = line.get("score", "") or ""
                    line_label = line.get("line", "") or ""

                    winner_names = line.get("winner_names") or []
                    loser_names = line.get("loser_names") or []

                    # Fix defaults/forfeits: if one side has no players,
                    # the side WITH players won by default.
                    home_has = bool(home_raw and home_raw.strip())
                    away_has = bool(away_raw and away_raw.strip())
                    if not winner_names and loser_names and (not home_has or not away_has):
                        winner_names = loser_names
                        loser_names = []
                    elif winner_names and not loser_names:
                        pass  # already correct
                    is_walkover = not winner_names or not loser_names

                    all_players: list[tuple[str, bool | None]] = []
                    if wt:
                        for nm in winner_names:
                            all_players.append((nm, True))
                        for nm in loser_names:
                            all_players.append((nm, False))
                    else:
                        home_names = [n.strip() for n in home_raw.split("/")
                                      if n.strip() and not _is_noise_name(n)]
                        away_names = [n.strip() for n in away_raw.split("/")
                                      if n.strip() and not _is_noise_name(n)]
                        is_walkover = not home_names or not away_names
                        if _match_wt:
                            for nm in home_names + away_names:
                                all_players.append((nm, None))
                        else:
                            for nm in home_names + away_names:
                                all_players.append((nm, None))

                    for nm, court_won in all_players:
                        nk = _bname_key(nm)
                        pdata = by_name.get(nk)
                        if not pdata:
                            continue
                        player_team = None
                        for _tf in (f"team_{div_sfx}", "team", "team_30", "team_35"):
                            _tv = (pdata.get(_tf) or "").strip().lower()
                            if _tv and (_tv == m_home_team.lower() or
                                        _tv == m_away_team.lower()):
                                player_team = _tv
                                break
                        if not player_team:
                            # Player's registered team is not in this match — skip.
                            # Prevents cross-team name collisions (same name, different teams).
                            continue
                        if court_won is not None:
                            won = court_won
                            lost = not court_won
                        elif _match_wt:
                            won = player_team.lower() in _match_wt.lower()
                            lost = not won
                        else:
                            continue
                        dedup = (mid, line_label, nk, div)
                        if dedup in _seen:
                            continue
                        _seen.add(dedup)

                        if winner_names or loser_names:
                            if won:
                                partners = [n for n in winner_names if _bname_key(n) != nk]
                                opps = loser_names if not is_walkover else []
                            else:
                                partners = [n for n in loser_names if _bname_key(n) != nk]
                                opps = winner_names if not is_walkover else []
                        else:
                            h_names = [n.strip() for n in home_raw.split("/")
                                       if n.strip() and not _is_noise_name(n)]
                            a_names = [n.strip() for n in away_raw.split("/")
                                       if n.strip() and not _is_noise_name(n)]
                            player_is_home = nk in [_bname_key(n) for n in h_names]
                            my_side = h_names if player_is_home else a_names
                            opp_side = a_names if player_is_home else h_names
                            partners = [n for n in my_side if _bname_key(n) != nk]
                            opps = opp_side if not is_walkover else []

                        opp_r_list = [_pit_r(_bname_key(o), date, div) for o in opps]

                        partner_r_list = [_pit_r(_bname_key(pt), date, div) for pt in partners]

                        opp_team = (lt if won else wt) or (
                            m_away_team if player_team.lower() in m_home_team.lower()
                            else m_home_team)
                        sw_h, sl_h, gw_h, gl_h = _parse_score(score, True)
                        sw_a, sl_a, gw_a, gl_a = _parse_score(score, False)
                        if won:
                            sw, sl, gw, gl = (sw_h, sl_h, gw_h, gl_h) if sw_h >= sl_h else (sw_a, sl_a, gw_a, gl_a)
                        else:
                            sw, sl, gw, gl = (sw_h, sl_h, gw_h, gl_h) if sl_h >= sw_h else (sw_a, sl_a, gw_a, gl_a)

                        dq_players_ln = line.get("dq_players") or []
                        rec = {
                            "date": date, "div": div, "line": line_label,
                            "won": won, "score": score, "wko": is_walkover,
                            "partners": partners, "partner_r": partner_r_list,
                            "opps": opps, "opp_r": opp_r_list,
                            "opp_team": opp_team,
                            "sw": sw, "sl": sl, "gw": gw, "gl": gl,
                            # Court had a disqualified player — not a valid competitive
                            # match. "dq" lists who; "self_dq" flags whether THIS player
                            # (nk) is the one disqualified (drives whole-row styling in
                            # the division she was DQ'd in, vs. just the line in others'
                            # histories where she's merely the opponent/partner).
                            "dq": dq_players_ln,
                            "self_dq": _bname_key(nm) in {_bname_key(d) for d in dq_players_ln},
                        }
                        player_histories.setdefault(nk, []).append(rec)
                        st = player_stats.setdefault(
                            nk, {"sw30":0,"sl30":0,"gw30":0,"gl30":0,
                                 "sw35":0,"sl35":0,"gw35":0,"gl35":0,
                                 "w":0,"l":0,"w30":0,"l30":0,"w35":0,"l35":0}
                        )
                        if is_walkover:
                            st["wko"] = st.get("wko", 0) + 1
                        else:
                            d = div.replace(".", "")
                            st[f"sw{d}"] += sw; st[f"sl{d}"] += sl
                            st[f"gw{d}"] += gw; st[f"gl{d}"] += gl
                            for _or in opp_r_list:
                                if _or:
                                    try:
                                        st[f"or_sum{d}"] = st.get(f"or_sum{d}", 0.0) + float(_or)
                                        st[f"or_n{d}"]   = st.get(f"or_n{d}",   0)   + 1
                                    except ValueError:
                                        pass
                            if won: st["w"] += 1
                            else:   st["l"] += 1
                            if div == "3.0":
                                if won: st["w30"] += 1
                                else:   st["l30"] += 1
                            else:
                                if won: st["w35"] += 1
                                else:   st["l35"] += 1

    for nk in player_histories:
        player_histories[nk].sort(key=lambda r: _date_sort_key(r["date"]))

    return player_histories, player_stats


def _players_tab(players: list[dict], ntrp: str, subflights: list[dict] = None,
                 other_subflights: list[dict] = None,
                 is_sectionals: bool = False,
                 all_players_pool: list[dict] = None,
                 sf_display: dict | None = None,
                 state_code: str = "") -> str:
    _sfx = ntrp.replace(".", "") if ntrp else ""
    other_ntrp = "3.5" if ntrp == "3.0" else "3.0"
    _other_sfx = other_ntrp.replace(".", "")
    sf_display = sf_display or {}

    # Build team → subflight column label lookup (keyed uppercase for case-insensitive match)
    # Don't let "Championships" override a team's regular-season subflight
    team_to_sf: dict[str, str] = {}
    team_to_sf_raw: dict[str, str] = {}
    for sf_obj in (subflights or []):
        raw_lbl = sf_obj.get("flight_label", "")
        _, col_lbl, _ = sf_display.get(raw_lbl, (raw_lbl, raw_lbl, ""))
        for t in sf_obj.get("teams", []):
            tn = t.get("team_name", "").upper()
            if tn not in team_to_sf or not raw_lbl.lower().startswith("championships"):
                team_to_sf[tn] = col_lbl
                team_to_sf_raw[tn] = raw_lbl

    def _in_ntrp(p):
        if p.get("division", "").startswith(ntrp):
            return True
        if _sfx and (p.get(f"lines_played_{_sfx}") or p.get(f"wl_record_{_sfx}")):
            return True
        return False

    div_players = [p for p in players if _in_ntrp(p)]
    div_players.sort(
        key=lambda p: -(p.get(f"rating_{_sfx}") or
                        p.get("current_division_rating") or
                        p.get("dynamic_rating_baseline") or 0)
    )

    # ── Build per-player match histories from both divisions ──────────────────
    _pool = all_players_pool if all_players_pool else players
    # Detect ambiguous names in pool for state-qualified key lookups
    _name_counts_pool: dict[str, int] = {}
    for _pp in _pool:
        _nk = _nkey(_pp.get("name", ""))
        if _nk:
            _name_counts_pool[_nk] = _name_counts_pool.get(_nk, 0) + 1
    _ambiguous_pool = {k for k, c in _name_counts_pool.items() if c > 1}

    def _player_history_key(p: dict) -> str:
        """Key used to look up this player's history/stats (state-qualified if ambiguous)."""
        nk = _nkey(p.get("name", ""))
        if nk in _ambiguous_pool:
            st = (p.get("state") or "").lower()
            return f"{st}::{nk}" if st else nk
        return nk
    player_histories, player_stats = _traverse_match_histories(
        _pool, ntrp, subflights, other_subflights, state_code=state_code)

    # ── Rating timeline lookups (for point-in-time rating in match history) ──
    # Keyed nkey -> sfx -> {date: rating} (NOT flat-merged — see _ap_pit_rating)
    _tl_pre: dict[str, dict[str, dict[str, float]]] = {}
    _tl_post: dict[str, dict[str, dict[str, float]]] = {}
    _tl_base: dict[str, str] = {}
    _tl_final: dict[str, str] = {}
    _tl_lever: dict[str, float] = {}  # cached Lever-3 delta per player
    for p in _pool:
        norm = _nkey(p.get("name", ""))
        if not norm:
            continue
        # Keep timelines per-division (do NOT merge with .update() — same-date entries
        # from different divisions would overwrite each other, causing both same-day
        # cross-division matches to show the later division's "going in" value).
        for sfx in (_sfx, _other_sfx):
            tl = p.get(f"rating_timeline_{sfx}")
            if tl and isinstance(tl, dict):
                _tl_pre.setdefault(norm, {})[sfx] = {k: float(v) for k, v in tl.items()}
            ptl = p.get(f"rating_post_timeline_{sfx}")
            if ptl and isinstance(ptl, dict):
                _tl_post.setdefault(norm, {})[sfx] = {k: float(v) for k, v in ptl.items()}
        raw_base = p.get("dynamic_rating_baseline")
        if raw_base is not None:
            try:
                _tl_base[norm] = f"{float(raw_base):.2f}"
            except (ValueError, TypeError):
                pass
        for sfx_c in (_sfx, _other_sfx):
            raw_r = p.get(f"rating_{sfx_c}")
            if raw_r is not None:
                try:
                    _tl_final[norm] = f"{float(raw_r):.2f}"
                except (ValueError, TypeError):
                    pass

    def _lever3_delta(nkey: str) -> float:
        """Lever-3 shift delta for a player (cached).

        Lever 3 adds a constant to every timeline entry. We recover it from
        the very first pre-match entry (= Lever-3-shifted baseline) minus
        the stored baseline. All displayed timeline values are then de-levered
        so they sit on the same scale as the baseline.
        """
        if nkey in _tl_lever:
            return _tl_lever[nkey]
        delta = 0.0
        baseline_str = _tl_base.get(nkey) or _tl_final.get(nkey)
        if baseline_str:
            baseline_val = float(baseline_str)
            all_dates: set[str] = set()
            for sfx in ("30", "35"):
                all_dates.update(_tl_pre.get(nkey, {}).get(sfx, {}).keys())
            if all_dates:
                first_date = min(all_dates, key=_date_sort_key)
                p30 = _tl_pre.get(nkey, {}).get("30", {}).get(first_date)
                p35 = _tl_pre.get(nkey, {}).get("35", {}).get(first_date)
                q30 = _tl_post.get(nkey, {}).get("30", {}).get(first_date)
                q35 = _tl_post.get(nkey, {}).get("35", {}).get(first_date)
                # Identify the "un-chained" pre value = going-in to the first
                # ever match (not chained from same-day other-div output).
                if p30 is not None and p35 is not None:
                    # Both divs played on the same first date. The chained one
                    # equals the other div's same-day post.
                    if q35 is not None and abs(p30 - q35) < 0.0003:
                        first_pre = p35  # 3.5 ran first
                    else:
                        first_pre = p30  # 3.0 ran first (default)
                elif p30 is not None:
                    first_pre = p30
                elif p35 is not None:
                    first_pre = p35
                else:
                    first_pre = None
                if first_pre is not None:
                    delta = first_pre - baseline_val
        _tl_lever[nkey] = delta
        return delta

    def _ap_pit_rating(nkey: str, match_date: str, rec_sfx: str) -> str:
        """Pre-match rating for the focal player going into a specific match.

        All Lever-3-shifted timeline values are de-levered before display so
        the sequence sits on the same absolute scale as the stored baseline.
        """
        other_sfx = "35" if rec_sfx == "30" else "30"
        pre_this  = _tl_pre.get(nkey, {}).get(rec_sfx, {})
        post_this = _tl_post.get(nkey, {}).get(rec_sfx, {})
        post_other = _tl_post.get(nkey, {}).get(other_sfx, {})
        match_key = _date_sort_key(match_date)
        delta = _lever3_delta(nkey)

        if match_date in pre_this:
            pre_val = pre_this[match_date]

            # Any prior post-timeline entry (either div) before this date?
            has_prior = (
                any(_date_sort_key(k) < match_key for k in post_this) or
                any(_date_sort_key(k) < match_key for k in post_other)
            )

            if not has_prior:
                # No matches before today. Check same-day cross-div chain:
                # post_other[today] == pre_this[today] means other div ran first.
                other_post_today = post_other.get(match_date)
                other_ran_first = (
                    other_post_today is not None
                    and abs(other_post_today - pre_val) < 0.0003
                )
                if not other_ran_first:
                    # Absolute first match — show unshifted baseline.
                    return _tl_base.get(nkey, "") or _tl_final.get(nkey, "")

            return f"{pre_val - delta:.2f}"

        # No pre-timeline entry for this date: fall back to most recent prior
        # post-timeline across both divisions.
        all_prior: list[tuple[str, float]] = []
        for sfx2 in (rec_sfx, other_sfx):
            for k, v in _tl_post.get(nkey, {}).get(sfx2, {}).items():
                if _date_sort_key(k) < match_key:
                    all_prior.append((k, v))
        if all_prior:
            _, latest_v = max(all_prior, key=lambda kv: _date_sort_key(kv[0]))
            return f"{latest_v - delta:.2f}"
        return _tl_base.get(nkey, "") or _tl_final.get(nkey, "")

    # ── Build HTML rows ───────────────────────────────────────────────────────
    rows = ""
    for p in div_players:
        ntrp_r   = p.get("ntrp_rating", "") or ""
        baseline = p.get("dynamic_rating_baseline")
        curr     = p.get(f"rating_{_sfx}") or p.get("current_division_rating")
        division = p.get("division", "")
        nk = _nkey(p.get("name", ""))
        _hk = _player_history_key(p)  # key for player_histories / player_stats lookups

        pst = player_stats.get(_hk, {})
        _w_computed = pst.get("w", 0)
        _l_computed = pst.get("l", 0)
        _wko = pst.get("wko", 0)
        if _w_computed + _l_computed > 0:
            wl = f"{_w_computed}-{_l_computed}"
            if _wko:
                wl += "*"
        else:
            wl = p.get(f"wl_record_{_sfx}") or "–"

        div_team = (p.get(f"team_{_sfx}", "") or "") if _sfx else ""
        _primary_team = p.get("team", "") or ""
        sf = team_to_sf.get(div_team.upper(), "") or team_to_sf.get(_primary_team.upper(), "")
        sf_raw = team_to_sf_raw.get(div_team.upper(), "") or team_to_sf_raw.get(_primary_team.upper(), "")
        if not sf and division.startswith(ntrp) and division:
            _fallback = division.split()[-1]
            # Only use letter fallback when the SF display labels are actually letter-based
            # (e.g. NV "A"/"B"). For color-based labels (e.g. UT "Teal"/"Green"), this
            # would wrongly show "A" or "B" from the division registration field.
            _sf_col_labels = {v[1] for v in sf_display.values() if not v[2].lower().startswith("dist")}
            if _fallback in _sf_col_labels:
                sf = _fallback

        _wl_sort = "0"
        _wl_display = str(wl) if wl else "–"
        _wl_clean = _wl_display.rstrip("*")
        if "-" in _wl_clean and _wl_clean != "–":
            _wparts = _wl_clean.split("-")
            try:
                _w, _l = int(_wparts[0]), int(_wparts[1])
                _wl_sort = str(_w * 100 - _l)
            except (ValueError, IndexError):
                pass

        _diff_html, _diff_sort = _baseline_diff_span(curr, baseline)

        st = player_stats.get(_hk, {})
        sw = st.get(f"sw{_sfx}", 0); sl = st.get(f"sl{_sfx}", 0)
        gw = st.get(f"gw{_sfx}", 0); gl = st.get(f"gl{_sfx}", 0)
        sets_str  = f"{sw}–{sl}" if (sw + sl) else "–"
        games_str = f"{gw}–{gl}" if (gw + gl) else "–"
        sets_sort  = str(sw * 100 - sl) if (sw + sl) else "0"
        games_sort = str(gw * 100 - gl) if (gw + gl) else "0"
        _or_sum = st.get(f"or_sum{_sfx}", 0.0)
        _or_n   = st.get(f"or_n{_sfx}",  0)
        avg_opp_val  = _or_sum / _or_n if _or_n else None
        avg_opp_str  = f"{avg_opp_val:.2f}" if avg_opp_val else "–"
        avg_opp_sort = f"{avg_opp_val:.4f}" if avg_opp_val else "0"

        hist = player_histories.get(_hk, [])
        has_history = bool(hist)
        expand_cls = " expandable" if has_history else ""
        # Was THIS player disqualified in the division this page is for?
        # Scoped to `ntrp` only — her 3.5 row/matches must stay unmarked
        # even if she was DQ'd in 3.0, and vice versa.
        self_dq_this_div = any(
            rec.get("self_dq") and rec.get("div") == ntrp for rec in hist
        )

        # Build compact combined-record string for the history header.
        # Only show "X overall (A in 3.0, B in 3.5)" when player has matches
        # in BOTH divisions; single-division players just show "X–Y in 3.0".
        w_total = st.get("w", 0); l_total = st.get("l", 0)
        w30 = st.get("w30", 0); l30 = st.get("l30", 0)
        w35 = st.get("w35", 0); l35 = st.get("l35", 0)
        combined_parts = []
        if w30 + l30: combined_parts.append(f"{w30}–{l30} in 3.0")
        if w35 + l35: combined_parts.append(f"{w35}–{l35} in 3.5")
        if len(combined_parts) == 2:
            # Cross-division: add total sets + games to the summary line
            sw_tot = st.get("sw30", 0) + st.get("sw35", 0)
            sl_tot = st.get("sl30", 0) + st.get("sl35", 0)
            gw_tot = st.get("gw30", 0) + st.get("gw35", 0)
            gl_tot = st.get("gl30", 0) + st.get("gl35", 0)
            sets_tot  = f"{sw_tot}–{sl_tot}" if (sw_tot + sl_tot) else ""
            games_tot = f"{gw_tot}–{gl_tot}" if (gw_tot + gl_tot) else ""
            sg_str = "  ·  " + "  ".join(filter(None, [
                f"{sets_tot} sets"  if sets_tot  else "",
                f"{games_tot} games" if games_tot else "",
            ])) if (sets_tot or games_tot) else ""
            combined_str = f"{w_total}–{l_total} overall  ({', '.join(combined_parts)}){sg_str}"
        elif combined_parts:
            combined_str = combined_parts[0]   # e.g. "7–0 in 3.0"
        else:
            combined_str = f"{w_total}–{l_total}"

        # Embed match history as compact JSON on the row.
        # JS renders the detail pane on first click — keeps the DOM lean
        # and the HTML file small (vs pre-rendering hidden HTML for every player).
        import json as _json
        hist_json = ""
        if has_history:
            hist_compact = [
                {k: v for k, v in {
                    "dt": rec["date"],
                    "dv": rec["div"].replace(".", ""),
                    "ln": _abbrev_line(rec["line"]),
                    "w":  rec["won"],
                    "sc": rec["score"],
                    "wk": rec.get("wko", False) or None,
                    "pt": rec["partners"] or None,
                    "ptr": [r for r in rec["partner_r"] if r] or None,
                    "op": rec["opps"] or None,
                    "or": [r for r in rec["opp_r"] if r] or None,
                    "ot": _abbrev_team(rec.get("opp_team", "")) or None,
                    "pr": _ap_pit_rating(nk, rec["date"], rec["div"].replace(".", "")) or None,
                    "dq": rec.get("dq") or None,
                    "sdq": rec.get("self_dq") or None,
                }.items() if v is not None}
                for rec in hist
            ]
            hist_json = _json.dumps(hist_compact, separators=(",", ":"))

        p_state = p.get("state", "") or ""
        col3_val = p_state if is_sectionals else sf
        col3_html = (f"<td data-sort='{_esc(sf_raw or sf)}'><span class='sf-pill'>{_esc(col3_val)}</span></td>"
                     if not is_sectionals
                     else f"<td class='sortable-cell'>{_esc(col3_val)}</td>")
        _dq_row_cls = " player-row-dq" if self_dq_this_div else ""
        _dq_title = (f' title="Disqualified in {_esc(ntrp)} — see match history"'
                    if self_dq_this_div else "")
        _dq_name_badge = ' <span class="dq-badge">(DQ)</span>' if self_dq_this_div else ""
        rows += (
            f"<tr data-sf='{_esc(sf_raw or sf)}' data-pkey='{_esc(nk)}' data-state='{_esc(p_state)}'"
            f" class='player-row{expand_cls}{_dq_row_cls}'{_dq_title}"
            + (f" data-history='{hist_json.replace(chr(39), '&apos;')}'"
               f" data-combined='{_esc(combined_str)}'" if has_history else "")
            + f" onclick=\"toggleHistory(this)\">"
            f"<td class='pname'>{_esc(p.get('name',''))}{_dq_name_badge}</td>"
            f"<td>{_esc(_abbrev_team((_sfx and p.get(f'team_{_sfx}')) or (_primary_team if team_to_sf.get(_primary_team.upper()) else '') or ''))}</td>"
            f"{col3_html}"
            f"<td data-sort='{_esc(ntrp_r)}'>{_esc(ntrp_r)}</td>"
            f"<td>{_esc(_fmt_rating(baseline))}</td>"
            f"<td>{_rating_span(curr, baseline, ntrp_r)}</td>"
            f"<td data-sort='{_diff_sort}'>{_diff_html}</td>"
            f"<td data-sort='{_wl_sort}' style='white-space:nowrap'>{_esc(_wl_display)}</td>"
            f"<td data-sort='{sets_sort}' style='white-space:nowrap'>{sets_str}</td>"
            f"<td data-sort='{games_sort}' style='white-space:nowrap'>{games_str}</td>"
            f"<td data-sort='{avg_opp_sort}'>{avg_opp_str}</td>"
            f"</tr>\n"
        )

    # Build unique subflight raw labels for filter buttons (using display map for text)
    sf_raw_labels = []
    _seen = set()
    for sf_obj in (subflights or []):
        raw = sf_obj.get("flight_label", "")
        if raw and raw not in _seen:
            _seen.add(raw)
            sf_raw_labels.append(raw)

    state_btns = ""
    if is_sectionals:
        states_in_data = sorted({p.get("state", "") for p in players if p.get("state")})
        if len(states_in_data) > 1:
            sb = '<button class="rtab on" onclick="filterPlayerState(\'all\',this)">All States</button>'
            for s in states_in_data:
                sb += f'<button class="rtab" onclick="filterPlayerState(\'{_esc(s)}\',this)">{_esc(s)}</button>'
            state_btns = f'<div class="sf-filter-btns" id="state-filter-btns">{sb}</div>'

    sf_section = ""
    if not is_sectionals:
        sf_btns = '<button class="rtab on" onclick="filterPlayerSF(\'all\',this)">All</button>'
        prev_group = None
        for raw_lbl in sf_raw_labels:
            tab_lbl, _, group = sf_display.get(raw_lbl, (raw_lbl, raw_lbl, ""))
            if group and group != prev_group:
                sf_btns += (f'<div class="sf-break"></div>' if group.lower() == "districts" else '') + f'<span class="sf-group-label{"" if group.lower() != "districts" else " sf-group-inline"}">{_esc(group)}</span>'
                prev_group = group
            elif not group and prev_group:
                prev_group = None
            sf_btns += (f'<button class="rtab" onclick="filterPlayerSF(\'{_esc(raw_lbl)}\',this)">'
                        f'{_esc(tab_lbl)}</button>')
        sf_section = f'<div class="sf-filter-btns" id="sf-filter-btns">{sf_btns}</div>'

    col3_hdr = ('<th class="sortable" onclick="sortAP(2)">State ↕</th>'
                if is_sectionals else '<th class="sortable" onclick="sortAP(2)">SF ↕</th>')

    return f"""
<div class="ap-controls">
  <input type="text" id="player-search" placeholder="Filter by name or team…"
         oninput="filterPlayers()">
  {state_btns}
  {sf_section}
</div>
<table id="ap-table">
  <thead><tr>
    <th class="sortable" onclick="sortAP(0)">Player ↕</th>
    <th class="sortable" onclick="sortAP(1)">Team ↕</th>
    {col3_hdr}
    <th class="sortable" onclick="sortAP(3)">NTRP ↕</th>
    <th class="sortable" onclick="sortAP(4)">Base ↕</th>
    <th class="sortable" onclick="sortAP(5)">New ↕</th>
    <th class="sortable" onclick="sortAP(6)">Diff ↕</th>
    <th class="sortable" onclick="sortAP(7)">W–L ↕</th>
    <th class="sortable" onclick="sortAP(8)">Sets ↕</th>
    <th class="sortable" onclick="sortAP(9)">Games ↕</th>
    <th class="sortable" onclick="sortAP(10)">Avg Opp ↕</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p class="wl-footnote">* W–L excludes defaults/walkovers (opponent absent)</p>"""


def _champ_sort_key(raw_label: str) -> tuple:
    """Sort key for championship/districts subflight labels reflecting actual
    bracket chronology: the area-playoff round (per sub-area, e.g. DEN 1-9,
    SOCO 1) happens first, then Flight A/B/C/..., then any final round last
    (Final Rounds, '3.0W', generic 'Districts').
    """
    # Area-playoff round: "<AREACODE> <N>" where AREACODE is 2+ letters
    # (distinguishes it from the single-letter "Flight A" pattern below).
    m = re.search(r'\b([A-Za-z]{2,})\s*(\d+)\s*$', raw_label.strip(), re.IGNORECASE)
    if m:
        return (0, m.group(1).upper(), int(m.group(2)))
    m = re.search(r'\bFlight\s+([A-Za-z])\s*$', raw_label.strip(), re.IGNORECASE)
    if m:
        return (1, m.group(1).upper())
    return (2, raw_label)


def _results_tab(subflights: list[dict], players: list[dict] | None = None,
                 sfx: str = "", sf_display: dict | None = None,
                 id_prefix: str = "re", include_rating_toggle: bool = True) -> str:
    # Build name → baseline and name → new (division) rating lookups
    _baseline_by_name: dict[str, str] = {}
    _new_by_name: dict[str, str] = {}
    # name → {date: pre-match rating} — used for exact-date hits (active player display)
    _timeline_by_name: dict[str, dict[str, float]] = {}
    # name → {date: post-match rating} — used for prior-date fallback (opponent display)
    # Shows the player's rating AFTER their last played week, not before it.
    # Example: Shi's W7 upset win pushes her from 2.95 → 3.11; her W8 opponent
    # display should show 3.11 (post-W7), not 2.95 (pre-W7).
    _post_timeline_by_name: dict[str, dict[str, float]] = {}
    _team_by_name: dict[str, str] = {}
    if players:
        for p in players:
            norm = re.sub(r"\s+", " ", (p.get("name") or "").strip().lower())
            raw_base = p.get("dynamic_rating_baseline")
            if raw_base is not None:
                try:
                    _baseline_by_name[norm] = f"{float(raw_base):.2f}"
                except (ValueError, TypeError):
                    pass
            raw_new = p.get(f"rating_{sfx}") if sfx else None
            if raw_new is None:
                raw_new = p.get("current_division_rating")
            if raw_new is not None:
                try:
                    _new_by_name[norm] = f"{float(raw_new):.2f}"
                except (ValueError, TypeError):
                    pass
            # Pre-match timeline: rating going INTO each played date
            timeline = p.get(f"rating_timeline_{sfx}") if sfx else None
            if timeline and isinstance(timeline, dict):
                _timeline_by_name[norm] = {k: float(v) for k, v in timeline.items()}
            # Post-match timeline: rating AFTER all matches on each played date
            post_tl = p.get(f"rating_post_timeline_{sfx}") if sfx else None
            if post_tl and isinstance(post_tl, dict):
                _post_timeline_by_name[norm] = {k: float(v) for k, v in post_tl.items()}
            # For swap detection in *this division's* pages we MUST use the
            # division-specific team (team_30 or team_35). Falling back to the
            # primary `team` for cross-listed players gives wrong votes — e.g.
            # Melissa Hicks plays DESERT PALM in 3.0 but DTC #3 in 3.5, and on
            # the 3.5 page she needs to count as DTC #3 for swap detection.
            div_team = p.get(f"team_{sfx}") if sfx else None
            if div_team:
                _team_by_name[norm] = div_team
            elif p.get("team"):
                _team_by_name[norm] = p["team"]
            else:
                # Last resort: any other team field
                for _tf in ("team_30", "team_35"):
                    _tv = p.get(_tf)
                    if _tv:
                        _team_by_name[norm] = _tv
                        break

    def _pname_key(name: str) -> str:
        """Normalise a player name for data-pkey attribute."""
        return re.sub(r"[^a-z0-9]", "-", name.strip().lower())

    def _pit_rating(nkey: str, match_date: str) -> str:
        """Return the point-in-time rating for a player at match_date.

        Priority:
        1. Exact pre-match timeline entry for match_date
           → player played this date: show their rating going IN.
        2. Most recent POST-match entry strictly before match_date
           → player last played an earlier week: show their rating AFTER that week
             (e.g. Shi's W7 upset bumped her 2.95→3.11; show 3.11 in W8, not 2.95).
        3. Baseline (player hasn't played yet in this division)
        4. Final season rating (no timeline at all — opponents from other divisions)
        """
        # Exact hit on pre-match timeline (player is active this date)
        pre_tl = _timeline_by_name.get(nkey, {})
        if match_date in pre_tl:
            return f"{pre_tl[match_date]:.2f}"
        # Fallback: most recent POST-match entry before this date
        match_key = _date_sort_key(match_date)
        post_tl = _post_timeline_by_name.get(nkey, {})
        prior_post = [(k, v) for k, v in post_tl.items() if _date_sort_key(k) < match_key]
        if prior_post:
            _, latest_v = max(prior_post, key=lambda kv: _date_sort_key(kv[0]))
            return f"{latest_v:.2f}"
        # Pre-match fallback (older data without post-timeline)
        prior_pre = [(k, v) for k, v in pre_tl.items() if _date_sort_key(k) < match_key]
        if prior_pre:
            _, latest_v = max(prior_pre, key=lambda kv: _date_sort_key(kv[0]))
            return f"{latest_v:.2f}"
        # No prior matches → baseline
        base = _baseline_by_name.get(nkey, "")
        return base if base else _new_by_name.get(nkey, "")

    def _render_players(raw: str, match_date: str = "", dq_players: list | None = None) -> str:
        """Wrap each player name in a clickable span with rating badge (base + new).

        When match_date is provided and the player has a sequential rating timeline,
        data-new shows the player's rating going *into* that match (based on all
        prior matches in the division), not their final end-of-season rating.

        dq_players: names disqualified for this specific line — rendered with a
        concise "(DQ)" badge next to that player only, never their partner.
        """
        dq_keys = {re.sub(r"\s+", " ", n.lower()) for n in (dq_players or [])}
        parts = [p.strip() for p in raw.split("/")
                 if p.strip() and not _is_noise_name(p)]
        rendered = []
        for name in parts:
            nkey = re.sub(r"\s+", " ", name.lower())
            base_r = _baseline_by_name.get(nkey, "")
            # Point-in-time new rating via timeline lookup (falls back to baseline
            # if player hadn't played yet — avoids showing end-of-season rating
            # on default lines or weeks before their first actual match).
            if match_date:
                new_r = _pit_rating(nkey, match_date)
            else:
                new_r = _new_by_name.get(nkey, "")
            # Embed both ratings as data attributes; text defaults to new rating.
            # JS setResultRatingMode() swaps displayed text without re-rendering.
            if base_r or new_r:
                default_txt = new_r or base_r
                rating_html = (
                    f'<em class="prating" data-base="{base_r}" data-new="{new_r}">'
                    f'({default_txt})</em>'
                )
            else:
                rating_html = ""
            dq_badge = ' <span class="dq-badge">(DQ)</span>' if nkey in dq_keys else ""
            key = _pname_key(name)
            rendered.append(
                f'<span class="pname" data-pkey="{key}" '
                f'onclick="highlightPlayer(this)">{_esc(name)}{rating_html}</span>{dq_badge}'
            )
        if not rendered:
            if _is_noise_name(raw):
                return '<em class="hr">—</em>'
            return _esc(raw)
        return " / ".join(rendered)

    sf_display = sf_display or {}
    sf_labels = [sf.get("flight_label", str(i)) for i, sf in enumerate(subflights)]
    first_sf = sf_labels[0] if sf_labels else ""

    # Build date → week-label map (W1, W2, …) from all unique match dates
    # across every subflight, sorted chronologically.
    _all_dates: set[str] = set()
    for _sf in subflights:
        for _m in _sf.get("matches", []):
            d = _m.get("date", "")
            if d:
                _all_dates.add(d)
    _sorted_dates = sorted(_all_dates, key=_date_sort_key)
    _week_label: dict[str, str] = {d: f"W{i+1}" for i, d in enumerate(_sorted_dates)}

    sf_btns = ""
    prev_group = None
    for i, lbl in enumerate(sf_labels):
        tab_lbl, _, group = sf_display.get(lbl, (lbl, lbl, ""))
        active = " on" if i == 0 else ""
        if group and group != prev_group:
            sf_btns += (f'<div class="sf-break"></div>' if group.lower() == "districts" else '') + f'<span class="sf-group-label{"" if group.lower() != "districts" else " sf-group-inline"}">{_esc(group)}</span>\n'
            prev_group = group
        elif not group and prev_group:
            prev_group = None
        sf_btns += (
            f'<button class="rtab sf-switcher{active}" '
            f'data-sf="{_esc(lbl)}" '
            f'onclick="filterSF(\'{_esc(lbl)}\',this,\'{id_prefix}-sf-tabs\',\'{id_prefix}-tabs\',\'{id_prefix}\')">'
            f'{_esc(tab_lbl)}</button>\n'
        )

    team_tabs, rpanes = "", ""
    first_seen = True
    for sf in subflights:
        sf_lbl = sf.get("flight_label", "")
        matches = sf.get("matches", [])
        for t in sf.get("teams", []):
            tname = t.get("team_name", "")
            if not tname:
                continue
            tid = f"{id_prefix}-{_slug(sf_lbl)}-{_slug(tname)}"
            active = " on" if first_seen else ""
            visible = "" if sf_lbl == first_sf else ' style="display:none"'
            first_seen = False

            team_matches = _team_result_for(matches, tname)
            blocks = ""
            for m in team_matches:
                badge = _result_badge(m["won"], m["score"], m["pending"])
                wlabel = _week_label.get(m.get("date", ""), "")
                lines = m.get("lines", [])

                # For tied matches (e.g. 2-2), put the game count in the score column
                # on the same line as "LINE RESULTS". Our team's games are always first
                # (left) since the results tab always puts our team on the left.
                # Uses winner_team/loser_team — no orientation dependency.
                tie_html = ""
                if m.get("is_tie") and lines:
                    ga, gb = _tie_game_totals(lines, tname, m["opponent"])
                    if ga or gb:
                        our_cls = "score-win" if ga > gb else ("score-lose" if ga < gb else "")
                        our_num = f'<span class="{our_cls}">{ga}</span>' if our_cls else str(ga)
                        tie_html = (
                            f'<div class="line-row lbl-row">'
                            f'<span class="lbl-left">LINE RESULTS</span>'
                            f'<span class="ls">Games:&nbsp;{our_num}–{gb}</span>'
                            f'<span></span>'
                            f'</div>'
                        )

                blocks += (
                    f'<div class="mblock">'
                    f'<div class="mhdr">'
                    f'<span class="mweek-lbl">{_esc(wlabel)} {badge}</span>'
                    f'<span class="mtitle">vs {_esc(m["opponent"])}</span>'
                    f'</div>'
                    f'<div class="mdate">{_esc(m["date"])}</div>'
                )
                if lines:
                    if m.get("is_tie") and tie_html:
                        blocks += tie_html
                    else:
                        blocks += '<div class="line-lbl">line results</div>'
                    _m_ht = m.get("home_team", "")
                    _m_at = m.get("away_team", "")
                    for ln in lines:
                        ph_raw = ln.get("players_home", "")
                        pa_raw = ln.get("players_away", "")
                        _ln_dq = ln.get("dq_players") or []
                        def _default_or_render(raw, _mdate=m["date"], _dq=_ln_dq):
                            s = raw.strip()
                            if not s or s.upper() == "N/A":
                                return '<em class="default-marker">default</em>'
                            cleaned = [n.strip() for n in s.split("/")
                                       if n.strip() and not _is_noise_name(n)]
                            if not cleaned:
                                return '<em class="default-marker">default</em>'
                            return _render_players(raw, _mdate, _dq)

                        _cwt = (ln.get("winner_team") or "").strip() or None
                        if _cwt:
                            _our_team_won = _cwt.upper() == tname.upper()
                        else:
                            _our_team_won = None

                        # Always show selected team on left, opponent on right.
                        _ph_is_our_team = False
                        for _pn in [x.strip() for x in ph_raw.split("/")
                                    if x.strip() and not _is_noise_name(x)]:
                            _pt = _team_by_name.get(re.sub(r"\s+", " ", _pn.lower().strip()), "")
                            if _pt and _pt.upper() == tname.upper():
                                _ph_is_our_team = True
                                break

                        # Don't fall back to match-level win — that would
                        # highlight all courts the same color on a 3-2 match.

                        if _ph_is_our_team:
                            left_html = _default_or_render(ph_raw)
                            right_html = _default_or_render(pa_raw)
                        else:
                            left_html = _default_or_render(pa_raw)
                            right_html = _default_or_render(ph_raw)

                        sc = _esc(ln.get("score", ""))
                        lnum = _line_label_short(ln.get("line", ""))
                        lw = rw = ""
                        if _our_team_won is True:
                            lw = "w"
                        elif _our_team_won is False:
                            rw = "w"
                        _dq_cls = " line-row-dq" if _ln_dq else ""
                        _dq_title = f' title="Invalid match — {_esc(", ".join(_ln_dq))} disqualified"' if _ln_dq else ""
                        blocks += (
                            f'<div class="line-row{_dq_cls}"{_dq_title}>'
                            f'<span class="lh {lw}"><span class="lr-lbl">{_esc(lnum)}</span> {left_html}</span>'
                            f'<span class="ls">{sc}</span>'
                            f'<span class="la {rw}">{right_html}</span>'
                            f'</div>'
                        )
                blocks += "</div>\n"

            if not blocks:
                blocks = '<p class="muted">No results yet.</p>'

            team_tabs += (
                f'<button class="rtab{active}" data-sf="{_esc(sf_lbl)}"{visible} '
                f'onclick="sr(\'{tid}\',this,\'{id_prefix}-tabs\')">'
                f'{_esc(_abbrev_team(tname))}'
                f'</button>\n'
            )
            rpanes += (
                f'<div id="{tid}" class="rpane{active}">{blocks}</div>\n'
            )

    rating_toggle = (
        '<div class="re-rating-toggle">'
        '<span class="re-rtog-label">Ratings:</span>'
        '<button class="rtab" onclick="setResultRatingMode(\'none\',this)">None</button>'
        '<button class="rtab" onclick="setResultRatingMode(\'base\',this)">Base</button>'
        '<button class="rtab on" onclick="setResultRatingMode(\'new\',this)">New</button>'
        '</div>'
    ) if include_rating_toggle else ""
    return (
        rating_toggle
        + f'<div class="rtabs" id="{id_prefix}-sf-tabs">{sf_btns}</div>'
        + f'<div class="rtabs scrollable" id="{id_prefix}-tabs">{team_tabs}</div>'
        + rpanes
    )


# ---------------------------------------------------------------------------
# Summary cards
# ---------------------------------------------------------------------------

def _summary_cards(ntrp: str, year: int, subflights: list[dict],
                   region_label: str = "NV Area F") -> str:
    total_teams = sum(len(sf.get("teams", [])) for sf in subflights)
    total_matches = sum(len(sf.get("matches", [])) for sf in subflights)
    played = sum(
        1 for sf in subflights
        for m in sf.get("matches", [])
        if not m.get("pending")
    )

    # Leader = team with most wins across all subflights
    best_team, best_w, best_l = "–", 0, 0
    for sf in subflights:
        for t in sf.get("teams", []):
            w = t.get("team_wins") or 0
            if w > best_w:
                best_w, best_l = w, (t.get("team_losses") or 0)
                best_team = t.get("team_name", "–")

    # Latest completed match date
    dates = [
        m.get("date", "")
        for sf in subflights
        for m in sf.get("matches", [])
        if not m.get("pending") and m.get("date")
    ]
    latest = sorted(dates)[-1] if dates else "–"

    return f"""<div class="cards-row">
  <div class="mcard">
    <div class="mcard-label">division</div>
    <div class="mcard-val">{_esc(region_label)} {_esc(ntrp)}</div>
    <div class="mcard-sub">Women · {year}</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">teams</div>
    <div class="mcard-val">{total_teams}</div>
    <div class="mcard-sub">{played} of {total_matches} matches played</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">leader</div>
    <div class="mcard-val">{_esc(best_team)}</div>
    <div class="mcard-sub">{best_w}–{best_l} record</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">last played</div>
    <div class="mcard-val">{_esc(latest)}</div>
    <div class="mcard-sub">through week of {_esc(latest)}</div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       padding: 1rem; background: #fff; color: #222; font-size: 13px; }
/* Cards */
.cards-row { display: grid; grid-template-columns: repeat(4, minmax(0,1fr));
             gap: 10px; margin-bottom: 16px; }
.mcard { background: #f5f4f0; border-radius: 8px; padding: .75rem 1rem; }
.mcard-label { font-size: 11px; color: #888; margin-bottom: 3px; }
.mcard-val { font-size: 20px; font-weight: 600; }
.mcard-sub { font-size: 11px; color: #888; margin-top: 2px; }
/* Main tabs */
.tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
.tab { padding: 6px 14px; border: 1px solid #ccc; border-radius: 20px;
       font-size: 13px; background: transparent; color: #666;
       cursor: pointer; font-weight: 500; }
.tab.on { background: #eee; color: #222; font-weight: 600; border-color: #999; }
.pane { display: none; } .pane.on { display: block; }
/* Sub-tabs */
.rtabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; align-items: center; }
.rtabs.scrollable { overflow-x: auto; flex-wrap: nowrap; padding-bottom: 4px; }
.rtabs .sf-group-label { flex-basis: 100%; font-size: 10px; font-weight: 700; color: #888; text-transform: uppercase;
  letter-spacing: 0.5px; padding: 4px 2px 2px 8px; white-space: nowrap; }
.rtabs .sf-group-label.sf-group-inline { flex-basis: auto; }
.sf-break { flex-basis: 100%; height: 0; }
.re-rating-toggle { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
.re-rtog-label { font-size: 11px; color: #888; }
.rtab { padding: 4px 10px; border: 1px solid #ccc; border-radius: 20px;
        font-size: 11px; background: transparent; color: #666; cursor: pointer;
        white-space: nowrap; }
.rtab.on { background: #eee; color: #222; font-weight: 600; }
.rpane { display: none; } .rpane.on { display: block; }
/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 5px 6px; color: #888; font-weight: 600;
     border-bottom: 1px solid #eee; font-size: 11px; }
th.sortable { cursor: pointer; }
th.sortable:hover { color: #333; }
td { padding: 5px 6px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
tr:last-child td { border-bottom: none; }
.rank { color: #aaa; width: 1.5rem; }
.tname { font-weight: 500; max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
.pname { font-weight: 500; }
#ap-table td { vertical-align: middle; }
.st-table td:nth-child(n+4):nth-child(-n+6) { white-space: nowrap; font-size: 11px; }
/* Courts/Sets/Games split sub-columns */
.st-w { text-align: right; padding-right: 2px; padding-left: 6px; white-space: nowrap; font-size: 11px; width: 1.6rem; }
.st-l { text-align: right; padding-left: 2px; padding-right: 10px; white-space: nowrap; font-size: 11px; color: #aaa; width: 1.6rem; }
.st-table thead th[colspan] { text-align: center; }
.rpane table td:nth-child(6), #ap-table td:nth-child(7) { white-space: nowrap; }
.muted { color: #aaa; font-size: 11px; }
.wl-footnote { color: #999; font-size: 11px; margin-top: 0.4rem; padding-left: 0.3rem; }
/* Badges */
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px;
         font-size: 11px; font-weight: 600; white-space: nowrap; }
.bw { background: #EAF3DE; color: #27500A; }
.bl { background: #FCEBEB; color: #791F1F; }
.bn { background: #F1EFE8; color: #444; }
/* Ratings */
.ru { color: #27500A; font-weight: 600; }
.rd { color: #791F1F; }
.rn { color: #888; }
/* SF pill */
.sf-pill { display: inline-block; padding: 0 5px; border-radius: 8px;
           font-size: 10px; font-weight: 600; background: #e8edf5;
           color: #3a5a8c; vertical-align: middle; }
/* All-players table column constraints — fixed layout so expanding a row's
   match history (which inserts a new <tr>) never reflows/rewraps the
   columns of rows already on screen. Widths set on <th> (first row),
   which table-layout:fixed uses to size every column for the whole table. */
#ap-table { table-layout: fixed; width: 100%; }
#ap-table th { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#ap-table th:nth-child(1) { width: 15%; }
#ap-table th:nth-child(2) { width: 21%; }
#ap-table th:nth-child(3) { width: 7%; text-align: center; }
#ap-table th:nth-child(4) { width: 7%; }
#ap-table th:nth-child(5) { width: 7%; }
#ap-table th:nth-child(6) { width: 7%; }
#ap-table th:nth-child(7) { width: 7%; }
#ap-table th:nth-child(8) { width: 7%; }
#ap-table th:nth-child(9) { width: 7%; }
#ap-table th:nth-child(10) { width: 7%; }
#ap-table th:nth-child(11) { width: 8%; }
#ap-table td:nth-child(3) { text-align: center; }
/* All-players history expansion */
.player-row.expandable { cursor: pointer; }
.player-row.expandable:hover { background: #f5f7fa; }
.player-row.expanded > td:first-child::before { content: "▾ "; color: #888; font-size: 10px; }
.player-row:not(.expanded) > td:first-child::before { content: "▸ "; color: #ccc; font-size: 10px; }
.player-row:not(.expandable) > td:first-child::before { content: ""; }
.history-row td { background: #fafbfd; padding: 0; }
.history-wrap { padding: 8px 12px 12px 24px; }
.history-summary { font-size: 11px !important; font-weight: 600; color: #555; margin-bottom: 6px;
                   -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
.history-table { font-size: 11px; width: auto; min-width: 60%; }
.history-table th { font-size: 10px; padding: 3px 8px; }
.history-table td { padding: 3px 8px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }
.history-table tr:last-child td { border-bottom: none; }
/* Division pills in history */
.dp { display: inline-block; padding: 1px 6px; border-radius: 8px;
      font-size: 10px; font-weight: 700; white-space: nowrap; }
.dp30 { background: #ddeeff; color: #1a5a9a; }
.dp35 { background: #fff0d8; color: #8a5200; }
/* W/L in history */
.hw { color: #27500A; font-weight: 700; }
.hl { color: #791F1F; font-weight: 700; }
.hr { color: #aaa; font-style: normal; font-size: 10px; }
/* Search + SF filter row */
.ap-controls { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.ap-controls input { flex: 1; min-width: 160px; max-width: 320px; padding: 5px 10px;
  border: 1px solid #ddd; border-radius: 20px; font-size: 12px; }
.sf-filter-btns { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
.sf-group-label { flex-basis: 100%; font-size: 10px; font-weight: 700; color: #888; text-transform: uppercase;
  letter-spacing: 0.5px; padding: 4px 6px 2px; margin-left: 4px; white-space: nowrap; }
.sf-group-label.sf-group-inline { flex-basis: auto; }
/* Match blocks */
.mblock { border: 1px solid #eee; border-radius: 8px;
          padding: .75rem 1rem; margin-bottom: 10px; }
.mhdr { display: flex; justify-content: space-between;
        align-items: center; margin-bottom: 4px; }
.mtitle { font-size: 13px; font-weight: 600; flex: 0 0 auto;
          text-align: right; color: #555; }
.mweek-lbl { font-size: 11px; font-weight: 700; color: #888;
             letter-spacing: .04em; text-align: left; flex: 1;
             padding: 0 8px 0 0; }
.mdate { font-size: 11px; color: #888; margin-bottom: 6px; }
.lbl-row { margin: 8px 0 3px; }
.lbl-left { font-size: 10px; font-weight: 600; color: #aaa;
            text-transform: uppercase; letter-spacing: .05em; }
.score-win { color: #27500A; font-weight: 700; }
.score-lose { color: #c0392b; font-weight: 700; }
.line-lbl { font-size: 10px; font-weight: 600; color: #aaa;
            text-transform: uppercase; letter-spacing: .05em; margin: 8px 0 3px; }
.line-row { display: grid; grid-template-columns: 1fr auto 1fr;
            gap: 2px 8px; font-size: 12px; align-items: center; padding: 2px 0; }
.lh { color: #aaa; } .lh.w { color: #27500A; font-weight: 600; }
.la { color: #aaa; text-align: right; } .la.w { color: #27500A; font-weight: 600; }
.ls { text-align: center; font-weight: 600; font-size: 11px; color: #aaa; }
/* A court where a player was disqualified — the result is on record but not
   a valid competitive match. Italicize the whole line as a visual flag;
   the (DQ) badge marks exactly who. */
.line-row-dq { font-style: italic; cursor: help; }
.line-row-dq .lh, .line-row-dq .la { opacity: 0.85; }
.dq-badge { font-size: 10px; font-weight: 700; color: #b02a2a;
            font-style: normal; letter-spacing: .02em; }
/* All Players match history — whole-row opacity so every element (div/W-L
   badges, pills, text) dims together proportionally, rather than fixed
   text-color leaving bright badges untouched. */
.hist-row-dq { font-style: italic; opacity: 0.5; }
/* Player disqualified in this division — mark the whole roster/all-players row */
.player-row-dq { background: #fdf1f1; }
.player-row-dq:hover { background: #fbe4e4; }
/* Clickable player names in results tab */
.pname { cursor: pointer; border-radius: 3px; padding: 0 2px; transition: background .1s; }
.pname:hover { background: #f0f4ff; }
.pname.phi { background: #fff3b0; outline: 1px solid #e6c800; }
.prating { font-size: 10px; color: #aaa; font-style: normal; font-weight: 400; margin-left: 2px; }
.lh.w .prating, .la.w .prating { color: #5a8a2a; }
/* Section title */
.sec-title { font-size: 11px; font-weight: 600; color: #888;
             text-transform: uppercase; letter-spacing: .07em; margin: 0 0 8px; }
/* Standings subflight header */
.sf-header { font-size: 12px; font-weight: 700; color: #555;
             text-transform: uppercase; letter-spacing: .06em;
             margin: 18px 0 6px; padding-bottom: 4px;
             border-bottom: 2px solid #e8e8e8; }
.sf-header:first-child { margin-top: 0; }
.st-table { margin-bottom: 6px; }
/* Validation warning */
.warn-box { background: #fffbe6; border: 1px solid #f0c040;
            border-radius: 6px; padding: .6rem .9rem;
            font-size: 12px; margin-bottom: 10px; }
.warn-box ul { margin: 4px 0 0 1.2em; }
.warn-box li { margin: 2px 0; }
/* Line court pills */
.line-pill { display: inline-block; padding: 1px 6px; border-radius: 8px;
             font-size: 10px; font-weight: 700; margin-right: 3px;
             letter-spacing: .02em; }
.pill-s1 { background: #FDEBD0; color: #A04000; }
.pill-s2 { background: #FAD7A0; color: #784212; }
.pill-s3 { background: #F9E79F; color: #7D6608; }
.pill-d1 { background: #D6EAF8; color: #1A5276; }
.pill-d2 { background: #D5F5E3; color: #1E8449; }
.pill-d3 { background: #E8DAEF; color: #6C3483; }
/* Line label in line-row */
.lr-lbl { display: inline-block; font-size: 10px; font-weight: 700;
          color: #aaa; min-width: 20px; }
/* Standings clickable team / record links */
.team-link { color: inherit; text-decoration: none; }
.team-link:hover { color: #1a5276; text-decoration: underline; cursor: pointer; }
/* Cross-dashboard link */
.top-bar { text-align: right; margin-bottom: 8px; }
.cross-link { font-size: 12px; color: #1a5276; text-decoration: none; font-weight: 500; }
.cross-link:hover { text-decoration: underline; }
/* Row hover highlight */
.st-table tbody tr:hover, .rpane table tbody tr:hover, #ap-table tbody tr:hover { background: #f5f7fa; }
/* Global diff column */
.gdiff-up   { font-size: 10px; color: #5a8a2a; font-weight: 500; }
.gdiff-dn   { font-size: 10px; color: #a04000; font-weight: 500; }
.gdiff-zero { font-size: 10px; color: #999; }
/* Notes column */
.notes-cell { font-size: 10px; color: #555; line-height: 1.35; min-width: 180px; max-width: 320px; }
/* Walkover/default win badge in roster notes */
.default-win-badge { display: inline-block; font-size: 9px; color: #888; background: #f3f3f3;
  border: 1px solid #ddd; border-radius: 3px; padding: 1px 4px; margin-top: 2px; font-style: italic; }
/* Default marker in results tab */
.default-marker { color: #999; font-style: italic; font-size: 11px; }
/* Subflight summary in standings */
.sf-summary { font-size: 12px; color: #444; line-height: 1.55; margin: 0 0 12px 0;
              padding: 10px 12px; background: #f9f9f9; border-left: 3px solid #d0d0d0; border-radius: 4px; }
/* Analysis + Predictions tab */
.insight { padding: 8px 0; border-bottom: 1px solid #f0f0f0; font-size: 12px; line-height: 1.6; }
.insight:last-child { border-bottom: none; }
.itag { display: inline-block; font-size: 10px; font-weight: 600; padding: 1px 6px;
        border-radius: 8px; margin-right: 5px; background: #E6F1FB; color: #0C447C; }
.itag.you { background: #EAF3DE; color: #27500A; }
.itag.warn { background: #FAEEDA; color: #633806; }
.itag.opp { background: #EEEDFE; color: #3C3489; }
/* Sectionals match-results: State -> district-subflight -> team hierarchy */
.state-tabs { display: flex; gap: 6px; margin-bottom: 14px; }
.state-tab { font-size: 14px; font-weight: 700; padding: 8px 18px;
             border: 1px solid #d5d5d5; border-radius: 8px; background: #fff;
             color: #444; cursor: pointer; }
.state-tab.on { background: #1a5fb4; color: #fff; border-color: #1a5fb4; }
.state-pane { display: none; }
.state-pane.on { display: block; }
"""

_JS = """
function sw(id, btn) {
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('on'));
  document.getElementById(id).classList.add('on');
  btn.classList.add('on');
}
function swState(id, btn, group) {
  group = group || 'results';
  var grp = btn.closest('.state-tabs');
  if (grp) grp.querySelectorAll('.state-tab').forEach(b => b.classList.remove('on'));
  // Scoped to this group's panes only — a second state-tab group elsewhere
  // on the page (e.g. a "standings" tab alongside "match results") must not
  // hide/show each other's panes when switching states independently.
  document.querySelectorAll('.state-pane[data-group="' + group + '"]').forEach(p => p.classList.remove('on'));
  document.getElementById(id).classList.add('on');
  btn.classList.add('on');
}
function sr(id, btn, groupId) {
  // Deactivate all tabs in the group
  var grp = document.getElementById(groupId);
  if (grp) grp.querySelectorAll('.rtab').forEach(b => b.classList.remove('on'));
  // Hide all rpanes that share the same prefix (e.g. "ro-" or "re-")
  var prefix = id.split('-')[0] + '-';
  document.querySelectorAll('[id^="' + prefix + '"]').forEach(p => {
    if (p.classList.contains('rpane')) p.classList.remove('on');
  });
  document.getElementById(id).classList.add('on');
  if (btn) btn.classList.add('on');
}

// Switch A/B subflight in rosters or results tab
// Simple subflight switcher for the standings tab (no team sub-tabs)
function filterStandingsSF(sf, prefix) {
  prefix = prefix || 'st';
  var tabs = document.getElementById(prefix + '-sf-tabs');
  if (tabs) {
    tabs.querySelectorAll('.rtab').forEach(function(b) {
      b.classList.toggle('on', b.dataset.sf === sf);
    });
  }
  document.querySelectorAll('.st-pane[data-prefix="' + prefix + '"]').forEach(function(p) {
    p.style.display = (p.dataset.sf === sf) ? '' : 'none';
  });
}

function filterSF(sf, btn, sfTabsId, teamTabsId, prefix) {
  // Update SF tab highlight
  var sfTabs = document.getElementById(sfTabsId);
  if (sfTabs) sfTabs.querySelectorAll('.rtab').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');

  // Show/hide team tabs based on data-sf
  var teamTabs = document.getElementById(teamTabsId);
  var firstVisible = null;
  if (teamTabs) {
    teamTabs.querySelectorAll('.rtab').forEach(b => {
      if (b.dataset.sf === sf) {
        b.style.display = '';
        if (!firstVisible) firstVisible = b;
      } else {
        b.style.display = 'none';
        b.classList.remove('on');
      }
    });
  }

  // Hide all rpanes for this prefix, then show the first visible team's pane
  document.querySelectorAll('[id^="' + prefix + '-"]').forEach(p => {
    if (p.classList.contains('rpane')) p.classList.remove('on');
  });
  if (firstVisible) {
    firstVisible.classList.add('on');
    var targetId = firstVisible.getAttribute('onclick').match(/'([^']+)'/)[1];
    var pane = document.getElementById(targetId);
    if (pane) pane.classList.add('on');
  }
}

// Highlight all occurrences of a player name in the results tab
function setResultRatingMode(mode, btn) {
  // Update toggle button state
  document.querySelectorAll('.re-rating-toggle .rtab').forEach(function(b) {
    b.classList.remove('on');
  });
  btn.classList.add('on');
  // Update every rating badge in the results pane
  document.querySelectorAll('.rpane .prating').forEach(function(em) {
    if (mode === 'none') {
      em.style.display = 'none';
    } else {
      em.style.display = '';
      var val = mode === 'base' ? em.dataset.base : em.dataset.new;
      em.textContent = val ? '(' + val + ')' : '';
    }
  });
}

function highlightPlayer(el) {
  var key = el.dataset.pkey;
  if (!key) return;
  var already = el.classList.contains('phi');
  // Clear all highlights first
  document.querySelectorAll('.pname.phi').forEach(function(n) { n.classList.remove('phi'); });
  if (!already) {
    document.querySelectorAll('.pname[data-pkey="' + key + '"]').forEach(function(n) {
      n.classList.add('phi');
    });
  }
}

var _apSF = 'all', _apState = 'all';
function filterPlayerSF(sf, btn) {
  _apSF = sf;
  document.querySelectorAll('#sf-filter-btns .rtab').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  applyPlayerFilters();
}
function filterPlayerState(st, btn) {
  _apState = st;
  document.querySelectorAll('#state-filter-btns .rtab').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  applyPlayerFilters();
}
var _filterTimer;
function filterPlayers() {
  clearTimeout(_filterTimer);
  _filterTimer = setTimeout(applyPlayerFilters, 120);
}
function toggleHistory(tr) {
  if (!tr.classList.contains('expandable')) return;
  var open = tr.classList.toggle('expanded');
  var next = tr.nextElementSibling;
  // First click: render the detail row from JSON, then insert it
  if (!next || !next.classList.contains('history-row')) {
    var data = JSON.parse(tr.dataset.history || '[]');
    var combined = tr.dataset.combined || '';
    var html = '<div class="history-wrap"><div class="history-summary">' + combined + '</div>'
      + '<table class="history-table"><thead><tr>'
      + '<th>Date</th><th>Div</th><th>Line</th><th>Rating</th><th>Result</th>'
      + '<th>Score</th><th>Partner(s)</th><th>Opponent(s)</th><th>Opp Team</th>'
      + '</tr></thead><tbody>';
    data.forEach(function(r) {
      var dv = r.dv || '';
      var divPill = '<span class="dp dp' + dv + '">' + (dv === '30' ? '3.0' : '3.5') + '</span>';
      var wko = r.wk;
      var resCls = r.w ? 'hw' : (wko ? '' : 'hl');
      var resTxt = wko ? (r.w ? 'W*' : 'L*') : (r.w ? 'W' : 'L');
      var dqArr = r.dq || [];
      var dqBadge = function(name) {
        return dqArr.indexOf(name) !== -1 ? ' <span class="dq-badge">(DQ)</span>' : '';
      };
      var opArr = r.op || [], orArr = r.or || [];
      var opHtml = opArr.length
        ? opArr.map(function(o,i){ return o + dqBadge(o) + (orArr[i] ? '<em class="hr"> ('+orArr[i]+')</em>' : ''); }).join(' / ')
        : '<em class="hr">default</em>';
      var ptArr = r.pt || [], ptrArr = r.ptr || [];
      var ptHtml = ptArr.length
        ? ptArr.map(function(p,i){ return p + dqBadge(p) + (ptrArr[i] ? '<em class="hr"> ('+ptrArr[i]+')</em>' : ''); }).join(', ')
        : '—';
      var sc = wko ? '<em class="hr">default</em>' : (r.sc || '');
      var prHtml = r.pr ? '<em class="prating">(' + r.pr + ')</em>' : '';
      var rowCls = dqArr.length ? ' class="hist-row-dq" title="Invalid match — ' + dqArr.join(', ') + ' disqualified"' : '';
      html += '<tr' + rowCls + '>'
        + '<td style="white-space:nowrap">' + (r.dt||'') + '</td>'
        + '<td>' + divPill + '</td>'
        + '<td>' + (r.ln||'') + '</td>'
        + '<td>' + prHtml + '</td>'
        + '<td><span class="' + resCls + '">' + resTxt + '</span></td>'
        + '<td style="white-space:nowrap">' + sc + '</td>'
        + '<td>' + ptHtml + '</td>'
        + '<td>' + opHtml + '</td>'
        + '<td>' + (r.ot||'') + '</td>'
        + '</tr>';
    });
    html += '</tbody></table></div>';
    var htr = document.createElement('tr');
    htr.className = 'history-row';
    var td = document.createElement('td');
    td.colSpan = 11; td.style.padding = '0';
    td.innerHTML = html;
    htr.appendChild(td);
    tr.parentNode.insertBefore(htr, tr.nextSibling);
    next = htr;
  }
  next.style.display = open ? '' : 'none';
}
function applyPlayerFilters() {
  var q = (document.getElementById('player-search') || {value:''}).value.toLowerCase();
  var rows = document.querySelectorAll('#ap-table tbody tr.player-row');
  for (var i = 0; i < rows.length; i++) {
    var tr = rows[i];
    var sfMatch = _apSF === 'all' || tr.dataset.sf === _apSF;
    var stMatch = _apState === 'all' || tr.dataset.state === _apState;
    var textMatch = true;
    if (q) {
      if (!tr.dataset.search) {
        var c = tr.cells;
        tr.dataset.search = ((c[0]||{}).textContent + ' ' + (c[1]||{}).textContent).toLowerCase();
      }
      textMatch = tr.dataset.search.includes(q);
    }
    var show = sfMatch && stMatch && textMatch;
    tr.style.display = show ? '' : 'none';
    var next = tr.nextElementSibling;
    if (next && next.classList.contains('history-row')) {
      if (!show) next.style.display = 'none';
    }
  }
}
var _sortDir = {};
function _sortTable(tbodyOrSelector, col, dirKey) {
  var tbody = typeof tbodyOrSelector === 'string'
    ? document.querySelector(tbodyOrSelector)
    : tbodyOrSelector;
  if (!tbody) return;
  var rows = Array.from(tbody.querySelectorAll('tr'));
  var dir = (_sortDir[dirKey] = !_sortDir[dirKey]);
  rows.sort(function(a, b) {
    var ac = a.cells[col], bc = b.cells[col];
    // Prefer data-sort numeric attribute when present (e.g. W-L cells)
    var ads = ac ? ac.dataset.sort : undefined;
    var bds = bc ? bc.dataset.sort : undefined;
    if (ads !== undefined && bds !== undefined) {
      var an2 = parseFloat(ads), bn2 = parseFloat(bds);
      if (!isNaN(an2) && !isNaN(bn2)) return dir ? an2 - bn2 : bn2 - an2;
    }
    var av = ac ? ac.innerText.trim() : '';
    var bv = bc ? bc.innerText.trim() : '';
    var an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return dir ? an - bn : bn - an;
    return dir ? av.localeCompare(bv) : bv.localeCompare(av);
  });
  rows.forEach(r => tbody.appendChild(r));
}
function sortAP(col) {
  var tbody = document.querySelector('#ap-table tbody');
  if (!tbody) return;
  var dir = (_sortDir['ap-' + col] = !_sortDir['ap-' + col]);
  // Collect only player rows (not history rows), paired with their history row
  var pairs = [];
  var rows = Array.from(tbody.children);
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].classList.contains('player-row')) {
      var hist = (i + 1 < rows.length && rows[i+1].classList.contains('history-row'))
        ? rows[i+1] : null;
      pairs.push([rows[i], hist]);
    }
  }
  pairs.sort(function(a, b) {
    var ac = a[0].cells[col], bc = b[0].cells[col];
    var ads = ac ? ac.dataset.sort : undefined;
    var bds = bc ? bc.dataset.sort : undefined;
    if (ads !== undefined && bds !== undefined) {
      var an2 = parseFloat(ads), bn2 = parseFloat(bds);
      if (!isNaN(an2) && !isNaN(bn2)) return dir ? an2 - bn2 : bn2 - an2;
    }
    var av = ac ? ac.innerText.trim() : '';
    var bv = bc ? bc.innerText.trim() : '';
    var an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return dir ? an - bn : bn - an;
    return dir ? av.localeCompare(bv) : bv.localeCompare(av);
  });
  pairs.forEach(function(pair) {
    tbody.appendChild(pair[0]);
    if (pair[1]) tbody.appendChild(pair[1]);
  });
}
function sortRoster(col) {
  // Sort the currently visible roster pane's table
  var pane = document.querySelector('.rpane.on[id^="ro-"]') || document.querySelector('.rpane.on[id^="sect-ro-"]');
  if (!pane) return;
  var tbody = pane.querySelector('tbody');
  _sortTable(tbody, col, 'ro-' + col + '-' + (pane ? pane.id : ''));
}

// Navigate from standings → team rosters tab for a specific team
function goToRoster(slug, sf) {
  // Switch to Team Rosters main tab
  document.querySelectorAll('.pane').forEach(function(p) { p.classList.remove('on'); });
  document.querySelectorAll('.tab').forEach(function(b) {
    b.classList.remove('on');
    if (b.textContent.trim().toLowerCase() === 'team rosters') b.classList.add('on');
  });
  var rPane = document.getElementById('rosters');
  if (rPane) rPane.classList.add('on');
  // Switch subflight
  if (sf) {
    var sfBtn = document.querySelector('#ro-sf-tabs .rtab[data-sf="' + sf + '"]');
    if (sfBtn) filterSF(sf, sfBtn, 'ro-sf-tabs', 'ro-tabs', 'ro');
  }
  // Activate the team pane
  setTimeout(function() {
    var targetId = 'ro-' + slug;
    var pane = document.getElementById(targetId);
    if (!pane) return;
    document.querySelectorAll('[id^="ro-"].rpane').forEach(function(p) { p.classList.remove('on'); });
    pane.classList.add('on');
    document.querySelectorAll('#ro-tabs .rtab').forEach(function(b) {
      b.classList.remove('on');
      var oc = b.getAttribute('onclick') || '';
      if (oc.indexOf(targetId) >= 0) b.classList.add('on');
    });
  }, 40);
}

// Navigate from standings → all results tab for a specific team
function goToResult(slug, sf) {
  // Switch to All Results main tab
  document.querySelectorAll('.pane').forEach(function(p) { p.classList.remove('on'); });
  document.querySelectorAll('.tab').forEach(function(b) {
    b.classList.remove('on');
    if (b.textContent.trim().toLowerCase() === 'all results') b.classList.add('on');
  });
  var rPane = document.getElementById('allresults');
  if (rPane) rPane.classList.add('on');
  // Switch subflight
  if (sf) {
    var sfBtn = document.querySelector('#re-sf-tabs .rtab[data-sf="' + sf + '"]');
    if (sfBtn) filterSF(sf, sfBtn, 're-sf-tabs', 're-tabs', 're');
  }
  // Activate the team pane
  setTimeout(function() {
    var targetId = 're-' + slug;
    var pane = document.getElementById(targetId);
    if (!pane) return;
    document.querySelectorAll('[id^="re-"].rpane').forEach(function(p) { p.classList.remove('on'); });
    pane.classList.add('on');
    document.querySelectorAll('#re-tabs .rtab').forEach(function(b) {
      b.classList.remove('on');
      var oc = b.getAttribute('onclick') || '';
      if (oc.indexOf(targetId) >= 0) b.classList.add('on');
    });
  }, 40);
}
// All-players table: cols 3-9 (NTRP, Base, New, Diff, W-L, Sets, Games) default descending
[3,4,5,6,7,8,9,10].forEach(function(c) { _sortDir['ap-' + c] = true; });
"""


ANALYSIS_30 = DATA_DIR / "analysis_30.json"
ANALYSIS_35 = DATA_DIR / "analysis_35.json"


# ---------------------------------------------------------------------------
# Score descriptor + win probability  (matchup page helpers)
# ---------------------------------------------------------------------------

def _score_desc_short(score: str) -> str:
    """Short descriptive label for a score string (used in matchup detail rows)."""
    if not score:
        return ""
    sets = re.findall(r"(\d+)-(\d+)", score)
    if not sets:
        return ""
    has_tb = any((a, b) in [("1", "0"), ("0", "1")] for a, b in sets)
    if len(sets) >= 3 and has_tb:
        return "3-set TB"
    regular = [(int(a), int(b)) for a, b in sets
               if not (int(a) <= 1 and int(b) <= 1 and (int(a) + int(b)) <= 1)]
    if not regular:
        return ""
    mc = min(min(a, b) for a, b in regular)
    if mc == 0:
        return "lopsided"
    if mc <= 1:
        return "dominant"
    if mc <= 2:
        return "dominant"
    if mc == 3:
        return "clear"
    return "tight"


_MX_WIN_PROB_STEPS = [
    (0.40, 0.82), (0.30, 0.75), (0.20, 0.68), (0.10, 0.58), (0.00, 0.50),
    (-0.10, 0.42), (-0.20, 0.32), (-0.30, 0.25), (-0.40, 0.18),
]
_MX_WIN_PROB_FLOOR = 0.12


def _win_prob_gap(gap: float) -> float:
    """Interpolated win probability from rating gap (player − opponent)."""
    gap = round(gap, 4)
    if gap >= _MX_WIN_PROB_STEPS[0][0]:
        return _MX_WIN_PROB_STEPS[0][1]
    if gap < _MX_WIN_PROB_STEPS[-1][0]:
        return _MX_WIN_PROB_FLOOR
    for i in range(len(_MX_WIN_PROB_STEPS) - 1):
        hi_gap, hi_prob = _MX_WIN_PROB_STEPS[i]
        lo_gap, lo_prob = _MX_WIN_PROB_STEPS[i + 1]
        if gap >= lo_gap:
            frac = (gap - lo_gap) / (hi_gap - lo_gap) if hi_gap != lo_gap else 0
            return lo_prob + frac * (hi_prob - lo_prob)
    return _MX_WIN_PROB_FLOOR


def _analysis_tab(ntrp: str) -> str:
    """
    Build the Analysis + Predictions tab from a JSON file.

    Expected JSON structure:
    {
      "sections": [
        {
          "title": "key findings — updated after W4",
          "insights": [
            {"tag": "your team", "tag_class": "you", "content": "Shi + Darian ..."},
            {"tag": "warning", "tag_class": "warn", "content": "LTF next week ..."},
            {"tag": "opponent", "tag_class": "opp", "content": "Red Rock CC #2 ..."}
          ]
        },
        {
          "title": "04/18 — Life Time Fitness/GV",
          "insights": [
            {"tag": "lineup", "tag_class": "", "content": "Predicted lineup: ..."}
          ]
        }
      ]
    }
    """
    path = ANALYSIS_35 if "3.5" in ntrp else ANALYSIS_30
    data = _load(path, {})
    sections = data.get("sections", [])

    if not sections:
        return (
            '<p class="muted" style="padding:1rem">'
            'No analysis content yet. Create '
            f'<code>{path.name}</code> to add insights and predictions.</p>'
        )

    html = ""
    for sec in sections:
        title = sec.get("title", "")
        if title:
            html += f'<p class="sec-title">{_esc(title)}</p>'
        for ins in sec.get("insights", []):
            tag = _esc(ins.get("tag", ""))
            tag_cls = _esc(ins.get("tag_class", ""))
            content = ins.get("content", "")   # allow HTML in content
            html += (
                f'<div class="insight">'
                f'<span class="itag {tag_cls}">{tag}</span>'
                f'{content}'
                f'</div>'
            )

    return html


def _generate_html(ntrp: str, standings: dict, players: list[dict],
                   other_standings: dict = None,
                   state_code: str = "NV",
                   region_label: str = "NV Area F") -> str:
    year = standings.get("year", "")
    subflights = standings.get("subflights", [])

    warnings = _validate(subflights)
    if warnings:
        for w in warnings:
            print(f"  [VALIDATION] {w}")

    # Scope to this exact state+division so each mismatched player is
    # flagged once, on the page where they'd actually appear.
    _ntrp_scope = [p for p in players
                   if p.get("state") == state_code and p.get("division", "").startswith(ntrp)]
    for w in _validate_ntrp(_ntrp_scope):
        print(f"  [VALIDATION] {w}")

    sf_display = _simplify_subflight_labels(subflights, ntrp)

    # Sort subflights: regular A→Z first, then championships/districts —
    # Flight A, Flight B, Flight C, ... in letter order, with any non-lettered
    # championship flight (Final Rounds, 3.0W, generic Districts) sorted last.
    def _sf_sort_key(sf):
        lbl = sf.get("flight_label", "")
        _, col, group = sf_display.get(lbl, ("zzz", "zzz", ""))
        is_champ = 1 if group.lower() == "districts" else 0
        if is_champ:
            return (is_champ, *_champ_sort_key(lbl))
        return (is_champ, col)
    subflights = sorted(subflights, key=_sf_sort_key)

    # Filter players to this state only for per-state dashboards
    state_players = [p for p in players if p.get("state") == state_code]
    # But keep ALL players available for match history cross-references
    all_players_pool = players

    cards_html = _summary_cards(ntrp, year, subflights, region_label)
    standings_html = _standings_tab(subflights, warnings, sf_display=sf_display)
    rosters_html = _rosters_tab(subflights, state_players, ntrp, sf_display=sf_display)
    other_subflights = (other_standings or {}).get("subflights", [])
    players_html = _players_tab(state_players, ntrp, subflights, other_subflights,
                                all_players_pool=all_players_pool, sf_display=sf_display,
                                state_code=state_code)
    results_html = _results_tab(subflights, state_players, sfx=ntrp.replace(".", ""),
                                sf_display=sf_display)

    tab_defs = [
        ("standings",  "standings",    standings_html),
        ("rosters",    "team rosters", rosters_html),
        ("allplayers", "all players",  players_html),
        ("allresults", "all results",  results_html),
    ]

    # Only include analysis tab for NV (the only state with AI-generated analysis)
    if state_code == "NV":
        analysis_html = _analysis_tab(ntrp)
        tab_defs.append(("analysis", "analysis + predictions", analysis_html))

    tab_btns = "".join(
        f'<button class="tab{" on" if i==0 else ""}" '
        f'onclick="sw(\'{tid}\',this)">{_esc(lbl)}</button>\n'
        for i, (tid, lbl, _) in enumerate(tab_defs)
    )
    tab_panes = "".join(
        f'<div id="{tid}" class="pane{" on" if i==0 else ""}">{html}</div>\n'
        for i, (tid, _, html) in enumerate(tab_defs)
    )

    # Cross-dashboard link + matchups link
    other_ntrp = "3.5" if ntrp == "3.0" else "3.0"
    st_lower = state_code.lower()
    other_file = f"women_{st_lower}_{other_ntrp.replace('.', '')}.html"
    mx_file = f"matchups_{st_lower}_{ntrp.replace('.', '')}.html"
    cross_link = (
        f'<a href="index.html" class="cross-link">← All States</a>'
        f' &nbsp;|&nbsp; '
        f'<a href="{other_file}" class="cross-link">'
        f'Switch to {_esc(other_ntrp)} Women →</a>'
        f' &nbsp;|&nbsp; '
        f'<a href="{mx_file}" class="cross-link">Singles &amp; Doubles Explorer →</a>'
        f' &nbsp;|&nbsp; '
        f'<a href="sectionals_30.html" class="cross-link">Sectionals →</a>'
    )

    n_matches = sum(len(sf.get("matches", [])) for sf in subflights)
    n_pending = sum(1 for sf in subflights for m in sf.get("matches", []) if m.get("pending"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(region_label)} {_esc(ntrp)} Women {year}</title>
<style>{_CSS}</style>
</head>
<body>

<div class="top-bar">{cross_link}</div>

{cards_html}

<div class="tabs">{tab_btns}</div>

{tab_panes}

<script>{_JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Matchup page builder
# ---------------------------------------------------------------------------

_MATCHUP_CSS = _CSS + """
/* ---- Matchup-page additions ---- */
body { max-width: 1100px; }
.mx-top-bar { display: flex; align-items: center; gap: 14px; margin-bottom: 14px;
              border-bottom: 1px solid #eee; padding-bottom: 10px; }
.mx-page-title { font-size: 14px; font-weight: 600; color: #333; }
.mx-section { margin-bottom: 32px; }
.mx-section-hdr { font-size: 13px; font-weight: 700; color: #333;
                  text-transform: uppercase; letter-spacing: .07em;
                  margin: 0 0 10px 0; padding-bottom: 5px;
                  border-bottom: 2px solid #e8e8e8; }
.mx-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
               flex-wrap: wrap; }
.mx-search { flex: 1; min-width: 160px; max-width: 300px; padding: 5px 10px;
             border: 1px solid #ddd; border-radius: 20px; font-size: 12px; }
.mc-chip { display: inline-block; width: 18px; height: 18px; border-radius: 50%;
           font-size: 9px; font-weight: 700; line-height: 18px; text-align: center;
           cursor: default; margin-right: 2px; }
.mc-w { background: #EAF3DE; color: #27500A; }
.mc-l { background: #FCEBEB; color: #791F1F; }
.mc-p { background: #f1efe8; color: #888; }
.mx-row { cursor: pointer; }
.mx-row td { vertical-align: middle; white-space: nowrap; }
.mx-row td:first-child { white-space: normal; display: flex; align-items: center; }
.mx-row:hover td { background: #f5f7fa; }
/* Clickable team cell */
.mx-team-cell { cursor: pointer; }
.mx-team-cell:hover { color: #1a5276; text-decoration: underline; }
/* Main tabs for singles / doubles */
.mx-tabs { display: flex; gap: 6px; margin-bottom: 14px; }
.mx-tab { padding: 6px 16px; border: 1px solid #ccc; border-radius: 20px;
          font-size: 13px; background: transparent; color: #666;
          cursor: pointer; font-weight: 500; }
.mx-tab.on { background: #eee; color: #222; font-weight: 600; border-color: #999; }
.mx-pane { display: none; }
.mx-pane.on { display: block; }
.mx-exp { font-size: 20px; color: #bbb; margin-left: 4px; transition: transform .15s; line-height: 1; vertical-align: middle; }
.mx-exp.open { display: inline-block; transform: rotate(90deg); }
.mx-detail td { background: #fafafa; padding: 6px 10px 10px 10px; }
.mx-matches { display: flex; flex-direction: column; gap: 3px; }
.mx-match-row { display: grid;
  grid-template-columns: 28px 36px 22px 1fr 90px 68px 68px;
  gap: 4px 8px; align-items: center; font-size: 11px;
  padding: 3px 4px; border-radius: 4px; }
.mx-match-row.mx-hi { background: #fffbe6; outline: 1px solid #e6c800; }
.mx-wk { font-size: 10px; font-weight: 700; color: #aaa; }
.mx-win { font-size: 12px; font-weight: 700; }
.mx-win.w { color: #27500A; }
.mx-win.l { color: #791F1F; }
.mx-score { font-size: 11px; font-weight: 600; color: #555; }
.mx-desc { font-size: 10px; color: #888; font-style: italic; }
.mx-odds { font-size: 10px; color: #888; }
.mx-odds.upset { color: #a04000; font-weight: 600; }
.mx-odds.solid { color: #27500A; font-weight: 600; }
.opp-link { color: #1a5276; cursor: pointer; border-radius: 3px; padding: 0 2px; }
.opp-link:hover { background: #e8f0f8; text-decoration: underline; }
.opp-team-label { color: #aaa; font-size: 10px; }
.opp-banner { display: none; align-items: center; gap: 8px; padding: 5px 10px;
              background: #fffbe6; border: 1px solid #e6c800; border-radius: 6px;
              font-size: 11px; margin-bottom: 8px; }
.opp-banner.on { display: flex; }
.opp-banner-clear { cursor: pointer; color: #791F1F; font-weight: 700; }
.min-matches-btns { display: flex; gap: 4px; margin-left: 8px; }
"""

_MATCHUP_JS = """
// ---- expand / collapse detail rows ----
function toggleDetail(id, triggerEl) {
  var det = document.getElementById(id);
  if (!det) return;
  var isOpen = det.style.display !== 'none';
  det.style.display = isOpen ? 'none' : '';
  var exp = triggerEl ? triggerEl.querySelector('.mx-exp') : null;
  if (!exp) {
    // find the corresponding main row
    var mainRow = document.querySelector('[data-det="' + id + '"]');
    if (mainRow) exp = mainRow.querySelector('.mx-exp');
  }
  if (exp) exp.classList.toggle('open', !isOpen);
}

// ---- SF filter (singles and doubles share the same pattern) ----
var _mxSF = { singles: 'all', doubles: 'all' };
function filterMxSF(sf, btn, section) {
  _mxSF[section] = sf;
  var grp = btn.parentElement;
  grp.querySelectorAll('.rtab').forEach(function(b) { b.classList.remove('on'); });
  btn.classList.add('on');
  _applyMxFilters(section);
}

// ---- search filter ----
function filterMxSearch(section) {
  _applyMxFilters(section);
}

// ---- opponent filter ----
var _oppKey = { singles: '', doubles: '' };
// Track which detail rows were auto-expanded by the opp filter (so we can collapse them on clear)
var _autoExpanded = { singles: [], doubles: [] };

function filterByOpp(key, display, section) {
  // Clicking the same opp again → clear the filter
  if (_oppKey[section] === key) {
    clearOppFilter(section);
    return;
  }
  // If switching from a different opp filter, collapse the old auto-expanded rows first
  _collapseAutoExpanded(section);

  _oppKey[section] = key;
  _autoExpanded[section] = [];

  var banner = document.getElementById(section + '-opp-banner');
  if (banner) {
    banner.classList.add('on');
    var lbl = banner.querySelector('.opp-banner-lbl');
    if (lbl) lbl.textContent = 'Filtered: players who faced ' + display;
  }
  _applyMxFilters(section);
  // auto-expand matching rows and highlight the specific match
  var tbody = document.getElementById(section + '-tbody');
  if (!tbody) return;
  tbody.querySelectorAll('tr.mx-row').forEach(function(row) {
    var keys = (row.dataset.oppKeys || '').split(' ');
    if (keys.indexOf(key) >= 0) {
      var detId = row.dataset.det;
      var det = document.getElementById(detId);
      if (det && det.style.display === 'none') {
        det.style.display = '';
        var exp = row.querySelector('.mx-exp');
        if (exp) exp.classList.add('open');
        _autoExpanded[section].push(detId);  // remember this was auto-expanded
      }
      // highlight matching match rows
      if (det) {
        det.querySelectorAll('.mx-match-row').forEach(function(mr) {
          mr.classList.toggle('mx-hi', mr.dataset.oppKey === key);
        });
      }
    }
  });
}

function _collapseAutoExpanded(section) {
  (_autoExpanded[section] || []).forEach(function(detId) {
    var det = document.getElementById(detId);
    if (det) {
      det.style.display = 'none';
      var mainRow = document.querySelector('[data-det="' + detId + '"]');
      if (mainRow) {
        var exp = mainRow.querySelector('.mx-exp');
        if (exp) exp.classList.remove('open');
      }
    }
  });
  _autoExpanded[section] = [];
}

function clearOppFilter(section) {
  _oppKey[section] = '';
  var banner = document.getElementById(section + '-opp-banner');
  if (banner) banner.classList.remove('on');
  var tbody = document.getElementById(section + '-tbody');
  if (tbody) {
    tbody.querySelectorAll('.mx-match-row').forEach(function(mr) {
      mr.classList.remove('mx-hi');
    });
  }
  _collapseAutoExpanded(section);
  _applyMxFilters(section);
}

// ---- main tab switcher (Singles / Doubles) ----
function switchMxTab(section, btn) {
  document.querySelectorAll('.mx-tab').forEach(function(b) { b.classList.remove('on'); });
  document.querySelectorAll('.mx-pane').forEach(function(p) { p.classList.remove('on'); });
  btn.classList.add('on');
  var pane = document.getElementById('mx-pane-' + section);
  if (pane) pane.classList.add('on');
}

// ---- team cell click → filter by team ----
function filterByTeam(abbrevTeam, section, event) {
  event.stopPropagation();  // don't also expand the row
  var searchEl = document.getElementById(section + '-search');
  if (!searchEl) return;
  // Toggle: clicking same team again clears the filter
  if (searchEl.value.toLowerCase() === abbrevTeam.toLowerCase()) {
    searchEl.value = '';
  } else {
    searchEl.value = abbrevTeam;
  }
  filterMxSearch(section);
}

// ---- default descending for quality columns (first click shows best first) ----
// _sortDir state is true → next click will be descending (dir = !true = false → bn-an)
['s3','s4','s5','s6','s7','d3','d4','d5'].forEach(function(k) { _sortDir[k] = true; });
function _applyMxFilters(section) {
  var sf = _mxSF[section];
  var oppK = _oppKey[section];
  var searchEl = document.getElementById(section + '-search');
  var q = searchEl ? searchEl.value.toLowerCase() : '';
  var tbody = document.getElementById(section + '-tbody');
  if (!tbody) return;
  var rows = tbody.querySelectorAll('tr.mx-row');
  rows.forEach(function(row) {
    var sfOk = sf === 'all' || row.dataset.sf === sf;
    var oppOk = !oppK || (row.dataset.oppKeys || '').split(' ').indexOf(oppK) >= 0;
    var textOk = !q || (row.dataset.searchText || '').indexOf(q) >= 0;
    var mmOk = parseInt(row.dataset.matchCount || '0') >= _minMatches[section];
    var vis = sfOk && oppOk && textOk && mmOk;
    row.style.display = vis ? '' : 'none';
    // also hide detail row when main row is hidden
    var det = document.getElementById(row.dataset.det);
    if (det && !vis) det.style.display = 'none';
  });
}

// ---- sorting ----
var _mxSortDir = {};
function sortMx(tbodyId, col, dirKey) {
  _sortTable('#' + tbodyId, col, dirKey);
  // after sorting, re-pair each main row with its detail row
  var tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  var rows = Array.from(tbody.querySelectorAll('tr'));
  // rebuild order: each mx-row followed immediately by its det row
  var mxRows = rows.filter(function(r) { return r.classList.contains('mx-row'); });
  mxRows.forEach(function(mr) {
    tbody.appendChild(mr);
    var det = document.getElementById(mr.dataset.det);
    if (det) tbody.appendChild(det);
  });
}

// ---- min-matches filter (singles + doubles) ----
var _minMatches = { singles: 1, doubles: 2 };
function setMinMatches(n, btn, section) {
  _minMatches[section] = n;
  btn.parentElement.querySelectorAll('.rtab').forEach(function(b) { b.classList.remove('on'); });
  btn.classList.add('on');
  _applyMxFilters(section);
}
"""


def _build_matchup_page(ntrp: str, standings: dict, players: list[dict]) -> str:
    """Generate matchups_{sfx}.html — singles + doubles explorer."""
    sfx = ntrp.replace(".", "")
    year = standings.get("year", "")
    subflights = standings.get("subflights", [])

    # ── lookup tables from players ──────────────────────────────────────────
    _baseline_by_name: dict[str, float] = {}
    _new_by_name: dict[str, float] = {}
    _timeline_by_name: dict[str, dict[str, float]] = {}
    _post_timeline_by_name: dict[str, dict[str, float]] = {}
    _team_by_name: dict[str, str] = {}

    team_to_sf: dict[str, str] = {}
    for sf_obj in subflights:
        lbl = sf_obj.get("flight_label", "")
        for t in sf_obj.get("teams", []):
            tn = t.get("team_name", "")
            if tn not in team_to_sf or not lbl.startswith("Championships"):
                team_to_sf[tn] = lbl

    for p in players:
        norm = re.sub(r"\s+", " ", (p.get("name") or "").strip().lower())
        if not norm:
            continue
        baseline = p.get("dynamic_rating_baseline")
        if baseline is not None:
            try:
                _baseline_by_name[norm] = float(baseline)
            except (ValueError, TypeError):
                pass
        curr = p.get(f"rating_{sfx}")
        if curr is not None:
            try:
                _new_by_name[norm] = float(curr)
            except (ValueError, TypeError):
                pass
        tl = p.get(f"rating_timeline_{sfx}")
        if tl and isinstance(tl, dict):
            _timeline_by_name[norm] = {k: float(v) for k, v in tl.items()}
        post_tl = p.get(f"rating_post_timeline_{sfx}")
        if post_tl and isinstance(post_tl, dict):
            _post_timeline_by_name[norm] = {k: float(v) for k, v in post_tl.items()}
        team_val = (p.get(f"team_{sfx}") or p.get("team") or "")
        if team_val:
            _team_by_name[norm] = team_val

    def _pit_r(nkey: str, date: str) -> Optional[float]:
        nkey = re.sub(r"\s+", " ", nkey.strip().lower())
        pre_tl = _timeline_by_name.get(nkey, {})
        if date in pre_tl:
            return pre_tl[date]
        mk = _date_sort_key(date)
        post_tl = _post_timeline_by_name.get(nkey, {})
        prior_post = [(k, v) for k, v in post_tl.items() if _date_sort_key(k) < mk]
        if prior_post:
            return max(prior_post, key=lambda kv: _date_sort_key(kv[0]))[1]
        prior_pre = [(k, v) for k, v in pre_tl.items() if _date_sort_key(k) < mk]
        if prior_pre:
            return max(prior_pre, key=lambda kv: _date_sort_key(kv[0]))[1]
        return _baseline_by_name.get(nkey) or _new_by_name.get(nkey)

    # ── week label map ───────────────────────────────────────────────────────
    all_dates: set[str] = set()
    for sf in subflights:
        for m in sf.get("matches", []):
            d = m.get("date", "")
            if d:
                all_dates.add(d)
    sorted_dates = sorted(all_dates, key=_date_sort_key)
    week_label: dict[str, str] = {d: f"W{i+1}" for i, d in enumerate(sorted_dates)}

    # ── walk all lines ───────────────────────────────────────────────────────
    singles_by_player: dict[str, dict] = {}      # norm_name → player data
    doubles_by_pair: dict[frozenset, dict] = {}  # frozenset(norm1, norm2) → pair data

    for sf in subflights:
        sf_label = sf.get("flight_label", "")
        for m in sf.get("matches", []):
            if m.get("pending"):
                continue
            home_team = m.get("home_team", "")
            away_team = m.get("away_team", "")
            date = m.get("date", "")
            wk = week_label.get(date, "")
            for ln in m.get("lines", []):
                line_str = ln.get("line", "")
                line_label = _line_label_short(line_str)
                is_singles = "Singles" in line_str
                score = ln.get("score", "")
                winner_team = (ln.get("winner_team") or "").upper()
                _match_wt_mx = _match_winner_team(m) if not winner_team else None

                ph = (ln.get("players_home") or "").strip()
                pa = (ln.get("players_away") or "").strip()
                if ph.upper() in ("", "N/A"):
                    ph = ""
                if pa.upper() in ("", "N/A"):
                    pa = ""
                ph = " / ".join(n.strip() for n in ph.split("/")
                                if n.strip() and not _is_noise_name(n))
                pa = " / ".join(n.strip() for n in pa.split("/")
                                if n.strip() and not _is_noise_name(n))

                sides = [
                    (ph, home_team, pa, away_team),
                    (pa, away_team, ph, home_team),
                ]

                if is_singles:
                    for side_raw, side_team, opp_raw, opp_team in sides:
                        if not side_raw:
                            continue
                        # Normalise all-caps names from scorecards to title case
                        raw_name = side_raw.strip()
                        player_name = raw_name.title() if raw_name.isupper() else raw_name
                        norm = re.sub(r"\s+", " ", player_name.lower())
                        opp_norm = re.sub(r"\s+", " ", (opp_raw or "").lower())

                        # Use _team_by_name for win detection — handles scorecard swaps
                        # where players_home/away columns may be swapped but winner_team
                        # always correctly names the winning team.
                        actual_team = (_team_by_name.get(norm) or side_team).upper()
                        won = (winner_team == actual_team) if winner_team else None
                        if won is None and _match_wt_mx:
                            won = actual_team == _match_wt_mx.upper()
                        if won is None:
                            continue
                        # Opponent's actual team (also handles swapped scorecards)
                        actual_opp_team = _team_by_name.get(opp_norm) or opp_team

                        pit = _pit_r(norm, date)
                        opp_pit = _pit_r(opp_norm, date)
                        desc = _score_desc_short(score)
                        exp_prob: Optional[float] = None
                        if pit is not None and opp_pit is not None:
                            exp_prob = _win_prob_gap(pit - opp_pit)

                        if norm not in singles_by_player:
                            team = _team_by_name.get(norm) or side_team
                            psf = team_to_sf.get(team, sf_label)
                            singles_by_player[norm] = {
                                "name": player_name,
                                "team": team,
                                "sf": psf,
                                "baseline": _baseline_by_name.get(norm),
                                "rating": _new_by_name.get(norm),
                                "matches": [],
                            }

                        opp_key = re.sub(r"[^a-z0-9]+", "-",
                                         opp_raw.strip().lower()).strip("-")
                        singles_by_player[norm]["matches"].append({
                            "date": date,
                            "week": wk,
                            "line": line_label,
                            "won": won,
                            "score": score,
                            "desc": desc,
                            "opp_name": opp_raw,
                            "opp_key": opp_key,
                            "opp_team": actual_opp_team,
                            "opp_rating": opp_pit,
                            "exp_prob": exp_prob,
                        })

                else:  # doubles
                    for side_raw, side_team, opp_raw, opp_team in sides:
                        if not side_raw:
                            continue
                        parts = [x.strip() for x in side_raw.split("/") if x.strip()]
                        if len(parts) != 2:
                            continue
                        # Normalise display names: title-case all-caps names from scorecards
                        p1_name = parts[0].title() if parts[0].isupper() else parts[0]
                        p2_name = parts[1].title() if parts[1].isupper() else parts[1]
                        n1 = re.sub(r"\s+", " ", p1_name.lower())
                        n2 = re.sub(r"\s+", " ", p2_name.lower())
                        # Use _team_by_name for win detection (handles swapped scorecards)
                        actual_d_team = (
                            _team_by_name.get(n1) or _team_by_name.get(n2) or side_team
                        ).upper()
                        won = (winner_team == actual_d_team) if winner_team else None
                        if won is None and _match_wt_mx:
                            won = actual_d_team == _match_wt_mx.upper()
                        if won is None:
                            continue

                        pair_key = frozenset([n1, n2])

                        opp_parts = [x.strip() for x in (opp_raw or "").split("/")
                                     if x.strip()]
                        opp_normed = [re.sub(r"\s+", " ", x.lower()) for x in opp_parts]
                        opp_ratings = [_pit_r(on, date) for on in opp_normed]
                        opp_ratings_clean = [r for r in opp_ratings if r is not None]
                        opp_avg = (sum(opp_ratings_clean) / len(opp_ratings_clean)
                                   if opp_ratings_clean else None)
                        opp_key = re.sub(r"[^a-z0-9]+", "-",
                                         (opp_raw or "").strip().lower()).strip("-")
                        # Opponent's actual team (resolves scorecard swap)
                        actual_opp_d_team = (
                            _team_by_name.get(opp_normed[0]) if opp_normed else None
                        ) or opp_team

                        desc = _score_desc_short(score)
                        r1 = _pit_r(n1, date)
                        r2 = _pit_r(n2, date)

                        if pair_key not in doubles_by_pair:
                            team = _team_by_name.get(n1) or _team_by_name.get(n2) or side_team
                            psf = team_to_sf.get(team, sf_label)
                            doubles_by_pair[pair_key] = {
                                "p1": p1_name, "p2": p2_name,
                                "team": team, "sf": psf,
                                "r1": _new_by_name.get(n1),
                                "r2": _new_by_name.get(n2),
                                "bl1": _baseline_by_name.get(n1),
                                "bl2": _baseline_by_name.get(n2),
                                "matches": [],
                            }

                        doubles_by_pair[pair_key]["matches"].append({
                            "date": date,
                            "week": wk,
                            "line": line_label,
                            "won": won,
                            "score": score,
                            "desc": desc,
                            "opp_names": opp_raw,
                            "opp_key": opp_key,
                            "opp_team": actual_opp_d_team,
                            "opp_avg_rating": opp_avg,
                        })

    # ── compute aggregates ───────────────────────────────────────────────────
    singles_list = sorted(
        singles_by_player.values(),
        key=lambda p: -(p.get("rating") or p.get("baseline") or 0)
    )

    doubles_list = []
    for pdata in doubles_by_pair.values():
        if len(pdata["matches"]) < 2:
            continue
        r1 = pdata.get("r1") or pdata.get("bl1") or 0.0
        r2 = pdata.get("r2") or pdata.get("bl2") or 0.0
        bl1 = pdata.get("bl1") or r1
        bl2 = pdata.get("bl2") or r2
        pdata["avg_rating"] = (r1 + r2) / 2 if r1 and r2 else (r1 or r2)
        pdata["avg_baseline"] = (bl1 + bl2) / 2 if bl1 and bl2 else (bl1 or bl2)
        doubles_list.append(pdata)
    doubles_list.sort(key=lambda p: -p.get("avg_rating", 0))

    # ── HTML generators ──────────────────────────────────────────────────────
    def _odds_html(exp_prob: Optional[float], won: bool) -> str:
        if exp_prob is None:
            return ""
        pct = int(round(exp_prob * 100))
        if won:
            if exp_prob < 0.40:
                return f'<span class="mx-odds upset">{pct}% odds — upset!</span>'
            elif exp_prob >= 0.65:
                return f'<span class="mx-odds solid">{pct}% fav</span>'
            return f'<span class="mx-odds">{pct}% fav</span>'
        else:
            if exp_prob >= 0.65:
                return f'<span class="mx-odds upset">lost as {pct}% fav</span>'
            return f'<span class="mx-odds">{pct}% chances</span>'

    def _singles_rows(plist) -> str:
        rows = ""
        for i, p in enumerate(plist):
            matches = sorted(p["matches"], key=lambda x: _date_sort_key(x["date"]))
            wins = sum(1 for mx in matches if mx["won"])
            losses = len(matches) - wins
            win_pct = int(round(wins / len(matches) * 100)) if matches else 0
            avg_opp = None
            opp_rs = [mx["opp_rating"] for mx in matches if mx["opp_rating"] is not None]
            if opp_rs:
                avg_opp = sum(opp_rs) / len(opp_rs)

            opp_keys_set = " ".join(
                mx["opp_key"] for mx in matches if mx.get("opp_key"))
            abbrev_team = _abbrev_team(p["team"])
            search_text = (
                p["name"] + " " + p["team"] + " " + abbrev_team
            ).lower()
            det_id = f"sdet-{i}"

            # Mini chips
            chips = ""
            for mx in matches:
                cls = "mc-w" if mx["won"] else "mc-l"
                lbl = "W" if mx["won"] else "L"
                tip = (f"{mx['week']} {mx['line']}: vs {mx['opp_name']} {mx['score']}"
                       .replace('"', "&quot;"))
                chips += f'<span class="mc-chip {cls}" title="{_esc(tip)}">{lbl}</span>'

            # Rating cell
            base = p.get("baseline")
            curr = p.get("rating")
            rat_html = _rating_span(curr, base, ntrp)

            wl_sort = wins * 100 - losses
            main_row = (
                f'<tr class="mx-row" data-sf="{_esc(p["sf"])}" '
                f'data-opp-keys="{_esc(opp_keys_set)}" '
                f'data-search-text="{_esc(search_text)}" '
                f'data-match-count="{len(matches)}" '
                f'data-det="{det_id}" '
                f'onclick="toggleDetail(\'{det_id}\',this)">'
                f'<td class="pname">{_esc(p["name"])} '
                f'<span class="mx-exp">▸</span></td>'
                f'<td class="mx-team-cell" '
                f'onclick="filterByTeam(\'{_esc(abbrev_team)}\',\'singles\',event)">'
                f'{_esc(abbrev_team)}</td>'
                f'<td><span class="sf-pill">{_esc(p["sf"])}</span></td>'
                f'<td data-sort="{_fmt_rating(base)}">{_esc(_fmt_rating(base))}</td>'
                f'<td data-sort="{_fmt_rating(curr)}">{rat_html}</td>'
                f'<td data-sort="{wl_sort}" style="white-space:nowrap">{wins}–{losses}</td>'
                f'<td data-sort="{win_pct}">{win_pct}%</td>'
                f'<td data-sort="{f"{avg_opp:.4f}" if avg_opp else "0"}">'
                f'{_fmt_rating(avg_opp)}</td>'
                f'<td>{chips}</td>'
                f'</tr>\n'
            )

            # Detail row
            match_rows = ""
            for mx in matches:
                win_cls = "w" if mx["won"] else "l"
                win_sym = "✓" if mx["won"] else "✗"
                line_cls = _LINE_PILL_COLORS.get(mx["line"], "pill-d1")
                opp_r_str = (f"{mx['opp_rating']:.2f}" if mx["opp_rating"] is not None
                             else "")
                opp_info = f'({opp_r_str}, {_abbrev_team(mx["opp_team"])})' if opp_r_str else ""
                odds_h = _odds_html(mx.get("exp_prob"), mx["won"])
                opp_key = mx.get("opp_key", "")
                match_rows += (
                    f'<div class="mx-match-row" data-opp-key="{_esc(opp_key)}">'
                    f'<span class="mx-wk">{_esc(mx["week"])}</span>'
                    f'<span class="line-pill {line_cls}">{_esc(mx["line"])}</span>'
                    f'<span class="mx-win {win_cls}">{win_sym}</span>'
                    f'<span>vs <span class="opp-link" '
                    f'onclick="filterByOpp(\'{_esc(opp_key)}\',\'{_esc(mx["opp_name"])}\',\'singles\');event.stopPropagation()">'
                    f'{_esc(mx["opp_name"])}</span> '
                    f'<span class="opp-team-label">{_esc(opp_info)}</span></span>'
                    f'<span class="mx-score">{_esc(mx["score"])}</span>'
                    f'<span class="mx-desc">{_esc(mx["desc"])}</span>'
                    f'{odds_h}'
                    f'</div>\n'
                )

            det_row = (
                f'<tr id="{det_id}" class="mx-detail" style="display:none">'
                f'<td colspan="9" class="mx-det-cell">'
                f'<div class="mx-matches">{match_rows}</div>'
                f'</td></tr>\n'
            )
            rows += main_row + det_row
        return rows

    def _doubles_rows(dlist) -> str:
        rows = ""
        for i, p in enumerate(dlist):
            matches = sorted(p["matches"], key=lambda x: _date_sort_key(x["date"]))
            wins = sum(1 for mx in matches if mx["won"])
            losses = len(matches) - wins
            win_pct = int(round(wins / len(matches) * 100)) if matches else 0
            nm = len(matches)

            # Lines pills
            from collections import Counter
            line_counts = Counter(mx["line"] for mx in matches)
            lines_html = "".join(
                f'<span class="line-pill {_LINE_PILL_COLORS.get(l, "pill-d1")}">'
                f'{_esc(l)}{"x" + str(c) if c > 1 else ""}</span>'
                for l, c in sorted(line_counts.items())
            )

            opp_keys_set = " ".join(
                mx["opp_key"] for mx in matches if mx.get("opp_key"))
            abbrev_team_d = _abbrev_team(p["team"])
            search_text = (
                p["p1"] + " " + p["p2"] + " " + p["team"] + " " + abbrev_team_d
            ).lower()
            det_id = f"ddet-{i}"

            chips = ""
            for mx in matches:
                cls = "mc-w" if mx["won"] else "mc-l"
                lbl = "W" if mx["won"] else "L"
                tip = (f"{mx['week']} {mx['line']}: vs {mx['opp_names']} {mx['score']}"
                       .replace('"', "&quot;"))
                chips += f'<span class="mc-chip {cls}" title="{_esc(tip)}">{lbl}</span>'

            avg_r = p.get("avg_rating", 0)
            avg_bl = p.get("avg_baseline", 0)
            r1 = p.get("r1")
            r2 = p.get("r2")
            bl1 = p.get("bl1")
            bl2 = p.get("bl2")

            if avg_r and avg_bl:
                if avg_r > avg_bl + 0.005:
                    avg_cls = "ru"
                elif avg_r < avg_bl - 0.005:
                    avg_cls = "rd"
                else:
                    avg_cls = "rn"
                avg_html = f'<span class="{avg_cls}">{avg_r:.2f}</span>'
            else:
                avg_html = _fmt_rating(avg_r)

            wl_sort = wins * 100 - losses

            main_row = (
                f'<tr class="mx-row" data-sf="{_esc(p["sf"])}" '
                f'data-opp-keys="{_esc(opp_keys_set)}" '
                f'data-search-text="{_esc(search_text)}" '
                f'data-match-count="{nm}" '
                f'data-det="{det_id}" '
                f'onclick="toggleDetail(\'{det_id}\',this)">'
                f'<td class="pname">{_esc(p["p1"])} / {_esc(p["p2"])} '
                f'<span class="mx-exp">▸</span></td>'
                f'<td class="mx-team-cell" '
                f'onclick="filterByTeam(\'{_esc(abbrev_team_d)}\',\'doubles\',event)">'
                f'{_esc(abbrev_team_d)}</td>'
                f'<td><span class="sf-pill">{_esc(p["sf"])}</span></td>'
                f'<td data-sort="{avg_r:.4f}">{avg_html}</td>'
                f'<td data-sort="{wl_sort}" style="white-space:nowrap">{wins}–{losses}</td>'
                f'<td data-sort="{win_pct}">{win_pct}%</td>'
                f'<td>{lines_html}</td>'
                f'<td>{chips}</td>'
                f'</tr>\n'
            )

            match_rows = ""
            for mx in matches:
                win_cls = "w" if mx["won"] else "l"
                win_sym = "✓" if mx["won"] else "✗"
                line_cls = _LINE_PILL_COLORS.get(mx["line"], "pill-d1")
                opp_avg = mx.get("opp_avg_rating")
                opp_r_str = f"{opp_avg:.2f}" if opp_avg is not None else ""
                opp_info = f'({opp_r_str} avg, {_abbrev_team(mx["opp_team"])})' if opp_r_str else ""
                opp_key = mx.get("opp_key", "")
                match_rows += (
                    f'<div class="mx-match-row" data-opp-key="{_esc(opp_key)}">'
                    f'<span class="mx-wk">{_esc(mx["week"])}</span>'
                    f'<span class="line-pill {line_cls}">{_esc(mx["line"])}</span>'
                    f'<span class="mx-win {win_cls}">{win_sym}</span>'
                    f'<span>vs <span class="opp-link" '
                    f'onclick="filterByOpp(\'{_esc(opp_key)}\',\'{_esc(mx["opp_names"])}\',\'doubles\');event.stopPropagation()">'
                    f'{_esc(mx["opp_names"])}</span> '
                    f'<span class="opp-team-label">{_esc(opp_info)}</span></span>'
                    f'<span class="mx-score">{_esc(mx["score"])}</span>'
                    f'<span class="mx-desc">{_esc(mx["desc"])}</span>'
                    f'</div>\n'
                )

            det_row = (
                f'<tr id="{det_id}" class="mx-detail" style="display:none">'
                f'<td colspan="8" class="mx-det-cell">'
                f'<div class="mx-matches">{match_rows}</div>'
                f'</td></tr>\n'
            )
            rows += main_row + det_row
        return rows

    s_rows = _singles_rows(singles_list)
    d_rows = _doubles_rows(doubles_list)

    other_ntrp = "3.5" if ntrp == "3.0" else "3.0"
    other_mx = f"matchups_{other_ntrp.replace('.', '')}.html"
    main_dash = f"women_{sfx}.html"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Singles &amp; Doubles Explorer — {_esc(ntrp)} Women {year}</title>
<style>{_MATCHUP_CSS}</style>
</head>
<body>

<div class="mx-top-bar">
  <a href="{main_dash}" class="cross-link">← Division Dashboard</a>
  <span class="mx-page-title">Singles &amp; Doubles Explorer — {_esc(ntrp)} Women {year}</span>
  <a href="{other_mx}" class="cross-link" style="margin-left:auto">{_esc(other_ntrp)} Explorer →</a>
</div>

<div class="mx-tabs">
  <button class="mx-tab on" onclick="switchMxTab('singles',this)">Singles</button>
  <button class="mx-tab" onclick="switchMxTab('doubles',this)">Doubles Pairs</button>
</div>

<!-- ========= SINGLES ========= -->
<div id="mx-pane-singles" class="mx-pane on">
<section class="mx-section" id="singles-section">
  <div id="singles-opp-banner" class="opp-banner">
    <span class="opp-banner-lbl"></span>
    <span class="opp-banner-clear" onclick="clearOppFilter('singles')">✕ clear</span>
  </div>
  <div class="mx-controls">
    <div class="sf-filter-btns">
      <button class="rtab on" onclick="filterMxSF('all',this,'singles')">All</button>
      <button class="rtab" onclick="filterMxSF('A',this,'singles')">A</button>
      <button class="rtab" onclick="filterMxSF('B',this,'singles')">B</button>
    </div>
    <input class="mx-search" id="singles-search" placeholder="Search player or team…"
           oninput="filterMxSearch('singles')">
    <div class="min-matches-btns">
      <span style="font-size:11px;color:#888">min matches:</span>
      <button class="rtab on" onclick="setMinMatches(1,this,'singles')">All</button>
      <button class="rtab" onclick="setMinMatches(2,this,'singles')">2+</button>
      <button class="rtab" onclick="setMinMatches(3,this,'singles')">3+</button>
    </div>
  </div>
  <table>
    <thead><tr>
      <th class="sortable" onclick="sortMx('singles-tbody',0,'s0')">Player ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',1,'s1')">Team ↕</th>
      <th>SF</th>
      <th class="sortable" onclick="sortMx('singles-tbody',3,'s3')">Base ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',4,'s4')">Rating ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',5,'s5')">W–L ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',6,'s6')">Win% ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',7,'s7')">Avg Opp ↕</th>
      <th>Matches</th>
    </tr></thead>
    <tbody id="singles-tbody">{s_rows}</tbody>
  </table>
</section>
</div>

<!-- ========= DOUBLES ========= -->
<div id="mx-pane-doubles" class="mx-pane">
<section class="mx-section" id="doubles-section">
  <div id="doubles-opp-banner" class="opp-banner">
    <span class="opp-banner-lbl"></span>
    <span class="opp-banner-clear" onclick="clearOppFilter('doubles')">✕ clear</span>
  </div>
  <div class="mx-controls">
    <div class="sf-filter-btns">
      <button class="rtab on" onclick="filterMxSF('all',this,'doubles')">All</button>
      <button class="rtab" onclick="filterMxSF('A',this,'doubles')">A</button>
      <button class="rtab" onclick="filterMxSF('B',this,'doubles')">B</button>
    </div>
    <input class="mx-search" id="doubles-search" placeholder="Search players or team…"
           oninput="filterMxSearch('doubles')">
    <div class="min-matches-btns">
      <span style="font-size:11px;color:#888">min matches:</span>
      <button class="rtab on" onclick="setMinMatches(2,this,'doubles')">2+</button>
      <button class="rtab" onclick="setMinMatches(3,this,'doubles')">3+</button>
      <button class="rtab" onclick="setMinMatches(4,this,'doubles')">4+</button>
    </div>
  </div>
  <table>
    <thead><tr>
      <th class="sortable" onclick="sortMx('doubles-tbody',0,'d0')">Players ↕</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',1,'d1')">Team ↕</th>
      <th>SF</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',3,'d3')">Avg Rating ↕</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',4,'d4')">W–L ↕</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',5,'d5')">Win% ↕</th>
      <th>Lines</th>
      <th>Matches</th>
    </tr></thead>
    <tbody id="doubles-tbody">{d_rows}</tbody>
  </table>
</section>
</div>

<script>{_JS}{_MATCHUP_JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_dashboards(states: list[str] | None = None) -> dict:
    """Build dashboards for the specified states (or all states in regions.json)."""
    players = _load(PLAYERS_JSON, [])
    regions = _load_regions()
    results = {}

    if states is None:
        states = list(regions.get("states", {}).keys())
    if not states:
        states = ["NV"]

    for state_code in states:
        cfg = _get_state_config(state_code)
        region_label = cfg.get("label", state_code)
        st_lower = state_code.lower()

        # Load standings for this state
        s30_path = DATA_DIR / f"standings_{st_lower}_30.json"
        s35_path = DATA_DIR / f"standings_{st_lower}_35.json"

        if not s30_path.exists() and not s35_path.exists():
            print(f"  [html] No standings data for {state_code}, skipping")
            continue

        s30 = _load(s30_path, {"ntrp": "3.0", "year": 2026, "subflights": []})
        s35 = _load(s35_path, {"ntrp": "3.5", "year": 2026, "subflights": []})

        for ntrp, standings, other_standings in [("3.0", s30, s35), ("3.5", s35, s30)]:
            out_path = Path(f"women_{st_lower}_{ntrp.replace('.', '')}.html")
            mx_path = Path(f"matchups_{st_lower}_{ntrp.replace('.', '')}.html")

            html = _generate_html(ntrp, standings, players, other_standings,
                                  state_code=state_code, region_label=region_label)
            out_path.write_text(html, encoding="utf-8")
            mx_html = _build_matchup_page(ntrp, standings, players)
            mx_path.write_text(mx_html, encoding="utf-8")
            state_p = [p for p in players if p.get("state") == state_code]
            n = len([p for p in state_p if p.get("division", "").startswith(ntrp)])
            n_matches = sum(
                len(sf.get("matches", []))
                for sf in standings.get("subflights", [])
            )
            print(f"  [html] {out_path}  ({n} players, {n_matches} matches, {region_label})")
            print(f"  [html] {mx_path}  (matchups explorer)")
            results[str(out_path)] = n

    return results


def build_dashboards_legacy() -> dict:
    """Legacy single-state builder for backward compat. Generates women_30/35.html."""
    players = _load(PLAYERS_JSON, [])
    results = {}
    s30 = _load(STANDINGS_30, {"ntrp": "3.0", "year": 2026, "subflights": []})
    s35 = _load(STANDINGS_35, {"ntrp": "3.5", "year": 2026, "subflights": []})
    for ntrp, standings, other_standings, out_path, mx_path in [
        ("3.0", s30, s35, Path("women_30.html"), Path("matchups_30.html")),
        ("3.5", s35, s30, Path("women_35.html"), Path("matchups_35.html")),
    ]:
        html = _generate_html(ntrp, standings, players, other_standings)
        out_path.write_text(html, encoding="utf-8")
        mx_html = _build_matchup_page(ntrp, standings, players)
        mx_path.write_text(mx_html, encoding="utf-8")
        n = len([p for p in players if p.get("division", "").startswith(ntrp)])
        results[str(out_path)] = n
    return results


# ---------------------------------------------------------------------------
# Sectionals comparison page
# ---------------------------------------------------------------------------

SECTIONALS_QUALIFIED_JSON = DATA_DIR / "sectionals_qualified.json"


def _build_sectionals_rosters(
    qualified_players: list[dict],
    teams: list[dict],
    all_subflights_30: list[dict],
    ntrp: str,
    player_stats: dict[str, dict] | None = None,
) -> str:
    _sfx = ntrp.replace(".", "") if ntrp else ""

    # Group players by state; each state has one qualifying team
    by_state: dict[str, list] = {}
    state_team_name: dict[str, str] = {}
    for t in teams:
        state_team_name[t["state"]] = t["team"]
    for p in qualified_players:
        st = p.get("state", "") or "?"
        by_state.setdefault(st, []).append(p)

    state_btns = ""
    rpanes = ""
    first = True

    for st in sorted(by_state.keys()):
        active = " on" if first else ""
        tid = f"sect-ro-{_slug(st)}"
        tname = state_team_name.get(st, st)
        state_btns += (
            f'<button class="rtab{active}" '
            f'onclick="sr(\'{tid}\',this,\'sect-state-tabs\')">'
            f'{_esc(st)}</button>\n'
        )

        roster = sorted(
            by_state[st],
            key=lambda p: -(p.get(f"rating_{_sfx}") or
                            p.get("current_division_rating") or
                            p.get("dynamic_rating_baseline") or 0)
        )
        rows = ""
        for p in roster:
            ntrp_r = p.get("ntrp_rating", "") or ""
            baseline = p.get("dynamic_rating_baseline")
            curr = p.get(f"rating_{_sfx}") or p.get("current_division_rating")
            nk = _nkey(p.get("name", ""))
            pst = (player_stats or {}).get(nk, {})
            _w = pst.get("w", 0)
            _l = pst.get("l", 0)
            _wko = pst.get("wko", 0)
            if _w + _l > 0:
                wl = f"{_w}-{_l}" + ("*" if _wko else "")
            else:
                wl = p.get(f"wl_record_{_sfx}") or "–"
            lines = p.get(f"lines_played_{_sfx}") or "–"
            rows += (
                f"<tr>"
                f"<td>{_esc(p.get('name',''))}</td>"
                f"<td>{_esc(ntrp_r)}</td>"
                f"<td>{_esc(_fmt_rating(baseline))}</td>"
                f"<td>{_rating_span(curr, baseline, ntrp_r)}</td>"
                f"<td style='white-space:nowrap'>{_esc(str(wl))}</td>"
                f"<td>{_lines_pills_html(lines)}</td>"
                f"</tr>\n"
            )
        if not rows:
            rows = "<tr><td colspan='6' class='muted'>No players yet.</td></tr>"

        rpanes += (
            f'<div id="{tid}" class="rpane{active}">'
            f'<p class="sec-title">{_esc(tname)} &mdash; {_esc(st)}</p>'
            f'<table><thead><tr>'
            f'<th class="sortable" onclick="sortRoster(0)">Player ↕</th>'
            f'<th class="sortable" onclick="sortRoster(1)">NTRP ↕</th>'
            f'<th class="sortable" onclick="sortRoster(2)">Base ↕</th>'
            f'<th class="sortable" onclick="sortRoster(3)">New ↕</th>'
            f'<th class="sortable" onclick="sortRoster(4)">W–L ↕</th><th>Lines</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>\n'
        )
        first = False

    return (
        f'<div class="rtabs" id="sect-state-tabs">{state_btns}</div>'
        + rpanes
        + '<p class="wl-footnote">* W–L excludes defaults/walkovers (opponent absent)</p>'
    )


def build_sectionals_page() -> str | None:
    """
    Build sectionals_30.html — a comparison dashboard showing only
    sectionals-qualified players from all states.
    """
    qualified = _load(SECTIONALS_QUALIFIED_JSON, {})
    teams = qualified.get("qualified_teams", [])
    if not teams:
        print("  [sectionals] No qualified teams found, skipping")
        return None

    players = _load(PLAYERS_JSON, [])
    regions = _load_regions()

    # Build set of qualified team names (lowered) by state
    qualified_teams_by_state: dict[str, set[str]] = {}
    for t in teams:
        st = t["state"]
        qualified_teams_by_state.setdefault(st, set()).add(t["team"].lower().strip())

    # Collect all standings across all states (for match history)
    all_subflights_30: list[dict] = []
    all_subflights_35: list[dict] = []
    for st in qualified_teams_by_state:
        st_lower = st.lower()
        s30 = _load(DATA_DIR / f"standings_{st_lower}_30.json", {})
        for sf in s30.get("subflights", []):
            all_subflights_30.append(sf)
        # Districts
        d30 = _load(DATA_DIR / f"districts_{st_lower}_30.json", {})
        if d30.get("matches"):
            all_subflights_30.append({"flight_label": "Districts", "teams": d30.get("teams", []),
                                      "matches": d30.get("matches", [])})
        # 3.5 for cross-division
        s35 = _load(DATA_DIR / f"standings_{st_lower}_35.json", {})
        for sf in s35.get("subflights", []):
            all_subflights_35.append(sf)

    # Filter players to those on qualified teams in the 3.0 division
    # (team names like "DTC #3" exist across multiple NTRP levels)
    qualified_players = []
    for p in players:
        st = p.get("state", "")
        team_30 = (p.get("team_30") or "").lower().strip()
        div = p.get("division", "")
        has_30_stats = bool(p.get("wl_record_30") or p.get("lines_played_30"))
        if st in qualified_teams_by_state:
            # Only use team_30 — never fall back to generic `team` field since
            # that may be set to the player's 3.5 team (e.g. DTC #3 for a 3.5
            # player whose 3.0 team is DTC #4).
            if team_30 and team_30 in qualified_teams_by_state[st]:
                qualified_players.append(p)

    if not qualified_players:
        print("  [sectionals] No qualified players found")
        return None

    print(f"  [sectionals] {len(qualified_players)} qualified players from "
          f"{len(qualified_teams_by_state)} states")

    # Build the HTML using existing tab builders
    ntrp = "3.0"
    _sfx = "30"

    # Compute traversal-based W-L for all players (used by both tabs)
    _, sect_player_stats = _traverse_match_histories(
        players, ntrp, all_subflights_30, all_subflights_35)

    players_html = _players_tab(qualified_players, ntrp, all_subflights_30,
                                all_subflights_35, is_sectionals=True,
                                all_players_pool=players)

    # Build team rosters tab grouped by state
    rosters_html = _build_sectionals_rosters(
        qualified_players, teams, all_subflights_30, ntrp,
        player_stats=sect_player_stats,
    )

    # Build the standings AND match-results tabs together — one State tab
    # per qualified state, each showing that state's districts/championship
    # subflights (sorted Flight A, B, C, ..., then any non-lettered final
    # flight), with the existing subflight -> team drill-down nested inside
    # results, and a subflight -> standings table drill-down for standings.
    results_state_btns, results_state_panes = "", ""
    standings_state_btns, standings_state_panes = "", ""
    for i, st in enumerate(sorted(qualified_teams_by_state)):
        st_lower = st.lower()
        st_subflights = []
        d30 = _load(DATA_DIR / f"districts_{st_lower}_30.json", {})
        if d30.get("matches"):
            st_subflights.append({
                "flight_label": "Districts",
                "teams": d30.get("teams", []),
                "matches": d30.get("matches", []),
            })
        s30 = _load(DATA_DIR / f"standings_{st_lower}_30.json", {})
        champ_sfs = []
        for sf in s30.get("subflights", []):
            fl = sf.get("flight_label", "")
            if fl.startswith("Championships"):
                suffix = fl[len("Championships"):].strip()
                lbl = suffix if suffix else "Championships"
                champ_sfs.append({
                    "flight_label": lbl,
                    "teams": sf.get("teams", []),
                    "matches": sf.get("matches", []),
                })
        champ_sfs.sort(key=lambda sf: _champ_sort_key(sf["flight_label"]))
        st_subflights.extend(champ_sfs)

        if not st_subflights:
            continue

        active = " on" if i == 0 else ""

        re_id_prefix = f"re{st_lower}"
        st_results_html = _results_tab(st_subflights, players, sfx=_sfx,
                                       id_prefix=re_id_prefix, include_rating_toggle=False)
        results_state_btns += (
            f'<button class="state-tab{active}" '
            f'onclick="swState(\'state-{st_lower}\',this,\'results\')">{_esc(st)}</button>\n'
        )
        results_state_panes += (
            f'<div id="state-{st_lower}" class="state-pane{active}" data-group="results">'
            f'{st_results_html}</div>\n'
        )

        st_id_prefix = f"st{st_lower}"
        st_standings_html = _standings_tab(st_subflights, [], id_prefix=st_id_prefix,
                                           show_nav_links=False)
        standings_state_btns += (
            f'<button class="state-tab{active}" '
            f'onclick="swState(\'stand-state-{st_lower}\',this,\'standings\')">{_esc(st)}</button>\n'
        )
        standings_state_panes += (
            f'<div id="stand-state-{st_lower}" class="state-pane{active}" data-group="standings">'
            f'{st_standings_html}</div>\n'
        )

    # One shared rating toggle above the state tabs — setResultRatingMode()
    # already operates globally (all .rpane .prating badges), so a single
    # toggle controls every state's display at once, matching the per-state
    # dashboards where the toggle sits above the subflight tabs.
    shared_rating_toggle = (
        '<div class="re-rating-toggle">'
        '<span class="re-rtog-label">Ratings:</span>'
        '<button class="rtab" onclick="setResultRatingMode(\'none\',this)">None</button>'
        '<button class="rtab" onclick="setResultRatingMode(\'base\',this)">Base</button>'
        '<button class="rtab on" onclick="setResultRatingMode(\'new\',this)">New</button>'
        '</div>'
    )
    results_html = (
        f'{shared_rating_toggle}<div class="tabs state-tabs">{results_state_btns}</div>{results_state_panes}'
        if results_state_btns else _results_tab(all_subflights_30, players, sfx=_sfx)
    )
    standings_html = (
        f'<div class="tabs state-tabs">{standings_state_btns}</div>{standings_state_panes}'
        if standings_state_btns else _standings_tab(all_subflights_30, [])
    )

    tab_defs = [
        ("standings", "standings", standings_html),
        ("rosters", "team rosters", rosters_html),
        ("allplayers", "all players", players_html),
        ("allresults", "match results", results_html),
    ]

    tab_btns = "".join(
        f'<button class="tab{" on" if i==0 else ""}" '
        f'onclick="sw(\'{tid}\',this)">{_esc(lbl)}</button>\n'
        for i, (tid, lbl, _) in enumerate(tab_defs)
    )
    tab_panes = "".join(
        f'<div id="{tid}" class="pane{" on" if i==0 else ""}">{html}</div>\n'
        for i, (tid, _, html) in enumerate(tab_defs)
    )

    cross_link = (
        f'<a href="index.html" class="cross-link">← All States</a>'
    )

    # Summary info
    states_str = ", ".join(sorted(qualified_teams_by_state.keys()))
    n_players = len(qualified_players)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Intermountain Sectionals 2026 – 3.0 Women Scouting</title>
<style>{_CSS}</style>
</head>
<body>

<div class="top-bar">{cross_link}</div>

<div class="cards-row">
  <div class="mcard">
    <div class="mcard-label">competition</div>
    <div class="mcard-val">Sectionals</div>
    <div class="mcard-sub">Intermountain · 2026</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">states</div>
    <div class="mcard-val">{states_str}</div>
    <div class="mcard-sub">{len(qualified_teams_by_state)} qualifying teams</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">players</div>
    <div class="mcard-val">{n_players}</div>
    <div class="mcard-sub">across all qualified rosters</div>
  </div>
</div>

<div class="tabs">{tab_btns}</div>

{tab_panes}

<script>{_JS}</script>
</body>
</html>"""

    out_path = Path("sectionals_30.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"  [html] {out_path}  ({n_players} qualified players)")
    return str(out_path)


if __name__ == "__main__":
    build_dashboards()
    build_sectionals_page()
