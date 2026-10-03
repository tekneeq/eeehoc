"""First-period score distribution."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from eeehoc.periods import PeriodFeed, first_period_bucket, period_one_complete


def _event(
    eid,
    iso,
    away_p1,
    home_p1,
    *,
    state="post",
    period=3,
    season_type=2,
    year=2027,
    status_name="STATUS_FINAL",
    detail="Final",
):
    def side(home_away, goals):
        return {
            "homeAway": home_away,
            "score": goals,
            "team": {"abbreviation": home_away[:3].upper(), "id": home_away},
            "linescores": [{"period": 1, "value": goals}],
        }

    return {
        "id": eid,
        "date": iso,
        "season": {"year": year, "type": season_type},
        "competitions": [
            {
                "id": eid,
                "date": iso,
                "competitors": [side("away", away_p1), side("home", home_p1)],
                "status": {
                    "period": period,
                    "type": {
                        "state": state,
                        "name": status_name,
                        "shortDetail": detail,
                        "completed": state == "post",
                    },
                },
            }
        ],
    }


def _payload(events):
    return {
        "leagues": [
            {
                "season": {
                    "year": 2027,
                    "displayName": "2026-27",
                    "startDate": "2026-09-15T07:00Z",
                    "type": {"type": 2, "name": "Regular Season"},
                }
            }
        ],
        "events": events,
    }


TODAY = date(2026, 10, 1)

EVENTS = [
    _event("pre", "2026-09-20T23:00Z", 2, 1, season_type=1),
    _event("reg-old", "2026-09-30T23:00Z", 0, 0),
    _event("before-season", "2026-08-15T23:00Z", 3, 0),
    _event("live-p1", "2026-10-01T23:30Z", 2, 0, state="in", period=1, status_name="STATUS_IN_PROGRESS", detail="8:05 - 1st"),
    _event("live-p2", "2026-10-01T23:00Z", 0, 1, state="in", period=2, status_name="STATUS_IN_PROGRESS", detail="12:00 - 2nd"),
    _event("end-1st", "2026-10-01T23:10Z", 1, 1, state="in", period=1, status_name="STATUS_END_PERIOD", detail="End of 1st"),
]


def test_buckets_ignore_which_team_scored():
    assert first_period_bucket(0, 0) == "0"
    assert first_period_bucket(1, 0) == "1"
    assert first_period_bucket(0, 1) == "1"
    assert first_period_bucket(1, 1) == "1-1"
    assert first_period_bucket(2, 0) == "2-0"
    assert first_period_bucket(0, 2) == "2-0"
    assert first_period_bucket(2, 1) == "2-1"
    assert first_period_bucket(1, 2) == "2-1"
    assert first_period_bucket(3, 0) == "3+"
    assert first_period_bucket(2, 2) == "3+"
    assert first_period_bucket(4, 1) == "3+"
    assert first_period_bucket(3, 2) == "3+"


def test_first_period_still_in_progress_does_not_count():
    assert period_one_complete("in", 1, "STATUS_IN_PROGRESS", "8:05 - 1st", True) is False
    assert period_one_complete("in", 2, "STATUS_IN_PROGRESS", "12:00 - 2nd", True) is True
    assert period_one_complete("in", 1, "STATUS_END_PERIOD", "End of 1st", True) is True
    assert period_one_complete("post", 3, "STATUS_FINAL", "Final", True) is True
    assert period_one_complete("pre", 0, "STATUS_SCHEDULED", "7:30 PM", False) is False


def test_distribution_splits_regular_season_and_past_30_days():
    def fetch(url: str) -> dict:
        assert "scoreboard" in url
        return _payload(EVENTS)

    board = PeriodFeed(fetch).get(today=TODAY)
    assert board["season_label"] == "2026-27"
    season = {row["id"]: row for row in board["season"]["buckets"]}
    recent = {row["id"]: row for row in board["recent"]["buckets"]}

    assert board["season"]["games"] == 3
    assert season["0"]["count"] == 1
    assert season["1"]["count"] == 1
    assert season["1-1"]["count"] == 1
    assert season["3+"]["count"] == 0
    assert sum(row["pct"] for row in board["season"]["buckets"]) == 100

    # Preseason 2-1 is only in the 30-day window. The August game is in neither.
    assert board["recent"]["games"] == 4
    assert recent["2-1"]["count"] == 1
    assert recent["0"]["count"] == 1
    assert recent["1"]["count"] == 1
    assert recent["1-1"]["count"] == 1
    assert board["recent"]["mix"]["preseason"] == 1
    assert board["recent"]["mix"]["regular"] == 3
    assert "preseason" in board["recent"]["detail"]


def test_month_header_does_not_replace_the_live_season():
    """September's scoreboard still advertises 2025-26. Season stats follow the live slate."""
    urls: list[str] = []

    def fetch(url: str) -> dict:
        urls.append(url)
        payload = _payload(EVENTS)
        if "dates=" in url:
            payload["leagues"][0]["season"] = {
                "year": 2026,
                "displayName": "2025-26",
                "startDate": "2025-09-20T07:00Z",
                "type": {"type": 2, "name": "Regular Season"},
            }
        return payload

    board = PeriodFeed(fetch).get(today=TODAY)
    assert board["season_year"] == 2027
    assert board["season"]["games"] == 3
    assert not any("dates=2025" in url for url in urls)


def test_distribution_endpoint_and_page_shell():
    from eeehoc.dashboard import DashboardState, make_handler

    state = DashboardState(periods=PeriodFeed(lambda url: _payload(EVENTS)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        board = json.loads(urllib.request.urlopen(base + "/api/periods", timeout=5).read())
        assert board["season_label"] == "2026-27"
        assert [row["id"] for row in board["season"]["buckets"]] == ["0", "1", "1-1", "2-0", "2-1", "3+"]
        assert [row["name"] for row in board["recent"]["buckets"]] == [
            "0 goals",
            "1 goal",
            "1-1",
            "2-0",
            "2-1",
            "3+ goals",
        ]
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert 'id="p1Board"' in html
        script = urllib.request.urlopen(base + "/static/app.js", timeout=5).read().decode()
        assert "/api/periods" in script
        css = urllib.request.urlopen(base + "/static/app.css", timeout=5).read().decode()
        assert ".p1-grid" in css
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_distribution_outage_is_502():
    from eeehoc.dashboard import DashboardState, make_handler

    def boom(url: str) -> dict:
        raise RuntimeError("espn down")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DashboardState(periods=PeriodFeed(boom))))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/periods", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 502
        assert "espn down" in json.loads(exc.read())["error"]
    else:
        raise AssertionError("expected 502")
    finally:
        httpd.shutdown()
        httpd.server_close()
