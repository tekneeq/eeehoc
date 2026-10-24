"""Where a game goes from the score at the end of a period — the branch tree.

eeesoc's scoreline tree, for hockey's natural checkpoints. Every final in the
last few seasons is read from ESPN's month scoreboards, which carry each
club's goals by period, the way the game ended (regulation, overtime or a
shootout) and the goalies' saves. A live game is placed at the score after its
last completed period, and three lenses say what followed from that state:

* the home club's own games, from its side (for–against);
* the away club's own games, the same way;
* every NHL game, home–away.

Each lens gives the share that won, lost or went past regulation, the branch
tree — how often each score came out of the *next* period, with the branch the
live game is on marked — and the final scores that followed.

On top of the history there is a model for the rest of *this* game: each
side's shots-on-goal pace (its season rate blended with tonight's) times the
other goalie's save percentage (his season number, shrunk toward the league's)
gives a goal rate for the time left; Poisson from the current score gives the
chance of each side winning or of overtime.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

from eeehoc.live import PLAYOFF_TYPE, SCOREBOARD_URL, FetchJson, _int, fetch_json
from eeehoc.periods import ET, REGULAR_TYPE, _game_day, _months, _season_meta

SEASONS_BACK = 2  # full seasons before the current one
REGULATION = 3
PERIOD_SECONDS = 20 * 60
REGULATION_SECONDS = REGULATION * PERIOD_SECONDS
MAX_BRANCHES = 6
MAX_FINALS = 6

# Model: tonight's shot pace counts only after the first few minutes, and is worth as much
# as the season rate once this much more has been played.
PACE_MIN_SECONDS = 5 * 60
PACE_HALF_LIFE_SECONDS = 20 * 60
# A goalie's season save percentage is pulled this far toward the league's.
GOALIE_TRUST = 0.6
LEAGUE_SAVE_PCT = 0.900
LEAGUE_SHOTS_PER_60 = 29.0
# Overtime goes to the side with the better goal rate, halfway between a coin flip and the rate ratio.
OT_RATE_WEIGHT = 0.5
POISSON_MAX = 10

CURRENT_MONTH_TTL = 90.0
PAST_MONTH_TTL = 6 * 60 * 60.0
HISTORY_TTL = 60.0
CACHE_VERSION = 1
OFF_SEASON_MONTHS = ("07", "08")

MODEL_NOTE = (
    "Rest-of-game model: each side's shots on goal per 60 (season rate, blended with tonight's pace as "
    "the game goes on) × the other goalie's save percentage (season number, shrunk toward the league's) "
    "gives a goal rate for the time left; Poisson from the current score. Ties after regulation go to "
    "overtime, split halfway between a coin flip and the two goal rates. Empty nets and penalties are not modelled."
)


def _stat(competitor: dict[str, Any], name: str) -> int | None:
    for stat in competitor.get("statistics") or []:
        if str(stat.get("name") or "") == name:
            value = stat.get("displayValue", stat.get("value"))
            return None if value in (None, "") else _int(value)
    return None


def _ending(detail: str) -> str:
    tail = detail.split("/", 1)[1].strip().upper() if "/" in detail else ""
    if tail.startswith("SO"):
        return "SO"
    if tail.startswith("OT") or tail.endswith("OT"):
        return "OT"
    return "REG"


def parse_month(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every regular-season or playoff final with goals by period."""
    games: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        stype = ((comp.get("status") or event.get("status") or {}).get("type")) or {}
        if str(stype.get("name") or "") != "STATUS_FINAL":
            continue
        season = event.get("season") or {}
        season_type = _int(season.get("type"))
        if season_type not in (REGULAR_TYPE, PLAYOFF_TYPE):
            continue
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        lines = {}
        for side, raw in (("home", home), ("away", away)):
            by_period = {}
            for ls in raw.get("linescores") or []:
                by_period[_int(ls.get("period"))] = _int(ls.get("value", ls.get("displayValue")))
            lines[side] = by_period
        if not lines["home"] or not lines["away"]:
            continue
        periods = []
        for p in range(1, REGULATION + 1):
            if p not in lines["home"] and p not in lines["away"]:
                break
            periods.append((lines["home"].get(p, 0), lines["away"].get(p, 0)))
        if len(periods) < REGULATION:
            continue
        home_score, away_score = _int(home.get("score")), _int(away.get("score"))
        day = _game_day(str(event.get("date") or comp.get("date") or ""))
        home_saves, away_saves = _stat(home, "saves"), _stat(away, "saves")
        home_goals, away_goals = _stat(home, "goals"), _stat(away, "goals")
        games.append(
            {
                "id": str(event.get("id") or comp.get("id") or ""),
                "day": day.isoformat() if day else "",
                "season_year": _int(season.get("year")),
                "season_type": season_type,
                "home_id": str((home.get("team") or {}).get("id") or ""),
                "home_abbr": str((home.get("team") or {}).get("abbreviation") or ""),
                "away_id": str((away.get("team") or {}).get("id") or ""),
                "away_abbr": str((away.get("team") or {}).get("abbreviation") or ""),
                "periods": periods,
                "home_score": home_score,
                "away_score": away_score,
                "end": _ending(str(stype.get("shortDetail") or stype.get("detail") or "")),
                # Shots on goal = the other goalie's saves + goals (shootout goals are not shots).
                "home_sog": (away_saves + (home_goals if home_goals is not None else home_score)) if away_saves is not None else None,
                "away_sog": (home_saves + (away_goals if away_goals is not None else away_score)) if home_saves is not None else None,
                "home_saves": home_saves,
                "away_saves": away_saves,
            }
        )
    return games


def score_after(game: dict[str, Any], periods: int) -> tuple[int, int]:
    """Home–away score after ``periods`` completed periods (3 = end of regulation)."""
    n = max(0, min(REGULATION, periods))
    return sum(h for h, _a in game["periods"][:n]), sum(a for _h, a in game["periods"][:n])


def _pct(count: int, total: int) -> float | None:
    return None if total <= 0 else round(100.0 * count / total, 0)


def _key(a: int, b: int) -> str:
    return f"{a}-{b}"


def _lens(
    games: list[dict[str, Any]],
    *,
    after: int,
    score: tuple[int, int],
    team_id: str | None,
    live_next: tuple[int, int] | None,
) -> dict[str, Any]:
    """What followed ``score`` after ``after`` periods — for one club (for–against) or the league (home–away)."""
    n = 0
    wins = losses = 0
    ends = {"REG": 0, "OT": 0, "SO": 0}
    win_by = {"REG": 0, "OT": 0, "SO": 0}
    stayed = 0
    next_counts: dict[str, int] = {}
    finals: dict[str, int] = {}
    for g in games:
        if team_id is not None:
            if g["home_id"] == team_id:
                flip = False
            elif g["away_id"] == team_id:
                flip = True
            else:
                continue
        else:
            flip = False
        h, a = score_after(g, after)
        state = (a, h) if flip else (h, a)
        if state != score:
            continue
        n += 1
        fh, fa = g["home_score"], g["away_score"]
        final = (fa, fh) if flip else (fh, fa)
        finals[_key(*final)] = finals.get(_key(*final), 0) + 1
        ends[g["end"]] += 1
        if final[0] > final[1]:
            wins += 1
            win_by[g["end"]] += 1
        elif final[1] > final[0]:
            losses += 1
        # Regulation score is the three periods; a shootout adds one to the winner's final.
        reg = score_after(g, REGULATION)
        reg_state = (reg[1], reg[0]) if flip else reg
        if reg_state == score:
            stayed += 1
        if after < REGULATION:
            nh, na = score_after(g, after + 1)
            nxt = (na, nh) if flip else (nh, na)
            next_counts[_key(*nxt)] = next_counts.get(_key(*nxt), 0) + 1
        else:
            next_counts[g["end"]] = next_counts.get(g["end"], 0) + 1

    live_key = _key(*live_next) if live_next is not None and after < REGULATION else None
    branches = []
    for key, count in sorted(next_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        branches.append({"score": key, "count": count, "pct": _pct(count, n), "live": key == live_key})
    shown = branches[:MAX_BRANCHES]
    if live_key and not any(b["live"] for b in shown):
        hit = next((b for b in branches if b["live"]), None)
        if hit:
            shown = shown[: MAX_BRANCHES - 1] + [hit]
        else:
            shown = shown[: MAX_BRANCHES - 1] + [{"score": live_key, "count": 0, "pct": 0.0 if n else None, "live": True}]
    rest = sum(b["count"] for b in branches) - sum(b["count"] for b in shown)
    return {
        "team": team_id,
        "count": n,
        "win_pct": _pct(wins, n),
        "loss_pct": _pct(losses, n),
        "past_regulation_pct": _pct(ends["OT"] + ends["SO"], n),
        "ot_pct": _pct(ends["OT"], n),
        "so_pct": _pct(ends["SO"], n),
        "wins": wins,
        "losses": losses,
        "win_by": win_by,
        "stayed_pct": _pct(stayed, n),
        "branches": shown,
        "other_count": rest,
        "other_pct": _pct(rest, n),
        "finals": [{"score": k, "count": c, "pct": _pct(c, n)} for k, c in sorted(finals.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_FINALS]],
    }


# ------------------------------------------------------------------ live state


def _clock_seconds(clock: str) -> float | None:
    text = str(clock or "").strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


def game_state(game: dict[str, Any]) -> dict[str, Any]:
    """Completed periods, the score then, the live score, and regulation time left."""
    state = str(game.get("state") or "pre")
    period = _int(game.get("period"))
    clock = _clock_seconds(str(game.get("clock") or ""))
    home_lines = list((game.get("home") or {}).get("linescores") or [])
    away_lines = list((game.get("away") or {}).get("linescores") or [])
    live_home = _int((game.get("home") or {}).get("score"))
    live_away = _int((game.get("away") or {}).get("score"))

    if state == "pre" or period < 1:
        completed = 0
        elapsed = 0
    elif state == "post":
        completed = REGULATION
        elapsed = REGULATION_SECONDS
    else:
        at_buzzer = clock is not None and clock <= 0
        completed = min(REGULATION, period if at_buzzer else period - 1)
        if period > REGULATION:
            elapsed = REGULATION_SECONDS
        else:
            remaining = PERIOD_SECONDS if clock is None else max(0.0, min(float(PERIOD_SECONDS), clock))
            elapsed = int((period - 1) * PERIOD_SECONDS + (PERIOD_SECONDS - remaining))
    if completed and len(home_lines) >= completed and len(away_lines) >= completed:
        score = (sum(home_lines[:completed]), sum(away_lines[:completed]))
    elif completed == REGULATION and state == "post" and str(game.get("status_detail") or "").split("/")[-1] in ("OT", "SO"):
        # No linescores: a game decided past regulation was tied after three.
        tied = min(live_home, live_away)
        score = (tied, tied)
    elif completed:
        score = (live_home, live_away)
    else:
        score = (0, 0)
    # The branch is "taken" once the next period has also finished; until then the live score is only "so far".
    next_done = completed < REGULATION and (state == "post" or (state == "in" and period > completed + 1))
    next_started = completed < REGULATION and (next_done or (state == "in" and period > completed))
    return {
        "state": state,
        "period": period,
        "completed": completed,
        "score": score,
        "live_score": (live_home, live_away),
        "elapsed": elapsed,
        "remaining": max(0, REGULATION_SECONDS - elapsed),
        "next_period_started": bool(next_started),
        "next_period_done": bool(next_done),
    }


# ----------------------------------------------------------------------- model


def _poisson(lam: float, k: int) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def _save_pct(text: Any) -> float | None:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if value > 1.0:
        value /= 100.0
    return value if 0.5 <= value <= 1.0 else None


def _goalie(team: dict[str, Any]) -> dict[str, Any]:
    tenders = team.get("goalies") or []
    named = next((g for g in tenders if g.get("name")), None)
    if named:
        return {
            "name": str(named.get("name") or ""),
            "rank": str(named.get("rank") or ""),
            "save_pct": _save_pct(named.get("save_pct_season")),
            "saves_tonight": _int(named.get("saves")) if named.get("saves") not in (None, "") else None,
            "shots_tonight": _int(named.get("shots_against")) if named.get("shots_against") not in (None, "") else None,
        }
    return {
        "name": str(team.get("goalie") or ""),
        "rank": str(team.get("goalie_rank") or ""),
        "save_pct": _save_pct(team.get("goalie_save_pct")),
        "saves_tonight": None,
        "shots_tonight": None,
    }


def rest_of_game(
    game: dict[str, Any],
    live: dict[str, Any],
    *,
    power: dict[str, Any] | None,
    league_shots: float,
    league_save_pct: float,
) -> dict[str, Any] | None:
    """Chance of each side winning from here, from shot pace and the goalies."""
    if live["state"] == "post":
        return None
    remaining = live["remaining"]
    elapsed = live["elapsed"]
    teams = (power or {}).get("teams") or {}
    sides = {}
    for side, other in (("home", "away"), ("away", "home")):
        team = game.get(side) or {}
        opp = game.get(other) or {}
        row = teams.get(str(team.get("id") or "")) or {}
        opp_row = teams.get(str(opp.get("id") or "")) or {}
        season_for = row.get("sog_per_game")
        season_allowed = opp_row.get("sog_allowed_per_game")
        rates = [r for r in (season_for, season_allowed) if r]
        season_rate = sum(rates) / len(rates) if rates else league_shots
        shots_text = (team.get("stats") or {}).get("shots")
        shots = _int(shots_text) if shots_text not in (None, "") else None
        if shots is not None and elapsed > 0:
            counted = max(0, elapsed - PACE_MIN_SECONDS)
            weight = counted / (counted + PACE_HALF_LIFE_SECONDS)
            tonight = shots * 3600.0 / elapsed
            rate = (1 - weight) * season_rate + weight * tonight
        else:
            tonight = None
            rate = season_rate
        goalie = _goalie(opp)
        sv = goalie["save_pct"]
        used = league_save_pct + GOALIE_TRUST * (sv - league_save_pct) if sv is not None else league_save_pct
        lam = rate / 3600.0 * (1.0 - used) * remaining
        sides[side] = {
            "abbr": str(team.get("abbr") or ""),
            "shots_per_60": round(season_rate, 1),
            "tonight_per_60": None if tonight is None else round(tonight, 1),
            "shots_tonight": shots,
            "rate_used": round(rate, 1),
            "opp_goalie": goalie["name"],
            "opp_goalie_rank": goalie["rank"],
            "opp_goalie_save_pct": None if sv is None else round(sv, 3),
            "save_pct_used": round(used, 3),
            "expected_goals_left": round(lam, 2),
        }
    lam_h, lam_a = sides["home"]["expected_goals_left"], sides["away"]["expected_goals_left"]
    hs, as_ = live["live_score"]
    p_home = p_away = p_tie = 0.0
    if remaining <= 0:
        if hs == as_:
            p_tie = 1.0
        elif hs > as_:
            p_home = 1.0
        else:
            p_away = 1.0
    else:
        ph = [_poisson(lam_h, k) for k in range(POISSON_MAX + 1)]
        pa = [_poisson(lam_a, k) for k in range(POISSON_MAX + 1)]
        for i, pi in enumerate(ph):
            for j, pj in enumerate(pa):
                prob = pi * pj
                diff = (hs + i) - (as_ + j)
                if diff > 0:
                    p_home += prob
                elif diff < 0:
                    p_away += prob
                else:
                    p_tie += prob
    total_rate = lam_h + lam_a
    ratio = lam_h / total_rate if total_rate > 0 else 0.5
    ot_home = 0.5 + OT_RATE_WEIGHT * (ratio - 0.5)
    return {
        "remaining_sec": remaining,
        "home_win": round(100.0 * (p_home + p_tie * ot_home), 0),
        "away_win": round(100.0 * (p_away + p_tie * (1 - ot_home)), 0),
        "to_overtime": round(100.0 * p_tie, 0),
        "home_win_regulation": round(100.0 * p_home, 0),
        "away_win_regulation": round(100.0 * p_away, 0),
        "home": sides["home"],
        "away": sides["away"],
        "league_shots_per_60": round(league_shots, 1),
        "league_save_pct": round(league_save_pct, 3),
        "note": MODEL_NOTE,
    }


# ----------------------------------------------------------------------- build


def league_rates(games: list[dict[str, Any]], season_year: int) -> tuple[float, float]:
    """League shots on goal per team-game and save percentage, this season (all seasons if none yet)."""
    pool = [g for g in games if g["season_year"] == season_year and g["home_sog"] is not None and g["away_sog"] is not None]
    if not pool:
        pool = [g for g in games if g["home_sog"] is not None and g["away_sog"] is not None]
    if not pool:
        return LEAGUE_SHOTS_PER_60, LEAGUE_SAVE_PCT
    shots = sum(g["home_sog"] + g["away_sog"] for g in pool)
    saves = sum((g["home_saves"] or 0) + (g["away_saves"] or 0) for g in pool)
    return shots / (2 * len(pool)), (saves / shots if shots else LEAGUE_SAVE_PCT)


def build_paths(
    games: list[dict[str, Any]],
    game: dict[str, Any],
    *,
    power: dict[str, Any] | None = None,
    season_year: int = 0,
) -> dict[str, Any]:
    live = game_state(game)
    after = live["completed"]
    score = live["score"]
    home_id = str((game.get("home") or {}).get("id") or "")
    away_id = str((game.get("away") or {}).get("id") or "")
    hs, as_ = live["live_score"]
    live_next = (hs, as_) if live["next_period_started"] else None
    lenses = {
        "home": _lens(games, after=after, score=score, team_id=home_id, live_next=live_next),
        "away": _lens(games, after=after, score=(score[1], score[0]), team_id=away_id, live_next=(as_, hs) if live_next else None),
        "league": _lens(games, after=after, score=score, team_id=None, live_next=live_next),
    }
    years = sorted({g["season_year"] for g in games if g["season_year"]})
    shots_60, save_pct = league_rates(games, season_year or (years[-1] if years else 0))
    model = rest_of_game(game, live, power=power, league_shots=shots_60, league_save_pct=save_pct)
    return {
        "game": str(game.get("id") or ""),
        "state": live["state"],
        "after": after,
        "score": _key(*score),
        "live_score": _key(hs, as_),
        "live_branch": _key(hs, as_) if live_next else None,
        "next_period_done": live["next_period_done"],
        "elapsed_sec": live["elapsed"],
        "remaining_sec": live["remaining"],
        "home": {"id": home_id, "abbr": str((game.get("home") or {}).get("abbr") or ""), **lenses["home"]},
        "away": {"id": away_id, "abbr": str((game.get("away") or {}).get("abbr") or ""), **lenses["away"]},
        "league": lenses["league"],
        "model": model,
        "games": len(games),
        "seasons": [f"{y - 1}-{str(y)[-2:]}" for y in years],
    }


class PathsFeed:
    """Finals from the last few seasons, loaded in the background, and the tree for any live game.

    The current season is fetched first and served straight away; older seasons
    are added behind it. Months that are over are parsed once and kept on disk
    (``EEEHOC_CACHE_DIR``, default ``~/.cache/eeehoc``), so a restart serves the
    whole history without going back to ESPN for it.
    """

    def __init__(self, fetch: FetchJson = fetch_json, *, workers: int = 6, cache_dir: Path | str | None = None) -> None:
        self._fetch = fetch
        self._workers = max(1, workers)
        self._lock = threading.Lock()
        self._months: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._history: tuple[float, date, int, list[dict[str, Any]], bool] | None = None
        self._progress: tuple[int, int] = (0, 0)
        self._worker: threading.Thread | None = None
        self._last_error: str | None = None
        root = cache_dir if cache_dir is not None else os.environ.get("EEEHOC_CACHE_DIR") or Path.home() / ".cache" / "eeehoc"
        self._cache_dir = Path(root) / "paths"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def progress(self) -> tuple[int, int]:
        return self._progress

    def _disk_path(self, ym: str) -> Path:
        return self._cache_dir / f"{ym}.json"

    def _read_disk(self, ym: str) -> list[dict[str, Any]] | None:
        try:
            raw = json.loads(self._disk_path(ym).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION or not isinstance(raw.get("games"), list):
            return None
        games = raw["games"]
        for g in games:
            g["periods"] = [tuple(p) for p in g.get("periods") or []]
        return games

    def _write_disk(self, ym: str, games: list[dict[str, Any]]) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._disk_path(ym).with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "month": ym, "games": games}), "utf-8")
            tmp.replace(self._disk_path(ym))
        except OSError as exc:
            self._last_error = f"paths cache: {exc}"

    def _load_month(self, ym: str, now: float, today: date) -> list[dict[str, Any]]:
        current = ym >= f"{today.year}{today.month:02d}"
        ttl = CURRENT_MONTH_TTL if current else PAST_MONTH_TTL
        with self._lock:
            cached = self._months.get(ym)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        if not current and cached is None:
            kept = self._read_disk(ym)
            if kept is not None:
                with self._lock:
                    self._months[ym] = (now, kept)
                return kept
        try:
            payload = self._fetch(f"{SCOREBOARD_URL}?dates={ym}&limit=1000")
        except Exception as exc:  # noqa: BLE001 - keep the month we had
            self._last_error = str(exc)
            if cached is not None:
                return cached[1]
            if current:
                raise
            return []
        games = parse_month(payload) if isinstance(payload, dict) else []
        with self._lock:
            self._months[ym] = (now, games)
        # A finished month is kept for good once it holds games; an empty answer could be a bad
        # response rather than an empty month, except in July and August, when there is no hockey.
        if not current and (games or ym[4:] in OFF_SEASON_MONTHS):
            self._write_disk(ym, games)
        return games

    def _season_year(self, today: date) -> int:
        try:
            year, _label, _start = _season_meta(self._fetch(SCOREBOARD_URL))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            year = 0
        return year or today.year + (1 if today.month >= 9 else 0)

    def _publish(self, now: float, today: date, year: int, batches: list[list[dict[str, Any]]], *, complete: bool) -> list[dict[str, Any]]:
        seen: set[str] = set()
        games: list[dict[str, Any]] = []
        for batch in batches:
            for g in batch:
                if g["id"] in seen or (g["day"] and g["day"] > today.isoformat()):
                    continue
                seen.add(g["id"])
                games.append(g)
        with self._lock:
            self._history = (now, today, year, games, complete)
        return games

    def refresh(self, *, today: date | None = None) -> list[dict[str, Any]]:
        """Rebuild: this season first (served as soon as it lands), then the seasons behind it."""
        today = today or datetime.now(ET).date()
        now = time.time()
        year = self._season_year(today)
        recent = _months(date(year - 1, 9, 1), today)
        older = _months(date(year - 1 - SEASONS_BACK, 9, 1), date(year - 1, 8, 31))
        total = len(recent) + len(older)
        done = 0
        batches: list[list[dict[str, Any]]] = []

        def load(months: list[str]) -> None:
            nonlocal done
            if not months:
                return
            with ThreadPoolExecutor(max_workers=min(self._workers, len(months))) as pool:
                for batch in pool.map(lambda ym: self._load_month(ym, now, today), months):
                    batches.append(batch)
                    done += 1
                    self._progress = (done, total)

        self._progress = (0, total)
        load(recent)
        with self._lock:
            first_load = self._history is None
        # Serve this season on its own only on the first load; a later refresh keeps the full history up meanwhile.
        if first_load or not older:
            self._publish(now, today, year, batches, complete=not older)
        load(older)
        return self._publish(now, today, year, batches, complete=True)

    def _refresh_in_background(self, today: date) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._safe_refresh, args=(today,), name="paths-refresh", daemon=True)
            self._worker.start()

    def _safe_refresh(self, today: date) -> None:
        try:
            self.refresh(today=today)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)

    def history(self, *, today: date | None = None, wait: bool = False) -> tuple[int, list[dict[str, Any]], bool] | None:
        """``(season_year, finals, complete)``; ``None`` only while nothing at all has loaded yet."""
        today = today or datetime.now(ET).date()
        now = time.time()
        with self._lock:
            cached = self._history
        fresh = cached is not None and cached[1] == today and cached[4] and now - cached[0] < HISTORY_TTL
        if fresh:
            return cached[2], cached[3], True
        if wait:
            games = self.refresh(today=today)
            with self._lock:
                year = self._history[2] if self._history else 0
            return year, games, True
        self._refresh_in_background(today)
        if cached is not None:
            return cached[2], cached[3], cached[4]
        return None

    def for_game(self, game: dict[str, Any], *, power: dict[str, Any] | None = None, today: date | None = None, wait: bool = False) -> dict[str, Any]:
        found = self.history(today=today, wait=wait)
        if found is None:
            done, total = self._progress
            return {"loading": True, "game": str(game.get("id") or ""), "months_done": done, "months_total": total, "error": self._last_error}
        year, games, complete = found
        out = build_paths(games, game, power=power, season_year=year)
        out["complete"] = complete
        out["error"] = self._last_error
        return out
