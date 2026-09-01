# Deploying Plotter

Everything runs as an ordinary user in containers. No sudo, no system Python,
no root systemd units. The only privileged thing you need is access to a
container engine: rootless Docker or Podman need nothing, and rootful Docker
needs your user in the `docker` group (a one-time `sudo usermod -aG docker
$USER`, done by whoever administers the box).

## 1. Copy it up

From your Windows machine, in the folder that contains this repo:

```powershell
scp -r * .env.example plotter@172.16.0.74:~/plotter/
```

With `rsync` (WSL or Git Bash) it repeats cleanly on every later update:

```bash
rsync -avz --delete --exclude .venv --exclude /data --exclude __pycache__ \
    --exclude .git --exclude .env \
    ./ plotter@172.16.0.74:~/plotter/
```

Note the user: `plotter@`, not `root@`.

## 2. Run it

```bash
ssh plotter@172.16.0.74
cd ~/plotter
make up          # or ./deploy/deploy.sh, same thing
```

That builds the image from `uv.lock`, writes a `.env` if there is none, and
brings up two containers:

- `app`, the FastAPI service, on the internal network only, running as your
  own uid so the state directory stays yours
- `waf`, Apache with ModSecurity v3 and the OWASP Core Rule Set, the only
  thing published to the host, on `:8088`

State (elevation cache, SQLite, harvested registers) lives in
`~/.local/share/plotter`. Override it with `PLOTTER_STATE_DIR` before the
first run, or edit `.env` afterwards.

Then open **http://172.16.0.74:8088**. The WAF answers only for the name in
`PLOTTER_SERVERNAME`, so from another machine either use that hostname or set
it to your own.

For local work without the WAF, published on loopback only:

```bash
make dev                        # http://127.0.0.1:8088
```

`deploy.sh` is idempotent. Re-run it after every rsync or pull to upgrade.

## 3. Fill the caches

The maintenance CLI runs in the same image, so there is no venv to activate:

```bash
cd ~/plotter

# Elevation. Estonia + Finland is ~180 Copernicus tiles, roughly 4 GB.
# Takes a while, so run it in tmux or screen.
make warm

# Masts and towers from OSM and the Estonian ETAK database.
make refresh

# Check what the TTJA JVIS portal actually looks like before harvesting.
make discover
```

Each of those is a one-off container running the same image
(`docker compose run --rm --no-deps app plotter-refresh ...`), so there is no
venv to activate and nothing installed on the host.

For the nightly refresh, install the systemd **user** timer (03:20 with
jitter, same as before, but owned by you rather than by root):

```bash
make timer                         # also enables lingering so it fires headless
make timer-status
./deploy/user-timer.sh run         # run it once, now, in the foreground
```

## Migrating off the old root install

The venv + systemd install is gone. On a box that still has it:

```bash
sudo systemctl disable --now plotter.service plotter-refresh.timer
sudo rm -f /etc/systemd/system/plotter.service \
           /etc/systemd/system/plotter-refresh.{service,timer}
sudo systemctl daemon-reload

# Keep the elevation cache. It is several GB and slow to rebuild.
sudo cp -a /var/lib/plotter/. ~/.local/share/plotter/
sudo chown -R "$USER:$USER" ~/.local/share/plotter

# Old settings worth carrying over (admin key, API keys, LiDAR dirs).
sudo cat /etc/plotter/plotter.env
```

Then edit `.env` to match, keeping the container paths (`/data`, port 8000) as
`.env.example` has them, and `sudo rm -rf /opt/plotter /etc/plotter` once the
new stack answers.

## Troubleshooting

```bash
make ps
make logs                           # follow both containers
make health
docker compose logs waf | grep ModSecurity
make timer-status
```

Common ones:

**`permission denied while trying to connect to the Docker daemon socket`.**
Your user is not in the `docker` group and rootless Docker is not set up. One
of: `sudo usermod -aG docker $USER` then log out and back in, or
`dockerd-rootless-setuptool.sh install`.

**The app cannot write `/data`.** `PLOTTER_RUN_AS` does not match the owner of
`PLOTTER_STATE_DIR`. Re-run `./deploy/deploy.sh`, which works it out from the
engine: your own uid:gid for rootful Docker, `0:0` for rootless Docker and
Podman (where container root is already you).

**Reachable locally but not from your desktop.** Either the request is not
sending the `Host` the WAF expects (`PLOTTER_SERVERNAME` in `.env`), or the
firewall: `sudo ufw allow from 172.16.0.0/24 to any port 8088`.

**A legitimate request gets a 403.** That is the Core Rule Set. Find the rule
id in `docker compose logs waf`, then either raise `ANOMALY_INBOUND` or add a
`SecRuleRemoveById` exclusion in the `waf` service environment.

**The browser says "NetworkError when attempting to fetch resource".** A long
request was cut by something in the middle, which fetch reports as a bare
network error with no status code. The web UI no longer makes long requests:
it calls `POST /api/coverage/start` and polls `GET /api/coverage/job/{id}`,
so the longest request in a fine-detail 250 km sweep is about 0.13 s instead
of 162 s. Nothing in the chain (Apache, Cloudflare's 100 s origin limit, the
browser) can time it out any more.

If you still see it, you are on an old bundle. Hard-reload the page, and check
that `POST /api/coverage/start` appears in the network tab rather than a
single long `POST /api/coverage`.

The synchronous `POST /api/coverage` is still there for scripts, and it is
still slow by nature: `Timeout` and `PROXY_TIMEOUT` on the `waf` service are
both 600 s for that reason. The CRS image sets `Timeout` to 60 by default,
which is separate from `PROXY_TIMEOUT` and easy to miss.

Picking a station pre-fetches the elevation tiles in the background, which
takes the download out of the run. To warm a region up front instead:

```bash
make warm                          # the whole Estonia + Finland box
curl -X POST http://127.0.0.1:8088/api/terrain/warm \
     -H 'Content-Type: application/json' \
     -d '{"lat":58.38,"lon":26.72,"radius_km":250}'   # repeat until remaining is 0
```

Warm and coarse is fast: the same area at 60 km and normal detail answers in
3.7 s.

**Rebuild is not picking up my changes.** `deploy.sh` builds every time unless
you pass `--no-build`. Dependencies are cached on `uv.lock`, so a source-only
change rebuilds in seconds.

## Updating later

```bash
rsync -avz --delete --exclude .venv --exclude /data --exclude __pycache__ \
    --exclude .git --exclude .env ./ plotter@172.16.0.74:~/plotter/
ssh plotter@172.16.0.74 'cd ~/plotter && ./deploy/deploy.sh'
```

Pushing to `main` does the same thing through the self-hosted runner (see
`.github/workflows/deploy.yml`). The runner user needs container access, and
nothing else.

## Using your own DEM tiles

The elevation tiles the app downloads are already cached on disk under
`PLOTTER_STATE_DIR` (`cache/copernicus` and `cache/maaamet-wcs`) and are
reused from there, so a warm area costs no network at all. A typical Estonian
install ends up a few GB.

If you would rather feed it national LiDAR instead of the 30 m Copernicus
fallback, download the Maa-amet 1 m or MML 2 m tiles once, build a `.vrt` over
them, and point the app at it:

```bash
# on the host
mkdir -p ~/dem/maaamet && cd ~/dem/maaamet
# ... download tiles ...
gdalbuildvrt maaamet.vrt *.tif
```

```bash
# in .env
PLOTTER_DEM_DIR=/home/plotter/dem       # mounted read-only at /dem
PLOTTER_LIDAR_EE_DIR=/dem/maaamet       # a path INSIDE the container
```

Then `./deploy/deploy.sh`. `/api/health` lists the providers in use, and every
link and coverage result reports which one answered in `terrain_source`.

This will not make a coverage sweep faster. On a warm cache the time is ITM
solving, not fetching: a fine-detail 250 km sweep is about 360 000 solves and
takes roughly 100 s whatever the terrain source. It buys accuracy, which is
what matters for microwave paths and tree lines.

## TLS

The WAF speaks plain HTTP on `:8088` and expects something in front of it
(Cloudflare origin pull, on this deployment). `deploy/nginx.conf` is a
reference config if you would rather terminate TLS on the host with nginx and
Let's Encrypt. That part does need root, because it is a system service.
