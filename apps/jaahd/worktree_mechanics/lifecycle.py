"""Slot lifecycle — roster, clone, create, claim and retire of a worktree or folder slot."""
from __future__ import annotations

import logging
import os
import re

import env_helpers
import git_mirror
import session_user

from worktree_mechanics import core, env_file, ports, managed_env, reset

log = logging.getLogger("jaahd.worktree_mechanics.lifecycle")


# A folder slot's own mode: owner-only, as `WORKTREE_PRIVATE_MODES` is for a tree's secret files — the folder
# is the runner's whole working area, and nothing else on the node has a reason to read it.
FOLDER_SLOT_MODE = 0o700


def create_folder(config: "core.PoolConfig", n: int | str) -> str:
    """Make folder slot N exist, EMPTY, and return its path — the whole of a folder slot's materialisation.

    An existing directory is returned UNTOUCHED: a resume or relaunch into a held folder keeps every file
    the task wrote (AC7), exactly as `create_worktree` leaves a built tree alone. Nothing is seeded — no
    clone, `.git`, `.env` or template. Created as the session user (the runner owns it) with
    `FOLDER_SLOT_MODE`; the parent `<jaah_home>/repos/<pid>` is created first, since a project with no
    repository has no clone to have made it. Raises `ResetRefused` when the directory cannot be made."""
    if config.has_repo:
        raise core.ResetRefused(f"create-folder: {config.name} has a repository — its slots are git worktrees")
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


# ── Pool roster from the DB SSOT ────────────────────────────────────────────────────────────────────────
def roster_from_db(project: str, project_id: int | None = None) -> dict[int, dict]:
    """The pool roster for PROJECT — which slots exist, and each one's branch/path/state/last_active —
    read from the DB `Worktree` projection via `GET /api/agent/worktrees`. Returns `{number: record}`.

    "COULD NOT READ" AND "READ, AND IT IS EMPTY" MUST NOT SHARE A RETURN: a failed read RAISES
    `ResetRefused`, and `{}` means genuinely read and genuinely empty. Swallowing the failure is the
    over-dispatch path — an unreachable API makes the whole pool look absent to a caller about to
    materialize a slot. The `control_plane_api` import is lazy, keeping this module third-party-free."""
    try:
        from control_plane_api import agent_api_worktrees
        records = agent_api_worktrees(project, project_id)
    except Exception as exc:  # noqa: BLE001 — unreachable API / import edge; see the docstring
        log.warning("roster_from_db: DB worktree read failed for %s: %s", project, exc)
        raise core.ResetRefused(
            f"roster-unavailable: could not read the dictated slot roster for {project} ({exc}) — refusing "
            f"rather than reading an unreadable roster as an empty pool and allocating on top of it"
        ) from exc
    out: dict[int, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        try:
            n = int(rec.get("number"))
        except (TypeError, ValueError):
            continue
        out[n] = rec
    return out


# ── The access guard (repo-access-levels.md § The git-native guard) ──────────────────────────────────────
# The marker every installed guard writes (`guards/install-access-guard.py::PUSH_ENABLED_CONFIG_KEY`).
_GUARD_PUSH_MARKER = "access-guard.push-enabled"
_GUARD_PREFIX_MARKER = "access-guard.branch-prefix"


def access_guard_installer():
    """`guards/install-access-guard.py` as a module (its file name is not importable)."""
    import importlib.util  # noqa: PLC0415

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "guards",
                        "install-access-guard.py")
    spec = importlib.util.spec_from_file_location("_jaah_install_access_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_access_guard(clone: str, branch_prefix: str, push_enabled: bool, integration_branch: str = "", *,
                         installer=None) -> None:
    """Install the guard's hook and markers in `clone`. Raises on failure: the caller decides what an unguarded
    clone costs. As the session user when the node switches (new inodes in `.git`), through the installer
    itself as `worktree_ops.sync_access_guard` runs it; on one uid, the installer's own `apply_rules`
    in-process — no CP lookup for a clone it is handed. `installer` loads that module (default
    `access_guard_installer`); an empty `integration_branch` leaves its informational marker as it is."""
    if session_user.switch_needed():
        import worktree_ops  # noqa: PLC0415 — command-layer module; only its installer argv is used

        proc = session_user.run_as_session_user(
            worktree_ops.access_guard_argv(clone, branch_prefix, push_enabled, integration_branch), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"installer exited {proc.returncode}: {(proc.stderr or '').strip()[-300:]}")
        return
    module = (installer or access_guard_installer)()
    module.apply_rules(clone, module.normalize_prefix(branch_prefix), push_enabled, integration_branch or None)


def _local_marker(clone: str, key: str) -> tuple[int, str]:
    """`(rc, value)` of a guard marker in the clone's OWN config file: `--local`, as the hook reads it — a value
    in the session user's `~/.gitconfig` is the runner's to plant, never the guard (repo-access-levels.md)."""
    rc, out, _err = core._git(clone, "config", "--local", "--get", key)
    return rc, out.strip()


def _guard_unguarded_clone(config: "core.PoolConfig", clone: str) -> None:
    """Give a clone that carries no guard the project's (`PoolConfig.branch_prefix`/`push_enabled`, the CP's row):
    the CP re-syncs only on a CHANGE (`sync_access_guard`), so a project made with push off would otherwise never
    be guarded. A clone already carrying markers is the CP's to re-sync, never overwritten here. Nothing declared
    (no prefix, push on) installs nothing. Raises `ResetRefused` when the markers did not land: a clone that
    could push where the project may not is never handed out (fails closed)."""
    if not config.branch_prefix and config.push_enabled:
        return
    if _local_marker(clone, _GUARD_PUSH_MARKER)[1]:
        return
    try:
        install_access_guard(clone, config.branch_prefix, config.push_enabled, config.base)
    except Exception as exc:  # noqa: BLE001 — judged by the markers below
        log.warning("ensure_clone: could not install the access guard in %s: %s", clone, exc)
    if (_local_marker(clone, _GUARD_PUSH_MARKER)[1] != ("true" if config.push_enabled else "false")
            or _local_marker(clone, _GUARD_PREFIX_MARKER)[0] != 0):
        raise core.ResetRefused(
            f"guard-failed: {clone} could not be given {config.name}'s push guard — refusing to hand out "
            f"a clone that could push where the project may not; see the node log"
        )


# ── create_worktree ────────────────────────────────────────────────────────────────────────────────────
# Set while `ensure_clone` builds a clone and unset as its last step, so a clone left half-made is finished by
# the next call rather than returned as done.
_CLONE_INCOMPLETE = "jaah.cloneIncomplete"


def ensure_clone(config: "core.PoolConfig") -> str:
    """The CLONE rung of materialisation: make `config.source_clone` exist so a slot under it can be added. A
    rung rather than an operator errand, called from `create_worktree` — the ONE site every
    materialisation path goes through.

    Builds the clone from the project's daemon-owned mirror (`git_mirror`), refreshed from `config.clone_url` —
    the CP's CREDENTIAL-FREE remote — first: `init`, `origin` = `config.clone_url`, a local credential-free fetch
    of every branch and tag (`git_mirror.local_fetch_args`), then the base checked out tracking `origin/<base>`
    (origin's default branch when origin lacks the base; `worktree add` reports its own failure). Not `git
    clone`, which names its remote `origin` while it fetches, so a `remote.origin.vcs` in the session user's
    runner-writable global config would choose its transport. `origin` holds `config.clone_url`
    verbatim: no token in the URL, so nothing lands in `.git/config` for an EBS snapshot or a `git remote -v`
    to leak. A private repository authenticates only in the mirror's refresh, through the node's git
    credential helper `jaah-git-credential` (installed by
    the node reprovision's `ensure_git_credential_helper`, pinned per git process by
    `git_env.DECLARED_OP`). There is deliberately no second token path here to keep in sync with it.

    IDEMPOTENT, and the two "already there" answers differ: a git repository at `clone` is returned
    untouched with no network, while a directory that is NOT a repository raises `ResetRefused` and is
    never cloned into nor removed, because both alternatives are destructive guesses. A clone this function
    started and did not finish (`_CLONE_INCOMPLETE` still set, or no `origin` ref fetched yet) is finished in
    place.
    `GIT_TERMINAL_PROMPT=0` because this runs unattended: a missing credential helper must fail with git's own auth
    error rather than block the pass on a credential prompt."""
    if not config.has_repo:
        raise core.ResetRefused(f"clone-unresolvable: {config.name} has no repository — its slots are folders")
    clone = config.source_clone
    fresh = not os.path.isdir(clone)
    if not fresh and core._git(clone, "rev-parse", "--git-dir")[0] == 0:
        # Done = the marker gone and `origin`'s refs fetched: a clone that died before its marker was even
        # written has neither.
        if (core._git(clone, "config", "--get", _CLONE_INCOMPLETE)[1].strip() != "true"
                and core._git(clone, "for-each-ref", "--count=1", "refs/remotes/origin/")[1].strip()):
            _guard_unguarded_clone(config, clone)
            return clone
    elif not fresh:
        raise core.ResetRefused(
            f"clone-unusable: {clone} exists but is not a git repository — refusing to clone into it or "
            f"replace it. Inspect the host: either it is the wrong directory (check the node's Clone root) "
            f"or a real checkout lost its .git"
        )
    if not config.clone_url:
        raise core.ResetRefused(
            f"clone-unresolvable: no clone_url for {config.name}, so there is no remote to clone from — "
            f"set the project's Repo URL on its card"
        )
    parent = os.path.dirname(clone.rstrip("/")) or "/"
    # As the session user, like every other write here: a `jaahd`-owned clone root is the same
    # ownership bug one directory up (see `_run_git`).
    err = session_user.ensure_dir_as_session_user(parent)
    if err:
        raise core.ResetRefused(f"clone-unresolvable: cannot create clone root {parent} for {config.name}: {err}")
    url = config.clone_url
    try:
        mirror = core._refresh_mirror(config)
    except git_mirror.MirrorError as exc:
        raise core.ResetRefused(f"clone-failed: the mirror of {url} could not be refreshed: {exc}") from None
    # Pin the integration base so the clone's checked-out branch is the one the pool works against, not the
    # remote's default.
    branch = config.base if config.base and git_mirror.branch_sha(mirror, config.base) else None
    if config.base and branch is None:
        log.warning("ensure_clone: %s has no branch %s on origin — checking out its default branch",
                    config.name, config.base)
    # Origin's default branch, when its HEAD names one that exists (an unborn HEAD names none).
    default = git_mirror.head_branch(mirror)
    default = default if default and git_mirror.branch_sha(mirror, default) else None
    branch = branch or default
    steps = [
        *([("init", "-q", clone)] if fresh else []),
        ("-C", clone, "config", _CLONE_INCOMPLETE, "true"),
        ("-C", clone, "config", "remote.origin.url", url),
        ("-C", clone, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
        ("-C", clone, *git_mirror.local_fetch_args(mirror, "+refs/heads/*:refs/remotes/origin/*",
                                                   "+refs/tags/*:refs/tags/*")),
        *([("-C", clone, "checkout", "-f", "-q", "-B", branch, "--track", f"origin/{branch}")] if branch else []),
        *([("-C", clone, "remote", "set-head", "origin", default)] if default else []),
    ]

    def run_step(step: tuple[str, ...]) -> None:
        rc, _out, err = core._run_git(*step, timeout=core._clone_timeout(), env_extra=git_mirror.LOCAL_FETCH_ENV)
        if rc != 0:
            detail = _git_cause(err)
            raise core.ResetRefused(
                f"clone-failed: git clone of {url} into {clone} failed (rc {rc})" + (f": {detail}" if detail else "")
            )

    for step in steps:
        run_step(step)
    # Guarded before it is marked done, so a clone whose guard failed is finished by the next call.
    _guard_unguarded_clone(config, clone)
    run_step(("-C", clone, "config", "--unset", _CLONE_INCOMPLETE))
    log.info("ensure_clone: cloned %s into %s", url, clone)
    return clone


def _git_cause(err: str | None) -> str:
    """The line of git's stderr that names the cause: its FIRST `fatal:` line, else its last non-empty one.
    The last line alone was the advice git appends ("and the repository exists."), never what went wrong."""
    lines = [line.strip() for line in (err or "").splitlines() if line.strip()]
    return next((line for line in lines if line.startswith("fatal:")), lines[-1] if lines else "")


def _worktree_add_refusal(config: "core.PoolConfig", wt: str, err: str) -> str:
    """The refusal for a failed `git worktree add`, naming a missing integration branch when that is the cause:
    'git worktree add failed' alone sent an operator hunting through the node for a project-settings fix."""
    if not core.base_resolves(config.source_clone, config.base):
        return core.missing_base_refusal(config.base)
    detail = _git_cause(err)
    return f"git worktree add failed for {wt}" + (f": {detail}" if detail else "")


def create_worktree(
    config: "core.PoolConfig", n: int | str, branch: str | None, node_id: str | None = None,
    band: dict[str, int] | None = None, auto_ports: "tuple[int, int] | None" = None,
) -> str:
    """Add worktree N off the base, seed .env from `.env.example`, then apply the CP's port band, stack
    identity and node identity. Returns the worktree path. Idempotent: an existing dir is returned
    untouched. Raises `ResetRefused` if `git worktree add` fails.

    `branch` falsy materialises DETACHED on `origin/<base>` (else local `base`) — a fresh dispatch; the
    runner creates its own branch. A truthy `branch` keeps `-B branch` (`create_mechanics`' `wt/N`).

    `band` None is a REFUSAL (`apply_port_band`) — a band is a CP decision the node may not invent.
    `auto_ports` None is the module default (20800-32767) — the tier-2 range is an operator knob with a
    working default. `ensure_clone` runs AFTER the existing-dir early return (no network for a built slot); a built
    slot's clone is only guarded when it carries no guard (`_guard_unguarded_clone`).

    A project with no repository (`config.has_repo` False) materialises a FOLDER instead (`create_folder`):
    none of the git, `.env`, band or identity steps below apply to it."""
    if not config.has_repo:
        return create_folder(config, n)
    wt = config.wt_path(n)
    base = config.base
    if os.path.isdir(wt):
        if os.path.isdir(config.source_clone):
            # A slot built before its clone was guarded (an upgraded node) is guarded before it is handed out.
            _guard_unguarded_clone(config, config.source_clone)
        return wt
    ensure_clone(config)
    # Not fatal (`worktree add` falls back to the local ref), but logged with git's stderr: "no credential"
    # and "no network" read differently. The base is created only off a refresh that SUCCEEDED.
    try:
        mirror = core._refresh_mirror(config)
    except git_mirror.MirrorError as exc:
        rc_fetch, err_fetch = 1, str(exc)
    else:
        rc_fetch, _out, err_fetch = core._fetch_from_mirror(config, config.source_clone, base, refresh=False)
        if rc_fetch != 0 and core._create_missing_base(config, mirror):
            # Pushed: fetch it. Local only: `worktree add` below lands on the local `base`.
            rc_fetch, _out, err_fetch = core._fetch_base_cp(config) if config.push_enabled else (0, "", "")
    if rc_fetch != 0:
        log.warning(
            "create_worktree: fetch of origin/%s in %s failed (rc %s) — materialising %s from the local "
            "ref instead: %s",
            base, config.source_clone, rc_fetch, wt, (err_fetch or "").strip() or "no stderr")
    mode = ("-B", branch) if branch else ("--detach",)
    if core._git(config.source_clone, "worktree", "add", "-q", *mode, wt, f"origin/{base}")[0] != 0:
        rc_add, _out, err_add = core._git(config.source_clone, "worktree", "add", "-q", *mode, wt, base)
        if rc_add != 0:
            raise core.ResetRefused(_worktree_add_refusal(config, wt, err_add))
    if config.repo_id is not None:
        # A NON-PRIMARY repository's worktree (`slot_repos`): no `.env`, port band, stack or node identity —
        # those are the primary's alone.
        return wt
    if not os.path.isfile(os.path.join(wt, ".env")) and os.path.isfile(os.path.join(wt, ".env.example")):
        _copy(os.path.join(wt, ".env.example"), os.path.join(wt, ".env"))
        log.info("seeded .env from .env.example")
    # BEFORE the applicators, so the very first band is written through the seam an owner-only `.env`
    # requires rather than through a mode this slot only briefly has (`harden_worktree_modes`).
    env_file.harden_worktree_modes(wt)
    # `band` is passed THROUGH, never collapsed with `or {}`: only `apply_port_band`, holding the declared
    # `port_vars`, can tell an absent band from a project that binds no ports.
    ports.apply_port_band(wt, band, port_vars=config.port_vars, auto_ports=auto_ports)
    ports.apply_stack_identity(wt, config.repo, n)   # placement, never the display name — see `stack_base`
    ports.apply_node_identity_env(wt, n, node_id)  # pool slot → this host's NODE_ID (main clone untouched)
    return wt


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
    task_id: object = None,
    node_id: str | None = None,
    band: dict[str, int] | None = None,
    # Tier-2 `(floor, ceiling)`; None = module default (see `create_worktree`).
    auto_ports: "tuple[int, int] | None" = None,
    # True for a fresh fleet dispatch: detached at base, no branch named (the runner makes its own).
    detach_base: bool = False,
    # `payload["env_vars"]` verbatim. Default None is load-bearing: never rebuilds (`apply_managed_env`),
    # only withdraws a marked slot's names.
    env_vars: "dict | None" = None,
) -> dict:
    """The git-heavy mechanical lifecycle of a lease on the slot the CP DICTATED, and ONLY that: claim
    slot `n`, materialise it if the host has not built it yet, reset its tracked tree to base, write the
    CP's port band and compose/node identity, and assemble the slot record. Takes no flock and reads or
    writes no ledger — it RETURNS the record and the daemon declares it to the CP.

    Nothing here chooses: `n`, `band` and `account` arrive decided. No `holder` — occupancy is
    `worktrees.state`, bound CP-side by `slot_state.lease()` before this runs.

    A FOLDER slot (`config.has_repo` False) is only `create_folder`: the existing folder is left exactly as
    it is, and no reset, `.env` overlay, port band or identity is applied — there is no tree to reset."""
    try:
        chosen = int(n)
    except (TypeError, ValueError):
        raise core.ResetRefused(f"claim-mechanics: {n!r} is not a slot number") from None
    if not config.has_repo:
        wt = create_folder(config, chosen)
        return folder_record(config, chosen, wt, task_id=task_id, account=account, session=True)

    branch: str | None = None
    own_branch: str | None = None
    if detach_base:
        # No placeholder: nothing to fall back to or collide on.
        # The conventional branch, DERIVED: `dispatch_compose/`'s charter is the one place the spelling
        # is written. Fed to `reset_to_base` as `own_branch`, the identity signal for its destructive gates
        # (this task's own crashed attempt → refuse/back up; unrelated debris → reset through).
        if task_id is not None and str(task_id).strip():
            own_branch = f"{config.branch_prefix}fleet/task-{task_id}"
    else:
        # `auto/` is deliberately UNPREFIXED: no task owns it, and retire refuses `{prefix}auto/*` by design.
        branch = f"auto/{config.name}-wt{chosen}"

    wt = config.wt_path(chosen)
    fresh = False
    if not os.path.isdir(wt):
        create_worktree(config, chosen, branch, node_id, band=band, auto_ports=auto_ports)
        fresh = True
    reset.reset_to_base(config, wt, branch, fresh=fresh, detach_base=detach_base, own_branch=own_branch)
    # After the reset: a checkout recreates files at the unit's umask. A convergence.
    env_file.harden_worktree_modes(wt)
    # The rebuild's one seam: after `harden_worktree_modes` (which owns the mode), before the applicators
    # (so they win). Not also in `create_worktree` (it would overlay twice).
    managed_env.apply_managed_env(wt, env_vars, n=chosen, port_vars=config.port_vars, band=band)
    # Every claim, tier-2 included — hence the range travels with it.
    ports.apply_port_band(wt, band, port_vars=config.port_vars, auto_ports=auto_ports)
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


# ── retire_mechanics — the in-process entry point ───────────────────────────────────────────────────────
def _fleet_branch_re(branch_prefix: str) -> "re.Pattern[str]":
    """The branch shape a retire may act on under this project's CP-declared `branch_prefix`:
    `{prefix}fleet/(issue|task)-N`.

    THE PREFIX IS ANCHORED AND ESCAPED, not made prefix-agnostic with a leading `.*`: `reset_to_base` with
    `detach_base` moves a real tree, and this refusal exists because a slot on anything OTHER than a
    resolved fleet branch holds contents nobody has accounted for. So a project declaring `sn/` still
    refuses a bare `fleet/task-1` — the fail-closed direction, where the operator uses release and nothing
    is moved."""
    return re.compile(rf"^{re.escape(branch_prefix)}fleet/(issue|task)-[0-9]+$")


def retire_mechanics(config: "core.PoolConfig", n: int | str) -> dict:
    """Retire a RESOLVED fleet slot: detach its tree to a fresh `origin/<base>`, leaving the
    `{branch_prefix}fleet/(issue|task)-N` branch ref intact so its commits stay recoverable. Raises
    `ResetRefused` unless the slot's ON-DISK branch is such a branch — retire only ever resolves a closed
    duplicate, never releases an ad-hoc branch. `reset_to_base`'s gates all fire (`fresh=False`).

    The prefix comes from `config.branch_prefix`, never from the branch name in hand (`_fleet_branch_re`).

    Returns a diagnostic record — informational only; the daemon frees the slot by project+number."""
    if not config.has_repo:
        raise core.ResetRefused(f"retire-mechanics: {config.name} #{n} is a folder slot — it has no branch to retire")
    wt = config.wt_path(n)
    if not os.path.isdir(wt):
        raise core.ResetRefused(f"retire-mechanics: no worktree #{n} for {config.name} at {wt}")
    branch = core.wt_branch(wt)
    if not _fleet_branch_re(config.branch_prefix).match(branch):
        raise core.ResetRefused(
            f"retire-mechanics: refusing to retire {config.name} #{n} — its branch is '{branch}', not a "
            f"{config.branch_prefix}fleet/(issue|task)-N slot; retire is only for a resolved fleet slot "
            f"(a closed duplicate). Use release for anything else."
        )
    reset.reset_to_base(config, wt, branch, fresh=False, detach_base=True)
    log.warning("retired %s #%s → %s (was %s, now detached at origin/%s; branch ref preserved)",
                config.name, n, wt, branch, config.base)
    return {"project": config.name, "number": str(n), "worktree": wt, "retired_from": branch}


# ── create_mechanics — the in-process entry point ───────────────────────────────────────────────────────
def create_mechanics(
    config: "core.PoolConfig",
    *,
    number: int | str | None = None,
    exclude: "list[str] | None" = None,
    node_id: str | None = None,
    roster: dict[int, dict] | None = None,
    band: dict[str, int] | None = None,
    bands: dict[int, dict[str, int]] | None = None,
    auto_ports: "tuple[int, int] | None" = None,
) -> dict:
    """`create_worktree` a slot the CP has ALREADY DECLARED but the host has not materialized yet.

    IT ITERATES THE ROSTER, NEVER ITS COMPLEMENT: the candidates are declared rows with no directory on
    disk, and a number not in the roster is refused rather than created — allocation is the one thing the
    node may not do. Pass `number` to materialize one specific declared row.

    `band` (a known slot) wins over `bands` (per number, for a slot picked below); a `None` band is
    REFUSED. `auto_ports` defaults (see `create_worktree`) but is passed so a convergence pass uses the
    operator's range. Returns the slot record (`session:False`, `merged:True` — an idle slot)."""
    if roster is None:
        # Raises on an unreadable roster rather than returning `{}`: "the API was down" and "the pool is
        # empty" arriving as one value is the over-dispatch path.
        roster = roster_from_db(config.name, config.project_id)
    excluded = set(str(e) for e in (exclude or []))
    if number is not None:
        try:
            candidates = [int(number)]
        except (TypeError, ValueError):
            raise core.ResetRefused(f"create-mechanics: {number!r} is not a slot number") from None
    else:
        # Every declared row but slot 0 (the clone), lowest first: the CP's roster is the whole index
        # space, the node holds no ceiling of its own.
        candidates = sorted(n for n in roster if n >= 1)
    chosen: int | None = None
    for i in candidates:
        if i not in roster:
            raise core.ResetRefused(
                f"no slot available: {config.name} #{i} is not a declared slot — create materializes a row "
                f"the CP dictated, it does not mint an id (D1). Assign the project to this node in the "
                f"console, or raise the assignment's static slots."
            )
        if str(i) in excluded:
            continue
        # Already materialized: keeps a create idempotent and stops it re-adopting an existing tree.
        if os.path.isdir(config.wt_path(i)):
            continue
        chosen = i
        break
    if chosen is None:
        raise core.ResetRefused(
            f"no slot to create for {config.name}: every declared slot is already materialized or "
            f"excluded — assign the project to this node in the console, or raise the assignment's "
            f"static slots."
        )
    # Resolve `bands` down to THIS slot's band now the pick has happened, rebinding `band` rather than
    # naming a second variable: a test reads the call site as text and requires it to thread `band=band`,
    # the mechanical trace of "the caller held the CP's decision".
    if band is None:
        band = (bands or {}).get(chosen)
    if not config.has_repo:
        wt = create_folder(config, chosen)
        log.info("created %s #%s → %s", config.name, chosen, wt)
        return folder_record(config, chosen, wt, task_id=None, account="", session=False)
    wt = create_worktree(config, chosen, f"wt/{chosen}", node_id, band=band, auto_ports=auto_ports)
    slot_ports = ports.wt_ports_json(wt, config.port_vars)
    urls = ports.wt_urls_json(slot_ports)
    stack = "down" if ports.has_stack(wt) else "none"
    t = env_helpers.now_iso()
    log.info("created %s #%s → %s", config.name, chosen, wt)
    return {
        "number": str(chosen),
        "path": wt,
        "project": config.name,
        "branch": core.wt_branch(wt),
        # No `holder`/`holder_since` — see `claim_mechanics`' record.
        "task_id": None,     # the release CLEARS the reference
        "ports": slot_ports,
        "urls": urls,
        "session": False,
        "merged": True,
        "stack": stack,
        "last_active": t,
        "idle_hours": 0,
        "last_used": t,
    }
