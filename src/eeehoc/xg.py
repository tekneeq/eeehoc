"""Expected goals for a shot from where it was taken and how.

ESPN's NHL play-by-play carries a rink coordinate for every unblocked shot
(feet from centre ice, the nets at x = ±89), the shot type in the play text,
and the strength. A logistic model fitted on this season's regular-season
shots turns those into a probability of a goal. It was fitted so the league's
expected goals equal its actual goals — 100 xG is 100 goals' worth of chances.

Blocked shots are left out (ESPN logs where the block happened, not the
shot). Shots at an empty net are not chances on a goalie and score nothing.
A penalty shot is a fixed ``PENALTY_SHOT_XG``.
"""

from __future__ import annotations

import math
from typing import Any

GOAL_X = 89.0

# Fitted 2026-10-19 on 11,241 unblocked regular-season shots (791 goals).
INTERCEPT = -0.7016
PER_FOOT = -0.0522
PER_RADIAN = -0.9696
STRENGTH = {"power-play": 0.2558, "short-handed": 0.3076}
SHOT_TYPE = {
    "wrist": 0.0,
    "snap": 0.3338,
    "slap": 0.3796,
    "backhand": -0.4261,
    "tip": -0.8652,
    "deflect": -0.3444,
    "wrap": -1.1378,
}
PENALTY_SHOT_XG = 0.33

MODEL_NOTE = (
    "Expected goals from shot distance, angle, type and strength, fitted on this season's "
    "unblocked shots so league xG equals league goals. Blocked shots and empty-net shots are left out."
)


def shot_type(text: str) -> str:
    """``"Tom Wilson Snap Shot saved by ..."`` -> ``"snap"``."""
    lower = (text or "").lower()
    for key in ("wrist", "snap", "slap", "backhand", "tip", "deflect", "wrap"):
        if key in lower:
            return key
    return "wrist"


def shot_geometry(x: float, y: float) -> tuple[float, float]:
    """Distance (feet) and angle (radians off the centre line) to the nearer net."""
    goal_x = GOAL_X if x >= 0 else -GOAL_X
    dx = abs(goal_x - x)
    return math.hypot(dx, y), math.atan2(abs(y), max(dx, 0.01))


def shot_xg(x: float, y: float, kind: str = "wrist", strength: str = "even-strength") -> float | None:
    """Goal probability for one unblocked shot. ``None`` for a shot at an empty net."""
    strength = (strength or "").lower()
    if strength == "empty-net":
        return None
    if strength == "penalty-shot":
        return PENALTY_SHOT_XG
    distance, angle = shot_geometry(x, y)
    z = INTERCEPT + PER_FOOT * distance + PER_RADIAN * angle
    z += STRENGTH.get(strength, 0.0) + SHOT_TYPE.get(kind, 0.0)
    return 1.0 / (1.0 + math.exp(-z))


def play_xg(play: dict[str, Any]) -> float | None:
    """xG for an ESPN play, or ``None`` when it is not an unblocked shot with a location."""
    kind = str((play.get("type") or {}).get("text") or "")
    if kind not in ("Shot", "Missed", "Goal"):
        return None
    coord = play.get("coordinate") or {}
    try:
        x, y = float(coord.get("x")), float(coord.get("y"))
    except (TypeError, ValueError):
        return None
    strength = str((play.get("strength") or {}).get("abbreviation") or "even-strength")
    return shot_xg(x, y, shot_type(str(play.get("text") or "")), strength)


def game_xg(plays: list[dict[str, Any]], home_id: str, away_id: str, *, playoffs: bool = False) -> dict[str, dict[str, float | int]]:
    """Each side's xG, unblocked attempts and goals from the play-by-play, shootout left out."""
    out = {
        "home": {"xg": 0.0, "attempts": 0, "goals": 0, "located": 0},
        "away": {"xg": 0.0, "attempts": 0, "goals": 0, "located": 0},
    }
    for play in plays or []:
        kind = str((play.get("type") or {}).get("text") or "")
        if kind not in ("Shot", "Missed", "Goal"):
            continue
        period = play.get("period") or {}
        number = int(period.get("number") or 0) if isinstance(period, dict) else int(period or 0)
        if number < 1 or (not playoffs and number >= 5):
            continue
        team_id = str((play.get("team") or {}).get("id") or "")
        side = "home" if team_id == str(home_id) else "away" if team_id == str(away_id) else None
        if side is None:
            continue
        row = out[side]
        row["attempts"] += 1
        if kind == "Goal":
            row["goals"] += 1
        value = play_xg(play)
        if value is not None:
            row["xg"] += value
            row["located"] += 1
    for row in out.values():
        row["xg"] = round(row["xg"], 2)
    return out
