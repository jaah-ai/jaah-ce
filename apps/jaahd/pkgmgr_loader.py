"""`infra/jaah/pkgmgr.py`, loaded BY PATH: the daemon's one way to ask the package manager (apt or dnf) anything.

The bundle ships it beside `apps/` (the node bundle's manifest), and the dev tree has the same layout,
so `parents[2]` resolves on a node and in a clone alike. On macOS/Windows `kind()` is None and
`installed_version` returns None — callers read that as "no package". Loaded once per process.
"""
from __future__ import annotations

import functools
import importlib.util
from pathlib import Path
from types import ModuleType

PKGMGR_PY = Path(__file__).resolve().parents[2] / "infra" / "jaah" / "pkgmgr.py"


@functools.lru_cache(maxsize=None)
def load() -> ModuleType:
    """The `pkgmgr` module, executed once."""
    spec = importlib.util.spec_from_file_location("jaah_pkgmgr", PKGMGR_PY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{PKGMGR_PY} is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
