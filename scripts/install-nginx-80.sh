#!/usr/bin/env bash
#
# Install nginx :80 (and :443 when a Let's Encrypt cert exists) → :8083.
# eeehoc, eeefut, and eeesoc each run on their own EC2 box.
#
# Amazon Linux ships a ``server { listen 80; server_name _; }`` in
# /etc/nginx/nginx.conf. This script comments that out, installs ours,
# and starts nginx if the unit is inactive.
#
# Usage (on the EC2 host, from the eeehoc repo root):
#   ./scripts/install-nginx-80.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HTTP_SRC="$ROOT/scripts/nginx-eeehoc-dashboard.conf"
TLS_SRC="$ROOT/scripts/nginx-eeehoc-https.conf"
DEST="/etc/nginx/conf.d/eeehoc-dashboard.conf"
CERT="${EEEHOC_TLS_CERT:-/etc/letsencrypt/live/eeehoc.com/fullchain.pem}"
MARKER="eeehoc: default :80 server disabled"

if [ -f "$CERT" ]; then
    SRC="$TLS_SRC"
else
    SRC="$HTTP_SRC"
fi

if [ ! -f "$SRC" ]; then
    echo "ERROR: missing $SRC" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -E "$0" "$@"
fi

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

disable_conf_file() {
    local conf="$1"
    [ -f "$conf" ] || return 0
    case "$conf" in
        "$DEST") return 0 ;;
    esac
    if grep -qE 'listen[[:space:]]+(\[::\]:)?80([[:space:]]|;)' "$conf"; then
        local bak="${conf}.disabled"
        log "moving $conf → $bak (it binds :80)"
        mv "$conf" "$bak"
    fi
}

# conf.d / sites-enabled / default.d snippets (not our dest)
shopt -s nullglob
for conf in /etc/nginx/conf.d/*.conf /etc/nginx/default.d/*.conf \
    /etc/nginx/sites-enabled/* /etc/nginx/sites-available/default
do
    disable_conf_file "$conf"
done
shopt -u nullglob

# Amazon Linux / RHEL keep the stock :80 server inside nginx.conf itself.
if [ -f /etc/nginx/nginx.conf ] && grep -qE 'listen[[:space:]]+(\[::\]:)?80([[:space:]]|;)' /etc/nginx/nginx.conf; then
    if grep -q "$MARKER" /etc/nginx/nginx.conf; then
        log "nginx.conf already patched ($MARKER)"
    else
        bak="/etc/nginx/nginx.conf.bak.eeehoc"
        if [ ! -f "$bak" ]; then
            cp -a /etc/nginx/nginx.conf "$bak"
            log "backed up /etc/nginx/nginx.conf → $bak"
        fi
        if ! python3 "$ROOT/scripts/disable_nginx_default_80.py" /etc/nginx/nginx.conf; then
            log "WARNING: could not comment the stock :80 server in nginx.conf"
            grep -nE 'listen[[:space:]]+(\[::\]:)?80' /etc/nginx/nginx.conf || true
        fi
    fi
fi

mkdir -p /var/www/certbot/.well-known/acme-challenge
chmod -R a+rX /var/www/certbot || true

install -m 0644 "$SRC" "$DEST"
log "installed $DEST (from $(basename "$SRC"))"

nginx -t

start_or_reload() {
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet nginx; then
            log "nginx is active — reloading"
            systemctl reload nginx
            return
        fi
        log "nginx.service is not active — enabling and starting"
        systemctl enable nginx >/dev/null 2>&1 || true
        systemctl start nginx
        return
    fi
    if [ -f /run/nginx.pid ] || [ -f /var/run/nginx.pid ]; then
        nginx -s reload
    else
        nginx
    fi
}

start_or_reload

if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active --quiet nginx && log "nginx is active" || {
        log "ERROR: nginx failed to start"
        systemctl status nginx --no-pager || true
        exit 1
    }
fi

# Amazon Linux keeps SELinux enforcing. nginx (httpd_t) cannot open a
# connection to :8083 until this boolean is on, so :80/health is 502
# while curl to the container itself still returns ok.
allow_nginx_proxy() {
    if ! command -v getenforce >/dev/null 2>&1; then
        return 0
    fi
    if [ "$(getenforce)" = "Disabled" ]; then
        return 0
    fi
    if ! command -v setsebool >/dev/null 2>&1; then
        log "WARNING: SELinux is on but setsebool is missing; nginx may not reach :8083"
        return 0
    fi
    if getsebool httpd_can_network_connect 2>/dev/null | grep -q ' on$'; then
        log "selinux: httpd_can_network_connect already on"
        return 0
    fi
    log "selinux: allowing nginx to proxy to :8083 (httpd_can_network_connect)"
    setsebool -P httpd_can_network_connect 1
}

allow_nginx_proxy

log "http://<host>/ → eeehoc :8083  (/health → container /health)"

if curl -fsS http://127.0.0.1/health >/dev/null 2>&1; then
    log "health via :80: $(curl -fsS http://127.0.0.1/health | tr -d '\n')"
else
    log "WARNING: nginx :80 did not proxy /health (container :8083 can still be fine)"
    curl -sS -o /dev/null -w "  :80 status %{http_code}\n" http://127.0.0.1/health || true
    docker ps --filter name=eeehoc-dashboard --format '{{.Names}} {{.Status}}' 2>/dev/null || true
    curl -fsS http://127.0.0.1:8083/health || true
    echo
    if [ -f /var/log/nginx/error.log ]; then
        log "last nginx errors:"
        tail -n 15 /var/log/nginx/error.log || true
    fi
fi
