"""tools/jaah_env_names.py — the daemon's env read (`env_get`), for `tools/`.

The rules and their why are the home's,
`apps/jaahd/jaah_env.py`, loaded here by path through `tools/jaahd_module.py` under its own name `jaah_env`
(the daemon's `environment.py` imports it by that name, so one object serves both trees). Named apart here
so a `tools/` import is never mistaken for the daemon's own `jaah_env`.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Loaded by path too (the suites, a hook run by installed path): then nothing put tools/ on sys.path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
import jaahd_module  # noqa: E402 — sibling module

_daemon = jaahd_module.load("jaah_env.py", "jaah_env")

env_get = _daemon.env_get
