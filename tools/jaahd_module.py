"""tools/jaahd_module.py — load a daemon module (`apps/jaahd/<file>`) by path, so `tools/` keeps no mirror of it.

`tools/` and `apps/jaahd/` sit beside each other everywhere a tools script runs — the checkout, the .deb
bundle (`/usr/lib/jaahd/tools` beside `/usr/lib/jaahd/apps/jaahd`) and the
macOS/Windows bundles (the MANIFEST closure at repo-relative paths) — but neither
tree is a package the other can import by name. Hand-kept copies (`agent_socket`, `runner_env`, `jaah_env`)
drifted, and a drift test only reports the drift. Loading the daemon's file by path makes it the single home;
`tools/<name>.py` is then a facade that binds the daemon module's names. Stdlib only.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

JAAHD_DIR = Path(__file__).resolve().parents[1] / "apps" / "jaahd"


def load(filename: str, module_name: str, *, siblings: bool = False) -> ModuleType:
    """`apps/jaahd/<filename>` as `sys.modules[module_name]` (loaded once; the cached object afterwards).

    `siblings` puts the daemon tree on `sys.path` (appended, so it never shadows a tools module) for a
    daemon module that imports its own siblings by bare name (`environment`, `host_platform`)."""
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    if siblings and str(JAAHD_DIR) not in sys.path:
        sys.path.append(str(JAAHD_DIR))
    path = JAAHD_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None or not path.is_file():
        raise ImportError(f"no daemon module at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # before exec: dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(module)
    except BaseException:  # a half-initialised module must not be served to the next importer
        del sys.modules[module_name]
        raise
    return module
