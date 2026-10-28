"""Power-play spans rebuilt from the penalties in a play-by-play.

The input is already parsed: penalties as ``{"side", "at", "units", "kind",
"label", "what", "when"}`` where ``units`` is the seconds of each part (a
double minor is two 120s units served back to back), and goals as
``{"side", "at", "pp"}`` where ``pp`` is the feed's own power-play flag
(``None`` when unknown).
The simulation follows the rules that move the manpower around:

* coincidental penalties of the same length cancel out;
* a team serves at most two penalties at a time, the rest wait their turn;
* a goal by the side with the advantage ends the other side's oldest minor;
* majors run their full five minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REGULATION_SECONDS = 3 * 20 * 60
MAX_SERVING = 2  # a team never drops below three skaters


@dataclass
class _Unit:
    uid: int
    side: str
    at: int
    length: int
    kind: str
    label: str
    what: str
    who: str
    when: str
    after: "_Unit | None" = None
    start: int | None = None
    end: int | None = None
    ended_by_goal: bool = False
    cancelled: bool = False
    opportunity: bool = False


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


def _units(penalties: list[dict[str, Any]]) -> list[_Unit]:
    out: list[_Unit] = []
    tail: dict[tuple[str, int, str], _Unit] = {}
    for uid, pen in enumerate(sorted(penalties, key=lambda p: int(p["at"]))):
        # One player given two penalties at a stoppage serves them one after the other.
        who = str(pen.get("who") or "")
        previous: _Unit | None = tail.get((str(pen["side"]), int(pen["at"]), who)) if who else None
        for length in pen.get("units") or []:
            unit = _Unit(
                uid=uid,
                side=str(pen["side"]),
                at=int(pen["at"]),
                length=int(length),
                kind=str(pen.get("kind") or "minor"),
                label=str(pen.get("label") or ""),
                what=str(pen.get("what") or pen.get("label") or ""),
                who=who,
                when=str(pen.get("when") or ""),
                after=previous,
            )
            out.append(unit)
            previous = unit
        if who and previous is not None:
            tail[(str(pen["side"]), int(pen["at"]), who)] = previous
    return out


def _cancel_coincidentals(units: list[_Unit]) -> None:
    by_time: dict[int, list[_Unit]] = {}
    for unit in units:
        by_time.setdefault(unit.at, []).append(unit)
    for group in by_time.values():
        for kind in ("minor", "major"):
            # Second halves of double minors go first so what remains starts on time.
            home = sorted((u for u in group if u.side == "home" and u.kind == kind), key=lambda u: u.after is None)
            away = sorted((u for u in group if u.side == "away" and u.kind == kind), key=lambda u: u.after is None)
            for a in away:
                # Matching infractions (roughing for roughing) cancel each other first.
                match = next((h for h in home if h.what == a.what), None) or (home[0] if home else None)
                if match is None:
                    break
                home.remove(match)
                match.cancelled = a.cancelled = True
    for unit in units:
        if unit.after is not None and unit.after.cancelled:
            unit.after = None


def _strength(side: str, serving: dict[str, int], at: int, *, playoffs: bool) -> str:
    other = _other(side)
    if at >= REGULATION_SECONDS and not playoffs:
        # Three-on-three overtime: a penalty adds a skater for the other side instead.
        edge = max(0, serving[other] - serving[side])
        return f"{min(5, 3 + edge)}-on-3"
    return f"{max(3, 5 - serving[side])}-on-{max(3, 5 - serving[other])}"


def _period_of(at: int, *, playoffs: bool) -> int:
    if at < REGULATION_SECONDS or playoffs:
        return at // 1200 + 1
    return 4


def power_plays(
    penalties: list[dict[str, Any]],
    goals: list[dict[str, Any]],
    *,
    end: int,
    live: bool,
    playoffs: bool = False,
) -> dict[str, Any]:
    """Advantage spans up to ``end`` seconds, plus a count, time and goals per side."""
    units = _units(penalties)
    _cancel_coincidentals(units)
    units = [u for u in units if not u.cancelled and u.at <= end]

    serving: dict[str, list[_Unit]] = {"home": [], "away": []}
    queue: dict[str, list[_Unit]] = {"home": [], "away": []}
    done: set[int] = set()
    intervals: list[tuple[int, int, dict[str, list[_Unit]]]] = []
    cursor = 0

    def change(t: int) -> None:
        nonlocal cursor
        if t > cursor:
            intervals.append((cursor, t, {"home": list(serving["home"]), "away": list(serving["away"])}))
            cursor = t

    def promote(side: str, t: int) -> None:
        for unit in list(queue[side]):
            if len(serving[side]) >= MAX_SERVING:
                return
            if unit.after is not None and id(unit.after) not in done:
                continue
            queue[side].remove(unit)
            unit.start = t
            unit.end = t + unit.length
            serving[side].append(unit)
            # The box score credits an opportunity only when the penalty puts the other side
            # ahead as it starts; each half of a double minor counts on its own.
            unit.opportunity = t < end and len(serving[side]) >= len(serving[_other(side)])

    def expire(until: int) -> None:
        while True:
            active = serving["home"] + serving["away"]
            soonest = min((u.end for u in active if u.end is not None), default=None)
            if soonest is None or soonest > until:
                return
            change(soonest)
            for side in ("home", "away"):
                for unit in list(serving[side]):
                    if unit.end is not None and unit.end <= soonest:
                        serving[side].remove(unit)
                        done.add(id(unit))
                promote(side, soonest)

    events: list[tuple[int, int, str, Any]] = []
    for unit in units:
        events.append((unit.at, 2, "penalty", unit))
    for goal in goals:
        # A goal the feed marks as not on the power play (``pp`` False) leaves the box alone.
        if goal.get("at") is not None and int(goal["at"]) <= end and goal.get("pp") is not False:
            events.append((int(goal["at"]), 1, "goal", str(goal.get("side") or "")))
    # A goal and a penalty at the same second: the goal came first (a challenge lost after a
    # goal is assessed at the goal's time), so it cannot end that penalty.
    events.sort(key=lambda e: (e[0], e[1]))

    for t, _, kind, payload in events:
        expire(t)
        change(t)
        if kind == "penalty":
            unit = payload
            queue[unit.side].append(unit)
            promote(unit.side, t)
        else:
            scorer = payload
            if scorer not in ("home", "away"):
                continue
            victim_side = _other(scorer)
            if len(serving[victim_side]) <= len(serving[scorer]):
                continue
            minors = [u for u in serving[victim_side] if u.kind == "minor"]
            if not minors:
                continue
            victim = min(minors, key=lambda u: (u.start or 0, u.uid))
            victim.end = t
            victim.ended_by_goal = True
            serving[victim_side].remove(victim)
            done.add(id(victim))
            promote(victim_side, t)
    expire(end)
    change(end)
    still_open = {id(u) for u in serving["home"] + serving["away"]} if live else set()

    spans: list[dict[str, Any]] = []
    for start, stop, who in intervals:
        counts = {"home": len(who["home"]), "away": len(who["away"])}
        if counts["home"] == counts["away"]:
            continue
        side = "home" if counts["home"] < counts["away"] else "away"
        against = who[_other(side)]
        segment = {"start": start, "end": stop, "strength": _strength(side, counts, start, playoffs=playoffs)}
        last = spans[-1] if spans else None
        if last and last["side"] == side and last["end"] == start:
            last["end"] = stop
            if last["segments"][-1]["strength"] == segment["strength"]:
                last["segments"][-1]["end"] = stop
            else:
                last["segments"].append(segment)
            for u in against:
                last["_units"].setdefault(u.uid, u)
            last["open"] = any(id(u) in still_open for u in against)
        else:
            spans.append(
                {
                    "side": side,
                    "start": start,
                    "end": stop,
                    "period": _period_of(start, playoffs=playoffs),
                    "segments": [segment],
                    "_units": {u.uid: u for u in against},
                    "open": any(id(u) in still_open for u in against),
                }
            )

    totals = {s: {"count": 0, "seconds": 0, "goals": 0, "spans": 0} for s in ("home", "away")}
    for unit in units:
        if unit.opportunity:
            totals[_other(unit.side)]["count"] += 1
    for span in spans:
        units_in = span.pop("_units")
        span["strength"] = max((seg["strength"] for seg in span["segments"]), key=lambda s: int(s.split("-")[0]) - int(s.split("-")[-1]))
        span["penalties"] = [
            {"label": u.label, "when": u.when, "kind": u.kind, "ended_by_goal": u.ended_by_goal}
            for _, u in sorted(units_in.items())
        ]
        span["goals"] = sum(
            1
            for g in goals
            if g.get("side") == span["side"]
            and g.get("at") is not None
            and g.get("pp") is not False
            and span["start"] < int(g["at"]) <= span["end"]
        )
        side = span["side"]
        totals[side]["spans"] += 1
        totals[side]["seconds"] += span["end"] - span["start"]
        totals[side]["goals"] += span["goals"]
    return {"spans": spans, "home": totals["home"], "away": totals["away"]}
