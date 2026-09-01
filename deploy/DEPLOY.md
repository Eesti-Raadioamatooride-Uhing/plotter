# Deploying Plotter to a server

Run these from your Windows machine, in the folder that contains this repo
(`Documents\ClaudeCode\Plotter`). Everything runs over your existing SSH key.

## 1. Copy it up

PowerShell:

```powershell
cd "$env:USERPROFILE\Documents\ClaudeCode\Plotter"
ssh root@172.16.0.74 "mkdir -p /opt/plotter-src"
scp -r * .env.example root@172.16.0.74:/opt/plotter-src/
```

If you have `rsync` (WSL or Git Bash), this is better — it skips caches and
repeats cleanly on every later update:

```bash
rsync -avz --delete \
    --exclude /venv --exclude /data --exclude __pycache__ --exclude .git \
    ./ root@172.16.0.74:/opt/plotter-src/
```

## 2. Install

```bash
ssh root@172.16.0.74
cd /opt/plotter-src
chmod +x deploy/install.sh
BIND=0.0.0.0 ./deploy/install.sh
```

`BIND=0.0.0.0` makes it reachable from the rest of your network, which is
what you want for a LAN server with no proxy in front. Drop it (or set
`BIND=127.0.0.1`) if you are putting nginx in front later.

The installer is idempotent — re-run it after every `rsync` to upgrade.

Then open **http://172.16.0.74:8088**.

## 3. Fill the caches

```bash
# Elevation. Estonia + Finland is ~180 Copernicus tiles, roughly 4 GB.
# Takes a while; run it in tmux or screen.
sudo -u plotter /opt/plotter/venv/bin/plotter-refresh warm

# Masts and towers from OSM and the Estonian ETAK database.
sudo -u plotter /opt/plotter/venv/bin/plotter-refresh refresh masts

# Check what the TTJA JVIS portal actually looks like before harvesting.
sudo -u plotter /opt/plotter/venv/bin/plotter-refresh discover
```

A nightly timer re-runs the data refresh at 03:20 with jitter.

## Troubleshooting

```bash
systemctl status plotter
journalctl -u plotter -n 100 --no-pager
journalctl -u plotter -f              # follow
systemctl list-timers plotter-refresh
```

Common ones:

**`rasterio` or `pyproj` fails to build.** The server is missing GDAL headers.
The installer apt-gets them, but on a minimal image you may also need
`python3-numpy` and `pkg-config`. On Debian 12 / Ubuntu 22.04+ the wheels are
prebuilt and no compilation should happen at all.

**Service starts then exits.** Almost always the config file. Check
`/etc/plotter/plotter.env` has `PLOTTER_HOST`, `PLOTTER_PORT` and
`PLOTTER_WORKERS` set — the systemd unit substitutes them into ExecStart, and
an empty value produces an unparseable command line.

**Reachable locally but not from your desktop.** Either `BIND` is still
127.0.0.1 (check `ss -lntp | grep 8088`), or the firewall:
`ufw allow from 172.16.0.0/24 to any port 8088`.

**Coverage runs time out through nginx.** Raise `proxy_read_timeout`; a
400 km fine-detail sweep can take a couple of minutes.

## Updating later

```bash
rsync -avz --delete --exclude /venv --exclude /data --exclude __pycache__ \
    ./ root@172.16.0.74:/opt/plotter-src/
ssh root@172.16.0.74 'cd /opt/plotter-src && BIND=0.0.0.0 ./deploy/install.sh'
```
