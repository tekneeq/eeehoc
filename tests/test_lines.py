"""Which line scored and which line was on for the goal against, from NHL shift charts."""

from __future__ import annotations

from eeehoc.lines import NHL_PBP_URL, NHL_SCHEDULE_URL, NHL_SHIFTS_URL, LineSource, game_lines, nhl_abbr

AWAY, HOME = 10, 20
# Away forwards 101-106 (two trios), defence 107-110 (two pairs), goalie 111.
# Home forwards 201-206, defence 207-210, goalie 211.
A_L1, A_L2, A_D1, A_D2 = [101, 102, 103], [104, 105, 106], [107, 108], [109, 110]
H_L1, H_L2, H_D1, H_D2 = [201, 202, 203], [204, 205, 206], [207, 208], [209, 210]


def _spot(team, pid, pos):
    return {"teamId": team, "playerId": pid, "positionCode": pos, "firstName": {"default": "P"}, "lastName": {"default": str(pid)}}


def _roster():
    spots = []
    for team, forwards, defence, goalie in ((AWAY, A_L1 + A_L2, A_D1 + A_D2, 111), (HOME, H_L1 + H_L2, H_D1 + H_D2, 211)):
        spots += [_spot(team, pid, "C") for pid in forwards]
        spots += [_spot(team, pid, "D") for pid in defence]
        spots.append(_spot(team, goalie, "G"))
    return spots


def _clock(seconds):
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _shifts():
    """Both clubs change on the minute all game: L1/D1 on even minutes, L2/D2 on odd ones.

    The first unit also takes the first ninety seconds of every period, so it
    plays more and comes out as line one.
    """
    rows = []

    def shift(pid, period, start, end):
        rows.append({"typeCode": 517, "playerId": pid, "period": period, "startTime": _clock(start), "endTime": _clock(end)})

    for period in (1, 2, 3):
        for minute in range(20):
            start, end = minute * 60, minute * 60 + 60
            first = minute % 2 == 0
            if minute == 1:
                start = 90  # the first unit stays on through 01:30
            if minute == 0:
                end = 90
            for pid in (A_L1 + A_D1 + H_L1 + H_D1) if first else (A_L2 + A_D2 + H_L2 + H_D2):
                shift(pid, period, start, end)
        for goalie in (111, 211):
            shift(goalie, period, 0, 1200)
    return {"data": rows}


def _goal(period, clock, owner, scorer, code="1551"):
    return {
        "typeDescKey": "goal",
        "periodDescriptor": {"number": period},
        "timeInPeriod": clock,
        "situationCode": code,
        "details": {"eventOwnerTeamId": owner, "scoringPlayerId": scorer},
    }


def _pbp(goals):
    return {"id": 2026020999, "homeTeam": {"id": HOME, "abbrev": "TBL"}, "awayTeam": {"id": AWAY, "abbrev": "LAK"}, "rosterSpots": _roster(), "plays": goals}


def test_game_lines_groups_units_and_credits_the_line_on_the_ice():
    goals = [
        _goal(1, "00:30", AWAY, 101),  # away L1/D1 scores on home L1/D1
        _goal(1, "01:45", HOME, 204),  # home L2/D2 scores on away L2/D2
        _goal(2, "03:00", HOME, 201, "1451"),  # a change at 03:00: the unit whose shift ends then was on; home power play
        _goal(3, "05:10", AWAY, 104, "0651"),  # away L2 with its own net empty, 6 on 5
        _goal(5, "00:10", AWAY, 101),  # shootout, dropped
    ]
    out = game_lines(_pbp(goals), _shifts())
    assert out is not None
    assert out["away"]["lines"] == [["P 101", "P 102", "P 103"], ["P 104", "P 105", "P 106"]]
    assert out["home"]["pairs"] == [["P 207", "P 208"], ["P 209", "P 210"]]

    assert out["away"]["for"] == {"line": [1, 1, 0, 0], "pair": [1, 1, 0], "line_none": 0, "pair_none": 0, "total": 2}
    assert out["home"]["against"] == {"line": [1, 1, 0, 0], "pair": [1, 1, 0], "line_none": 0, "pair_none": 0, "total": 2}
    assert out["home"]["for"] == {"line": [1, 1, 0, 0], "pair": [1, 1, 0], "line_none": 0, "pair_none": 0, "total": 2}
    assert out["away"]["against"]["line"] == [1, 1, 0, 0]

    assert [g["period"] for g in out["goals"]] == [1, 1, 2, 3]
    first, second, third, fourth = out["goals"]
    assert first["side"] == "away" and first["for_line"] == 1 and first["against_line"] == 1 and first["strength"] == "EV"
    assert second["for_line"] == 2 and second["against_pair"] == 2
    assert third["for_line"] == 1 and third["against_line"] == 1 and third["strength"] == "PP" and third["scorer"] == "P 201"
    assert fourth["strength"] == "PP"  # 6 on 5 with its own net empty


def test_game_lines_needs_a_shift_chart_and_rosters():
    assert game_lines(_pbp([]), {"data": []}) is None
    assert game_lines({"homeTeam": {"id": HOME}, "awayTeam": {"id": AWAY}, "rosterSpots": []}, _shifts()) is None


def test_nhl_abbreviations():
    assert nhl_abbr("TB") == "TBL" and nhl_abbr("LA") == "LAK" and nhl_abbr("NJ") == "NJD" and nhl_abbr("SJ") == "SJS"
    assert nhl_abbr("WSH") == "WSH" and nhl_abbr("uta") == "UTA"


def _schedule():
    return {
        "gameWeek": [
            {"date": "2026-10-14", "games": [{"id": 2026020999, "gameType": 2, "awayTeam": {"abbrev": "LAK"}, "homeTeam": {"abbrev": "TBL"}}]},
            {"date": "2026-10-15", "games": [{"id": 2026020777, "gameType": 2, "awayTeam": {"abbrev": "BUF"}, "homeTeam": {"abbrev": "CBJ"}}]},
        ]
    }


def test_line_source_matches_espn_finals_to_nhl_games_and_caches():
    calls = []

    def fetch(url):
        calls.append(url)
        if url == NHL_SCHEDULE_URL.format(day="2026-10-14"):
            return _schedule()
        if url == NHL_PBP_URL.format(game_id=2026020999):
            return _pbp([_goal(1, "00:30", AWAY, 101)])
        if url == NHL_SHIFTS_URL.format(game_id=2026020999):
            return _shifts()
        if url == NHL_SHIFTS_URL.format(game_id=2026020777):
            return {"data": []}  # not posted yet
        if url == NHL_PBP_URL.format(game_id=2026020777):
            return _pbp([])
        raise RuntimeError(f"offline {url}")

    source = LineSource(fetch)
    record = {"day": "2026-10-14", "away": {"abbr": "LA"}, "home": {"abbr": "TB"}}
    lines = source.for_record(record, now=1000.0)
    assert lines is not None and lines["nhl_id"] == 2026020999 and lines["away"]["for"]["total"] == 1
    assert source.for_record(record, now=1001.0) is lines
    assert len(calls) == 3  # schedule, play-by-play, shifts — once

    # The next day was in the same schedule week, so no schedule call; the shift chart is missing, so a miss for a while.
    later = {"day": "2026-10-15", "away": {"abbr": "BUF"}, "home": {"abbr": "CBJ"}}
    assert source.for_record(later, now=1002.0) is None
    assert len(calls) == 5
    assert source.for_record(later, now=1003.0) is None
    assert len(calls) == 5  # within MISS_TTL: no new requests
    assert source.for_record(later, now=1003.0 + 11 * 60) is None
    assert len(calls) == 7  # asked again after the miss expired

    # A game the NHL does not list is a miss too, and a bad abbreviation never reaches the network.
    assert source.for_record({"day": "2026-10-14", "away": {"abbr": "BUF"}, "home": {"abbr": "TB"}}, now=1004.0) is None
    assert "no NHL game BUF @ TBL" in (source.last_error or "")
    assert source.for_record({"day": "", "away": {"abbr": "BUF"}, "home": {"abbr": "TB"}}, now=1005.0) is None
