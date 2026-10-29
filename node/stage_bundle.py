#!/usr/bin/env python3
"""stage_bundle.py — stage the node bundle a community-edition node runs, from the public tree.

  <python> stage_bundle.py <public-tree> <dest> [<interpreter>]

The one staging step behind both the node image (Dockerfile.node) and the native installer (jaah-ce-node). The bundle
is the node closure below plus the doctrine a node reads (`_DOCTRINE`), copied from a tree the edition's build already
made: every file in it passed the build's filter and the edition is already written, so nothing here reads what the
build read. From the Jaah monorepo, build the public tree first (its community-edition README) and stage from that.

The bundle is relocatable: a community-edition daemon finds its root from its own location (`host_platform.linux
._ce_paths`). Its runner-facing clients go in `<dest>/bin`, each a launcher into the daemon tree under
`<interpreter>` — the Python the node runs under; by default the one running THIS script (`sys.executable`). The
path is only written into the launchers, so it need not exist where this script runs: the node image passes its
venv's interpreter, built later from the bundle's own `packaging/jaahd-venv`. This script's own directory is copied
to `<dest>/node`. Stdlib only.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# The node closure: the daemon (its hash lock `apps/jaahd/requirements.txt` with it), the tools/ modules its forge CLI
# loads by path (`forge_cli/policy.py`; the tree ships only those), the pins and locks its CLI installer reads
# (`apps/jaahd/runner_cli_pins.py`), and the venv builder that installs the lock (`packaging/jaahd-venv`, the script
# the fleet package's postinst runs; the node image runs it over the staged bundle).
# Nothing from the fleet host provisioning: a local node renders its clients below instead of provisioning them, and
# runs without the fleet harness (`agents.base.read_harness`).
_CLOSURE = ("apps/jaahd", "tools", "packaging/platform-pins.toml", "packaging/runner-cli-locks", "packaging/jaahd-venv",
            "packages/acp-forks")
# The debris a tree's own runs leave (a checkout's caches): no git here.
_SKIPPED_PARTS = {"tests", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".omc"}

# The doctrine a node reads, by its public-tree path -> its bundle path: the subagents a Claude runner is given
# (`agents/agent_layer.py::place_subagents`) and the context-architecture policy the onboarding role reads at the
# package's `docs/policies/` (`app/dispatch_compose/doctrine.py`).
_DOCTRINE = {"doctrine/agents/": "doctrine/agents/",
             "doctrine/context-architecture.md": "docs/policies/context-architecture.md",
             # Not doctrine, but placed the same way: the package-manager seam the daemon loads by path
             # (`pkgmgr_loader.py`), at its fleet path. The tree ships it under `toolchain/`.
             "toolchain/pkgmgr.py": "infra/jaah/pkgmgr.py"}

# `<dest>/bin/<name>` -> the `module:function` it runs, argv after the name passed through. The fleet node's twins
# are the package's and its provisioner's renders; the macOS package ships the same set.
# `account_auth.CREDENTIAL_HELPER` names `bin/jaah-credential` on a local node.
LAUNCHERS = {
    "jaah-credential": "windows_clis:credential_main",
    "jaah-call": "windows_clis:call_main",
    "jaah-forge": "forge_cli.cli:main",
    "jaah-acp-host": "acp.host:main",
    "jaah": "jaah_cli:main",
}


def launcher(python: str, target: str) -> str:
    """A `bin/` launcher: `target`'s function under `python` with `-I` (no PYTHONPATH, user site or script dir can
    shadow the tree), the daemon tree found beside the launcher's own directory, so the bundle can move."""
    module, func = target.split(":")
    return (
        "#!/bin/sh\n"
        f"# Rendered by stage_bundle.py: {module}.{func} from this bundle's daemon tree.\n"
        f"exec {_shq(python)} -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); "
        f"from {module} import {func} as main; raise SystemExit(main(sys.argv[1:]))' "
        '"${0%/*}/../apps/jaahd" "$@"\n'
    )


def _shq(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _tree_files(source: Path, top: str):
    root = source / top
    for path in sorted(root.rglob("*")) if root.is_dir() else [root] if root.is_file() else []:
        relative = path.relative_to(source)
        if path.is_file() and not _SKIPPED_PARTS.intersection(relative.parts):
            yield relative.as_posix(), path


def stage(source: Path, dest: Path, python: str) -> None:
    edition = source / "apps" / "jaahd" / "edition.py"
    if not (source / "node" / "stage_bundle.py").is_file() or not (source / "doctrine").is_dir() \
            or not edition.is_file() or 'EDITION: str = "ce"' not in edition.read_text(encoding="utf-8"):
        raise SystemExit(f"stage_bundle: {source} is not a built community-edition tree — build it first")
    for top in _CLOSURE:
        for rel, path in _tree_files(source, top):
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
    for published, path in [*_tree_files(source, "doctrine"), *_tree_files(source, "toolchain/pkgmgr.py")]:
        prefix = next((p for p in _DOCTRINE if published.startswith(p)), None)
        if prefix is None:
            continue
        out = dest / (_DOCTRINE[prefix] + published[len(prefix):])
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
    bin_dir = dest / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, target in LAUNCHERS.items():
        (bin_dir / name).write_text(launcher(python, target), encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    shutil.copytree(Path(__file__).resolve().parent, dest / "node", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    stage(Path(argv[0]).resolve(), Path(argv[1]), argv[2] if len(argv) == 3 else sys.executable)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
