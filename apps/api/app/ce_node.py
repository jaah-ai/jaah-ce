"""`python -m app.ce_node env [--native]` — print the community edition's node identity as three env lines.
`python -m app.ce_node home <absolute path> | --container` — record where the node keeps its pool (`nodes.jaah_home`).

Bare, the lines the container node reads (`app/ce_bootstrap.py::NODE_ENV_FILE`, CP `http://api:8000`).
`--native`, the same node dialling the control plane from the host: `JAAH_CP_BASE_URL` is
`http://127.0.0.1:<NATIVE_API_PORT>`, the port the edition's compose publishes on loopback. The installer
writes that output to `~/.config/jaah-ce/node.env` (0600). Prints the token: run it only where the
caller is the machine's owner.

`home`: every slot and clone path the control plane hands the node derives from its `jaah_home`. The boot starts
it at the container node's (`ce_bootstrap.CONTAINER_JAAH_HOME`); the native installer, whose home sits under
the installing user's data directory, records its own with this; its fallback to the container node records
`--container` (the boot's own value) again.
"""
from __future__ import annotations

import sys
from pathlib import PurePosixPath

from sqlalchemy import update

from app import edition, ce_bootstrap

#: The API port the community edition's compose publishes on 127.0.0.1 — the single home of that number.
NATIVE_API_PORT = 8000
NATIVE_CP_BASE_URL = f"http://127.0.0.1:{NATIVE_API_PORT}"

_USAGE = "usage: python -m app.ce_node env [--native] | home <absolute path> | home --container"


def env_lines(*, native: bool = False) -> str:
    """The node identity file's three lines, re-pointed at the host's loopback for `native`. Raises
    `RuntimeError` when the control plane has not booted yet (no identity file)."""
    held = ce_bootstrap.read_node_env()
    if not held.get("NODE_ID") or not held.get("NODE_TOKEN"):
        raise RuntimeError(
            f"no node identity at {ce_bootstrap.NODE_ENV_FILE} — start the control plane first"
        )
    cp_base_url = NATIVE_CP_BASE_URL if native else held.get("JAAH_CP_BASE_URL", ce_bootstrap.CONTAINER_CP_BASE_URL)
    return ce_bootstrap.node_env_lines(held["NODE_ID"], held["NODE_TOKEN"], cp_base_url)


def set_home(path: str) -> None:
    """Point the edition's one node at `path` (absolute, normalised); `ValueError` for any other shape, and
    `RuntimeError` outside the community edition or before its boot has created the node."""
    from app.db import SessionLocal
    from app.models.nodes import Node
    from app.tenancy import seam

    if not edition.IS_CE:
        raise RuntimeError("this command belongs to the community edition")
    home = PurePosixPath(path)
    if not home.is_absolute() or ".." in home.parts or any(ch in path for ch in "\0\n\r"):
        raise ValueError(f"the node's home must be an absolute path, not {path!r}")
    with seam.operator_scope(), SessionLocal() as session:
        result = session.execute(
            update(Node).where(Node.org_id == ce_bootstrap.CE_ORG_ID).values(jaah_home=str(home))
        )
        if not result.rowcount:
            raise RuntimeError("no node yet — start the control plane first")
        session.commit()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not edition.IS_CE:
        print("this command belongs to the community edition", file=sys.stderr)
        return 2
    if len(args) == 2 and args[0] == "home":
        try:
            set_home(ce_bootstrap.CONTAINER_JAAH_HOME if args[1] == "--container" else args[1])
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if not args or args[0] != "env" or any(arg != "--native" for arg in args[1:]):
        print(_USAGE, file=sys.stderr)
        return 2
    try:
        sys.stdout.write(env_lines(native="--native" in args[1:]))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
