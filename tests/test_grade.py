"""Live grade: a team and its goalie against their own usual by the same point of past games."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from eeehoc.dashboard import DashboardState, make_handler
from eeehoc.grade import (
    FULL_GAME,
    PARTS,
    GoalieGame,
    GradeBoard,
    GradeIndex,
    SideGame,
    blend,
    count_percentile,
    game_sides,
    grade_game,
    grade_goalie,
    grade_side,
    letter_for,
)
from eeehoc.penalties import power_plays

HOME, AWAY = "1", "2"
HOME_GOALIE, AWAY_GOALIE, AWAY_BACKUP = "91", "92", "93"


def _clock(at):
    return {"period": {"number": at // 1200 + 1}, "clock": {"displayValue": f"{at % 1200 // 60}:{at % 60:02d}"}}


def _shot(kind, team, at, *, strength="even-strength", goalie=None, x=75, y=5):
    play = {
        "type": {"text": kind},
        "team": {"id": team},
        "coordinate": {"x": x, "y": y},
        "text": "Wrist Shot",
        "strength": {"abbreviation": strength},
        **_clock(at),
    }
    if kind == "Goal":
        play["scoringPlay"] = True
    people = [{"athlete": {"id": "7", "shortName": "A. Shooter"}, "type": "scorer" if kind == "Goal" else "shooter"}]
    if goalie and kind == "Shot":
        people.append({"athlete": {"id": goalie, "shortName": f"G. Tender{goalie}"}, "type": "saver"})
    if goalie and kind == "Missed":
        people.append({"athlete": {"id": goalie, "shortName": f"G. Tender{goalie}"}})
    play["participants"] = people
    return play


def _faceoff(team, at):
    return {"type": {"text": "Face Off"}, "team": {"id": team}, **_clock(at)}


def _penalty(team, at):
    return {
        "type": {"text": "Tripping", "penaltyMinutes": "2", "penaltyType": "Minor"},
        "text": "Tripping",
        "team": {"id": team},
        "participants": [{"athlete": {"shortName": "B. Offender"}}],
        **_clock(at),
    }


def _box(*goalies):
    """Box score goalie blocks: ``(team, id)`` for each goalie who dressed and played."""
    groups: dict[str, list[dict]] = {}
    for team, goalie_id in goalies:
        groups.setdefault(team, []).append(
            {"athlete": {"id": goalie_id, "shortName": f"G. Tender{goalie_id}"}, "stats": ["20", "1", "21", "60:00"]}
        )
    return {
        "players": [
            {"team": {"id": team}, "statistics": [{"name": "goalies", "labels": ["SV", "GA", "SA", "TOI"], "athletes": rows}]}
            for team, rows in groups.items()
        ]
    }


def _summary(plays, *goalies):
    return {"plays": plays, "boxscore": _box(*(goalies or ((HOME, HOME_GOALIE), (AWAY, AWAY_GOALIE))))}


def _side(team=HOME, *, event="e", start="2026-10-01T23:00Z", sog=(), goals=(), shots=None, other=None, **more):
    """A side whose shots on goal (and attempts) came at the given seconds."""
    game = SideGame(event_id=event, start=start, team_id=team, sog=sorted(sog), goals=sorted(goals), **more)
    game.shots = sorted(shots if shots is not None else sog)
    game.other = other or SideGame(event_id=event, start=start, team_id="x")
    game.other.other = game
    return game


def _record(event, start, home, away):
    home.event_id = away.event_id = event
    home.start = away.start = start
    home.other, away.other = away, home
    return {"id": event, "date": start, "timeline": {"home": home, "away": away}}


def _history(team, per_period, n=10, *, opponent="9"):
    """``n`` past games in which ``team`` put ``per_period`` shots on goal in every period, evenly spaced."""
    records = []
    for i in range(n):
        step = 1200 // per_period
        mine = [p * 1200 + k * step + step // 2 for p in range(3) for k in range(per_period)]
        records.append(
            _record(
                f"h{team}{i}",
                f"2026-10-{i + 1:02d}T23:00Z",
                SideGame(event_id="", start="", team_id=team, sog=mine, shots=list(mine)),
                SideGame(event_id="", start="", team_id=opponent, sog=list(mine), shots=list(mine)),
            )
        )
    return records


def _part(graded, key):
    return next(p for p in graded["parts"] if p["key"] == key)


# ------------------------------------------------------------ reading a game


def test_game_sides_counts_shots_attempts_and_power_play_work_per_side():
    plays = [
        _faceoff(HOME, 0),
        _shot("Shot", HOME, 60, goalie=AWAY_GOALIE),
        _shot("Missed", HOME, 90, goalie=AWAY_GOALIE),
        # ESPN credits a block to the blocker: this is an AWAY attempt.
        {"type": {"text": "Blocked"}, "team": {"id": HOME}, "strength": {"abbreviation": "even-strength"}, **_clock(120)},
        _faceoff(AWAY, 130),
        _penalty(AWAY, 200),
        _shot("Shot", HOME, 230, strength="power-play", goalie=AWAY_GOALIE),
        _shot("Missed", HOME, 240, strength="power-play", goalie=AWAY_GOALIE),
        _shot("Goal", HOME, 260, strength="power-play"),
        _shot("Shot", AWAY, 400, goalie=HOME_GOALIE),
    ]
    sides = game_sides(_summary(plays), HOME, AWAY, end=600, live=True)
    home, away = sides["home"], sides["away"]
    assert home.sog == [60, 230, 260] and home.goals == [260]
    assert home.shots == [60, 90, 230, 240, 260]  # on goal and missed
    assert away.shots == [120, 400] and away.sog == [400]  # the blocked attempt and the save
    # Only shots on goal count as power-play shots; the power play ends on its goal.
    assert home.pp_sog == [230, 260] and home.pp_goals == [260]
    assert home.pp_starts == [200] and home.pp_spans == [(200, 260)] and home.pp_seconds(FULL_GAME) == 60
    assert home.pp_seconds(230) == 30 and away.pp_starts == []
    assert home.faceoffs(FULL_GAME) == (1, 2) and away.faceoffs(100) == (0, 1)
    assert home.count("sog", 100) == 1 and home.count("sog", FULL_GAME) == 3
    assert home.count("xg", 100) > 0 and home.count("xg", FULL_GAME) > home.count("xg", 100)
    assert home.other is away and away.other is home


def test_game_sides_gives_each_goalie_his_own_shots_and_leaves_the_empty_net_out():
    plays = [
        # Before any save names him, the starter is not known from the plays.
        _shot("Goal", HOME, 30),
        _shot("Shot", HOME, 60, goalie=AWAY_GOALIE),
        _shot("Goal", HOME, 300),
        # The backup comes in: the miss names him, and the goal after it is his.
        _shot("Missed", HOME, 900, goalie=AWAY_BACKUP),
        _shot("Goal", HOME, 950),
        _shot("Shot", HOME, 1000, goalie=AWAY_BACKUP),
        _shot("Goal", HOME, 3500, strength="empty-net"),
        _shot("Shot", AWAY, 100, goalie=HOME_GOALIE),
    ]
    summary = _summary(plays, (HOME, HOME_GOALIE), (AWAY, AWAY_GOALIE), (AWAY, AWAY_BACKUP))
    sides = game_sides(summary, HOME, AWAY)
    starter, backup = sides["away"].goalies[AWAY_GOALIE], sides["away"].goalies[AWAY_BACKUP]
    assert (starter.shots(FULL_GAME), starter.goals(FULL_GAME)) == (3, 2)
    assert (backup.shots(FULL_GAME), backup.goals(FULL_GAME)) == (2, 1)
    assert starter.allowed == [30, 300] and backup.allowed == [950]
    # The miss is a chance he faced but not a shot on goal; the empty-net goal is nobody's.
    assert len(backup.xg_at) == 3 and backup.faced == [950, 1000]
    assert sides["home"].goals == [30, 300, 950, 3500]
    assert sides["home"].goalies[HOME_GOALIE].shots(FULL_GAME) == 1
    assert starter.name == "Tender92"  # as the rink writes him


def test_game_sides_falls_back_on_the_box_score_for_a_goalie_who_never_made_a_save():
    sides = game_sides(_summary([_shot("Goal", HOME, 30), _shot("Goal", HOME, 80)]), HOME, AWAY)
    goalie = sides["away"].goalies[AWAY_GOALIE]
    assert (goalie.shots(FULL_GAME), goalie.goals(FULL_GAME)) == (2, 2)
    assert game_sides({"plays": []}, HOME, AWAY) is None


def test_power_plays_report_when_each_opportunity_started():
    pens = [
        {"side": "away", "at": 100, "units": [120], "kind": "minor"},
        {"side": "away", "at": 1000, "units": [120], "kind": "minor"},
        {"side": "home", "at": 2000, "units": [120], "kind": "minor"},
    ]
    out = power_plays(pens, [], end=3600, live=False)
    assert out["starts"] == {"home": [100, 1000], "away": [2000]}
    assert out["home"]["count"] == 2 and out["away"]["count"] == 1


# ------------------------------------------------------------------ the maths


def test_count_percentile_puts_the_usual_in_the_middle():
    assert abs(count_percentile(10, 10.0) - 0.5) < 0.07
    assert count_percentile(16, 10.0) > 0.95 and count_percentile(4, 10.0) < 0.05
    assert count_percentile(0, 0.0) == 0.5 and count_percentile(1, 0.0) == 1.0
    # A wider spread makes the same miss less remarkable.
    assert count_percentile(16, 10.0, 2.0) < count_percentile(16, 10.0, 1.0)
    assert count_percentile(4, 10.0, 2.0) > count_percentile(4, 10.0, 1.0)
    values = [count_percentile(k, 6.0, 1.5) for k in range(20)]
    assert values == sorted(values) and all(0 <= v <= 1 for v in values)


def test_letters_and_blend():
    assert [letter_for(s) for s in (99, 75, 74, 60, 59, 40, 39, 25, 24, 1)] == list("AABBCCDDFF")
    assert blend([(0.5, 1.0), (0.5, 2.0)]) == 50.0 and blend([]) == 50.0
    # Several reads that agree say more than any one of them.
    assert blend([(0.7, 1.0)] * 6) > 70 > blend([(0.7, 1.0), (0.3, 1.0), (0.7, 1.0)])
    assert blend([(0.3, 1.0)] * 6) < 30
    # The heavier read wins a disagreement.
    assert blend([(0.9, 3.0), (0.1, 1.0)]) > 50 > blend([(0.9, 1.0), (0.1, 3.0)])
    assert 0 < blend([(0.0, 1.0)]) < blend([(1.0, 1.0)]) < 100


def test_weights_add_up():
    assert abs(sum(part.weight for part in PARTS) - 1.0) < 1e-9
    assert len({part.key for part in PARTS}) == len(PARTS)


# ------------------------------------------------------------ against the usual


def test_the_same_count_grades_differently_early_and_late():
    index = GradeIndex(_history(HOME, 10))  # ten on goal a period, every game
    past = index.history(HOME, event_id="now", start="2026-11-01T23:00Z")
    assert len(past) == 10
    five = [100, 200, 300, 400, 500]

    early = grade_side(index, _side(sog=five), past, at=600)  # usual by 10:00 is five
    late = grade_side(index, _side(sog=five), past, at=1200)  # usual by 20:00 is ten
    assert _part(early, "sog")["usual"] == 5.0 and _part(late, "sog")["usual"] == 10.0
    assert _part(early, "sog")["actual"] == _part(late, "sog")["actual"] == 5
    assert 40 <= _part(early, "sog")["score"] <= 60
    assert _part(late, "sog")["score"] < 15
    assert early["score"] > late["score"]
    assert early["games"] == 10 and early["basis"] == "club"


def test_usual_is_the_clubs_own_not_the_leagues():
    index = GradeIndex(_history(HOME, 14) + _history(AWAY, 6, opponent="8"))
    ten = [60 * k for k in range(1, 11)]
    when = {"event_id": "now", "start": "2026-11-01T23:00Z"}
    busy = grade_side(index, _side(HOME, sog=ten), index.history(HOME, **when), at=1200)
    quiet = grade_side(index, _side(AWAY, sog=ten), index.history(AWAY, **when), at=1200)
    # Ten on goal in a period is a slow one for the club that gets fourteen and a big one for the club that gets six.
    assert _part(busy, "sog")["score"] < 30 and _part(quiet, "sog")["score"] > 80
    assert _part(busy, "sog")["usual"] > 13 and _part(quiet, "sog")["usual"] < 7


def test_allowing_fewer_scores_higher_and_history_leaves_out_this_game_and_later_ones():
    index = GradeIndex(_history(HOME, 10))
    past = index.history(HOME, event_id="hx", start="2026-10-06T23:00Z")
    assert [g.event_id for g in past] == [f"h{HOME}{i}" for i in (4, 3, 2, 1, 0)]
    assert len(index.history(HOME, event_id=f"h{HOME}9", start="")) == 9

    tight = _side(other=SideGame(event_id="e", start="", team_id="x", sog=[100, 200]))
    loose = _side(other=SideGame(event_id="e", start="", team_id="x", sog=[40 * k for k in range(1, 21)]))
    good = _part(grade_side(index, tight, past, at=1200), "sog_against")
    bad = _part(grade_side(index, loose, past, at=1200), "sog_against")
    assert good["lower"] and good["actual"] == 2 and bad["actual"] == 20
    assert good["score"] > 90 and bad["score"] < 10


def test_power_play_reads_wait_for_power_play_time_and_use_the_clubs_rate():
    records = _history(HOME, 10)
    for rec in records:  # two minutes of power play a game, two shots on goal from it
        rec["timeline"]["home"].pp_spans = [(100, 220)]
        rec["timeline"]["home"].pp_starts = [100]
        rec["timeline"]["home"].pp_sog = [150, 200]
    index = GradeIndex(records)
    past = index.history(HOME, event_id="now", start="2026-11-01T23:00Z")

    none = grade_side(index, _side(), past, at=1200)
    assert {p["key"] for p in none["parts"]}.isdisjoint({"pp_sog", "pp_goals"}) and none["pp_seconds"] == 0

    live = _side(pp_spans=[(300, 420), (800, 920)], pp_starts=[300, 800], pp_sog=[310, 330, 350, 810, 830, 850, 870, 900])
    graded = grade_side(index, live, past, at=1200)
    shots = _part(graded, "pp_sog")
    # Four minutes at two shots per two minutes is four; eight is a big night.
    assert graded["pp_seconds"] == 240 and shots["actual"] == 8 and 3.0 < shots["usual"] < 4.5
    assert shots["score"] > 85
    assert _part(graded, "pp")["actual"] == 2


def test_a_club_with_no_games_is_read_against_the_league():
    index = GradeIndex(_history(HOME, 10))
    graded = grade_side(index, _side("77", sog=[100, 200]), [], at=1200)
    assert graded["basis"] == "league" and graded["games"] == 0
    assert _part(graded, "sog")["usual"] == 10.0
    assert grade_side(GradeIndex([]), _side("77"), [], at=1200) is None


# ------------------------------------------------------------------ the goalie


def _goalie_game(goalie_id, saves, goals, *, event="g", start="2026-10-01T23:00Z", each=0.1):
    game = GoalieGame(goalie_id, "G. Tender", event_id=event, start=start)
    game.faced = list(range(1, saves + goals + 1))
    game.allowed = list(range(1, goals + 1))
    game.xg_at = list(game.faced)
    game.xg_sum = [round(each * (i + 1), 4) for i in range(len(game.faced))]
    return game


def _goalie_index(goalie_id, games):
    records = []
    for i, game in enumerate(games):
        side = SideGame(event_id=f"g{i}", start=f"2026-10-{i + 1:02d}T23:00Z", team_id=HOME, goalies={goalie_id: game})
        game.event_id, game.start = side.event_id, side.start
        records.append(_record(side.event_id, side.start, side, SideGame(event_id="", start="", team_id=AWAY)))
    return GradeIndex(records)


def test_goalie_grade_is_goals_allowed_against_what_the_shots_were_worth():
    # Thirty shots a night worth three goals; he lets in three.
    index = _goalie_index("50", [_goalie_game("50", 27, 3) for _ in range(12)])
    past = index.goalie_history("50", event_id="now", start="2026-11-01T23:00Z")
    assert abs(index.goals_per_xg - 1.0) < 1e-6 and len(past) == 12

    usual = grade_goalie(index, _goalie_game("50", 27, 3), past, in_net=True)
    stood = grade_goalie(index, _goalie_game("50", 30, 0), past, in_net=True)
    leaky = grade_goalie(index, _goalie_game("50", 23, 7), past, in_net=False)
    assert 35 <= usual["score"] <= 65 and usual["expected"] == 3.0 and usual["saved_above"] == 0.0
    assert stood["score"] > 90 and stood["letter"] == "A" and stood["saved_above"] == 3.0
    assert leaky["score"] < 10 and leaky["letter"] == "F" and not leaky["in_net"]
    assert (usual["shots"], usual["saves"], usual["goals"], usual["save_pct"]) == (30, 27, 3, 0.9)
    assert usual["usual_save_pct"] == 0.9 and usual["games"] == 12 and usual["basis"] == "goalie"


def test_goalie_grade_is_against_his_own_rate_once_he_has_games():
    # A goalie who lets in half of what he should has a higher bar: the same three goals are an off night.
    stingy = [_goalie_game("60", 28, 2, each=4 / 30) for _ in range(20)]
    index = _goalie_index("60", stingy)
    index.goals_per_xg = 1.0
    past = index.goalie_history("60", event_id="now", start="2026-11-01T23:00Z")
    tonight = _goalie_game("60", 27, 3, each=4 / 30)
    own = grade_goalie(index, tonight, past, in_net=True)
    fresh = grade_goalie(index, tonight, [], in_net=True)
    assert own["expected"] < fresh["expected"] and own["score"] < fresh["score"]
    assert fresh["basis"] == "league" and fresh["usual_save_pct"] is None
    assert grade_goalie(index, GoalieGame("60", "G. Tender"), past, in_net=True) is None


def test_goalie_grade_falls_back_on_shots_when_there_are_no_locations():
    index = _goalie_index("50", [_goalie_game("50", 27, 3) for _ in range(12)])
    blind = GoalieGame("50", "G. Tender", faced=list(range(1, 31)), allowed=[5])
    graded = grade_goalie(index, blind, index.goalie_history("50", event_id="now", start=""), in_net=True)
    assert graded["xg"] == 0 and graded["expected"] == 3.0 and graded["score"] > 70


# ------------------------------------------------------------- the whole game


def _game(state="in", *, elapsed=1200, event="now"):
    return {
        "id": event,
        "state": state,
        "date": "2026-11-01T23:00Z",
        "elapsed_sec": elapsed,
        "playoffs": False,
        "ot_seconds": 300,
        "home": {"id": HOME},
        "away": {"id": AWAY},
    }


def _live_plays(home_shots, away_shots):
    plays = [_shot("Shot", HOME, at, goalie=AWAY_GOALIE) for at in home_shots]
    plays += [_shot("Shot", AWAY, at, goalie=HOME_GOALIE) for at in away_shots]
    plays += [_faceoff(HOME if k % 2 else AWAY, 50 * k) for k in range(1, 21)]
    return sorted(plays, key=lambda p: (p["period"]["number"], int(p["clock"]["displayValue"].split(":")[0])))


def test_grade_game_grades_both_sides_and_their_goalies():
    index = GradeIndex(_history(HOME, 10) + _history(AWAY, 10, opponent="8"))
    summary = _summary(_live_plays([50 * k for k in range(1, 19)], [300, 900]))
    graded = grade_game(_game(), summary, index)
    assert graded["at"] == 1200 and graded["final"] is False
    home, away = graded["home"], graded["away"]
    # Eighteen on goal against a usual ten; two against a usual ten.
    assert home["team"]["score"] > 75 and away["team"]["score"] < 25
    assert _part(home["team"], "sog")["actual"] == 18 and _part(away["team"], "sog")["actual"] == 2
    assert _part(home["team"], "faceoffs")["actual"] == 50
    assert [g["id"] for g in away["goalies"]] == [AWAY_GOALIE] and away["goalies"][0]["shots"] == 18
    assert away["goalies"][0]["goals"] == 0 and away["goalies"][0]["score"] > 60 and away["goalies"][0]["in_net"]
    assert home["goalies"][0]["shots"] == 2

    final = grade_game(_game("post", elapsed=3600), summary, index)
    # Against a full game's usual thirty, the same eighteen is a quiet night.
    assert final["final"] is True and _part(final["home"]["team"], "sog")["usual"] == 30.0
    assert _part(final["home"]["team"], "sog")["score"] < 10


def test_grade_game_waits_for_five_minutes_and_for_plays():
    index = GradeIndex(_history(HOME, 10))
    early = _summary([_shot("Shot", HOME, 100, goalie=AWAY_GOALIE)])
    assert grade_game(_game(elapsed=200), early, index) is None
    assert grade_game(_game(elapsed=400), early, index) is not None
    assert grade_game(_game("pre", elapsed=0), early, index) is None
    assert grade_game(_game(), None, index) is None and grade_game(_game(), {"plays": []}, index) is None


class _Live:
    def __init__(self, games, summaries):
        self.games, self.summaries, self.asked = games, summaries, []

    def get(self, *, force=False, dates=None):
        self.asked.append(dates)
        return {"games": self.games}

    def cached_summary(self, game_id):
        return self.summaries.get(str(game_id))


class _Power:
    def __init__(self, records):
        self._records = records

    def records(self):
        return list(self._records)


def test_grade_board_grades_the_board_and_says_when_the_season_is_still_loading():
    summary = _summary(_live_plays([50 * k for k in range(1, 19)], [300, 900]))
    games = [_game(), _game("pre", event="later"), _game(event="nosummary")]
    live = _Live(games, {"now": summary})

    loading = GradeBoard(live, _Power([])).get()
    assert loading["loading"] is True and loading["games"] == {} and loading["history_games"] == 0

    board = GradeBoard(live, _Power(_history(HOME, 10) + _history(AWAY, 10, opponent="8"))).get(dates="20261101")
    assert live.asked[-1] == "20261101"
    assert board["loading"] is False and board["history_games"] == 20 and board["min_seconds"] == 300
    assert list(board["games"]) == ["now"]
    assert set(board["weights"]) == {part.key for part in PARTS}
    assert board["games"]["now"]["home"]["team"]["letter"] == "A"
    json.dumps(board)


def test_grade_endpoint_serves_the_board_and_rejects_bad_dates():
    summary = _summary(_live_plays([50 * k for k in range(1, 19)], [300, 900]))
    live = _Live([_game()], {"now": summary})
    grade = GradeBoard(live, _Power(_history(HOME, 10)))
    state = DashboardState(live=live, grade=grade)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/grade?dates=20261101", timeout=5) as res:
            board = json.loads(res.read())
        assert board["games"]["now"]["at"] == 1200 and live.asked[-1] == "20261101"
        try:
            urllib.request.urlopen(f"{base}/api/grade?dates=nope", timeout=5)
        except urllib.error.HTTPError as err:
            assert err.code == 400
        else:
            raise AssertionError("bad dates were accepted")
    finally:
        server.shutdown()
        server.server_close()
