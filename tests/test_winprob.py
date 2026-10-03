"""Elo win probability: replay, grading, buckets, and the endpoint."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from eeehoc.winprob import (
    EloConfig,
    WinProbService,
    bucket_for,
    bucket_record,
    build_board,
    day_view,
    elo_win_prob,
    margin_multiplier,
    parse_month,
    run_model,
    team_bucket_for,
    team_bucket_records,
)

TEAMS = {
    "1": ("BOS", "Bruins"),
    "2": ("BUF", "Sabres"),
    "3": ("CAR", "Hurricanes"),
    "4": ("CHI", "Blackhawks"),
}


def _event(eid, iso, away, home, *, away_score=None, home_score=None, state="pre", detail=None, year=2027, season_type=2, status_name=None):
    def side(home_away, tid, score):
        abbr, name = TEAMS[tid]
        comp = {"homeAway": home_away, "team": {"id": tid, "abbreviation": abbr, "shortDisplayName": name, "logo": f"https://x/{abbr}.png", "color": "112233"}}
        if score is not None:
            comp["score"] = str(score)
        return comp

    if detail is None:
        detail = "Final" if state == "post" else "7:00 PM"
    return {
        "id": eid,
        "date": iso,
        "season": {"year": year, "type": season_type},
        "competitions": [
            {
                "neutralSite": False,
                "status": {"type": {"state": state, "name": status_name or ("STATUS_FINAL" if state == "post" else "STATUS_SCHEDULED"), "shortDetail": detail}},
                "competitors": [side("home", home, home_score), side("away", away, away_score)],
            }
        ],
    }


def _games(events):
    return parse_month({"events": events})


def test_probability_helpers():
    assert elo_win_prob(0) == 0.5
    assert 0.549 < elo_win_prob(35) < 0.551
    assert margin_multiplier(1) < margin_multiplier(3)
    assert bucket_for(0.52) == "50-55"
    assert bucket_for(0.48) == "50-55"  # the favourite is the other side
    assert bucket_for(0.9) == ">70"
    assert team_bucket_for(0.2) == "<35"
    assert team_bucket_for(0.47) == "45-50"
    assert team_bucket_for(0.8) == ">65"


def test_parse_month_keeps_scheduled_and_final_but_not_postponed():
    games = _games(
        [
            _event("a", "2026-10-02T23:00Z", "1", "2", away_score=3, home_score=4, state="post", detail="Final/OT"),
            _event("b", "2026-10-03T23:00Z", "3", "4"),
            _event("c", "2026-10-03T23:00Z", "4", "1", state="post", status_name="STATUS_POSTPONED", detail="Postponed"),
        ]
    )
    assert [g["id"] for g in games] == ["a", "b"]
    assert games[0]["final"] and games[0]["ot"] == "OT" and games[0]["home_score"] == 4
    assert games[0]["day"] == "2026-10-02"
    assert games[1]["state"] == "pre" and games[1]["home_score"] is None and games[1]["ot"] is None


def test_replay_grades_with_pregame_ratings_and_home_ice():
    cfg = EloConfig(k=20, hfa=35, regress=0.5, preseason_k=0.5)
    events = [
        # Preseason: moves ratings (half K) but is not graded.
        _event("pre", "2026-09-25T23:00Z", "2", "1", away_score=1, home_score=5, state="post", season_type=1),
        # Opening night: BOS (now rated above BUF) visits BUF.
        _event("g1", "2026-10-01T23:00Z", "1", "2", away_score=2, home_score=3, state="post", detail="Final/SO"),
        _event("g2", "2026-10-02T23:00Z", "3", "4", away_score=4, home_score=1, state="post"),
        _event("g3", "2026-10-03T23:00Z", "4", "3"),
    ]
    model = run_model(_games(events), {2027}, config=cfg)
    preds = {g["id"]: g for g in model["predictions"]}
    assert set(preds) == {"g1", "g2", "g3"}

    g1 = preds["g1"]
    assert g1["away"]["elo"] > 1500 > g1["home"]["elo"]  # preseason win already counted
    assert g1["home"]["record"] == "0-0-0"  # pre-game record, before the result
    assert g1["result"] == {"winner": "home", "winner_abbr": "BUF", "correct": g1["favorite"] == "home", "margin": 1, "ot": "SO", "brier": g1["result"]["brier"]}

    g2 = preds["g2"]
    assert g2["favorite"] == "home" and g2["home"]["prob"] > 0.5  # even teams: home ice decides
    assert g2["result"]["correct"] is False and g2["result"]["winner_abbr"] == "CAR"

    g3 = preds["g3"]
    assert g3["played"] is False and g3["result"] is None
    assert g3["home"]["prob"] > 0.5  # CAR just won by three and is at home
    assert g3["home"]["record"] == "1-0-0" and g3["away"]["record"] == "0-1-0"

    by_abbr = {r["abbr"]: r for r in model["ratings"]}
    assert by_abbr["CAR"]["rank"] == 1
    assert by_abbr["BOS"]["record"] == "0-0-1"  # shootout loss is an OTL
    assert model["through_day"] == "2026-10-02"


def test_ratings_regress_between_seasons():
    cfg = EloConfig(k=20, hfa=0, regress=0.5)
    events = [
        _event("old", "2025-10-10T23:00Z", "1", "2", away_score=0, home_score=6, state="post", year=2026),
        _event("new", "2026-10-10T23:00Z", "1", "2"),
    ]
    model = run_model(_games(events), {2026, 2027}, config=cfg)
    old = next(g for g in model["predictions"] if g["id"] == "old")
    new = next(g for g in model["predictions"] if g["id"] == "new")
    gained = old["home"]["elo"] + 20 * margin_multiplier(6) * 0.5 - 1500
    assert abs((new["home"]["elo"] - 1500) - gained / 2) < 0.2


def test_buckets_and_team_records():
    cfg = EloConfig(k=20, hfa=35)
    events = [
        _event("a", "2026-10-01T23:00Z", "1", "2", away_score=1, home_score=2, state="post"),
        _event("b", "2026-10-02T23:00Z", "2", "1", away_score=3, home_score=1, state="post", detail="Final/OT"),
        _event("c", "2026-10-03T23:00Z", "3", "4", away_score=0, home_score=1, state="post"),
        _event("d", "2026-10-04T23:00Z", "1", "4"),
    ]
    model = run_model(_games(events), {2027}, config=cfg)
    board = build_board(model, 2027, today=date(2026, 10, 4), config=cfg)

    assert board["record"]["total"]["record"] == "2-1"
    assert board["record"]["total"]["pending"] == 1
    assert board["current_day"] == "2026-10-04"
    assert board["days"] == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]

    # Even teams at home are 55.0% (35 Elo), so a and c sit in the 55-60 band;
    # b is BOS at home after losing a, a 53% favourite that then lost in OT.
    buckets = {b["key"]: b for b in bucket_record(board["games"])}
    assert buckets["55-60"]["record"] == "2-0"
    assert buckets["55-60"]["home"]["record"] == "2-0" and buckets["55-60"]["away"]["record"] == "0-0"
    assert buckets["55-60"]["expected_pct"] == 55.0
    assert buckets["50-55"]["record"] == "0-1"
    assert sum(b["pending"] for b in buckets.values()) == 1

    teams = {t["abbr"]: t for t in team_bucket_records(board["games"], model["ratings"])}
    assert teams["BUF"]["favored"]["record"] == "1-0" and teams["BUF"]["underdog"]["record"] == "1-0"
    assert teams["BUF"]["buckets"]["55-60"]["record"] == "1-0" and teams["BUF"]["buckets"]["45-50"]["record"] == "1-0"
    assert teams["BOS"]["favored"]["record"] == "0-1" and teams["BOS"]["underdog"]["record"] == "0-1"
    assert teams["BOS"]["buckets"]["35-45"]["record"] == "0-1" and teams["BOS"]["buckets"]["50-55"]["record"] == "0-1"
    assert teams["CHI"]["favored"]["record"] == "1-0" and teams["CHI"]["buckets"]["55-60"]["record"] == "1-0"
    assert teams["CAR"]["underdog"]["record"] == "0-1" and teams["CAR"]["buckets"]["35-45"]["record"] == "0-1"
    assert [t["abbr"] for t in teams.values()][0] == max(model["ratings"], key=lambda r: r["elo"])["abbr"]

    view = day_view(board, "2026-10-02")
    assert [g["id"] for g in view["games"]] == ["b"]
    assert view["prev_day"] == "2026-10-01" and view["next_day"] == "2026-10-03"
    assert view["day_record"]["record"] == "0-1"
    assert "games" in view and len(day_view(board, None)["games"]) == 1


def test_finished_season_recent_window_ends_on_its_last_game_day():
    events = [
        _event("a", "2026-04-10T23:00Z", "1", "2", away_score=1, home_score=2, state="post", year=2026),
        _event("b", "2026-04-16T23:00Z", "3", "4", away_score=0, home_score=1, state="post", year=2026),
    ]
    model = run_model(_games(events), {2026})
    board = build_board(model, 2026, today=date(2026, 10, 3))
    assert board["record"]["recent"]["through"] == "2026-04-16"
    assert board["record"]["recent"]["record"] == "2-0"
    assert [d["day"] for d in board["record"]["daily"]] == ["2026-04-10", "2026-04-16"]


def _fetch_factory(calls):
    def fetch(url):
        calls.append(url)
        if "dates=" not in url:
            return {"leagues": [{"season": {"year": 2027, "displayName": "2026-27"}}], "events": []}
        ym = url.split("dates=")[1][:6]
        if ym == "202610":
            return {
                "events": [
                    _event("a", "2026-10-01T23:00Z", "1", "2", away_score=1, home_score=2, state="post"),
                    _event("d", "2026-10-03T23:00Z", "1", "4"),
                ]
            }
        if ym == "202511":
            return {"events": [_event("old", "2025-11-01T23:00Z", "3", "4", away_score=2, home_score=5, state="post", year=2026)]}
        if ym == "202409":
            raise RuntimeError("archive offline")
        return {"events": []}

    return fetch


def test_service_replays_two_seasons_and_caches():
    calls = []
    svc = WinProbService(_fetch_factory(calls), workers=2)
    board = svc.get(today=date(2026, 10, 3))
    assert board["season"] == 2027 and board["seasons"] == [2027, 2026]
    assert board["season_label"] == "2026-27"
    assert any("dates=202409" in u for u in calls)  # replay starts two seasons back
    assert any("dates=202610" in u for u in calls)
    assert board["error"] == "archive offline"  # a missing archive month is survivable
    assert board["record"]["total"]["record"] == "1-0"
    assert {t["abbr"] for t in board["ratings"]} == {"BOS", "BUF", "CAR", "CHI"}

    before = len(calls)
    again = svc.get(today=date(2026, 10, 3))
    assert again is board and len(calls) == before

    prev = svc.get(season=2026, today=date(2026, 10, 3))
    assert prev["season_label"] == "2025-26" and prev["record"]["total"]["record"] == "1-0"
    assert [g["id"] for g in prev["games"]] == ["old"]


def test_winprob_endpoint_and_page_shell():
    from eeehoc.dashboard import DashboardState, make_handler

    state = DashboardState(winprob=WinProbService(_fetch_factory([]), workers=2))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        board = json.loads(urllib.request.urlopen(base + "/api/winprob", timeout=5).read())
        assert board["day"] in board["days"]
        assert [b["key"] for b in board["buckets"]] == ["50-55", "55-60", "60-65", "65-70", ">70"]
        assert list(board["team_buckets"][0]["buckets"]) == ["<35", "35-45", "45-50", "50-55", "55-60", "60-65", ">65"]
        day = json.loads(urllib.request.urlopen(base + "/api/winprob?season=2027&day=2026-10-01", timeout=5).read())
        assert [g["id"] for g in day["games"]] == ["a"] and day["day_record"]["record"] == "1-0"
        bogus = json.loads(urllib.request.urlopen(base + "/api/winprob?day=nope", timeout=5).read())
        assert bogus["day"] == bogus["current_day"]
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert 'data-tab="winprob"' in html and 'id="wpTeamBuckets"' in html
        script = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert "/api/winprob" in script
        css = urllib.request.urlopen(base + "/static/app.css", timeout=5).read().decode()
        assert ".tb-grid" in css and ".wp-bar" in css
    finally:
        httpd.shutdown()
        httpd.server_close()
