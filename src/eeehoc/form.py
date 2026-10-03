"""Each team's last few finished games with the score and shots by period."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from eeehoc.live import (
    PLAYOFF_TYPE,
    REGULATION_PERIODS,
    SUMMARY_URL,
    FetchJson,
    _int,
    _team_side,
    fetch_json,
    period_shot_lines,
)

SCHEDULE_URL = "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/teams/{team_id}/schedule"
RECENT_GAMES = 5
SCHEDULE_TTL = 30 * 60.0
SUMMARY_TTL = 24 * 60 * 60.0
PRESEASON_TYPE = 1
REGULAR_TYPE = 2


def _period_label(index: int, *, playoffs: bool) -> str:
    if index < REGULATION_PERIODS:
        return str(index + 1)
    extra = index - REGULATION_PERIODS + 1
    if not playoffs:
        return "OT" if extra == 1 else "SO"
    return "OT" if extra == 1 else f"{extra}OT"


def _result(won: bool, detail: str, *, scored: int, allowed: int) -> str:
    text = (detail or "").upper()
    if won:
        return "W"
    if scored == allowed:
        return "T"
    if "SO" in text:
        return "SOL"
    if "OT" in text:
        return "OTL"
    return "L"


def summarize_game(summary: dict[str, Any], team_id: str) -> dict[str, Any] | None:
    """One finished game from this team's point of view, period by period."""
    header = summary.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    me = next((c for c in competitors if str((c.get("team") or {}).get("id")) == str(team_id)), None)
    them = next((c for c in competitors if str((c.get("team") or {}).get("id")) != str(team_id)), None)
    if not me or not them:
        return None
    status = comp.get("status") or {}
    stype = status.get("type") or {}
    if str(stype.get("state") or "") != "post":
        return None
    season = header.get("season") or {}
    season_type = _int(season.get("type"))
    playoffs = season_type == PLAYOFF_TYPE
    home_id = str(next((c.get("team") or {}).get("id") for c in competitors if c.get("homeAway") == "home"))
    away_id = str(next((c.get("team") or {}).get("id") for c in competitors if c.get("homeAway") == "away"))
    my_side = _team_side(team_id, home_id, away_id) or "home"
    their_side = "away" if my_side == "home" else "home"

    my_lines = [_int(ls.get("value", ls.get("displayValue"))) for ls in me.get("linescores") or []]
    their_lines = [_int(ls.get("value", ls.get("displayValue"))) for ls in them.get("linescores") or []]
    n_periods = max(len(my_lines), len(their_lines), REGULATION_PERIODS)
    plays = summary.get("plays") or []
    # ESPN drops the play-by-play for some archived games; report shots as
    # unknown rather than as zero in that case.
    shot_rows = (
        period_shot_lines(
            plays,
            home_id,
            away_id,
            periods=min(n_periods, REGULATION_PERIODS + 1) if not playoffs else n_periods,
            playoffs=playoffs,
        )
        if plays
        else []
    )
    shots_by_period = {row["period"]: row for row in shot_rows}

    periods = []
    for index in range(n_periods):
        label = _period_label(index, playoffs=playoffs)
        shots = shots_by_period.get(index + 1) or {}
        periods.append(
            {
                "label": label,
                "gf": my_lines[index] if index < len(my_lines) else 0,
                "ga": their_lines[index] if index < len(their_lines) else 0,
                "sog_f": shots.get(f"{my_side}_sog") if shots else None,
                "sog_a": shots.get(f"{their_side}_sog") if shots else None,
                "shots_f": shots.get(f"{my_side}_shots") if shots else None,
                "shots_a": shots.get(f"{their_side}_shots") if shots else None,
                "shootout": label == "SO",
            }
        )

    scored = _int(me.get("score"))
    allowed = _int(them.get("score"))
    won = bool(me.get("winner")) or (not them.get("winner") and scored > allowed)
    detail = str(stype.get("shortDetail") or stype.get("detail") or "Final")
    opponent = them.get("team") or {}
    return {
        "id": str(header.get("id") or comp.get("id") or ""),
        "date": str(comp.get("date") or ""),
        "venue": my_side,
        "opponent": str(opponent.get("abbreviation") or ""),
        "opponent_name": str(opponent.get("shortDisplayName") or opponent.get("name") or ""),
        "opponent_color": str(opponent.get("color") or ""),
        "result": _result(won, detail, scored=scored, allowed=allowed),
        "gf": scored,
        "ga": allowed,
        "detail": detail,
        "season_type": season_type,
        "preseason": season_type == PRESEASON_TYPE,
        "periods": periods,
    }


def _finished_events(schedule: dict[str, Any]) -> list[tuple[str, str]]:
    found = []
    for event in schedule.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        stype = (comp.get("status") or {}).get("type") or {}
        if str(stype.get("state") or "") != "post":
            continue
        eid = str(event.get("id") or comp.get("id") or "")
        if eid:
            found.append((str(event.get("date") or comp.get("date") or ""), eid))
    return found


class FormFeed:
    """Cached schedules and final summaries rolled into a per-team last-five list."""

    def __init__(self, fetch: FetchJson = fetch_json, *, workers: int = 6) -> None:
        self._fetch = fetch
        self._workers = max(1, workers)
        self._lock = threading.Lock()
        self._schedules: dict[str, tuple[float, dict[str, Any]]] = {}
        self._summaries: dict[str, tuple[float, dict[str, Any]]] = {}

    def _cached(self, store: dict[str, tuple[float, dict[str, Any]]], key: str, ttl: float, url: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            hit = store.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
        payload = self._fetch(url)
        if not isinstance(payload, dict):
            payload = {}
        with self._lock:
            store[key] = (now, payload)
        return payload

    def _schedule(self, team_id: str, season_type: int) -> dict[str, Any]:
        url = f"{SCHEDULE_URL.format(team_id=team_id)}?seasontype={season_type}"
        key = f"{team_id}:{season_type}"
        try:
            return self._cached(self._schedules, key, SCHEDULE_TTL, url)
        except Exception:  # noqa: BLE001 - a season type that does not exist yet is not an error
            with self._lock:
                self._schedules[key] = (time.time(), {})
            return {}

    def _summary(self, event_id: str) -> dict[str, Any] | None:
        try:
            return self._cached(self._summaries, event_id, SUMMARY_TTL, SUMMARY_URL.format(event_id=event_id))
        except Exception:  # noqa: BLE001
            return None

    def recent(self, team_id: str, *, exclude: str = "", season_type: int = REGULAR_TYPE, limit: int = RECENT_GAMES) -> list[dict[str, Any]]:
        """Last ``limit`` finals, oldest first. Preseason fills in when the season is young."""
        team_id = str(team_id)
        order = [season_type] + [t for t in (PLAYOFF_TYPE, REGULAR_TYPE, PRESEASON_TYPE) if t != season_type]
        events: list[tuple[str, str]] = []
        for stype in order:
            events.extend(_finished_events(self._schedule(team_id, stype)))
            unique = {eid: date for date, eid in events if eid != exclude}
            if len(unique) >= limit:
                break
        unique = {eid: date for date, eid in events if eid != exclude}
        picked = sorted(unique.items(), key=lambda item: item[1])[-limit:]
        if not picked:
            return []
        with ThreadPoolExecutor(max_workers=min(self._workers, len(picked))) as pool:
            summaries = list(pool.map(lambda item: self._summary(item[0]), picked))
        games = []
        for (eid, date), summary in zip(picked, summaries):
            if not summary:
                continue
            game = summarize_game(summary, team_id)
            if game:
                if not game["date"]:
                    game["date"] = date
                games.append(game)
        games.sort(key=lambda g: g["date"])
        return games

    def for_game(self, home_id: str, away_id: str, *, exclude: str = "", season_type: int = REGULAR_TYPE) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            home_f = pool.submit(self.recent, home_id, exclude=exclude, season_type=season_type)
            away_f = pool.submit(self.recent, away_id, exclude=exclude, season_type=season_type)
            return {"home": home_f.result(), "away": away_f.result(), "limit": RECENT_GAMES}
