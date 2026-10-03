"""Pre-game win probability: margin-aware Elo replayed over ESPN's month scoreboards.

Every game of the target season is graded with the ratings *as they stood before the
puck dropped*, so the record and calibration buckets are honest. Unplayed games use the
current ratings. The replay starts two seasons back so the opening-night ratings carry
real information; ratings regress halfway to the mean between seasons.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from eeehoc.live import PLAYOFF_TYPE, SCOREBOARD_URL, FetchJson, _int, fetch_json
from eeehoc.periods import ET, PRESEASON_TYPE, REGULAR_TYPE, _game_day, _months, _season_meta

# Favourite's pre-game probability bands (headline calibration).
BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("50-55", 0.50, 0.55),
    ("55-60", 0.55, 0.60),
    ("60-65", 0.60, 0.65),
    ("65-70", 0.65, 0.70),
    (">70", 0.70, 1.01),
)

# A team's *own* pre-game probability, favourite or not, for the per-team table.
TEAM_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<35", 0.0, 0.35),
    ("35-45", 0.35, 0.45),
    ("45-50", 0.45, 0.50),
    ("50-55", 0.50, 0.55),
    ("55-60", 0.55, 0.60),
    ("60-65", 0.60, 0.65),
    (">65", 0.65, 1.01),
)

GAME_TYPES = {PRESEASON_TYPE: "PRE", REGULAR_TYPE: "REG", PLAYOFF_TYPE: "POST"}
REPLAY_SEASONS = 2  # full seasons replayed before the target season
LOOKAHEAD_DAYS = 10
RECENT_DAYS = 7
DAILY_POINTS = 14

CURRENT_MONTH_TTL = 90.0
PAST_MONTH_TTL = 6 * 60 * 60.0
BOARD_TTL = 60.0


@dataclass(frozen=True)
class EloConfig:
    k: float = 6.0
    hfa: float = 35.0  # home ice, in Elo points (~55% for even teams)
    mean: float = 1500.0
    regress: float = 0.5  # pull toward the mean between seasons
    preseason_k: float = 0.5  # preseason results move ratings half as much
    playoff_k: float = 1.25


def elo_win_prob(diff: float) -> float:
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def margin_multiplier(mov: int) -> float:
    """FiveThirtyEight's NHL margin-of-victory multiplier (1 goal → 0.80, 3 goals → 1.54)."""
    return 0.6686 * math.log(max(1, abs(mov))) + 0.8048


def bucket_for(prob: float) -> str:
    p = max(prob, 1 - prob)
    for key, lo, hi in BUCKETS:
        if lo <= p < hi:
            return key
    return BUCKETS[-1][0]


def team_bucket_for(prob: float) -> str:
    for key, lo, hi in TEAM_BUCKETS:
        if lo <= prob < hi:
            return key
    return TEAM_BUCKETS[-1][0]


def season_label(year: int) -> str:
    return f"{year - 1}-{str(year)[-2:]}" if year else "Season"


# ------------------------------------------------------------------ scoreboard parsing


def _team(comp: dict[str, Any]) -> dict[str, Any]:
    team = comp.get("team") or {}
    return {
        "id": str(team.get("id") or ""),
        "abbr": str(team.get("abbreviation") or ""),
        "name": str(team.get("shortDisplayName") or team.get("name") or ""),
        "full_name": str(team.get("displayName") or ""),
        "logo": str(team.get("logo") or ""),
        "color": str(team.get("color") or "444444"),
        "alt_color": str(team.get("alternateColor") or ""),
    }


def _overtime(detail: str) -> str | None:
    tail = detail.split("/", 1)[1].strip() if "/" in detail else ""
    return tail or None


def parse_month(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every scheduled, live, or final game in a month scoreboard."""
    games: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        status = comp.get("status") or event.get("status") or {}
        stype = status.get("type") or {}
        state = str(stype.get("state") or "pre")
        name = str(stype.get("name") or "")
        if state == "post" and name != "STATUS_FINAL":
            continue  # postponed / cancelled
        iso = str(event.get("date") or comp.get("date") or "")
        day = _game_day(iso)
        if day is None:
            continue
        season = event.get("season") or {}
        final = state == "post"
        detail = str(stype.get("shortDetail") or stype.get("detail") or "")
        games.append(
            {
                "id": str(event.get("id") or comp.get("id") or ""),
                "date": iso,
                "day": day.isoformat(),
                "season_year": _int(season.get("year")),
                "season_type": _int(season.get("type")),
                "neutral": bool(comp.get("neutralSite", False)),
                "state": state,
                "detail": detail,
                "home": _team(home),
                "away": _team(away),
                "home_score": _int(home.get("score")) if state != "pre" else None,
                "away_score": _int(away.get("score")) if state != "pre" else None,
                "final": final,
                "ot": _overtime(detail) if final else None,
            }
        )
    return games


# ------------------------------------------------------------------ Elo replay


def _record_str(w: int, l: int, otl: int) -> str:
    return f"{w}-{l}-{otl}"


def run_model(
    games: Iterable[dict[str, Any]],
    seasons: set[int],
    *,
    config: EloConfig = EloConfig(),
) -> dict[str, Any]:
    """Replay Elo chronologically; return pre-game predictions for ``seasons`` and current ratings."""
    ratings: dict[str, float] = {}
    teams: dict[str, dict[str, Any]] = {}
    standings: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0, 0])  # W, L, OTL
    predictions: list[dict[str, Any]] = []
    last_season: int | None = None
    last_final_day = ""

    def rating(team_id: str) -> float:
        return ratings.setdefault(team_id, config.mean)

    ordered = sorted(games, key=lambda g: (g["date"], g["id"]))
    for g in ordered:
        year = g["season_year"]
        if not year or not g["home"]["id"] or not g["away"]["id"]:
            continue
        if last_season is not None and year != last_season:
            for team_id in list(ratings):
                ratings[team_id] = config.mean + (ratings[team_id] - config.mean) * (1 - config.regress)
        last_season = year

        home_id, away_id = g["home"]["id"], g["away"]["id"]
        teams[home_id] = g["home"]
        teams[away_id] = g["away"]
        home_elo, away_elo = rating(home_id), rating(away_id)
        diff = home_elo - away_elo + (0.0 if g["neutral"] else config.hfa)
        p_home = elo_win_prob(diff)
        game_type = GAME_TYPES.get(g["season_type"], "REG")

        if year in seasons and game_type != "PRE":
            predictions.append(_prediction(g, game_type, home_elo, away_elo, p_home, standings))

        if not g["final"]:
            continue
        hs, as_ = g["home_score"], g["away_score"]
        if hs is None or as_ is None or hs == as_:
            continue
        home_won = hs > as_
        k = config.k * (config.preseason_k if game_type == "PRE" else config.playoff_k if game_type == "POST" else 1.0)
        shift = k * margin_multiplier(hs - as_) * ((1.0 if home_won else 0.0) - p_home)
        ratings[home_id] = home_elo + shift
        ratings[away_id] = away_elo - shift
        if g["day"] > last_final_day:
            last_final_day = g["day"]
        if game_type == "REG":
            winner, loser = (home_id, away_id) if home_won else (away_id, home_id)
            standings[(year, winner)][0] += 1
            standings[(year, loser)][2 if g["ot"] else 1] += 1

    table = []
    for team_id, elo in ratings.items():
        meta = teams.get(team_id)
        if not meta or not meta["abbr"]:
            continue
        w, l, otl = standings.get((last_season or 0, team_id), [0, 0, 0])
        table.append({**meta, "elo": round(elo, 1), "record": _record_str(w, l, otl), "wins": w, "losses": l, "otl": otl})
    table.sort(key=lambda r: -r["elo"])
    for i, row in enumerate(table, start=1):
        row["rank"] = i
    return {"predictions": predictions, "ratings": table, "through_day": last_final_day, "season": last_season or 0}


def _prediction(
    g: dict[str, Any],
    game_type: str,
    home_elo: float,
    away_elo: float,
    p_home: float,
    standings: dict[tuple[int, str], list[int]],
) -> dict[str, Any]:
    year = g["season_year"]
    favorite = "home" if p_home >= 0.5 else "away"
    fav_prob = max(p_home, 1 - p_home)
    out: dict[str, Any] = {
        "id": g["id"],
        "day": g["day"],
        "date": g["date"],
        "season_year": year,
        "game_type": game_type,
        "neutral": g["neutral"],
        "state": g["state"],
        "detail": g["detail"],
        "home": {**g["home"], "elo": round(home_elo, 1), "prob": round(p_home, 4), "record": _record_str(*standings[(year, g["home"]["id"])])},
        "away": {**g["away"], "elo": round(away_elo, 1), "prob": round(1 - p_home, 4), "record": _record_str(*standings[(year, g["away"]["id"])])},
        "favorite": favorite,
        "favorite_abbr": g[favorite]["abbr"],
        "favorite_prob": round(fav_prob, 4),
        "bucket": bucket_for(p_home),
        "played": g["final"],
        "home_score": g["home_score"],
        "away_score": g["away_score"],
        "ot": g["ot"],
        "result": None,
    }
    if g["final"] and g["home_score"] is not None and g["away_score"] is not None and g["home_score"] != g["away_score"]:
        winner = "home" if g["home_score"] > g["away_score"] else "away"
        outcome = 1.0 if winner == "home" else 0.0
        out["result"] = {
            "winner": winner,
            "winner_abbr": g[winner]["abbr"],
            "correct": winner == favorite,
            "margin": abs(g["home_score"] - g["away_score"]),
            "ot": g["ot"],
            "brier": round((p_home - outcome) ** 2, 4),
        }
    return out


# ------------------------------------------------------------------ dashboards


def _block() -> dict[str, Any]:
    return {"games": 0, "decided": 0, "correct": 0, "wrong": 0, "pending": 0, "brier_sum": 0.0}


def _tally(block: dict[str, Any], game: dict[str, Any]) -> None:
    block["games"] += 1
    res = game.get("result")
    if not res:
        block["pending"] += 1
        return
    block["decided"] += 1
    block["correct"] += int(res["correct"])
    block["wrong"] += int(not res["correct"])
    block["brier_sum"] += res["brier"]


def _finish(block: dict[str, Any]) -> dict[str, Any]:
    decided = block["decided"]
    out = {k: v for k, v in block.items() if k != "brier_sum"}
    out["pct"] = round(block["correct"] / decided * 100, 1) if decided else None
    out["brier"] = round(block["brier_sum"] / decided, 3) if decided else None
    out["record"] = f"{block['correct']}-{block['wrong']}"
    return out


def record_summary(games: list[dict[str, Any]], *, today: date) -> dict[str, Any]:
    total, recent = _block(), _block()
    by_day: dict[str, dict[str, Any]] = {}
    # A finished season's "recent" window ends on its last game day, not today.
    anchor = today.isoformat()
    if games and not any(g["result"] is None for g in games):
        anchor = min(anchor, max(g["day"] for g in games))
    cutoff = (date.fromisoformat(anchor) - timedelta(days=RECENT_DAYS - 1)).isoformat()
    for g in games:
        _tally(total, g)
        if cutoff <= g["day"] <= anchor:
            _tally(recent, g)
        _tally(by_day.setdefault(g["day"], _block()), g)
    days = sorted(d for d in by_day if d <= anchor)[-DAILY_POINTS:]
    return {
        "total": _finish(total),
        "recent": {"days": RECENT_DAYS, "through": anchor, **_finish(recent)},
        "daily": [{"day": d, **_finish(by_day[d])} for d in days],
    }


def _side_block() -> dict[str, Any]:
    return {"games": 0, "wins": 0, "losses": 0}


def _close_side(block: dict[str, Any]) -> dict[str, Any]:
    decided = block["wins"] + block["losses"]
    block["record"] = f"{block['wins']}-{block['losses']}"
    block["pct"] = round(block["wins"] / decided * 100, 1) if decided else None
    return block


def bucket_record(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Favourite's record by pre-game probability band, split home/away underneath."""
    blocks = {
        key: {"key": key, "label": key + "%", "lo": lo, "hi": hi, "pending": 0, "prob_sum": 0.0, **_side_block(), "home": _side_block(), "away": _side_block()}
        for key, lo, hi in BUCKETS
    }
    for g in games:
        b = blocks[g["bucket"]]
        res = g.get("result")
        if not res:
            b["pending"] += 1
            continue
        side = b[g["favorite"]]
        b["games"] += 1
        side["games"] += 1
        b["prob_sum"] += g["favorite_prob"]
        key = "wins" if res["correct"] else "losses"
        b[key] += 1
        side[key] += 1
    out = []
    for key, _lo, _hi in BUCKETS:
        b = blocks[key]
        _close_side(b)
        b["expected_pct"] = round(b["prob_sum"] / b["games"] * 100, 1) if b["games"] else None
        b.pop("prob_sum")
        _close_side(b["home"])
        _close_side(b["away"])
        out.append(b)
    return out


def team_bucket_records(games: list[dict[str, Any]], ratings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per team: record when favoured, when the underdog, and by its own pre-game probability."""
    by_id = {r["id"]: r for r in ratings}
    teams: dict[str, dict[str, Any]] = {}

    def shell(meta: dict[str, Any]) -> dict[str, Any]:
        rated = by_id.get(meta["id"]) or {}
        return {
            "id": meta["id"],
            "abbr": meta["abbr"],
            "name": meta["name"],
            "logo": meta["logo"],
            "color": meta["color"],
            "alt_color": meta["alt_color"],
            "elo": rated.get("elo"),
            "rank": rated.get("rank"),
            "season_record": rated.get("record"),
            "favored": _side_block(),
            "underdog": _side_block(),
            "buckets": {key: _side_block() for key, _, _ in TEAM_BUCKETS},
        }

    for g in games:
        res = g.get("result")
        if not res:
            continue
        for side in ("home", "away"):
            meta = g[side]
            row = teams.setdefault(meta["id"], shell(meta))
            won = res["winner"] == side
            role = row["favored" if g["favorite"] == side else "underdog"]
            bucket = row["buckets"][team_bucket_for(meta["prob"])]
            for blk in (role, bucket):
                blk["games"] += 1
                blk["wins" if won else "losses"] += 1
    out = []
    for row in teams.values():
        _close_side(row["favored"])
        _close_side(row["underdog"])
        for blk in row["buckets"].values():
            _close_side(blk)
        out.append(row)
    out.sort(key=lambda t: (t["rank"] or 999, t["abbr"]))
    return out


def pick_day(games: list[dict[str, Any]], today: date) -> str:
    """Today if it has games, else the next day with games, else the last day that had any."""
    days = sorted({g["day"] for g in games})
    iso = today.isoformat()
    if iso in days:
        return iso
    upcoming = [d for d in days if d > iso]
    if upcoming:
        return upcoming[0]
    return days[-1] if days else iso


def build_board(model: dict[str, Any], season: int, *, today: date, config: EloConfig = EloConfig()) -> dict[str, Any]:
    games = [g for g in model["predictions"] if g["season_year"] == season]
    games.sort(key=lambda g: (g["date"], g["id"]))
    return {
        "season": season,
        "season_label": season_label(season),
        "model": {"name": "Elo + margin of victory", "k": config.k, "hfa": config.hfa, "hfa_pct": round(elo_win_prob(config.hfa) * 100, 1)},
        "through_day": model["through_day"],
        "days": sorted({g["day"] for g in games}),
        "current_day": pick_day(games, today),
        "record": record_summary(games, today=today),
        "buckets": bucket_record(games),
        "team_buckets": team_bucket_records(games, model["ratings"]),
        "ratings": model["ratings"],
        "games": games,
        "generated_at": int(time.time()),
    }


def day_view(board: dict[str, Any], day: str | None) -> dict[str, Any]:
    """The board with only one day's games, plus prev/next pointers for the day nav."""
    days = board["days"]
    day = day if day in days else board["current_day"]
    games = [g for g in board["games"] if g["day"] == day]
    block = _block()
    for g in games:
        _tally(block, g)
    index = days.index(day) if day in days else -1
    out = {k: v for k, v in board.items() if k != "games"}
    out.update(
        {
            "day": day,
            "prev_day": days[index - 1] if index > 0 else None,
            "next_day": days[index + 1] if 0 <= index < len(days) - 1 else None,
            "day_record": _finish(block),
            "games": games,
        }
    )
    return out


# ------------------------------------------------------------------ service


class WinProbService:
    """Caches month scoreboards and the replayed model; recomputes once a minute at most."""

    def __init__(self, fetch: FetchJson = fetch_json, *, workers: int = 6, config: EloConfig = EloConfig()) -> None:
        self._fetch = fetch
        self._workers = max(1, workers)
        self._config = config
        self._lock = threading.Lock()
        self._months: dict[str, tuple[float, dict[str, Any]]] = {}
        self._boards: dict[int, tuple[float, date, dict[str, Any]]] = {}
        self._season: tuple[float, int] | None = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _load_month(self, ym: str, now: float, today: date) -> dict[str, Any]:
        past = ym < f"{today.year}{today.month:02d}"
        ttl = PAST_MONTH_TTL if past else CURRENT_MONTH_TTL
        with self._lock:
            cached = self._months.get(ym)
            if cached is not None and now - cached[0] < ttl:
                return cached[1]
        try:
            payload = self._fetch(f"{SCOREBOARD_URL}?dates={ym}&limit=1000")
        except Exception as exc:  # noqa: BLE001 - serve what we have for that month
            self._last_error = str(exc)
            with self._lock:
                cached = self._months.get(ym)
            if cached is not None:
                return cached[1]
            if past:
                return {}
            raise
        if not isinstance(payload, dict):
            payload = {}
        with self._lock:
            self._months[ym] = (now, payload)
        return payload

    def _season_year(self, today: date, now: float, *, force: bool = False) -> int:
        with self._lock:
            if self._season and not force and now - self._season[0] < BOARD_TTL:
                return self._season[1]
        try:
            year, _label, _start = _season_meta(self._fetch(SCOREBOARD_URL))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            year = 0
        if not year:
            year = today.year + (1 if today.month >= 9 else 0)
        with self._lock:
            self._season = (now, year)
        return year

    def get(self, *, season: int | None = None, today: date | None = None, force: bool = False) -> dict[str, Any]:
        today = today or datetime.now(ET).date()
        now = time.time()
        current = self._season_year(today, now, force=force)
        season = season or current
        with self._lock:
            hit = self._boards.get(season)
            if hit and not force and hit[1] == today and now - hit[0] < BOARD_TTL:
                return hit[2]
        if force:
            with self._lock:
                self._months.clear()

        start = date(current - 1 - REPLAY_SEASONS, 9, 1)
        end = today + timedelta(days=LOOKAHEAD_DAYS)
        months = _months(start, end)
        with ThreadPoolExecutor(max_workers=min(self._workers, len(months))) as pool:
            payloads = list(pool.map(lambda ym: self._load_month(ym, now, today), months))
        seen: set[str] = set()
        games: list[dict[str, Any]] = []
        for payload in payloads:
            for g in parse_month(payload):
                if g["id"] in seen:
                    continue
                seen.add(g["id"])
                games.append(g)

        model = run_model(games, {current, current - 1}, config=self._config)
        boards = {year: build_board(model, year, today=today, config=self._config) for year in (current, current - 1)}
        for board in boards.values():
            board["seasons"] = [current, current - 1]
            board["current_season"] = current
            board["error"] = self._last_error
        with self._lock:
            for year, board in boards.items():
                self._boards[year] = (now, today, board)
        return boards.get(season) or boards[current]
