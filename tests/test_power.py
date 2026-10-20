"""Offence and defence power, special teams, and expected goals."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from eeehoc.live import SCOREBOARD_URL, SUMMARY_URL
from eeehoc.power import PAR_POWER, PowerFeed, build_power, game_record
from eeehoc.xg import game_xg, play_xg, shot_type, shot_xg

BUF, CBJ, DET = "2", "29", "5"
ABBR = {BUF: "BUF", CBJ: "CBJ", DET: "DET"}
TODAY = date(2026, 10, 10)


def _play(kind, team, x, y, text="Wrist Shot", strength="even-strength", period=1):
    return {
        "type": {"text": kind},
        "team": {"id": team},
        "coordinate": {"x": x, "y": y},
        "text": text,
        "strength": {"abbreviation": strength},
        "period": {"number": period},
    }


def _summary(eid, iso, home, away, *, home_goals, away_goals, plays, box, season_type=2, year=2027, state="post"):
    def comp(side, tid, goals):
        return {"homeAway": side, "team": {"id": tid, "abbreviation": ABBR[tid]}, "score": str(goals)}

    def team_box(tid, stats):
        return {
            "team": {"id": tid},
            "statistics": [{"name": name, "displayValue": str(value)} for name, value in stats.items()],
        }

    return {
        "header": {
            "id": eid,
            "season": {"year": year, "type": season_type},
            "competitions": [
                {
                    "date": iso,
                    "status": {"type": {"state": state}},
                    "competitors": [comp("home", home, home_goals), comp("away", away, away_goals)],
                }
            ],
        },
        "boxscore": {"teams": [team_box(home, box[home]), team_box(away, box[away])]},
        "plays": plays,
    }


def test_shot_xg_falls_with_distance_and_angle_and_knows_the_shot_type():
    close = shot_xg(80, 0)
    far = shot_xg(40, 0)
    straight = shot_xg(58, 0)  # 31 ft, straight on
    wide = shot_xg(80, 30)  # 31 ft, from the side
    assert 0 < wide < far < straight < close < 1
    assert shot_xg(80, 0, "slap") > shot_xg(80, 0, "wrist") > shot_xg(80, 0, "wrap")
    assert shot_xg(80, 0, "wrist", "power-play") > shot_xg(80, 0, "wrist", "even-strength")
    assert shot_xg(-80, 0) == shot_xg(80, 0)  # either net
    assert shot_xg(80, 0, strength="empty-net") is None
    assert shot_xg(80, 0, strength="penalty-shot") == 0.33
    assert shot_type("Tom Wilson Snap Shot saved by Jakub Dobes") == "snap"
    assert shot_type("Deflected shot") == "deflect"
    assert shot_type("Shot") == "wrist"


def test_play_xg_only_counts_unblocked_shots_with_a_location():
    assert play_xg(_play("Shot", BUF, 75, 5)) is not None
    assert play_xg(_play("Blocked", BUF, 60, 5)) is None
    assert play_xg({"type": {"text": "Shot"}, "team": {"id": BUF}, "text": "Wrist Shot"}) is None


def test_game_xg_splits_sides_and_drops_the_shootout():
    plays = [
        _play("Shot", BUF, 80, 0),
        _play("Goal", BUF, 85, 2, "Snap Shot", "power-play"),
        _play("Missed", CBJ, -70, 10),
        _play("Blocked", CBJ, -50, 0),
        _play("Goal", CBJ, -80, 0, period=5),
        _play("Goal", BUF, 88, 0, strength="empty-net", period=3),
    ]
    out = game_xg(plays, home_id=BUF, away_id=CBJ)
    assert out["home"]["attempts"] == 3 and out["home"]["goals"] == 2
    assert out["home"]["located"] == 2  # the empty-net goal is not a chance on a goalie
    assert out["away"]["attempts"] == 1 and out["away"]["goals"] == 0
    assert 0 < out["away"]["xg"] < out["home"]["xg"] < 1


def _games():
    buf_box = {"powerPlayGoals": 1, "powerPlayOpportunities": 3, "shortHandedGoals": 0, "shotsTotal": 30}
    cbj_box = {"powerPlayGoals": 0, "powerPlayOpportunities": 2, "shortHandedGoals": 1, "shotsTotal": 20}
    det_box = {"powerPlayGoals": 2, "powerPlayOpportunities": 4, "shortHandedGoals": 0, "shotsTotal": 25}
    buf_shots = [_play("Shot", BUF, 80, 0)] * 10 + [_play("Goal", BUF, 85, 0), _play("Goal", BUF, 84, 3, "Snap Shot", "power-play"), _play("Goal", BUF, 82, 1)]
    cbj_shots = [_play("Missed", CBJ, -50, 20)] * 8 + [_play("Goal", CBJ, -60, 10, "Wrist Shot", "short-handed")]
    det_shots = [_play("Shot", DET, 70, 5)] * 9 + [_play("Goal", DET, 80, 0, "Slap Shot", "power-play")] * 2 + [_play("Goal", DET, 86, 0)] * 2
    return [
        # Regular season: BUF 3-1 CBJ at home
        _summary("r1", "2026-10-01T23:00Z", BUF, CBJ, home_goals=3, away_goals=1, plays=buf_shots + cbj_shots, box={BUF: buf_box, CBJ: cbj_box}),
        # Regular season: DET 4-1 CBJ, DET home; CBJ's one goal again shorthanded
        _summary("r2", "2026-10-03T23:00Z", DET, CBJ, home_goals=4, away_goals=1, plays=det_shots + cbj_shots, box={DET: det_box, CBJ: cbj_box}),
        # Preseason, ignored once there is regular-season play
        _summary("p1", "2026-09-22T23:00Z", CBJ, BUF, home_goals=5, away_goals=0, plays=buf_shots, box={BUF: buf_box, CBJ: cbj_box}, season_type=1),
        # Tomorrow, ignored
        _summary("r9", "2026-10-11T23:00Z", DET, BUF, home_goals=1, away_goals=0, plays=det_shots, box={BUF: buf_box, DET: det_box}),
    ]


def test_game_record_reads_goals_shots_xg_and_special_teams():
    rec = game_record(_games()[0])
    assert rec["id"] == "r1" and rec["day"] == "2026-10-01" and rec["plays"] is True
    assert rec["home"]["abbr"] == "BUF" and rec["home"]["goals"] == 3 and rec["away"]["goals"] == 1
    assert rec["home"]["attempts"] == 13 and rec["away"]["attempts"] == 9
    assert rec["home"]["sog"] == 30 and rec["home"]["ppg"] == 1 and rec["home"]["ppo"] == 3
    assert rec["away"]["shg"] == 1
    assert game_record(_summary("live", "2026-10-10T23:00Z", BUF, CBJ, home_goals=0, away_goals=0, plays=[], box={BUF: {}, CBJ: {}}, state="in")) is None


def test_build_power_ranks_offence_defence_and_special_teams():
    records = [game_record(s) for s in _games()]
    board = build_power(records, today=TODAY, season_year=2027, season_label="2026-27")
    assert board["season_kind"] == "regular season" and board["games"] == 2
    teams = board["teams"]
    buf, cbj, det = teams[BUF], teams[CBJ], teams[DET]
    league = board["league"]

    assert league["par_power"] == PAR_POWER and league["teams"] == 3 and league["teams_ranked"] == 1
    # 9 goals over 4 team-games
    assert league["goals_per_game"] == 2.25
    assert cbj["games"] == 2 and buf["games"] == 1 and det["games"] == 1
    # CBJ is the only club with two finals, so it is the only ranked one
    assert cbj["offense_rank"] == 1 and buf["offense_rank"] is None
    # CBJ creates the least and allows the most; DET and BUF are above par
    assert cbj["offense_power"] < PAR_POWER < det["offense_power"]
    assert cbj["defense_power"] < PAR_POWER < buf["defense_power"]
    assert not cbj["potent"] and not cbj["solid"] and buf["solid"]
    assert det["scoring_power"] > buf["scoring_power"] > cbj["scoring_power"]

    # Power play: DET 2 of 4 goals on the PP (50%), 2 for 4; CBJ 0 for 4 across two games
    assert det["pp"] == {"games": 1, "goals": 2, "opps": 4, "pct": 50.0, "of_goals": 4, "share": 50.0, "sh_goals": 0, "rank": None}
    assert cbj["pp"]["goals"] == 0 and cbj["pp"]["opps"] == 4 and cbj["pp"]["pct"] == 0.0 and cbj["pp"]["share"] == 0.0
    assert cbj["pp"]["sh_goals"] == 2
    # Penalty kill: CBJ was shorthanded 3 + 4 times and allowed 1 + 2 power-play goals
    assert cbj["pk"]["allowed"] == 3 and cbj["pk"]["times_short"] == 7 and cbj["pk"]["pct"] == 57.1
    assert cbj["pk"]["share"] == 43.0  # 3 of the 7 goals it allowed
    assert cbj["pk"]["rank"] == 1
    assert buf["pk"]["allowed"] == 0 and buf["pk"]["times_short"] == 2 and buf["pk"]["pct"] == 100.0
    assert league["pp_pct"] == 27.3  # 3 for 11 league-wide

    # First period: BUF put 13 on goal in the first (10 saved + 3 goals) and finished with 3;
    # CBJ's misses are not on goal, so its one goal is its only first-period shot in each game.
    assert [b["label"] for b in buf["first_period"]["buckets"]] == ["0–4", "5–9", "10–14", "15–19", "20+"]
    assert buf["first_period"]["games"] == 1
    assert buf["first_period"]["buckets"][2] == {"label": "10–14", "low": 10, "high": 14, "games": 1, "goals": 3, "wins": 1, "avg": 3.0}
    assert cbj["first_period"]["buckets"][0] == {"label": "0–4", "low": 0, "high": 4, "games": 2, "goals": 2, "wins": 0, "avg": 1.0}
    assert cbj["first_period"]["goals_per_game"] == 1.0
    assert league["first_period"]["games"] == 4 and league["first_period"]["buckets"][2]["games"] == 2
    assert league["first_period"]["buckets"][2]["avg"] == 3.5  # BUF 3 and DET 4

    # No NHL shift charts in these fixtures: lines are empty but well-formed
    assert cbj["lines"]["games"] == 0 and cbj["lines"]["scored"]["total"] == 0
    assert cbj["lines"]["scored"]["line_pct"] == [None, None, None, None]
    assert board["lines_note"]


def test_build_power_falls_back_to_preseason():
    records = [game_record(s) for s in _games() if s["header"]["season"]["type"] == 1]
    board = build_power(records, today=TODAY, season_year=2027, season_label="2026-27")
    assert board["season_kind"] == "preseason" and board["games"] == 1


def _scoreboard(events):
    return {
        "leagues": [{"season": {"year": 2027, "displayName": "2026-27", "startDate": "2026-09-15T07:00Z"}}],
        "events": events,
    }


def _event(summary, name="STATUS_FINAL"):
    header = summary["header"]
    comp = header["competitions"][0]
    return {
        "id": header["id"],
        "date": comp["date"],
        "season": header["season"],
        "competitions": [{"status": {"type": {"name": name}}}],
    }


def _fetch_for(summaries):
    by_id = {s["header"]["id"]: s for s in summaries}
    calls = []

    def fetch(url: str) -> dict:
        calls.append(url)
        if url.startswith(SCOREBOARD_URL):
            if "dates=" not in url:
                return _scoreboard([])
            return _scoreboard([_event(s) for s in summaries if s["header"]["season"]["type"] != 1] + [_event(s, "STATUS_SCHEDULED") for s in summaries if s["header"]["season"]["type"] == 1])
        for eid, s in by_id.items():
            if url == SUMMARY_URL.format(event_id=eid):
                return s
        raise AssertionError(f"unexpected url {url}")

    return fetch, calls


def test_feed_fetches_each_final_once_and_serves_in_the_background():
    fetch, calls = _fetch_for(_games())
    feed = PowerFeed(fetch, workers=2)
    first = feed.get(today=TODAY)
    assert first.get("loading") is True and first["teams"] == {}
    feed._worker.join(timeout=5)
    board = feed.get(today=TODAY)
    assert board["games"] == 2 and set(board["teams"]) == {BUF, CBJ, DET}
    summaries_fetched = [u for u in calls if "summary" in u]
    assert len(summaries_fetched) == 3  # r1, r2, r9 (r9 is tomorrow and dropped by the build)
    feed.refresh(today=TODAY)
    assert len([u for u in calls if "summary" in u]) == 3  # records are kept
    again = feed.get(today=TODAY)
    assert again["games"] == 2 and "loading" not in again


class _FakeLines:
    """Stands in for the NHL feeds: lines for r1 only, and r2 lands on the second ask."""

    def __init__(self):
        self.asked = []

    def for_record(self, record, now):
        self.asked.append(record["id"])
        units = lambda line, pair, total: {"line": line, "pair": pair, "line_none": 0, "pair_none": 0, "total": total}  # noqa: E731
        if record["id"] == "r1":
            return {
                "home": {"lines": [], "pairs": [], "for": units([2, 1, 0, 0], [3, 0, 0], 3), "against": units([0, 0, 1, 0], [0, 1, 0], 1)},
                "away": {"lines": [], "pairs": [], "for": units([0, 0, 1, 0], [0, 1, 0], 1), "against": units([2, 1, 0, 0], [3, 0, 0], 3)},
                "goals": [],
            }
        if record["id"] == "r2" and self.asked.count("r2") >= 2:
            return {
                "home": {"lines": [], "pairs": [], "for": units([4, 0, 0, 0], [4, 0, 0], 4), "against": units([0, 0, 0, 1], [0, 0, 1], 1)},
                "away": {"lines": [], "pairs": [], "for": units([0, 0, 0, 1], [0, 0, 1], 1), "against": units([4, 0, 0, 0], [4, 0, 0], 4)},
                "goals": [],
            }
        return None


def test_feed_attaches_lines_and_keeps_asking_until_the_nhl_has_them():
    fetch, _calls = _fetch_for(_games())
    source = _FakeLines()
    feed = PowerFeed(fetch, workers=1, lines=source)
    board = feed.refresh(today=TODAY)
    cbj = board["teams"][CBJ]
    # Only r1 has lines so far: CBJ allowed 3 there (two on L1, one on L2) and scored once from L3.
    assert cbj["lines"]["games"] == 1
    assert cbj["lines"]["scored"]["line"] == [0, 0, 1, 0] and cbj["lines"]["scored"]["line_pct"] == [0.0, 0.0, 100.0, 0.0]
    assert cbj["lines"]["allowed"]["line"] == [2, 1, 0, 0] and cbj["lines"]["allowed"]["pair"] == [3, 0, 0]
    assert board["league"]["lines"]["games"] == 1 and board["league"]["lines"]["scored"]["total"] == 4

    board = feed.refresh(today=TODAY)
    cbj = board["teams"][CBJ]
    assert cbj["lines"]["games"] == 2
    assert cbj["lines"]["scored"]["line"] == [0, 0, 1, 1] and cbj["lines"]["scored"]["line_pct"] == [0.0, 0.0, 50.0, 50.0]
    assert cbj["lines"]["allowed"]["line"] == [6, 1, 0, 0] and cbj["lines"]["allowed"]["line_pct"] == [86.0, 14.0, 0.0, 0.0]
    assert cbj["lines"]["allowed"]["total"] == 7
    # r1 was never asked again once it had lines; r9 (tomorrow) is still asked, r2 twice before it landed
    assert source.asked.count("r1") == 1 and source.asked.count("r2") == 2


def test_power_endpoint_and_page_shell():
    from eeehoc.dashboard import DashboardState, make_handler

    fetch, _calls = _fetch_for(_games())
    state = DashboardState(power=PowerFeed(fetch))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        board = json.loads(urllib.request.urlopen(base + "/api/power?wait=1", timeout=5).read())
        assert board["league"]["par_power"] == PAR_POWER
        assert board["teams"][CBJ]["pk"]["allowed"] == 3
        script = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert board["teams"][BUF]["first_period"]["buckets"][2]["goals"] == 3
        assert "lines" in board["teams"][BUF] and "first_period" in board["league"]
        script = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert all(token in script for token in ("/api/power", "powerHtml", "xgRow", "Expected chances", "PP goals", "PK allowed"))
        assert all(token in script for token in ("linesHtml", "firstPeriodHtml", "Goals by line", "1st-period shots"))
        css = urllib.request.urlopen(base + "/static/app.css", timeout=5).read().decode()
        assert ".pwr-grid" in css and ".gbl-bar" in css and ".p1b-grid" in css
    finally:
        httpd.shutdown()
        httpd.server_close()
