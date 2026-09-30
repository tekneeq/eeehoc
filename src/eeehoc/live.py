"""Live NHL scoreboard feed (ESPN public API) normalised for the Live tab."""

from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
SUMMARY_URL = "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event={event_id}"
USER_AGENT = "eeehoc/0.1 (+https://github.com/tekneeq/eeehoc)"

SCOREBOARD_TTL = 20.0  # seconds
SUMMARY_TTL_LIVE = 20.0
SUMMARY_TTL_FINAL = 15 * 60.0
FORCE_MIN_INTERVAL = 5.0

REGULATION_PERIODS = 3
PERIOD_SECONDS = 20 * 60
REGULATION_MINUTES = 60
# Regular-season overtime is 5:00. Playoffs (ESPN season type 3) play 20:00 OT.
PLAYOFF_TYPE = 3

# boxscore statistic name -> compact key shown on a chiclet
TEAM_STAT_KEYS = {
    "blockedShots": "blocked",
    "hits": "hits",
    "takeaways": "takeaways",
    "shotsTotal": "shots",
    "powerPlayGoals": "pp_goals",
    "powerPlayOpportunities": "pp_opps",
    "shortHandedGoals": "sh_goals",
    "faceoffsWon": "faceoffs",
    "faceoffPercent": "faceoff_pct",
    "giveaways": "giveaways",
    "penalties": "penalties",
    "penaltyMinutes": "pim",
}

LEADER_KEYS = {"goals": "goals", "assists": "assists", "points": "points"}
STAR_RANKS = {"firstStar": "1", "secondStar": "2", "thirdStar": "3"}
DATE_RE = re.compile(r"^\d{8}$")

# ESPN play clock for NHL counts up. Skip bookkeeping rows when picking "last play".
_SKIP_PLAYS = {"Period Start", "Period End", "End of Game", "Stoppage", "Game End"}

FetchJson = Callable[[str], dict[str, Any]]


def fetch_json(url: str, timeout: float = 15.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Live feed HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Live feed unreachable: {exc.reason}") from exc


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def period_name(period: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "OT"}.get(period, "SO" if period >= 5 else f"P{period}")


def elapsed_minute(period: int, clock_seconds: float | None) -> int:
    """ESPN period + seconds remaining -> elapsed game minute 1..60 (OT/SO clamp to 60).

    The scoreboard clock counts down. 1st period at 20:00 is minute 1; 2nd at 12:00
    remaining (eight minutes played in the period) is minute 28.
    """
    period = max(1, _int(period, 1))
    if period > REGULATION_PERIODS:
        return REGULATION_MINUTES
    remaining = 0.0 if clock_seconds is None else max(0.0, min(float(PERIOD_SECONDS), float(clock_seconds)))
    elapsed = (period - 1) * 20 + (20 - remaining / 60.0)
    return max(1, min(REGULATION_MINUTES, math.ceil(elapsed - 1e-9)))


def remaining_from_elapsed(clock: str, period: int, ot_seconds: int = 300, *, playoffs: bool = False) -> str:
    """ESPN play-by-play clock counts up. Goals read better as time remaining.

    Regulation is 20:00. Regular-season overtime is ``ot_seconds`` (5:00); a clock
    past that window is treated as 20:00. Playoff overtimes are 20:00 each.
    A regular-season shootout (period 5) is not a running clock.
    """
    if period >= 5 and not playoffs:
        return "SO"
    raw = (clock or "").strip()
    if ":" not in raw:
        return raw
    mins_s, secs_s = raw.split(":", 1)
    try:
        elapsed = int(mins_s) * 60 + int(float(secs_s))
    except ValueError:
        return raw
    if period <= REGULATION_PERIODS or playoffs:
        length = PERIOD_SECONDS
    else:
        length = max(60, int(ot_seconds))
        if elapsed > length:
            length = PERIOD_SECONDS
    rem = max(0, length - elapsed)
    return f"{rem // 60}:{rem % 60:02d}"


def _ot_label(period: int, clock: str, *, playoffs: bool) -> str:
    if playoffs and period > 4:
        return f"{period - REGULATION_PERIODS}OT {clock}".strip()
    return f"OT {clock}".strip()


def _period_label(status: dict[str, Any], state: str, *, playoffs: bool = False) -> str:
    stype = status.get("type") or {}
    name = str(stype.get("name") or "")
    period = _int(status.get("period"))
    clock = str(status.get("displayClock") or "").strip()
    short = str(stype.get("shortDetail") or "")
    shootout = "SHOOTOUT" in name or (not playoffs and period >= 5)
    if "POSTPONED" in name:
        return "Postponed"
    if "CANCEL" in name:
        return "Canceled"
    if "SUSPENDED" in name:
        return "Suspended"
    if "DELAYED" in name and state != "in":
        return "Delayed"
    if state == "post":
        if shootout:
            return "Final/SO"
        if "OT" in name or period > REGULATION_PERIODS:
            if playoffs and period > 4:
                return f"Final/{period - REGULATION_PERIODS}OT"
            return "Final/OT"
        return "Final"
    if state != "in":
        return short or "Scheduled"
    if "END_PERIOD" in name or "INTERMISSION" in name or "HALFTIME" in name:
        if period > REGULATION_PERIODS:
            return f"End {_ot_label(period, '', playoffs=playoffs)}".replace("  ", " ").strip()
        return f"End {period_name(period)}" if period else "Intermission"
    if shootout:
        return "SO"
    if period > REGULATION_PERIODS or "OVERTIME" in name:
        return _ot_label(period, clock, playoffs=playoffs)
    if period >= 1:
        return f"{period_name(period)} {clock}".strip()
    return short or "In progress"


def _team_side(team_id: str | None, home_id: str, away_id: str) -> str | None:
    if not team_id:
        return None
    if str(team_id) == str(home_id):
        return "home"
    if str(team_id) == str(away_id):
        return "away"
    return None


def _record(comp: dict[str, Any]) -> str:
    records = comp.get("records") or []
    for want in ("total", "ytd"):
        for rec in records:
            if rec.get("type") == want and rec.get("summary"):
                return str(rec["summary"])
    for rec in records:
        if rec.get("summary"):
            return str(rec["summary"])
    return ""


def _probable_goalie(comp: dict[str, Any]) -> str:
    for item in comp.get("probables") or []:
        if item.get("name") != "probableStartingGoalie":
            continue
        athlete = item.get("athlete") or {}
        return str(athlete.get("shortName") or athlete.get("displayName") or "")
    return ""


def _leaders(raw: list[dict[str, Any]] | None, team_id: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for cat in raw or []:
        key = LEADER_KEYS.get(str(cat.get("name") or ""))
        if not key:
            continue
        for leader in cat.get("leaders") or []:
            athlete = leader.get("athlete") or {}
            lid = str((leader.get("team") or {}).get("id") or (athlete.get("team") or {}).get("id") or "")
            if lid and team_id and lid != str(team_id):
                continue
            pos = athlete.get("position") or {}
            out[key] = {
                "name": str(athlete.get("shortName") or athlete.get("displayName") or ""),
                "position": str(pos.get("abbreviation") or "") if isinstance(pos, dict) else str(pos or ""),
                "line": str(leader.get("displayValue") or ""),
            }
            break
    return out


def _stars(status: dict[str, Any], home_id: str, away_id: str) -> list[dict[str, str]]:
    stars: list[dict[str, str]] = []
    for item in status.get("featuredAthletes") or []:
        rank = STAR_RANKS.get(str(item.get("name") or ""))
        if not rank:
            continue
        athlete = item.get("athlete") or {}
        stats = {str(s.get("name")): str(s.get("displayValue") or "") for s in item.get("statistics") or []}
        side = _team_side(
            (item.get("team") or {}).get("id") or (athlete.get("team") or {}).get("id"),
            home_id,
            away_id,
        )
        pos = athlete.get("position") or ""
        pos_s = str(pos.get("abbreviation") or "") if isinstance(pos, dict) else str(pos or "")
        bits: list[str] = []
        if pos_s == "G" or (stats.get("saves") and stats.get("saves") != "0"):
            if stats.get("saves"):
                bits.append(f"{stats['saves']} SV")
            if stats.get("savePct"):
                bits.append(stats["savePct"])
        else:
            if stats.get("goals") and stats["goals"] != "0":
                bits.append(f"{stats['goals']} G")
            if stats.get("assists") and stats["assists"] != "0":
                bits.append(f"{stats['assists']} A")
            if not bits and stats.get("points") and stats["points"] != "0":
                bits.append(f"{stats['points']} PTS")
        stars.append(
            {
                "rank": rank,
                "name": str(athlete.get("shortName") or athlete.get("displayName") or ""),
                "position": pos_s,
                "side": side or "",
                "line": " · ".join(bits),
            }
        )
    stars.sort(key=lambda s: s["rank"])
    return stars


def _competitor(comp: dict[str, Any]) -> dict[str, Any]:
    team = comp.get("team") or {}
    linescores = [_int(ls.get("value", ls.get("displayValue"))) for ls in comp.get("linescores") or []]
    return {
        "id": str(team.get("id") or ""),
        "abbr": str(team.get("abbreviation") or ""),
        "name": str(team.get("shortDisplayName") or team.get("name") or ""),
        "full_name": str(team.get("displayName") or ""),
        "location": str(team.get("location") or ""),
        "color": str(team.get("color") or "444444"),
        "alt_color": str(team.get("alternateColor") or ""),
        "logo": str(team.get("logo") or ""),
        "score": _int(comp.get("score")),
        "record": _record(comp),
        "linescores": linescores,
        "winner": bool(comp.get("winner", False)),
        "goalie": _probable_goalie(comp),
        "leaders": _leaders(comp.get("leaders"), str(team.get("id") or "")),
        "stats": {},
        "goalies": [],
    }


def _coordinate(raw: dict[str, Any] | None) -> tuple[float | None, float | None]:
    coord = (raw or {}).get("coordinate") or {}
    x, y = _float(coord.get("x")), _float(coord.get("y"))
    return x, y


def _last_play(raw: dict[str, Any] | None, home_id: str, away_id: str) -> dict[str, Any] | None:
    if not raw:
        return None
    kind = str((raw.get("type") or {}).get("text") or "")
    if kind in _SKIP_PLAYS:
        return None
    text = str(raw.get("text") or "")
    if not text and not kind:
        return None
    x, y = _coordinate(raw)
    clock = raw.get("clock")
    if isinstance(clock, dict):
        clock_s = str(clock.get("displayValue") or "")
    else:
        clock_s = str(clock or "")
    return {
        "text": text,
        "type": kind,
        "team": _team_side((raw.get("team") or {}).get("id"), home_id, away_id),
        "score_value": _int(raw.get("scoreValue")),
        "period": _int((raw.get("period") or {}).get("number") or raw.get("period")),
        "clock": clock_s,
        "x": x,
        "y": y,
        "strength": str((raw.get("strength") or {}).get("abbreviation") or ""),
    }


def _on_ice_counts(summary: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for side in summary.get("onIce") or []:
        tid = str(side.get("teamId") or "")
        if not tid:
            continue
        n = 0
        for entry in side.get("entries") or []:
            where = entry.get("whereabouts") or {}
            desc = str(where.get("description") or "")
            if desc == "In Play" or str(where.get("id") or "") == "1":
                n += 1
        counts[tid] = n
    return counts


def _strength_label(home_n: int | None, away_n: int | None) -> tuple[str, str | None]:
    """Return ('5-on-4', 'home'|'away'|None). Counts include the goalie."""
    if home_n is None or away_n is None or home_n == away_n:
        return "", None
    home_sk = max(0, home_n - 1)
    away_sk = max(0, away_n - 1)
    if home_sk == away_sk:
        return "", None
    power_play = "home" if home_sk > away_sk else "away"
    hi, lo = sorted((home_sk, away_sk), reverse=True)
    return f"{hi}-on-{lo}", power_play


def situation_from_summary(summary: dict[str, Any], home_id: str, away_id: str) -> dict[str, Any]:
    last = None
    for play in reversed(summary.get("plays") or []):
        parsed = _last_play(play, home_id, away_id)
        if parsed:
            last = parsed
            break
    counts = _on_ice_counts(summary)
    home_n = counts.get(str(home_id))
    away_n = counts.get(str(away_id))
    label, power_play = _strength_label(home_n, away_n)
    return {
        "last_play": last,
        "power_play": power_play,
        "home_on_ice": home_n,
        "away_on_ice": away_n,
        "strength": label,
    }


def _goal_tag(play: dict[str, Any]) -> str:
    abbr = str((play.get("strength") or {}).get("abbreviation") or "")
    text = str(play.get("text") or "").lower()
    tag = ""
    if abbr == "power-play":
        tag = "PP"
    elif abbr == "short-handed":
        tag = "SH"
    if "empty net" in text or "empty-net" in text:
        tag = f"{tag} EN".strip()
    return tag


def _goal_when(period: int, elapsed: str, ot_seconds: int, playoffs: bool) -> str:
    remaining = remaining_from_elapsed(elapsed, period, ot_seconds, playoffs=playoffs)
    if period >= 5 and not playoffs:
        return "SO"
    if period > REGULATION_PERIODS and playoffs:
        return f"{period - REGULATION_PERIODS}OT {remaining}".strip()
    return f"{period_name(period)} {remaining}".strip()


def _goals(
    plays: list[dict[str, Any]], home_id: str, away_id: str, ot_seconds: int, *, playoffs: bool = False
) -> list[dict[str, Any]]:
    goals: list[dict[str, Any]] = []
    for play in plays:
        if not play.get("scoringPlay"):
            continue
        participants = play.get("participants") or []
        scorer = next((p for p in participants if p.get("type") == "scorer"), participants[0] if participants else {})
        athlete = (scorer or {}).get("athlete") or {}
        assists = []
        for person in participants:
            if person.get("type") != "assister":
                continue
            name = str((person.get("athlete") or {}).get("shortName") or "")
            if name:
                assists.append(name)
        period = _int((play.get("period") or {}).get("number"))
        elapsed = str((play.get("clock") or {}).get("displayValue") or "")
        name = str(athlete.get("shortName") or athlete.get("displayName") or "")
        ytd = scorer.get("ytdGoals") if isinstance(scorer, dict) else None
        if ytd is not None and str(ytd) != "":
            name = f"{name} ({_int(ytd)})".strip()
        when = _goal_when(period, elapsed, ot_seconds, playoffs)
        goals.append(
            {
                "period": period,
                "when": when,
                "side": _team_side((play.get("team") or {}).get("id"), home_id, away_id) or "",
                "scorer": name,
                "assists": assists,
                "tag": _goal_tag(play),
                "text": str(play.get("text") or ""),
            }
        )
    return goals


def _indexed(labels: list[Any], stats: list[Any], label: str) -> str:
    try:
        i = [str(x) for x in labels].index(label)
    except ValueError:
        return ""
    if i >= len(stats):
        return ""
    return str(stats[i])


def _toi_seconds(toi: str) -> int:
    if ":" not in (toi or ""):
        return 0
    mins_s, secs_s = toi.split(":", 1)
    try:
        return int(mins_s) * 60 + int(float(secs_s))
    except ValueError:
        return 0


def _goalies(summary: dict[str, Any], home_id: str, away_id: str) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {"home": [], "away": []}
    for group in (summary.get("boxscore") or {}).get("players") or []:
        side = _team_side((group.get("team") or {}).get("id"), home_id, away_id)
        if side is None:
            continue
        for block in group.get("statistics") or []:
            if str(block.get("name") or "") != "goalies":
                continue
            labels = block.get("labels") or []
            rows: list[dict[str, str]] = []
            for row in block.get("athletes") or []:
                person = row.get("athlete") or {}
                stats = row.get("stats") or []
                rows.append(
                    {
                        "name": str(person.get("shortName") or person.get("displayName") or ""),
                        "saves": _indexed(labels, stats, "SV"),
                        "save_pct": _indexed(labels, stats, "SV%"),
                        "goals_against": _indexed(labels, stats, "GA"),
                        "shots_against": _indexed(labels, stats, "SA"),
                        "toi": _indexed(labels, stats, "TOI"),
                    }
                )
            played = [g for g in rows if _toi_seconds(g["toi"]) > 0]
            out[side] = sorted(played or rows, key=lambda g: _toi_seconds(g["toi"]), reverse=True)
    return out


def _broadcast(comp: dict[str, Any]) -> str:
    names: list[str] = []
    for row in comp.get("broadcasts") or []:
        for name in row.get("names") or []:
            text = str(name).strip()
            if text and text not in names:
                names.append(text)
    if names:
        return " · ".join(names)
    return str(comp.get("broadcast") or "")


def normalize_event(event: dict[str, Any], *, ot_seconds: int = 300, playoffs: bool = False) -> dict[str, Any]:
    comp = (event.get("competitions") or [{}])[0]
    status = comp.get("status") or event.get("status") or {}
    stype = status.get("type") or {}
    state = str(stype.get("state") or "pre")
    competitors = comp.get("competitors") or []
    home_raw = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0] if competitors else {})
    away_raw = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1] if competitors else {})
    home = _competitor(home_raw)
    away = _competitor(away_raw)
    if state == "post" and not home["winner"] and not away["winner"]:
        if home["score"] > away["score"]:
            home["winner"] = True
        elif away["score"] > home["score"]:
            away["winner"] = True
    # Scoreboard "leaders" on a game that has not started are season totals, not this game.
    if state == "pre":
        home["leaders"] = {}
        away["leaders"] = {}
    period = _int(status.get("period"))
    clock_seconds = _float(status.get("clock"))
    notes = []
    for note in comp.get("notes") or []:
        headline = str(note.get("headline") or note.get("text") or "").strip()
        if headline:
            notes.append(headline)
    game = {
        "id": str(event.get("id") or comp.get("id") or ""),
        "name": str(event.get("name") or ""),
        "short_name": str(event.get("shortName") or f"{away['abbr']} @ {home['abbr']}"),
        "date": str(event.get("date") or comp.get("date") or ""),
        "state": state,
        "completed": bool(stype.get("completed", state == "post")),
        "status_detail": str(stype.get("shortDetail") or stype.get("detail") or ""),
        "period": period,
        "clock": str(status.get("displayClock") or ""),
        "period_label": _period_label(status, state, playoffs=playoffs),
        "venue": str((comp.get("venue") or {}).get("fullName") or ""),
        "broadcast": _broadcast(comp),
        "notes": notes,
        "ot_seconds": ot_seconds,
        "playoffs": playoffs,
        "home": home,
        "away": away,
        "situation": None,
        "goals": [],
        "stars": _stars(status, home["id"], away["id"]),
    }
    if state in ("in", "post"):
        game["minute"] = REGULATION_MINUTES if state == "post" else elapsed_minute(period, clock_seconds)
    else:
        game["minute"] = None
    raw_situation = comp.get("situation") if state == "in" else None
    if isinstance(raw_situation, dict):
        game["situation"] = {
            "last_play": _last_play(raw_situation.get("lastPlay"), home["id"], away["id"]),
            "power_play": None,
            "home_on_ice": None,
            "away_on_ice": None,
            "strength": "",
        }
    return game


def _season_bits(payload: dict[str, Any]) -> tuple[int, str, int, str, int]:
    leagues = payload.get("leagues") or []
    season = payload.get("season") or {}
    league_season = (leagues[0].get("season") if leagues else None) or {}
    year = _int(league_season.get("year") or season.get("year"))
    display = str(league_season.get("displayName") or "")
    if not display and year:
        display = f"{year - 1}–{str(year)[-2:]}"
    type_info = league_season.get("type") if isinstance(league_season.get("type"), dict) else {}
    type_id = _int((type_info or {}).get("type") or season.get("type"))
    type_name = str((type_info or {}).get("name") or "")
    ot_seconds = PERIOD_SECONDS if type_id == PLAYOFF_TYPE else 5 * 60
    return year, display, type_id, type_name, ot_seconds


def normalize_scoreboard(payload: dict[str, Any]) -> dict[str, Any]:
    year, display, type_id, type_name, ot_seconds = _season_bits(payload)
    events = payload.get("events") or []
    playoffs = type_id == PLAYOFF_TYPE
    games = [normalize_event(e, ot_seconds=ot_seconds, playoffs=playoffs) for e in events]
    order = {"in": 0, "pre": 1, "post": 2}
    games.sort(key=lambda g: (order.get(g["state"], 3), g["date"], g["short_name"]))
    return {
        "season": year,
        "season_label": display,
        "season_type": type_id,
        "season_type_name": type_name,
        "day": str((payload.get("day") or {}).get("date") or ""),
        "ot_seconds": ot_seconds,
        "games": games,
    }


def _compose_special_teams(stats: dict[str, Any]) -> None:
    if "pp_goals" in stats or "pp_opps" in stats:
        stats["power_play"] = f"{stats.get('pp_goals') or '0'}/{stats.get('pp_opps') or '0'}"


def apply_summary(game: dict[str, Any], summary: dict[str, Any]) -> None:
    """Merge team box stats, goals, goalies, and the live situation into a game."""
    home_id, away_id = game["home"]["id"], game["away"]["id"]
    for team_box in (summary.get("boxscore") or {}).get("teams") or []:
        side = _team_side((team_box.get("team") or {}).get("id"), home_id, away_id)
        if side is None:
            side = "home" if team_box.get("homeAway") == "home" else "away" if team_box.get("homeAway") == "away" else None
        if side is None:
            continue
        stats: dict[str, Any] = {}
        for stat in team_box.get("statistics") or []:
            key = TEAM_STAT_KEYS.get(str(stat.get("name") or ""))
            if key and key not in stats:
                stats[key] = str(stat.get("displayValue") or "")
        _compose_special_teams(stats)
        game[side]["stats"] = stats

    game["goals"] = _goals(
        summary.get("plays") or [],
        home_id,
        away_id,
        _int(game.get("ot_seconds"), 300),
        playoffs=bool(game.get("playoffs")),
    )
    tenders = _goalies(summary, home_id, away_id)
    for side in ("home", "away"):
        if tenders[side]:
            game[side]["goalies"] = tenders[side]

    for team_leaders in summary.get("leaders") or []:
        side = _team_side((team_leaders.get("team") or {}).get("id"), home_id, away_id)
        if side is None:
            continue
        merged = _leaders(team_leaders.get("leaders"), game[side]["id"])
        if merged:
            game[side]["leaders"] = {**game[side]["leaders"], **merged}

    if game["state"] == "in":
        game["situation"] = situation_from_summary(summary, home_id, away_id)
    else:
        game["situation"] = None

    if not game["stars"]:
        header_comp = ((summary.get("header") or {}).get("competitions") or [{}])[0]
        header_status = header_comp.get("status") or {}
        stars = _stars(header_status, home_id, away_id)
        if stars:
            game["stars"] = stars


def scoreboard_url(dates: str | None) -> str:
    if dates:
        return f"{SCOREBOARD_URL}?dates={dates}"
    return SCOREBOARD_URL


class LiveFeed:
    """Cached scoreboard + per-game summaries; safe to call from many request threads."""

    def __init__(
        self,
        fetch: FetchJson = fetch_json,
        *,
        ttl: float = SCOREBOARD_TTL,
        workers: int = 8,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._workers = max(1, workers)
        self._lock = threading.Lock()
        self._boards: dict[str, tuple[float, dict[str, Any]]] = {}
        self._summaries: dict[str, tuple[float, dict[str, Any], str]] = {}
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _summary_for(self, game: dict[str, Any], now: float) -> dict[str, Any] | None:
        gid = game["id"]
        cached = self._summaries.get(gid)
        ttl = SUMMARY_TTL_FINAL if game["state"] == "post" else SUMMARY_TTL_LIVE
        if cached and now - cached[0] < ttl and cached[2] == game["state"]:
            return cached[1]
        try:
            summary = self._fetch(SUMMARY_URL.format(event_id=gid))
        except Exception:  # noqa: BLE001 - a missing box score must not break the board
            return cached[1] if cached else None
        self._summaries[gid] = (now, summary, game["state"])
        return summary

    def _build(self, now: float, dates: str | None) -> dict[str, Any]:
        board = normalize_scoreboard(self._fetch(scoreboard_url(dates)))
        games = board["games"]
        with_box = [g for g in games if g["state"] in ("in", "post")]
        if with_box:
            with ThreadPoolExecutor(max_workers=min(self._workers, len(with_box))) as pool:
                summaries = list(pool.map(lambda g: self._summary_for(g, now), with_box))
            for game, summary in zip(with_box, summaries):
                if summary:
                    apply_summary(game, summary)
        if dates and not board["day"]:
            board["day"] = f"{dates[:4]}-{dates[4:6]}-{dates[6:8]}"
        board["dates"] = dates or ""
        board["fetched_at"] = int(now)
        board["ttl"] = int(self._ttl)
        board["counts"] = {
            "live": sum(1 for g in games if g["state"] == "in"),
            "final": sum(1 for g in games if g["state"] == "post"),
            "upcoming": sum(1 for g in games if g["state"] == "pre"),
        }
        return board

    def get(self, *, force: bool = False, dates: str | None = None) -> dict[str, Any]:
        if dates is not None and not DATE_RE.fullmatch(dates):
            raise ValueError("dates must be YYYYMMDD")
        key = dates or ""
        now = time.time()
        with self._lock:
            cached = self._boards.get(key)
            if cached is not None:
                age = now - cached[0]
                if age < (FORCE_MIN_INTERVAL if force else self._ttl):
                    return cached[1]
            try:
                payload = self._build(now, dates)
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                if cached is not None:
                    stale = dict(cached[1])
                    stale["stale"] = True
                    stale["error"] = self._last_error
                    return stale
                raise
            self._last_error = None
            self._boards[key] = (now, payload)
            return payload
