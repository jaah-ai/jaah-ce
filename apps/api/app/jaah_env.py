"""app/jaah_env.py — the env read and the env-flag parse the api shares with the node daemon.

The one home is the daemon's `apps/jaahd/jaah_env.py`. This module
LOADS it by path (`app/shared_module.py`: the api image COPYs it to `/app/shared/jaah_env.py`
per the API image's Dockerfile; the local stack mounts it read-only) and re-exports it, never a second copy.
"""
from __future__ import annotations

from app import shared_module

_home = shared_module.load("jaah_env", shared_module.candidates("jaah_env.py", "apps/jaahd"),
                           consequence="; the renamed-variable reads need it")

env_get = _home.env_get
TRUTHY = _home.TRUTHY
env_flag_enabled = _home.env_flag_enabled
insecure_agent_enabled = _home.insecure_agent_enabled
