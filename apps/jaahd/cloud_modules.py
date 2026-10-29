"""The node's cloud-only modules, reached by name: MicroVM, NodeLink, the GHA runner pool, the apt self-upgrade,
the virtual-node host and the Architectural Guard. Stdlib only.

The community edition's public tree leaves them out (`tools/community_edition/manifest.toml` `exclude`), and its
`check_tree.py` fails any shipped module that imports one by an import statement. So a shipped module asks
`load(name)` instead: the module wherever it ships (every cloud node), None where the tree left it out (a
community-edition node), and the caller does without it. The cloud node's own steps, command handlers and bus
callbacks live in `cloud_node.py`, which the daemon discovers the same way.
"""
from __future__ import annotations

import importlib
from typing import Any


def load(name: str) -> Any:
    """Module NAME (dotted for a submodule), or None when this tree does not carry it. A module that is present
    but fails its own import raises as any import would: only NAME itself being absent reads as "not here"."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name is None or not (name == exc.name or name.startswith(f"{exc.name}.")):
            raise
        return None
