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
DIR_YEARS = (2022, 2023, 2024, 2025, 2026, 2027)  # directory columns, oldest first


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
    years = list(DIR_YEARS)
    i_from = years.index(YEAR_FROM)
    # Column keys: s (sex), y<i> (year i), d<i> (change from year i-1 to year i), q (ShiQ)
    cols = [("s", "Sex")]
    for i, y in enumerate(years):
        if i:
            cols.append((f"d{i}", f"Δ {years[i-1]}–{str(y)[-2:]}"))
        cols.append((f"y{i}", str(y)))
    cols.append(("q", "ShiQ"))
    default_on = {k: 1 for k, _ in cols
                  if not ((k[0] == "y" and int(k[1:]) < i_from) or (k[0] == "d" and int(k[1:]) <= i_from))}
    hide_css = "\n".join(f"table.h-{k} .c-{k} {{ display: none; }}" for k, _ in cols)
    heads = ['<th data-k="n" class="c-n">Name</th>']
    for k, lbl in cols:
        if k == "s":
            heads.append('<th data-k="s" class="c-s sex">Sex</th>')
        elif k[0] == "d":
            i = int(k[1:])
            heads.append(f'<th data-k="{k}" class="c-{k} delta" title="Change from {years[i-1]} to {years[i]}">Δ</th>')
        elif k[0] == "y":
            heads.append(f'<th data-k="{k}" class="c-{k} num">{lbl}</th>')
        else:
            heads.append('<th data-k="q" class="c-q num">ShiQ</th>')
    menu = "".join(f'<label><input type="checkbox" data-c="{k}"> {_esc(lbl)}</label>' for k, lbl in cols)
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
.colbar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: .3rem;
          position: relative; max-width: 640px; }}
.count {{ font-size: 11px; color: #888; }}
.colbtn {{ padding: 4px 10px; border: 1px solid #ccc; border-radius: 20px; font: inherit; font-size: 11px;
          color: #222; background: #fff; cursor: pointer; }}
.colmenu {{ position: absolute; right: 0; top: 28px; z-index: 6; background: #fff; border-radius: 10px; padding: 4px;
           min-width: 160px; box-shadow: 0 6px 24px rgba(0,0,0,.14), 0 1px 3px rgba(0,0,0,.08); }}
.colmenu[hidden] {{ display: none; }}
.colmenu label {{ display: flex; gap: 8px; align-items: center; padding: 6px 10px; border-radius: 6px;
                 font-size: 12px; cursor: pointer; white-space: nowrap; }}
.colmenu label:hover {{ background: #f3f2ee; }}
.colmenu .reset {{ color: #1a5fb4; font-size: 11px; padding: 6px 10px; cursor: pointer; }}
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
.lt {{ font-size: 10px; color: inherit; margin-left: 2px; cursor: help; }}
.up {{ color: #27500A; }}
.caret {{ vertical-align: middle; }}
.caret.down {{ transform: scaleY(-1); }}
.down {{ color: #791F1F; }}
.more {{ margin: .8rem 0; padding: .35rem 1rem; border: 1px solid #ccc;
        border-radius: 20px; background: #fff; cursor: pointer; font-size: 12px; }}
.note {{ background: #fff8e1; border: 1px solid #f0d58a; border-radius: 6px; padding: .5rem .8rem;
        font-size: 13px; margin-bottom: .8rem; }}
{hide_css}
@media (max-width: 500px) {{ table:not(.show-s) .c-s {{ display: none; }} }}
</style></head><body><div class="wrap">
<div class="crumbs"><a href="index.html">Tennis Dashboard</a> › Nevada</div>
<h1>Nevada players</h1>
{note}
<div class="controls">
  <input type="search" id="q" placeholder="Search name…" aria-label="Search name">
  <span class="seg" id="fg" role="group" aria-label="Sex"><button class="on" data-v="">All</button><button data-v="M">M</button><button data-v="F">F</button></span>
  <select id="fr" aria-label="Rating" title="Filters on the current rating: {YEAR_TO} when published, otherwise {YEAR_FROM}"><option value="">Rating: all</option></select>
</div>
<div class="colbar"><span class="count" id="count"></span>
<button class="colbtn" id="colbtn" aria-haspopup="true" aria-expanded="false">Columns ▾</button>
<div class="colmenu" id="colmenu" hidden>{menu}<div class="reset" id="colreset">Reset</div></div></div>
<div class="tbl-wrap"><table id="tbl">
<thead><tr>{"".join(heads)}</tr></thead><tbody id="tb"></tbody></table></div>
<button class="more" id="more" hidden>Show more</button>
</div>
<script>
// rows: [name, gender, shiq, link, [level per year], [letter per year]]; YEARS gives the year order.
var ROWS = {data};
var YEARS = {json.dumps(years)};
var IFROM = {i_from};
var TYPES = {types_json};
var COLS = {json.dumps([k for k, _ in cols])};
var DEF = {json.dumps(default_on)};
var PAGE = 300, shown = PAGE, sortK = 'n', sortDir = 1, view = [], on;
var sex = '';
var LAST = YEARS.length - 1;
function cur(r) {{ return r[4][LAST] != null ? r[4][LAST] : r[4][IFROM]; }}
// Stacked carets (like a fast-forward icon turned up/down): one per 0.5 level.
function caret(up, n) {{
  var shapes = n >= 2 ? '<polygon points="0,14 5,7 10,14"/><polygon points="0,8 5,1 10,8"/>'
                      : '<polygon points="0,11 5,4 10,11"/>';
  var label = (up ? 'Up ' : 'Down ') + (n >= 2 ? 'two levels' : 'one level');
  return '<svg class="caret ' + (up ? 'up' : 'down') + '" viewBox="0 0 10 15" width="10" height="15" role="img" aria-label="'
    + label + '"><title>' + label + '</title><g fill="currentColor">' + shapes + '</g></svg>';
}}
// change from year i-1 to year i
function delta(r, i) {{ var a = r[4][i - 1], b = r[4][i]; return (a != null && b != null) ? Math.round((b - a) * 10) / 10 : 0; }}
function fmt(l) {{ return l == null ? '' : l.toFixed(1); }}
function ntrp(l, t, cls) {{
  if (l == null) return '';
  var s = fmt(l) + (t ? '<span class="lt" title="' + (TYPES[t] || t) + '">' + t + '</span>' : '');
  return cls ? '<span class="' + cls + '">' + s + '</span>' : s;
}}
function esc(s) {{ return String(s).replace(/[&<>"]/g, function(c) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
function fillSelect(id, vals, label) {{
  var el = document.getElementById(id);
  vals.forEach(function(v) {{ var o = document.createElement('option'); o.value = v; o.textContent = label(v); el.appendChild(o); }});
}}
function uniq(f) {{ var s = {{}}; ROWS.forEach(function(r) {{ var v = f(r); if (v != null && v !== '') s[v] = 1; }}); return Object.keys(s).sort(); }}
fillSelect('fr', uniq(function(r) {{ return cur(r) == null ? null : fmt(cur(r)); }}), function(v) {{ return v; }});
function val(r, k) {{
  if (k === 'n') return r[0];
  if (k === 's') return r[1];
  if (k === 'q') return r[2];
  if (k[0] === 'y') return r[4][+k.slice(1)];
  return null;
}}
function cmp(a, b) {{
  var x, y;
  if (sortK[0] === 'd') {{ var i = +sortK.slice(1); x = delta(a, i); y = delta(b, i);
    // movers first: biggest move up, then down; non-movers last
    if ((x === 0) !== (y === 0)) return x === 0 ? 1 : -1;
  }} else {{ x = val(a, sortK); y = val(b, sortK); }}
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
    var nm = r[3] ? '<a href="' + r[3] + '">' + esc(r[0]) + '</a>' : esc(r[0]);
    var h = '<tr><td class="c-n">' + nm + '</td><td class="c-s sex">' + (r[1] || '') + '</td>';
    for (var i = 0; i < YEARS.length; i++) {{
      var d = i ? delta(r, i) : 0;
      if (i) {{
        // one arrow per 0.5 level moved
        var n = Math.round(Math.abs(d) / 0.5);
        h += '<td class="c-d' + i + ' delta">' + (n ? caret(d > 0, n) : '') + '</td>';
      }}
      // color a changed rating only when its Δ column is showing
      var cls = (i && on['d' + i]) ? (d > 0 ? 'mv-up' : d < 0 ? 'mv-down' : '') : '';
      h += '<td class="c-y' + i + ' num">' + ntrp(r[4][i], r[5][i], cls) + '</td>';
    }}
    h += '<td class="c-q num">' + (r[2] == null ? '' : r[2].toFixed(2)) + '</td></tr>';
    out.push(h);
  }});
  document.getElementById('tb').innerHTML = out.join('');
  document.getElementById('count').textContent = view.length === ROWS.length
    ? ROWS.length.toLocaleString() + ' players'
    : view.length.toLocaleString() + ' of ' + ROWS.length.toLocaleString() + ' players';
  document.getElementById('more').hidden = shown >= view.length;
}}
// Columns menu: choice saved per browser
(function() {{
  var KEY = 'nvDirCols', tbl = document.getElementById('tbl'), btn = document.getElementById('colbtn'),
      menu = document.getElementById('colmenu');
  try {{ on = JSON.parse(localStorage.getItem(KEY)); }} catch (e) {{ on = null; }}
  if (!on || typeof on !== 'object') on = Object.assign({{}}, DEF);
  window.applyCols = function() {{
    COLS.forEach(function(k) {{ tbl.classList.toggle('h-' + k, !on[k]); }});
    tbl.classList.toggle('show-s', !!on.s && on._s === 1);
    menu.querySelectorAll('input').forEach(function(i) {{ i.checked = !!on[i.dataset.c]; }});
  }};
  function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(on)); }} catch (e) {{}} }}
  function setOpen(v) {{ menu.hidden = !v; btn.setAttribute('aria-expanded', v); btn.textContent = v ? 'Columns ▴' : 'Columns ▾'; }}
  btn.onclick = function(e) {{ e.stopPropagation(); setOpen(menu.hidden); }};
  menu.addEventListener('change', function(e) {{ var c = e.target.dataset.c; if (!c) return;
    on[c] = e.target.checked ? 1 : 0; if (c === 's') on._s = 1; applyCols(); save(); render(); }});
  document.getElementById('colreset').onclick = function() {{ on = Object.assign({{}}, DEF); applyCols(); render();
    try {{ localStorage.removeItem(KEY); }} catch (e) {{}} }};
  document.addEventListener('click', function(e) {{ if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) setOpen(false); }});
  applyCols();
}})();
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
    if (sortK === k) sortDir = -sortDir; else {{ sortK = k; sortDir = (k[0] === 'd' || k === 'q') ? -1 : 1; }}
    document.querySelectorAll('th').forEach(function(x) {{ x.classList.remove('sorted', 'desc'); }});
    th.classList.add('sorted'); if (sortDir < 0) th.classList.add('desc');
    apply();
  }};
}});
document.querySelector('th[data-k="n"]').classList.add('sorted');
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
    parts = name.split()
    short = f"{parts[0][0]}. {' '.join(parts[1:])}" if len(parts) > 1 else name
    label = f'<span class="fn">{_esc(name)}</span><span class="sn">{_esc(short)}</span>'
    nm = f'<a href="{_esc(href)}">{label}</a>' if href else label
    r = f' <span class="rt">{_esc(rating or "NR")}</span>' if rating is not None else ""
    return f'<span class="pn">{nm}{r}</span>'


def _mdy(d: str) -> str:
    """'2/11/2026' -> '2/11/26'; anything unparseable is returned unchanged."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", d or "")
    if not m:
        return (d or "").strip()
    return f"{int(m.group(1))}/{int(m.group(2))}/{int(m.group(3)) % 100:02d}"


_HIST_COLS = [("date", "Date"), ("div", "Div"), ("team", "Team"), ("line", "Line"), ("pit", "ShiQ"),
              ("r", "R"), ("score", "Score"), ("partner", "Partner"), ("opp", "Opponents"),
              ("oppteam", "Opp Team")]


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
        score = ('<em class="muted">default</em>' if wko else
                 " ".join(f'<span class="set">{_esc(x)}</span>' for x in (r.get("score") or "").split()))
        partners = "<br>".join(_name_link(n, (r.get("partner_r") or [None] * 9)[i], link_for)
                               for i, n in enumerate(r.get("partners") or [])) or "—"
        opps = "<br>".join(_name_link(n, (r.get("opp_r") or [None] * 9)[i], link_for)
                           for i, n in enumerate(r.get("opps") or [])) or '<em class="muted">default</em>'
        dq = r.get("dq") or []
        row_attr = f' class="dq" title="Invalid match: {_esc(", ".join(dq))} disqualified"' if dq else ""
        date = _mdy(r["date"])
        body.append(
            f'<tr data-y="{_year(r["date"])}"{row_attr}><td class="c-date">{_esc(date)}</td>'
            f'<td class="c-div"><span class="dp dp{sfx}">{_esc(r["div"])}</span></td>'
            f'<td class="c-team">{_esc(r.get("my_team", ""))}</td><td class="c-line">{pill}</td>'
            f'<td class="num c-pit">{_esc(pit(nk, r["date"], sfx))}</td>'
            f'<td class="c-r"><span class="{rcls}"{res_tip}>{res}</span></td><td class="c-score">{score}</td>'
            f'<td class="c-partner">{partners}</td><td class="c-opp">{opps}</td>'
            f'<td class="c-oppteam">{_esc(r.get("opp_team", ""))}</td></tr>')

    counts = {}
    for k in ["all"] + years:
        rows_k = history if k == "all" else [r for r in history if _year(r["date"]) == k]
        n = len(rows_k)
        counts[k] = f'{n} match{"" if n == 1 else "es"} · {_wl(compute_records(rows_k)["overall"])}'

    def _dkey(d):
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", d or "")
        return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)
    last = max((_dkey(r["date"]) for r in history), default=(0, 0, 0))
    shiq_sub = f"{last[1]}/{last[2]}/{last[0] % 100:02d}" if last[0] else "&nbsp;"

    city_state = ", ".join(x for x in [info.get("city"), info.get("state")] if x) or p.get("state", "")
    gender = {"F": "Female", "M": "Male"}.get(info.get("gender") or "F", "")
    lvl, letter = info.get("cur_level", info.get("level")), info.get("cur_type", info.get("rating_type"))
    if lvl is None:
        lvl, letter = parse_ntrp(p.get("ntrp_rating"))
    letter_tip = f' title="{_esc(RATING_TYPES.get(letter, ""))}"' if letter else ""
    usta_v = (f'{float(lvl):.1f}<span class="lt">{_esc(letter or "")}</span>' if lvl is not None else "—")
    dyn = p.get("global_rating")
    fmt2 = lambda v: f"{float(v):.2f}" if v is not None else "—"  # noqa: E731
    rdate = info.get("cur_date", info.get("rating_date")) or ""
    usta_sub = _esc(_mdy(rdate)) or "&nbsp;"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(p["name"])}</title>
<style>{_BASE_CSS}
body {{ background: #fff; }}
.phdr {{ display: flex; justify-content: flex-start; align-items: center; gap: 20px; margin-bottom: 1rem; }}
.phdr .who {{ min-width: 0; }}
.phdr h1 {{ font-size: 20px; font-weight: 600; margin-bottom: .15rem; }}
.phdr .loc {{ font-size: 12px; color: #888; }}
.cards {{ display: flex; gap: 8px; flex-wrap: nowrap; }}
.card {{ text-align: center; background: linear-gradient(180deg,#f8f7f3,#f2f1ec); border-radius: 8px; padding: .6rem .9rem;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.7); }}
.card .l {{ font-size: 11px; color: #888; }}
.card .v {{ font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.25; }}
.card .s {{ font-size: 11px; color: #888; }}
.lt {{ font-size: 10px; color: inherit; margin-left: 2px; font-weight: 400; }}
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
.sn {{ display: none; }}
.colbar {{ display: none; justify-content: space-between; align-items: center; margin: 4px 0 6px; position: relative; }}
.colbar .count {{ font-size: 11px; color: #888; }}
.colbtn {{ padding: 4px 10px; border: 1px solid #ccc; border-radius: 20px; font: inherit; font-size: 11px;
          color: #222; background: #fff; cursor: pointer; }}
.colmenu {{ position: absolute; right: 0; top: 28px; z-index: 6; background: #fff; border-radius: 10px; padding: 4px;
           min-width: 150px; box-shadow: 0 6px 24px rgba(0,0,0,.14), 0 1px 3px rgba(0,0,0,.08); }}
.colmenu[hidden] {{ display: none; }}
.colmenu label {{ display: flex; gap: 8px; align-items: center; padding: 6px 10px; border-radius: 6px;
                 font-size: 12px; cursor: pointer; }}
.colmenu label:hover {{ background: #f3f2ee; }}
.colmenu .reset {{ color: #1a5fb4; font-size: 11px; padding: 6px 10px; cursor: pointer; }}
@media (max-width: 500px) {{
  .phdr {{ gap: 12px; }}
  .phdr h1 {{ font-size: 18px; }}
  .card {{ padding: .5rem .7rem; }}
  .colbar {{ display: flex; }}
  .fn {{ display: none; }}
  .c-partner .rt {{ display: block; }}
  .c-score .set {{ display: block; }}
  .sn {{ display: inline; }}
  .tbl-wrap {{ overflow-x: visible; }}
  table.hist {{ width: 100%; }}
  .hist th, .hist td {{ padding: 5px 2px; }}
  .hist .c-date {{ padding-left: 0; }}
  .hist .c-opp, .hist .c-oppteam {{ padding-right: 0; }}
  .hist td.c-partner, .hist td.c-opp {{ white-space: normal; }}
  .hist .pn {{ white-space: normal; }}
  .hist.h-date .c-date {{ display: none; }}\n  .hist.h-div .c-div {{ display: none; }}\n  .hist.h-team .c-team {{ display: none; }}\n  .hist.h-line .c-line {{ display: none; }}\n  .hist.h-pit .c-pit {{ display: none; }}\n  .hist.h-r .c-r {{ display: none; }}\n  .hist.h-score .c-score {{ display: none; }}\n  .hist.h-partner .c-partner {{ display: none; }}\n  .hist.h-opp .c-opp {{ display: none; }}\n  .hist.h-oppteam .c-oppteam {{ display: none; }}
}}
</style></head><body><div class="wrap">
<div class="crumbs"><a href="../../index.html">Tennis Dashboard</a> › <a href="../../nv_directory.html">Nevada players</a></div>
<div class="phdr">
  <div class="who"><h1>{_esc(p["name"])}</h1>
    <div class="loc">{_esc(city_state)} · {_esc(gender)}</div></div>
  <div class="cards">
    <div class="card"><div class="l">USTA</div><div class="v"{letter_tip}>{usta_v}</div><div class="s">{usta_sub}</div></div>
    <div class="card"><div class="l">ShiQ</div><div class="v">{fmt2(dyn)}</div><div class="s">{shiq_sub}</div></div>
  </div>
</div>
<div class="recs"><span class="yrs">{toggle}</span>{strip_html}</div>
<div class="colbar"><span class="count" id="cnt">{_esc(counts["all"])}</span>
<button class="colbtn" id="colbtn" aria-haspopup="true" aria-expanded="false">Columns ▾</button>
<div class="colmenu" id="colmenu" hidden>{"".join(f'<label><input type="checkbox" data-c="{k}"> {lbl}</label>' for k, lbl in _HIST_COLS)}<div class="reset" id="colreset">Reset</div></div></div>
<div class="tbl-wrap"><table class="hist"><thead><tr>
<th class="c-date">Date</th><th class="c-div">Div</th><th class="c-team">Team</th><th class="c-line">Line</th><th class="num c-pit" title="ShiQ going into the match">ShiQ</th><th class="c-r">R</th><th class="c-score">Score</th>
<th class="c-partner">Partner</th><th class="c-opp">Opponents</th><th class="c-oppteam">Opp Team</th></tr></thead>
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
    document.getElementById('cnt').textContent = COUNTS[y] || '';
  }};
}});
var COUNTS = {json.dumps(counts, ensure_ascii=False)};
(function() {{
  var ALL = ['date','div','team','line','pit','r','score','partner','opp','oppteam'];
  var DEF = {{date:1, line:1, r:1, score:1, partner:1, opp:1}};
  var KEY = 'nvPlayerCols';
  var tbl = document.querySelector('table.hist'), btn = document.getElementById('colbtn'),
      menu = document.getElementById('colmenu'), on;
  try {{ on = JSON.parse(localStorage.getItem(KEY)); }} catch (e) {{ on = null; }}
  if (!on || typeof on !== 'object') on = Object.assign({{}}, DEF);
  function apply() {{
    ALL.forEach(function(k) {{ tbl.classList.toggle('h-' + k, !on[k]); }});
    menu.querySelectorAll('input').forEach(function(i) {{ i.checked = !!on[i.dataset.c]; }});
  }}
  function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(on)); }} catch (e) {{}} }}
  function setOpen(v) {{ menu.hidden = !v; btn.setAttribute('aria-expanded', v); btn.textContent = v ? 'Columns ▴' : 'Columns ▾'; }}
  btn.onclick = function(e) {{ e.stopPropagation(); setOpen(menu.hidden); }};
  menu.addEventListener('change', function(e) {{ var c = e.target.dataset.c; if (!c) return;
    on[c] = e.target.checked ? 1 : 0; apply(); save(); }});
  document.getElementById('colreset').onclick = function() {{ on = Object.assign({{}}, DEF); apply();
    try {{ localStorage.removeItem(KEY); }} catch (e) {{}} }};
  document.addEventListener('click', function(e) {{ if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) setOpen(false); }});
  apply();
}})();
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
    other_links = {}
    for y in DIR_YEARS:
        if y in (YEAR_FROM, YEAR_TO):
            continue
        rows_y = _usta_rows(y)
        other_links[y] = link_years(usta, rows_y) if rows_y and not placeholder else {}

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
        by_year = {YEAR_FROM: r, YEAR_TO: r27}
        for y, lk in other_links.items():
            by_year[y] = lk.get(i) or {}
        rows.append([r["name"], r.get("gender"), dyn, href,
                     [by_year[y].get("level") for y in DIR_YEARS],
                     [by_year[y].get("rating_type") for y in DIR_YEARS]])

    # Player pages
    PLAYER_DIR.mkdir(parents=True, exist_ok=True)
    page_for_name = {_nkey(p["name"]): f"{p['id']}.html" for p in with_hist.values()}

    def link_for(name: str):
        return page_for_name.get(_nkey(name))

    for pid, p in with_hist.items():
        info = info_by_id.get(pid) or {"state": STATE, "gender": "F"}
        html = _player_html(p, info, histories[_nkey(p["name"])], pit, link_for)
        (PLAYER_DIR / f"{pid}.html").write_text(html, encoding="utf-8")

    n_linked = sum(1 for r in rows if r[3])
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
