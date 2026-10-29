"""Read the runner identity (`CONTROL_TASK_ID`, `CONTROL_RUNNER_SESSION_ID`) from the inherited env.

`apps/jaahd/runner_env.py` is the home (including why a blank value is absent, never a blank identity);
this facade loads it by path (`tools/jaahd_module.py`) and binds its names, so `import runner_env` from
a tools script stays as it was. Its `docker_env` imports `host_platform` lazily, hence `siblings`."""
from __future__ import annotations

import sys
from pathlib import Path

# Loaded by path too (the suites, a hook run by installed path): then nothing put tools/ on sys.path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import jaahd_module  # noqa: E402 — sibling module

_daemon = jaahd_module.load("runner_env.py", "_jaahd_runner_env", siblings=True)

task_id = _daemon.task_id
runner_session_id = _daemon.runner_session_id
docker_env = _daemon.docker_env
LIB_ROOT_ENV = _daemon.LIB_ROOT_ENV
DOCKER_HOST_ENV = _daemon.DOCKER_HOST_ENV
PACKAGED_LIB_PREFIX = _daemon.PACKAGED_LIB_PREFIX
rehome_lib_paths = _daemon.rehome_lib_paths
lib_root_relocated = _daemon.lib_root_relocated
python_cmd = _daemon.python_cmd
rehome_python = _daemon.rehome_python
