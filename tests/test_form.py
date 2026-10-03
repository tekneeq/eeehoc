"""Last five games per team: score and shots on goal by period."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from eeehoc.form import FormFeed, summarize_game

BUF, CBJ, DET, PIT = "2", "29", "5", "16"


def _summary(eid, iso, home_id, away_id, home_lines, away_lines, *, detail="Final", season_type=2, plays=None):
    home_score = sum(home_lines)
    away_score = sum(away_lines)
    return {
        "header": {
            "id": eid,
            "season": {"year": 2027, "type": season_type},
            "competitions": [
                {
                    "id": eid,
                    "date": iso,
                    "status": {"type": {"state": "post", "name": "STATUS_FINAL", "shortDetail": detail}},
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {"id": home_id, "abbreviation": f"T{home_id}", "shortDisplayName": f"Team {home_id}"},
                            "score": str(home_score),
                            "winner": home_score > away_score,
                            "linescores": [{"displayValue": str(v)} for v in home_lines],
                        },
                        {
                            "homeAway": "away",
                            "team": {"id": away_id, "abbreviation": f"T{away_id}", "shortDisplayName": f"Team {away_id}"},
                            "score": str(away_score),
                            "winner": away_score > home_score,
                            "linescores": [{"displayValue": str(v)} for v in away_lines],
                        },
                    ],
                }
            ],
        },
        "plays": plays or [],
    }


def _shot(kind, period, team_id):
    return {"type": {"text": kind}, "period": {"number": period}, "team": {"id": team_id}}


PLAYS_OT = [
    _shot("Shot", 1, BUF),
    _shot("Shot", 1, BUF),
    _shot("Goal", 1, BUF),
    _shot("Shot", 1, DET),
    _shot("Goal", 1, DET),
    _shot("Missed", 1, BUF),
    _shot("Blocked", 1, DET),  # DET blocked a BUF attempt
    _shot("Shot", 2, DET),
    _shot("Goal", 2, BUF),
    _shot("Shot", 3, DET),
    _shot("Goal", 3, DET),
    _shot("Goal", 4, DET),
]

SUMMARIES = {
    # BUF @ DET, DET wins in OT 3-2. Lines are DET 1-0-1-1, BUF 1-1-0-0.
    "ot": _summary("ot", "2026-09-24T23:00Z", DET, BUF, [1, 0, 1, 1], [1, 1, 0, 0], detail="Final/OT", season_type=1, plays=PLAYS_OT),
    "win": _summary("win", "2026-09-22T23:00Z", BUF, CBJ, [2, 2, 1], [0, 1, 0], season_type=1),
    "reg": _summary("reg", "2026-10-01T23:00Z", CBJ, BUF, [3, 1, 2], [1, 2, 0]),
    "so": _summary("so", "2026-09-26T23:00Z", BUF, PIT, [0, 1, 0, 0, 0], [0, 0, 1, 0, 1], detail="Final/SO", season_type=1),
    "live": {
        "header": {
            "id": "live",
            "competitions": [{"status": {"type": {"state": "in"}}, "competitors": []}],
        },
        "plays": [],
    },
}


def _schedule(events):
    return {
        "events": [
            {
                "id": eid,
                "date": iso,
                "competitions": [{"id": eid, "date": iso, "status": {"type": {"state": state}}}],
            }
            for eid, iso, state in events
        ]
    }


def _fetch(calls):
    def fetch(url):
        calls.append(url)
        if "/teams/2/schedule" in url:
            if "seasontype=2" in url:
                return _schedule([("reg", "2026-10-01T23:00Z", "post"), ("next", "2026-10-03T23:00Z", "pre")])
            if "seasontype=1" in url:
                return _schedule(
                    [
                        ("win", "2026-09-22T23:00Z", "post"),
                        ("ot", "2026-09-24T23:00Z", "post"),
                        ("so", "2026-09-26T23:00Z", "post"),
                    ]
                )
            raise RuntimeError("no playoffs yet")
        if "/teams/29/schedule" in url:
            if "seasontype=2" in url:
                return _schedule([("reg", "2026-10-01T23:00Z", "post")])
            return _schedule([])
        if "summary?event=" in url:
            return SUMMARIES[url.rsplit("=", 1)[1]]
        raise RuntimeError(f"unexpected {url}")

    return fetch


def test_summarize_game_is_from_the_teams_side():
    game = summarize_game(SUMMARIES["ot"], BUF)
    assert game["venue"] == "away"
    assert game["opponent"] == f"T{DET}"
    assert game["result"] == "OTL"
    assert (game["gf"], game["ga"]) == (2, 3)
    assert game["preseason"] is True
    assert [p["label"] for p in game["periods"]] == ["1", "2", "3", "OT"]
    first = game["periods"][0]
    assert (first["gf"], first["ga"]) == (1, 1)
    # BUF: 2 saved + 1 goal on goal, plus a miss and a blocked attempt.
    assert (first["sog_f"], first["sog_a"]) == (3, 2)
    assert (first["shots_f"], first["shots_a"]) == (5, 2)
    assert game["periods"][3]["ga"] == 1

    det = summarize_game(SUMMARIES["ot"], DET)
    assert det["result"] == "W"
    assert det["venue"] == "home"
    assert det["periods"][0]["sog_f"] == 2

    so = summarize_game(SUMMARIES["so"], BUF)
    assert so["result"] == "SOL"
    assert so["periods"][4]["shootout"] is True

    assert summarize_game(SUMMARIES["live"], BUF) is None


def test_recent_fills_from_preseason_and_skips_the_current_game():
    calls: list[str] = []
    feed = FormFeed(_fetch(calls))

    games = feed.recent(BUF, exclude="reg")
    assert [g["id"] for g in games] == ["win", "ot", "so"]
    assert games[0]["result"] == "W"
    assert games[0]["periods"][0]["sog_f"] is None  # no plays archived for that game
    assert any("seasontype=1" in url for url in calls)

    with_current = feed.recent(BUF)
    assert [g["id"] for g in with_current] == ["win", "ot", "so", "reg"]
    assert with_current[-1]["result"] == "L"
    assert with_current[-1]["venue"] == "away"

    before = len(calls)
    feed.recent(BUF)
    assert len(calls) == before  # schedules and summaries are cached


def test_for_game_endpoint():
    from eeehoc.dashboard import DashboardState, make_handler

    calls: list[str] = []
    state = DashboardState(form=FormFeed(_fetch(calls)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        form = json.loads(urllib.request.urlopen(f"{base}/api/form?home={CBJ}&away={BUF}&game=reg", timeout=5).read())
        assert form["limit"] == 5
        assert [g["id"] for g in form["away"]] == ["win", "ot", "so"]
        assert form["home"] == []
        html = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert "/api/form" in html
        assert "last5" in urllib.request.urlopen(base + "/static/app.css", timeout=5).read().decode()
        try:
            urllib.request.urlopen(f"{base}/api/form?home=x&away=2", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("expected 400")
    finally:
        httpd.shutdown()
        httpd.server_close()
