"""Shots on goal per goal for every team: season to date and the last five games.

ESPN's month scoreboard carries each side's ``goals`` (regulation + overtime, no
shootout) and ``saves``, so a team's shots on goal in a game is the opponent's
saves plus its own goals — this matches the box score exactly. One cached month
payload therefore covers the whole league without touching a single summary.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime
from typing import Any

from eeehoc.live import PLAYOFF_TYPE, SCOREBOARD_URL, FetchJson, _int, fetch_json
from eeehoc.periods import ET, PRESEASON_TYPE, REGULAR_TYPE, _game_day, _months, _season_meta

RECENT_GAMES = 5
RECENT_KEEP = RECENT_GAMES + 1  # the client drops the chiclet's own game, then takes five
CURRENT_MONTH_TTL = 90.0
PAST_MONTH_TTL = 6 * 60 * 60.0
RESULT_TTL = 60.0


def _stat(competitor: dict[str, Any], name: str) -> int | None:
    for stat in competitor.get("statistics") or []:
        if stat.get("name") == name:
            value = stat.get("displayValue", stat.get("value"))
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
    return None


def _team(comp: dict[str, Any]) -> dict[str, str]:
    team = comp.get("team") or {}
    return {"id": str(team.get("id") or ""), "abbr": str(team.get("abbreviation") or "")}


def team_games(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per team per final: goals and shots on goal for and against."""
    rows: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        status = comp.get("status") or event.get("status") or {}
        stype = status.get("type") or {}
        if str(stype.get("name") or "") != "STATUS_FINAL":
            continue
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        day = _game_day(str(event.get("date") or comp.get("date") or ""))
        if day is None:
            continue
        season = event.get("season") or {}
        goals = {"home": _stat(home, "goals"), "away": _stat(away, "goals")}
        saves = {"home": _stat(home, "saves"), "away": _stat(away, "saves")}
        if goals["home"] is None:
            goals["home"] = _int(home.get("score"))
        if goals["away"] is None:
            goals["away"] = _int(away.get("score"))
        sog = {
            "home": (saves["away"] + goals["home"]) if saves["away"] is not None else None,
            "away": (saves["home"] + goals["away"]) if saves["home"] is not None else None,
        }
        for side, other, comp_side in (("home", "away", home), ("away", "home", away)):
            me, them = _team(comp_side), _team(home if other == "home" else away)
            if not me["id"]:
                continue
            rows.append(
                {
                    "id": str(event.get("id") or comp.get("id") or ""),
                    "date": str(event.get("date") or ""),
                    "day": day.isoformat(),
                    "season_year": _int(season.get("year")),
                    "season_type": _int(season.get("type")),
                    "team_id": me["id"],
                    "abbr": me["abbr"],
                    "opponent": them["abbr"],
                    "venue": side,
                    "gf": goals[side],
                    "ga": goals[other],
                    "sog_f": sog[side],
                    "sog_a": sog[other],
                }
            )
    return rows


def _ratio(shots: int, goals: int) -> float | None:
    if goals <= 0:
        return None
    return round(shots / goals, 1)


def shots_per_goal(games: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over ``games`` and the shots-on-goal-per-goal ratios both ways."""
    with_shots = [g for g in games if g["sog_f"] is not None and g["sog_a"] is not None]
    gf = sum(g["gf"] for g in with_shots)
    ga = sum(g["ga"] for g in with_shots)
    sog_f = sum(g["sog_f"] for g in with_shots)
    sog_a = sum(g["sog_a"] for g in with_shots)
    return {
        "games": len(with_shots),
        "gf": gf,
        "ga": ga,
        "sog_f": sog_f,
        "sog_a": sog_a,
        "spg_f": _ratio(sog_f, gf),  # shots it takes this team to score
        "spg_a": _ratio(sog_a, ga),  # shots opponents need to score on it
        "shooting_pct": round(100 * gf / sog_f, 1) if sog_f else None,
        "save_pct": round(100 * (1 - ga / sog_a), 1) if sog_a else None,
    }


def build_shots(rows: list[dict[str, Any]], *, today: date, season_year: int, season_label: str) -> dict[str, Any]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (row["id"], row["team_id"])
        if key in seen or row["day"] > today.isoformat():
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: (r["date"], r["id"]))

    this_season = [r for r in unique if r["season_year"] == season_year]
    regular = [r for r in this_season if r["season_type"] in (REGULAR_TYPE, PLAYOFF_TYPE)]
    season_rows, season_kind = (regular, "regular season") if regular else (
        [r for r in this_season if r["season_type"] == PRESEASON_TYPE],
        "preseason",
    )

    by_team: dict[str, list[dict[str, Any]]] = {}
    for row in unique:
        by_team.setdefault(row["team_id"], []).append(row)

    teams: dict[str, dict[str, Any]] = {}
    for team_id, games in by_team.items():
        mine = [g for g in season_rows if g["team_id"] == team_id]
        recent = games[-RECENT_KEEP:]
        teams[team_id] = {
            "id": team_id,
            "abbr": games[-1]["abbr"],
            "season": shots_per_goal(mine),
            "recent": [
                {
                    "id": g["id"],
                    "day": g["day"],
                    "opponent": g["opponent"],
                    "venue": g["venue"],
                    "preseason": g["season_type"] == PRESEASON_TYPE,
                    "gf": g["gf"],
                    "ga": g["ga"],
                    "sog_f": g["sog_f"],
                    "sog_a": g["sog_a"],
                }
                for g in recent
            ],
        }

    league = shots_per_goal(season_rows)
    return {
        "season_label": season_label,
        "season_year": season_year,
        "season_kind": season_kind,
        "as_of": today.isoformat(),
        "recent_games": RECENT_GAMES,
        "league": {"games": league["games"] // 2, "spg": league["spg_f"], "shooting_pct": league["shooting_pct"]},
        "teams": teams,
    }


class ShotsFeed:
    """Cached month scoreboards rolled into shots per goal for every team."""

    def __init__(self, fetch: FetchJson = fetch_json) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._months: dict[str, tuple[float, dict[str, Any]]] = {}
        self._result: tuple[float, date, dict[str, Any]] | None = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _load_month(self, ym: str, now: float, today: date) -> dict[str, Any]:
        current = ym == f"{today.year}{today.month:02d}"
        ttl = CURRENT_MONTH_TTL if current else PAST_MONTH_TTL
        with self._lock:
            cached = self._months.get(ym)
            if cached is not None and now - cached[0] < ttl:
                return cached[1]
        try:
            payload = self._fetch(f"{SCOREBOARD_URL}?dates={ym}&limit=1000")
        except Exception:
            with self._lock:
                cached = self._months.get(ym)
            if cached is not None:
                return cached[1]
            raise
        if not isinstance(payload, dict):
            payload = {}
        with self._lock:
            self._months[ym] = (now, payload)
        return payload

    def get(self, *, today: date | None = None) -> dict[str, Any]:
        today = today or datetime.now(ET).date()
        now = time.time()
        with self._lock:
            cached = self._result
            if cached is not None and cached[1] == today and now - cached[0] < RESULT_TTL:
                return cached[2]

        try:
            year, label, _start = _season_meta(self._fetch(SCOREBOARD_URL))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            year, label = 0, ""
        if not year:
            year = today.year + (1 if today.month >= 9 else 0)
            label = f"{year - 1}-{str(year)[-2:]}"

        rows: list[dict[str, Any]] = []
        try:
            for ym in _months(date(year - 1, 9, 1), today):
                rows.extend(team_games(self._load_month(ym, now, today)))
        except Exception as exc:  # noqa: BLE001 - serve the last good result
            self._last_error = str(exc)
            with self._lock:
                if self._result is not None:
                    stale = dict(self._result[2])
                    stale["stale"] = True
                    stale["error"] = self._last_error
                    return stale
            raise

        result = build_shots(rows, today=today, season_year=year, season_label=label)
        with self._lock:
            self._result = (now, today, result)
        return result
