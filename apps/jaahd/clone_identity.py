#!/usr/bin/env python3
"""Which project this daemon is running a network git op for right now — the one home for that fact, on a
channel that is deliberately NOT the credential socket.

Daemon-side git runs against credential-free URLs; credential helper → `cred_broker` → the CP, which resolves the
credential of the human who declared that project's slots, so it must be told which project. EVERY network
op needs this context (clone, fetches, resets): one left outside it is unauthenticated, and its swallowed
failure shows up as a slot reset against a stale base. The name is in-process, never in the socket request:
any group-`jaah` process can write a request field, which would let it choose whose credential it gets.

One slot, one name: a DIFFERENT project waits at most `_WAIT_TIMEOUT_S`, then `ProjectBusy` (retryable).
Re-entrant per thread for the SAME name (an op nested in its caller's declaration); the same thread naming
a different project raises immediately — a caller bug, not contention.
Stdlib only, and a LEAF: `worktree_mechanics` and `cred_broker` both import it. Its one in-repo import is
the stdlib-only `host_platform` seam, which every process read goes through.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
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

    Retryable: nothing was touched, and the caller's next attempt may find the identity free."""


@contextmanager
def cloning(
    project: str, *, wait_s: float | None = None, project_id: int | None = None, repo_id: int | None = None,
) -> Iterator[None]:
    """Declare that this daemon's network git ops belong to `project` for the duration of the block.

    ENTER AND LEAVE IT ON ONE THREAD, NEVER HOLD IT ACROSS AN `await`. The `RLock`'s exclusion and
    re-entrancy are per thread: held across an await, a second coroutine on the loop thread re-acquires
    it. An async caller runs the context AND its git call in one worker thread (`microvm_images._source`). No `asyncio.Lock` alternative: two primitives over one variable
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


# ── Acting: WHOSE access a slot clone spends ─────────────────────────────────────────────────────────────
#
# A slot is cloned and refreshed with the access of the work it is for (D9): the task that leased it, through
# the CP's task arm (`/tasks/{id}/git-token`), never the slot declarer's. `acting_for` publishes that subject on
# THIS thread only, and a delegated op registered inside it (`_Registration`) carries it, so the broker serves
# the op the acting subject's login. Unlike `cloning` it takes no node-wide lock: the subject rides each
# registration, never a global, so two slots of two tasks clone at once.
ACTING_TASK = "task"
# A "Repo script" trigger's preflight clone: `id` is the CP's grant (`app/trigger_preflight_script.py::mint_grant`),
# traded at the CP's trigger arm for the trigger owner's login.
ACTING_TRIGGER = "trigger"
# A grant's shape: `<base64url body>.<base64url mac>`. The CP verifies it; this only keeps junk out of a URL.
GRANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,1536}\.[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Acting:
    """Whose access one network git op spends: `kind` (an `ACTING_*`), the subject's `id` (a task id; for a
    trigger, its grant), the `run` that keys its cached login (a dispatch, so a restart by another person is asked
    afresh), and `repo_id`, a non-primary `project_repos.id` (None: the primary)."""

    kind: str
    id: str
    run: str = ""
    repo_id: int | None = None

    def __post_init__(self) -> None:
        valid = (str(self.id).strip().isdigit() if self.kind == ACTING_TASK
                 else self.kind == ACTING_TRIGGER and bool(GRANT_RE.match(str(self.id))))
        if not valid:
            # Never the id itself: a trigger's is a grant.
            raise ValueError(f"not an acting subject: kind={self.kind!r}")

    def __repr__(self) -> str:
        # A grant buys a login: it never reaches a log line through a repr.
        shown = self.id if self.kind == ACTING_TASK else "<grant>"
        return f"Acting(kind={self.kind!r}, id={shown!r}, run={self.run!r}, repo_id={self.repo_id!r})"

    __str__ = __repr__

    @property
    def label(self) -> str:
        """What a log line names: `task <id>`, or `trigger` (a grant is never logged)."""
        return f"task {self.id}" if self.kind == ACTING_TASK else "trigger"


_ACTING = threading.local()


@contextmanager
def acting_for(acting: Acting) -> Iterator[None]:
    """Publish `acting` as this thread's acting subject for the block. Nested frames must name the same subject
    (a caller bug otherwise, raised as `ProjectBusy`); the outer one is restored on exit whatever happens."""
    previous = getattr(_ACTING, "value", None)
    if previous is not None and previous != acting:
        raise ProjectBusy(f"this call stack already acts for {previous}; it may not also act for {acting}")
    _ACTING.value = acting
    try:
        yield
    finally:
        _ACTING.value = previous


def current_acting() -> Acting | None:
    """This thread's acting subject (`acting_for`), or None."""
    return getattr(_ACTING, "value", None)


# ── Refused logins: a declaration whose cached login the next ask must read past ─────────────────────
#
# `cred_broker` caches a declaration's login for its TTL, and only git's `erase` (a 401) reads past it. A forge
# that refuses with "Repository not found" (GitHub's 404 for a login that cannot see the repository) never
# triggers `erase`, so after the CP changes whose login a clone uses (Change clone owner) the stale login was
# served again until the TTL ran out. A failed network op marks its declaration here; the broker's next ask
# for it skips the cache once (`take_refused_login`).
_REFUSED_LOCK = threading.Lock()
_REFUSED: set[tuple[str, int | None, int | None]] = set()


def mark_login_refused() -> None:
    """Mark THIS thread's own declaration (inside `cloning`) as refused; a no-op outside one."""
    declaration = _own_declaration()
    if declaration == _NO_DECL:
        return
    with _REFUSED_LOCK:
        _REFUSED.add(declaration)


def take_refused_login(declaration: tuple[str, int | None, int | None]) -> bool:
    """Whether `declaration` was marked refused since its last ask, clearing the mark."""
    with _REFUSED_LOCK:
        if declaration in _REFUSED:
            _REFUSED.discard(declaration)
            return True
    return False


# ── The delegated-op registry: WHICH PROCESSES ARE THIS DAEMON'S OWN GIT OP ──────────────────────────
#
# `cred_broker._resolve`'s system arm admits a caller with no runner pane in its ancestry only on this daemon's
# own uid, and serves it the declaration (and acting subject) of the op it descends from, registered HERE by
# `run_own` — the one way a git op is registered. Proof is by ANCESTRY (a process cannot choose its parents),
# like `runner_registry.resolve_peer`, and the registered pid is the daemon's own child, read from `Popen`, never
# a reported value. Every credentialed network op runs that way (`git_mirror`); nothing the session user's tmux
# server forks is ever registered, so no same-uid process on that channel can be served a login. A daemon-uid
# caller outside any registration is admitted but served no project — never the live global, which another
# thread's `cloning` may hold.

#: The registry's own lock, deliberately NOT `_LOCK`: `_LOCK` is held for the whole op whose credential helper the
#: reader is serving, so taking it deadlocks (observed live: `jaah-credential` timed out while the broker
#: blocked). A plain `Lock` suffices — held only for dict assignment/pop/copy, never across a wait.
_DELEGATED_LOCK = threading.Lock()

#: pid → (start-time ticks at registration, the registering thread's declaration, its acting subject). The ticks
#: make a RECYCLED pid safe; the declaration and acting subject are what the broker serves that op
#: (`delegated_op_of`), never the live global one, which another thread's `cloning` may hold while this op runs
#: code planted in its tree (a filter driver).
_DELEGATED: dict[int, tuple[int, tuple[str, int | None, int | None], "Acting | None"]] = {}

#: The real chain is short (credential helper → git → timeout → sh); this bound stops a malformed `/proc` or a cycle
#: from spinning the credential socket's serving loop.
_MAX_WALK_DEPTH = 24


@contextmanager
def delegated_op() -> Iterator["_Registration"]:
    """Admit ONE process tree this daemon spawned itself as its own git op for the duration of the block.

    `claim(pid)` is called with the child's pid right after spawn (`run_own`). Nothing is admitted until `claim`
    succeeds; the entry is removed on exit whatever happens."""
    registration = _Registration()
    try:
        yield registration
    finally:
        registration.release()


class _Registration:
    """One in-flight delegated op's entry in the registry. Not constructed directly — see `delegated_op`."""

    def __init__(self) -> None:
        self._pid: int | None = None
        # Read where the op is registered, on the thread that runs it (`_own_declaration`, `current_acting`).
        self._declaration = _own_declaration()
        self._acting = current_acting()

    def claim(self, pid: int) -> bool:
        """Admit `pid`'s process tree — this daemon's own child — answering whether it was admitted. Refuses a
        pid whose start time is unreadable (gone). Answers rather than raises: a refused op still runs, just
        without a credential."""
        ticks = host_platform.start_time(pid)
        if ticks is None:
            return False
        with _DELEGATED_LOCK:
            _DELEGATED[pid] = (ticks, self._declaration, self._acting)
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
    daemon's own uid, registered (`delegated_op`) under THIS thread's declaration and acting subject — the
    only registration there is. Without it the op's credential helper is served no project. Takes `Popen`'s keywords only (no `input`, `check` or `capture_output`).

    The child is claimed right after spawn, before git can reach a remote and ask its credential helper; a
    claim that fails (pid unreadable) leaves the op unauthenticated, the fail-closed direction. The pid is this
    process's own child, never a reported value.

    A timeout kills the child and returns at once, as `subprocess.run` does: off Windows it WAITS rather than
    draining the pipes, which a grandchild (`git-remote-https`) may hold open for as long as it hangs."""
    with delegated_op() as registration:
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


def delegated_op_of(peer_pid: object) -> tuple[tuple[str, int | None, int | None], Acting | None] | None:
    """`(declaration, acting subject)` of the delegated git op `peer_pid` runs inside — `("", None, None)` and
    None for an op registered outside `cloning` and `acting_for` — or None when it runs inside none.

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
        if entry is not None and host_platform.start_time(pid) == entry[0]:
            return entry[1], entry[2]
        parent = host_platform.parent_of(pid)
        if parent is None:
            return None
        pid = parent
        depth += 1
    return None
