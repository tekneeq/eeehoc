"""Live chiclet feed: clock, scoreboard normalisation, cache, dashboard."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

def _home_bos(score="2"):
    return {
        "homeAway": "home",
        "score": score,
        "team": {
            "id": "1",
            "abbreviation": "BOS",
            "displayName": "Boston Bruins",
            "shortDisplayName": "Bruins",
            "location": "Boston",
            "color": "231f20",
            "logo": "https://a.espncdn.com/i/teamlogos/nhl/500/bos.png",
        },
        "records": [{"name": "YTD", "type": "ytd", "summary": "2-0-0"}],
        "linescores": [{"value": 1.0, "displayValue": "1"}, {"value": 1.0, "displayValue": "1"}],
        "leaders": [
            {
                "name": "goals",
                "leaders": [
                    {
                        "displayValue": "1",
                        "athlete": {"shortName": "M. Eyssimont", "position": {"abbreviation": "C"}, "team": {"id": "1"}},
                        "team": {"id": "1"},
                    }
                ],
            }
        ],
    }


def _away_phi(score="1"):
    return {
        "homeAway": "away",
        "score": score,
        "team": {
            "id": "15",
            "abbreviation": "PHI",
            "shortDisplayName": "Flyers",
            "color": "F74902",
            "logo": "https://a.espncdn.com/i/teamlogos/nhl/500/phi.png",
        },
        "records": [{"type": "total", "summary": "0-2-0"}],
        "linescores": [{"value": 1}, {"value": 0}],
    }


LIVE_SCOREBOARD = {
    "leagues": [
        {
            "season": {
                "year": 2027,
                "displayName": "2026-27",
                "type": {"id": "2", "type": 2, "name": "Regular Season", "abbreviation": "reg"},
            }
        }
    ],
    "season": {"year": 2027, "type": 2},
    "day": {"date": "2026-09-30"},
    "events": [
        {
            "id": "900",
            "date": "2026-09-30T23:00Z",
            "name": "Dallas Stars at Colorado Avalanche",
            "shortName": "DAL @ COL",
            "competitions": [
                {
                    "id": "900",
                    "date": "2026-09-30T23:00Z",
                    "venue": {"fullName": "Ball Arena"},
                    "broadcasts": [{"names": ["ESPN"]}],
                    "status": {
                        "clock": 0.0,
                        "displayClock": "0:00",
                        "period": 0,
                        "type": {
                            "state": "pre",
                            "completed": False,
                            "name": "STATUS_SCHEDULED",
                            "shortDetail": "9/30 - 7:00 PM EDT",
                        },
                    },
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "0",
                            "team": {
                                "id": "17",
                                "abbreviation": "COL",
                                "displayName": "Colorado Avalanche",
                                "shortDisplayName": "Avalanche",
                                "color": "6F263D",
                                "logo": "https://a.espncdn.com/i/teamlogos/nhl/500/col.png",
                            },
                            "records": [{"type": "total", "summary": "0-0-0"}],
                            "leaders": [
                                {
                                    "name": "goals",
                                    "leaders": [
                                        {"displayValue": "12", "athlete": {"shortName": "N. MacKinnon"}, "team": {"id": "17"}}
                                    ],
                                }
                            ],
                            "probables": [
                                {
                                    "name": "probableStartingGoalie",
                                    "athlete": {"shortName": "A. Georgiev"},
                                }
                            ],
                        },
                        {
                            "homeAway": "away",
                            "score": "0",
                            "team": {
                                "id": "9",
                                "abbreviation": "DAL",
                                "shortDisplayName": "Stars",
                                "color": "006847",
                                "logo": "https://a.espncdn.com/i/teamlogos/nhl/500/dal.png",
                            },
                            "records": [{"type": "ytd", "summary": "0-0-0"}],
                            "probables": [
                                {"name": "probableStartingGoalie", "athlete": {"shortName": "J. Oettinger"}}
                            ],
                        },
                    ],
                }
            ],
        },
        {
            "id": "401",
            "date": "2026-09-30T23:30Z",
            "name": "Philadelphia Flyers at Boston Bruins",
            "shortName": "PHI @ BOS",
            "competitions": [
                {
                    "id": "401",
                    "venue": {"fullName": "TD Garden"},
                    "broadcasts": [{"names": ["TNT"]}, {"names": ["truTV"]}],
                    "notes": [{"headline": "Preseason"}],
                    "status": {
                        "clock": 720.0,
                        "displayClock": "12:00",
                        "period": 2,
                        "type": {
                            "state": "in",
                            "completed": False,
                            "name": "STATUS_IN_PROGRESS",
                            "shortDetail": "2nd - 12:00",
                        },
                    },
                    "situation": {
                        "lastPlay": {
                            "type": {"text": "Shot"},
                            "text": "Nikita Zadorov Wrist Shot saved by Dan Vladar",
                            "scoreValue": 0,
                            "team": {"id": "1"},
                            "period": {"number": 2},
                            "clock": {"displayValue": "8:00"},
                            "coordinate": {"x": 74, "y": 6},
                        }
                    },
                    "competitors": [_home_bos(), _away_phi()],
                }
            ],
        },
        {
            "id": "402",
            "date": "2026-09-30T17:00Z",
            "name": "Toronto Maple Leafs at Montreal Canadiens",
            "shortName": "TOR @ MTL",
            "competitions": [
                {
                    "id": "402",
                    "venue": {"fullName": "Bell Centre"},
                    "broadcast": "ESPN+",
                    "status": {
                        "clock": 0.0,
                        "displayClock": "0:00",
                        "period": 3,
                        "type": {"state": "post", "completed": True, "name": "STATUS_FINAL", "shortDetail": "Final"},
                        "featuredAthletes": [
                            {
                                "name": "firstStar",
                                "athlete": {"shortName": "A. Matthews", "position": "C", "team": {"id": "10"}},
                                "team": {"id": "10"},
                                "statistics": [
                                    {"name": "goals", "displayValue": "2"},
                                    {"name": "assists", "displayValue": "1"},
                                ],
                            }
                        ],
                    },
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "2",
                            "team": {"id": "8", "abbreviation": "MTL", "shortDisplayName": "Canadiens", "color": "AF1E2D"},
                            "linescores": [{"value": 1}, {"value": 0}, {"value": 1}],
                            "records": [{"type": "ytd", "summary": "0-1-0"}],
                        },
                        {
                            "homeAway": "away",
                            "score": "4",
                            "team": {"id": "10", "abbreviation": "TOR", "shortDisplayName": "Maple Leafs", "color": "00205B"},
                            "linescores": [{"value": 2}, {"value": 1}, {"value": 1}],
                            "records": [{"type": "ytd", "summary": "1-0-0"}],
                        },
                    ],
                }
            ],
        },
    ],
}


LIVE_SUMMARY_401 = {
    "boxscore": {
        "teams": [
            {
                "homeAway": "home",
                "team": {"id": "1"},
                "statistics": [
                    {"name": "shotsTotal", "displayValue": "26"},
                    {"name": "hits", "displayValue": "18"},
                    {"name": "blockedShots", "displayValue": "9"},
                    {"name": "faceoffsWon", "displayValue": "22"},
                    {"name": "faceoffPercent", "displayValue": "55.0"},
                    {"name": "powerPlayGoals", "displayValue": "1"},
                    {"name": "powerPlayOpportunities", "displayValue": "3"},
                    {"name": "penaltyMinutes", "displayValue": "4"},
                    {"name": "giveaways", "displayValue": "6"},
                    {"name": "takeaways", "displayValue": "5"},
                ],
            },
            {
                "homeAway": "away",
                "team": {"id": "15"},
                "statistics": [
                    {"name": "shotsTotal", "displayValue": "14"},
                    {"name": "hits", "displayValue": "11"},
                    {"name": "powerPlayGoals", "displayValue": "0"},
                    {"name": "powerPlayOpportunities", "displayValue": "1"},
                    {"name": "faceoffsWon", "displayValue": "18"},
                    {"name": "faceoffPercent", "displayValue": "45.0"},
                ],
            },
        ],
        "players": [
            {
                "team": {"id": "1"},
                "statistics": [
                    {
                        "name": "goalies",
                        "labels": ["GA", "SA", "SV", "SV%", "TOI"],
                        "athletes": [
                            {"athlete": {"shortName": "J. Korpisalo"}, "stats": ["0", "4", "4", "1.000", "8:00"]},
                            {"athlete": {"shortName": "J. Swayman"}, "stats": ["1", "15", "14", ".933", "32:00"]},
                            {"athlete": {"shortName": "B. Bussi"}, "stats": ["0", "0", "0", ".000", "0:00"]},
                        ],
                    }
                ],
            }
        ],
    },
    "plays": [
        {
            "type": {"text": "Goal"},
            "text": "Michael Eyssimont Goal (1) Tip-In, assists: Frederic Brunet (1), Tanner Jeannot (1)",
            "scoringPlay": True,
            "scoreValue": 1,
            "period": {"number": 1, "displayValue": "1st"},
            "clock": {"displayValue": "13:31"},
            "team": {"id": "1"},
            "participants": [
                {"type": "scorer", "ytdGoals": 1, "athlete": {"shortName": "M. Eyssimont"}},
                {"type": "assister", "athlete": {"shortName": "F. Brunet"}},
                {"type": "assister", "athlete": {"shortName": "T. Jeannot"}},
            ],
            "strength": {"abbreviation": "power-play", "text": "Power Play"},
            "coordinate": {"x": 80, "y": -4},
        },
        {
            "type": {"text": "Shot"},
            "text": "Nikita Zadorov Wrist Shot saved by Dan Vladar",
            "scoringPlay": False,
            "period": {"number": 2},
            "clock": {"displayValue": "8:00"},
            "team": {"id": "1"},
            "coordinate": {"x": -56, "y": 22},
            "strength": {"abbreviation": "even-strength"},
        },
        {"type": {"text": "Stoppage"}, "text": "Icing"},
    ],
    "onIce": [
        {"teamId": "1", "entries": [{"whereabouts": {"id": "1", "description": "In Play"}}] * 6},
        {"teamId": "15", "entries": [{"whereabouts": {"description": "In Play"}}] * 5},
    ],
    "leaders": [
        {
            "team": {"id": "1"},
            "leaders": [
                {
                    "name": "points",
                    "leaders": [{"displayValue": "2", "athlete": {"shortName": "M. Eyssimont", "team": {"id": "1"}}}],
                }
            ],
        }
    ],
}


def _fake_fetch(calls: list[str]):
    def fetch(url: str) -> dict:
        calls.append(url)
        if "scoreboard" in url and "summary" not in url:
            return LIVE_SCOREBOARD
        if "event=401" in url:
            return LIVE_SUMMARY_401
        if "event=402" in url:
            return {"boxscore": {"teams": []}, "plays": []}
        raise RuntimeError(f"unexpected {url}")

    return fetch


def test_elapsed_minute_and_goal_clock():
    from eeehoc.live import elapsed_minute, period_name, play_game_seconds, remaining_from_elapsed, scoreboard_game_seconds

    assert elapsed_minute(1, 1200) == 1
    assert elapsed_minute(1, 0) == 20
    assert elapsed_minute(2, 720) == 28  # 2nd, 12:00 left
    assert elapsed_minute(3, 0) == 60
    assert elapsed_minute(4, 120) == 60  # OT clamps
    assert period_name(1) == "1st"
    assert period_name(4) == "OT"
    assert period_name(5) == "SO"
    # 13:31 elapsed in the 1st is 6:29 remaining
    assert remaining_from_elapsed("13:31", 1) == "6:29"
    assert remaining_from_elapsed("3:12", 4, 300) == "1:48"
    assert remaining_from_elapsed("0:40", 5) == "SO"
    assert remaining_from_elapsed("8:00", 5, playoffs=True) == "12:00"
    assert play_game_seconds("13:31", 1) == 13 * 60 + 31
    assert play_game_seconds("8:00", 2) == 20 * 60 + 8 * 60
    assert play_game_seconds("1:12", 4, ot_seconds=300) == 60 * 60 + 72
    assert play_game_seconds("0:40", 5) is None
    assert scoreboard_game_seconds(2, 720) == 28 * 60
    assert scoreboard_game_seconds(4, 0, ot_seconds=300) == 65 * 60


def test_period_labels_final_ot_and_shootout():
    from eeehoc.live import _period_label

    live = {"period": 2, "displayClock": "12:00", "type": {"name": "STATUS_IN_PROGRESS", "state": "in"}}
    assert _period_label(live, "in") == "2nd 12:00"
    end = {"period": 1, "displayClock": "0:00", "type": {"name": "STATUS_END_PERIOD"}}
    assert _period_label(end, "in") == "End 1st"
    final = {"period": 3, "type": {"name": "STATUS_FINAL"}}
    assert _period_label(final, "post") == "Final"
    ot = {"period": 4, "type": {"name": "STATUS_FINAL_OT"}}
    assert _period_label(ot, "post") == "Final/OT"
    so = {"period": 5, "type": {"name": "STATUS_FINAL_SHOOTOUT"}}
    assert _period_label(so, "post") == "Final/SO"
    playoff = {"period": 5, "displayClock": "12:40", "type": {"name": "STATUS_IN_PROGRESS"}}
    assert _period_label(playoff, "in", playoffs=True) == "2OT 12:40"
    sched = {"period": 0, "type": {"name": "STATUS_SCHEDULED", "shortDetail": "9/30 - 7:30 PM EDT"}}
    assert _period_label(sched, "pre") == "9/30 - 7:30 PM EDT"


def test_normalize_scoreboard_orders_live_first():
    from eeehoc.live import normalize_scoreboard

    board = normalize_scoreboard(LIVE_SCOREBOARD)
    assert board["season"] == 2027
    assert board["season_label"] == "2026-27"
    assert board["season_type_name"] == "Regular Season"
    assert board["day"] == "2026-09-30"
    assert board["ot_seconds"] == 300
    assert [g["state"] for g in board["games"]] == ["in", "pre", "post"]

    live = board["games"][0]
    assert live["short_name"] == "PHI @ BOS"
    assert live["period_label"] == "2nd 12:00"
    assert live["minute"] == 28
    assert live["elapsed_sec"] == 28 * 60
    assert live["broadcast"] == "TNT · truTV"
    assert live["notes"] == ["Preseason"]
    assert live["home"]["abbr"] == "BOS"
    assert live["home"]["record"] == "2-0-0"
    assert live["home"]["linescores"] == [1, 1]
    assert live["home"]["leaders"]["goals"]["name"] == "M. Eyssimont"
    assert live["away"]["record"] == "0-2-0"
    sit = live["situation"]
    assert sit["last_play"]["type"] == "Shot"
    assert sit["last_play"]["team"] == "home"
    assert sit["last_play"]["x"] == 74
    assert sit["last_play"]["y"] == 6

    upcoming = board["games"][1]
    assert upcoming["minute"] is None
    assert upcoming["period_label"] == "9/30 - 7:00 PM EDT"
    assert upcoming["home"]["goalie"] == "A. Georgiev"
    assert upcoming["home"]["leaders"] == {}
    assert upcoming["away"]["leaders"] == {}
    assert upcoming["away"]["goalie"] == "J. Oettinger"
    assert upcoming["situation"] is None

    final = board["games"][2]
    assert final["period_label"] == "Final"
    assert final["minute"] == 60
    assert final["situation"] is None
    assert final["away"]["winner"] and not final["home"]["winner"]
    assert final["stars"][0]["name"] == "A. Matthews"
    assert final["stars"][0]["line"] == "2 G · 1 A"
    assert final["stars"][0]["side"] == "away"
    assert final["broadcast"] == "ESPN+"


def test_apply_summary_goals_goalies_and_power_play():
    from eeehoc.live import apply_summary, normalize_scoreboard

    game = normalize_scoreboard(LIVE_SCOREBOARD)["games"][0]
    apply_summary(game, LIVE_SUMMARY_401)

    assert game["home"]["stats"]["shots"] == "26"
    assert game["home"]["stats"]["power_play"] == "1/3"
    assert game["away"]["stats"]["shots"] == "14"
    assert game["away"]["stats"]["power_play"] == "0/1"
    assert [g["name"] for g in game["home"]["goalies"]] == ["J. Swayman", "J. Korpisalo"]
    assert game["home"]["goalies"][0]["saves"] == "14"
    assert game["home"]["goalies"][0]["save_pct"] == ".933"
    assert game["home"]["leaders"]["points"]["line"] == "2"

    goal = game["goals"][0]
    assert goal["when"] == "1st 6:29"
    assert goal["at"] == 13 * 60 + 31
    assert goal["score"] == "0-1"
    assert goal["side"] == "home"
    assert goal["scorer"] == "M. Eyssimont (1)"
    assert goal["assists"] == ["F. Brunet", "T. Jeannot"]
    assert goal["tag"] == "PP"

    sit = game["situation"]
    assert sit["power_play"] == "home"
    assert sit["strength"] == "5-on-4"
    assert sit["home_on_ice"] == 6
    assert sit["away_on_ice"] == 5
    assert sit["last_play"]["type"] == "Shot"
    assert sit["last_play"]["x"] == -56

    final = normalize_scoreboard(LIVE_SCOREBOARD)["games"][2]
    apply_summary(final, {"boxscore": {"teams": []}, "plays": []})
    assert final["situation"] is None
    assert final["goals"] == []

    # Goal in the 1st and a saved shot in the 2nd, both by the home team.
    # The live game is in the 2nd, so both periods are listed.
    assert [(row["label"], row["away_sog"], row["home_sog"], row["away_shots"], row["home_shots"]) for row in game["period_shots"]] == [
        ("1", 0, 1, 0, 1),
        ("2", 0, 1, 0, 1),
    ]


def test_period_shots_count_missed_and_blocked_for_the_shooter():
    from eeehoc.live import period_shot_lines

    plays = [
        {"type": {"text": "Goal"}, "period": {"number": 1}, "team": {"id": "10"}},
        {"type": {"text": "Shot"}, "period": {"number": 1}, "team": {"id": "10"}},
        {"type": {"text": "Missed"}, "period": {"number": 1}, "team": {"id": "10"}},
        # ESPN credits the blocker. The attempt belongs to the other team.
        {"type": {"text": "Blocked"}, "period": {"number": 1}, "team": {"id": "8"}},
        {"type": {"text": "Shot"}, "period": {"number": 2}, "team": {"id": "8"}},
        {"type": {"text": "Shot"}, "period": {"number": 5}, "team": {"id": "10"}},
    ]
    rows = period_shot_lines(plays, "8", "10", periods=3, playoffs=False)
    by_label = {row["label"]: row for row in rows}
    assert list(by_label) == ["1", "2", "3"]
    assert rows[0]["away_sog"] == 2
    assert rows[0]["away_shots"] == 4  # 2 on goal + missed + the home block
    assert rows[0]["home_sog"] == 0
    assert rows[0]["home_shots"] == 0
    assert rows[1] == {"period": 2, "label": "2", "away_sog": 0, "home_sog": 1, "away_shots": 0, "home_shots": 1}
    assert rows[2]["away_shots"] == 0 and rows[2]["home_shots"] == 0

    playoff = period_shot_lines(
        [{"type": {"text": "Goal"}, "period": {"number": 5}, "team": {"id": "10"}}],
        "8",
        "10",
        periods=5,
        playoffs=True,
    )
    assert [row["label"] for row in playoff] == ["1", "2", "3", "OT", "2OT"]
    assert playoff[-1]["away_sog"] == 1


def test_live_feed_caches_and_falls_back_to_stale(monkeypatch):
    from eeehoc import live as live_mod
    from eeehoc.live import LiveFeed

    calls: list[str] = []
    feed = LiveFeed(_fake_fetch(calls), ttl=100)
    board = feed.get()
    assert board["counts"] == {"live": 1, "final": 1, "upcoming": 1}
    assert sum("scoreboard" in u and "dates=" not in u for u in calls) == 1
    assert sum("event=" in u for u in calls) == 2

    feed.get()
    assert sum("scoreboard" in u for u in calls) == 1

    monkeypatch.setattr(live_mod, "FORCE_MIN_INTERVAL", 0.0)
    feed.get(force=True)
    assert sum("scoreboard" in u for u in calls) == 2

    feed.get(dates="20260922")
    assert any("dates=20260922" in u for u in calls)

    try:
        feed.get(dates="yesterday")
    except ValueError as exc:
        assert "YYYYMMDD" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected bad dates to raise")

    def boom(url: str) -> dict:
        raise RuntimeError("espn down")

    feed._fetch = boom  # noqa: SLF001
    feed._boards[""] = (0.0, feed._boards[""][1])  # noqa: SLF001 - expire cache
    stale = feed.get()
    assert stale["stale"] is True
    assert "espn down" in stale["error"]
    assert stale["counts"]["live"] == 1

    empty = LiveFeed(boom)
    try:
        empty.get()
    except RuntimeError as exc:
        assert "espn down" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected first-fetch failure to raise")


def test_dashboard_live_api():
    from eeehoc.dashboard import DashboardState, make_handler
    from eeehoc.live import LiveFeed

    state = DashboardState(live=LiveFeed(_fake_fetch([])))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        board = json.loads(urllib.request.urlopen(base + "/api/live", timeout=5).read())
        assert board["season_label"] == "2026-27"
        assert len(board["games"]) == 3
        live = board["games"][0]
        assert live["situation"]["strength"] == "5-on-4"
        assert live["goals"][0]["when"] == "1st 6:29"
        assert live["home"]["stats"]["shots"] == "26"
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert 'id="liveGrid"' in html
        assert 'id="liveSort"' in html and 'data-sort="clock"' in html and 'id="liveCollapse"' in html
        script = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert "chiclet" in script
        assert all(
            token in script
            for token in (
                'data-act="collapse"',
                'data-act="drag"',
                "elapsed_sec",
                "eeehoc.order.",
                "netminder",
                "goalie_rank",
                "goalie_depth",
                "starterCaption",
            )
        )
        css = urllib.request.urlopen(base + "/static/app.css", timeout=5).read().decode()
        assert ".rink .netminder" in css
        assert ".rink .netminder.meta" in css
        assert urllib.request.urlopen(base + "/health", timeout=5).read() == b"ok\n"

        try:
            urllib.request.urlopen(base + "/api/live?dates=nope", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            assert "YYYYMMDD" in json.loads(exc.read())["error"]
        else:  # pragma: no cover
            raise AssertionError("expected 400")

        dated = json.loads(urllib.request.urlopen(base + "/api/live?dates=20260922", timeout=5).read())
        assert dated["dates"] == "20260922"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_dashboard_live_outage_is_502():
    from eeehoc.dashboard import DashboardState, make_handler
    from eeehoc.live import LiveFeed

    def boom(url: str) -> dict:
        raise RuntimeError("espn down")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DashboardState(live=LiveFeed(boom))))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/live", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 502
        assert "espn down" in json.loads(exc.read())["error"]
    else:  # pragma: no cover
        raise AssertionError("expected 502")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_rank_label_depth_and_season_starts():
    from eeehoc.live import depth_by_starts, goalie_rates, rank_label, roster_goalies, season_starts

    assert rank_label("8th", 8) == "#8"
    assert rank_label("Tied-1st", 1) == "T1"
    assert rank_label("Tied-2nd", None) == "T2"
    assert rank_label("", None) == ""
    assert rank_label("14th", "14") == "#14"

    assert goalie_rates(
        {
            "athlete": {
                "statsSummary": {
                    "statistics": [
                        {"name": "savePct", "displayValue": ".932", "rank": 8, "rankDisplayValue": "8th"},
                        {"name": "avgGoalsAgainst", "displayValue": "1.98", "rank": 14, "rankDisplayValue": "14th"},
                    ]
                }
            }
        }
    ) == ("#8", ".932", "1.98")
    assert goalie_rates(None) == ("", "", "")
    assert goalie_rates({"athlete": {}}) == ("", "", "")

    stats = {
        "categories": [
            {
                "names": ["games", "gameStarted", "wins"],
                "statistics": [
                    {"season": {"year": 2026}, "stats": ["10", "9", "4"]},
                    {"season": {"year": 2027}, "stats": ["2", "2", "1"]},
                    {"season": {"year": 2027}, "stats": ["3", "1", "1"]},
                ],
            }
        ]
    }
    assert season_starts(stats, 2027) == (5, 3)
    assert season_starts(stats, 2025) == (0, 0)
    assert season_starts(None, 2027) is None
    assert season_starts({"categories": []}, 2027) == (0, 0)

    assert roster_goalies(
        {
            "athletes": [
                {"position": "Centers", "items": [{"id": "9", "shortName": "N. Someone"}]},
                {
                    "position": "Goalies",
                    "items": [
                        {"id": "4392765", "shortName": "M. DiPietro"},
                        {"id": "4712036", "displayName": "Jeremy Swayman"},
                    ],
                },
            ]
        }
    ) == [
        {"id": "4392765", "name": "M. DiPietro"},
        {"id": "4712036", "name": "Jeremy Swayman"},
    ]
    assert depth_by_starts(
        [
            {"id": "sway", "name": "J. Swayman", "starts": 5, "games": 5},
            {"id": "dip", "name": "M. DiPietro", "starts": 0, "games": 0},
        ]
    ) == {"sway": 1, "dip": 2}
    # Same starts and games: name breaks the tie, so the chart is stable.
    assert depth_by_starts(
        [
            {"id": "b", "name": "B", "starts": 3, "games": 4},
            {"id": "a", "name": "A", "starts": 3, "games": 4},
        ]
    ) == {"a": 1, "b": 2}


def _goalie_board():
    return {
        "leagues": [
            {
                "season": {
                    "year": 2027,
                    "displayName": "2026-27",
                    "type": {"type": 2, "name": "Regular Season"},
                }
            }
        ],
        "events": [
            {
                "id": "1",
                "date": "2026-10-11T23:00Z",
                "shortName": "PHI @ BOS",
                "competitions": [
                    {
                        "status": {
                            "clock": 720,
                            "displayClock": "12:00",
                            "period": 2,
                            "type": {"state": "in", "shortDetail": "2nd"},
                        },
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "2",
                                "team": {"id": "1", "abbreviation": "BOS", "shortDisplayName": "Bruins"},
                                "records": [{"type": "ytd", "summary": "4-2-0"}],
                            },
                            {
                                "homeAway": "away",
                                "score": "1",
                                "team": {"id": "15", "abbreviation": "PHI", "shortDisplayName": "Flyers"},
                                "records": [{"type": "ytd", "summary": "0-4-2"}],
                            },
                        ],
                    }
                ],
            },
            {
                "id": "2",
                "date": "2026-10-12T23:00Z",
                "shortName": "DAL @ COL",
                "competitions": [
                    {
                        "status": {
                            "clock": 0,
                            "displayClock": "0:00",
                            "period": 0,
                            "type": {"state": "pre", "shortDetail": "7:00 PM"},
                        },
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "0",
                                "team": {"id": "17", "abbreviation": "COL"},
                                "records": [{"type": "total", "summary": "3-1-0"}],
                                "probables": [
                                    {
                                        "name": "probableStartingGoalie",
                                        "athlete": {"id": "123", "shortName": "A. Georgiev"},
                                    }
                                ],
                            },
                            {"homeAway": "away", "score": "0", "team": {"id": "9", "abbreviation": "DAL"}},
                        ],
                    }
                ],
            },
        ],
    }


def _counting(games, starts):
    return {
        "categories": [
            {
                "names": ["games", "gameStarted"],
                "statistics": [{"season": {"year": 2027}, "stats": [str(games), str(starts)]}],
            }
        ]
    }


def _goalie_fetch(calls, *, roster_error=False, backup_stats_error=False):
    summary = {
        "boxscore": {
            "teams": [],
            "players": [
                {
                    "team": {"id": "1"},
                    "statistics": [
                        {
                            "name": "goalies",
                            "labels": ["SV", "TOI"],
                            "athletes": [
                                {"athlete": {"id": "4392765", "shortName": "M. DiPietro"}, "stats": ["4", "8:00"]},
                                {"athlete": {"id": "4712036", "shortName": "J. Swayman"}, "stats": ["14", "32:00"]},
                            ],
                        }
                    ],
                }
            ],
        },
        "plays": [],
    }

    def fetch(url: str) -> dict:
        calls.append(url)
        if "scoreboard" in url:
            return _goalie_board()
        if "event=1" in url:
            return summary
        if url.endswith("/teams/1/roster"):
            if roster_error:
                raise RuntimeError("roster down")
            return {
                "athletes": [
                    {
                        "position": "Goalies",
                        "items": [
                            {"id": "4392765", "shortName": "M. DiPietro"},
                            {"id": "4712036", "shortName": "J. Swayman"},
                        ],
                    }
                ]
            }
        if url.endswith("/teams/17/roster"):
            return {
                "athletes": [
                    {
                        "position": "Goalies",
                        "items": [
                            {"id": "123", "shortName": "A. Georgiev"},
                            {"id": "456", "shortName": "Z. Backup"},
                        ],
                    }
                ]
            }
        if url.endswith("/athletes/4712036/stats"):
            return _counting(5, 5)
        if url.endswith("/athletes/4392765/stats"):
            if backup_stats_error:
                raise RuntimeError("stats down")
            return {"categories": [{"names": ["games", "gameStarted"], "statistics": []}]}
        if url.endswith("/athletes/123/stats"):
            return _counting(4, 4)
        if url.endswith("/athletes/456/stats"):
            return _counting(1, 0)
        if url.endswith("/athletes/4712036"):
            return {
                "athlete": {
                    "statsSummary": {
                        "statistics": [
                            {"name": "savePct", "displayValue": ".932", "rank": 8, "rankDisplayValue": "8th"},
                            {"name": "avgGoalsAgainst", "displayValue": "1.98", "rank": 14, "rankDisplayValue": "14th"},
                        ]
                    }
                }
            }
        if url.endswith("/athletes/4392765"):
            return {"athlete": {"statsSummary": {"statistics": []}}}
        if url.endswith("/athletes/123"):
            return {
                "athlete": {
                    "statsSummary": {
                        "statistics": [
                            {"name": "savePct", "displayValue": ".910", "rank": 1, "rankDisplayValue": "Tied-1st"}
                        ]
                    }
                }
            }
        raise RuntimeError(url)

    return fetch


def test_live_feed_ranks_goalie_by_save_pct_and_starts():
    from eeehoc.live import LiveFeed

    calls: list[str] = []
    feed = LiveFeed(_goalie_fetch(calls), ttl=100)
    board = feed.get()
    live = next(game for game in board["games"] if game["id"] == "1")
    home = live["home"]
    assert home["record"] == "4-2-0"
    assert home["goalie_rank"] == "#8"
    assert home["goalie_depth"] == 1
    assert home["goalie_starts"] == 5
    assert home["goalie_save_pct"] == ".932"
    assert home["goalie_gaa"] == "1.98"
    assert [goalie["name"] for goalie in home["goalies"]] == ["J. Swayman", "M. DiPietro"]
    assert home["goalies"][0]["id"] == "4712036"
    assert home["goalies"][0]["rank"] == "#8"
    assert home["goalies"][0]["depth"] == 1
    assert home["goalies"][0]["starts"] == 5
    assert home["goalies"][1]["depth"] == 2
    assert home["goalies"][1]["starts"] == 0
    assert home["goalies"][1]["rank"] == ""
    assert "goalie_rank" not in live["away"]

    upcoming = next(game for game in board["games"] if game["id"] == "2")
    assert upcoming["home"]["goalie"] == "A. Georgiev"
    assert upcoming["home"]["record"] == "3-1-0"
    assert upcoming["home"]["goalie_rank"] == "T1"
    assert upcoming["home"]["goalie_depth"] == 1
    assert upcoming["home"]["goalie_starts"] == 4

    before = len(calls)
    feed._boards[""] = (0.0, feed._boards[""][1])  # noqa: SLF001 - expire the scoreboard, keep goalie cards
    feed.get()
    again = calls[before:]
    assert any("scoreboard" in url for url in again)
    assert not any("athletes" in url or "roster" in url for url in again)


def test_goalie_depth_is_omitted_when_the_chart_is_incomplete():
    from eeehoc.live import LiveFeed

    for kwargs in ({"roster_error": True}, {"backup_stats_error": True}):
        feed = LiveFeed(_goalie_fetch([], **kwargs), ttl=100)
        home = next(game for game in feed.get()["games"] if game["id"] == "1")["home"]
        assert home["goalie_rank"] == "#8"
        assert home["goalie_starts"] == 5
        assert home["record"] == "4-2-0"
        assert home["goalie_depth"] is None
        assert home["goalies"][0]["depth"] is None
