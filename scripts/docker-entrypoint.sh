#!/usr/bin/env bash
# Serve the NHL Live chiclet board. No season cache to warm — the slate is live from ESPN.
set -euo pipefail

PORT="${EEEHOC_PORT:-8083}"
HOST="${EEEHOC_HOST:-0.0.0.0}"

echo "[eeehoc] bind=${HOST}:${PORT}"
echo "[eeehoc] git=${EEEHOC_GIT_SHA:-unknown} @ ${EEEHOC_GIT_COMMIT_TIME:-unknown}"

exec uv run eeehoc --dashboard --host "$HOST" --port "$PORT"
