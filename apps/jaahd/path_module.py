"""path_module.py — the daemon's one way to execute a file BY PATH as a module.

For a file that is not importable by name: a hook (`hooks/` has no `__init__.py`), a guard script whose name
is not an identifier (`guards/install-access-guard.py`), the hook dispatcher at its installed path, or a file
another tree ships beside the daemon (`infra/jaah/pkgmgr.py`, `infra/jaah/node_reprovision.py`, the GitHub
Actions runner installer). Each borrower names the path and the module name; the caller caches
the module when the file should execute once (`functools.lru_cache`). Four files keep their own copy on purpose,
because they run where this tree is not on `sys.path`: `jaah_cli.py` (alone at /usr/bin) and the hook and guard
scripts installed into a clone (`hooks/autoclose-check.py`, `hooks/runner-status.py`, `guards/guard-pre-push.py`,
`guards/access-guard-pre-push.py`).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load(path: Path, name: str, *, error: type[Exception] = RuntimeError) -> ModuleType:
    """`path` executed as the module `name`; `error` (its message naming the path) when it cannot be loaded."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise error(f"{path} is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
