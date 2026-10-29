"""runner_clis.py — which runner CLIs user `jaah` actually has on this node, sensed from the disk.

Published as `runner_clis`; `routes/fleet.py::_account_candidates` refuses an account whose provider's
`binary` is missing. Sensed, never declared: only the names (`agent_providers.PROVIDERS`) are declared.
Fails closed — a refusal is visible and heals next pass, an over-attestation launches a runner that dies
at `exec` (the opposite trade to `agent_config_dirs.dir_present`, whose consumer is a kill gate).

Never exec from `~jaah/.local/bin` (runner-writable; would hand a runner the daemon uid). Never
`os.access`/`shutil.which` (they answer for this uid): execute bits are checked against the runner's
uid/gid. A shebang is followed one hop and only a proven absence (`ENOENT`/`ENOTDIR`) disqualifies; the
first executable PATH match is decisive (bash does not fall through to a later copy). `None`, not `[]`,
when the runner user is unresolvable — the caller omits the key. Stdlib only, no subprocess (every pass)."""
from __future__ import annotations

import logging
import os
import stat

import agent_providers
import host_platform
import session_user

log = logging.getLogger("jaahd.runner_clis")

# Every provider, the default one included: a provider exempt from attestation is one whose absence
# nothing can refuse. Installed into `~jaah/.local/bin` by the package postinst's runner-CLI floor.
# Each provider's `companions` too: dispatch matches on `binary` alone, so a companion name in the list is
# inert there, and its absence is what the console reports as a missing companion. And each `acp_binary`: the
# control plane keeps a run off the ACP lane until its adapter is attested (a fresh node is still installing it).
RUNNER_CLI_BINARIES: tuple[str, ...] = tuple(sorted({
    name for provider in agent_providers.PROVIDERS
    for name in (provider.binary, *provider.companions, provider.acp_binary) if name
}))


def _resolves(name: str, path: str) -> bool:
    """Would a command run AS THE RUNNER USER find an executable `name` on `path`?

    `shutil.which`'s search, decided on the runner's uid/gid mode bits. `os.stat` follows symlinks, so a
    dangling one is absent. A script's shebang interpreter must resolve too."""
    # Windows: no uid/gid (ACLs decide, and the runner account is not this one), and the native installer's
    # binary is `claude.exe` — bash resolves the bare name to it; a pinned npm install's bin is a `.cmd` shim
    # (`runner_cli_pins`), which the ACP host's `which` finds by PATHEXT.
    on_windows = host_platform.is_windows()
    uid, gid = (None, None) if on_windows else (session_user.session_uid(), session_user.session_gid())
    names = (name + ".exe", name + ".cmd") if on_windows else (name,)
    for candidate in (os.path.join(d, n) for d in path.split(os.pathsep) if d for n in names):
        try:
            st = os.stat(candidate)
        except OSError:
            # Cannot see a CLI here: fail closed, same as absent.
            continue
        if not (stat.S_ISREG(st.st_mode) and st.st_mode & _exec_bit_for(st, uid, gid)):
            continue                       # not a match a real PATH search would take either
        if on_windows and candidate.endswith(".cmd") and not _windows_node_present():
            # A shim has no shebang to follow: its interpreter is the Node it names by absolute path.
            log.debug("runner_clis: %s needs Node.js, which is not installed — not attesting it", candidate)
            return False
        # Decisive: the launcher execs this file, so a working later copy must not vouch for it.
        if _interpreter_is_missing(candidate, path, uid, gid):
            log.debug(
                "runner_clis: %s is executable but its interpreter is not on the runner's PATH — not "
                "attesting it; a dispatch would die at exec", candidate,
            )
            return False
        return True
    return False


def _windows_node_present() -> bool:
    """Whether the `node.exe` a Windows `.cmd` shim runs (`runner_cli_pins._node_exe`) is there."""
    from host_platform import windows  # noqa: PLC0415 — Windows arm only

    return os.path.isfile(os.path.join(windows.NODEJS_DIR, "node.exe"))


# Past any real shebang (the kernel reads 128 or 256 bytes depending on the build).
_HEAD_BYTES = 256


def _head_bytes(candidate: str) -> bytes | None:
    """The first 256 bytes of CANDIDATE, or None when this process cannot read them.

    Fd-based for security: user `jaah` can swap the stat'd file for a FIFO, and a blocking `open` on
    that FIFO never returns (the sensor runs in `asyncio.to_thread`; no timeout, no watchdog).
    `O_NONBLOCK` returns immediately and `os.fstat` checks the descriptor actually opened. Symlinks are
    followed on purpose (no `O_NOFOLLOW`). `O_NONBLOCK` does not exist on Windows (no FIFOs to block on):
    a bare `os.O_NONBLOCK` there raises AttributeError, which would kill the whole observation pass."""
    try:
        fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None                                   # unreadable: not a proven absence, so not our call
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None                               # not what we stat'd — say nothing about it
        return os.read(fd, _HEAD_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)


def _shebang_interpreter(candidate: str) -> str | None:
    """The program a `#!` line names, or None when there is no shebang to follow.

    `/usr/bin/env foo` (and `env -S foo --flag`) resolves to `foo`."""
    head = _head_bytes(candidate)
    if head is None:
        return None
    parts = head.split(b"\n", 1)
    if len(parts) == 1 and len(head) == _HEAD_BYTES:
        # Truncated line: the visible name may not be the name — uncertainty, which attests.
        return None
    first = parts[0]
    if not first.startswith(b"#!"):
        return None                                   # a real binary; nothing to follow
    try:
        words = first[2:].decode("utf-8", "strict").split()
    except UnicodeDecodeError:
        return None
    if not words:
        return None
    interpreter = words[0]
    if os.path.basename(interpreter) == "env":
        # Skip flags and `VAR=value` assignments (`env -S NODE_OPTIONS=... node` is a live npm spelling).
        rest = [word for word in words[1:] if not word.startswith("-") and "=" not in word]
        if not rest:
            return None                               # nothing left to name — unparseable, so allowed
        interpreter = rest[0]
    # `env -S "node"` is real and `split()` keeps the quotes: strip one matched pair; any other quote is a
    # shape not understood (None).
    for quote in ('"', "'"):
        if len(interpreter) >= 2 and interpreter.startswith(quote) and interpreter.endswith(quote):
            interpreter = interpreter[1:-1]
            break
    if not interpreter or '"' in interpreter or "'" in interpreter:
        return None
    return interpreter


# Absolute interpreters probed only under system prefixes: a shebang is runner-authored, and an arbitrary
# path would be an existence oracle with the daemon's traversal rights (or a hung mount). Others attest.
_PROBED_INTERPRETER_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/opt/", "/lib/")


def _proven_not_executable(target: str, uid: int | None, gid: int | None) -> bool | None:
    """True = proven unrunnable, False = proven runnable, None = this process cannot tell.

    Only `ENOENT`/`ENOTDIR` prove absence; `EACCES` means the daemon (not the runner uid) could not look."""
    try:
        st = os.stat(target)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return None
    return not (stat.S_ISREG(st.st_mode) and st.st_mode & _exec_bit_for(st, uid, gid))


def _interpreter_is_missing(candidate: str, path: str, uid: int | None, gid: int | None) -> bool:
    """True only when this process has PROVEN the candidate's interpreter is not runnable.

    Every uncertain case answers False (attested)."""
    interpreter = _shebang_interpreter(candidate)
    if interpreter is None:
        return False
    if "/" in interpreter:
        if not interpreter.startswith(_PROBED_INTERPRETER_PREFIXES):
            return False                              # not a shape we judge — see the prefix note above
        return _proven_not_executable(interpreter, uid, gid) is True
    # A bare name is looked up on the same PATH the launched process searches. No recursion.
    could_not_tell = False
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        verdict = _proven_not_executable(os.path.join(directory, interpreter), uid, gid)
        if verdict is False:
            return False                              # found it, and it runs
        if verdict is None:
            could_not_tell = True
    # Any unreadable directory is uncertainty, which attests.
    return not could_not_tell


def _exec_bit_for(st: os.stat_result, uid: int | None, gid: int | None) -> int:
    """WHICH execute bit decides for the runner user: owner, group, or other.

    `0o111` only when the runner user is unresolvable (dev/CI with an explicit PATH)."""
    if uid is None or gid is None:
        return 0o111
    if st.st_uid == uid:
        return 0o100
    if st.st_gid == gid:
        return 0o010
    return 0o001


def windows_cli_dirs() -> list[str]:
    """The bundle dirs a Windows node's runner CLIs ship in, in catalogue order: each `windows_runner` provider
    whose adapter names one (`windows_cli_dir`). The one list launch.sh's PATH (`fleet_launch._windows_path_lines`)
    and this sensor both search, so what is attested is what `exec` finds."""
    import agents  # noqa: PLC0415 — the adapter registry; deferred like this module's other Windows-arm imports

    adapters = (agents.get_adapter(p.value) for p in agent_providers.PROVIDERS if p.windows_runner)
    return [adapter.windows_cli_dir() for adapter in adapters if hasattr(adapter, "windows_cli_dir")]


def sense_runner_clis(path: str | None = None) -> list[str] | None:
    """The subset of `RUNNER_CLI_BINARIES` (binaries and companions) this node actually has, sorted — or None
    when unknowable.

    PATH defaults to `session_user.delegated_path()`, the PATH the launched runner will search. None when
    the runner user has no home (that PATH would describe the daemon's uid); the caller omits the key."""
    if path is None and host_platform.is_windows():
        # The runner account's own `.local\\bin`, where `claude_cli_windows.ensure` installs, then the bundle's
        # CLI dirs (`windows_cli_dirs`) — launch.sh's PATH (`fleet_launch._windows_path_lines`), in its order;
        # not `delegated_path`, which reads passwd.
        import claude_cli_windows  # noqa: PLC0415 — Windows arm only

        path = os.pathsep.join((claude_cli_windows.cli_dir(), *windows_cli_dirs()))
    if path is None:
        if not session_user.session_home():
            log.debug(
                "runner_clis: no resolvable home for the session user %r — attesting nothing this pass "
                "(an answer built from the daemon's own PATH would describe the wrong uid)",
                session_user.session_user(),
            )
            return None
        path = session_user.delegated_path()
    return sorted(name for name in RUNNER_CLI_BINARIES if _resolves(name, path))
