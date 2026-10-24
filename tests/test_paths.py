"""Branch trees from the score after a period, and the rest-of-game model."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from eeehoc.live import SCOREBOARD_URL
from eeehoc.paths import PathsFeed, build_paths, game_state, league_rates, parse_month, rest_of_game, score_after

MTL, CHI, BOS = "10", "4", "1"
ABBR = {MTL: "MTL", CHI: "CHI", BOS: "BOS"}
TODAY = date(2026, 10, 23)


def _event(eid, iso, home, away, home_periods, away_periods, *, detail="Final", saves=(28, 30), year=2027, stype=2, name="STATUS_FINAL"):
    def comp(side, tid, periods, save_count, goals_stat):
        return {
            "homeAway": side,
            "team": {"id": tid, "abbreviation": ABBR[tid]},
            "score": str(sum(periods)),
            "linescores": [{"period": i + 1, "value": float(g), "displayValue": str(g)} for i, g in enumerate(periods)],
            "statistics": [{"name": "saves", "displayValue": str(save_count)}, {"name": "goals", "displayValue": str(goals_stat)}],
        }

    # The "goals" stat leaves a shootout winner out, like ESPN's.
    so = detail.endswith("SO")
    home_goals = sum(home_periods) - (1 if so and sum(home_periods) > sum(away_periods) else 0)
    away_goals = sum(away_periods) - (1 if so and sum(away_periods) > sum(home_periods) else 0)
    return {
        "id": eid,
        "date": iso,
        "season": {"year": year, "type": stype},
        "competitions": [
            {
                "date": iso,
                "status": {"type": {"name": name, "state": "post" if name == "STATUS_FINAL" else "in", "shortDetail": detail}},
                "competitors": [comp("home", home, home_periods, saves[0], home_goals), comp("away", away, away_periods, saves[1], away_goals)],
            }
        ],
    }


def _history():
    return [
        # MTL home, 1-1 after the 1st, wins 3-2 in regulation
        _event("a", "2026-10-01T23:00Z", MTL, CHI, [1, 1, 1], [1, 1, 0]),
        # MTL away at BOS, 1-1 after the 1st, loses 2-3 in OT (BOS scores the OT goal)
        _event("b", "2026-10-03T23:00Z", BOS, MTL, [1, 1, 0, 1], [1, 0, 1, 0], detail="Final/OT"),
        # CHI home v BOS, 1-1 after the 1st, CHI wins in a shootout; 1-1 after the 2nd too
        _event("c", "2026-10-05T23:00Z", CHI, BOS, [1, 0, 0, 0, 1], [1, 0, 0, 0, 0], detail="Final/SO"),
        # BOS home v MTL, 0-0 after the 1st, BOS wins 2-0
        _event("d", "2026-10-07T23:00Z", BOS, MTL, [0, 1, 1], [0, 0, 0]),
        # Last season, league only matters here: 1-1 after the 1st, away wins 1-4
        _event("e", "2025-11-07T23:00Z", BOS, CHI, [1, 0, 0], [1, 2, 1], year=2026),
        # Preseason, ignored
        _event("p", "2026-09-25T23:00Z", MTL, CHI, [1, 1, 1], [1, 1, 1], stype=1),
        # Still in progress, ignored
        _event("l", "2026-10-23T23:00Z", MTL, CHI, [1], [1], name="STATUS_IN_PROGRESS"),
    ]


def test_parse_month_reads_periods_endings_and_shots():
    games = parse_month({"events": _history()})
    assert [g["id"] for g in games] == ["a", "b", "c", "d", "e"]
    a, b, c = games[0], games[1], games[2]
    assert a["periods"] == [(1, 1), (1, 1), (1, 0)] and a["end"] == "REG" and a["home_score"] == 3
    assert score_after(a, 1) == (1, 1) and score_after(a, 2) == (2, 2) and score_after(a, 3) == (3, 2)
    assert b["end"] == "OT" and b["home_score"] == 3 and score_after(b, 3) == (2, 2)
    assert c["end"] == "SO" and c["home_score"] == 2 and c["away_score"] == 1 and score_after(c, 3) == (1, 1)
    # Shots on goal: the other goalie's saves plus goals; the shootout goal is not a shot
    assert a["home_sog"] == 30 + 3 and a["away_sog"] == 28 + 2
    assert c["home_sog"] == 30 + 1
    shots, save_pct = league_rates(games, 2027)
    assert 28 < shots < 33 and 0.85 < save_pct < 0.95


def _live(period, clock, home_score, away_score, home_lines, away_lines, *, state="in", home_shots="12", away_shots="9", detail=""):
    return {
        "id": "401000001",
        "state": state,
        "period": period,
        "clock": clock,
        "status_detail": detail,
        "home": {
            "id": CHI,
            "abbr": "CHI",
            "score": home_score,
            "linescores": home_lines,
            "stats": {"shots": home_shots},
            "goalies": [{"name": "S. Soderblom", "rank": "#14", "save_pct_season": ".912", "saves": "8", "shots_against": "9"}],
        },
        "away": {
            "id": MTL,
            "abbr": "MTL",
            "score": away_score,
            "linescores": away_lines,
            "stats": {"shots": away_shots},
            "goalies": [{"name": "J. Dobes", "rank": "#5", "save_pct_season": ".925", "saves": "11", "shots_against": "12"}],
        },
    }


def test_game_state_finds_the_last_completed_period():
    # Intermission after the 1st at 1-1
    s = game_state(_live(1, "0:00", 1, 1, [1], [1]))
    assert s["completed"] == 1 and s["score"] == (1, 1) and s["elapsed"] == 1200 and not s["next_period_started"]
    # Midway through the 2nd, CHI has scored: state is still 1-1 after the 1st, live branch so far
    s = game_state(_live(2, "12:00", 2, 1, [1, 1], [1, 0]))
    assert s["completed"] == 1 and s["score"] == (1, 1) and s["live_score"] == (2, 1) and s["elapsed"] == 1680 and s["remaining"] == 1920
    assert s["next_period_started"] and not s["next_period_done"]
    # In the 3rd: the 2nd is done, so the branch was taken
    s = game_state(_live(3, "15:00", 2, 1, [1, 1, 0], [1, 0, 0]))
    assert s["completed"] == 2 and s["score"] == (2, 1) and s["remaining"] == 900
    # Overtime: end of regulation, tied
    s = game_state(_live(4, "3:21", 2, 2, [1, 1, 0], [1, 0, 1]))
    assert s["completed"] == 3 and s["score"] == (2, 2) and s["remaining"] == 0 and not s["next_period_started"]
    # Before the puck drops
    s = game_state(_live(0, "", 0, 0, [], [], state="pre"))
    assert s["completed"] == 0 and s["score"] == (0, 0) and s["remaining"] == 3600
    # Final in a shootout without linescores: tied after three
    s = game_state({"id": "f", "state": "post", "period": 5, "clock": "0:00", "status_detail": "Final/SO", "home": {"score": 3, "linescores": []}, "away": {"score": 2, "linescores": []}})
    assert s["completed"] == 3 and s["score"] == (2, 2)


def test_build_paths_three_lenses_with_the_live_branch_marked():
    games = parse_month({"events": _history()})
    out = build_paths(games, _live(2, "12:00", 2, 1, [1, 1], [1, 0]), season_year=2027)
    assert out["after"] == 1 and out["score"] == "1-1" and out["live_branch"] == "2-1" and not out["next_period_done"]
    assert out["seasons"] == ["2025-26", "2026-27"] and out["games"] == 5

    # CHI (home lens, for-against): games a (lost 2-3), c (won in SO), e (won 4-1 away) were 1-1 after the 1st
    chi = out["home"]
    assert chi["count"] == 3 and chi["wins"] == 2 and chi["losses"] == 1
    assert chi["win_pct"] == 67.0 and chi["loss_pct"] == 33.0 and chi["past_regulation_pct"] == 33.0 and chi["so_pct"] == 33.0
    assert chi["win_by"] == {"REG": 1, "OT": 0, "SO": 1}
    branches = {b["score"]: b for b in chi["branches"]}
    # after the 2nd: a → 2-2 (CHI scored one, MTL one), c → 1-1, e → 3-1 for CHI; live 2-1 is not in the history, so it is shown with no games
    assert branches["2-2"]["count"] == 1 and branches["1-1"]["count"] == 1 and branches["3-1"]["count"] == 1
    assert branches["2-1"]["count"] == 0 and branches["2-1"]["live"] is True and branches["2-1"]["pct"] == 0.0
    assert chi["stayed_pct"] == 33.0  # game c was still 1-1 at the end of regulation

    # MTL (away lens): a (won 3-2) and b (lost in OT) were 1-1 after the 1st; d was 0-0
    mtl = out["away"]
    assert mtl["count"] == 2 and mtl["win_pct"] == 50.0 and mtl["past_regulation_pct"] == 50.0 and mtl["ot_pct"] == 50.0
    mtl_branches = {b["score"]: b for b in mtl["branches"]}
    assert mtl_branches["2-2"]["count"] == 1 and mtl_branches["1-2"]["count"] == 1  # for-against: b was 1-2 after the 2nd for MTL
    assert mtl_branches["1-2"]["live"] is True
    assert [f["score"] for f in mtl["finals"]] == ["2-3", "3-2"]

    # League (home-away): a, b, c, e were 1-1 after the 1st; the home side won a, b and c
    nhl = out["league"]
    assert nhl["count"] == 4 and nhl["win_pct"] == 75.0 and nhl["loss_pct"] == 25.0 and nhl["past_regulation_pct"] == 50.0
    assert {b["score"]: b["count"] for b in nhl["branches"] if b["count"]} == {"2-2": 1, "2-1": 1, "1-1": 1, "1-3": 1}
    assert any(b["live"] and b["score"] == "2-1" for b in nhl["branches"])


def test_build_paths_follows_the_exact_route_of_period_scores_with_an_any_way_view():
    extra = [
        # 1-1 after the 1st, then 1-3 after the 2nd (CHI home, MTL away) — the same route as the live game
        _event("x1", "2026-10-09T23:00Z", CHI, MTL, [1, 0, 1], [1, 2, 1]),
        # 1-3 after the 2nd by another route: 0-2 after the 1st
        _event("x2", "2026-10-11T23:00Z", CHI, BOS, [0, 1, 2], [2, 1, 0]),
        # 1-3 after the 2nd via 1-1 again, league only (BOS home)
        _event("x3", "2026-10-13T23:00Z", BOS, MTL, [1, 0, 0], [1, 2, 0]),
    ]
    games = parse_month({"events": _history() + extra})
    live = _live(3, "15:00", 1, 3, [1, 0, 0], [1, 2, 0])  # CHI home 1, MTL away 3; 1-1 → 1-3
    out = build_paths(games, live, season_year=2027)
    assert out["after"] == 2 and out["score"] == "1-3" and out["path"] == ["1-1", "1-3"] and out["exact"] is True

    # Exact route (default): e, x1 and x3 went 1-1 → 1-3; x2 (0-2 → 1-3) did not
    assert out["league"]["count"] == 3 and {b["score"] for b in out["league"]["branches"]} == {"1-4", "2-4", "1-3"}
    assert out["home"]["count"] == 1  # CHI at home only in x1 (for–against 1-1 → 1-3), lost 2-4
    assert out["home"]["loss_pct"] == 100.0
    assert out["away"]["count"] == 2  # MTL in x1 and x3, both for–against 1-1 → 3-1, both won
    assert out["away"]["win_pct"] == 100.0

    # Any way: everything that was 1-3 after the 2nd, however it got there
    assert out["any"]["league"]["count"] == 4 and out["any"]["home"]["count"] == 2 and out["any"]["away"]["count"] == 2

    # After the 1st only, the route and the state are the same thing: no second view
    one = build_paths(games, _live(2, "12:00", 1, 1, [1, 0], [1, 0]), season_year=2027)
    assert one["exact"] is False and one["any"] is None and one["path"] == ["1-1"]


def test_build_paths_at_the_end_of_regulation_shows_how_tied_games_ended():
    games = parse_month({"events": _history()})
    out = build_paths(games, _live(4, "3:21", 2, 2, [1, 1, 0], [1, 0, 1]), season_year=2027)
    assert out["after"] == 3 and out["score"] == "2-2" and out["live_branch"] is None
    # Only game b was 2-2 after three (BOS v MTL, BOS won in OT); MTL lost it
    assert out["league"]["count"] == 1 and out["league"]["branches"] == [{"score": "OT", "count": 1, "pct": 100.0, "live": False}]
    assert out["away"]["count"] == 1 and out["away"]["loss_pct"] == 100.0
    assert out["model"]["remaining_sec"] == 0 and out["model"]["to_overtime"] == 100.0


def test_rest_of_game_model_leans_on_shot_pace_and_the_goalies():
    power = {
        "teams": {
            CHI: {"sog_per_game": 26.0, "sog_allowed_per_game": 30.0},
            MTL: {"sog_per_game": 32.0, "sog_allowed_per_game": 28.0},
        }
    }
    live = game_state(_live(2, "12:00", 2, 1, [1, 1], [1, 0], home_shots="9", away_shots="15"))
    model = rest_of_game(_live(2, "12:00", 2, 1, [1, 1], [1, 0], home_shots="9", away_shots="15"), live, power=power, league_shots=29.0, league_save_pct=0.900)
    assert model["remaining_sec"] == 1920
    # CHI shoots (26 + 28) / 2 = 27 a game; MTL (32 + 30) / 2 = 31
    assert model["home"]["shots_per_60"] == 27.0 and model["away"]["shots_per_60"] == 31.0
    # Tonight's pace: 9 shots in 28 minutes, 15 in 28, blended by (elapsed − 5 min) / (that + 20 min)
    assert model["home"]["tonight_per_60"] == 19.3 and model["away"]["tonight_per_60"] == 32.1
    assert model["home"]["rate_used"] < 27.0 < 31.0 < model["away"]["rate_used"]
    # Three shots in the first ninety seconds are not a pace yet
    early = rest_of_game(_live(1, "18:30", 0, 0, [], [], home_shots="1", away_shots="3"), game_state(_live(1, "18:30", 0, 0, [], [], home_shots="1", away_shots="3")), power=power, league_shots=29.0, league_save_pct=0.9)
    assert early["away"]["tonight_per_60"] == 120.0 and early["away"]["rate_used"] == 31.0
    # Goalies: CHI shoots into Dobes (.925 → .915 used); MTL into Soderblom (.912 → .907 used)
    assert model["home"]["opp_goalie"] == "J. Dobes" and model["home"]["opp_goalie_rank"] == "#5" and model["home"]["save_pct_used"] == 0.915
    assert model["away"]["save_pct_used"] == 0.907
    assert model["home"]["expected_goals_left"] < model["away"]["expected_goals_left"]
    # CHI leads 2-1 but is being outshot into the better goalie; still, a one-goal lead is worth a lot with 32 minutes left
    assert 50 < model["home_win"] < 80 and model["home_win"] + model["away_win"] == 100.0
    assert 0 < model["to_overtime"] < 40

    # Without a power table the league rate stands in; a lead with no time left is a win
    bare = rest_of_game(_live(3, "0:00", 3, 1, [1, 1, 1], [1, 0, 0]), game_state(_live(3, "0:00", 3, 1, [1, 1, 1], [1, 0, 0])), power=None, league_shots=29.0, league_save_pct=0.9)
    assert bare["home"]["shots_per_60"] == 29.0 and bare["home_win"] == 100.0 and bare["to_overtime"] == 0.0
    assert rest_of_game(_live(3, "0:00", 3, 1, [1, 1, 1], [1, 0, 0], state="post"), game_state(_live(3, "0:00", 3, 1, [1, 1, 1], [1, 0, 0], state="post")), power=None, league_shots=29.0, league_save_pct=0.9) is None


def _fetch_for(events):
    calls = []

    def fetch(url):
        calls.append(url)
        if url == SCOREBOARD_URL:
            return {"leagues": [{"season": {"year": 2027, "displayName": "2026-27", "startDate": "2026-09-15T07:00Z"}}], "events": []}
        if url.startswith(SCOREBOARD_URL) and "dates=" in url:
            ym = url.split("dates=")[1][:6]
            return {"events": [e for e in events if e["date"][:7].replace("-", "") == ym]}
        raise AssertionError(url)

    return fetch, calls


def test_feed_serves_this_season_first_then_fills_in_and_caches_past_months(tmp_path):
    fetch, calls = _fetch_for(_history())
    feed = PathsFeed(fetch, workers=2, cache_dir=tmp_path)
    game = _live(1, "0:00", 1, 1, [1], [1])
    first = feed.for_game(game, today=TODAY)
    assert first.get("loading") is True and "months_total" in first
    feed._worker.join(timeout=5)
    out = feed.for_game(game, today=TODAY)
    assert out["complete"] is True and out["after"] == 1 and out["league"]["count"] == 4 and out["games"] == 5
    months = [u for u in calls if "dates=" in u]
    assert len(months) == len(set(months))  # each month once
    assert any("dates=202409" in u for u in months) and any("dates=202610" in u for u in months)
    # Finished months with games were written to disk, and the empty summer months; an empty in-season month
    # and the current month were not
    assert (tmp_path / "paths" / "202511.json").exists() and (tmp_path / "paths" / "202507.json").exists()
    assert not (tmp_path / "paths" / "202509.json").exists() and not (tmp_path / "paths" / "202610.json").exists()
    feed.for_game(game, today=TODAY)
    assert len([u for u in calls if "dates=" in u]) == len(months)  # served from memory

    # A fresh process reads the finished months from disk and only asks ESPN for the current month
    fetch2, calls2 = _fetch_for(_history())
    again = PathsFeed(fetch2, workers=2, cache_dir=tmp_path)
    again.refresh(today=TODAY)
    asked = [u.split("dates=")[1][:6] for u in calls2 if "dates=" in u]
    assert "202610" in asked and "202511" not in asked and "202507" not in asked and "202509" in asked
    assert again.for_game(game, today=TODAY)["league"]["count"] == 4  # game e came back from the 2025-11 file
    assert again.for_game(game, today=TODAY)["games"] == 5


def test_feed_publishes_the_current_season_before_the_older_ones_land():
    import threading as _threading

    gate = _threading.Event()
    fetch, _calls = _fetch_for(_history())

    def slow(url):
        if "dates=2024" in url or "dates=2025" in url:
            gate.wait(5)
        return fetch(url)

    feed = PathsFeed(slow, workers=2, cache_dir="/nonexistent/eeehoc-test-cache")
    game = _live(1, "0:00", 1, 1, [1], [1])
    assert feed.for_game(game, today=TODAY).get("loading") is True
    end = time.time() + 5
    partial = None
    while time.time() < end:
        partial = feed.for_game(game, today=TODAY)
        if not partial.get("loading"):
            break
        time.sleep(0.05)
    assert partial and partial["complete"] is False and partial["seasons"] == ["2026-27"] and partial["games"] == 4
    gate.set()
    feed._worker.join(timeout=5)
    full = feed.for_game(game, today=TODAY)
    assert full["complete"] is True and full["games"] == 5 and full["seasons"] == ["2025-26", "2026-27"]


def test_paths_endpoint_and_page_shell(tmp_path):
    from eeehoc.dashboard import DashboardState, make_handler
    from eeehoc.live import LiveFeed

    fetch, _calls = _fetch_for(_history())
    live_game = _live(2, "12:00", 2, 1, [1, 1], [1, 0])

    class FakeLive(LiveFeed):
        def get(self, *, force=False, dates=None):  # noqa: ARG002
            return {"games": [live_game]}

    class NoPower:
        def get(self, **_kwargs):
            return {"loading": True, "teams": {}}

    state = DashboardState(live=FakeLive(), paths=PathsFeed(fetch, cache_dir=tmp_path), power=NoPower())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        out = json.loads(urllib.request.urlopen(base + "/api/paths?game=401000001&wait=1", timeout=5).read())
        assert out["score"] == "1-1" and out["live_branch"] == "2-1" and out["model"]["home_win"] > 50
        try:
            urllib.request.urlopen(base + "/api/paths?game=999", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        script = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert all(token in script for token in ("/api/paths", "pathsHtml", "loadVisiblePaths", "branchChips", "pathsModelHtml"))
        css = urllib.request.urlopen(base + "/static/app.css", timeout=5).read().decode()
        assert ".pth-chip.live" in css and ".pth-model" in css
    finally:
        httpd.shutdown()
        httpd.server_close()
