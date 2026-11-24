# ===========================================================================
# IMPORTANT NOTES — read before editing this file
# ===========================================================================
# 1. Builds nv_directory.html (every player on USTA's Nevada NTRP list) and
#    one page per player we have match history for: players/nv/<id>.html.
# 2. The player list comes from data/ntrp_nv_<year>.json (scraped with
#    scrapers/scrape_ntrp_list.py, parsed with scrapers/ntrp_list.py). Until
#    the 2026 list exists, the directory falls back to our own NV players.
# 3. Match history reuses build_html._traverse_match_histories and the shared
#    point-in-time rating lookup (build_html._make_pit_rating) so player pages
#    agree with the state dashboards.
# 4. These notes must be preserved unless the user explicitly says to remove them.
# ===========================================================================
"""
engine/build_directory.py
Nevada player directory + per-player match-history pages.

    from engine.build_directory import build_directory
    build_directory()          # writes nv_directory.html + players/nv/*.html
"""
from __future__ import annotations

import html as _html
import json
import re
from collections import defaultdict
from pathlib import Path

from engine.build_html import (
    _LINE_PILL_COLORS,
    _date_sort_key,
    _line_label_short,
    _make_pit_rating,
    _nkey,
    _traverse_match_histories,
)
from engine.ntrp_defs import RATING_TYPES, format_ntrp, parse_ntrp

DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"
STATE = "NV"
DIRECTORY_HTML = Path("nv_directory.html")
PLAYER_DIR = Path("players") / "nv"
YEAR_FROM = 2026
YEAR_TO = 2027


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def _load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


# ---------------------------------------------------------------------------
# Name matching (USTA list <-> our match database)
# ---------------------------------------------------------------------------

def match_key(name: str) -> str:
    """Loose key: lowercase, punctuation dropped, single spaces."""
    s = _nkey(name or "")
    s = re.sub(r"[.'’,]", "", s)
    s = re.sub(r"[-_]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def first_last_key(name: str) -> str:
    """First and last token only, so 'Shi L Oskooi' == 'Shi Oskooi'."""
    toks = match_key(name).split()
    if len(toks) < 2:
        return " ".join(toks)
    return f"{toks[0]} {toks[-1]}"


def link_usta_to_db(usta: list[dict], db_players: list[dict]) -> dict[int, dict]:
    """Map index in `usta` -> db player. Only unique matches on both sides are linked.

    Our database is women-only, so only USTA rows with gender F are candidates.
    """
    by_full: dict[str, list[dict]] = defaultdict(list)
    by_fl: dict[str, list[dict]] = defaultdict(list)
    for p in db_players:
        by_full[match_key(p["name"])].append(p)
        by_fl[first_last_key(p["name"])].append(p)

    usta_full: dict[str, list[int]] = defaultdict(list)
    usta_fl: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(usta):
        if (r.get("gender") or "F") != "F":
            continue
        usta_full[match_key(r["name"])].append(i)
        usta_fl[first_last_key(r["name"])].append(i)

    links: dict[int, dict] = {}
    used: set[str] = set()
    for k, idxs in usta_full.items():
        cands = by_full.get(k, [])
        if len(idxs) == 1 and len(cands) == 1:
            links[idxs[0]] = cands[0]
            used.add(cands[0]["id"])
    for k, idxs in usta_fl.items():
        idxs = [i for i in idxs if i not in links]
        cands = [p for p in by_fl.get(k, []) if p["id"] not in used]
        if len(idxs) == 1 and len(cands) == 1 and len(usta_fl[k]) == 1:
            links[idxs[0]] = cands[0]
            used.add(cands[0]["id"])
    return links


def link_years(rows_from: list[dict], rows_to: list[dict]) -> dict[int, dict]:
    """Map index in rows_from -> the same player's row in rows_to (name+city, then name)."""
    by_nc: dict[tuple, list[dict]] = defaultdict(list)
    by_n: dict[str, list[dict]] = defaultdict(list)
    for r in rows_to:
        by_nc[(match_key(r["name"]), (r.get("city") or "").lower(), r.get("gender"))].append(r)
        by_n[match_key(r["name"]) + "|" + (r.get("gender") or "")].append(r)
    from_n: dict[str, int] = defaultdict(int)
    for r in rows_from:
        from_n[match_key(r["name"]) + "|" + (r.get("gender") or "")] += 1
    out = {}
    for i, r in enumerate(rows_from):
        c = by_nc.get((match_key(r["name"]), (r.get("city") or "").lower(), r.get("gender")), [])
        if len(c) == 1:
            out[i] = c[0]
            continue
        nk = match_key(r["name"]) + "|" + (r.get("gender") or "")
        c = by_n.get(nk, [])
        if len(c) == 1 and from_n[nk] == 1:
            out[i] = c[0]
    return out


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _year(date: str) -> str:
    m = re.search(r"(\d{4})\s*$", date or "")
    return m.group(1) if m else ""


def _is_singles(line: str) -> bool:
    return "singles" in (line or "").lower()


def compute_records(history: list[dict]) -> dict:
    """W-L splits from a player's history. Defaults (walkovers) are not counted."""
    def wl():
        return [0, 0]
    rec = {"overall": wl(), "by_year": defaultdict(wl), "by_div": defaultdict(wl),
           "by_div_type": defaultdict(wl), "singles": wl(), "doubles": wl(),
           "singles_by_div": defaultdict(wl), "doubles_by_div": defaultdict(wl)}
    for r in history:
        if r.get("wko") or r.get("dq"):
            continue
        i = 0 if r["won"] else 1
        div = r["div"]
        kind = "singles" if _is_singles(r["line"]) else "doubles"
        rec["overall"][i] += 1
        rec["by_year"][_year(r["date"])][i] += 1
        rec["by_div"][div][i] += 1
        rec["by_div_type"][(div, kind)][i] += 1
        rec[kind][i] += 1
        rec[f"{kind}_by_div"][div][i] += 1
    return rec


def _wl(pair) -> str:
    return f"{pair[0]}–{pair[1]}"


# ---------------------------------------------------------------------------
# Shared CSS
# ---------------------------------------------------------------------------

_BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       color: #222; background: #fafafa; padding: 1.5rem 16px 3rem; }
a { color: #1a5fb4; text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1100px; margin: 0 auto; }
.crumbs { font-size: 13px; margin-bottom: .8rem; color: #666; }
h1 { font-size: 1.5rem; margin-bottom: .2rem; }
.sub { color: #666; font-size: .92rem; margin-bottom: 1.2rem; }
.line-pill { display: inline-block; padding: 1px 6px; border-radius: 8px;
             font-size: 10px; font-weight: 700; margin-right: 3px; letter-spacing: .02em; }
.pill-s1 { background: #FDEBD0; color: #A04000; }
.pill-s2 { background: #FAD7A0; color: #784212; }
.pill-s3 { background: #F9E79F; color: #7D6608; }
.pill-d1 { background: #D6EAF8; color: #1A5276; }
.pill-d2 { background: #D5F5E3; color: #1E8449; }
.pill-d3 { background: #E8DAEF; color: #6C3483; }
.dp { display: inline-block; padding: 1px 7px; border-radius: 8px; font-size: 11px; font-weight: 700; }
.dp30 { background: #ddeeff; color: #1a5a9a; }
.dp35 { background: #fff0d8; color: #8a5200; }
.up { color: #1e8449; font-weight: 700; }
.down { color: #b03a2e; font-weight: 700; }
.muted { color: #999; }
"""


# ---------------------------------------------------------------------------
# Directory page
# ---------------------------------------------------------------------------

def _directory_html(rows: list[list], placeholder: bool, n_linked: int) -> str:
    data = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    types_json = json.dumps(RATING_TYPES)
    note = ("<p class='note'>Showing our own Nevada players until the full USTA 2026 "
            "list is loaded.</p>" if placeholder else "")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nevada Players</title>
<style>{_BASE_CSS}
body {{ background: #fff; }}
h1 {{ font-size: 20px; font-weight: 600; margin-bottom: .8rem; }}
.controls {{ display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin-bottom: .6rem; }}
.controls input[type=search] {{ flex: 0 1 260px; padding: .35rem .8rem; border: 1px solid #ccc;
  border-radius: 20px; font-size: 13px; outline: none; }}
.controls input[type=search]:focus {{ border-color: #1a5fb4; }}
.controls select {{ appearance: none; -webkit-appearance: none; padding: .35rem 1.8rem .35rem .8rem;
  border: 1px solid #ccc; border-radius: 20px; font-size: 13px; color: #333; cursor: pointer;
  background: #fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23888' stroke-width='1.5'/%3E%3C/svg%3E") no-repeat right .7rem center; }}
.seg {{ display: inline-flex; gap: 2px; padding: 2px; border: 1px solid #ccc; border-radius: 20px; background: #fff; }}
.seg button {{ border: 0; background: transparent; padding: .25rem .75rem; border-radius: 16px; font-size: 13px; cursor: pointer; color: #555; }}
.seg button.on {{ background: linear-gradient(180deg,#f3f3f3,#e6e6e6); color: #222; font-weight: 600;
                 box-shadow: 0 1px 1px rgba(0,0,0,.08); }}
.mv-up {{ color: #27500A; font-weight: 600; }}
.mv-down {{ color: #791F1F; font-weight: 600; }}
.count {{ font-size: 11px; color: #888; margin-bottom: .3rem; }}
.tbl-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; font-size: 12px; }}
th, td {{ padding: 4px 10px; text-align: left; white-space: nowrap; }}
th:first-child, td:first-child {{ padding-left: 0; }}
th {{ font-size: 11px; font-weight: 600; color: #888; cursor: pointer; user-select: none;
     position: sticky; top: 0; background: #fff; }}
th.sorted::after {{ content: " ▲"; font-size: 8px; }}
th.sorted.desc::after {{ content: " ▼"; }}
tbody tr:hover td {{ background: #faf9f6; }}
th.sex, td.sex {{ text-align: center; }}
th.delta, td.delta {{ text-align: center; padding-left: 4px; padding-right: 4px; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.lt {{ font-size: 10px; color: #aaa; margin-left: 2px; cursor: help; }}
.up {{ color: #27500A; }}
.caret {{ vertical-align: middle; }}
.caret.down {{ transform: scaleY(-1); }}
.down {{ color: #791F1F; }}
.more {{ margin: .8rem 0; padding: .35rem 1rem; border: 1px solid #ccc;
        border-radius: 20px; background: #fff; cursor: pointer; font-size: 12px; }}
.note {{ background: #fff8e1; border: 1px solid #f0d58a; border-radius: 6px; padding: .5rem .8rem;
        font-size: 13px; margin-bottom: .8rem; }}
@media (max-width: 500px) {{ th.sex, td.sex {{ display: none; }} }}
</style></head><body><div class="wrap">
<div class="crumbs"><a href="index.html">Tennis Dashboard</a> › Nevada</div>
<h1>Nevada players</h1>
{note}
<div class="controls">
  <input type="search" id="q" placeholder="Search name…" aria-label="Search name">
  <span class="seg" id="fg" role="group" aria-label="Sex"><button class="on" data-v="">All</button><button data-v="M">M</button><button data-v="F">F</button></span>
  <select id="fr" aria-label="Rating" title="Filters on the current rating: {YEAR_TO} when published, otherwise {YEAR_FROM}"><option value="">Rating: all</option></select>
</div>
<div class="count" id="count"></div>
<div class="tbl-wrap"><table>
<thead><tr>
  <th data-k="0">Name</th><th data-k="1" class="sex">Sex</th><th data-k="2" class="num">{YEAR_FROM}</th>
  <th data-k="d" class="delta" title="Change from {YEAR_FROM} to {YEAR_TO}">Δ</th>
  <th data-k="4" class="num">{YEAR_TO}</th><th data-k="6" class="num">ShiQ</th>
</tr></thead><tbody id="tb"></tbody></table></div>
<button class="more" id="more" hidden>Show more</button>
</div>
<script>
// rows: [name, gender, lvl26, type26, lvl27, type27, dynamic, link]
var ROWS = {data};
var TYPES = {types_json};
var PAGE = 300, shown = PAGE, sortK = '0', sortDir = 1, view = [];
var sex = '';
function cur(r) {{ return r[4] != null ? r[4] : r[2]; }}
// Stacked carets (like a fast-forward icon turned up/down): one per 0.5 level.
function caret(up, n) {{
  var shapes = n >= 2 ? '<polygon points="0,14 5,7 10,14"/><polygon points="0,8 5,1 10,8"/>'
                      : '<polygon points="0,11 5,4 10,11"/>';
  var label = (up ? 'Up ' : 'Down ') + (n >= 2 ? 'two levels' : 'one level');
  return '<svg class="caret ' + (up ? 'up' : 'down') + '" viewBox="0 0 10 15" width="10" height="15" role="img" aria-label="'
    + label + '"><title>' + label + '</title><g fill="currentColor">' + shapes + '</g></svg>';
}}
function delta(r) {{ return (r[2] != null && r[4] != null) ? Math.round((r[4] - r[2]) * 10) / 10 : 0; }}
function fmt(l) {{ return l == null ? '' : l.toFixed(1); }}
function ntrp(l, t, cls) {{
  if (l == null) return '';
  var s = cls ? '<span class="' + cls + '">' + fmt(l) + '</span>' : fmt(l);
  if (!t) return s;
  return s + '<span class="lt" title="' + (TYPES[t] || t) + '">' + t + '</span>';
}}
function esc(s) {{ return String(s).replace(/[&<>"]/g, function(c) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
function fillSelect(id, vals, label) {{
  var el = document.getElementById(id);
  vals.forEach(function(v) {{ var o = document.createElement('option'); o.value = v; o.textContent = label(v); el.appendChild(o); }});
}}
function uniq(f) {{ var s = {{}}; ROWS.forEach(function(r) {{ var v = f(r); if (v != null && v !== '') s[v] = 1; }}); return Object.keys(s).sort(); }}
fillSelect('fr', uniq(function(r) {{ return cur(r) == null ? null : fmt(cur(r)); }}), function(v) {{ return v; }});
function cmp(a, b) {{
  var x, y;
  if (sortK === 'd') {{ x = delta(a); y = delta(b);
    // movers first: biggest move up, then down; non-movers last
    if ((x === 0) !== (y === 0)) return x === 0 ? 1 : -1;
  }} else {{ var k = +sortK; x = a[k]; y = b[k]; }}
  if (x == null && y == null) return 0;
  if (x == null) return 1;
  if (y == null) return -1;
  if (typeof x === 'string') return sortDir * x.localeCompare(y);
  return sortDir * (x - y);
}}
function apply() {{
  var q = document.getElementById('q').value.trim().toLowerCase();
  var lv = document.getElementById('fr').value;
  view = ROWS.filter(function(r) {{
    if (q && r[0].toLowerCase().indexOf(q) === -1) return false;
    if (sex && r[1] !== sex) return false;
    if (lv && fmt(cur(r)) !== lv) return false;
    return true;
  }});
  view.sort(cmp);
  shown = PAGE; render();
}}
function render() {{
  var out = [];
  view.slice(0, shown).forEach(function(r) {{
    // one arrow per 0.5 level moved: ▲ / ▲▲ up, ▼ / ▼▼ down
    var d = delta(r), n = Math.round(Math.abs(d) / 0.5), dh = '';
    if (n) dh = caret(d > 0, n);
    var nm = r[7] ? '<a href="' + r[7] + '">' + esc(r[0]) + '</a>' : esc(r[0]);
    out.push('<tr><td>' + nm + '</td><td class="sex">' + (r[1] || '') + '</td><td class="num">' + ntrp(r[2], r[3])
      + '</td><td class="delta">' + dh + '</td><td class="num">' + ntrp(r[4], r[5], d > 0 ? 'mv-up' : d < 0 ? 'mv-down' : '')
      + '</td><td class="num">' + (r[6] == null ? '' : r[6].toFixed(2)) + '</td></tr>');
  }});
  document.getElementById('tb').innerHTML = out.join('');
  document.getElementById('count').textContent = view.length === ROWS.length
    ? ROWS.length.toLocaleString() + ' players'
    : view.length.toLocaleString() + ' of ' + ROWS.length.toLocaleString() + ' players';
  document.getElementById('more').hidden = shown >= view.length;
}}
document.getElementById('more').onclick = function() {{ shown += PAGE * 3; render(); }};
document.getElementById('q').addEventListener('input', apply);
document.getElementById('fr').addEventListener('change', apply);
document.querySelectorAll('#fg button').forEach(function(b) {{
  b.onclick = function() {{
    sex = b.dataset.v;
    document.querySelectorAll('#fg button').forEach(function(x) {{ x.classList.toggle('on', x === b); }});
    apply();
  }};
}});
document.querySelectorAll('th[data-k]').forEach(function(th) {{
  th.onclick = function() {{
    var k = th.dataset.k;
    if (sortK === k) sortDir = -sortDir; else {{ sortK = k; sortDir = (k === 'd' || k === '6') ? -1 : 1; }}
    document.querySelectorAll('th').forEach(function(x) {{ x.classList.remove('sorted', 'desc'); }});
    th.classList.add('sorted'); if (sortDir < 0) th.classList.add('desc');
    apply();
  }};
}});
document.querySelector('th[data-k="0"]').classList.add('sorted');
apply();
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Player page
# ---------------------------------------------------------------------------

def _tip(label: str, value: str, tip_rows: list[tuple[str, str]]) -> str:
    rows = "".join(f"<tr><td>{_esc(a)}</td><td>{_esc(b)}</td></tr>" for a, b in tip_rows)
    tip = f'<span class="tipbox"><table>{rows}</table></span>' if tip_rows else ""
    return (f'<span class="stat" tabindex="0"><span class="stat-l">{_esc(label)}</span>'
            f'<span class="stat-v">{_esc(value)}</span>{tip}</span>')


def _record_strip(rec: dict) -> str:
    parts = []
    parts.append(_tip("Overall", _wl(rec["overall"]),
                      [(y, _wl(v)) for y, v in sorted(rec["by_year"].items())]))
    for div in sorted(rec["by_div"]):
        parts.append(_tip(div, _wl(rec["by_div"][div]),
                          [("Singles", _wl(rec["by_div_type"].get((div, "singles"), [0, 0]))),
                           ("Doubles", _wl(rec["by_div_type"].get((div, "doubles"), [0, 0])))]))
    for kind in ("singles", "doubles"):
        parts.append(_tip(kind.title(), _wl(rec[kind]),
                          [(d, _wl(v)) for d, v in sorted(rec[f"{kind}_by_div"].items())]))
    return "".join(parts)


def _name_link(name: str, rating: str, link_for) -> str:
    href = link_for(name)
    nm = f'<a href="{_esc(href)}">{_esc(name)}</a>' if href else _esc(name)
    r = f' <span class="rt">{_esc(rating or "NR")}</span>' if rating is not None else ""
    return f'<span class="pn">{nm}{r}</span>'


def _player_html(p: dict, info: dict, history: list[dict], pit, link_for) -> str:
    years = sorted({_year(r["date"]) for r in history if _year(r["date"])})
    strips = {"all": _record_strip(compute_records(history))}
    for y in years:
        strips[y] = _record_strip(compute_records([r for r in history if _year(r["date"]) == y]))
    toggle = "".join(
        f'<button class="yr{" on" if k == "all" else ""}" data-y="{k}">{k}</button>'
        for k in ["all"] + years)
    strip_html = "".join(
        f'<div class="strip" data-y="{k}"{"" if k == "all" else " hidden"}>{v}</div>'
        for k, v in strips.items())

    nk = _nkey(p["name"])
    body = []
    for r in history:
        sfx = r["div"].replace(".", "")
        lbl = _line_label_short(r["line"])
        pill = f'<span class="line-pill {_LINE_PILL_COLORS.get(lbl, "pill-d1")}">{_esc(lbl)}</span>'
        wko = r.get("wko")
        res = ("W*" if r["won"] else "L*") if wko else ("W" if r["won"] else "L")
        res_tip = ' title="Default, not counted in records"' if wko else ""
        rcls = "" if wko else ("up" if r["won"] else "down")
        score = '<em class="muted">default</em>' if wko else _esc(r.get("score", ""))
        partners = "<br>".join(_name_link(n, (r.get("partner_r") or [None] * 9)[i], link_for)
                               for i, n in enumerate(r.get("partners") or [])) or "—"
        opps = "<br>".join(_name_link(n, (r.get("opp_r") or [None] * 9)[i], link_for)
                           for i, n in enumerate(r.get("opps") or [])) or '<em class="muted">default</em>'
        dq = r.get("dq") or []
        row_attr = f' class="dq" title="Invalid match: {_esc(", ".join(dq))} disqualified"' if dq else ""
        date = re.sub(r"/\d{4}$", "", r["date"])
        body.append(
            f'<tr data-y="{_year(r["date"])}"{row_attr}><td>{_esc(date)}</td>'
            f'<td><span class="dp dp{sfx}">{_esc(r["div"])}</span></td>'
            f'<td>{_esc(r.get("my_team", ""))}</td><td>{pill}</td>'
            f'<td class="num">{_esc(pit(nk, r["date"], sfx))}</td>'
            f'<td><span class="{rcls}"{res_tip}>{res}</span></td><td>{score}</td>'
            f'<td>{partners}</td><td>{opps}</td><td>{_esc(r.get("opp_team", ""))}</td></tr>')

    city_state = ", ".join(x for x in [info.get("city"), info.get("state")] if x) or p.get("state", "")
    gender = {"F": "Female", "M": "Male"}.get(info.get("gender") or "F", "")
    lvl, letter = info.get("cur_level", info.get("level")), info.get("cur_type", info.get("rating_type"))
    if lvl is None:
        lvl, letter = parse_ntrp(p.get("ntrp_rating"))
    letter_tip = f' title="{_esc(RATING_TYPES.get(letter, ""))}"' if letter else ""
    usta_v = (f'{float(lvl):.1f}<span class="lt">{_esc(letter or "")}</span>' if lvl is not None else "—")
    base = p.get("dynamic_rating_baseline")
    dyn = p.get("global_rating")
    fmt2 = lambda v: f"{float(v):.2f}" if v is not None else "—"  # noqa: E731
    if base is not None and dyn is not None:
        d = float(dyn) - float(base)
        dcls = "mv-up" if d > 0.005 else ("mv-down" if d < -0.005 else "")
        shiq_sub = f'<span class="{dcls}">{d:+.2f}</span>'
    else:
        shiq_sub = "&nbsp;"
    usta_sub = _esc(info.get("cur_date", info.get("rating_date")) or "") or "&nbsp;"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(p["name"])}</title>
<style>{_BASE_CSS}
body {{ background: #fff; }}
.hdr {{ display: flex; flex-wrap: wrap; gap: .8rem 1.6rem; align-items: flex-end; margin-bottom: 1rem; }}
.hdr .who {{ flex: 1 1 240px; }}
.hdr h1 {{ font-size: 20px; font-weight: 600; margin-bottom: .15rem; }}
.hdr .loc {{ font-size: 12px; color: #888; }}
.cards {{ display: flex; gap: 10px; flex-wrap: wrap; }}
.card {{ text-align: center; background: linear-gradient(180deg,#f8f7f3,#f2f1ec); border-radius: 8px; padding: .6rem .9rem;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.7); }}
.card .l {{ font-size: 11px; color: #888; }}
.card .v {{ font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.25; }}
.card .s {{ font-size: 11px; color: #888; }}
.lt {{ font-size: 10px; color: #aaa; margin-left: 2px; font-weight: 400; }}
.v .lt {{ font-size: 12px; margin-left: 3px; }}
.mv-up {{ color: #27500A; font-weight: 600; }}
.mv-down {{ color: #791F1F; font-weight: 600; }}
.recs {{ display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; margin: .4rem 0 1.1rem; }}
.yrs {{ display: inline-flex; gap: 4px; margin-right: .4rem; }}
.yr {{ border: 1px solid #ccc; border-radius: 20px; background: #fff; padding: 2px 10px; font-size: 11px;
      cursor: pointer; color: #555; font-family: inherit; }}
.yr.on {{ background: linear-gradient(180deg,#f4f4f2,#e4e4e0); color: #222; border-color: #bbb; font-weight: 600; }}
.strip {{ display: flex; flex-wrap: wrap; gap: .4rem; }}
.strip[hidden] {{ display: none; }}
.stat {{ position: relative; border: 1px solid #ccc; border-radius: 20px; background: #fff;
        padding: 2px 10px; font-size: 11px; cursor: default; outline: none; }}
.stat-l {{ color: #888; margin-right: .35rem; }}
.stat-v {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
.tipbox {{ display: none; position: absolute; left: 0; top: calc(100% + 4px); z-index: 5; background: #222;
          color: #fff; border-radius: 6px; padding: .35rem .5rem; font-size: 12px; white-space: nowrap; }}
.tipbox td {{ padding: 1px 6px; }}
.tipbox td + td {{ font-weight: 700; text-align: right; }}
.stat:hover .tipbox, .stat:focus .tipbox {{ display: block; }}
.tbl-wrap {{ overflow-x: auto; }}
table.hist {{ border-collapse: collapse; font-size: 12px; }}
.hist th, .hist td {{ padding: .3rem .6rem; text-align: left; vertical-align: middle; white-space: nowrap; }}
.hist th:first-child, .hist td:first-child {{ padding-left: 0; }}
.hist th {{ font-size: 11px; color: #888; font-weight: 600; }}
.hist th.num, .hist td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.hist td.num {{ color: #aaa; }}
.hist tbody tr:hover td {{ background: #faf9f6; }}
.hist tr.dq td {{ opacity: .5; text-decoration: line-through; }}
.pn {{ white-space: nowrap; }}
.rt {{ color: #aaa; font-variant-numeric: tabular-nums; }}
</style></head><body><div class="wrap">
<div class="crumbs"><a href="../../index.html">Tennis Dashboard</a> › <a href="../../nv_directory.html">Nevada players</a></div>
<div class="hdr">
  <div class="who"><h1>{_esc(p["name"])}</h1>
    <div class="loc">{_esc(city_state)} · {_esc(gender)}</div></div>
  <div class="cards">
    <div class="card"><div class="l">Baseline</div><div class="v">{fmt2(base)}</div><div class="s">Dec 2025</div></div>
    <div class="card"><div class="l">USTA</div><div class="v"{letter_tip}>{usta_v}</div><div class="s">{usta_sub}</div></div>
    <div class="card"><div class="l">ShiQ</div><div class="v">{fmt2(dyn)}</div><div class="s">{shiq_sub}</div></div>
  </div>
</div>
<div class="recs"><span class="yrs">{toggle}</span>{strip_html}</div>
<div class="tbl-wrap"><table class="hist"><thead><tr>
<th>Date</th><th>Div</th><th>Team</th><th>Line</th><th class="num" title="Our rating going into each match">PIT</th><th>R</th><th>Score</th>
<th>Partner</th><th>Opponent(s)</th><th>Opp Team</th></tr></thead>
<tbody>{"".join(body)}</tbody></table></div>
</div>
<script>
document.querySelectorAll('.yr').forEach(function(b) {{
  b.onclick = function() {{
    var y = b.dataset.y;
    document.querySelectorAll('.yr').forEach(function(x) {{ x.classList.toggle('on', x === b); }});
    document.querySelectorAll('.strip').forEach(function(s) {{ s.hidden = s.dataset.y !== y; }});
    document.querySelectorAll('.hist tbody tr').forEach(function(tr) {{
      tr.hidden = y !== 'all' && tr.dataset.y !== y; }});
  }};
}});
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _usta_rows(year: int) -> list[dict]:
    rows = _load(DATA_DIR / f"ntrp_{STATE.lower()}_{year}.json", [])
    for r in rows:
        r["level"] = float(r["level"]) if r.get("level") else None
    return rows


def build_directory() -> dict:
    players = _load(PLAYERS_JSON, [])
    db = [p for p in players if p.get("state") == STATE and p.get("id")]

    s30 = _load(DATA_DIR / f"standings_{STATE.lower()}_30.json", {"subflights": []})
    s35 = _load(DATA_DIR / f"standings_{STATE.lower()}_35.json", {"subflights": []})
    histories, _ = _traverse_match_histories(db, "3.0", s30.get("subflights", []),
                                             s35.get("subflights", []), state_code=STATE)
    pit = _make_pit_rating(db, "30", "35")
    with_hist = {p["id"]: p for p in db if histories.get(_nkey(p["name"]))}

    usta = _usta_rows(YEAR_FROM)
    placeholder = not usta
    if placeholder:
        usta = []
        for p in db:
            lvl, letter = parse_ntrp(p.get("ntrp_rating"))
            usta.append({"name": p["name"], "gender": "F", "level": lvl, "rating_type": letter,
                         "city": None, "state": STATE, "_db": p})
        links = {i: r["_db"] for i, r in enumerate(usta)}
    else:
        links = link_usta_to_db(usta, db)
    nxt = _usta_rows(YEAR_TO)
    year_links = link_years(usta, nxt) if nxt else {}

    info_by_id: dict[str, dict] = {}
    rows = []
    for i, r in enumerate(usta):
        p = links.get(i)
        href = None
        dyn = None
        if p and p["id"] in with_hist:
            href = f"{PLAYER_DIR.as_posix()}/{p['id']}.html"
            dyn = round(float(p["global_rating"]), 4) if p.get("global_rating") is not None else None
        r27 = year_links.get(i) or {}
        if href:
            info = dict(r)
            if r27.get("level") is not None:
                info.update(cur_level=r27["level"], cur_type=r27.get("rating_type"), cur_year=YEAR_TO,
                            cur_date=r27.get("rating_date"))
            else:
                info["cur_year"] = YEAR_FROM if not placeholder else None
            info_by_id[p["id"]] = info
        rows.append([r["name"], r.get("gender"), r.get("level"), r.get("rating_type"),
                     r27.get("level"), r27.get("rating_type"), dyn, href])

    # Player pages
    PLAYER_DIR.mkdir(parents=True, exist_ok=True)
    page_for_name = {_nkey(p["name"]): f"{p['id']}.html" for p in with_hist.values()}

    def link_for(name: str):
        return page_for_name.get(_nkey(name))

    for pid, p in with_hist.items():
        info = info_by_id.get(pid) or {"state": STATE, "gender": "F"}
        html = _player_html(p, info, histories[_nkey(p["name"])], pit, link_for)
        (PLAYER_DIR / f"{pid}.html").write_text(html, encoding="utf-8")

    n_linked = sum(1 for r in rows if r[7])
    DIRECTORY_HTML.write_text(_directory_html(rows, placeholder, n_linked), encoding="utf-8")
    unlinked = [p["name"] for pid, p in with_hist.items() if pid not in info_by_id]
    print(f"  [html] {DIRECTORY_HTML}  ({len(rows)} players, {n_linked} linked"
          f"{', placeholder list' if placeholder else ''})")
    print(f"  [html] {PLAYER_DIR}/  ({len(with_hist)} player pages)")
    if not placeholder and unlinked:
        print(f"  [warn] {len(unlinked)} players with match history not found on the USTA list")
    return {"rows": len(rows), "linked": n_linked, "pages": len(with_hist),
            "unlinked": unlinked, "placeholder": placeholder}


if __name__ == "__main__":
    build_directory()
