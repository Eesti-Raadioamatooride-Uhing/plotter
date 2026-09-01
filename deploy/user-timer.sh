#!/usr/bin/env bash
# Install the nightly data refresh as a systemd USER timer. No root involved.
#
#   ./deploy/user-timer.sh install    install, enable and start it
#   ./deploy/user-timer.sh status     show when it last ran and runs next
#   ./deploy/user-timer.sh run        run the refresh now, in the foreground
#   ./deploy/user-timer.sh remove     stop and uninstall it
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

engine_cmd() {
    if docker compose version >/dev/null 2>&1; then echo "/usr/bin/docker compose"
    elif command -v podman >/dev/null 2>&1; then echo "$(command -v podman) compose"
    else echo "/usr/bin/docker compose"; fi
}

case "${1:-install}" in
install)
    mkdir -p "$UNIT_DIR"
    sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$SRC|" \
        -e "s|^ExecStart=.*|ExecStart=$(engine_cmd) run --rm --no-deps app plotter-refresh refresh all --tolerant|" \
        "$SRC/deploy/systemd-user/plotter-refresh.service" > "$UNIT_DIR/plotter-refresh.service"
    install -m 644 "$SRC/deploy/systemd-user/plotter-refresh.timer" "$UNIT_DIR/plotter-refresh.timer"
    systemctl --user daemon-reload
    systemctl --user enable --now plotter-refresh.timer
    say "Installed to $UNIT_DIR"
    # Without lingering, user units stop when the last session closes, so a
    # headless server would never fire the timer. This does not need root:
    # polkit lets you enable lingering for yourself on a normal desktop or
    # server install. If it is refused, ask an admin for
    #   loginctl enable-linger $(id -un)
    if ! loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null | grep -q yes; then
        say "Enabling lingering so the timer fires without an open session"
        loginctl enable-linger "$(id -un)" || \
            echo "could not enable lingering. Ask an admin to run: loginctl enable-linger $(id -un)" >&2
    fi
    systemctl --user list-timers plotter-refresh --no-pager || true
    ;;
status)
    systemctl --user status plotter-refresh.timer --no-pager || true
    systemctl --user list-timers plotter-refresh --no-pager || true
    ;;
run)
    cd "$SRC"
    exec $(engine_cmd) run --rm --no-deps app plotter-refresh refresh all --tolerant
    ;;
remove)
    systemctl --user disable --now plotter-refresh.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/plotter-refresh.service" "$UNIT_DIR/plotter-refresh.timer"
    systemctl --user daemon-reload
    say "Removed"
    ;;
*)
    sed -n '2,8p' "$0" | sed 's/^# \?//'
    exit 2
    ;;
esac
