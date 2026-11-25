from engine.crawl_history import crawl_histories, norm_name


def test_norm_name_matches_identity_keys():
    assert norm_name("Jamie O'Connell") == "jamie o connell"
    assert norm_name("Anne Stein-Gray") == "anne stein gray"
    assert norm_name("José  Núñez") == "jose nunez"


def test_punctuated_opponent_gets_rating():
    crawl = {
        "teams": {}, "champ_flights": {},
        "identity_person": {
            "therese l#A": "t", "carrie m#A": "c",
            "jamie o connell#B": "j", "anne stein gray#B": "a",
        },
        "matches": {"L1": {
            "date": "11/11/2026", "teams": ["A", "B"], "home": "A", "away": "B",
            "lines": [{"line": "#4 Doubles", "players_home": ["Therese L", "Carrie M"],
                       "players_away": ["Jamie O'Connell", "Anne Stein-Gray"],
                       "score": "6-2 7-5", "winner": "home"}],
        }},
        "people": {"t": {"name": "Therese L", "matches": [["L1", "#4 Doubles", "home"]]}},
    }
    tl = {"j": {"11/11/2026": [2.85, 2.77]}, "a": {"11/11/2026": [3.0, 2.9]}}
    row = crawl_histories(crawl, tl)["t"][0]
    assert row["opp_ids"] == ["j", "a"]
    assert row["opp_r"] == ["2.85", "3.00"]


def test_team_number_keeps_club_abbreviation():
    from engine.build_html import _abbrev_team
    assert _abbrev_team("ANTHEM CC #1") == "Anthem #1"
    assert _abbrev_team("Anthem CC") == "Anthem"
    assert _abbrev_team("LIFE TIME FITNESS/GV #2") == "LTF #2"
    assert _abbrev_team("DTC #2") == "DTC #2"
