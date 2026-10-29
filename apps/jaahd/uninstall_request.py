"""The node's half of SELF-HOSTED UNINSTALL: the CP's `uninstall` bus frame becomes one empty marker.

The CP pushes `uninstall` (`node_bus.FRAME_UNINSTALL`) after deleting a self-hosted host with "uninstall".
The unprivileged daemon cannot remove a package, so it writes `/run/jaah/requests/uninstall`;
`jaah-uninstall.path` starts the root `jaah-uninstall.service`, which deletes the marker and removes Jaah
ONLY once the CP refuses this node's token (the row is gone) — so a stray marker asks, never uninstalls a
registered node. The marker is EMPTY and never read (`marker_request.py` says why).

Stdlib only; every path is resolved lazily.
"""
from __future__ import annotations

import logging
from pathlib import Path

import marker_request

log = logging.getLogger("jaahd.uninstall_request")

# Under the node runtime root; `jaah-uninstall.path` watches it and `/usr/libexec/jaah-uninstall`
# removes it.
MARKER_PARTS = ("requests", "uninstall")
_MARKER_ENV = "JAAH_UNINSTALL_MARKER"


def marker_path() -> Path:
    """`$JAAH_UNINSTALL_MARKER`, else `<run root>/requests/uninstall`."""
    return marker_request.marker_path(_MARKER_ENV, MARKER_PARTS)


def requests_enabled() -> bool:
    """`marker_request.root_request_enabled` for the uninstall marker (the vnode rule is D7)."""
    return marker_request.root_request_enabled(_MARKER_ENV)


def request_uninstall(marker: Path | None = None) -> bool:
    """Ask the root path unit to remove Jaah from this machine. True iff the marker was written.

    Never raises: it runs on the node bus's read loop."""
    if not requests_enabled():
        log.info("uninstall-request: not on this node (virtual node, not Linux, or sandbox) — no request")
        return False
    target = marker_path() if marker is None else Path(marker)
    try:
        # `write_bytes(b"")`, not `touch()`: the marker must be EMPTY.
        target.write_bytes(b"")
    except OSError as exc:
        log.warning("uninstall-request: could not create %s: %s", target, exc)
        return False
    log.info("uninstall-request: the control plane deleted this node — wrote %s for jaah-uninstall.path", target)
    return True
