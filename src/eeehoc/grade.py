"""Live grade: how a team and its goalie are playing against their own usual.

eeesoc's live grade, on hockey's inputs. Every final this season is kept at
the second, so "usual" is not a per-game average scaled by the clock — it is
what this club had actually put up by the same point of its past games. A team
that normally has eleven shots on goal after one period and has five grades
low; the same five at the eight-minute mark barely moves it.

A team's grade blends eleven reads, each scored 0–100 as a percentile of what
the club usually has by now (50 = exactly its usual):

* attack — goals, shots on goal, shot attempts (missed and blocked included)
  and expected goals;
* power play — opportunities drawn, then shots on goal and goals for the
  power-play time it has had tonight, against its usual rate per minute;
* defence — shots on goal and expected goals allowed, and times shorthanded,
  where fewer than usual scores higher;
* the puck — share of faceoffs won.

Eleven percentiles averaged together bunch up around 50, so the blend is
spread back out to the scale of a single read (``PART_CORRELATION``): scores
run evenly from 1 to 99, and an A (75+) is about one game in four.

The goalie has his own number. The unblocked shots he has faced are worth so
many goals (``eeehoc.xg``); at his own season rate of goals allowed per
expected goal he would have let in ``expected`` of them. The grade is where
the goals he has actually allowed sit against that, 50 = his usual. Shots at
an empty net are not his.

Clubs and goalies with few games are pulled toward the league's average, so
the grade reads "against the league" until there is a history of their own.
"""

from __future__ import annotations

import math
import threading
from bisect import bisect_right
from dataclasses import dataclass, field
from itertools import accumulate
from statistics import NormalDist
from typing import Any

from eeehoc.live import (
    PERIOD_SECONDS,
    REGULATION_PERIODS,
    _chart_name,
    _game_end_seconds,
    _goalies,
    _int,
    _team_side,
    play_game_seconds,
    power_play_spans,
)
from eeehoc.xg import play_xg

GRADE_VERSION = 1
REGULATION_SECONDS = REGULATION_PERIODS * PERIOD_SECONDS
FULL_GAME = 10**9
# Before this the counts are too small to say anything about either side.
MIN_SECONDS = 5 * 60
# Most recent games that make up a club's "usual".
HISTORY_GAMES = 30
# League-average games mixed into every club's usual, so one odd game is not a baseline.
PRIOR_GAMES = 2.0
# Power-play seconds of league-average play mixed into a club's power-play rates (five opportunities).
PRIOR_PP_SECONDS = 600.0
# A power play has to have run this long before its shots say anything.
MIN_PP_SECONDS = 30
# Faceoffs taken before a share means anything.
MIN_FACEOFFS = 8
# Game-to-game spread of a club's faceoff share in points, before the luck of a small sample.
FACEOFF_SD = 4.0
# Variance of an expected-goals total per unit of expected goals (a few big chances carry it).
XG_VARIANCE = 0.18
# Expected goals of league-average goaltending mixed into a goalie's own rate (about three games).
PRIOR_GOALIE_XG = 8.0
PRIOR_GOALIE_SHOTS = 80.0
# How alike the reads are on a typical night. Sets how far the blend is spread back out.
PART_CORRELATION = 0.2
# A single read never counts for more than this far from the middle.
PERCENTILE_FLOOR = 0.02

LETTERS = ((75, "A"), (60, "B"), (40, "C"), (25, "D"), (0, "F"))

_NORMAL = NormalDist()


@dataclass(frozen=True)
class Part:
    key: str
    label: str
    group: str
    weight: float
    # count: events by now. xg: expected goals by now. rate: events for the power-play time so far.
    # share: faceoffs won of those taken.
    kind: str
    source: str
    # Read from the other side of the game, where fewer is better.
    against: bool = False
    # Variance over mean of the count around a club's own usual.
    dispersion: float = 1.0


PARTS: tuple[Part, ...] = (
    Part("goals", "Goals", "attack", 0.14, "count", "goals", dispersion=1.0),
    Part("sog", "On goal", "attack", 0.12, "count", "sog", dispersion=1.3),
    Part("shots", "Shot attempts", "attack", 0.08, "count", "shots", dispersion=1.9),
    Part("xg", "xG", "attack", 0.16, "xg", "xg"),
    Part("pp", "Power plays", "power play", 0.04, "count", "pp_starts", dispersion=1.0),
    Part("pp_sog", "PP shots", "power play", 0.05, "rate", "pp_sog", dispersion=1.3),
    Part("pp_goals", "PP goals", "power play", 0.05, "rate", "pp_goals", dispersion=1.0),
    Part("sog_against", "SOG allowed", "defence", 0.10, "count", "sog", against=True, dispersion=1.3),
    Part("xg_against", "xG allowed", "defence", 0.14, "xg", "xg", against=True),
    Part("short", "Times short", "defence", 0.04, "count", "pp_starts", against=True, dispersion=1.0),
    Part("faceoffs", "Faceoff %", "puck", 0.08, "share", "faceoffs_won"),
)


@dataclass(slots=True)
class GoalieGame:
    """One goalie's work in one game: when each shot on goal, goal and chance came."""

    goalie_id: str
    name: str
    event_id: str = ""
    start: str = ""
    faced: list[int] = field(default_factory=list)
    allowed: list[int] = field(default_factory=list)
    xg_at: list[int] = field(default_factory=list)
    xg_sum: list[float] = field(default_factory=list)
    last_seen: int = 0

    def shots(self, at: int) -> int:
        return bisect_right(self.faced, at)

    def goals(self, at: int) -> int:
        return bisect_right(self.allowed, at)

    def xg(self, at: int) -> float:
        index = bisect_right(self.xg_at, at)
        return self.xg_sum[index - 1] if index else 0.0


@dataclass(slots=True)
class SideGame:
    """One club's side of one game, kept as second lists so any cut is a bisect."""

    event_id: str
    start: str
    team_id: str
    goals: list[int] = field(default_factory=list)
    sog: list[int] = field(default_factory=list)
    shots: list[int] = field(default_factory=list)
    xg_at: list[int] = field(default_factory=list)
    xg_sum: list[float] = field(default_factory=list)
    pp_starts: list[int] = field(default_factory=list)
    pp_spans: list[tuple[int, int]] = field(default_factory=list)
    pp_sog: list[int] = field(default_factory=list)
    pp_goals: list[int] = field(default_factory=list)
    faceoffs_won: list[int] = field(default_factory=list)
    goalies: dict[str, GoalieGame] = field(default_factory=dict)
    other: "SideGame | None" = None

    def count(self, source: str, at: int) -> float:
        if source == "xg":
            index = bisect_right(self.xg_at, at)
            return self.xg_sum[index - 1] if index else 0.0
        return float(bisect_right(getattr(self, source), at))

    def pp_seconds(self, at: int) -> int:
        return sum(min(end, at) - start for start, end in self.pp_spans if start < at)

    def faceoffs(self, at: int) -> tuple[int, int]:
        """Draws won and draws taken through ``at``."""
        won = bisect_right(self.faceoffs_won, at)
        lost = bisect_right(self.other.faceoffs_won, at) if self.other is not None else 0
        return won, won + lost


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


def _goalie_on(play: dict[str, Any], kind: str, known: dict[str, str]) -> tuple[str, str] | None:
    """The goalie the play names: the saver on a shot, or the goalie beside a miss."""
    for person in play.get("participants") or []:
        athlete = person.get("athlete") or {}
        goalie_id = str(athlete.get("id") or "")
        if not goalie_id:
            continue
        role = str(person.get("type") or "")
        if (kind == "Shot" and role == "saver") or (kind == "Missed" and not role and goalie_id in known):
            return goalie_id, str(athlete.get("shortName") or athlete.get("displayName") or known.get(goalie_id) or "")
    return None


def game_sides(
    summary: dict[str, Any],
    home_id: str,
    away_id: str,
    *,
    playoffs: bool = False,
    ot_seconds: int = 300,
    end: int | None = None,
    live: bool = False,
    event_id: str = "",
    start: str = "",
) -> dict[str, SideGame] | None:
    """Both sides of a play-by-play as second lists. ``None`` when there are no plays."""
    plays = summary.get("plays") or []
    if not plays:
        return None
    ids = {"home": str(home_id), "away": str(away_id)}
    sides = {side: SideGame(event_id=event_id, start=start, team_id=ids[side]) for side in ("home", "away")}
    sides["home"].other, sides["away"].other = sides["away"], sides["home"]
    known = {side: {g["id"]: g["name"] for g in rows if g.get("id")} for side, rows in _goalies(summary, home_id, away_id).items()}
    in_net: dict[str, GoalieGame | None] = {"home": None, "away": None}
    # Chances that came before the play-by-play had named the goalie.
    waiting: dict[str, list[tuple[int, bool, bool, float | None]]] = {"home": [], "away": []}
    chances: dict[str, list[tuple[int, float]]] = {"home": [], "away": []}
    faced_xg: dict[str, list[tuple[int, float]]] = {}
    latest = 0

    def credit(goalie: GoalieGame, at: int, on_goal: bool, goal: bool, value: float | None) -> None:
        if on_goal:
            goalie.faced.append(at)
        if goal:
            goalie.allowed.append(at)
        if value is not None:
            faced_xg.setdefault(goalie.goalie_id, []).append((at, value))
        goalie.last_seen = max(goalie.last_seen, at)

    for play in plays:
        kind = str((play.get("type") or {}).get("text") or "")
        if kind not in ("Shot", "Goal", "Missed", "Blocked", "Face Off"):
            continue
        period = _int((play.get("period") or {}).get("number"))
        clock = str((play.get("clock") or {}).get("displayValue") or "")
        at = play_game_seconds(clock, period, playoffs=playoffs, ot_seconds=ot_seconds)
        side = _team_side((play.get("team") or {}).get("id"), home_id, away_id)
        if at is None or side is None:
            continue
        latest = max(latest, at)
        if kind == "Face Off":
            sides[side].faceoffs_won.append(at)
            continue
        if kind == "Blocked":
            # The block is credited to the blocker, so the attempt belongs to the other side.
            sides[_other(side)].shots.append(at)
            continue
        me = sides[side]
        defending = _other(side)
        strength = str((play.get("strength") or {}).get("abbreviation") or "")
        on_goal = kind in ("Shot", "Goal")
        goal = kind == "Goal"
        me.shots.append(at)
        if on_goal:
            me.sog.append(at)
        if goal:
            me.goals.append(at)
        if strength == "power-play" and on_goal:
            me.pp_sog.append(at)
            if goal:
                me.pp_goals.append(at)
        value = play_xg(play)
        if value is not None:
            chances[side].append((at, value))
        if strength == "empty-net":
            continue
        named = _goalie_on(play, kind, known[defending])
        if named is not None:
            goalie = sides[defending].goalies.get(named[0])
            if goalie is None:
                goalie = GoalieGame(named[0], _chart_name(named[1]) or named[1], event_id=event_id, start=start)
                sides[defending].goalies[named[0]] = goalie
            if in_net[defending] is None:
                for early in waiting[defending]:
                    credit(goalie, *early)
                waiting[defending] = []
            in_net[defending] = goalie
        current = in_net[defending]
        if current is None:
            waiting[defending].append((at, on_goal, goal, value))
        else:
            credit(current, at, on_goal, goal, value)

    for side in ("home", "away"):
        # A goalie who never made a save is only known from the box score.
        if waiting[side] and len(known[side]) == 1:
            goalie_id, name = next(iter(known[side].items()))
            goalie = sides[side].goalies.setdefault(
                goalie_id, GoalieGame(goalie_id, _chart_name(name) or name, event_id=event_id, start=start)
            )
            for early in waiting[side]:
                credit(goalie, *early)
        me = sides[side]
        for key in ("goals", "sog", "shots", "pp_sog", "pp_goals", "faceoffs_won"):
            getattr(me, key).sort()
        chances[side].sort()
        me.xg_at = [at for at, _ in chances[side]]
        me.xg_sum = [round(v, 4) for v in accumulate(v for _, v in chances[side])]
        for goalie in me.goalies.values():
            goalie.faced.sort()
            goalie.allowed.sort()
            points = sorted(faced_xg.get(goalie.goalie_id, []))
            goalie.xg_at = [at for at, _ in points]
            goalie.xg_sum = [round(v, 4) for v in accumulate(v for _, v in points)]

    stop = max(latest, REGULATION_SECONDS) if end is None else end
    advantage = power_play_spans(plays, home_id, away_id, end=stop, live=live, playoffs=playoffs, ot_seconds=ot_seconds)
    for side in ("home", "away"):
        sides[side].pp_spans = [(int(s["start"]), int(s["end"])) for s in advantage["spans"] if s["side"] == side]
        sides[side].pp_starts = list(advantage["starts"][side])
    return sides


class GradeIndex:
    """Every graded final by club and by goalie, with the league's averages memoised per second."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.teams: dict[str, list[SideGame]] = {}
        self.goalies: dict[str, list[GoalieGame]] = {}
        self.sides: list[SideGame] = []
        self._means: dict[tuple[str, int], float | None] = {}
        self._rates: dict[str, float | None] = {}
        for rec in records:
            timeline = rec.get("timeline")
            if not timeline:
                continue
            for side in ("home", "away"):
                game = timeline[side]
                self.sides.append(game)
                if game.team_id:
                    self.teams.setdefault(game.team_id, []).append(game)
                for goalie in game.goalies.values():
                    self.goalies.setdefault(goalie.goalie_id, []).append(goalie)
        shots = sum(len(g.faced) for games in self.goalies.values() for g in games)
        goals = sum(len(g.allowed) for games in self.goalies.values() for g in games)
        chances = sum(g.xg(FULL_GAME) for games in self.goalies.values() for g in games)
        # Goals a goalie lets in per expected goal, and per shot on goal, across the league.
        self.goals_per_xg = goals / chances if chances > 0 else 1.0
        self.goals_per_shot = goals / shots if shots > 0 else 0.1

    def history(self, team_id: str, *, event_id: str, start: str) -> list[SideGame]:
        """A club's most recent games before this one."""
        games = [
            g
            for g in self.teams.get(str(team_id), [])
            if g.event_id != str(event_id or "") and not (start and g.start and g.start >= start)
        ]
        games.sort(key=lambda g: g.start, reverse=True)
        return games[:HISTORY_GAMES]

    def goalie_history(self, goalie_id: str, *, event_id: str, start: str) -> list[GoalieGame]:
        return [
            g
            for g in self.goalies.get(str(goalie_id), [])
            if g.event_id != str(event_id or "") and not (start and g.start and g.start >= start)
        ]

    def league_mean(self, source: str, at: int) -> float | None:
        """Average of a count through ``at`` over every side of every game."""
        key = (source, at)
        if key not in self._means:
            values = [g.count(source, at) for g in self.sides]
            self._means[key] = sum(values) / len(values) if values else None
        return self._means[key]

    def league_rate(self, source: str) -> float | None:
        """Power-play events per power-play second across the league."""
        if source not in self._rates:
            seconds = sum(g.pp_seconds(FULL_GAME) for g in self.sides)
            events = sum(g.count(source, FULL_GAME) for g in self.sides)
            self._rates[source] = events / seconds if seconds > 0 else None
        return self._rates[source]


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def count_percentile(actual: float, usual: float, dispersion: float = 1.0) -> float:
    """
    Where ``actual`` sits among the counts a side with mean ``usual`` produces.

    Mid-percentile of a Poisson (``dispersion`` ≤ 1) or a negative binomial
    with variance ``dispersion × usual``: half the weight of the observed
    count goes below it, so a side exactly at its usual lands near 0.5.
    """
    k = max(0, round(actual))
    if usual <= 1e-9:
        return 0.5 if k == 0 else 1.0
    if dispersion <= 1.0 + 1e-9:
        # Poisson: each count is the one before times usual / k.
        p, size, ratio = math.exp(-usual), 0.0, usual
    else:
        keep = 1.0 / dispersion
        size = usual / (dispersion - 1.0)
        p, ratio = keep**size, 1.0 - keep
    below = 0.0
    for i in range(k):
        below += p
        p *= (ratio if size == 0.0 else (i + size) * ratio) / (i + 1)
    return max(0.0, min(1.0, below + 0.5 * p))


def letter_for(score: float) -> str:
    return next(letter for floor, letter in LETTERS if score >= floor)


def _usual_count(index: GradeIndex, games: list[SideGame], part: Part, at: int) -> tuple[float, int] | None:
    """What the club usually has by ``at``, mixed with ``PRIOR_GAMES`` league-average games."""
    prior = index.league_mean(part.source, at)
    if not games:
        return (prior, 0) if prior is not None else None
    own = sum((g.other if part.against else g).count(part.source, at) for g in games) / len(games)
    if prior is None:
        return own, len(games)
    return (len(games) * own + PRIOR_GAMES * prior) / (len(games) + PRIOR_GAMES), len(games)


def _usual_rate(index: GradeIndex, games: list[SideGame], part: Part) -> float | None:
    """The club's power-play events per power-play second, pulled toward the league's."""
    prior = index.league_rate(part.source)
    seconds = sum(g.pp_seconds(FULL_GAME) for g in games)
    events = sum(g.count(part.source, FULL_GAME) for g in games)
    if prior is None:
        return events / seconds if seconds > 0 else None
    return (events + PRIOR_PP_SECONDS * prior) / (seconds + PRIOR_PP_SECONDS)


def _usual_share(games: list[SideGame], at: int) -> tuple[float, int]:
    """The club's usual faceoff share by ``at``. The league's is 50 by construction."""
    shares = []
    for g in games:
        won, taken = g.faceoffs(at)
        if taken >= MIN_FACEOFFS:
            shares.append(100.0 * won / taken)
    n = len(shares)
    return (sum(shares) + PRIOR_GAMES * 50.0) / (n + PRIOR_GAMES), n


def _read(index: GradeIndex, live: SideGame, games: list[SideGame], part: Part, at: int) -> dict[str, Any] | None:
    """One read: tonight's number, the usual, and where tonight sits against it (0–1, higher is better)."""
    now = live.other if part.against else live
    if now is None:
        return None
    if part.kind == "share":
        won, taken = live.faceoffs(FULL_GAME)
        if taken < MIN_FACEOFFS:
            return None
        usual, n = _usual_share(games, at)
        actual = 100.0 * won / taken
        sd = math.sqrt(FACEOFF_SD**2 + 2500.0 / taken)
        return {"actual": round(actual), "usual": round(usual), "pct": _normal_cdf((actual - usual) / sd), "games": n}
    actual = now.count(part.source, FULL_GAME)
    if part.kind == "rate":
        seconds = live.pp_seconds(FULL_GAME)
        rate = _usual_rate(index, games, part)
        if seconds < MIN_PP_SECONDS or rate is None:
            return None
        usual, n = rate * seconds, len(games)
        pct = count_percentile(actual, usual, part.dispersion)
        return {"actual": int(actual), "usual": round(usual, 1), "pct": pct, "games": n}
    base = _usual_count(index, games, part, at)
    if base is None:
        return None
    usual, n = base
    if part.kind == "xg":
        pct = _normal_cdf((actual - usual) / math.sqrt(XG_VARIANCE * max(usual, 0.0) + 0.01))
        out = {"actual": round(actual, 2), "usual": round(usual, 2), "pct": pct, "games": n}
    else:
        out = {"actual": int(actual), "usual": round(usual, 1), "pct": count_percentile(actual, usual, part.dispersion), "games": n}
    if part.against:
        out["pct"] = 1.0 - out["pct"]
    return out


def blend(reads: list[tuple[float, float]]) -> float:
    """
    One 0–100 number from ``(percentile, weight)`` reads.

    Each percentile becomes a z-score, the weighted mean is put back on the
    scale of a single read (the reads share ``PART_CORRELATION``), and that
    goes back to a percentile.
    """
    total = sum(weight for _, weight in reads)
    if total <= 0:
        return 50.0
    z = 0.0
    squares = 0.0
    for pct, weight in reads:
        share = weight / total
        z += share * _NORMAL.inv_cdf(max(PERCENTILE_FLOOR, min(1.0 - PERCENTILE_FLOOR, pct)))
        squares += share * share
    spread = math.sqrt(squares + PART_CORRELATION * (1.0 - squares))
    return 100.0 * _normal_cdf(z / spread)


def grade_side(index: GradeIndex, live: SideGame, games: list[SideGame], *, at: int) -> dict[str, Any] | None:
    """One club's grade at ``at`` seconds: the blended score, its letter and every read behind it."""
    parts: list[dict[str, Any]] = []
    reads: list[tuple[float, float]] = []
    for part in PARTS:
        read = _read(index, live, games, part, at)
        if read is None:
            continue
        pct = read.pop("pct")
        reads.append((pct, part.weight))
        parts.append(
            {
                "key": part.key,
                "label": part.label,
                "group": part.group,
                "lower": part.against,
                "score": max(1, min(99, round(100 * pct))),
                "weight": part.weight,
                **read,
            }
        )
    if not parts:
        return None
    score = max(1, min(99, round(blend(reads))))
    return {
        "score": score,
        "letter": letter_for(score),
        "games": len(games),
        "basis": "club" if games else "league",
        "pp_seconds": live.pp_seconds(FULL_GAME),
        "parts": parts,
    }


def grade_goalie(index: GradeIndex, live: GoalieGame, games: list[GoalieGame], *, in_net: bool) -> dict[str, Any] | None:
    """A goalie's grade: the goals he has allowed against what the chances were worth at his usual."""
    shots, goals, chances = live.shots(FULL_GAME), live.goals(FULL_GAME), live.xg(FULL_GAME)
    if not shots and not goals and chances <= 0:
        return None
    past_shots = sum(g.shots(FULL_GAME) for g in games)
    past_goals = sum(g.goals(FULL_GAME) for g in games)
    past_chances = sum(g.xg(FULL_GAME) for g in games)
    if chances > 0:
        rate = (past_goals + PRIOR_GOALIE_XG * index.goals_per_xg) / (past_chances + PRIOR_GOALIE_XG)
        expected = chances * rate
    else:
        # No shot locations in the feed: fall back on how many shots he has seen.
        rate = (past_goals + PRIOR_GOALIE_SHOTS * index.goals_per_shot) / (past_shots + PRIOR_GOALIE_SHOTS)
        expected = shots * rate
    score = max(1, min(99, round(100 * (1.0 - count_percentile(goals, expected)))))
    return {
        "id": live.goalie_id,
        "name": live.name,
        "in_net": in_net,
        "score": score,
        "letter": letter_for(score),
        "games": len(games),
        "basis": "goalie" if games else "league",
        "shots": shots,
        "saves": shots - goals,
        "goals": goals,
        "xg": round(chances, 2),
        "expected": round(expected, 2),
        "saved_above": round(expected - goals, 2),
        "save_pct": round((shots - goals) / shots, 3) if shots else None,
        "usual_save_pct": round((past_shots - past_goals) / past_shots, 3) if past_shots else None,
    }


def grade_game(game: dict[str, Any], summary: dict[str, Any] | None, index: GradeIndex) -> dict[str, Any] | None:
    """Both sides' grades for a game in progress or just finished; ``None`` before there is anything to grade."""
    if game.get("state") not in ("in", "post") or not summary:
        return None
    plays = summary.get("plays") or []
    if not plays:
        return None
    final = game["state"] == "post"
    end = _game_end_seconds(game, plays)
    if not final and end < MIN_SECONDS:
        return None
    home_id, away_id = str(game["home"]["id"]), str(game["away"]["id"])
    event_id, start = str(game.get("id") or ""), str(game.get("date") or "")
    sides = game_sides(
        summary,
        home_id,
        away_id,
        playoffs=bool(game.get("playoffs")),
        ot_seconds=_int(game.get("ot_seconds"), 300),
        end=end,
        live=not final,
        event_id=event_id,
        start=start,
    )
    if sides is None:
        return None
    at = FULL_GAME if final else end
    out: dict[str, Any] = {"version": GRADE_VERSION, "at": end, "final": final}
    for side in ("home", "away"):
        live = sides[side]
        team = grade_side(index, live, index.history(live.team_id, event_id=event_id, start=start), at=at)
        last = max(live.goalies.values(), key=lambda g: g.last_seen, default=None)
        tenders = []
        for goalie in sorted(live.goalies.values(), key=lambda g: (g is not last, -len(g.faced))):
            graded = grade_goalie(
                index,
                goalie,
                index.goalie_history(goalie.goalie_id, event_id=event_id, start=start),
                in_net=goalie is last,
            )
            if graded is not None:
                tenders.append(graded)
        out[side] = {"team": team, "goalies": tenders}
    if not any(out[side]["team"] or out[side]["goalies"] for side in ("home", "away")):
        return None
    return out


class GradeBoard:
    """Grades for every game on a live board, against the finals a ``PowerFeed`` has collected."""

    def __init__(self, live: Any, power: Any) -> None:
        self._live = live
        self._power = power
        self._lock = threading.Lock()
        self._index: tuple[tuple[str, ...], GradeIndex] | None = None

    def _index_for(self, records: list[dict[str, Any]]) -> GradeIndex:
        key = tuple(sorted(str(rec.get("id") or "") for rec in records))
        with self._lock:
            if self._index is not None and self._index[0] == key:
                return self._index[1]
        index = GradeIndex(records)
        with self._lock:
            self._index = (key, index)
        return index

    def get(self, *, dates: str | None = None) -> dict[str, Any]:
        board = self._live.get(dates=dates)
        records = self._power.records()
        index = self._index_for(records)
        games: dict[str, Any] = {}
        # Without the season there is no usual to grade against yet.
        for game in (board.get("games") or []) if records else []:
            if game.get("state") not in ("in", "post"):
                continue
            try:
                graded = grade_game(game, self._live.cached_summary(game["id"]), index)
            except Exception:  # noqa: BLE001 - a grade must never take the board down
                graded = None
            if graded is not None:
                games[str(game["id"])] = graded
        return {
            "version": GRADE_VERSION,
            "loading": not records,
            "history_games": len(index.sides) // 2,
            "min_seconds": MIN_SECONDS,
            "weights": {part.key: part.weight for part in PARTS},
            "games": games,
        }
