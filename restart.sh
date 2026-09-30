#!/usr/bin/env bash
# Pull latest main, rebuild, and recreate the eeehoc-dashboard container.
# Same shape as tekneeq/eeefut restart.sh.
set -euo pipefail
cd "$(dirname "$0")"

git pull

GIT_SHA="$(git rev-parse --short HEAD)"
GIT_COMMIT_TIME="$(git show -s --format=%cI HEAD)"

docker build \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    --build-arg "GIT_COMMIT_TIME=${GIT_COMMIT_TIME}" \
    -t eeehoc-dashboard:latest .
docker rm -f eeehoc-dashboard 2>/dev/null || true
docker run -d --name eeehoc-dashboard --restart unless-stopped \
    -p 8083:8083 \
    -e "EEEHOC_GIT_SHA=${GIT_SHA}" \
    -e "EEEHOC_GIT_COMMIT_TIME=${GIT_COMMIT_TIME}" \
    eeehoc-dashboard:latest

echo "Started eeehoc-dashboard at ${GIT_SHA} (${GIT_COMMIT_TIME})"
