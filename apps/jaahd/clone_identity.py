#!/usr/bin/env python3
"""Which project this daemon is running a network git op for right now — the one home for that fact, on a
channel that is deliberately NOT the credential socket.

Daemon-side git runs against credential-free URLs; credential helper → `cred_broker` → the CP, which resolves the
credential of the human who declared that project's slots, so it must be told which project. EVERY network
op needs this context (clone, fetches, resets): one left outside it is unauthenticated, and its swallowed
failure shows up as a slot reset against a stale base. The name is in-process, never in the socket request:
any group-`jaah` process can write a request field, which would let it choose whose credential it gets.

One slot, one name: a DIFFERENT project waits at most `_WAIT_TIMEOUT_S`, then `ProjectBusy` (retryable).
Re-entrant per thread for the SAME name (`create_worktree` → `ensure_clone` nests); the same thread naming
a different project raises immediately — a caller bug, not contention.
Stdlib only, and a LEAF: `worktree_mechanics` and `cred_broker` both import it. Its one in-repo import is
the stdlib-only `host_platform` seam, which every process read goes through.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from contextlib import contextmanager
from typing import Iterator

import host_platform

log = logging.getLogger("jaahd.clone_identity")

# Held for the WHOLE op, not just the assignment: "at most one project in flight". Re-entrant (nesting).
_LOCK = threading.RLock()
_CURRENT = ""
# `(name, projects.id)` of the declaration, assigned as ONE tuple so a lock-free reader (`current_declaration`)
# never pairs one op's name with another's id. The id is what `cred_broker` addresses the CP by (a name is
# display only); None = its declarer knew only the name, and the CP falls back to it inside this node's org.
# The third element is the `project_repos.id` of a non-primary repository, None for the primary —
# today's credential. Same tuple, same atomicity.
_CURRENT_DECL: tuple[str, int | None, int | None] = ("", None, None)
_DEPTH = 0
# The thread inside `cloning` (the `_LOCK` holder), None when none is: a delegated op registered from any
# OTHER thread is not that declaration's op and carries none (`_Registration`).
_HOLDER: int | None = None
_NO_DECL: tuple[str, int | None, int | None] = ("", None, None)

# Well under `_clone_timeout`'s 900s on purpose: a to_thread worker must not park behind another
# project's clone, while a fetch queued behind a fetch (seconds) never comes near it.
_WAIT_TIMEOUT_S = 60.0


class ProjectBusy(RuntimeError):
    """This daemon is already running a network git op for ANOTHER project and did not free up in time.

    Retryable: the slot is untouched; `worktree_mechanics.core._refresh_mirror` turns it into `ResetRefused`,
    which the CP re-issues next pass."""


@contextmanager
def cloning(
    project: str, *, wait_s: float | None = None, project_id: int | None = None, repo_id: int | None = None,
) -> Iterator[None]:
    """Declare that this daemon's network git ops belong to `project` for the duration of the block.

    ENTER AND LEAVE IT ON ONE THREAD, NEVER HOLD IT ACROSS AN `await`. The `RLock`'s exclusion and
    re-entrancy are per thread: held across an await, a second coroutine on the loop thread re-acquires
    it. An async caller runs the context AND its git call in one worker thread
    (`reconcile.context._fetch_base_blocking`). No `asyncio.Lock` alternative: two primitives over one variable
    do not exclude each other.

    Cleared at the OUTERMOST exit whatever happens — a stale name would be served to the next unrelated
    credential helper; an inner clear would unauthenticate the outer op's remaining calls.

    `wait_s` LOWERS the 60s bound for a caller whose own op is shorter; a ceiling, never a floor.
    `project_id` rides with the name (`current_declaration`); a nested frame naming none keeps the outer's.
    `repo_id` names a NON-PRIMARY repository of that project (None = the primary); a nested frame naming
    none keeps the outer's too."""
    global _CURRENT, _CURRENT_DECL, _DEPTH, _HOLDER
    budget = _WAIT_TIMEOUT_S if wait_s is None else min(wait_s, _WAIT_TIMEOUT_S)
    if not _LOCK.acquire(timeout=budget):
        raise ProjectBusy(
            f"a network git op for {_CURRENT or 'another project'!r} still held this daemon's git "
            f"identity after {budget:.0f}s — {project!r} was not started"
        )
    try:
        if _CURRENT and _CURRENT != project:
            # Same thread, different name: cannot be waited out (the waiter is the holder), must not be
            # overwritten.
            raise ProjectBusy(
                f"this call stack already declared {_CURRENT!r}; it may not also declare {project!r}"
            )
        # Save and restore per frame: an empty outer declaration (`""` slips past the arm above) must
        # not be left publishing an inner frame's project.
        previous, previous_decl = _CURRENT, _CURRENT_DECL
        _CURRENT = project
        keep_id = project_id is None and previous_decl[0] == project
        keep_repo = repo_id is None and previous_decl[0] == project
        _CURRENT_DECL = (project, previous_decl[1] if keep_id else project_id,
                         previous_decl[2] if keep_repo else repo_id)
        _DEPTH += 1
        _HOLDER = threading.get_ident()
        try:
            yield
        finally:
            _DEPTH -= 1
            _CURRENT = previous if _DEPTH else ""
            _CURRENT_DECL = previous_decl if _DEPTH else _NO_DECL
            if not _DEPTH:
                _HOLDER = None
    finally:
        _LOCK.release()


def current_declaration() -> tuple[str, int | None]:
    """`(the project whose network git op is in flight, its projects.id or None)` from ONE read, so a caller
    that sends both never pairs one op's name with another's id; `("", None)` when none is.

    "" is an answer, not an error: `cred_broker` sends no `project` and the CP refuses — never substitute
    a default (that would be this node choosing whose credential to spend).

    READ WITHOUT THE LOCK: the reader is the loop serving the credential helper of the op that still holds `_LOCK`,
    so taking it would deadlock. A tuple assignment is atomic."""
    return _CURRENT_DECL[0], _CURRENT_DECL[1]


def _own_declaration() -> tuple[str, int | None, int | None]:
    """The declaration when THIS thread is the one inside `cloning`, else none. Lock-free: `_HOLDER` is this
    thread's own ident only while this thread holds `_LOCK`, so no other thread can make it match."""
    return _CURRENT_DECL if _HOLDER == threading.get_ident() else _NO_DECL


# ── The delegated-op registry: WHICH PROCESSES ARE THIS DAEMON'S OWN GIT OP ──────────────────────────
#
# `cred_broker._resolve`'s system arm admits a caller with no runner pane in its ancestry only if its
# SO_PEERCRED uid equals this daemon's OR it descends from an op registered here. On a packaged node
# `_run_git` delegates through `session_user.run_as_session_user`, so git and its credential helper run as `jaah`,
# not the daemon's uid — without this registry every clone is refused. Never loosen the uid gate instead.
# Proof is by ANCESTRY (a process cannot choose its parents), like `runner_registry.resolve_peer`.
# The registry is also the only source of WHICH PROJECT an op is served, for every uid: a git run on the
# daemon's own uid (`run_own`) is registered the same way, so a daemon-uid caller outside any registration is
# admitted but served no project — never the live global, which another thread's `cloning` may hold.
#
# ACCEPTED RESIDUAL: the pid arrives through the result spool, writable by the session user, and group
# `jaah` can list `/run/jaah/exec/req` to learn the request id — the `token_hex` is not a secret from
# it. So a live same-uid process could be registered and served the token for one op. `claim` refuses a
# dead or foreign-uid pid. Narrowing it: a result-path id disclosed only in the 0750 script, and binding
# `claim` to descent from the dispatching tmux server.

#: The registry's own lock, deliberately NOT `_LOCK`: `_LOCK` is held for the whole op whose credential helper the
#: reader is serving, so taking it deadlocks (observed live: `jaah-credential` timed out while the broker
#: blocked). A plain `Lock` suffices — held only for dict assignment/pop/copy, never across a wait.
_DELEGATED_LOCK = threading.Lock()

#: pid → (start-time ticks at registration, the registering thread's declaration). The ticks make a RECYCLED pid
#: safe; the declaration is what the broker serves that op (`delegated_declaration`), never the live global one,
#: which another thread's `cloning` may hold while this op runs code planted in its tree (a filter driver).
_DELEGATED: dict[int, tuple[int, tuple[str, int | None, int | None]]] = {}

#: The real chain is short (credential helper → git → timeout → sh); this bound stops a malformed `/proc` or a cycle
#: from spinning the credential socket's serving loop.
_MAX_WALK_DEPTH = 24


def _start_ticks(pid: int) -> int | None:
    """The process's start time (field-22 ticks on Linux), or None when it cannot be read. Delegates to
    `host_platform.start_time` — the same seam `runner_registry` reads, so both walks agree."""
    return host_platform.start_time(pid)


def _windows_owner_is_runner(pid: int, ticks: int) -> bool:
    """Whether `pid` — the very process registered at `ticks` — runs as the runner account. Fail closed.

    By ancestry to the verified fleet tmux server (`host_platform.windows`' `_pane_owner_check`), never by
    the process's token, which the daemon's account cannot open; the creation time is re-read after the
    check, so a pid recycled meanwhile is refused. Injectable: tests replace this name."""
    from host_platform import windows  # noqa: PLC0415 — Windows-only; importable anywhere, native on use

    return windows._pane_owner_check(pid) and _start_ticks(pid) == ticks


@contextmanager
def delegated_op(expected_uid: int | None) -> Iterator["_Registration"]:
    """Admit ONE delegated process tree as this daemon's own git op for the duration of the block.

    `claim(pid)` is called once the delegated shell reports its own pid (spawned by the session user's tmux
    server, so unknown up front). Nothing is admitted until `claim` succeeds; the entry is removed on exit
    whatever happens. `expected_uid` is the session user's; `None` disables the check."""
    registration = _Registration(expected_uid)
    try:
        yield registration
    finally:
        registration.release()


class _Registration:
    """One in-flight delegated op's entry in the registry. Not constructed directly — see `delegated_op`."""

    def __init__(self, expected_uid: int | None) -> None:
        self._expected_uid = expected_uid
        self._pid: int | None = None
        # Read where the op is registered, on the thread that runs it (`_own_declaration`).
        self._declaration = _own_declaration()

    def claim(self, pid: int) -> bool:
        """Admit `pid`'s process tree, answering whether it was admitted.

        Refuses a pid that is not a live process of the expected uid. Answers rather than raises: a refused
        op still runs, just without a credential.

        ON WINDOWS `pid` is already the Windows pid (`session_user` maps the shell's MSYS `$$` before calling
        back) and there is no uid (`owner_uid` is unported): with an owner expected, the process must be the
        runner account's (`_windows_owner_is_runner`, by ancestry) and still carry the creation time
        registered — so a pid recycled between the two reads is refused. Fails closed on anything
        unreadable."""
        ticks = _start_ticks(pid)
        if ticks is None:
            return False
        if self._expected_uid is not None:
            if host_platform.is_windows():
                if not _windows_owner_is_runner(pid, ticks):
                    return False
            elif host_platform.owner_uid(pid) != self._expected_uid:
                return False
        with _DELEGATED_LOCK:
            _DELEGATED[pid] = (ticks, self._declaration)
        self._pid = pid
        return True

    def release(self) -> None:
        with _DELEGATED_LOCK:
            if self._pid is not None:
                _DELEGATED.pop(self._pid, None)
        self._pid = None


def run_own(argv: list[str], *, timeout: float | None = None, **popen_kwargs: object
            ) -> "subprocess.CompletedProcess[str]":
    """`subprocess.run(argv, capture_output=True, timeout=…, **popen_kwargs)` for a git op run IN-PROCESS on the
    daemon's own uid, registered (`delegated_op`) under THIS thread's declaration — the daemon-uid twin of
    `session_user.run_as_session_user`'s `on_child_pid` claim. Without it the op's credential helper is served
    no project. Takes `Popen`'s keywords only (no `input`, `check` or `capture_output`).

    The child is claimed right after spawn, before git can reach a remote and ask its credential helper; a
    claim that fails (pid unreadable) leaves the op unauthenticated, the fail-closed direction. No uid check:
    the pid is this process's own child, never a reported value.

    A timeout kills the child and returns at once, as `subprocess.run` does: off Windows it WAITS rather than
    draining the pipes, which a grandchild (`git-remote-https`) may hold open for as long as it hangs."""
    with delegated_op(None) as registration:
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,  # noqa: S603 — callers' argv
                              **popen_kwargs) as proc:  # type: ignore[call-overload]
            if not registration.claim(proc.pid):
                log.warning("run_own: could not register git pid %s as this daemon's op — its credential helper "
                            "will be served no project", proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                if host_platform.is_windows():
                    exc.stdout, exc.stderr = proc.communicate()
                else:
                    proc.wait()
                raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def is_delegated_op(peer_pid: object) -> bool:
    """Whether `peer_pid` runs inside a delegated git op this daemon started (`delegated_declaration`)."""
    return delegated_declaration(peer_pid) is not None


def delegated_declaration(peer_pid: object) -> tuple[str, int | None, int | None] | None:
    """The declaration of the delegated git op `peer_pid` runs inside — `("", None, None)` for an op registered
    outside `cloning` — or None when it runs inside none.

    `peer_pid` MUST be the kernel's (`SO_PEERCRED`), never a request value — an ancestry proof is worth
    only that the caller did not choose the start. Takes `_DELEGATED_LOCK`, never `_LOCK` (deadlock); the
    `/proc` walk runs outside it."""
    try:
        pid = int(str(peer_pid).strip())
    except (TypeError, ValueError):
        return None
    with _DELEGATED_LOCK:
        registered = dict(_DELEGATED)
    if not registered:
        return None
    seen: set[int] = set()
    depth = 0
    while pid > 1 and depth < _MAX_WALK_DEPTH and pid not in seen:
        seen.add(pid)
        entry = registered.get(pid)
        # Start-time re-check: a recycled pid no longer matches.
        if entry is not None and _start_ticks(pid) == entry[0]:
            return entry[1]
        parent = host_platform.parent_of(pid)
        if parent is None:
            return None
        pid = parent
        depth += 1
    return None
