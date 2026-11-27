"""Past directory years come from TennisLink rosters; a mid-year move up is a D."""
from engine import build_directory as BD


def _crawl():
    url = "https://tennislink.usta.com/x?par1=AbC%2fd%3d%3d&e=1"
    return {
        "people": {"p": {"name": "Courtney Long", "area": "NEVADA", "history_url": url,
                         "teams": ["NV|SPRING|2.5|RR", "NV|FALL|3.0|RR", "CHAMP|x"]}},
        "teams": {"NV|SPRING|2.5|RR": {"roster": [{"name": "Courtney Long", "ntrp": "2.5"}]},
                  "NV|FALL|3.0|RR": {"roster": [{"name": "Courtney Long", "ntrp": "3"}]}},
        "matches": {"a": {"date": "1/10/2024", "teams": ["NV|SPRING|2.5|RR"]},
                    "b": {"date": "9/21/2024", "teams": ["NV|FALL|3.0|RR"]}},
    }


def test_latest_roster_level_and_d(monkeypatch):
    import engine.crawl_events as C
    monkeypatch.setattr(C, "load_crawl", lambda year: _crawl())
    BD._TL_YEAR.clear()
    out = BD._tl_year_levels(2024)
    assert out["AbC/d=="] == {"level": 3.0, "rating_type": "D"}
    BD._TL_YEAR.clear()
