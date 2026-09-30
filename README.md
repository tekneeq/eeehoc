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

## Deploy on EC2 (same pattern as `tekneeq/eeefut`)

eeehoc, eeefut, and eeesoc each have **their own instance**. This repo only SSHes into the eeehoc box.

Flow on every push/merge to `main`:

1. GitHub Actions workflow `.github/workflows/deploy-ec2.yml` SSHes into the box
2. `git pull --ff-only origin main`
3. `./deploy.sh` → `./restart.sh` (docker rebuild + `docker run --restart unless-stopped`)
4. Container entrypoint serves `:8083` (no season cache to warm)

### One-time bootstrap on the EC2 host

```bash
git clone https://github.com/tekneeq/eeehoc.git ~/eeehoc
cd ~/eeehoc
chmod +x deploy.sh restart.sh scripts/docker-entrypoint.sh scripts/install-docker-amazon-linux.sh
./deploy.sh
```

`./deploy.sh` installs Docker on a fresh Amazon Linux box (`dnf`/`yum install docker`), starts the daemon, and adds `ec2-user` to the `docker` group. The same shell uses `sudo docker` until you log out and back in. You can also install it by hand first:

```bash
./scripts/install-docker-amazon-linux.sh
newgrp docker
./deploy.sh
```

`EC2_APP_DIR` should be that checkout (for example `/home/ec2-user/eeehoc`).

Nginx on this box: **:80 / :443 → 127.0.0.1:8083** with `server_name eeehoc.com www.eeehoc.com`.

```bash
cd ~/eeehoc
./scripts/install-nginx-80.sh          # HTTP + ACME webroot
./scripts/enable-https.sh              # Let's Encrypt, then HTTPS
# CERTBOT_EMAIL=you@example.com ./scripts/enable-https.sh --www
```

Amazon Linux leaves SELinux enforcing, and nginx cannot open a connection to `:8083` until `httpd_can_network_connect` is on. The install script sets that boolean. If `:8083/health` is `ok` but `:80/health` is a 502, run:

```bash
sudo setsebool -P httpd_can_network_connect 1
curl -fsS http://127.0.0.1/health
```

Browsers type `eeehoc.com` as **https://** first. Until `:443` has a cert, the domain looks down while `http://<public-ip>/` still works. Override the name with `EEEHOC_DOMAIN` if DNS is not `eeehoc.com`.

Checklist if the domain fails in a browser:

1. Route53 **A** for `eeehoc.com` = this instance's **public** IPv4 (`curl -4 https://checkip.amazonaws.com` on the box). `www` needs an A or CNAME too.
2. Security group inbound **80** and **443**.
3. Run `enable-https.sh` so nginx listens on 443.

### Auto-deploy on merge

Pushes to `main` (a merged pull request) trigger `.github/workflows/deploy-ec2.yml`, which SSHes in and runs `./deploy.sh`.

Repo secrets (Settings → Secrets and variables → Actions):

| Secret | Notes |
| --- | --- |
| `EC2_HOST` | This instance only (not eeefut / eeesoc) |
| `EC2_USER` | Usually `ec2-user` |
| `EC2_SSH_PRIVATE_KEY` | Full `.pem`, including `BEGIN`/`END` lines |
| `EC2_APP_DIR` | Checkout on the box, for example `/home/ec2-user/eeehoc` |
| `EC2_SSH_PORT` | Optional, default `22` |

Manual redeploy / diagnostics: Actions → **Deploy to EC2** / **EC2 status** → Run workflow.

Local-on-box redeploy anytime:

```bash
cd ~/eeehoc && ./deploy.sh
```
