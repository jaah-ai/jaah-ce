"""Reconcile's shared vocabulary: wire constants, probe budgets and the subprocess/clock/container primitives."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from datetime import datetime, timezone

import control_plane_api
from jaah_env import env_get

log = logging.getLogger(__name__)


# Policy thresholds live CP-side; only probe costs remain here.
AGENTS_PROBE_TIMEOUT = 15

GIT_FAST_TIMEOUT = 10

GIT_FETCH_TIMEOUT = 20


# The observation wire version; bump only for a breaking record change. A pid alone is not an identity
# (recycled): `session_pid` always ships with `session_proc_started_at`.
OBSERVATION_VERSION = 3


# Console tri-state: a downed bridge is `probe_unavailable`, never a false `not_attached`.
CONSOLE_ATTACHED = "attached"

CONSOLE_NOT_ATTACHED = "not_attached"

CONSOLE_PROBE_UNAVAILABLE = "probe_unavailable"


# Intent verbs this node can enact — not the set that exists. A verb with no executor is answered
# `unsupported_verb` once, terminally.
VERB_RESET_TO_BASE = "reset_to_base"


# The idle-stop mechanic only: the CP decides when; this runs the down command its payload names.
VERB_STOP_STACK = "stop_stack"


# The one dispatch channel. `lease_slot` builds/resets and launches (`payload["resume"]` = launch
# into the kept tree); `release_slot` tears down a hold the CP already freed.
VERB_LEASE_SLOT = "lease_slot"

VERB_RELEASE_SLOT = "release_slot"


# Deliver a human's gate answer into the runner blocked on `AskUserQuestion` (rationale:
# `app/fleet_verdicts.py`).
VERB_DELIVER_ANSWER = "deliver_answer"


# Type a CP-composed message into a live runner's pane WITHOUT `Escape` (the merge lane's failure report;
# rationale: `app/fleet_verdicts.py`).
VERB_DELIVER_MESSAGE = "deliver_message"


# Kill a runner that finished without exiting and free its slot. Decided off the DB.
VERB_KILL_RUNNER = "kill_runner"


# Destroy a dynamic slot: kill, record unpushed work, stack down WITH volumes, erase the tree.
VERB_DESTROY_SLOT = "destroy_slot"


# Erase a tree the roster no longer names (a carried-over slot whose project moved to another org), addressed
# by its payload: `worktree_ops.worktree_erase`'s own arguments (`app/slot_destroy.py::enqueue_erase`).
VERB_ERASE_TREE = "erase_tree"


# Queue an operator's steer/interrupt/answer/close for an ACP session's host (`acp/runtime.py`'s control lane),
# addressed by the payload's session id: `{session_id, intent: {id, type, payload}}`.
VERB_RUNTIME_CONTROL = "runtime_control"


# Verbs decided off the DB, arriving with `decided_on_observed_at: None` and all-None preconditions, so R3's
# fail-closed stamp guard (which would refuse every one as `refused_stale`) must not apply. The
# `generation`/`dispatch_id` fence is stronger. `kill_runner`'s own gates in `_enact_kill_runner` are its
# whole fence; `reset_to_base` is minted on an operator Reset, fenced by the task its payload names;
# `destroy_slot` is the CP's order whatever the tree's state (`slot_allocation.retire`).
_UNSTAMPED_VERBS = frozenset(
    {VERB_LEASE_SLOT, VERB_RELEASE_SLOT, VERB_DELIVER_ANSWER, VERB_DELIVER_MESSAGE, VERB_KILL_RUNNER,
     VERB_RESET_TO_BASE, VERB_DESTROY_SLOT, VERB_ERASE_TREE, VERB_RUNTIME_CONTROL})


# Only `done` retires an enacted intent; refusals stay pending for re-decision. `unsupported_verb` also
# retires, or it would re-serve forever.
OUTCOME_DONE = control_plane_api.SLOT_INTENT_DONE

OUTCOME_UNSUPPORTED_VERB = control_plane_api.SLOT_INTENT_UNSUPPORTED_VERB

OUTCOME_REFUSED_STALE = "refused_stale"

OUTCOME_REFUSED_PRECONDITION = "refused_precondition"

OUTCOME_FAILED = "failed"


def _env_int(name: str, default: int) -> int:
    raw = env_get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _epoch(t: str) -> int:
    """`t` as a unix epoch, 0 on parse failure."""
    if not t:
        return 0
    try:
        return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


def _to_utc_iso(t: str) -> str:
    """Normalize any parseable timestamp to the `...Z` form; "" on failure."""
    if not t:
        return ""
    try:
        return (
            datetime.fromisoformat(t.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (ValueError, TypeError):
        return ""


def secs_since(t: str) -> int:
    if not t:
        return 999999
    return int(datetime.now(timezone.utc).timestamp()) - _epoch(t)


def _age_secs(t: str) -> int | None:
    """Age in seconds against the node's own clock (the CP never subtracts a node timestamp from its now).
    None for an unreadable or absent stamp."""
    if not t:
        return None
    return secs_since(t)


async def _sh(argv: list[str], timeout: float, cwd: str | None = None,
              env: dict | None = None) -> tuple[int, str, str]:
    """Async, timeout-bounded subprocess — the one probe primitive; never holds a lock. Timeout kills and
    returns rc=124; a missing executable returns rc=127. `env` is the child's WHOLE environment (None
    inherits)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except OSError:
        return 127, "", ""
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", ""
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def slot_container(path: str) -> str:
    """The directory that would hold a slot at `path` — what `container_state` tests to tell "genuinely gone"
    from "could not look". Shared by `_unprobed_slot_entry` and `reconcile_pass`'s sweep so they agree.

    The path's own parent, not `slot.clone`: cross-mount, the clone can resolve while the slot's mount is
    gone, and for the main slot `path == clone`. `normpath` first (a trailing slash defeats `dirname`)."""
    return os.path.dirname(os.path.normpath(path))


# `present` and `absent` are both observations; only `unreadable` is "could not look".
CONTAINER_PRESENT = "present"

CONTAINER_ABSENT = "absent"

CONTAINER_UNREADABLE = "unreadable"


def container_state(container: str) -> str:
    """Tri-state: does the directory that would hold a slot exist? One `os.stat`.

    Not a boolean: ENOENT bottoming out on a readable ancestor (`_enoent_verdict`) is a sighting of
    absence, while EACCES/EIO/ENOTDIR/ELOOP or a dangling symlink is "could not look", and nothing
    unobserved may be deleted (R1). One-directional: everything else, an empty path and a non-directory are
    `unreadable`. Erring spare-ward costs one pass; the other direction once cost four slots on one
    transient 503."""
    if not container:
        return CONTAINER_UNREADABLE
    try:
        info = os.stat(container)
    except FileNotFoundError:
        return _enoent_verdict(container)
    except (OSError, ValueError):
        return CONTAINER_UNREADABLE
    return CONTAINER_PRESENT if stat.S_ISDIR(info.st_mode) else CONTAINER_UNREADABLE


def _enoent_verdict(container: str) -> str:
    """`os.stat` said ENOENT — an observation or a failure to resolve? `CONTAINER_ABSENT` only for the former.

    `os.stat` answers ENOENT for any unresolvable component, a dangling symlink included. So this walks up:
    every component passed must be missing outright (`lstat` agrees) and the first existing one must be a
    statable directory. A parent-only test would wedge a node that lost its whole `<org>` tree."""
    missing = container
    while True:
        try:
            os.lstat(missing)
        except FileNotFoundError:
            pass  # the name is not there at all — keep walking up
        except (OSError, ValueError):
            return CONTAINER_UNREADABLE
        else:
            # The name exists but did not resolve: a dangling symlink, nothing observed.
            return CONTAINER_UNREADABLE
        parent = os.path.dirname(missing)
        if parent == missing:
            # Nothing readable anchored the absence.
            return CONTAINER_UNREADABLE
        try:
            info = os.stat(parent)
        except FileNotFoundError:
            missing = parent
            continue
        except (OSError, ValueError):
            return CONTAINER_UNREADABLE
        return CONTAINER_ABSENT if stat.S_ISDIR(info.st_mode) else CONTAINER_UNREADABLE
