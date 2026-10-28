from eeehoc.live import _penalties
from eeehoc.penalties import power_plays


def _pen(side, at, minutes=2, who="A. Player", what="Tripping"):
    units = {2: [120], 4: [120, 120], 5: [300]}[minutes]
    return {
        "side": side,
        "at": at,
        "units": units,
        "kind": "major" if minutes == 5 else "minor",
        "label": f"{who} {what.lower()}",
        "what": what,
        "who": who,
        "when": f"{at // 1200 + 1} {at % 1200 // 60}:{at % 60:02d}",
    }


def test_a_minor_is_two_minutes_for_the_other_side():
    out = power_plays([_pen("away", 300)], [], end=3600, live=False)
    assert out["home"] == {"count": 1, "seconds": 120, "goals": 0, "spans": 1}
    assert out["away"] == {"count": 0, "seconds": 0, "goals": 0, "spans": 0}
    span = out["spans"][0]
    assert (span["side"], span["start"], span["end"], span["strength"], span["period"], span["open"]) == ("home", 300, 420, "5-on-4", 1, False)
    assert span["penalties"] == [{"label": "A. Player tripping", "when": "1 5:00", "kind": "minor", "ended_by_goal": False}]


def test_a_power_play_goal_ends_the_minor_but_not_a_major():
    out = power_plays([_pen("away", 300)], [{"side": "home", "at": 350, "pp": True}], end=3600, live=False)
    assert out["spans"][0]["end"] == 350 and out["spans"][0]["goals"] == 1 and out["spans"][0]["penalties"][0]["ended_by_goal"]
    assert out["home"]["seconds"] == 50 and out["home"]["goals"] == 1

    out = power_plays([_pen("away", 300, 5, what="Boarding")], [{"side": "home", "at": 350, "pp": True}, {"side": "home", "at": 500, "pp": True}], end=3600, live=False)
    assert out["spans"][0]["end"] == 600 and out["spans"][0]["goals"] == 2 and out["home"]["count"] == 1

    # A short-handed goal changes nothing
    out = power_plays([_pen("away", 300)], [{"side": "away", "at": 350, "pp": False}], end=3600, live=False)
    assert out["spans"][0]["end"] == 420 and out["spans"][0]["goals"] == 0


def test_two_penalties_make_a_five_on_three_and_count_as_two():
    out = power_plays([_pen("away", 300, who="A"), _pen("away", 360, who="B")], [], end=3600, live=False)
    assert len(out["spans"]) == 1
    span = out["spans"][0]
    assert (span["start"], span["end"], span["strength"]) == (300, 480, "5-on-3")
    assert [(s["start"], s["end"], s["strength"]) for s in span["segments"]] == [(300, 360, "5-on-4"), (360, 420, "5-on-3"), (420, 480, "5-on-4")]
    assert out["home"] == {"count": 2, "seconds": 180, "goals": 0, "spans": 1}
    # The goal ends the older penalty first
    out = power_plays([_pen("away", 300, who="A"), _pen("away", 360, who="B")], [{"side": "home", "at": 400}], end=3600, live=False)
    assert [(s["start"], s["end"], s["strength"]) for s in out["spans"][0]["segments"]] == [(300, 360, "5-on-4"), (360, 400, "5-on-3"), (400, 480, "5-on-4")]
    assert out["spans"][0]["penalties"][0]["ended_by_goal"] and not out["spans"][0]["penalties"][1]["ended_by_goal"]


def test_coincidental_minors_cancel_and_a_third_penalty_still_counts():
    pens = [_pen("home", 1680, who="A", what="Roughing"), _pen("away", 1680, who="B", what="Roughing"), _pen("home", 1680, who="C", what="Tripping")]
    out = power_plays(pens, [], end=3600, live=False)
    assert len(out["spans"]) == 1 and out["spans"][0]["side"] == "away"
    assert out["spans"][0]["penalties"][0]["label"] == "C tripping"
    # Fighting majors cancel too
    out = power_plays([_pen("home", 100, 5, who="A", what="Fighting"), _pen("away", 100, 5, who="B", what="Fighting")], [], end=3600, live=False)
    assert out["spans"] == []


def test_offsetting_penalties_at_different_times_give_four_on_four_then_an_edge():
    out = power_plays([_pen("home", 1664, who="A"), _pen("away", 1700, who="B")], [], end=3600, live=False)
    assert [(s["side"], s["start"], s["end"]) for s in out["spans"]] == [("away", 1664, 1700), ("home", 1784, 1820)]
    # Both penalties count: each side had the edge for part of its opponent's penalty
    assert out["away"]["count"] == 1 and out["home"]["count"] == 1


def test_double_minor_and_two_penalties_to_one_player_are_served_back_to_back():
    out = power_plays([_pen("away", 300, 4, what="High sticking")], [{"side": "home", "at": 350}], end=3600, live=False)
    # The goal ends the first half; the second half starts right away and runs its two minutes
    assert [(s["start"], s["end"], s["strength"]) for s in out["spans"][0]["segments"]] == [(300, 470, "5-on-4")]
    assert out["home"]["count"] == 2 and out["home"]["seconds"] == 170

    out = power_plays([_pen("away", 2340, who="Z", what="Elbowing"), _pen("away", 2340, who="Z", what="Roughing")], [], end=3600, live=False)
    assert out["spans"][0]["strength"] == "5-on-4" and out["spans"][0]["end"] == 2580 and out["home"]["count"] == 2


def test_a_third_penalty_waits_and_time_stops_at_the_clock():
    pens = [_pen("away", 300, who="A"), _pen("away", 310, who="B"), _pen("away", 320, who="C")]
    out = power_plays(pens, [], end=3600, live=False)
    span = out["spans"][0]
    # C starts when A ends at 420 and runs to 540
    assert (span["start"], span["end"]) == (300, 540) and out["home"]["count"] == 3

    # Live: the clock is at 350, so the span is open and cut there
    out = power_plays(pens, [], end=350, live=True)
    assert out["spans"][0]["end"] == 350 and out["spans"][0]["open"] is True and out["home"]["seconds"] == 50
    # A penalty with no time left is not an opportunity
    out = power_plays([_pen("away", 3600)], [], end=3600, live=False)
    assert out["spans"] == [] and out["home"]["count"] == 0


def test_overtime_penalty_reads_four_on_three():
    out = power_plays([_pen("home", 3650)], [], end=3900, live=False)
    assert out["spans"][0]["strength"] == "4-on-3" and out["spans"][0]["period"] == 4 and out["spans"][0]["side"] == "away"


def test_penalties_are_read_from_the_play_by_play():
    def play(kind, minutes, text, period, clock, team, who=None):
        p = {
            "type": {"text": text, "penaltyMinutes": str(minutes), "penaltyType": kind},
            "text": text,
            "period": {"number": period},
            "clock": {"displayValue": clock},
            "team": {"id": team},
        }
        if who:
            p["participants"] = [{"athlete": {"shortName": who}}]
        return p

    plays = [
        play("Minor", 2, "Tripping", 1, "5:09", "20", "B. Hagel"),
        play("Bench", 2, "Too Many Men on the Ice", 2, "17:17", "17"),
        play("Double Minor", 4, "High sticking", 2, "1:11", "20", "C. Verhaeghe"),
        play("Major", 5, "Fighting", 1, "16:49", "17", "L. Stanley"),
        play("Misconduct", 10, "Misconduct", 1, "16:49", "17", "L. Stanley"),
        play("Game", 10, "Game Misconduct", 1, "16:49", "17", "L. Stanley"),
        play("PS", 0, "PS-Hooking on breakaway", 2, "4:09", "20", "E. Karlsson"),
        {"type": {"text": "Shot"}, "period": {"number": 1}, "clock": {"displayValue": "1:00"}, "team": {"id": "20"}},
    ]
    pens = _penalties(plays, "17", "20", ot_seconds=300, playoffs=False)
    assert [(p["side"], p["at"], p["units"], p["kind"], p["label"], p["who"], p["when"]) for p in pens] == [
        ("away", 309, [120], "minor", "B. Hagel tripping", "B. Hagel", "1st 5:09"),
        ("home", 2237, [120], "minor", "Too Many Men on the Ice", "", "2nd 17:17"),
        ("away", 1271, [120, 120], "minor", "C. Verhaeghe high sticking", "C. Verhaeghe", "2nd 1:11"),
        ("home", 1009, [300], "major", "L. Stanley fighting", "L. Stanley", "1st 16:49"),
    ]
