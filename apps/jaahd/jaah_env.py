"""jaah_env.py — the one read of a `JAAH_*` / `JAAHD_*` environment variable, and the shared env-flag parse.

The single home. `tools/jaah_env_names.py` is a facade that loads this file by path (`tools/jaahd_module.py`),
and `apps/api/app/jaah_env.py` borrows it by path too: the three trees share no package, but share this file.
Also here, for the same reason: the one parse of a boolean opt-in (`env_flag_enabled`) and the local-dev auth
bypass read through it (`insecure_agent_enabled`), which the api (`app/dev_bypass.py`) and the node
(`env_helpers`, `node_frame_auth`) MUST agree on — the API's check and the node's re-check, now one function.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional


def env_get(name: str, default: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """`name`'s value when it is PRESENT (even empty), else `default`; `environ` defaults to the process env."""
    env = os.environ if environ is None else environ
    return env.get(name, default)


# ── Environment-flag truthiness ──────────────────────────────────────────────────────────────────────

# What counts as ON for a boolean env var: an ALLOWLIST (see `env_flag_enabled`), compared stripped+lower.
TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag_enabled(raw: Optional[str]) -> bool:
    """Is one env var's RAW value an opt-IN? Only a `TRUTHY` spelling is, compared after strip+lower.

    Fail CLOSED: it gates a flag whose ON state turns an auth check OFF, so every unrecognised value
    (unset, typo, `disabled`, fullwidth `０`) leaves the surface closed.
    """
    return (raw or "").strip().lower() in TRUTHY


def insecure_agent_enabled() -> bool:
    """The local-dev auth bypass: `ALLOW_INSECURE_AGENT` read through `env_flag_enabled`, at call time.

    Nothing configured outranks this flag: the api's `node_credentials._unconfigured_bypass` honours it whenever
    nothing else authenticated the caller and the process is local, and the node's `node_frame_auth.frame_auth_ok`
    asks it. The api's boot refusal (`app/dev_bypass.py::assert_agent_auth_posture`) is what keeps it off a
    deployed stack; a node daemon legitimately runs with `APP_ENV` unset, so it has none.
    """
    return env_flag_enabled(os.environ.get("ALLOW_INSECURE_AGENT"))
