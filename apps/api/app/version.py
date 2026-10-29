"""App version string."""
from __future__ import annotations

import os
from datetime import datetime, timezone


def _file_version() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    roots = ["/app", here]
    p = here
    for _ in range(6):
        p = os.path.dirname(p)
        roots.append(p)
    for root in roots:
        try:
            with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        if text:
            return text
    return None


def resolve_version() -> str:
    return os.environ.get("APP_VERSION") or _file_version() or "0.0.0"


TOOL_VERSION = resolve_version()


def stamped_release() -> str | None:
    """The release nodes are meant to run — this control plane's own version — or None for an unstamped build
    (`0.0.0`), which names no published bundle. Read by `GET /api/agent/release-desired` and the macOS
    installer (`app/node_bootstrap.py`)."""
    if not TOOL_VERSION or TOOL_VERSION == "0.0.0":
        return None
    return TOOL_VERSION

# When this process began serving TOOL_VERSION; `GET /api/agent/release-desired` pairs the two, and the
# STALE_RELEASE grace window reads it (app/node_liveness.py). A CP's version changes only on a restart, so
# process start is the change time.
PROCESS_STARTED_AT = datetime.now(timezone.utc)
