"""Load a file another deployable owns, BY PATH — the one loader behind every module the api borrows.

A rule single-homed outside `apps/api` (the node's provider catalogue and redaction pass, the agent-manifest
contract in `tools/`) is never forked here and never reached through `sys.path`, which must not expose the
other tree's modules. Each borrower names its candidates, first match wins:

    /app/shared/<file>   the api image (its Dockerfile COPYs it) and the local stack (a
                         read-only single-file mount at the same path, in the compose file)
    <repo>/<home>/<file> a host-run pytest, or a build stage laid out like the repo

None present is a DEPLOYMENT DEFECT: `load` raises the borrower's `missing` error naming every path tried.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

#: The checkout root on a host run; in the image `/app/app` has no repo above it and the root is `/`.
REPO = Path(__file__).resolve().parents[min(3, len(Path(__file__).resolve().parents) - 1)]
SHARED = Path("/app/shared")


def candidates(filename: str, home: str) -> tuple[Path, ...]:
    """Where `home/filename` (repo-relative) is looked for, in order."""
    return (SHARED / filename, REPO / home / filename)


def load(
    module_name: str, paths: tuple[Path, ...], missing: type[Exception] = ImportError, consequence: str = ""
) -> ModuleType:
    """Execute the first of `paths` that exists as `module_name`, once. Registered in `sys.modules` BEFORE
    execution, as `spec_from_file_location` documents: `@dataclass` looks its module up by name. `consequence`
    ends the `missing` error's sentence: what the borrower refuses to do without the file."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    for path in paths:
        spec = importlib.util.spec_from_file_location(module_name, path) if path.is_file() else None
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[module_name]
            raise
        return module
    raise missing(
        f"{paths[0].name} is at none of " + ", ".join(str(p) for p in paths)
        + " — the api image must COPY it (infra/local/Dockerfile.api)" + consequence
    )
