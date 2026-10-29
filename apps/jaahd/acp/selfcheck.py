"""acp/selfcheck.py — does any process of this run hold a model key in its environment?

The ACP host is the pane root: the engine and every MCP server it starts are its descendants. A model key
in any of their environments means a credential reached a model-driven process by a route the daemon does
not control, so the host reports it (an `auth` error event) at session start and again at every turn end.

NAMES only. `/proc/<pid>/environ` is read, matched by variable name, and the bytes are dropped on the
spot: a finding is `{pid, comm, var}` and nothing that could carry the value ever leaves this module.

Linux-only (`/proc`); elsewhere every scan answers empty. Stdlib only, no daemon imports.
"""
from __future__ import annotations

import os
import subprocess
import sys

# The variables a model CLI reads a key from: a denylist the leak check must spell out in full. Every
# registered adapter's `api_key_env()` name is already here; importing `agents` to read them costs ~0.3 s
# per host start for no new name, so a new adapter's variable is added to this set by hand.
MODEL_KEY_VARS: frozenset[str] = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY",
    "DEEPSEEK_API_KEY",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
    "AZURE_OPENAI_API_KEY", "XAI_API_KEY", "MISTRAL_API_KEY",
})

_NAMES = frozenset(name.encode() for name in MODEL_KEY_VARS)
_PROC = "/proc"


def _linux() -> bool:
    return sys.platform.startswith("linux")


def _ppid(pid: int) -> int | None:
    """Field 4 of /proc/<pid>/stat, read past the LAST `)` (a comm may contain one)."""
    try:
        with open(f"{_PROC}/{pid}/stat", "rb") as fh:
            stat = fh.read()
        return int(stat[stat.rindex(b")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def descendants(root_pid: int, *, include_root: bool = False) -> list[int]:
    """Every live descendant of `root_pid` (by ppid), breadth-first; `root_pid` first when `include_root`."""
    if not _linux():
        return []
    children: dict[int, list[int]] = {}
    try:
        names = os.listdir(_PROC)
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        parent = _ppid(int(name))
        if parent is not None:
            children.setdefault(parent, []).append(int(name))
    found: list[int] = [root_pid] if include_root else []
    queue, seen = [root_pid], {root_pid}
    while queue:
        for child in children.get(queue.pop(0), ()):
            if child not in seen:
                seen.add(child)
                found.append(child)
                queue.append(child)
    return found


def _comm(pid: int) -> str:
    try:
        with open(f"{_PROC}/{pid}/comm", "rb") as fh:
            return fh.read().decode(errors="replace").strip()
    except OSError:
        return ""


def _key_vars(pid: int) -> list[str]:
    """The model-key NAMES set non-empty in `pid`'s environment. The environ bytes never leave this frame."""
    try:
        with open(f"{_PROC}/{pid}/environ", "rb") as fh:
            entries = fh.read().split(b"\0")
    except OSError:
        return []
    hits = []
    for entry in entries:
        name, sep, value = entry.partition(b"=")
        if sep and value and name in _NAMES:
            hits.append(name.decode())
    del entries
    return sorted(set(hits))


def scan_tree(root_pid: int, *, include_root: bool = True) -> list[dict]:
    """`[{pid, comm, var}]` for every model key set in the tree under `root_pid`. Unreadable pids (another
    user's, or exited mid-walk) are skipped: this reports what it can see, never a guess."""
    if not _linux():
        return []
    findings = []
    for pid in descendants(root_pid, include_root=include_root):
        for var in _key_vars(pid):
            findings.append({"pid": pid, "comm": _comm(pid), "var": var})
    return findings


def cli_versions(argv0s: list[str], timeout: float = 5.0) -> dict[str, str]:
    """`<exe> --version`'s first stdout line per executable; "" when it fails or says nothing."""
    if not _linux():
        return {}
    versions: dict[str, str] = {}
    for exe in argv0s:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=timeout,
                                 stdin=subprocess.DEVNULL, check=False).stdout
        except (OSError, subprocess.SubprocessError, ValueError):
            out = ""
        versions[exe] = (out.strip().splitlines() or [""])[0].strip()
    return versions
