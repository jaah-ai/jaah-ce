"""Which changed files a PR's line counts leave out: lockfiles, vendored trees, build output, generated code.

ONE pattern list (an operator decision). An excluded file still counts in
`files_changed`, and its lines land in `excluded_additions`/`excluded_deletions` rather than in
`additions`/`deletions` (`adapters.base.diff_stats`). Nothing here reads `.gitattributes`.
"""
from __future__ import annotations

import fnmatch

# Matched against the file's BASENAME.
_NAME_PATTERNS = (
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "Cargo.lock", "go.sum",
    "composer.lock", "Gemfile.lock", "uv.lock", "*.min.js", "*.min.css", "*.map", "*.snap", "*.pb.go",
    "*_pb2.py", "*.generated.*",
)
# A path is excluded when any of its DIRECTORY segments is one of these.
_DIR_NAMES = frozenset({"vendor", "node_modules", "third_party", "dist", "build", "__snapshots__"})


def is_excluded(path: str) -> bool:
    """True when `path` (repo-relative, `/`-separated; a leading `/` is tolerated) is generated or vendored."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _NAME_PATTERNS):
        return True
    return any(part in _DIR_NAMES for part in parts[:-1])
