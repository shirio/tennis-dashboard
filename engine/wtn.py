"""
engine/wtn.py
Turn USTA's World Tennis Number (WTN, ITF) into an NTRP-scale number.

WTN runs backwards (lower = stronger, about 40 for a beginner down to 1 for a
pro) and has separate singles and doubles numbers. We fit a straight line
between WTN and ShiQ for NV players whose WTN comes from real match play
(source MATCHUPS, confidence >= 40), using reduced-major-axis regression so
the line works in both directions:

    WTN = A - B * NTRP      =>      NTRP = (A - WTN) / B

Singles WTN with source SEED is a placeholder USTA assigns before any
singles results, so it is ignored.

The fit constants below come from ``python3 -m engine.wtn --fit`` against
ShiQ v9 (2026-11-25). Re-run it after a new snapshot and update them.

Data: data/wtn_snapshot_*.json (newest file wins), written by
scrapers/scrape_wtn.py.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
MIN_CONFIDENCE = 40

# WTN = A - B * NTRP   (filled from --fit; see module docstring)
FIT = {
    "doubles": {"a": 46.02, "b": 6.73, "corr": 0.86, "sd": 0.21, "n": 396},
    "singles": {"a": 49.89, "b": 6.49, "corr": 0.78, "sd": 0.26, "n": 143},
}


def latest_snapshot() -> Optional[Path]:
    files = sorted((ROOT / "data").glob("wtn_snapshot_*.json"))
    return files[-1] if files else None


def load_snapshot(path: Optional[Path] = None) -> dict[str, dict]:
    """player id -> {"singles": wtn-dict|None, "doubles": wtn-dict|None, ...}.

    Only players matched to exactly one USTA account are used.
    """
    path = path or latest_snapshot()
    if not path or not path.exists():
        return {}
    d = json.loads(path.read_text())
    out = {}
    for p in d.get("players", []):
        c = p.get("candidates") or []
        if p.get("status") != "ok" or len(c) != 1 or not p.get("id"):
            continue
        c = c[0]
        out[p["id"]] = {"singles": c.get("wtn_singles"), "doubles": c.get("wtn_doubles"),
                        "ntrp": c.get("ntrp"), "snapshot_date": d.get("snapshot_date")}
    return out


def usable(w: Optional[dict]) -> bool:
    return bool(w and w.get("tennisNumber") is not None
                and w.get("source") == "MATCHUPS"
                and (w.get("confidence") or 0) >= MIN_CONFIDENCE)


def to_ntrp(wtn: float, fmt: str) -> float:
    f = FIT[fmt]
    return (f["a"] - wtn) / f["b"]


def from_ntrp(ntrp: float, fmt: str) -> float:
    f = FIT[fmt]
    return f["a"] - f["b"] * ntrp


def bands(fmt: str, levels=(2.5, 3.0, 3.5, 4.0)) -> dict[float, tuple[float, float]]:
    """NTRP level -> (weakest WTN, strongest WTN) for that level's range."""
    return {lv: (round(from_ntrp(lv - 0.5, fmt), 1), round(from_ntrp(lv, fmt), 1))
            for lv in levels}


def implied(wtn_entry: dict, fmt: str) -> Optional[dict]:
    """{"wtn", "ntrp", "confidence"} for one format, or None if not usable."""
    w = wtn_entry.get(fmt)
    if not usable(w):
        return None
    return {"wtn": w["tennisNumber"], "ntrp": round(to_ntrp(w["tennisNumber"], fmt), 2),
            "confidence": w.get("confidence")}


def apply_wtn(players: list[dict]) -> int:
    """Write p["wtn"] = {"singles": {...}|None, "doubles": {...}|None, "date"}."""
    snap = load_snapshot()
    n = 0
    for p in players:
        e = snap.get(p.get("id"))
        if not e:
            p.pop("wtn", None)
            continue
        s, d = implied(e, "singles"), implied(e, "doubles")
        if not (s or d):
            p.pop("wtn", None)
            continue
        p["wtn"] = {"singles": s, "doubles": d, "date": e.get("snapshot_date")}
        n += 1
    return n


def run_wtn() -> int:
    """Write the WTN fields onto data/players.json (called by rebuild.py)."""
    path = ROOT / "data" / "players.json"
    players = json.loads(path.read_text())
    n = apply_wtn(players)
    path.write_text(json.dumps(players, indent=2, ensure_ascii=False))
    print(f"  [wtn] {n} players with a usable WTN")
    return n


def fit(players: list[dict], fmt: str) -> dict:
    """Reduced-major-axis fit of WTN against ShiQ for one format."""
    snap = load_snapshot()
    xs, ys = [], []
    for p in players:
        e = snap.get(p.get("id"))
        if not e or not usable(e.get(fmt)):
            continue
        shiq = p.get("shiq_singles" if fmt == "singles" else "shiq_doubles")
        if shiq is None:
            continue
        xs.append(shiq)
        ys.append(e[fmt]["tennisNumber"])
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n * sx * sy)
    b = sy / sx
    a = my + b * mx
    resid = [(a - y) / b - x for x, y in zip(xs, ys)]
    sd = math.sqrt(sum(r * r for r in resid) / n)
    return {"a": round(a, 2), "b": round(b, 2), "corr": round(abs(corr), 2),
            "sd": round(sd, 2), "n": n}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--fit", action="store_true", help="refit against players.json ShiQ")
    args = ap.parse_args()
    players = json.loads((ROOT / "data" / "players.json").read_text())
    if args.fit:
        for fmt in ("doubles", "singles"):
            print(fmt, fit(players, fmt))
    for fmt in ("doubles", "singles"):
        print(fmt, {lv: b for lv, b in bands(fmt).items()})


if __name__ == "__main__":
    main()
