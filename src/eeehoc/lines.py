"""Which line scored, and which line was on the ice when a goal went in.

ESPN's play-by-play names the scorer and the assists but not the other
skaters on the ice, so this module reads the NHL's own feeds for each final:
the play-by-play for the roster (with positions) and the goals, and the shift
chart for every skater's shifts.

Lines are worked out per game from the shifts: forwards are grouped in threes
by how much even-strength ice time they share (the most-used forward takes the
two forwards he was on with most, and so on), and the trios are numbered L1–L4
by combined even-strength ice time. Defence pairs are built the same way,
D1–D3. A goal belongs to the line (and pair) with the most members on the ice
when it went in, for the scoring team and for the team that allowed it, so a
power-play goal from a mixed unit still lands on the line that supplied most
of it.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from eeehoc.live import FetchJson, _int, fetch_json

NHL_SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{day}"
NHL_PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
NHL_SHIFTS_URL = "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}"

# ESPN abbreviations that differ from the NHL's.
NHL_ABBR = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL"}

FORWARDS = ("C", "L", "R")
LINE_COUNT = 4
PAIR_COUNT = 3
SHIFT_TYPE = 517
PERIOD_SECONDS = 20 * 60
NHL_PLAYOFFS = 3

MISS_TTL = 10 * 60.0
SCHEDULE_TTL = 6 * 60 * 60.0

LINES_NOTE = (
    "Lines L1–L4 and pairs D1–D3 are read from each game's shift chart: forwards grouped by shared "
    "even-strength ice time and numbered by how much the trio played. A goal belongs to the line with "
    "the most members on the ice when it went in — for the team that scored and the team that allowed it."
)


def nhl_abbr(espn_abbr: str) -> str:
    abbr = str(espn_abbr or "").upper()
    return NHL_ABBR.get(abbr, abbr)


def _secs(clock: Any) -> int | None:
    text = str(clock or "")
    if ":" not in text:
        return None
    minutes, _, seconds = text.partition(":")
    try:
        return int(minutes) * 60 + int(seconds)
    except ValueError:
        return None


def _player_name(spot: dict[str, Any]) -> str:
    first = (spot.get("firstName") or {}).get("default") if isinstance(spot.get("firstName"), dict) else spot.get("firstName")
    last = (spot.get("lastName") or {}).get("default") if isinstance(spot.get("lastName"), dict) else spot.get("lastName")
    return " ".join(part for part in (str(first or "").strip(), str(last or "").strip()) if part)


def _shift_table(shifts: dict[str, Any]) -> dict[int, list[tuple[int, int, int]]]:
    """``player -> [(period, start, end)]`` from the NHL shift chart."""
    table: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for row in shifts.get("data") or []:
        if _int(row.get("typeCode"), SHIFT_TYPE) != SHIFT_TYPE:
            continue
        start, end = _secs(row.get("startTime")), _secs(row.get("endTime"))
        period = _int(row.get("period"))
        if start is None or end is None or period < 1 or end <= start:
            continue
        table[_int(row.get("playerId"))].append((period, start, end))
    return table


def _even_strength(
    table: dict[int, list[tuple[int, int, int]]],
    team_of: dict[int, int],
    skater: dict[int, bool],
    teams: tuple[int, int],
) -> dict[int, list[bool]]:
    """Per period, a second-by-second flag: both clubs have exactly five skaters out."""
    periods = sorted({p for v in table.values() for p, _s, _e in v})
    length = PERIOD_SECONDS + 1
    counts = {(tid, p): [0] * length for tid in teams for p in periods}
    for pid, shifts in table.items():
        tid = team_of.get(pid)
        if tid not in teams or not skater.get(pid):
            continue
        for period, start, end in shifts:
            arr = counts[(tid, period)]
            for t in range(start, min(end, PERIOD_SECONDS)):
                arr[t] += 1
    even = {}
    for p in periods:
        a, b = counts[(teams[0], p)], counts[(teams[1], p)]
        even[p] = [a[t] == 5 and b[t] == 5 for t in range(length)]
    return even


def _group_units(
    players: list[int],
    table: dict[int, list[tuple[int, int, int]]],
    even: dict[int, list[bool]],
    size: int,
    limit: int,
) -> list[list[int]]:
    def ev_time(pid: int) -> int:
        return sum(sum(even[p][s:e]) for p, s, e in table[pid] if p in even)

    def ev_overlap(a: int, b: int) -> int:
        total = 0
        for p1, s1, e1 in table[a]:
            flags = even.get(p1)
            if flags is None:
                continue
            for p2, s2, e2 in table[b]:
                lo, hi = max(s1, s2), min(e1, e2)
                if p1 == p2 and hi > lo:
                    total += sum(flags[lo:hi])
        return total

    left = sorted(players, key=lambda pid: (-ev_time(pid), pid))
    units: list[list[int]] = []
    while len(left) >= size:
        anchor = left.pop(0)
        partners = sorted(left, key=lambda q: (-ev_overlap(anchor, q), q))[: size - 1]
        for q in partners:
            left.remove(q)
        units.append([anchor, *partners])
    units.sort(key=lambda unit: -sum(ev_time(pid) for pid in unit))
    return units[:limit]


def _strength(code: str, *, owner_is_home: bool) -> str:
    """``situationCode`` is away goalie, away skaters, home skaters, home goalie."""
    digits = str(code or "")
    if len(digits) != 4 or not digits.isdigit():
        return ""
    away_g, away_s, home_s, home_g = (int(ch) for ch in digits)
    own_s, opp_s = (home_s, away_s) if owner_is_home else (away_s, home_s)
    opp_g = away_g if owner_is_home else home_g
    if not opp_g:
        return "EN"
    if own_s > opp_s:
        return "PP"
    if own_s < opp_s:
        return "SH"
    return "EV"


def _unit_index(units: list[list[int]], on_ice: set[int]) -> int | None:
    """Index of the unit with the most members out; ties go to the higher unit."""
    best, best_count = None, 0
    for index, unit in enumerate(units):
        count = sum(1 for pid in unit if pid in on_ice)
        if count > best_count:
            best, best_count = index, count
    return best


def game_lines(pbp: dict[str, Any], shifts: dict[str, Any], *, playoffs: bool = False) -> dict[str, Any] | None:
    """Both sides' lines for one game and the line each goal came from and went in against.

    ``None`` when the shift chart is empty (the NHL publishes it a little after
    the final horn) or the rosters are missing.
    """
    home_id = _int((pbp.get("homeTeam") or {}).get("id"))
    away_id = _int((pbp.get("awayTeam") or {}).get("id"))
    if not home_id or not away_id:
        return None
    table = _shift_table(shifts)
    if not table:
        return None
    team_of: dict[int, int] = {}
    position: dict[int, str] = {}
    names: dict[int, str] = {}
    for spot in pbp.get("rosterSpots") or []:
        pid = _int(spot.get("playerId"))
        if not pid:
            continue
        team_of[pid] = _int(spot.get("teamId"))
        position[pid] = str(spot.get("positionCode") or "").upper()
        names[pid] = _player_name(spot)
    if not team_of:
        return None
    skater = {pid: position.get(pid, "") != "G" for pid in table}
    even = _even_strength(table, team_of, skater, (away_id, home_id))

    sides = {"home": home_id, "away": away_id}
    units: dict[str, dict[str, list[list[int]]]] = {}
    for side, tid in sides.items():
        forwards = [pid for pid in table if team_of.get(pid) == tid and position.get(pid) in FORWARDS]
        defence = [pid for pid in table if team_of.get(pid) == tid and position.get(pid) == "D"]
        units[side] = {
            "line": _group_units(forwards, table, even, 3, LINE_COUNT),
            "pair": _group_units(defence, table, even, 2, PAIR_COUNT),
        }

    def blank() -> dict[str, Any]:
        return {"line": [0] * LINE_COUNT, "pair": [0] * PAIR_COUNT, "line_none": 0, "pair_none": 0, "total": 0}

    out: dict[str, Any] = {"goals": []}
    for side in sides:
        out[side] = {
            "lines": [[names.get(pid, str(pid)) for pid in unit] for unit in units[side]["line"]],
            "pairs": [[names.get(pid, str(pid)) for pid in unit] for unit in units[side]["pair"]],
            "for": blank(),
            "against": blank(),
        }

    for play in pbp.get("plays") or []:
        if str(play.get("typeDescKey") or "") != "goal":
            continue
        period = _int((play.get("periodDescriptor") or {}).get("number"))
        when = _secs(play.get("timeInPeriod"))
        if period < 1 or when is None or (not playoffs and period >= 5):
            continue
        details = play.get("details") or {}
        owner = _int(details.get("eventOwnerTeamId"))
        if owner not in (home_id, away_id):
            continue
        scorer_side = "home" if owner == home_id else "away"
        other_side = "away" if scorer_side == "home" else "home"
        on_ice = {
            pid
            for pid, rows in table.items()
            if skater.get(pid) and any(p == period and s < when <= e for p, s, e in rows)
        }
        goal: dict[str, Any] = {
            "period": period,
            "time": str(play.get("timeInPeriod") or ""),
            "side": scorer_side,
            "strength": _strength(str(play.get("situationCode") or ""), owner_is_home=scorer_side == "home"),
            "scorer": names.get(_int(details.get("scoringPlayerId")), ""),
        }
        for side, key in ((scorer_side, "for"), (other_side, "against")):
            mine = {pid for pid in on_ice if team_of.get(pid) == sides[side]}
            bucket = out[side][key]
            bucket["total"] += 1
            for kind in ("line", "pair"):
                index = _unit_index(units[side][kind], mine)
                if index is None:
                    bucket[f"{kind}_none"] += 1
                else:
                    bucket[kind][index] += 1
                goal[f"{key}_{kind}"] = None if index is None else index + 1
        out["goals"].append(goal)
    return out


class LineSource:
    """NHL schedule, play-by-play and shift charts, matched to ESPN finals and cached."""

    def __init__(self, fetch: FetchJson = fetch_json) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._schedule: dict[str, tuple[float, dict[tuple[str, str], tuple[int, int]]]] = {}
        self._games: dict[int, dict[str, Any]] = {}
        self._misses: dict[str, float] = {}
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _day_games(self, day: str, now: float) -> dict[tuple[str, str], tuple[int, int]]:
        """``(away, home) -> (nhl game id, game type)`` for one Eastern date."""
        with self._lock:
            cached = self._schedule.get(day)
            if cached is not None and now - cached[0] < SCHEDULE_TTL:
                return cached[1]
        payload = self._fetch(NHL_SCHEDULE_URL.format(day=day))
        found: dict[str, dict[tuple[str, str], tuple[int, int]]] = defaultdict(dict)
        for week_day in (payload.get("gameWeek") or []) if isinstance(payload, dict) else []:
            date_key = str(week_day.get("date") or "")
            for game in week_day.get("games") or []:
                away = str((game.get("awayTeam") or {}).get("abbrev") or "").upper()
                home = str((game.get("homeTeam") or {}).get("abbrev") or "").upper()
                gid = _int(game.get("id"))
                if date_key and away and home and gid:
                    found[date_key][(away, home)] = (gid, _int(game.get("gameType")))
        found.setdefault(day, {})
        with self._lock:
            for date_key, games in found.items():
                self._schedule[date_key] = (now, games)
        return found[day]

    def for_record(self, record: dict[str, Any], now: float) -> dict[str, Any] | None:
        """Lines for a power record (``day`` plus both ``abbr``); ``None`` when the NHL has nothing yet."""
        day = str(record.get("day") or "")
        away = nhl_abbr((record.get("away") or {}).get("abbr", ""))
        home = nhl_abbr((record.get("home") or {}).get("abbr", ""))
        if not day or not away or not home:
            return None
        miss_key = f"{day}:{away}@{home}"
        with self._lock:
            if now - self._misses.get(miss_key, 0.0) < MISS_TTL:
                return None
        try:
            gid, game_type = self._day_games(day, now).get((away, home), (0, 0))
            if not gid:
                raise LookupError(f"no NHL game {away} @ {home} on {day}")
            with self._lock:
                hit = self._games.get(gid)
            if hit is not None:
                return hit
            pbp = self._fetch(NHL_PBP_URL.format(game_id=gid))
            shifts = self._fetch(NHL_SHIFTS_URL.format(game_id=gid))
            lines = game_lines(pbp, shifts, playoffs=game_type == NHL_PLAYOFFS) if isinstance(pbp, dict) and isinstance(shifts, dict) else None
        except Exception as exc:  # noqa: BLE001 - lines are a bonus on top of the power table
            self._last_error = str(exc)
            lines = None
        with self._lock:
            if lines is None:
                self._misses[miss_key] = now
                return None
            lines["nhl_id"] = gid
            self._games[gid] = lines
        return lines
