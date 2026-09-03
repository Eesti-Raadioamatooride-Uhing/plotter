"""Keep the test run out of the developer's own configuration.

`.env` in the repo root is the container's configuration (it points at /data),
and pydantic-settings reads it at import time. Without this, running the tests
on the host tries to create /data and dies before collecting anything.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="plotter-tests-")
os.environ.setdefault("PLOTTER_ENV_OVERRIDDEN", "1")
os.environ["PLOTTER_DATA_DIR"] = _tmp
os.environ["PLOTTER_CACHE_DIR"] = os.path.join(_tmp, "cache")
os.environ["PLOTTER_DATABASE_URL"] = f"sqlite:///{os.path.join(_tmp, 'test.sqlite')}"
