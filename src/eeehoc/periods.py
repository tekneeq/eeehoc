"""First-period score distribution: regular season and the past 30 days.

ESPN's month scoreboard (``dates=YYYYMM``) carries every game's period lines.
A game counts once the 1st period is over. Scorelines are unordered, so 2-1
and 1-2 are the same bucket. Anything that is not 0-0, 1-0, 1-1, 2-0, or 2-1
is "3+".
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from eeehoc.live import PLAYOFF_TYPE, SCOREBOARD_URL, FetchJson, _int, fetch_json

ET = ZoneInfo("America/New_York")

PRESEASON_TYPE = 1
REGULAR_TYPE = 2
RECENT_DAYS = 30

# id, short label, spoken name
BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("0", "0", "0 goals"),
    ("1", "1", "1 goal"),
    ("1-1", "1-1", "1-1"),
    ("2-0", "2-0", "2-0"),
    ("2-1", "2-1", "2-1"),
    ("3+", "3+", "3+ goals"),
)

CURRENT_MONTH_TTL = 90.0
PAST_MONTH_TTL = 6 * 60 * 60.0
RESULT_TTL = 60.0


def first_period_bucket(away: int, home: int) -> str:
    """Map a finished 1st-period score to a distribution bucket."""
    lo, hi = sorted((_int(away), _int(home)))
    pair = (lo, hi)
    if pair == (0, 0):
        return "0"
    if pair == (0, 1):
        return "1"
    if pair == (1, 1):
        return "1-1"
    if pair == (0, 2):
        return "2-0"
    if pair == (1, 2):
        return "2-1"
    return "3+"


def _game_day(iso: str) -> date | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET).date()


def _p1_goals(competitor: dict[str, Any]) -> int | None:
    lines = competitor.get("linescores") or []
    for line in lines:
        if _int(line.get("period")) == 1:
            return _int(line.get("value", line.get("displayValue")))
    if not lines:
        return None
    return _int(lines[0].get("value", lines[0].get("displayValue")))


def period_one_complete(state: str, period: int, status_name: str, detail: str, scored: bool) -> bool:
    """True when the 1st-period score will not change."""
    if not scored or state == "pre":
        return False
    if state == "post":
        return True
    if state != "in":
        return False
    if period >= 2:
        return True
    text = f"{status_name} {detail}".lower()
    if period >= 1 and ("end of 1" in text or "end 1st" in text or status_name == "STATUS_END_PERIOD"):
        return True
    return False


def _months(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year}{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def _season_meta(payload: dict[str, Any]) -> tuple[int, str, date | None]:
    leagues = payload.get("leagues") or []
    season = (leagues[0].get("season") if leagues else None) or payload.get("season") or {}
    if not isinstance(season, dict):
        season = {}
    year = _int(season.get("year"))
    display = str(season.get("displayName") or "")
    if not display and year:
        display = f"{year - 1}-{str(year)[-2:]}"
    start = _game_day(str(season.get("startDate") or ""))
    return year, display, start


def _iter_games(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        status = comp.get("status") or event.get("status") or {}
        stype = status.get("type") or {}
        state = str(stype.get("state") or "pre")
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_p1 = _p1_goals(home)
        away_p1 = _p1_goals(away)
        if not period_one_complete(
            state,
            _int(status.get("period")),
            str(stype.get("name") or ""),
            str(stype.get("shortDetail") or stype.get("detail") or ""),
            home_p1 is not None and away_p1 is not None,
        ):
            continue
        day = _game_day(str(event.get("date") or comp.get("date") or ""))
        if day is None:
            continue
        season = event.get("season") or {}
        found.append(
            {
                "id": str(event.get("id") or comp.get("id") or ""),
                "day": day,
                "season_year": _int(season.get("year")),
                "season_type": _int(season.get("type")),
                "bucket": first_period_bucket(away_p1 or 0, home_p1 or 0),
            }
        )
    return found


def _shares(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(game["bucket"] for game in games)
    total = sum(counts[key] for key, _, _ in BUCKETS)
    raw = [(100.0 * counts[key] / total) if total else 0.0 for key, _, _ in BUCKETS]
    floors = [int(value) for value in raw]
    leftover = 100 - sum(floors) if total else 0
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floors[i], -i), reverse=True)
    for index in order[:leftover]:
        floors[index] += 1
    rows = []
    for (key, label, name), pct in zip(BUCKETS, floors):
        rows.append(
            {
                "id": key,
                "label": label,
                "name": name,
                "count": counts[key],
                "pct": pct if total else 0,
            }
        )
    return rows


def _mix(games: list[dict[str, Any]]) -> dict[str, int]:
    types = Counter(game["season_type"] for game in games)
    return {
        "preseason": types[PRESEASON_TYPE],
        "regular": types[REGULAR_TYPE],
        "playoffs": types[PLAYOFF_TYPE],
    }


def _window(games: list[dict[str, Any]], *, label: str, detail: str) -> dict[str, Any]:
    days = [game["day"] for game in games]
    return {
        "label": label,
        "detail": detail,
        "games": len(games),
        "from": min(days).isoformat() if days else "",
        "to": max(days).isoformat() if days else "",
        "mix": _mix(games),
        "buckets": _shares(games),
    }


def build_distribution(
    games: list[dict[str, Any]],
    *,
    today: date,
    season_year: int,
    season_label: str,
    season_start: date | None,
) -> dict[str, Any]:
    """Split completed 1st periods into the regular season and the last 30 days."""
    recent_start = today - timedelta(days=RECENT_DAYS - 1)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for game in games:
        gid = game["id"]
        if gid and gid in seen:
            continue
        if gid:
            seen.add(gid)
        if game["day"] > today:
            continue
        unique.append(game)

    season_games = [
        game
        for game in unique
        if game["season_type"] == REGULAR_TYPE
        and game["season_year"] == season_year
        and (season_start is None or game["day"] >= season_start)
    ]
    recent_games = [
        game
        for game in unique
        if game["day"] >= recent_start and game["season_type"] in (PRESEASON_TYPE, REGULAR_TYPE, PLAYOFF_TYPE)
    ]
    label = season_label or (f"{season_year - 1}-{str(season_year)[-2:]}" if season_year else "Season")
    return {
        "season_label": label,
        "season_year": season_year,
        "as_of": today.isoformat(),
        "days": RECENT_DAYS,
        "season": _window(season_games, label="Season", detail=f"{label} regular season"),
        "recent": _window(recent_games, label="Past 30 days", detail=_recent_detail(recent_games)),
    }


def _recent_detail(games: list[dict[str, Any]]) -> str:
    mix = _mix(games)
    bits = []
    if mix["preseason"]:
        bits.append(f"{mix['preseason']} preseason")
    if mix["regular"]:
        bits.append(f"{mix['regular']} regular season")
    if mix["playoffs"]:
        bits.append(f"{mix['playoffs']} playoff")
    if not bits:
        return "No completed 1st periods"
    return " · ".join(bits)


class PeriodFeed:
    """Cached month scoreboards rolled into the 1st-period distribution."""

    def __init__(self, fetch: FetchJson = fetch_json, *, workers: int = 6) -> None:
        self._fetch = fetch
        self._workers = max(1, workers)
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

        # A month query's league header can still name the previous season
        # (September 2026 says 2025-26) while its events are the new one.
        # The undated scoreboard is the season that is actually in progress.
        try:
            year, label, start = _season_meta(self._fetch(SCOREBOARD_URL))
        except Exception as exc:  # noqa: BLE001 - fall through to a month header
            self._last_error = str(exc)
            year, label, start = 0, "", None

        current_ym = f"{today.year}{today.month:02d}"
        try:
            current = self._load_month(current_ym, now, today)
        except Exception as exc:  # noqa: BLE001 - serve the last good distribution
            self._last_error = str(exc)
            with self._lock:
                if self._result is not None:
                    stale = dict(self._result[2])
                    stale["stale"] = True
                    stale["error"] = self._last_error
                    return stale
            raise

        if not year:
            year, label, start = _season_meta(current)
        recent_start = today - timedelta(days=RECENT_DAYS - 1)
        range_start = recent_start
        if start is not None:
            range_start = min(start, recent_start)
        elif year:
            range_start = min(date(year - 1, 9, 1), recent_start)
        months = _months(range_start, today)
        payloads = {current_ym: current}
        others = [ym for ym in months if ym != current_ym]
        if others:
            with ThreadPoolExecutor(max_workers=min(self._workers, len(others))) as pool:
                loaded = list(pool.map(lambda ym: self._load_month(ym, now, today), others))
            for ym, payload in zip(others, loaded):
                payloads[ym] = payload

        games: list[dict[str, Any]] = []
        for payload in payloads.values():
            games.extend(_iter_games(payload))
        result = build_distribution(
            games,
            today=today,
            season_year=year,
            season_label=label,
            season_start=start,
        )
        result["fetched_at"] = int(now)
        self._last_error = None
        with self._lock:
            self._result = (now, today, result)
        return result
