"""Orphan reaping — killing the processes a dead session left in a slot, and refusing any it cannot warrant."""
from __future__ import annotations

import errno
import logging
import ntpath
import os
import re
import signal
import subprocess
import time
from typing import Callable, Iterable

import environment
import host_platform
import session_user

from worktree_mechanics import processes

log = logging.getLogger("jaahd.worktree_mechanics.reap")


# ── THE ORPHAN REAPER — debris a dead session left rooted in a pool tree ──────────────────────────────
# Gate 0 answers the KILL decision, not LIVENESS: a dead runner's `run_in_background` shells reparent to
# init and the slot reads occupied by nobody, forever (measured: 12 of 14 slots, one orphan at 100% CPU
# for 3h). Debris is open-ended, so do not classify it (as `_is_non_runner_process` does) — reap it.
_ORPHAN_TERM_GRACE_SECS = 5.0
_ORPHAN_REAP_POLL_SECS = 0.2


# Docker is exempt from every sweep: a warm compose stack's processes legitimately sit in the tree, and
# killing them is the spawn-and-destroy worktree-pool-policy.md forbids. Anchored at argv[0] (anti-forge,
# as `_NON_RUNNER_CMDLINE_RE`); unreadable is NOT docker — fail-closed for a sweep.
_DOCKER_CMDLINE_RE = re.compile(
    r"^(?:\S*/)?(?:docker|dockerd|docker-compose|docker-proxy|containerd|containerd-shim[\w.-]*)(?:\s|$)"
)


def _is_docker_process(pid: int) -> bool:
    """True when PID execs a docker binary — the one thing a tree sweep leaves alone."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    return bool(_DOCKER_CMDLINE_RE.match(raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()))


def _still_reapable(pid: int, wt_real: str, *, orphans_only: bool) -> bool:
    """The immediately-before-signalling re-check, for a pid whose cwd this process may not be able to read.

    False on EACCES would drop every pid the delegated scan just proved. `orphans_only=False` is the
    release's: a live `claude` still parented to its pane is what has to go. Re-asserts the cheap readable
    facts (exists, not docker, still reparented); an unreadable cwd's warrant is `_reap_roots`' batched
    delegation — the batch-to-signal gap is a narrower form of the readable path's existing chdir race."""
    if _is_docker_process(pid):
        return False
    if orphans_only and processes._proc_ppid(pid) != 1:
        return False
    try:
        target = processes._proc_cwd(pid)
    except PermissionError:
        return True                      # proven by the delegated scan; the readable checks above stand
    except OSError:
        return False
    return target == wt_real or target.startswith(wt_real + "/")


def _reap_roots(wt_real: str, *, orphans_only: bool,
                resolve_unreadable: Callable[..., processes.CwdScan] | None = None,
                ) -> tuple[set[int], tuple[int, ...]]:
    """The pids rooted in WT_REAL that this sweep may signal, plus the pids whose cwds nobody could read.

    `readlink /proc/<pid>/cwd` needs a matching uid or `CAP_SYS_PTRACE` (group grants nothing), so every
    runner leftover raises EACCES daemon-side; treating that as "not rooted" made the sweep silently
    select nothing. Such pids are asked of the session user in ONE batched delegation (per-pid would
    spawn a subprocess per process per pass). `ppid` and the docker test need no delegation, so they run
    first.

    Only pids OWNED BY THE SESSION USER are deferred (gate 0's local rule): a system daemon's cwd is
    unreadable to both uids, and deferring it poisoned the whole batch into PROBE_FAILED every pass
    (measured: 28 systemd children). Skipping them removes no target — `_kill_across_uid_split` could not
    signal them either, a premise that holds while this daemon lacks `CAP_KILL`/root (a root reader never
    gets EACCES here). It strictly widens the kill set, inside the existing guards (`_reapable_tree`,
    `ppid == 1`, the per-pid `_still_reapable` re-test).

    Returns `(roots, unreadable)`: `unreadable` is the deferred pids when delegation could not answer —
    reported by the caller (unreadable is not empty) and used as the report's fingerprint."""
    direct: set[int] = set()
    deferred: list[int] = []
    # As in `scan_cwd_processes_multi`: None when no switch is in play (nobody to defer to).
    blind_uid = session_user.session_uid() if session_user.switch_needed() else None
    not_ours = 0
    for pid in processes._proc_pids():
        if _is_docker_process(pid):
            continue
        if orphans_only and processes._proc_ppid(pid) != 1:
            continue
        try:
            target = processes._proc_cwd(pid)
        except PermissionError:
            if blind_uid is not None and blind_uid in processes._proc_owner_uids(pid):
                deferred.append(pid)                 # ours to ask about, not ours to read
            else:
                not_ours += 1
            continue
        except OSError:
            continue                                 # exited, or genuinely unreadable — not a target
        if target == wt_real or target.startswith(wt_real + "/"):
            direct.add(pid)
    # A trail for the unreported population: on a single-uid host a genuinely unreadable occupant lands
    # here too. DEBUG and un-deduped on purpose.
    if not_ours:
        log.debug("_reap_roots: %d process(es) under %s have cwds neither this process (uid %d) nor the "
                  "session user could read and neither could signal, so they were not asked about",
                  not_ours, wt_real, os.geteuid())
    if not deferred:
        return direct, ()
    # Looked up at call time: a default argument would freeze the attribute at import.
    resolve = resolve_unreadable or processes.resolve_cwds_as_session_user
    scan = resolve(wt_real, deferred, collect=True)
    if scan.status == processes.PROBE_FAILED:
        # Nobody can tell: fail closed on the kill, loud on the report.
        return direct, tuple(sorted(deferred))
    return direct | {int(p["pid"]) for p in scan.processes if isinstance(p.get("pid"), int)}, ()


def _orphan_descendants(roots: set[int]) -> set[int]:
    """`roots` plus every transitive child of them, from one /proc pass. A reparented shell's children are
    the same debris as the shell: they are only NOT `ppid == 1` because their own parent outlived the
    session too."""
    children: dict[int, list[int]] = {}
    for pid in processes._proc_pids():
        ppid = processes._proc_ppid(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    seen = set(roots)
    queue = list(roots)
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


# $JAAH_HOME/repos/<project id>/<n> — the
# shallowest thing that may ever be a reap target. An ancestor of the pool is never one, and neither is
# the pool root itself.
_MIN_REAPABLE_DEPTH = 3


# A persistently refused kill re-logs every ~5s; the count is the alarm, the capped list the lead.
_REFUSAL_LOG_LIMIT = 10


def _refusal_summary(refused: dict[int, str], cmds: dict[int, str]) -> str:
    """The refused pids as one bounded line — `pid (cmd) why`, capped, with the remainder counted."""
    shown = sorted(refused.items())[:_REFUSAL_LOG_LIMIT]
    text = "; ".join(f"{pid} ({cmds.get(pid, processes.UNREADABLE_CMD)}) {why}" for pid, why in shown)
    if len(refused) > _REFUSAL_LOG_LIMIT:
        text += f" … and {len(refused) - _REFUSAL_LOG_LIMIT} more"
    return text


def _reapable_tree(wt: str) -> str:
    """The realpath this reap may sweep, or `""` when WT is not a pool worktree we may act on.

    The path decides who dies: the kill set is every pid whose cwd is under `realpath(wt)`, and the pool
    parent is group-writable to the runner uid, so `mv 1 1.bak && ln -s /home/jaah 1` would admit the
    fleet tmux server and every pane. The release path sweeps `orphans_only=False` over a CP-named path.
    All four required:
      * `JAAH_HOME` resolves.
      * the slot directory is not ITSELF a symlink (the swap a runner can do).
      * the resolved path is strictly under the pool root, at least `_MIN_REAPABLE_DEPTH` deep.
      * it has `.git` (catches a symlink planted elsewhere in the chain).
    Fails CLOSED: `""` and the caller reaps nothing."""
    raw = (wt or "").rstrip("/")
    if not raw:
        return ""
    # Raw: `realpath` must see exactly what the environment spells. An unset root resolves to the cwd,
    # which the conditions below must reject on their own merits.
    home = os.path.realpath(environment.pool_root_raw())
    if not home or home == os.sep:
        return ""
    # Asked of the un-resolved path: `realpath` is what the attack subverts.
    if os.path.islink(raw):
        return ""
    real = os.path.realpath(raw)
    if real == home or not real.startswith(home + os.sep):
        return ""
    if len(real[len(home):].strip(os.sep).split(os.sep)) < _MIN_REAPABLE_DEPTH:
        return ""
    if not os.path.exists(os.path.join(real, ".git")):
        return ""
    return real


def _reap_windows_tree(wt: str) -> list[dict[str, object]]:
    """Windows `reap_orphan_processes` (wide mode): the tree's processes, signalled AS THE SESSION USER.

    Killing the runner's tmux window ends the runner, but a child whose parent has already exited (the
    runner's own `sleep 900`, measured on a real PC) outlives it, and the daemon's account
    holds no right to terminate a runner process. So the runner account does it, by the MSYS runtime's own
    record of each process's cwd. The tree is validated exactly as on posix (`_reapable_tree`)."""
    from host_platform import windows  # the MSYS sweep exists only there
    real = _reapable_tree(wt)
    if not real:
        log.error("reap_orphan_processes: REFUSING to sweep %r — not a pool worktree under JAAH_HOME", wt)
        return []
    drive, rest = ntpath.splitdrive(real)
    tree = (f"/{drive[0].lower()}" if drive.endswith(":") else drive) + rest.replace("\\", "/")
    try:
        proc = session_user.run_as_session_user(
            ["/bin/sh", "-c", windows.REAP_TREE_SH],
            env={"SU_TREE": tree.rstrip("/"), "SU_GRACE": str(int(_ORPHAN_TERM_GRACE_SECS))},
            timeout=_ORPHAN_TERM_GRACE_SECS + 60)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log.error("reap_orphan_processes: could not sweep %s as the session user (%s) — the tree may still "
                  "be dirty", wt, exc)
        return []
    return [{"pid": int(pid), "cmd": processes.UNREADABLE_CMD, "signal": "TERM"}
            for pid in (proc.stdout or "").split() if pid.isdigit()]


def _kill_across_uid_split(pid: int, sig: int) -> None:
    """`os.kill`, escalated through the SESSION USER when this process may not signal PID directly.

    Group membership grants no signal permission, so every reap of a runner's leftovers raised EPERM
    silently (measured: orphans held two `free` slots for two days). Escalates via the same seam tmux
    uses (`session_user.run_as_session_user`) — no `CAP_KILL`, no setuid helper; on a single-uid host
    `os.kill` already succeeds. RAISES `OSError` when it still cannot signal; `ProcessLookupError`
    propagates untouched (already gone = the wanted outcome)."""
    try:
        os.kill(pid, sig)
        return
    except PermissionError:
        pass
    if not session_user.can_run_as_session_user():
        raise PermissionError(
            errno.EPERM,
            f"may not signal pid {pid} (this process is uid {os.geteuid()}) and no session-user switch "
            f"is available to escalate through",
        )
    if pid <= 1:
        # A floor: `kill -TERM -1` means "everything this uid may signal".
        raise ValueError(f"refusing to signal pid {pid}")
    try:
        proc = session_user.run_as_session_user(
            ["kill", f"-{int(sig)}", str(int(pid))], timeout=processes._SESSION_PROBE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — RuntimeError / TimeoutExpired, neither an OSError
        # Converted to this pid's refusal: unconverted, a non-`OSError` would abandon every remaining pid
        # unreported through the caller's blanket `except`.
        raise PermissionError(errno.EPERM, f"session-user switch failed for pid {pid}: {exc}") from exc
    # Gone is success, whatever the exit code says.
    if proc.returncode != 0 and os.path.exists(f"/proc/{pid}"):
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise PermissionError(
            errno.EPERM, f"session-user `kill -{int(sig)} {pid}` did not succeed: {detail}")


def _in_this_process_group(pid: int) -> bool:
    """Is PID in the process group this daemon runs in? True also when its pgid cannot be read.

    `os.getpgid`, not `pid in {getpid(), getpgrp()}` (two values, not a group — it admitted the daemon's
    children). Fails CLOSED: an unreadable pgid means the identity check below could not succeed either."""
    try:
        return os.getpgid(pid) == os.getpgrp()
    except OSError:
        return True


def _warranted_pids(extra_pids: Iterable[tuple[int, str]]) -> dict[int, str]:
    """`reap_orphan_processes`' caller-warranted `(pid, proc_started_at)` pairs → `{pid: start stamp}`.

    Admitted, never coerced: a warranted pid never passes `_reapable_tree`. `type(...) is int` (`True` is not
    a pid); `<= 1` refused. A pid alone is not an identity (pids recycle): no start stamp, no warrant.

    The stamp is read by `reconcile.context._session_metas_for` and re-read here moments later, so this catches a
    recycle INSIDE the sweep — not an hour-old sighting whose number was reused (both reads see the
    stranger). That is bounded by the 5s leased floor in `reconcile.observe.observe_slot`; this is defence in
    depth. The daemon's own process group is excluded (`_in_this_process_group`); other processes are
    covered by the identity re-assertion at signal time."""
    admitted: dict[int, str] = {}
    for item in extra_pids:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        pid, started = item
        if type(pid) is not int or pid <= 1:
            continue
        if type(started) is not str or not started:
            continue                       # half a warrant is no warrant: a pid without its start stamp
        if _in_this_process_group(pid):
            continue
        if not _names_a_claude_session(pid):
            continue                       # the warrant names something that is not a session — see above
        admitted[pid] = started
    return admitted


# A warrant must name a `claude`, anchored at argv[0]: warrants come from `claude agents --json`, whose
# state the session user writes, and every other warrant check is about the NUMBER — so a forged record
# could nominate any pid for `_kill_across_uid_split` at the daemon's uid. Unreadable is not a claude.
_CLAUDE_CMDLINE_RE = re.compile(r"^(?:\S*/)?(?:claude|node\s+\S*/claude)(?:\s|$)")


def _names_a_claude_session(pid: int) -> bool:
    """True when PID's argv[0] is a `claude`. The warrant's own shape check — see the note above."""
    cmdline = processes._read_cmdline(pid)
    if not cmdline:
        return False
    return bool(_CLAUDE_CMDLINE_RE.match(cmdline.strip()))


def _warrant_refusal(pid: int, expected: str) -> str:
    """`""` when PID is still the process the warrant named, else the one line saying why it is not.

    The extra pid's `_still_reapable`, run at the same two points (a number can recycle inside the TERM
    grace, and an extra pid never proved a cwd). An unreadable start time is a refusal; the caller records
    it in `refused`."""
    actual = processes.proc_started_at(pid)
    if not actual:
        return (f"warrant not re-verified: start time unreadable, so this pid could not be shown to be "
                f"the process the warrant named (started {expected})")
    if actual != expected:
        return f"warrant stale: pid recycled — started {actual}, warrant named {expected}"
    return ""


def reap_orphan_processes(
    wt: str,
    *,
    orphans_only: bool = True,
    extra_pids: Iterable[tuple[int, str]] = (),
    kill: Callable[[int, int], None] = _kill_across_uid_split,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, object]]:
    """Kill the processes rooted in `wt` and return one record per pid signalled — ORPHANS by default,
    EVERYTHING (docker excepted) when `orphans_only=False`.

    The wide mode is the release's: the CP decided the slot is released, so whatever sits in the tree goes.
    Asserts `ppid == 1` and the cwd match TWICE: building the set, and per pid right before the signal.

    `extra_pids` is the caller's WARRANT — `(pid, proc_started_at)`, since a pid alone is not an identity —
    for a registered `claude` session not rooted in the tree (measured: a session outlived its release by
    76 minutes with `cwd_process_count = 0`). Same TERM → grace → KILL ladder and accounting; re-asserted
    on IDENTITY (`_warrant_refusal`), not the tree. An extra pid is warranted ALONE — never its
    descendants: an unbounded walk from an unproven pid (e.g. the fleet tmux server) would take every pane.
    Nothing is lost: a descendant really in the tree is cwd-rooted and in `roots`.

    SIGTERM, then SIGKILL after `_ORPHAN_TERM_GRACE_SECS` (TERM gives a stack or port holder its shutdown
    path; the grace is the mercy, not a veto). Returns `[{"pid", "cmd", "signal"}]`, `cmd` redacted.
    Never raises. A pid it could not signal is logged at ERROR here — the only surface saying the tree is
    dirty (`session` is claude-only), since `[]` reads like a clean tree."""
    records: list[dict[str, object]] = []
    # Which pids could not be signalled, and why.
    refused: dict[int, str] = {}
    # Hoisted: a `NameError` from the `finally` would replace the promised return.
    cmds: dict[int, str] = {}
    wt_real = ""
    try:
        if host_platform.is_windows():
            # The release/kill sweep only: an `orphans_only` pass has no parent to judge "orphan" by here.
            if not orphans_only:
                records.extend(_reap_windows_tree(wt))
            return records
        if not os.path.isdir("/proc"):
            return records
        wt_real = _reapable_tree(wt)
        if not wt_real:
            log.error(
                "reap_orphan_processes: REFUSING to sweep %r — it does not resolve to a pool worktree "
                "under JAAH_HOME (%r). The resolved path IS the kill set, and the pool parent is "
                "writable by the session user, so an unvalidated one is a node-wide kill primitive. "
                "Nothing was signalled",
                wt, environment.pool_root_raw(),
            )
            return records
        roots, unreadable = _reap_roots(wt_real, orphans_only=orphans_only)
        # A standing fault: once per pid set (the repeating-fault block).
        fault_key = f"reap-unreadable:{wt_real}"
        if unreadable:
            speak = log.error if processes._fault_is_new(fault_key, unreadable) else log.debug
            speak(
                "reap_orphan_processes: %d process(es) in %s have cwds neither this process (uid %d) nor "
                "the session user could read, so they were NOT selected — the tree may still be dirty and "
                "`session` (claude-only) will not say so. Check `jaah-runner@<node>.service`",
                len(unreadable), wt, os.geteuid(),
            )
        else:
            processes._fault_cleared(fault_key)
        extra = _warranted_pids(extra_pids)
        # Both empty, not `roots` alone: the warrant's case has zero cwd-rooted processes.
        if not roots and not extra:
            return records
        # Descendants inherit their root's warrant (no `ppid == 1` re-test). The walk starts from `roots`
        # ALONE; warranted pids are unioned in afterwards (see the docstring).
        targets = _orphan_descendants(roots) | set(extra)
        # Named before the first signal: a killed process's cmdline is gone.
        cmds.update({
            pid: (processes._cmd_summary(cmdline) if (cmdline := processes._read_cmdline(pid)) else processes.UNREADABLE_CMD)
            for pid in targets
        })

        termed: set[int] = set()
        for pid in sorted(targets):
            # A root is tested as a root and NEVER also as a warrant (nested, so the `elif` below never
            # runs for a root).
            if pid in roots:
                if not _still_reapable(pid, wt_real, orphans_only=orphans_only):
                    continue
            # The warrant's re-assertion, right before the first signal — the only thing between a
            # warranted pid and a signal. The `elif` binds to `if pid in roots`.
            elif why := (_warrant_refusal(pid, extra[pid]) if pid in extra else ""):
                refused[pid] = f"TERM: {why}"
                continue
            try:
                kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue                      # already gone — the outcome we wanted, not a failure
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                refused[pid] = f"TERM: {exc}"
                continue
            termed.add(pid)
            records.append({"pid": pid, "cmd": cmds.get(pid, processes.UNREADABLE_CMD), "signal": "TERM"})
        if not termed:
            return records

        deadline = monotonic() + _ORPHAN_TERM_GRACE_SECS
        while monotonic() < deadline:
            if not any(os.path.exists(f"/proc/{pid}") for pid in termed):
                return records
            sleep(_ORPHAN_REAP_POLL_SECS)
        for pid in sorted(pid for pid in termed if os.path.exists(f"/proc/{pid}")):
            # The root re-test again (recycle inside the grace), nested as in the TERM loop.
            if pid in roots:
                if not _still_reapable(pid, wt_real, orphans_only=orphans_only):
                    continue
            # And the warrant's, for the same window.
            elif why := (_warrant_refusal(pid, extra[pid]) if pid in extra else ""):
                refused[pid] = f"KILL: {why}"
                continue
            try:
                kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                refused[pid] = f"KILL: {exc}"
                continue
            records.append({"pid": pid, "cmd": cmds.get(pid, processes.UNREADABLE_CMD), "signal": "KILL"})
        return records
    except Exception:  # noqa: BLE001 — see the docstring: debris left is the status quo, a raise is not
        log.exception("reap_orphan_processes: failed while reaping %s", wt)
        return records
    finally:
        # Once per fact, keyed on the RESOLVED tree (two spellings of one tree must share one memo).
        refusal_key = f"reap-refused:{wt_real or wt}"
        if refused:
            # Fingerprint on (pid, why) pairs: the same pid refused for a new reason is a new fault.
            speak = log.error if processes._fault_is_new(refusal_key, tuple(sorted(refused.items()))) else log.debug
            speak(
                # "Selected for", not "rooted in": a warrant refusal is not rooted in the tree. Two causes
                # (uid split vs identity), so the line says which reason means which.
                "reap_orphan_processes: could NOT signal %d process(es) selected for %s — the tree stays "
                "dirty and nothing else reports it (`session` is claude-only). This process is uid %d. "
                "A `TERM:`/`KILL:` reason naming EPERM is the uid split — signalling a runner's debris "
                "needs `session_user.run_as_session_user` available here; one naming a stale or "
                "unreadable warrant is a pid this sweep declined on identity and left alone. Refused: %s",
                len(refused), wt, os.geteuid(), _refusal_summary(refused, cmds),
            )
        else:
            processes._fault_cleared(refusal_key)
