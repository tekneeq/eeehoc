# eeehoc

NHL **Live** dashboard with the same dark Revenant chiclets as [eeesoc](https://github.com/tekneeq/eeesoc) (soccer) and [eeefut](https://github.com/tekneeq/eeefut) (NFL).

The **Live** tab (`/`) shows one day's slate as chiclets: score, period clock, shots, the goal log (time remaining, scorer, assists, PP / SH / EN), and — while the game is in progress — a rink with the last play and the on-ice strength (5-on-4, 5-on-3). Open a chiclet for hits, blocked shots, faceoffs, power play, penalty minutes, goalies, leaders, three stars, and the period linescore. Step the day back and forward; **Today** returns to ESPN's current slate.

Data comes from ESPN's public scoreboard (`/api/live`, 20s cache; the box needs outbound HTTPS to `site.web.api.espn.com`).

## Quick start

```bash
uv sync --extra dev
uv run eeehoc --dashboard --port 8083
```

Open http://127.0.0.1:8083

```bash
# Today's slate as JSON, or a specific day
uv run eeehoc --live
uv run eeehoc --live --date 20260922

uv run pytest
```

## Notes

- The scoreboard clock counts down (`2nd 12:00` is twelve minutes left). Play-by-play clocks count up, so goal times are flipped to time remaining. Regulation is 20:00. Overtime is 5:00, except playoffs (ESPN season type 3), which stay at 20:00.
- A power-play pill is the team with more skaters on the ice (ESPN's on-ice list, goalie included). 6 vs 5 is 5-on-4.
- Docker binds `0.0.0.0:8083`.

## Deploy

Same shape as `eeefut` and `eeesoc`, on its own instance, when one exists: `docker compose up -d --build` (or `./scripts/docker-entrypoint.sh` inside the image). Health check is `GET /health`.
