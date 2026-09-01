"""Coverage runs stored on disk, so they outlive the request that made them.

Two things forced this. A fine-detail 250 km sweep is around 360 000 ITM
solves and measured over 100 s, which no proxy, CDN or browser will hold a
connection open for, so the work has to happen in the background while the
browser polls. And the app runs several uvicorn workers, so anything kept in
a module-level dict is visible to roughly one request in four: the rendered
PNG used to 404 about half the time because the image request landed on a
worker that had never seen that run.

Files in the state directory solve both. Any worker can read them, they
survive a restart, and there is no broker to deploy.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"

# Keep the last few runs so a user can flip back to an earlier overlay, and
# so a reload does not have to recompute. Each run is a PNG plus a little
# JSON, so this is megabytes, not gigabytes.
MAX_RUNS = 32


class Store:
    """A directory of coverage runs, one subdirectory per request hash."""

    def __init__(self, root: Path):
        self.root = Path(root) / "coverage-runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, key: str) -> Path:
        # Keys are our own hex digests, but never build a path from caller
        # input without checking it.
        if not key.isalnum() or len(key) > 64:
            raise ValueError("bad run key")
        return self.root / key

    # --- writing ---------------------------------------------------------

    def _write(self, path: Path, data: bytes) -> None:
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def claim(self, key: str) -> bool:
        """Try to become the worker that computes this run.

        mkdir is atomic, so exactly one caller wins even across processes.
        """
        d = self._dir(key)
        try:
            d.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return False
        self.set_status(key, STATE_RUNNING, progress=0.0)
        return True

    def set_status(self, key: str, state: str, *, progress: float = 0.0,
                   error: str = "") -> None:
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        self._write(d / "status.json", json.dumps({
            "state": state, "progress": round(float(progress), 3),
            "error": error, "updated": time.time(),
        }).encode())

    def finish(self, key: str, payload: dict, png: bytes) -> None:
        d = self._dir(key)
        self._write(d / "image.png", png)
        self._write(d / "result.json", json.dumps(payload).encode())
        self.set_status(key, STATE_DONE, progress=1.0)
        self.prune()

    def fail(self, key: str, message: str) -> None:
        self.set_status(key, STATE_ERROR, error=message)

    # --- reading ---------------------------------------------------------

    def status(self, key: str) -> dict | None:
        try:
            raw = (self._dir(key) / "status.json").read_bytes()
        except (OSError, ValueError):
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def result(self, key: str) -> dict | None:
        try:
            return json.loads((self._dir(key) / "result.json").read_bytes())
        except (OSError, ValueError):
            return None

    def png(self, key: str) -> bytes | None:
        try:
            return (self._dir(key) / "image.png").read_bytes()
        except (OSError, ValueError):
            return None

    # --- housekeeping ----------------------------------------------------

    def prune(self, keep: int = MAX_RUNS) -> None:
        try:
            runs = sorted(
                (p for p in self.root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in runs[keep:]:
            shutil.rmtree(old, ignore_errors=True)

    def reap_stale(self, max_age_s: float = 3600.0) -> None:
        """Clear runs left 'running' by a worker that died mid-sweep.

        Without this the claim directory would sit there forever and every
        later request for the same parameters would poll a job nobody is
        computing.
        """
        now = time.time()
        try:
            runs = [p for p in self.root.iterdir() if p.is_dir()]
        except OSError:
            return
        for d in runs:
            st = self.status(d.name)
            if (st and st.get("state") == STATE_RUNNING
                    and now - st.get("updated", 0) > max_age_s):
                log.warning("reaping stale coverage run %s", d.name)
                shutil.rmtree(d, ignore_errors=True)
