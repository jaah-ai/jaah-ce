"""The node half of `destroy_slot`: the synchronous mechanics `reconcile.enact_signal._enact_destroy_slot` runs
off the event loop, after it has killed what runs in the tree. Policy (when a slot is destroyed, what its
ack does to the row and the task) is the CP's (`app/slot_allocation.py::retire`, `app/slot_destroy.py`).

Three steps, in this order, each reported on the ack's `report`:

1. `scan_unpushed` — what would be lost: commits on the tree's checked-out HEAD and on the task's own
   `fleet/task-<id>` branch that no `origin` ref contains, read BEFORE the erase. Scoped to those two refs
   because `refs/heads` is clone-wide: every other local branch belongs to a sibling slot, or to no slot.
2. `compose_down` — `docker compose -p <compose_project> down -v --rmi local --remove-orphans`, volumes and
   built images included. Not `stack_ops.stop_stack`, which refuses `-v` on purpose (its idle stop keeps
   data); a destroyed slot's data is the thing being destroyed. What still carries the project's label is
   removed after it. A node with no docker (rc 127) has no stack: the step is skipped, not failed.
3. `erase_tree` — `worktree_ops.worktree_erase`: `git worktree remove --force` and prune, the slot's scratch
   dirs, an already-absent tree counted erased. It never refuses for what the tree holds; its containment
   checks (the path is not this slot's) are the ack's `refused`.

The `_shell`/`_erase` seams are module globals so a test substitutes docker and the erase handler without
touching git."""
from __future__ import annotations

import json
import logging
import re
import shutil

log = logging.getLogger("jaahd.slot_destroy")

# The commit list per branch is bounded: the ack is a JSON body, and the note names a dozen at most
# (`apps/api/app/slot_destroy.py::lost_work_summary`).
_MAX_SHAS = 200

# `slot_allocation.retire` composes `p<project_id>-<number>`; anything else is refused unrun, so a
# malformed payload can never take down a stack this slot does not own.
_COMPOSE_PROJECT = re.compile(r"p([0-9]+)-([0-9]+)")


def compose_project_for(payload: dict, project_id: int | None, number: str) -> str:
    """The payload's `compose_project` when it names THIS slot — its roster project id and its index — else
    `""`. A roster that did not say the project id proves nothing, so it matches nothing."""
    value = str(payload.get("compose_project") or "")
    match = _COMPOSE_PROJECT.fullmatch(value)
    if project_id is None or match is None or match.groups() != (str(project_id), str(number)):
        return ""
    return value


def scan_unpushed(cwd: str, *, head: bool, task_branch: str) -> list[dict] | dict:
    """`[{"branch", "shas"}]` for each looked-at ref with commits on no `origin` ref, or
    `{"unknown": True, "reason"}` when git could not answer — "nobody could look" never reads as "nothing
    lost". `head` adds the tree's own checkout (absent once the tree is gone); `task_branch` (`""` = none)
    is read from the clone, where it outlives the tree."""
    from worktree_mechanics.core import _git  # noqa: PLC0415 — deferred, as every git caller in this package

    refs: list[tuple[str, str]] = []
    if head:
        rc, out, _err = _git(cwd, "symbolic-ref", "-q", "--short", "HEAD")
        refs.append((out.strip(), out.strip()) if rc == 0 and out.strip() else ("HEAD (detached)", "HEAD"))
    if task_branch and all(label != task_branch for label, _ref in refs):
        rc, _out, _err = _git(cwd, "rev-parse", "--verify", "-q", f"refs/heads/{task_branch}")
        if rc == 0:
            refs.append((task_branch, f"refs/heads/{task_branch}"))
    found: list[dict] = []
    for label, ref in refs:
        rc, out, err = _git(cwd, "rev-list", ref, "--not", "--remotes=origin")
        if rc != 0:
            return {"unknown": True, "reason": f"git rev-list {label} failed: {(err or '').strip()[:200]}"}
        shas = out.split()
        if shas:
            found.append({"branch": label, "shas": shas[:_MAX_SHAS]})
    return found


NO_DOCKER = "docker not installed: no stack to stop"


def _docker_missing() -> bool:
    """No `docker` on this daemon's PATH (`slot_residue.purge`'s same skip). A seam, as `_shell`."""
    return shutil.which("docker") is None


def _shell(argv: list[str], cwd: str) -> dict:
    """One docker command as the session user (`stack_ops._dc_step`: the daemon uid has no docker
    socket). `{exit_code, stdout, stderr}`."""
    import commands  # noqa: PLC0415 — deferred: `commands` → `reconcile` → here is a cycle
    import stack_ops  # noqa: PLC0415 — deferred, as above

    return stack_ops._dc_step(commands._native_run, argv, cwd)


def compose_down(compose_project: str, cwd: str) -> tuple[bool, str]:
    """Take the stack down with its volumes and the images compose built for it (`--rmi local`), then
    remove what still carries the project's label — a down run where no compose file names the services
    (the tree is gone, or `.env` points elsewhere) leaves volumes and built images behind. `(ok, detail)`;
    `detail` names anything that still survived (logged, never a failure: the down itself succeeded)."""
    # No docker on this node (a CE container node): it never ran a stack, so there is none to stop. Asked of PATH
    # first (the in-process run raises rather than answer 127), and of the delegated shell's 127 after.
    if _docker_missing():
        return True, NO_DOCKER
    down = _shell(["docker", "compose", "-p", compose_project, "down", "-v", "--rmi", "local",
                   "--remove-orphans"], cwd)
    if down.get("exit_code") == 127:
        return True, NO_DOCKER
    if down.get("exit_code") != 0:
        why = (down.get("stderr") or down.get("stdout") or "").strip()[:500]
        return False, f"docker compose -p {compose_project} down -v failed (rc={down.get('exit_code')}): {why}"
    label = f"label=com.docker.compose.project={compose_project}"
    left: list[str] = []
    for kind in ("volume", "image"):
        listed = _shell(["docker", kind, "ls", "-q", "--filter", label], cwd)
        # `image ls -q` prints an id once per tag. One at a time, never forced: the engine refuses an image
        # another project's container still uses, and that refusal must not count the rest as survivors.
        for name in sorted(set((listed.get("stdout") or "").split())) if listed.get("exit_code") == 0 else []:
            if _shell(["docker", kind, "rm", name], cwd).get("exit_code") != 0:
                left.append(name)
    if left:
        log.warning("destroy_slot: %s is down but %s survived it — left for an operator",
                    compose_project, ", ".join(left))
        return True, f"not removed by the down: {', '.join(left)}"
    return True, ""


def _erase(project: str, number: int, clone: str) -> dict:
    """`worktree_ops.worktree_erase`, forced, with the stack already down (no `stack_down_cmd`)."""
    import commands  # noqa: PLC0415 — deferred: the cycle `_shell` names
    import worktree_ops  # noqa: PLC0415 — deferred, as above

    return worktree_ops.worktree_erase(
        "worktree_erase",
        {"project": project, "number": number, "clone_path": clone, "force": True},
        commands._native_run,
    )


def erase_folder(project: str, number: int, clone: str, project_id: int | None) -> dict:
    """Erase a FOLDER slot (`worktree_ops.folder_erase`) → `{"erased", "refused", "detail"}`. `refused` is
    a containment verdict (exit -1) a retry will not change; anything else unerased is a partial failure the
    CP re-orders. Nothing to scan, take down or bundle: a folder slot has no git history and no stack."""
    import worktree_ops  # noqa: PLC0415 — deferred: the cycle `_shell` names

    reply = worktree_ops.folder_erase(project, number, clone, project_id)
    try:
        body = json.loads(reply.get("stdout") or "{}")
    except ValueError:
        body = {}
    if reply.get("exit_code") == 0:
        return {"erased": True, "refused": False, "detail": "already absent" if body.get("already_absent") else ""}
    return {"erased": False, "refused": reply.get("exit_code") == -1,
            "detail": (reply.get("stderr") or "").strip()[:2000] or "the erase reported no reason"}


def erase_tree(project: str, number: int, clone: str) -> dict:
    """Erase the tree → `{"erased", "refused", "detail"}`.

    `refused` is a verdict a retry will not change: the erase's containment checks (exit -1). Anything
    else unerased — a busy erase lock, a filesystem error — is a partial failure the CP re-orders."""
    reply = _erase(project, number, clone)
    try:
        body = json.loads(reply.get("stdout") or "{}")
    except ValueError:
        body = {}
    detail = (reply.get("stderr") or "").strip()[:2000]
    if reply.get("exit_code") == 0:
        return {"erased": True, "refused": False,
                "detail": "already absent" if body.get("already_absent") else ""}
    refused = reply.get("exit_code") == -1
    return {"erased": False, "refused": refused,
            "detail": detail or "the erase reported no reason"}
