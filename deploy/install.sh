#!/usr/bin/env bash
# Plotter installer for a Debian/Ubuntu server. Idempotent: safe to re-run
# to upgrade an existing install.
set -euo pipefail

# BIND=0.0.0.0 makes it reachable from the rest of your network. Leave it
# at 127.0.0.1 if nginx or another proxy will sit in front.
BIND=${BIND:-127.0.0.1}
PORT=${PORT:-8088}
APP_DIR=${APP_DIR:-/opt/plotter}
STATE_DIR=${STATE_DIR:-/var/lib/plotter}
CONF_DIR=${CONF_DIR:-/etc/plotter}
USER=${PLOTTER_USER:-plotter}
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "run me as root" >&2; exit 1; }; }
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

need_root

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential \
    libgdal-dev gdal-bin libgeos-dev libproj-dev proj-data ca-certificates \
    rsync curl

if ! id -u "$USER" >/dev/null 2>&1; then
    say "Creating service user $USER"
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$USER"
fi

say "Installing application to $APP_DIR"
mkdir -p "$APP_DIR" "$STATE_DIR/cache" "$CONF_DIR"
# copy source, keeping the venv and any local data in place.
# Anchor these to the top level (/venv, /data) so we skip only the root
# cache and virtualenv -- an unanchored "data" would also drop the
# plotter/core/data package. __pycache__ stays unanchored on purpose.
rsync -a --delete --exclude /venv --exclude /data --exclude '__pycache__' \
      "$SRC"/ "$APP_DIR"/

if [ ! -d "$APP_DIR/venv" ]; then
    say "Creating virtualenv"
    python3 -m venv "$APP_DIR/venv"
fi
say "Installing Python dependencies"
"$APP_DIR/venv/bin/pip" install --upgrade -q pip wheel
"$APP_DIR/venv/bin/pip" install -q -e "$APP_DIR"

if [ ! -f "$CONF_DIR/plotter.env" ]; then
    say "Writing default configuration to $CONF_DIR/plotter.env"
    cp "$APP_DIR/.env.example" "$CONF_DIR/plotter.env"
    sed -i "s#^PLOTTER_DATA_DIR=.*#PLOTTER_DATA_DIR=$STATE_DIR#" "$CONF_DIR/plotter.env"
    sed -i "s#^PLOTTER_CACHE_DIR=.*#PLOTTER_CACHE_DIR=$STATE_DIR/cache#" "$CONF_DIR/plotter.env"
    sed -i "s#^PLOTTER_DATABASE_URL=.*#PLOTTER_DATABASE_URL=sqlite:///$STATE_DIR/plotter.sqlite#" "$CONF_DIR/plotter.env"
    chmod 640 "$CONF_DIR/plotter.env"
fi
# These three drive the systemd ExecStart line, so they must always be present.
sed -i "s#^PLOTTER_HOST=.*#PLOTTER_HOST=$BIND#" "$CONF_DIR/plotter.env"
sed -i "s#^PLOTTER_PORT=.*#PLOTTER_PORT=$PORT#" "$CONF_DIR/plotter.env"
grep -q '^PLOTTER_WORKERS=' "$CONF_DIR/plotter.env" || echo "PLOTTER_WORKERS=4" >> "$CONF_DIR/plotter.env"

chown -R "$USER:$USER" "$STATE_DIR" "$CONF_DIR"
chown -R root:root "$APP_DIR"

say "Installing systemd units"
install -m 644 "$APP_DIR/deploy/plotter.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/plotter-refresh.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/plotter-refresh.timer" /etc/systemd/system/
systemctl daemon-reload
# enable for boot, then restart so an upgrade actually loads the new code
# (enable --now is a no-op on an already-running service).
systemctl enable plotter.service plotter-refresh.timer >/dev/null 2>&1 || true
systemctl restart plotter.service
systemctl start plotter-refresh.timer

say "Waiting for the service to answer"
CHECK_HOST=$BIND
[ "$BIND" = "0.0.0.0" ] && CHECK_HOST=127.0.0.1
UP=no
for i in $(seq 1 30); do
    if curl -fsS "http://$CHECK_HOST:$PORT/api/health" >/dev/null 2>&1; then
        UP=yes
        say "Plotter is up on http://$(hostname -I | awk '{print $1}'):$PORT"
        break
    fi
    sleep 1
done
if [ "$UP" != yes ]; then
    echo "Service did not answer in 30s. Check: journalctl -u plotter -n 50" >&2
    systemctl --no-pager status plotter || true
    exit 1
fi

cat <<EOF

Next steps
  1. Put nginx (deploy/nginx.conf) in front of it if this is public facing.
  2. Warm the elevation cache for the region you care about:
       sudo -u $USER $APP_DIR/venv/bin/plotter-refresh warm
     Estonia + Finland is about 180 Copernicus tiles, roughly 4 GB.
  3. Pull in the mast and register data now rather than waiting for 03:20:
       sudo -u $USER $APP_DIR/venv/bin/plotter-refresh refresh all
  4. Check the frequency register scraper against the live portal:
       sudo -u $USER $APP_DIR/venv/bin/plotter-refresh discover

Logs:    journalctl -u plotter -f
Config:  $CONF_DIR/plotter.env
State:   $STATE_DIR
EOF
