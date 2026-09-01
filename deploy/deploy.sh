#!/usr/bin/env bash
# Docker deploy for Plotter: builds the app image and brings up the
# app + Apache/ModSecurity(WAF) stack, replacing the legacy systemd service.
# Idempotent, and rolls back to the systemd service if the stack is unhealthy.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/plotter
PORT=8088
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run me as root" >&2; exit 1; }

if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq docker.io docker-compose-v2 \
        || apt-get install -y -qq docker.io docker-compose
    systemctl enable --now docker
fi
COMPOSE="docker compose"; $COMPOSE version >/dev/null 2>&1 || COMPOSE="docker-compose"

mkdir -p /var/lib/plotter/cache

# Sync the project into a stable location (keep the venv so the legacy service
# stays available as rollback, and so the host-side refresh CLI keeps working).
say "Syncing project to $APP_DIR"
rsync -a --delete --exclude /venv --exclude /data --exclude '__pycache__' \
      "$SRC"/ "$APP_DIR"/
cd "$APP_DIR"

say "Building images"
$COMPOSE build

say "Stopping legacy systemd service (kept installed for rollback)"
systemctl stop plotter.service 2>/dev/null || true
systemctl disable plotter.service 2>/dev/null || true

say "Starting the docker stack (app + Apache/ModSecurity WAF)"
$COMPOSE up -d --remove-orphans

say "Waiting for the WAF-fronted app to answer on :$PORT"
ok=no
for i in $(seq 1 45); do
    if curl -fsS --max-time 5 -H "Host: plotter.erau.ee" \
        "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ok=yes; break; fi
    sleep 2
done

if [ "$ok" != yes ]; then
    echo "Stack did not answer on :$PORT — rolling back to the systemd service" >&2
    $COMPOSE logs --tail 40 || true
    $COMPOSE down || true
    systemctl enable --now plotter.service
    exit 1
fi

say "Plotter is up via Docker + Apache + ModSecurity/CRS on :$PORT"
docker image prune -f >/dev/null 2>&1 || true
