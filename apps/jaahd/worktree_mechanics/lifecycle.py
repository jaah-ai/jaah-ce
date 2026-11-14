"""Slot lifecycle — the clone, guard and claim of a repository slot, and a folder slot's folder."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import clone_identity
import env_helpers
import path_module
import session_user
import slot_clone

from worktree_mechanics import core, env_file, ports, managed_env

log = logging.getLogger("jaahd.worktree_mechanics.lifecycle")


# A folder slot's own mode: owner-only, as `WORKTREE_PRIVATE_MODES` is for a tree's secret files — the folder
# is the runner's whole working area, and nothing else on the node has a reason to read it.
FOLDER_SLOT_MODE = 0o700


def create_folder(config: "core.PoolConfig", n: int | str) -> str:
    """Make folder slot N exist, EMPTY, and return its path — the whole of a folder slot's materialisation.

    An existing directory is returned UNTOUCHED: a resume or relaunch into a held folder keeps every file
    the task wrote (AC7), as a lease leaves a held tree alone on a resume. Nothing is seeded — no
    clone, `.git`, `.env` or template. Created as the session user (the runner owns it) with
    `FOLDER_SLOT_MODE`; the parent `<jaah_home>/repos/<pid>` is created first, since a project with no
    repository has no clone to have made it. Raises `ResetRefused` when the directory cannot be made."""
    if config.has_repo:
        raise core.ResetRefused(f"create-folder: {config.name} has a repository — its slots are git clones")
    return ensure_folder(config.wt_path(n), config.name)


def ensure_folder(wt: str, name: str) -> str:
    """`create_folder` by path: make folder slot `wt` of project `name` exist, empty, owner-only, as the session
    user, and return it; an existing directory is returned untouched. The console launcher's way to make a
    folder slot it was handed (`clauded_wt.py`). Raises `ResetRefused` when it cannot."""
    if os.path.islink(wt):
        # `isdir` follows a link, so a planted one would hand the runner a directory somewhere else.
        raise core.ResetRefused(f"folder-unusable: {wt} is a symlink — refusing to launch into it")
    if os.path.isdir(wt):
        return wt
    parent = os.path.dirname(wt.rstrip("/")) or "/"
    err = session_user.ensure_dir_as_session_user(parent)
    if err:
        raise core.ResetRefused(f"folder-unresolvable: cannot create {parent} for {name}: {err}")
    err = session_user.ensure_dir_as_session_user(wt, mode=FOLDER_SLOT_MODE)
    if err:
        raise core.ResetRefused(f"folder-unresolvable: cannot create folder slot {wt} for {name}: {err}")
    log.info("create_folder: %s → %s (empty)", name, wt)
    return wt


def folder_record(config: "core.PoolConfig", n: int | str, wt: str, *, task_id: object, account: str,
                  session: bool) -> dict:
    """The slot record for a folder slot: the git, port and stack fields carry nothing to sense."""
    t = env_helpers.now_iso()
    return {
        "number": str(n), "path": wt, "project": config.name, "branch": None, "task_id": task_id,
        "account": account, "ports": {}, "urls": {}, "session": session, "merged": not session,
        "stack": "none", "last_active": t, "idle_hours": 0, "last_used": t,
    }


# ── The access guard (repo-access-levels.md § The git-native guard) ──────────────────────────────────────
# The marker every installed guard writes (`guards/install-access-guard.py::PUSH_ENABLED_CONFIG_KEY`).
_GUARD_PUSH_MARKER = "access-guard.push-enabled"
_GUARD_PREFIX_MARKER = "access-guard.branch-prefix"
_GUARD_PROTECT_MARKER = "access-guard.protect-integration"


def access_guard_installer():
    """`guards/install-access-guard.py` as a module (its file name is not importable)."""
    path = Path(__file__).resolve().parents[1] / "guards" / "install-access-guard.py"
    return path_module.load(path, "_jaah_install_access_guard")


def install_access_guard(clone: str, branch_prefix: str, push_enabled: bool, integration_branch: str = "", *,
                         installer=None, allow_force_push: bool | None = None,
                         protect_integration: bool = False, jaah_repo_prefix: str | None = None) -> None:
    """Install the guard's hook and markers in `clone`. Raises on failure: the caller decides what an unguarded
    clone costs. As the session user when the node switches (new inodes in `.git`), through the installer
    itself as `worktree_ops.sync_access_guard` runs it; on one uid, the installer's own `apply_rules`
    in-process — no CP lookup for a clone it is handed. `installer` loads that module (default
    `access_guard_installer`); an empty `integration_branch`, `allow_force_push=None` or `jaah_repo_prefix=None`
    leaves that marker as it is; `protect_integration` True writes the opt-in integration protection (False
    writes nothing)."""
    if session_user.switch_needed():
        import worktree_ops  # noqa: PLC0415 — command-layer module; only its installer argv is used

        proc = session_user.run_as_session_user(
            worktree_ops.access_guard_argv(clone, branch_prefix, push_enabled, integration_branch, allow_force_push,
                                           protect_integration, jaah_repo_prefix),
            timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"installer exited {proc.returncode}: {(proc.stderr or '').strip()[-300:]}")
        return
    module = (installer or access_guard_installer)()
    module.apply_rules(clone, module.normalize_prefix(branch_prefix), push_enabled, integration_branch or None,
                       protect_integration=protect_integration, allow_force_push=allow_force_push,
                       jaah_repo_prefix=jaah_repo_prefix)


def _local_marker(clone: str, key: str) -> tuple[int, str]:
    """`(rc, value)` of a guard marker in the clone's OWN config file: `--local`, as the hook reads it — a value
    in the session user's `~/.gitconfig` is the runner's to plant, never the guard (repo-access-levels.md)."""
    rc, out, _err = core._git(clone, "config", "--local", "--get", key)
    return rc, out.strip()


# ── The slot's clone (worktree-pool-policy.md §Layout) ──────────────────────────────────────────────────
def prepare_tree(config: "core.PoolConfig", wt: str, branch: str, acting: "clone_identity.Acting | None") -> None:
    """Make slot tree `wt` a standalone clone of `config.clone_url` on the hold's start point (`slot_clone.prepare`:
    cloned when absent, refreshed when held over, re-cloned over the retired layout). `branch` "" detaches at
    base (a blind review child). Raises `slot_clone.CloneFailed` (the forge) or `ResetRefused` (anything local, or
    no acting task)."""
    if acting is None:
        raise core.ResetRefused(f"clone-unresolvable: no task acts for {wt}, so there is no access to clone it with")
    slot_clone.prepare(wt, config.clone_url, config.base, branch, acting, push_missing_base=config.push_enabled)


def _primary_repo_id(config: "core.PoolConfig") -> int | None:
    primary = next((spec for spec in config.repos if getattr(spec, "primary", False)), None)
    return getattr(primary, "id", None)


def _allow_force_push(config: "core.PoolConfig") -> bool | None:
    """The primary repository's Allow force push from the roster, or None (an older CP: the marker is left)."""
    primary = next((spec for spec in config.repos if getattr(spec, "primary", False)), None)
    return getattr(primary, "allow_force_push", None)


def stamp_repo_id(config: "core.PoolConfig", clone: str) -> None:
    """Write the primary's `project_repos.id` into the slot clone (`slot_repos.REPO_ID_KEY`), which tells a later
    converge which repository the slot path holds. None for a project whose roster names no repositories."""
    rid = _primary_repo_id(config)
    if rid is not None and core._git(clone, "config", "jaah.repoId", str(rid))[0] != 0:
        log.warning("stamp_repo_id: could not stamp %s with repository %s", clone, rid)


def guard_clone(config: "core.PoolConfig", clone: str) -> None:
    """Install the project's CURRENT push guard in `clone` (`branch_prefix`, `push_enabled`, the integration
    branch, Allow force push — the CP's row, never read off the clone) on every lease: a held-over clone gets
    any change made while it was free. A clone `prepare_tree` just landed on an EMPTY forge
    (`slot_clone.has_no_branch`) also gets integration protection: its one genesis push (the integration branch
    and `main` in one command) goes through, and every later direct push to them is refused for the rest of the
    hold, whatever the prefix (the hook's docstring). Raises `ResetRefused` when the markers did not land: a
    clone that could push where the project may not is never handed out (fails closed)."""
    protect = slot_clone.has_no_branch(clone)
    try:
        install_access_guard(clone, config.branch_prefix, config.push_enabled, config.base,
                             allow_force_push=_allow_force_push(config), protect_integration=protect,
                             jaah_repo_prefix=config.jaah_repo_prefix)
    except Exception as exc:  # noqa: BLE001 — judged by the markers below
        log.warning("guard_clone: could not install the access guard in %s: %s", clone, exc)
    if (_local_marker(clone, _GUARD_PUSH_MARKER)[1] != ("true" if config.push_enabled else "false")
            or _local_marker(clone, _GUARD_PREFIX_MARKER)[0] != 0
            or (protect and _local_marker(clone, _GUARD_PROTECT_MARKER)[1] != "true")):
        raise core.ResetRefused(
            f"guard-failed: {clone} could not be given {config.name}'s push guard — refusing to hand out "
            f"a clone that could push where the project may not; see the node log"
        )


def _copy(src: str, dst: str) -> None:
    """Seed DST from SRC, as the session user where the uid split applies: a NEW file the daemon writes
    into a worktree must be owned by `jaah` (the in-place .env applicators are fine — rewriting an
    existing inode preserves its owner). Failure is a warning: an unseeded .env is degraded, not broken."""
    if session_user.switch_needed():
        try:
            proc = session_user.run_as_session_user(["cp", "--", src, dst], timeout=30)
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch unavailable), TimeoutExpired, OSError
            log.warning("could not seed %s from %s as the session user: %s", dst, src, exc)
            return
        if proc.returncode != 0:
            log.warning("could not seed %s from %s: %s", dst, src, (proc.stderr or "").strip())
        return
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        with open(dst, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        log.warning("could not seed %s from %s: %s", dst, src, exc)


# ── claim_mechanics — the in-process entry point ────────────────────────────────────────────────────────
def claim_mechanics(
    config: "core.PoolConfig",
    n: int | str,
    *,
    account: str,
    acting: "clone_identity.Acting | None" = None,
    task_id: object = None,
    node_id: str | None = None,
    # The node-assigned port range `(floor, ceiling)`; None = the module default.
    auto_ports: "tuple[int, int] | None" = None,
    # True for a blind review child: HEAD detached at base, no task branch.
    detach: bool = False,
    # `payload["env_vars"]` verbatim. Default None is load-bearing: never rebuilds (`apply_managed_env`),
    # only withdraws a marked slot's names.
    env_vars: "dict | None" = None,
) -> dict:
    """The mechanical lifecycle of a fresh lease on the slot the CP DICTATED, and ONLY that: make slot `n` a
    standalone clone on the task's branch (`prepare_tree`, with the leasing task's access `acting`), install the
    push guard, seed `.env`, write the CP's environment, node-assigned ports and compose/node identity, and assemble the
    slot record. Takes no flock and writes no ledger — it RETURNS the record and the daemon declares it.

    Nothing here chooses but the ports: `n` and `account` arrive decided; occupancy is `worktrees.state`, bound CP-side
    by `slot_state.lease()` before this runs. A resume never comes here: its tree is left byte for byte.

    The task's branch is `core.task_branch` (`{branch_prefix}fleet/task-<id>`, `fleet/` once), started from `origin/<branch>` when the forge has it,
    else from `origin/<base>` (`slot_clone.land`).

    A FOLDER slot (`config.has_repo` False) is only `create_folder`: the existing folder is left exactly as
    it is, and no clone, `.env` overlay, port or identity is applied."""
    try:
        chosen = int(n)
    except (TypeError, ValueError):
        raise core.ResetRefused(f"claim-mechanics: {n!r} is not a slot number") from None
    if not config.has_repo:
        wt = create_folder(config, chosen)
        return folder_record(config, chosen, wt, task_id=task_id, account=account, session=True)

    branch = ""
    if not detach and task_id is not None and str(task_id).strip():
        # The conventional branch, DERIVED: `dispatch_compose/`'s charter is the one place the spelling is written.
        branch = core.task_branch(config.branch_prefix, task_id)
    wt = config.wt_path(chosen)
    prepare_tree(config, wt, branch, acting)
    guard_clone(config, wt)
    stamp_repo_id(config, wt)
    if not os.path.isfile(os.path.join(wt, ".env")) and os.path.isfile(os.path.join(wt, ".env.example")):
        _copy(os.path.join(wt, ".env.example"), os.path.join(wt, ".env"))
        log.info("seeded .env from .env.example")
    # After the checkout: it recreates files at the unit's umask. Before the applicators, so the first port is
    # written through the seam an owner-only `.env` requires.
    env_file.harden_worktree_modes(wt)
    # The rebuild's one seam: after `harden_worktree_modes` (which owns the mode), before the applicators.
    managed_env.apply_managed_env(wt, env_vars, n=chosen, port_vars=config.port_vars)
    floor, ceiling = auto_ports or (ports.AUTO_PORT_FLOOR, ports.AUTO_PORT_CEILING)
    ports.apply_auto_ports(wt, port_vars=config.port_vars, floor=floor, ceiling=ceiling,
                           preferred=config.port_preferred)
    ports.apply_stack_identity(wt, config.repo, chosen)   # placement, never the display name — see `stack_base`
    ports.apply_node_identity_env(wt, chosen, node_id)

    slot_ports = ports.wt_ports_json(wt, config.port_vars)
    urls = ports.wt_urls_json(slot_ports)
    stack = "up" if ports.has_stack(wt) else "none"
    t = env_helpers.now_iso()
    return {
        "number": str(chosen),
        "path": wt,
        "project": config.name,
        "branch": branch or "DETACHED",
        # No `holder`: every field here is sensed; `worktrees.state` is the CP's.
        "task_id": task_id,
        "account": account,
        "ports": slot_ports,
        "urls": urls,
        "session": True,
        "merged": False,
        "stack": stack,
        "last_active": t,
        "idle_hours": 0,
        "last_used": t,
    }
