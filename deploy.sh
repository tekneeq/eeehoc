#!/usr/bin/env bash
#
# Pull latest code and rebuild the dashboard container.
#
# Usage (on the EC2 host, from the repo root):
#   ./deploy.sh
#
# Called automatically by .github/workflows/deploy-ec2.yml on pushes to
# main (via SSH). Safe to run by hand any time.
#
# Mirrors tekneeq/eeefut deploy.sh. The container only binds :8083 — there
# is no season cache to warm.

set -euo pipefail

cd "$(dirname "$0")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

log "=== deploy start (cwd=$(pwd), rev=$(git rev-parse --short HEAD 2>/dev/null || echo '?')) ==="

log "1/2  rebuild dashboard container (git pull + docker build/run)"
./restart.sh

sleep 3
if ! docker ps --format '{{.Names}}' | grep -qx eeehoc-dashboard; then
    log "ERROR: eeehoc-dashboard container is not running after restart.sh"
    exit 1
fi

log "2/2  health check"
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://127.0.0.1:8083/health" >/dev/null 2>&1; then
        log "healthy: $(curl -fsS http://127.0.0.1:8083/health | tr -d '\n')"
        break
    fi
    if [ "$i" -eq 10 ]; then
        log "ERROR: /health did not respond after restart"
        docker logs --tail 80 eeehoc-dashboard || true
        exit 1
    fi
    sleep 2
done

log "=== deploy done (rev=$(git rev-parse --short HEAD)) ==="
docker ps --filter name=eeehoc-dashboard --format '{{.Names}} {{.Status}} {{.Image}}'
