"""Offence and defence power, special teams, and expected goals for every team.

Built from this season's finals: each game's play-by-play gives every
unblocked shot (for ``eeehoc.xg``) and the goals; the box score gives the
power-play goals and opportunities. The same construction as eeesoc's
clinical module, on hockey's inputs.

* ``offense_power`` blends the club's per-game xG created, shots on goal,
  unblocked attempts and goals, each over the league's rate and shrunk toward
  par over ``PRIOR_GAMES`` games, with ``OFFENSE_WEIGHTS``. ``PAR_POWER`` (50)
  is league average; 65 creates about 30% more than the league. ``potent``
  is True above par, otherwise the attack is blunt.
* ``defense_power`` is the league's xG allowed per game over the club's
  (shrunk), on the same scale — 50 allows the league's chances, 63 allows a
  fifth less. ``solid`` is True above par, otherwise leaky.
* ``scoring_power`` / ``stopping_power`` are the same two indexes on actual
  goals instead of chances, so the chiclet can read expected goals from
  results as well as from chances.
* ``clinical`` is goals per 100 xG (100 = scores what its chances are worth).
* ``pp`` is the power play: goals, opportunities, conversion, and the share
  of the club's goals that came on the power play. ``pk`` is the penalty
  kill: power-play goals allowed, times shorthanded, and the kill rate.
* ``lines`` is where the goals come from: how many (and what share) the club
  scored from each forward line L1–L4 and defence pair D1–D3, and how many it
  allowed while each was on the ice, from ``eeehoc.lines``.
* ``first_period`` sorts the club's games by its first-period shots on goal
  (``P1_BUCKETS``) and says how many goals it finished those games with.
* Ranks are 1 = best among clubs with at least ``MIN_GAMES`` finals.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

from eeehoc.lines import LINE_COUNT, LINES_NOTE, PAIR_COUNT, LineSource
from eeehoc.live import PLAYOFF_TYPE, SCOREBOARD_URL, SUMMARY_URL, FetchJson, _int, fetch_json, period_shot_lines
from eeehoc.periods import ET, PRESEASON_TYPE, REGULAR_TYPE, _game_day, _months, _season_meta
from eeehoc.xg import MODEL_NOTE, game_xg

PAR_POWER = 50
PRIOR_GAMES = 2.0
PRIOR_XG = 2.0
MIN_GAMES = 2
OFFENSE_WEIGHTS = {"xg": 0.40, "sog": 0.20, "attempts": 0.15, "goals": 0.25}

# First-period shots on goal, lower bound inclusive; None = no upper bound.
P1_BUCKETS: list[tuple[int, int | None]] = [(0, 4), (5, 9), (10, 14), (15, 19), (20, None)]

CURRENT_MONTH_TTL = 90.0
PAST_MONTH_TTL = 6 * 60 * 60.0
RESULT_TTL = 120.0
MISS_TTL = 10 * 60.0

BOX_KEYS = {
    "powerPlayGoals": "ppg",
    "powerPlayOpportunities": "ppo",
    "shortHandedGoals": "shg",
    "shotsTotal": "sog",
}


def _box_stats(summary: dict[str, Any], home_id: str, away_id: str) -> dict[str, dict[str, int]]:
    out = {"home": {}, "away": {}}
    for team_box in (summary.get("boxscore") or {}).get("teams") or []:
        team_id = str((team_box.get("team") or {}).get("id") or "")
        side = "home" if team_id == home_id else "away" if team_id == away_id else None
        if side is None:
            continue
        for stat in team_box.get("statistics") or []:
            key = BOX_KEYS.get(str(stat.get("name") or ""))
            if key:
                out[side][key] = _int(stat.get("displayValue", stat.get("value")))
    return out


def game_record(summary: dict[str, Any], *, event_id: str = "") -> dict[str, Any] | None:
    """One final, both sides: goals, shots, xG and special teams. ``None`` until it is final."""
    header = summary.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    stype = (comp.get("status") or {}).get("type") or {}
    if str(stype.get("state") or "") != "post":
        return None
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    home_id = str((home.get("team") or {}).get("id") or "")
    away_id = str((away.get("team") or {}).get("id") or "")
    season = header.get("season") or {}
    season_type = _int(season.get("type"))
    playoffs = season_type == PLAYOFF_TYPE
    plays = summary.get("plays") or []
    shots = game_xg(plays, home_id, away_id, playoffs=playoffs) if plays else None
    first = period_shot_lines(plays, home_id, away_id, periods=1, playoffs=playoffs)[:1] if plays else []
    box = _box_stats(summary, home_id, away_id)
    iso = str(comp.get("date") or "")
    day = _game_day(iso)
    sides = {}
    for side, raw in (("home", home), ("away", away)):
        played = shots[side] if shots else None
        sides[side] = {
            "id": home_id if side == "home" else away_id,
            "abbr": str((raw.get("team") or {}).get("abbreviation") or ""),
            # Goals from the plays leave the shootout out; the header score counts it.
            "goals": played["goals"] if played else _int(raw.get("score")),
            "sog": box[side].get("sog"),
            "attempts": played["attempts"] if played else None,
            "xg": played["xg"] if played else None,
            "ppg": box[side].get("ppg"),
            "ppo": box[side].get("ppo"),
            "shg": box[side].get("shg"),
            "p1_sog": first[0][f"{side}_sog"] if first else None,
        }
    return {
        "id": str(header.get("id") or comp.get("id") or event_id),
        "date": iso,
        "day": day.isoformat() if day else "",
        "season_year": _int(season.get("year")),
        "season_type": season_type,
        "plays": bool(plays),
        "home": sides["home"],
        "away": sides["away"],
        "lines": None,
    }


def p1_bucket(sog: int) -> int:
    for index, (low, high) in enumerate(P1_BUCKETS):
        if sog >= low and (high is None or sog <= high):
            return index
    return len(P1_BUCKETS) - 1


def p1_bucket_label(index: int) -> str:
    low, high = P1_BUCKETS[index]
    return f"{low}+" if high is None else f"{low}–{high}"


def _blank_buckets() -> list[dict[str, int]]:
    return [{"games": 0, "goals": 0, "wins": 0} for _ in P1_BUCKETS]


def _blank_units() -> dict[str, Any]:
    return {"line": [0] * LINE_COUNT, "pair": [0] * PAIR_COUNT, "line_none": 0, "pair_none": 0, "total": 0}


def _add_units(into: dict[str, Any], more: dict[str, Any]) -> None:
    for kind in ("line", "pair"):
        counts = more.get(kind) or []
        for index, value in enumerate(counts[: len(into[kind])]):
            into[kind][index] += _int(value)
        into[f"{kind}_none"] += _int(more.get(f"{kind}_none"))
    into["total"] += _int(more.get("total"))


def _blank(team_id: str, abbr: str) -> dict[str, Any]:
    return {
        "id": team_id,
        "abbr": abbr,
        "games": 0,
        "games_xg": 0,
        "gf": 0,
        "ga": 0,
        "gf_xg": 0,
        "sog_f": 0,
        "sog_a": 0,
        "games_sog": 0,
        "att_f": 0,
        "att_a": 0,
        "xg_f": 0.0,
        "xg_a": 0.0,
        "ppg": 0,
        "ppo": 0,
        "ppga": 0,
        "tsh": 0,
        "shg_f": 0,
        "shg_a": 0,
        "games_pp": 0,
        "games_lines": 0,
        "lines_for": _blank_units(),
        "lines_against": _blank_units(),
        "games_p1": 0,
        "p1": _blank_buckets(),
    }


def _accumulate(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    teams: dict[str, dict[str, Any]] = {}
    for rec in records:
        for side, other in (("home", "away"), ("away", "home")):
            me, them = rec[side], rec[other]
            if not me["id"]:
                continue
            row = teams.setdefault(me["id"], _blank(me["id"], me["abbr"]))
            row["abbr"] = me["abbr"] or row["abbr"]
            row["games"] += 1
            row["gf"] += me["goals"]
            row["ga"] += them["goals"]
            if rec["plays"] and me["xg"] is not None and them["xg"] is not None:
                row["games_xg"] += 1
                row["gf_xg"] += me["goals"]
                row["xg_f"] += me["xg"]
                row["xg_a"] += them["xg"]
                row["att_f"] += me["attempts"] or 0
                row["att_a"] += them["attempts"] or 0
            if me["sog"] is not None and them["sog"] is not None:
                row["games_sog"] += 1
                row["sog_f"] += me["sog"]
                row["sog_a"] += them["sog"]
            if me["ppg"] is not None and them["ppg"] is not None:
                row["games_pp"] += 1
                row["ppg"] += me["ppg"]
                row["ppo"] += me["ppo"] or 0
                row["ppga"] += them["ppg"]
                row["tsh"] += them["ppo"] or 0
                row["shg_f"] += me["shg"] or 0
                row["shg_a"] += them["shg"] or 0
            lines = rec.get("lines")
            if lines and isinstance(lines.get(side), dict):
                row["games_lines"] += 1
                _add_units(row["lines_for"], lines[side].get("for") or {})
                _add_units(row["lines_against"], lines[side].get("against") or {})
            if me.get("p1_sog") is not None:
                row["games_p1"] += 1
                bucket = row["p1"][p1_bucket(me["p1_sog"])]
                bucket["games"] += 1
                bucket["goals"] += me["goals"]
                bucket["wins"] += 1 if me["goals"] > them["goals"] else 0
    return teams


def _units_out(units: dict[str, Any]) -> dict[str, Any]:
    total = units["total"]
    return {
        "total": total,
        "line": list(units["line"]),
        "line_pct": [_pct(n, total, 0) for n in units["line"]],
        "line_none": units["line_none"],
        "line_none_pct": _pct(units["line_none"], total, 0),
        "pair": list(units["pair"]),
        "pair_pct": [_pct(n, total, 0) for n in units["pair"]],
        "pair_none": units["pair_none"],
        "pair_none_pct": _pct(units["pair_none"], total, 0),
    }


def _buckets_out(buckets: list[dict[str, int]]) -> list[dict[str, Any]]:
    return [
        {
            "label": p1_bucket_label(index),
            "low": P1_BUCKETS[index][0],
            "high": P1_BUCKETS[index][1],
            "games": b["games"],
            "goals": b["goals"],
            "wins": b["wins"],
            "avg": _per_game(b["goals"], b["games"]),
        }
        for index, b in enumerate(buckets)
    ]


def _shrunk(total: float, games: float, par: float) -> float:
    return (total + PRIOR_GAMES * par) / (games + PRIOR_GAMES)


def _published(value_100: float) -> int:
    return int(round(value_100 * PAR_POWER / 100.0))


def _pct(num: float, den: float, digits: int = 1) -> float | None:
    if den <= 0:
        return None
    return round(100.0 * num / den, digits)


def _per_game(total: float, games: int, digits: int = 2) -> float | None:
    if games <= 0:
        return None
    return round(total / games, digits)


def _rank(rows: list[dict[str, Any]], key: str, out: str, *, ranked: set[str]) -> None:
    """1 = highest ``key`` among ``ranked`` clubs, ties broken by abbreviation."""
    eligible = [r for r in rows if r["id"] in ranked and r.get(key) is not None]
    eligible.sort(key=lambda r: (-r[key], r["abbr"]))
    for index, row in enumerate(eligible, start=1):
        row[out] = index
    for row in rows:
        row.setdefault(out, None)


def build_power(records: list[dict[str, Any]], *, today: date, season_year: int, season_label: str) -> dict[str, Any]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for rec in sorted(records, key=lambda r: (r["date"], r["id"])):
        if rec["id"] in seen or (rec["day"] and rec["day"] > today.isoformat()):
            continue
        seen.add(rec["id"])
        unique.append(rec)
    this_season = [r for r in unique if r["season_year"] == season_year]
    regular = [r for r in this_season if r["season_type"] in (REGULAR_TYPE, PLAYOFF_TYPE)]
    season_rows, season_kind = (regular, "regular season") if regular else (
        [r for r in this_season if r["season_type"] == PRESEASON_TYPE],
        "preseason",
    )

    teams = _accumulate(season_rows)
    rows = list(teams.values())
    team_games = sum(r["games"] for r in rows)
    xg_games = sum(r["games_xg"] for r in rows)
    sog_games = sum(r["games_sog"] for r in rows)
    pp_games = sum(r["games_pp"] for r in rows)
    par = {
        "goals": (sum(r["gf"] for r in rows) / team_games) if team_games else 0.0,
        "xg": (sum(r["xg_f"] for r in rows) / xg_games) if xg_games else 0.0,
        "sog": (sum(r["sog_f"] for r in rows) / sog_games) if sog_games else 0.0,
        "attempts": (sum(r["att_f"] for r in rows) / xg_games) if xg_games else 0.0,
    }
    total_xg = sum(r["xg_f"] for r in rows)
    par_ratio = (sum(r["gf_xg"] for r in rows) / total_xg) if total_xg > 0 else 1.0
    league_ppg = sum(r["ppg"] for r in rows)
    league_ppo = sum(r["ppo"] for r in rows)
    league_gf_pp = sum(r["gf"] for r in rows if r["games_pp"])

    def offense(r: dict[str, Any]) -> float:
        score = 0.0
        for key, weight in OFFENSE_WEIGHTS.items():
            p = par[key]
            if p <= 0:
                score += weight
                continue
            total, games = {
                "xg": (r["xg_f"], r["games_xg"]),
                "sog": (r["sog_f"], r["games_sog"]),
                "attempts": (r["att_f"], r["games_xg"]),
                "goals": (r["gf"], r["games"]),
            }[key]
            score += weight * (_shrunk(total, games, p) / p)
        return 100.0 * score

    out_rows = []
    for r in rows:
        off = offense(r)
        xga_pg = _shrunk(r["xg_a"], r["games_xg"], par["xg"]) if par["xg"] > 0 else 0.0
        defense = 100.0 * par["xg"] / xga_pg if xga_pg > 0 else 100.0
        clinical = 100.0 * (r["gf_xg"] + PRIOR_XG * par_ratio) / (r["xg_f"] + PRIOR_XG) if par_ratio else 100.0
        if par["goals"] > 0:
            scoring = 100.0 * _shrunk(r["gf"], r["games"], par["goals"]) / par["goals"]
            ga_pg = _shrunk(r["ga"], r["games"], par["goals"])
            stopping = 100.0 * par["goals"] / ga_pg if ga_pg > 0 else 100.0
        else:
            scoring = stopping = 100.0
        out_rows.append(
            {
                "id": r["id"],
                "abbr": r["abbr"],
                "games": r["games"],
                "games_xg": r["games_xg"],
                "gf": r["gf"],
                "ga": r["ga"],
                "goals_per_game": _per_game(r["gf"], r["games"]),
                "ga_per_game": _per_game(r["ga"], r["games"]),
                "xg_per_game": _per_game(r["xg_f"], r["games_xg"]),
                "xga_per_game": _per_game(r["xg_a"], r["games_xg"]),
                "sog_per_game": _per_game(r["sog_f"], r["games_sog"], 1),
                "sog_allowed_per_game": _per_game(r["sog_a"], r["games_sog"], 1),
                "attempts_per_game": _per_game(r["att_f"], r["games_xg"], 1),
                "offense_power": _published(off),
                "potent": off > 100.0,
                "defense_power": _published(defense),
                "solid": defense > 100.0,
                "clinical": int(round(clinical)),
                "scoring_power": _published(scoring),
                "stopping_power": _published(stopping),
                "pp": {
                    "games": r["games_pp"],
                    "goals": r["ppg"],
                    "opps": r["ppo"],
                    "pct": _pct(r["ppg"], r["ppo"]),
                    "of_goals": r["gf"],
                    "share": _pct(r["ppg"], r["gf"], 0) if r["games_pp"] else None,
                    "sh_goals": r["shg_f"],
                },
                "pk": {
                    "games": r["games_pp"],
                    "allowed": r["ppga"],
                    "times_short": r["tsh"],
                    "pct": _pct(r["tsh"] - r["ppga"], r["tsh"]),
                    "of_goals": r["ga"],
                    "share": _pct(r["ppga"], r["ga"], 0) if r["games_pp"] else None,
                    "sh_goals_allowed": r["shg_a"],
                },
                "lines": {
                    "games": r["games_lines"],
                    "scored": _units_out(r["lines_for"]),
                    "allowed": _units_out(r["lines_against"]),
                },
                "first_period": {
                    "games": r["games_p1"],
                    "goals_per_game": _per_game(sum(b["goals"] for b in r["p1"]), r["games_p1"]),
                    "buckets": _buckets_out(r["p1"]),
                },
            }
        )

    league_lines = _blank_units()
    league_lines_against = _blank_units()
    league_p1 = _blank_buckets()
    for r in rows:
        _add_units(league_lines, r["lines_for"])
        _add_units(league_lines_against, r["lines_against"])
        for index, b in enumerate(r["p1"]):
            for key in ("games", "goals", "wins"):
                league_p1[index][key] += b[key]
    league_p1_games = sum(b["games"] for b in league_p1)

    ranked = {r["id"] for r in out_rows if r["games"] >= MIN_GAMES}
    _rank(out_rows, "offense_power", "offense_rank", ranked=ranked)
    _rank(out_rows, "defense_power", "defense_rank", ranked=ranked)
    _rank(out_rows, "scoring_power", "scoring_rank", ranked=ranked)
    _rank(out_rows, "stopping_power", "stopping_rank", ranked=ranked)
    for row in out_rows:
        row["pp_value"] = row["pp"]["pct"]
        row["pk_value"] = row["pk"]["pct"]
    _rank(out_rows, "pp_value", "pp_rank", ranked={r["id"] for r in out_rows if r["id"] in ranked and r["pp"]["opps"] > 0})
    _rank(out_rows, "pk_value", "pk_rank", ranked={r["id"] for r in out_rows if r["id"] in ranked and r["pk"]["times_short"] > 0})
    for row in out_rows:
        row["pp"]["rank"] = row.pop("pp_rank")
        row["pk"]["rank"] = row.pop("pk_rank")
        row.pop("pp_value")
        row.pop("pk_value")

    return {
        "season_label": season_label,
        "season_year": season_year,
        "season_kind": season_kind,
        "as_of": today.isoformat(),
        "games": len(season_rows),
        "min_games": MIN_GAMES,
        "model": MODEL_NOTE,
        "league": {
            "par_power": PAR_POWER,
            "teams": len(out_rows),
            "teams_ranked": len(ranked),
            "goals_per_game": round(par["goals"], 2),
            "xg_per_game": round(par["xg"], 2),
            "sog_per_game": round(par["sog"], 1),
            "attempts_per_game": round(par["attempts"], 1),
            "goals_per_xg": round(par_ratio, 2),
            "pp_pct": _pct(league_ppg, league_ppo),
            "pp_share": _pct(league_ppg, league_gf_pp, 0),
            "games_with_plays": xg_games // 2,
            "games_with_pp": pp_games // 2,
            "lines": {
                "games": sum(r["games_lines"] for r in rows) // 2,
                "scored": _units_out(league_lines),
                "allowed": _units_out(league_lines_against),
            },
            "first_period": {
                "games": league_p1_games,
                "goals_per_game": _per_game(sum(b["goals"] for b in league_p1), league_p1_games),
                "buckets": _buckets_out(league_p1),
            },
        },
        "lines_note": LINES_NOTE,
        "teams": {row["id"]: row for row in out_rows},
    }


def _finals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for event in payload.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        stype = ((comp.get("status") or event.get("status") or {}).get("type")) or {}
        if str(stype.get("name") or "") != "STATUS_FINAL":
            continue
        eid = str(event.get("id") or comp.get("id") or "")
        if not eid:
            continue
        season = event.get("season") or {}
        found.append(
            {
                "id": eid,
                "date": str(event.get("date") or comp.get("date") or ""),
                "season_year": _int(season.get("year")),
                "season_type": _int(season.get("type")),
            }
        )
    return found


class PowerFeed:
    """Season finals rolled into power numbers; refreshes in the background."""

    def __init__(self, fetch: FetchJson = fetch_json, *, workers: int = 6, lines: LineSource | None = None) -> None:
        self._fetch = fetch
        self._lines = lines if lines is not None else LineSource(fetch)
        self._workers = max(1, workers)
        self._lock = threading.Lock()
        self._months: dict[str, tuple[float, dict[str, Any]]] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._misses: dict[str, float] = {}
        self._result: tuple[float, date, dict[str, Any]] | None = None
        self._last_error: str | None = None
        self._worker: threading.Thread | None = None

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

    def _record(self, final: dict[str, Any], now: float) -> dict[str, Any] | None:
        eid = final["id"]
        with self._lock:
            hit = self._records.get(eid)
            if hit is None and now - self._misses.get(eid, 0.0) < MISS_TTL:
                return None
        record = hit
        if record is None:
            try:
                summary = self._fetch(SUMMARY_URL.format(event_id=eid))
                record = game_record(summary, event_id=eid) if isinstance(summary, dict) else None
            except Exception as exc:  # noqa: BLE001 - one missing game must not sink the table
                self._last_error = str(exc)
                record = None
            with self._lock:
                if record is None:
                    self._misses[eid] = now
                    return None
                # The month scoreboard knows the season; a summary header sometimes does not.
                record["season_year"] = record["season_year"] or final["season_year"]
                record["season_type"] = record["season_type"] or final["season_type"]
                record["date"] = record["date"] or final["date"]
                if not record["day"]:
                    day = _game_day(record["date"])
                    record["day"] = day.isoformat() if day else ""
                self._records[eid] = record
        # The NHL posts a game's shift chart a little after the final; keep asking until it lands.
        if record.get("lines") is None:
            lines = self._lines.for_record(record, now)
            if lines is not None:
                with self._lock:
                    record["lines"] = lines
        return record

    def refresh(self, *, today: date | None = None) -> dict[str, Any]:
        """Rebuild now, fetching any finals not seen yet."""
        today = today or datetime.now(ET).date()
        now = time.time()
        try:
            year, label, _start = _season_meta(self._fetch(SCOREBOARD_URL))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            year, label = 0, ""
        if not year:
            year = today.year + (1 if today.month >= 9 else 0)
            label = f"{year - 1}-{str(year)[-2:]}"

        finals: list[dict[str, Any]] = []
        for ym in _months(date(year - 1, 9, 1), today):
            finals.extend(_finals(self._load_month(ym, now, today)))
        finals = list({f["id"]: f for f in finals if f["season_year"] == year or not f["season_year"]}.values())
        regular = [f for f in finals if f["season_type"] in (REGULAR_TYPE, PLAYOFF_TYPE)]
        wanted = regular or [f for f in finals if f["season_type"] == PRESEASON_TYPE]
        with ThreadPoolExecutor(max_workers=min(self._workers, max(1, len(wanted)))) as pool:
            records = [r for r in pool.map(lambda f: self._record(f, now), wanted) if r]
        result = build_power(records, today=today, season_year=year, season_label=label)
        result["fetched_at"] = int(now)
        result["missing"] = len(wanted) - len(records)
        with self._lock:
            self._result = (now, today, result)
        return result

    def _refresh_in_background(self, today: date) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._safe_refresh, args=(today,), name="power-refresh", daemon=True)
            self._worker.start()

    def _safe_refresh(self, today: date) -> None:
        try:
            self.refresh(today=today)
        except Exception as exc:  # noqa: BLE001 - keep serving the last table
            self._last_error = str(exc)

    def get(self, *, today: date | None = None, wait: bool = False) -> dict[str, Any]:
        """The latest table. The first call starts a build and says ``loading`` until it lands."""
        today = today or datetime.now(ET).date()
        now = time.time()
        with self._lock:
            cached = self._result
        fresh = cached is not None and cached[1] == today and now - cached[0] < RESULT_TTL
        if fresh:
            return cached[2]
        if wait:
            return self.refresh(today=today)
        self._refresh_in_background(today)
        if cached is not None:
            stale = dict(cached[2])
            stale["refreshing"] = True
            return stale
        return {"loading": True, "teams": {}, "league": {}, "error": self._last_error}
