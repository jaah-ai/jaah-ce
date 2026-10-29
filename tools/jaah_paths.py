#!/usr/bin/env python3
"""Where this host keeps jaah-managed state — the `tools/` side of the JAAH_HOME layout.

Single home for path resolution across `tools/` and the portfolio engine; the LAYOUT is single-homed in
`apps/jaahd/env_helpers.py` (`_LAYOUT`). Duplicated, not imported: the two deployables share no package.

SOFT default, unlike `env_helpers.jaah_home()` which raises: these tools run from cron with no shell
profile, so an unset `JAAH_HOME` is normal here. Nothing in this module reads credentials, which is
why the daemon's no-fallback rule does not apply.
"""
from __future__ import annotations

import os
from pathlib import Path

# Every host's home: a pre-rename home was moved here and its old name left a link (by the host
# installer, and by the package's home-rename script).
JAAH_HOME_DIRNAME = ".jaah"


def jaah_root() -> Path:
    """`$JAAH_HOME` when set (and non-blank), else `~/.jaah`.

    `or`, not `.get(k, default)`: a set-but-EMPTY value must count as unset (as bash `${X:-…}` does),
    else every path becomes relative to cwd."""
    value = (os.environ.get("JAAH_HOME") or "").strip()
    if value:
        return Path(value)
    return Path.home() / JAAH_HOME_DIRNAME


def log_dir(*parts: str) -> Path:
    """`$FLEET_LOG_DIR` when set, else `<root>/log`. Append-only text a human reads: `log/cron`,
    `log/daemon`, `log/hook`, `log/agent`, `log/runner`."""
    override = (os.environ.get("FLEET_LOG_DIR") or "").strip()
    root = Path(override) if override else jaah_root() / "log"
    return root.joinpath(*parts)


def cache_dir(*parts: str) -> Path:
    """`<root>/cache/...` — derivable state, safe to delete at any time (cookie jars, guard caches)."""
    return jaah_root().joinpath("cache", *parts)


def state_dir(*parts: str) -> Path:
    """`$FLEET_STATE_DIR` when set, else `<root>/state` — durable markers a later pass reads back.
    Distinct from `log_dir` so a scratch log dir cannot re-arm an idempotency stamp."""
    override = (os.environ.get("FLEET_STATE_DIR") or "").strip()
    root = Path(override) if override else jaah_root() / "state"
    return root.joinpath(*parts)
