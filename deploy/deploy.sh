#!/usr/bin/env bash
# Build and run Plotter as an ordinary user. No sudo, no system packages, no
# root systemd units: everything lives in this checkout plus a state directory
# you own, and runs in containers (app + Apache/ModSecurity WAF).
#
#   ./deploy/deploy.sh                 build and (re)start the stack
#   ./deploy/deploy.sh --dev           app only, published on 127.0.0.1
#   ./deploy/deploy.sh --no-build      restart without rebuilding
#
# Requires: docker (rootless, or with your user in the docker group) or podman.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC"

DEV=no
BUILD=yes
for arg in "$@"; do
    case "$arg" in
        --dev) DEV=yes ;;
        --no-build) BUILD=no ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mxxx\033[0m %s\n' "$*" >&2; exit 1; }

# --- container engine
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose); ENGINE=docker
elif command -v docker-compose >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    COMPOSE=(docker-compose); ENGINE=docker
elif podman compose version >/dev/null 2>&1; then
    COMPOSE=(podman compose); ENGINE=podman
elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE=(podman-compose); ENGINE=podman
else
    die "no usable docker/podman compose found. Install docker (and add yourself
    to the docker group, or set up rootless docker) or podman."
fi
$ENGINE info >/dev/null 2>&1 || die "cannot talk to $ENGINE as $(id -un). For rootful
    docker: sudo usermod -aG docker $(id -un), then log out and back in.
    For rootless: dockerd-rootless-setuptool.sh install"

# --- who the app runs as inside the container
# Under rootless docker/podman the container's uid 0 is already your host user,
# so files land owned by you. Under rootful docker we have to pin the app to
# your uid explicitly or the bind-mounted state ends up owned by someone else.
if [ "$ENGINE" = podman ] && [ "$(id -u)" -ne 0 ]; then
    ROOTLESS=yes
elif $ENGINE info -f '{{.SecurityOptions}}' 2>/dev/null | grep -qi rootless; then
    ROOTLESS=yes
else
    ROOTLESS=no
fi
if [ "$ROOTLESS" = yes ]; then
    RUN_AS="0:0"
else
    RUN_AS="$(id -u):$(id -g)"
fi

# --- configuration
if [ ! -f .env ]; then
    say "Creating .env from .env.example"
    cp .env.example .env
fi
# The host-side knobs compose interpolates. Keep them in sync with what we
# work out below rather than trusting a stale .env.
set_env() {
    local key=$1 val=$2
    if grep -q "^${key}=" .env; then
        # | as the sed delimiter: values are paths.
        sed -i "s|^${key}=.*|${key}=${val}|" .env
    else
        printf '%s=%s\n' "$key" "$val" >> .env
    fi
}
get_env() { sed -n "s/^$1=//p" .env | tail -1; }

# --- state directory (elevation cache, SQLite, downloaded registers)
# The environment wins, then whatever .env already says, then your data home.
STATE_DIR=${PLOTTER_STATE_DIR:-$(get_env PLOTTER_STATE_DIR)}
STATE_DIR=${STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/plotter}
mkdir -p "$STATE_DIR/cache"

set_env PLOTTER_RUN_AS "$RUN_AS"
set_env PLOTTER_STATE_DIR "$STATE_DIR"
# Your own DEM tiles, mounted read-only at /dem. Default it to the state
# directory: docker creates a missing bind-mount source as root, and a
# root-owned directory appearing in the project is exactly what this install
# is meant to avoid.
DEM_DIR=${PLOTTER_DEM_DIR:-$(get_env PLOTTER_DEM_DIR)}
DEM_DIR=${DEM_DIR:-$STATE_DIR}
mkdir -p "$DEM_DIR"
set_env PLOTTER_DEM_DIR "$DEM_DIR"
# Port and server name stay as .env has them unless the environment overrides,
# which is how CI pins the name the WAF must answer for.
PUBLIC_PORT=${PLOTTER_PUBLIC_PORT:-$(get_env PLOTTER_PUBLIC_PORT)}
set_env PLOTTER_PUBLIC_PORT "${PUBLIC_PORT:-8088}"
PUBLIC_PORT=${PUBLIC_PORT:-8088}
SERVERNAME=${PLOTTER_SERVERNAME:-$(get_env PLOTTER_SERVERNAME)}
if [ -n "$SERVERNAME" ]; then set_env PLOTTER_SERVERNAME "$SERVERNAME"; fi
chmod 600 .env

FILES=(-f docker-compose.yml)
if [ "$DEV" = yes ]; then FILES+=(-f docker-compose.dev.yml); fi
compose() { "${COMPOSE[@]}" "${FILES[@]}" "$@"; }

say "Engine: $ENGINE (rootless: $ROOTLESS), app uid:gid $RUN_AS"
say "State:  $STATE_DIR"

if [ "$BUILD" = yes ]; then
    say "Building the app image from uv.lock"
    compose build
fi

if [ "$DEV" = yes ]; then
    # The WAF is excluded by a profile in dev, but a container left running
    # from a previous full-stack run still holds the published port, so the
    # dev app would come up unreachable behind a proxy pointing nowhere.
    compose rm -sf waf >/dev/null 2>&1 || true
fi

say "Starting the stack"
compose up -d --remove-orphans

say "Waiting for a health answer on :$PUBLIC_PORT"
# The WAF only answers for its configured ServerName, so send it.
HOSTHDR=()
if [ -n "$SERVERNAME" ]; then HOSTHDR=(-H "Host: $SERVERNAME"); fi
ok=no
for _ in $(seq 1 45); do
    if curl -fsS --max-time 5 "${HOSTHDR[@]}" \
        "http://127.0.0.1:$PUBLIC_PORT/api/health" >/dev/null 2>&1; then
        ok=yes; break
    fi
    sleep 2
done

if [ "$ok" != yes ]; then
    compose logs --tail 40 || true
    die "the stack did not answer on :$PUBLIC_PORT. It is still running so you
    can look at it: ${COMPOSE[*]} ${FILES[*]} logs -f"
fi

if [ "$DEV" = yes ]; then
    say "Plotter is up on http://127.0.0.1:$PUBLIC_PORT (no WAF, dev mode)"
else
    say "Plotter is up behind Apache + ModSecurity/CRS on :$PUBLIC_PORT"
fi
$ENGINE image prune -f >/dev/null 2>&1 || true
