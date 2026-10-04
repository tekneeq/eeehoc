"""Shots on goal per goal: season and last five."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from eeehoc.shots import ShotsFeed, build_shots, shots_per_goal, team_games

BUF, CBJ, DET = "2", "29", "5"
ABBR = {BUF: "BUF", CBJ: "CBJ", DET: "DET"}


def _event(eid, iso, away, home, *, away_goals, home_goals, away_saves, home_saves, so_winner=None, season_type=2, year=2027, state="post", name="STATUS_FINAL"):
    def side(home_away, tid, goals, saves, so):
        comp = {
            "homeAway": home_away,
            "team": {"id": tid, "abbreviation": ABBR[tid]},
            "score": str(goals + (1 if so else 0)),
            "statistics": [{"name": "saves", "displayValue": str(saves)}, {"name": "goals", "displayValue": str(goals)}],
        }
        return comp

    return {
        "id": eid,
        "date": iso,
        "season": {"year": year, "type": season_type},
        "competitions": [
            {
                "status": {"type": {"state": state, "name": name, "shortDetail": "Final/SO" if so_winner else "Final"}},
                "competitors": [side("home", home, home_goals, home_saves, so_winner == home), side("away", away, away_goals, away_saves, so_winner == away)],
            }
        ],
    }


EVENTS = [
    # Preseason: BUF 5-1 CBJ (BUF 35 SOG, CBJ 25 SOG)
    _event("p1", "2026-09-22T23:00Z", CBJ, BUF, away_goals=1, home_goals=5, away_saves=30, home_saves=24, season_type=1),
    # Regular: CBJ 6-3 BUF (CBJ 23 SOG, BUF 26 SOG)
    _event("r1", "2026-10-01T23:00Z", BUF, CBJ, away_goals=3, home_goals=6, away_saves=17, home_saves=23),
    # Regular, shootout: DET beats BUF 3-2 in the SO; shots exclude the deciding goal
    # (BUF 22 SOG = DET's 20 saves + 2 goals; DET 30 SOG = BUF's 28 saves + 2 goals)
    _event("r2", "2026-10-03T23:00Z", BUF, DET, away_goals=2, home_goals=2, away_saves=28, home_saves=20, so_winner=DET),
    # Scheduled, ignored
    _event("r3", "2026-10-05T23:00Z", DET, CBJ, away_goals=0, home_goals=0, away_saves=0, home_saves=0, state="pre", name="STATUS_SCHEDULED"),
    # Postponed, ignored
    _event("r4", "2026-10-04T23:00Z", DET, CBJ, away_goals=0, home_goals=0, away_saves=0, home_saves=0, name="STATUS_POSTPONED"),
]


def test_team_games_reconstruct_shots_from_saves_and_goals():
    rows = {(r["id"], r["abbr"]): r for r in team_games({"events": EVENTS})}
    assert {k[0] for k in rows} == {"p1", "r1", "r2"}
    buf = rows[("r1", "BUF")]
    assert (buf["gf"], buf["ga"], buf["sog_f"], buf["sog_a"]) == (3, 6, 26, 23)
    assert buf["venue"] == "away" and buf["opponent"] == "CBJ" and buf["day"] == "2026-10-01"
    cbj = rows[("r1", "CBJ")]
    assert (cbj["gf"], cbj["ga"], cbj["sog_f"], cbj["sog_a"]) == (6, 3, 23, 26)
    so = rows[("r2", "DET")]
    assert (so["gf"], so["ga"], so["sog_f"], so["sog_a"]) == (2, 2, 30, 22)  # shootout goal is not a shot
    assert rows[("r2", "BUF")]["sog_f"] == 22


def test_shots_per_goal_totals_and_ratios():
    games = [
        {"gf": 3, "ga": 6, "sog_f": 26, "sog_a": 23},
        {"gf": 2, "ga": 2, "sog_f": 30, "sog_a": 22},
        {"gf": 9, "ga": 1, "sog_f": None, "sog_a": None},  # no saves archived: skipped
    ]
    totals = shots_per_goal(games)
    assert totals["games"] == 2
    assert (totals["gf"], totals["ga"], totals["sog_f"], totals["sog_a"]) == (5, 8, 56, 45)
    assert totals["spg_f"] == 11.2 and totals["spg_a"] == 5.6
    assert totals["shooting_pct"] == 8.9 and totals["save_pct"] == 82.2
    assert shots_per_goal([{"gf": 0, "ga": 1, "sog_f": 20, "sog_a": 10}])["spg_f"] is None
    assert shots_per_goal([])["spg_f"] is None


def test_build_shots_uses_regular_season_and_keeps_six_recent():
    rows = team_games({"events": EVENTS})
    board = build_shots(rows, today=date(2026, 10, 3), season_year=2027, season_label="2026-27")
    assert board["season_kind"] == "regular season"
    buf = board["teams"][BUF]
    assert buf["abbr"] == "BUF"
    assert buf["season"]["games"] == 2 and buf["season"]["gf"] == 5 and buf["season"]["sog_f"] == 48
    assert [g["id"] for g in buf["recent"]] == ["p1", "r1", "r2"]
    assert buf["recent"][0]["preseason"] is True and buf["recent"][-1]["opponent"] == "DET"
    det = board["teams"][DET]
    assert det["season"]["games"] == 1 and det["recent"][0]["venue"] == "home"
    assert board["league"]["games"] == 2  # two regular-season games, not four team-games
    assert board["league"]["spg"] == round((26 + 23 + 22 + 30) / (3 + 6 + 2 + 2), 1)

    # Before opening night only preseason exists: use it, and say so.
    early = build_shots(rows, today=date(2026, 9, 30), season_year=2027, season_label="2026-27")
    assert early["season_kind"] == "preseason"
    assert early["teams"][BUF]["season"]["games"] == 1 and early["teams"][BUF]["season"]["spg_f"] == 7.0
    assert DET not in early["teams"]


def _fetch_factory(calls):
    def fetch(url):
        calls.append(url)
        if "dates=" not in url:
            return {"leagues": [{"season": {"year": 2027, "displayName": "2026-27", "startDate": "2026-09-15T07:00Z"}}]}
        ym = url.split("dates=")[1][:6]
        if ym == "202609":
            return {"events": EVENTS[:1]}
        if ym == "202610":
            return {"events": EVENTS[1:]}
        return {"events": []}

    return fetch


def test_feed_caches_months_and_result():
    calls = []
    feed = ShotsFeed(_fetch_factory(calls))
    board = feed.get(today=date(2026, 10, 3))
    assert board["season_label"] == "2026-27" and board["teams"][CBJ]["season"]["spg_f"] == round(23 / 6, 1)
    assert any("dates=202609" in u for u in calls) and any("dates=202610" in u for u in calls)
    assert not any("dates=202608" in u for u in calls)
    before = len(calls)
    assert feed.get(today=date(2026, 10, 3)) is board
    assert len(calls) == before


def test_shots_endpoint_and_page_shell():
    from eeehoc.dashboard import DashboardState, make_handler

    state = DashboardState(shots=ShotsFeed(_fetch_factory([])))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        board = json.loads(urllib.request.urlopen(base + "/api/shots", timeout=5).read())
        assert board["recent_games"] == 5
        assert set(board["teams"]) >= {BUF, CBJ, DET}
        assert {"season", "recent", "abbr"} <= set(board["teams"][BUF])
        script = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert "/api/shots" in script and "shotsPerGoalHtml" in script
        css = urllib.request.urlopen(base + "/static/app.css", timeout=5).read().decode()
        assert ".spg-grid" in css
    finally:
        httpd.shutdown()
        httpd.server_close()
