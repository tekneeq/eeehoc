# Sourced by deploy.sh and restart.sh. Installs Docker on a fresh Amazon
# Linux box, then picks a docker command that works in this shell.
# shellcheck shell=bash

docker_cmd() { command docker "$@"; }

ensure_docker() {
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if ! command -v docker >/dev/null 2>&1; then
    bash "$root/install-docker-amazon-linux.sh"
    hash -r || true
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is still not on PATH after install" >&2
    return 1
  fi
  if docker info >/dev/null 2>&1; then
    docker_cmd() { command docker "$@"; }
    return 0
  fi
  if sudo docker info >/dev/null 2>&1; then
    echo "docker group is not active in this shell; using sudo docker"
    docker_cmd() { sudo docker "$@"; }
    return 0
  fi
  echo "ERROR: docker is installed but this shell cannot reach the daemon." >&2
  echo "Run: newgrp docker" >&2
  echo "then: ./deploy.sh" >&2
  return 1
}
