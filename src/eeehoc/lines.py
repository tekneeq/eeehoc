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

import re
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Callable

from eeehoc.live import USER_AGENT, FetchJson, _int, fetch_json
from eeehoc.periods import ET

NHL_SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{day}"
NHL_PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
NHL_SHIFTS_URL = "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}"
# The stats shift chart sits on the previous period for a long stretch. These reports
# are the ones the NHL game sheet updates while the period is being played.
NHL_TOI_URL = "https://www.nhl.com/scores/htmlreports/{season}/T{side}{number}.HTM"

# ESPN abbreviations that differ from the NHL's.
NHL_ABBR = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL"}

FORWARDS = ("C", "L", "R")
LINE_COUNT = 4
PAIR_COUNT = 3
SHIFT_TYPE = 517
PERIOD_SECONDS = 20 * 60
NHL_PLAYOFFS = 3

# A fresh final's shift chart lands within minutes; an old game without one is rarely backfilled.
MISS_TTL = 10 * 60.0
OLD_MISS_TTL = 6 * 60 * 60.0
OLD_AFTER_DAYS = 2
SCHEDULE_TTL = 6 * 60 * 60.0
# The shift reports trail the clock by a little, so a live game is re-read about as often as the scoreboard.
LIVE_CHART_TTL = 20.0
FetchText = Callable[[str], str]

_PLAYER_HEADING = re.compile(r'class="playerHeading[^"]*"[^>]*>\s*(\d+)\s+([^,<]+?)\s*,\s*([^<]+?)\s*<', re.IGNORECASE)
_SHIFT_ROW = re.compile(
    r">\s*(\d+)\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>\s*"
    r"<td[^>]*>\s*(\d{1,2}:\d{2})\s*/\s*\d{1,2}:\d{2}\s*</td>\s*"
    r"<td[^>]*>\s*(\d{1,2}:\d{2})\s*/\s*\d{1,2}:\d{2}\s*</td>",
    re.IGNORECASE,
)

LINES_NOTE = (
    "Lines L1–L4 and pairs D1–D3 are read from each game's shift chart: forwards grouped by shared "
    "even-strength ice time and numbered by how much the trio played. A goal belongs to the line with "
    "the most members on the ice when it went in — for the team that scored and the team that allowed it."
)


def _miss_ttl(day: str, now: float) -> float:
    try:
        played = date.fromisoformat(day)
    except ValueError:
        return MISS_TTL
    age = (datetime.fromtimestamp(now, ET).date() - played).days
    return OLD_MISS_TTL if age > OLD_AFTER_DAYS else MISS_TTL


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


def _norm_token(value: Any) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _spot_last(spot: dict[str, Any]) -> str:
    last = spot.get("lastName")
    if isinstance(last, dict):
        last = last.get("default")
    return _norm_token(last)


def _chart_reach(rows: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    """How far each player's shifts have been published, as ``(period, end seconds)``."""
    reach: dict[int, tuple[int, int]] = {}
    for row in rows:
        if _int(row.get("typeCode"), SHIFT_TYPE) != SHIFT_TYPE:
            continue
        pid = _int(row.get("playerId"))
        end = _secs(row.get("endTime"))
        period = _int(row.get("period"))
        if not pid or end is None or period < 1:
            continue
        reach[pid] = max(reach.get(pid, (0, -1)), (period, end))
    return reach


def toi_report_urls(game_id: int) -> tuple[str, str]:
    """Visitor and home time-on-ice reports for an NHL game id."""
    text = f"{int(game_id):010d}"
    start = int(text[:4])
    season = f"{start}{start + 1}"
    number = text[-6:]
    return (
        NHL_TOI_URL.format(season=season, side="V", number=number),
        NHL_TOI_URL.format(season=season, side="H", number=number),
    )


def shifts_from_toi_report(html: str, spots: list[dict[str, Any]], team_id: int) -> list[dict[str, Any]]:
    """Shift rows from one club's HTML time-on-ice report, keyed by NHL player id.

    The report names skaters as ``sweater LAST, FIRST``. Sweater plus last name
    matches the play-by-play roster. Unmatched skaters are left out.
    """
    if not html or not team_id:
        return []
    index: dict[tuple[int, str], int] = {}
    for spot in spots:
        if _int(spot.get("teamId")) != team_id:
            continue
        sweater = _int(spot.get("sweaterNumber"))
        last = _spot_last(spot)
        pid = _int(spot.get("playerId"))
        if sweater and last and pid:
            index[(sweater, last)] = pid
    rows: list[dict[str, Any]] = []
    for heading in _PLAYER_HEADING.finditer(html):
        pid = index.get((_int(heading.group(1)), _norm_token(heading.group(2))))
        if not pid:
            continue
        block_end = html.find('class="playerHeading', heading.end())
        block = html[heading.end() : block_end if block_end != -1 else None]
        for number, period, start, end in _SHIFT_ROW.findall(block):
            del number
            rows.append(
                {
                    "typeCode": SHIFT_TYPE,
                    "playerId": pid,
                    "period": int(period),
                    "startTime": start,
                    "endTime": end,
                }
            )
    return rows


def merge_shift_charts(shifts: dict[str, Any] | None, extra: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep each player's shifts from whichever chart reaches further into the game.

    A player published in the HTML report is replaced wholesale, because that
    report lists every shift from the opening faceoff through the latest one.
    """
    base = [row for row in ((shifts or {}).get("data") or []) if isinstance(row, dict)]
    if not extra:
        return shifts if isinstance(shifts, dict) else {"data": base}
    fresh = _chart_reach(extra)
    stale = _chart_reach(base)
    replace = {pid for pid, reach in fresh.items() if reach > stale.get(pid, (0, -1))}
    if not replace:
        return shifts if isinstance(shifts, dict) else {"data": base}
    kept = [row for row in base if _int(row.get("playerId")) not in replace]
    added = [row for row in extra if _int(row.get("playerId")) in replace]
    return {"data": kept + added}


def fetch_text(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("latin-1", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Live feed HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Live feed unreachable: {exc.reason}") from exc


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


def together_by_period(table: dict[int, list[tuple[int, int, int]]], players: list[int]) -> dict[int, int]:
    """Seconds every player in ``players`` is on the ice at once, by period."""
    if not players:
        return {}
    need = len(set(players))
    totals: dict[int, int] = {}
    periods = {period for pid in players for period, _start, _end in table.get(pid, [])}
    for period in periods:
        events: list[tuple[int, int, int]] = []
        for pid in players:
            for shift_period, start, end in table.get(pid, []):
                if shift_period == period:
                    events.append((start, 1, pid))
                    events.append((end, -1, pid))
        # A shift ending at t and another starting at t do not overlap.
        events.sort(key=lambda item: (item[0], 0 if item[1] < 0 else 1))
        open_count: dict[int, int] = {}
        previous: int | None = None
        together = False
        total = 0
        for moment, delta, pid in events:
            if previous is not None and together and moment > previous:
                total += moment - previous
            open_count[pid] = open_count.get(pid, 0) + delta
            if open_count[pid] <= 0:
                open_count.pop(pid, None)
            together = len(open_count) >= need
            previous = moment
        if total:
            totals[period] = total
    return totals


def together_seconds(table: dict[int, list[tuple[int, int, int]]], players: list[int]) -> int:
    """Seconds every player in ``players`` is on the ice at once, any strength."""
    return sum(together_by_period(table, players).values())


def _period_label(period: int, *, playoffs: bool) -> str:
    if period == 1:
        return "1st"
    if period == 2:
        return "2nd"
    if period == 3:
        return "3rd"
    if period == 4:
        return "OT"
    if not playoffs:
        return "SO"
    return f"{period - 3}OT"


def _name_tokens(name: str) -> list[str]:
    return [part.strip(".") for part in str(name or "").replace(".", " ").split() if part.strip(".")]


def _last_key(name: str) -> str:
    tokens = _name_tokens(name)
    return tokens[-1].casefold().replace("'", "").replace("’", "") if tokens else ""


def _last_display(name: str) -> str:
    tokens = _name_tokens(name)
    return tokens[-1] if tokens else ""


def _initial(name: str) -> str:
    tokens = _name_tokens(name)
    return tokens[0][:1].casefold() if len(tokens) >= 2 else ""


def _match_ids(names: dict[int, str], skaters: list[dict[str, str]]) -> set[int]:
    """NHL player ids for ESPN short names, by last name and first initial."""
    by_last: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for pid, name in names.items():
        by_last[_last_key(name)].append((_initial(name), pid))
    found: set[int] = set()
    for skater in skaters:
        candidates = by_last.get(_last_key(str(skater.get("name") or "")), [])
        initial = _initial(str(skater.get("name") or ""))
        exact = [pid for candidate, pid in candidates if initial and candidate == initial]
        if len(exact) == 1:
            found.add(exact[0])
        elif len(candidates) == 1:
            found.add(candidates[0][1])
    return found


def _through_label(period: int, elapsed: int, *, playoffs: bool) -> str:
    """How far the shift chart has been published, as elapsed time on the period clock."""
    name = {1: "1st", 2: "2nd", 3: "3rd", 4: "OT"}.get(period, f"P{period}")
    length = PERIOD_SECONDS if period <= 3 or playoffs else 5 * 60
    shown = min(max(0, elapsed), length)
    return f"{name} {shown // 60}:{shown % 60:02d}"


def live_lines(
    pbp: dict[str, Any] | None,
    shifts: dict[str, Any] | None,
    on_ice: dict[str, list[dict[str, str]]] | None,
    *,
    playoffs: bool = False,
) -> dict[str, Any] | None:
    """Lines and pairs on the ice now, and how long each has been out together.

    Who is out comes from ``on_ice`` (ESPN's live list, short names and positions).
    The groups and their ice time come from the NHL shift chart, which trails the
    clock by a few minutes while a game is going. ``None`` when there is nothing
    to show.
    """
    pbp = pbp or {}
    on_ice = on_ice or {}
    if not any(on_ice.get(side) for side in ("home", "away")) and not (shifts or {}).get("data"):
        return None
    home_id = _int((pbp.get("homeTeam") or {}).get("id"))
    away_id = _int((pbp.get("awayTeam") or {}).get("id"))
    table = _shift_table(shifts or {})
    names: dict[int, str] = {}
    team_of: dict[int, int] = {}
    position: dict[int, str] = {}
    for spot in pbp.get("rosterSpots") or []:
        pid = _int(spot.get("playerId"))
        if not pid:
            continue
        team_of[pid] = _int(spot.get("teamId"))
        position[pid] = str(spot.get("positionCode") or "").upper()
        names[pid] = _player_name(spot)
    # The shift chart carries names too, for anyone the roster has not listed yet.
    for row in (shifts or {}).get("data") or []:
        pid = _int(row.get("playerId"))
        if pid and pid not in names:
            names[pid] = " ".join(part for part in (str(row.get("firstName") or ""), str(row.get("lastName") or "")) if part)

    skater = {pid: position.get(pid, "") != "G" for pid in table}
    even = _even_strength(table, team_of, skater, (away_id, home_id)) if home_id and away_id and table else {}
    sides = {"home": home_id, "away": away_id}
    grouped: dict[str, dict[str, list[list[int]]]] = {}
    for side, tid in sides.items():
        forwards = [pid for pid in table if team_of.get(pid) == tid and position.get(pid) in FORWARDS]
        defence = [pid for pid in table if team_of.get(pid) == tid and position.get(pid) == "D"]
        grouped[side] = {
            "line": _group_units(forwards, table, even, 3, LINE_COUNT) if even else [],
            "pair": _group_units(defence, table, even, 2, PAIR_COUNT) if even else [],
        }

    latest = (0, -1)
    for rows in table.values():
        for period, _start, end in rows:
            if (period, end) > latest:
                latest = (period, end)

    def show(unit: list[int]) -> list[str]:
        return [_last_display(names.get(pid, "")) or str(pid) for pid in unit]

    periods = list(range(1, latest[0] + 1)) if latest[0] else []

    def unit_row(label: str, unit: list[int], on: bool) -> dict[str, Any]:
        by_period = together_by_period(table, unit)
        split = [by_period.get(period, 0) for period in periods]
        return {
            "label": label,
            "players": show(unit),
            "seconds": sum(split),
            "by_period": split,
            "on": on,
        }

    out: dict[str, Any] = {
        "through": _through_label(latest[0], latest[1], playoffs=playoffs) if latest[0] else "",
        "periods": [{"n": period, "label": _period_label(period, playoffs=playoffs)} for period in periods],
        "home": {},
        "away": {},
    }
    for side in ("home", "away"):
        skaters = [dict(row) for row in on_ice.get(side) or [] if isinstance(row, dict)]
        forwards_out = [row for row in skaters if str(row.get("position") or "").upper() not in ("D", "G")]
        defence_out = [row for row in skaters if str(row.get("position") or "").upper() == "D"]
        matched = _match_ids({pid: names[pid] for pid in names if team_of.get(pid) == sides[side]}, skaters)
        lines = grouped[side]["line"]
        pairs = grouped[side]["pair"]
        line_at = _unit_index(lines, matched)
        pair_at = _unit_index(pairs, matched)
        if line_at is not None and sum(1 for pid in lines[line_at] if pid in matched) < 2:
            line_at = None
        if pair_at is not None and sum(1 for pid in pairs[pair_at] if pid in matched) < 2:
            pair_at = None
        out[side] = {
            "lines": [unit_row(f"L{index + 1}", unit, index == line_at) for index, unit in enumerate(lines)],
            "pairs": [unit_row(f"D{index + 1}", unit, index == pair_at) for index, unit in enumerate(pairs)],
            "on_forwards": [_last_display(str(row.get("name") or "")) for row in forwards_out if _last_display(str(row.get("name") or ""))],
            "on_defence": [_last_display(str(row.get("name") or "")) for row in defence_out if _last_display(str(row.get("name") or ""))],
        }
    return out


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

    def __init__(self, fetch: FetchJson = fetch_json, fetch_text: FetchText = fetch_text) -> None:
        self._fetch = fetch
        self._fetch_text = fetch_text
        self._lock = threading.Lock()
        self._schedule: dict[str, tuple[float, dict[tuple[str, str], tuple[int, int]]]] = {}
        self._games: dict[int, dict[str, Any]] = {}
        self._misses: dict[str, float] = {}
        self._live_raw: dict[int, tuple[float, dict[str, Any], dict[str, Any]]] = {}
        self._live_miss: dict[str, float] = {}
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _report_shifts(self, game_id: int, pbp: dict[str, Any]) -> list[dict[str, Any]]:
        """Shifts from the HTML reports. A missing report leaves the JSON chart in place."""
        home_id = _int((pbp.get("homeTeam") or {}).get("id"))
        away_id = _int((pbp.get("awayTeam") or {}).get("id"))
        spots = [spot for spot in (pbp.get("rosterSpots") or []) if isinstance(spot, dict)]
        if not spots or not home_id or not away_id:
            return []

        def one(url: str, team_id: int) -> list[dict[str, Any]]:
            try:
                html = self._fetch_text(url)
            except Exception:  # noqa: BLE001 - the JSON chart still covers this club
                return []
            try:
                return shifts_from_toi_report(html, spots, team_id)
            except Exception:  # noqa: BLE001 - a broken report must not blank the chiclet
                return []

        rows: list[dict[str, Any]] = []
        urls = toi_report_urls(game_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            for result in pool.map(lambda item: one(*item), zip(urls, (away_id, home_id), strict=True)):
                rows.extend(result)
        return rows

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
            if now - self._misses.get(miss_key, 0.0) < _miss_ttl(day, now):
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

    def for_live(
        self,
        day: str,
        away_abbr: str,
        home_abbr: str,
        on_ice: dict[str, list[dict[str, str]]] | None,
        *,
        playoffs: bool,
        now: float,
    ) -> dict[str, Any] | None:
        """Who is out and how long each line has played, for a game still going."""
        away, home = nhl_abbr(away_abbr), nhl_abbr(home_abbr)
        if not day or not away or not home:
            return None
        key = f"{day}:{away}@{home}"
        with self._lock:
            if now - self._live_miss.get(key, 0.0) < LIVE_CHART_TTL:
                return None
        try:
            gid, _game_type = self._day_games(day, now).get((away, home), (0, 0))
            if not gid:
                raise LookupError(f"no NHL game {away} @ {home} on {day}")
            with self._lock:
                cached = self._live_raw.get(gid)
            if cached is not None and now - cached[0] < LIVE_CHART_TTL:
                pbp, shifts = cached[1], cached[2]
            else:
                pbp = self._fetch(NHL_PBP_URL.format(game_id=gid))
                shifts = self._fetch(f"{NHL_SHIFTS_URL.format(game_id=gid)}&limit=2000")
                pbp = pbp if isinstance(pbp, dict) else {}
                shifts = shifts if isinstance(shifts, dict) else {}
                shifts = merge_shift_charts(shifts, self._report_shifts(gid, pbp))
                with self._lock:
                    self._live_raw[gid] = (now, pbp, shifts)
        except Exception as exc:  # noqa: BLE001 - the chiclet still renders without lines
            self._last_error = str(exc)
            with self._lock:
                self._live_miss[key] = now
            return None
        return live_lines(pbp, shifts, on_ice, playoffs=playoffs)
