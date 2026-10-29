#!/usr/bin/env python3
"""The node's RUNNER-LAUNCH mechanics. It decides nothing: the control plane is the sole lease
authority and hands this node a fully-decided `lease_slot` intent
(pool-tick → `reconcile.execute._INTENT_EXECUTORS` → the intent ack).

What only this side can do: stage attachment bytes onto a filesystem the CP cannot see
(`materialize_attachments`, best-effort; `materialize_prerequisite_artifacts`, FAIL-LOUD); resolve the
CP's registry account path onto THIS host's `JAAH_HOME` (`local_account_dir`); write `_meta`,
`launch.sh` and workspace trust and open the tmux window (`launch_runner`); and ack with pid + start
time + session id + dispatch id/generation (a bare pid is reused after reboot).

DAEMON-SPACE: heavy siblings are imported LAZILY inside functions, so `import fleet_dispatch` pulls in
no third-party distribution; no stdlib-only module may import this."""
from __future__ import annotations

import contextlib
import json
import logging
import ntpath
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import account_auth
import acp_runtime  # the runtime switch's node half: legacy launch or `jaah-acp-host`
import agent_config_dirs  # the three-state credential vocabulary (token/none/unreadable)
import agent_providers  # the provider vocabulary: what an account with no named provider is
import bedrock_launch  # a Jaah-account launch: K2 → exports, settings, pre-flight
import bedrock_usage_tail
import agents  # the runner-adapter registry: which env var points a CLI at its account,
               # and which file that account's credential IS
from agents import claude as default_runtime  # the default provider's legacy-runtime facts, named in its adapter
import control_plane_api
import env_helpers
import environment
import fleet_launch
import git_env
import ce_caps  # the community edition's node-side caps (a no-op on a cloud node)
import cloud_modules
from jaah_env import env_get  # renamed env vars
import host_platform
# Imported for `CONSENT_HASH_ENV`, the one key `_delegated_account_env` spells out.
import plugin_repair as plugin_repair_mod
import project_memory_bundle
import runner_env
import runner_registry
import session_user
import session_vendor  # the node's own note of which vendor transcript a session has
import shared_storage
from acp.launch import NATIVE_REF_KEY  # launch.json's resume target (`session_meta`), read by `acp/host.py`

log = logging.getLogger("jaahd.fleet_dispatch")
# The Architectural Guard (the `compliance` feature's): None where this tree does not carry it.
guard_bundle = cloud_modules.load("guard_bundle")


# ── Paths, resolved from this file ─────────────────────────────────────────────────────────────────────
HUB_ROOT = Path(__file__).resolve().parents[2]


# Hook commands are pinned against `HUB_ROOT`, not a movable symlink: dpkg replaces this code IN PLACE
# under `/usr/lib/jaahd`, so the root keeps resolving for a runner that outlives its package.
HOOKS_DIR = HUB_ROOT / "apps" / "jaahd" / "hooks"

# ── The `_meta` launch artifacts' permissions — THE SINGLE HOME OF THESE THREE NUMBERS ─────────────────
# They remove `other` and group WRITE — not a boundary between runners (all are uid `jaah`),
# but they exclude `jaahd`, the operator account and every other uid from rewriting a task's prompt
# or the `launch.sh` the next dispatch execs. Unset, the runner's `UMask=0002` would give 0664/0775.
# Group READ stays: `session_sweep` (the daemon) uploads `<ref>.sys`/`.task`/`.settings.json`, and
# 0700/0600 would EACCES it. The setgid bit is carried over (a bare 0750 would drop it) so files land
# in group `jaah`, the daemon's read path.
META_DIR_MODE = 0o2750
META_FILE_MODE = 0o640
# `launch.sh` needs the execute bit; it carries no credential. Group write is what matters.
META_EXEC_MODE = 0o750

# ── The rendered report's staging root ────────────────────────────────────────────────────────────────
# Every runner renders its `artifact.upload` here, never in the worktree (it would show in `git status`).
# Keyed by TASK ID, not slot: a slot is reused across units, the lease guarantees one runner per task.
REPORT_TMP_ROOT = Path("/tmp/cockpit-task-reports")


def report_dir_for(task_id: object) -> str:
    """The temp directory this task's runner renders its report into, or "" when `task_id` is not digits.

    THE ONE DERIVATION of the primary path; `ensure_report_dir` creates it (else the slot's `_meta/report-task-<id>`
    fallback), and both launchers export (`JAAH_REPORT_DIR`) and record that, which `cred_broker` confines
    uploads to. ASCII digits only (`str.isdigit` admits Unicode digits)."""
    task = str(task_id).strip()
    if not re.fullmatch(r"[0-9]+", task):
        return ""
    if host_platform.is_windows():
        # No shared `/tmp` there: Python reads it as `<cwd drive>:\\tmp`, Git Bash as Git's own tmp, so the
        # runner and this daemon would name two dirs. The run dir both accounts may Modify.
        return ntpath.join(host_platform.paths().run_dir, REPORT_TMP_ROOT.name, f"task-{task}")
    return str(REPORT_TMP_ROOT / f"task-{task}")


def ensure_report_dir(task_id: object, meta: str) -> str:
    """The report dir a launch exports and records, CREATED as the session user — never "".

    Every task session has one: `report_dir_for(task_id)`, else (non-digit id, or that mkdir refused) the
    ONE fallback `<meta>/report-task-<id>` — the slot's `_meta`, which the launcher has already created (fatal if not)
    and which sits beside the worktree, so a report there is outside `git status` too. A refused fallback
    raises `RuntimeError` like every other `_meta` failure: a session with no report dir would have every
    `artifact.upload` refused, so the launch fails loudly instead."""
    primary = report_dir_for(task_id)
    err = session_user.ensure_dir_as_session_user(primary) if primary else (
        f"task id {task_id!r} is not a plain integer, so no staging dir was derived")
    if not err:
        return primary
    # Keyed by task like the primary: `_meta` outlives the task, so a shared name would hand the next task
    # its predecessor's files to upload as its own.
    fallback = os.path.join(meta, f"report-{os.path.basename(primary)}" if primary else "report")
    log.warning("report dir for task %s: %s (%s) — using the slot's %s", task_id, primary or "(none)", err,
                fallback)
    err = session_user.ensure_dir_as_session_user(fallback)
    if err:
        raise RuntimeError(f"could not create the report dir {fallback}: {err}")
    return fallback


# An account dir is `<JAAH_HOME>/auth/<name>`; matched as a path SEGMENT so only that shape is rebased.
_AUTH_MARKER = f"{os.sep}{os.path.join('.jaah', 'auth')}{os.sep}"


def rebased_account_dir(config_dir: object) -> str:
    """A registry `config_dir` with its `.jaah/auth/<tail>` suffix rebased onto THIS host's
    `JAAH_HOME`, or "" when the string carries no such suffix (or `JAAH_HOME` is unset).

    The single home for that translation, and pure (no filesystem). `local_account_dir` layers the
    credential precedence on top; `accounts.py::_account_delete` needs it without, since a delete must
    resolve even when the dir is already gone."""
    if not isinstance(config_dir, str) or not config_dir:
        return ""
    idx = config_dir.find(_AUTH_MARKER)
    if idx == -1:
        return ""
    try:
        return str(Path(env_helpers.jaah_home()) / "auth" / config_dir[idx + len(_AUTH_MARKER):])
    except RuntimeError:
        return ""


def local_account_dir(config_dir: object, *, require_credential: bool = False,
                      provider: object = agent_providers.DEFAULT_PROVIDER) -> str:
    """The local agent-CLI account/config dir for a CP-registered account dir, or "" when this host has
    none.

    `require_credential`: False (observation) resolves even a credential-less dir (console shows
    `no_credentials`); True (dispatch) answers NO for one. Candidates: the rebased and the literal path,
    ranked by LIVE TOKEN — readable, then unreadable-but-present (a valid `0600` token may EACCES under
    the uid split), then bare existence (observation only). The token is asked at
    `shared_storage.token_dir` (the token store, else the config dir); the RETURN is still the config dir. A
    config dir absent here is dropped. `provider` picks the adapter's `credential_state` (asking the
    default's question of another CLI's account answers "none" forever); empty = default, unknown fails
    closed for dispatch."""
    if not isinstance(config_dir, str) or not config_dir:
        return ""
    # A data default (the column's server_default), not a vendor fallback: the registry still RAISES
    # for an unknown provider.
    wanted_provider = str(provider or "") or agent_providers.DEFAULT_PROVIDER
    try:
        adapter = agents.get_adapter(wanted_provider)
    except agents.UnknownProvider as exc:
        _warn_once(f"fleet_dispatch: {exc} — no credential question can be asked for {config_dir}")
        if require_credential:
            return ""
        adapter = None
    candidates = []
    rebased = rebased_account_dir(config_dir)
    if rebased:
        candidates.append(rebased)
    candidates.append(config_dir)
    present = [c for c in candidates if _usable_dir(c)]
    if adapter is None:
        # Observation only (dispatch returned above): no credential question to ask, so the existence
        # fallback, with its own message.
        for candidate in present:
            _warn_once(
                f"fleet_dispatch: this node has no runner adapter for {wanted_provider}, so no credential "
                f"question could be asked about {config_dir} at all — resolving to {candidate} because it "
                f"EXISTS, for observation only. It is NOT dispatchable here.")
            return candidate
        return ""
    # Asked once per candidate, then ranked: on a Windows NodeLink each answer is a runner-account round trip.
    states: list[tuple[str, str, str | None]] = []
    for candidate in present:
        # The token question is asked at `token_dir` (differs from `candidate` only when the token is
        # in the store); the answer is still the config dir. The adapter's three-state answer must not be
        # collapsed: `unreadable` is not `none`.
        token_dir = shared_storage.token_dir(candidate)
        try:
            state = adapter.credential_state(config_dir=token_dir)
        except Exception as exc:  # noqa: BLE001 — STRUCTURAL, and the reason is the blast radius
            # One account's bad path must not blank the whole roster: `main._usable_account_rows` builds
            # the node's entire availability overlay over this function. Fail closed for this account.
            _warn_once(f"fleet_dispatch: the {wanted_provider} credential probe raised for "
                       f"{token_dir} ({type(exc).__name__}) — treating this ACCOUNT as having no "
                       f"credential, rather than dropping every account on this node")
            state = None
        states.append((candidate, token_dir, state))
    for wanted in (agent_config_dirs.CREDS_TOKEN, agent_config_dirs.CREDS_UNREADABLE):
        for candidate, token_dir, state in states:
            if state == wanted:
                if wanted == agent_config_dirs.CREDS_UNREADABLE:
                    _warn_once(
                        f"fleet_dispatch: this process (uid {getattr(os, "geteuid", lambda: "n/a")()}) may not read the "
                        f"{wanted_provider} credential under {token_dir} — taking it as the account's "
                        f"live credential anyway, since the runner runs as its owner. Deliberate: the "
                        f"alternative is to read a permission error as 'no token' and fall through to "
                        f"picking a directory by mere existence.")
                return candidate
    if require_credential:
        # The DISPATCH answer is "no": no candidate holds a credential this process could even see.
        return ""
    for candidate in present:
        _warn_once(
            f"fleet_dispatch: no candidate for {config_dir} presented a credential file — falling back to "
            f"{candidate} because it EXISTS. A login may never have completed there; a runner dispatched "
            f"onto it will start with no account and exit immediately.")
        return candidate
    return ""


# Called once per account per dispatch pass, and these states are steady: log each message once per
# process.
_WARNED_ONCE: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _WARNED_ONCE:
        return
    _WARNED_ONCE.add(message)
    print(message, file=sys.stderr)


def _usable_dir(path: str) -> bool:
    """`is_dir()` that answers False instead of raising when the path cannot be examined.

    `Path.is_dir()` lets EACCES propagate, and a candidate routinely names another user's home (e.g.
    under an unprivileged CI `gha` user). A directory this process may not look at is one it does not have.
    """
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def account_dir_usable(config_dir: object, *,
                       provider: object = agent_providers.DEFAULT_PROVIDER) -> bool:
    """The `require_on_disk` gate the CP cannot apply itself: a credential dir's login is a
    manual step on THIS host, so the DB row can exist before the directory does.

    Resolved through `local_account_dir` ("this host has this account", at its own path). `usable`, not
    `on_disk`: a credential-less dir or an in-flight login must answer NO. A token in the store counts
    (`shared_storage.token_dir`), mounted or not. `provider` must be the account's own, or a non-default
    account answers NO forever; the fail-closed rules live in `local_account_dir`."""
    return bool(local_account_dir(config_dir, require_credential=True, provider=provider))


def _regrant_hops_to_group(config_dir: str, relative: Path) -> str:
    """Re-assert `2770` on each directory between `config_dir` and the credential, as the home's owner: ""
    or a reason. A vendor CLI makes its own state dir under its own umask (one measured with no group write), so this
    daemon — another uid in the group — can no longer place the link there. The owner could
    chmod them itself, so delegating this widens nothing; the link is still made by descriptor."""
    directory = config_dir
    for part in relative.parent.parts:
        directory = os.path.join(directory, part)
        failure = session_user.ensure_dir_as_session_user(directory, mode=0o2770)
        if failure:
            return failure
    return ""


def _link_credential_by_descriptor(config_dir: str, relative: Path, target: str, link: Path, *,
                                   regranted: bool = False) -> tuple[bool, str]:
    """`ensure_shared_credential_link`'s walk off Windows: `(made, "")`, or `(False, reason)`. Denied once
    under the uid split, the hops are re-granted to the group and the walk runs again."""
    # Walked BY DESCRIPTOR, no-follow at every hop: the home is runner-writable (2770), so a runner could plant
    # a parent directory of the credential as a link and aim this daemon's write at another account's file.
    try:
        dfd = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return False, f"{config_dir} is not a directory this node can open ({type(exc).__name__})"
    try:
        for part in relative.parent.parts:
            try:
                os.mkdir(part, 0o2770, dir_fd=dfd)
            except FileExistsError:
                pass
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        name = relative.name
        try:
            info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode) and os.readlink(name, dir_fd=dfd) == target:
            made = False
        elif info is not None and not (stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return False, f"{link} is neither a file nor a link — refusing to replace it"
        else:
            if info is not None and stat.S_ISREG(info.st_mode):
                log.warning("fleet_dispatch: %s held a node-local credential — replacing it with a link to "
                            "%s (the token store's copy is the account's)",
                            link, target)
            staged = f".{name}.jaah-link.{os.getpid()}.{secrets.token_hex(4)}"
            os.symlink(target, staged, dir_fd=dfd)
            os.replace(staged, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            made = True
    except OSError as exc:
        reason = f"could not link {link} to {target} ({type(exc).__name__}: {exc})"
        if regranted or not isinstance(exc, PermissionError) or not session_user.switch_needed():
            return False, reason
    else:
        return made, ""
    finally:
        os.close(dfd)
    failure = _regrant_hops_to_group(config_dir, relative)
    if failure:
        return False, f"{reason}; re-granting its directories to the group failed: {failure}"
    return _link_credential_by_descriptor(config_dir, relative, target, link, regranted=True)


def _link_credential_by_path(config_dir: str, relative: Path, target: str, link: Path) -> tuple[bool, str]:
    """`ensure_shared_credential_link`'s walk on Windows: `(made, "")`, or `(False, reason)`.

    The POSIX walk is by descriptor, and Windows has none of it (`os.open` takes no `dir_fd` there, and there
    is no `O_DIRECTORY`/`O_NOFOLLOW`). So this daemon only `lstat`s each hop by path, refusing a reparse point
    (a symlink or junction a runner planted) or a non-directory, and the runner account — which owns the home
    and holds the symlink right — creates a missing hop and makes the link (`session_user`). A path walk, so a
    hop swapped between its check and the link's creation is not excluded as it is by descriptor."""
    directory = config_dir
    for hop in ("", *relative.parent.parts):
        if hop:
            directory = os.path.join(directory, hop)
            if not os.path.lexists(directory):
                failure = session_user.ensure_dir_as_session_user(directory)
                if failure:
                    return False, f"could not create {directory}: {failure}"
        try:
            info = os.lstat(directory)
        except OSError as exc:
            return False, f"{directory} is not a directory this node can open ({type(exc).__name__})"
        if shared_storage.is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            return False, f"{directory} is a link or not a directory — refusing to link through it"
    path = os.path.join(directory, relative.name)
    try:
        leaf: os.stat_result | None = os.lstat(path)
    except FileNotFoundError:
        leaf = None
    if leaf is not None and stat.S_ISLNK(leaf.st_mode):
        current = shared_storage.link_target_of(path)
        if current is not None and ntpath.normcase(current) == ntpath.normcase(target):
            return False, ""
    elif leaf is not None and stat.S_ISREG(leaf.st_mode) and not shared_storage.is_reparse_point(leaf):
        log.warning("fleet_dispatch: %s held a node-local credential — replacing it with a link to %s (the "
                    "token store's copy is the account's)", link, target)
    elif leaf is not None:
        return False, f"{link} is neither a file nor a link — refusing to replace it"
    failure = session_user.symlink_as_session_user(target, path, directory=False, replace_file=True)
    return (False, failure) if failure else (True, "")


def ensure_shared_credential_link(config_dir: str, securestorage_dir: str, provider: object, *,
                                  layer: bool | None = None) -> str:
    """Make a `shared_token_link` account's per-node home use the credential IN THE TOKEN STORE: its
    `credential_file` becomes a symlink to `<store>/<credential_file>`. `""` on success (and for every
    account this does not apply to), else a one-line reason. Called with the store only when the token is
    there (`shared_storage.token_dir`), or right after a login published it.

    The home stays per-node (config, MCP registration, hooks, charter, transcripts), so nothing another
    mounting node writes to the store is ever executed here — only the credential is read through the link,
    and both CLIs write it back IN PLACE, so a refresh lands in the store too. The store side is only checked
    (`store_is_on_share`), never written: the login's publish (`shared_storage.publish_credential`) creates
    it. A regular file where the link belongs is replaced, not adopted: the login that just published is
    the newer credential, and keeping the local one is a second live token.

    The vendor's per-home agent layer (`finalize`) is laid when the link is (re)made (`layer=None`), always (`True`), or never (`False`, for a login whose own
    `finalize` follows). An already-linked launch skips it, so a linked home is not rewritten on every dispatch
    where an unlinked one never is; the launch re-asserts the control MCP server itself (`launch_runner`).
    Never raises."""
    row = agent_providers.by_value(str(provider or "") or agent_providers.DEFAULT_PROVIDER)
    if not securestorage_dir or row is None or not row.shared_token_link:
        return ""
    if not shared_storage.store_is_on_share(securestorage_dir, row.credential_file):
        return (f"{securestorage_dir} is not a real directory in the token store, so {config_dir}'s "
                f"credential cannot be linked to it")
    relative = Path(row.credential_file)
    target = str(Path(securestorage_dir) / relative)
    link = Path(config_dir) / relative
    walk = _link_credential_by_path if host_platform.is_windows() else _link_credential_by_descriptor
    made, reason = walk(config_dir, relative, target, link)
    if reason:
        return reason
    if layer is False or not (made or layer):
        return ""
    from agents import login as agent_login  # noqa: PLC0415 — the login registry, only for a linked account

    adapter = agent_login.login_adapter_for(row.value)
    try:
        if adapter is not None:
            adapter.finalize(config_dir=config_dir)
    except Exception as exc:  # noqa: BLE001 — an adapter defect must not strand the dispatch
        return f"the {row.label} agent layer refused {config_dir}: {type(exc).__name__}"
    return ""


# ── The per-node account layer, converged AT LAUNCH ────────────────────────────────────────────────────
# The package postinst's `converge_agent_plugins` is the standard (deploy-triggered) procedure for what
# an account must carry. It misses an account that appears BETWEEN deploys on a peer node: such a runner
# has no hooks (its questions never reach the CP) and no plugins (no control MCP tools, so it cannot
# settle or fail). Launch always happens on the node that runs the session, so it is the missing
# trigger — same entry point, same two phases. `provision_accounts.py`'s docstring is the single home
# for the layers. Phase 1 is local and sub-second, so the launch waits (bounded).
_ACCOUNT_LAYER_OFFLINE_TIMEOUT_S = 60.0
# Phase 2 clones a marketplace, so it is DETACHED and serves the next launch (a launch waiting minutes
# would be redispatched). Cooldown per account per daemon process, so a hookless account does not
# spawn a converge per dispatch.
_ACCOUNT_LAYER_ONLINE_COOLDOWN_S = 900.0
_account_layer_online_at: dict[str, float] = {}


def _delegated_account_env() -> dict:
    """The environment the account converge needs SPELLED OUT, rather than left to be inherited.

    One key: the managed-settings consent hash, the only thing keeping a peerless node's first runner off
    an approval prompt. The CP's copy off the pool tick (`plugin_repair.carried_consent_hash`) is preferred
    over the launch-time seed in `/etc/jaah/jaahd.env`, which only CP-launched nodes have and which a
    settings change leaves stale; a consenting peer on the node still outranks both (`consent_hash`). It would also arrive by tmux-server inheritance, but that env is frozen at
    server start and `run_as_session_user` says not to rely on it. Absent stays absent — no empty export
    (absence-is-not-a-state)."""
    seed = (plugin_repair_mod.carried_consent_hash()
            or (env_get(plugin_repair_mod.CONSENT_HASH_ENV) or "").strip())
    return {plugin_repair_mod.CONSENT_HASH_ENV: seed} if seed else {}


def _run_as_account_owner(argv: list[str], *, timeout: float, env: dict | None = None):
    """`argv` as the OWNER of the auth tree — user `jaah` — or directly when this process already is it.
    `env` is added to the delegated key.

    Never as the daemon: a root/`jaahd` write leaves files the runner user cannot repair. Without a
    switch (already the session user), a plain `subprocess.run` inherits this process's env."""
    delegated = {**_delegated_account_env(), **(env or {})}
    if session_user.switch_needed():
        return session_user.run_as_session_user(argv, timeout=timeout, env=delegated)
    # No switch: inherit this process's env PLUS the delegated key, which the tick may hold and env not.
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **delegated})


def converge_account_layer(account_dir: object, *, now: float = 0.0, run=None) -> dict:
    """Converge this node's managed layer for `account_dir`, immediately before a runner launches into it.

    Returns `{"phase1": <rc|None>, "plugins_missing": [...], "phase2": "spawned"|"cooldown"|""}` for the
    log line. NEVER RAISES — a degraded layer must not strand a dispatch. Phase 2 is gated on a plugin
    being ABSENT, not stale: keeping plugins current is the deploy's job, and a network update per
    dispatch would buy nothing."""
    result: dict = {"phase1": None, "plugins_missing": [], "phase2": ""}
    if not isinstance(account_dir, str) or not account_dir:
        return result
    cfg = Path(account_dir).expanduser()
    if not cfg.is_dir():
        return result
    import provision_accounts  # lazy, per this module's import-direction note at the top
    runner = run or _run_as_account_owner
    # Phase 1 — the managed settings block + the registry re-rooting, synchronously.
    try:
        done = runner(provision_accounts.converge_argv(str(cfg), offline=True),
                      timeout=_ACCOUNT_LAYER_OFFLINE_TIMEOUT_S)
        result["phase1"] = getattr(done, "returncode", None)
        if result["phase1"]:
            log.warning("fleet_dispatch: account layer phase 1 for %s exited %s: %s", cfg,
                        result["phase1"], (getattr(done, "stderr", "") or "").strip()[-400:])
        else:
            log.info("fleet_dispatch: account layer converged for %s: %s", cfg,
                     (getattr(done, "stderr", "") or "").strip()[-400:] or "no change")
    except Exception as exc:  # noqa: BLE001 — best-effort by contract, see the docstring
        log.warning("fleet_dispatch: could not converge %s's managed settings (%s: %s) — this runner may "
                    "launch with no hooks", cfg, type(exc).__name__, exc)
    # Phase 2 — only when something is MISSING outright, and detached. `needs_converge` is the single
    # home for that question; `absent_only` keeps a session's own `/model` write-back from triggering it.
    try:
        reason = provision_accounts.needs_converge(str(cfg), absent_only=True)
    except Exception as exc:  # noqa: BLE001 — same contract
        log.warning("fleet_dispatch: could not tell whether %s is provisioned (%s: %s)", cfg,
                    type(exc).__name__, exc)
        return result
    if not reason:
        return result
    result["plugins_missing"] = [reason]
    stamp = now or time.time()
    # Absence is not a state: `.get(..., 0.0)` would read "never converged" as "converged at the epoch".
    last = _account_layer_online_at.get(str(cfg))
    if last is not None and stamp - last < _ACCOUNT_LAYER_ONLINE_COOLDOWN_S:
        result["phase2"] = "cooldown"
        log.warning("fleet_dispatch: %s is still missing %s and a converge is already in flight — this "
                    "runner launches without it", cfg, reason)
        return result
    _account_layer_online_at[str(cfg)] = stamp
    # The detached form is the script's own, so this and the first-sight converge cannot drift.
    try:
        runner(provision_accounts.detached_command(str(cfg)),
               timeout=_ACCOUNT_LAYER_OFFLINE_TIMEOUT_S)
        result["phase2"] = "spawned"
        log.warning("fleet_dispatch: %s is not fully provisioned (%s) — a detached converge was "
                    "started; THIS runner launches without it, and holds no control MCP tools at all if "
                    "the missing part is the `jaah@jaah` plugin", cfg, reason)
    except Exception as exc:  # noqa: BLE001 — same contract
        _account_layer_online_at.pop(str(cfg), None)
        log.warning("fleet_dispatch: could not start a plugin converge for %s (%s: %s)", cfg,
                    type(exc).__name__, exc)
    return result


# The package postinst's `converge_accounts` converges the HOST's accounts on every upgrade; a vnode's live
# under its own `$JAAH_HOME`, which postinst never names. `jaah vnode converge-units` restarts each vnode's
# daemon onto the new code, so that daemon's first start on a version is the vnode's deploy trigger: the
# same full detached converge, once per version. The stamp lives in the daemon's own state dir.
_UPGRADE_CONVERGE_STAMP = "account-converge-version"


def converge_accounts_on_upgrade(version: str | None, *, stamp_dir: Path, run=None) -> str:
    """Start the full converge of every account on this node once per package `version`.

    Returns "spawned", "current" (already started for this version) or "" (no version, or the start
    failed — the stamp stays unwritten, so the next call retries). Never raises."""
    if not version:
        return ""
    stamp = stamp_dir / _UPGRADE_CONVERGE_STAMP
    try:
        if stamp.read_text().strip() == version:
            return "current"
    except OSError:
        pass  # no stamp yet: never converged on this node
    import provision_accounts  # lazy, per this module's import-direction note at the top
    # Spelled out, as `accounts.py`'s delegated publish does: a delegated script gets no inherited env
    # (`session_user.run_as_session_user`), and the converge resolves its accounts from these.
    env: dict = {}
    for name in (environment.POOL_ROOT_ENV, environment.SHARED_ACCOUNTS_ROOT_ENV):
        value = env_get(name)
        if value:
            env[name] = value
    try:
        done = (run or _run_as_account_owner)(provision_accounts.detached_command(None), env=env,
                                              timeout=_ACCOUNT_LAYER_OFFLINE_TIMEOUT_S)
        if getattr(done, "returncode", 0):
            raise RuntimeError(f"the start exited {done.returncode}: "
                               f"{(getattr(done, 'stderr', '') or '').strip()[-400:]}")
        stamp_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text(version + "\n")
    except Exception as exc:  # noqa: BLE001 — best-effort: a failed start retries on the next call
        log.warning("fleet_dispatch: could not start the account converge for package %s (%s: %s) — "
                    "accounts keep the previous version's plugin layer until it starts", version,
                    type(exc).__name__, exc)
        return ""
    log.info("fleet_dispatch: started the account converge for package %s", version)
    return "spawned"


# ── Attachment staging (the executor half of the composer split) ───────────────────────────────────────
#: The ONE root an attachment workdir lives under; a literal mirror of
#: `app/dispatch_compose/input_staging.py::_ATTACHMENTS_TMP_ROOT` (a separate deployable).
_ATTACHMENTS_ROOT = Path("/tmp/cockpit-task-attachments")


def materialize_attachments(attachments: list, attachments_dir: str, *, wipe: bool = True) -> None:
    """Write the composed verdict's attachment bytes into the directory the CP already named in the
    runner's prompt (`app/dispatch_compose._attachment_workdir` decides which and where).

    Each is fetched by its OWN `task_id` (the projection unions the parent epic's). Names are prefixed
    with the attachment id and reduced to a basename (no traversal) — `app/routes/tasks/answers.py::
    _attachment_paths_line` mirrors this rule to name the path in an answer. At launch (`wipe`) the dir
    is wiped first (no inherited files) and `0700`. On a warm answer delivery (`wipe=False`) it is only
    created if absent and a file already there is not fetched again, so the runner's live workspace keeps
    what it has and gains what the answer attached. BEST EFFORT: attachments are supplementary, so every
    failure is logged and the runner proceeds without them."""
    if not attachments or not attachments_dir:
        return
    import control_plane_api

    try:
        workdir = Path(attachments_dir)
        if not wipe and (workdir.parent != _ATTACHMENTS_ROOT or not re.fullmatch(r"task-[0-9]+", workdir.name)):
            raise RuntimeError(f"{attachments_dir!r} is not an attachment workdir ({_ATTACHMENTS_ROOT}/task-<id>)")
        reset = 'rm -rf -- "$SU_DIR" && ' if wipe else ""
        # The consumer is the runner (uid `jaah`) and the dir is `0700`, so under the uid split it must
        # be owned by the session user; wipe + recreate cross the seam as one step.
        if session_user.switch_needed():
            proc = session_user.run_as_session_user(
                ["/bin/sh", "-c", reset + 'mkdir -p -- "$SU_DIR" && chmod 0700 -- "$SU_DIR"'],
                env={"SU_DIR": str(workdir)}, timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"delegated workdir reset rc {proc.returncode}: "
                                   f"{(proc.stderr or '').strip()}")
        else:
            if wipe and workdir.exists():
                shutil.rmtree(workdir)
            workdir.mkdir(parents=True, exist_ok=True)
            workdir.chmod(0o700)
        for att in attachments:
            safe_name = Path(att.get("filename") or "").name or f"attachment-{att['id']}"
            target = workdir / f"{att['id']}_{safe_name}"
            # Already staged — judged by size, so a partial write from a failed fetch is fetched again.
            if not wipe and target.exists() and target.stat().st_size == att.get("size_bytes"):
                continue
            # Streamed, never buffered: up to 5 GB (`control_plane_api.MAX_STREAM_BYTES`) must not sit
            # resident in the root daemon; the write goes straight into the seam's spool descriptor.
            route = f"/api/agent/tasks/{att['task_id']}/attachments/{att['id']}/content"
            # Per file: one failing file must not keep the others (an answer's new files come last) off disk.
            try:
                err = session_user.stream_file_as_session_user(
                    str(target),
                    lambda handle, route=route: control_plane_api.agent_api_stream_bytes(route, handle),
                    mode=0o600)
            except Exception as exc:  # noqa: BLE001 — best-effort; see the docstring
                err = str(exc)
            if err:
                print(f"fleet_dispatch: could not stage attachments into {attachments_dir} (attachment "
                      f"{att['id']}: {err}) — the runner goes on without it", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — best-effort; see the docstring
        print(f"fleet_dispatch: could not stage attachments into {attachments_dir} ({exc}) — the runner "
              f"launches without them", file=sys.stderr)


# ── Prerequisite-artifact staging: the FAIL-LOUD half ─────────────────────────────────────────────────
#: The ONE root a prerequisite-input directory may live under; a literal mirror of
#: `app/dispatch_compose/input_staging.py::_TASK_INPUTS_TMP_ROOT` (a separate deployable), defended by the check below.
_TASK_INPUTS_ROOT = Path("/tmp/cockpit-task-inputs")


def materialize_prerequisite_artifacts(artifacts: list, inputs_dir: str) -> None:
    """Write a dependent stage's REQUIRED inputs — its prerequisites' settled `prd`/`architecture`
    documents — into the directory the CP already named on the runner's prompt. Raises on ANY failure.

    Unlike `materialize_attachments`, fail-loud: the prompt names each path as input, so a silent failure
    launches a runner reading nothing. Raising fails the intent (Task back to `ready`, attempt counted, no
    runner); the `lease_slot` executor calls this before claiming launch permission. A MISSING artifact is
    not a failure here — the CP renders it NOT AVAILABLE in the prompt; what raises is one that exists
    and could not be staged. Fetched by its own `task_id`; `filename` must be the CP's exact basename.

    The dir is WIPED first: after a Reset withdraws a kind, a stale file would still sit at the path.
    Accepted residual: `0700` keeps out other uids, not other runners (all uid `jaah`)."""
    if not inputs_dir:
        if artifacts:
            raise RuntimeError(
                "prerequisite artifacts to stage but no directory to stage them into — the composer names "
                "the path in the runner's prompt, so an empty one means the prompt and the disk disagree"
            )
        return
    import control_plane_api

    workdir = Path(inputs_dir)
    # Assert the shape before the `rm -rf`: this runs on every `architecture`/`plan` dispatch, as uid
    # `jaah` (owner of the pool and credentials), so a malformed payload must cost one attempt, never
    # a recursive delete. Also rejects whitespace and relative paths.
    if workdir.parent != _TASK_INPUTS_ROOT or not re.fullmatch(r"task-[0-9]+", workdir.name):
        raise RuntimeError(
            f"refusing to stage into (and wipe) {inputs_dir!r}: a prerequisite-input directory is "
            f"{_TASK_INPUTS_ROOT}/task-<id> and nothing else"
        )
    # Same uid-split seam as `materialize_attachments`; wipe + recreate as ONE delegated step so the dir
    # is never briefly under the wrong owner. `0700` and the assertion protect it (the sticky bit is on
    # `/tmp`, not our root). An EMPTY list still wipes — the dir is per task id, and "nothing to stage"
    # is exactly what a Reset produces; only the recreate is conditional.
    make = ' && mkdir -p -- "$SU_DIR" && chmod 0700 -- "$SU_DIR"' if artifacts else ""
    if session_user.switch_needed():
        proc = session_user.run_as_session_user(
            ["/bin/sh", "-c", f'rm -rf -- "$SU_DIR"{make}'],
            env={"SU_DIR": str(workdir)}, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"delegated workdir reset rc {proc.returncode} for {workdir}: "
                               f"{(proc.stderr or '').strip()}")
    else:
        # `is_symlink` first (`exists()` follows it): `rmtree` refuses a symlink, and a dangling one
        # would make `mkdir(exist_ok=True)` raise — wedging the task. Matches the delegated `rm -rf`.
        if workdir.is_symlink():
            workdir.unlink()
        elif workdir.exists():
            shutil.rmtree(workdir)
        if artifacts:
            workdir.mkdir(parents=True, exist_ok=True)
            workdir.chmod(0o700)
    if not artifacts:
        return
    for art in artifacts:
        kind = str(art.get("kind") or "?")
        # Vet the name before fetching. Only the CP's exact basename; a deviation RAISES: `.name` keeps
        # `..`, and this stager writes the basename bare (`workdir / ".."` is the staging root).
        # Sanitising would be wrong: the prompt already named the path, so writing elsewhere still
        # leaves the runner reading nothing.
        raw = str(art.get("filename") or "")
        safe_name = Path(raw).name
        if not raw or safe_name != raw or safe_name in (".", "..") or "\0" in raw:
            raise RuntimeError(
                f"refusing to stage the required {kind} input under {raw!r}: the composer writes a plain "
                f"kind-keyed basename, so anything else means the prompt names a path this write would "
                f"not have created"
            )
        # Streamed, as above. The empty check raises INSIDE `produce`, so nothing is created — a
        # zero-byte file at the prompt's path is what the check prevents.
        route = f"/api/agent/tasks/{art['task_id']}/attachments/{art['id']}/content"
        staged_bytes = 0

        def _stage(handle, route=route, kind=kind, art=art):
            nonlocal staged_bytes
            staged_bytes = control_plane_api.agent_api_stream_bytes(route, handle)
            if staged_bytes == 0:
                raise RuntimeError(
                    f"required {kind} input (attachment {art.get('id')} of task {art.get('task_id')}) "
                    f"fetched EMPTY — refusing to hand the runner a path to nothing"
                )

        err = session_user.stream_file_as_session_user(
            str(workdir / safe_name), _stage, mode=0o600)
        if err:
            raise RuntimeError(f"could not stage required {kind} input at {workdir / safe_name}: {err}")
        log.info("fleet_dispatch: staged required %s input (%d bytes) at %s",
                 kind, staged_bytes, workdir / safe_name)


def proc_started_at(pid: object) -> str:
    """The UTC moment `pid` started, read from `/proc` — "" when it cannot be read.

    A pid alone is not an identity (reused after reboot); boot time + `/proc/<pid>/stat` field 22 makes
    the pair unique. Delegated to `reconcile.context.proc_started_at`, the single home of the arithmetic AND the
    string format: both copies feed one equality check in `app/slot_state.py`. Lazy import."""
    try:
        from reconcile import context  # noqa: PLC0415 — deferred: see the docstring's import-direction note

        started = context.proc_started_at(pid)
    except Exception as exc:  # noqa: BLE001 — the ack is better with a bare pid than not sent at all
        print(f"fleet_dispatch: could not read the start time of pid {pid} ({exc})", file=sys.stderr)
        return ""
    if not started:
        # `reconcile.context.proc_started_at` answers "" rather than raising; logging is this module's contract.
        print(f"fleet_dispatch: could not read the start time of pid {pid}", file=sys.stderr)
    return started


def _slot_number(worktree: str) -> str:
    """The slot component of a worktree path — a bare `N`/`main` under the computed
    `<jaah_home>/repos/<project id>/{main,N}` layout, or the whole `<repo>-N` basename under the legacy
    sibling-suffix one. Both shapes are what `slot_window_name` takes as its `number`."""
    return os.path.basename(worktree.rstrip("/"))


def _worktree_ref(project: str, worktree: str) -> str:
    """The pool-slot identity for a worktree path — `project` for the main clone, else `<project>-N` —
    matching `worktree_mechanics.ports.apply_stack_identity`'s naming. Not the basename alone: under the
    computed layout that is a bare "N"/"main" that collides across projects. Delegated to
    `fleet_launch.slot_ref`, shared with the window-name composer."""
    return fleet_launch.slot_ref(project, _slot_number(worktree))


def api_key_account_dir(slug: str) -> str:
    """This node's config dir for API-key account `slug`, created as the session user at the fleet's account
    mode (`accounts.ACCOUNT_DIR_MODE`), or `""` when it cannot be derived or created — the runner then
    launches as before, in the session user's own CLI home, and the log says why. The path is
    `account_auth.api_key_config_dir`'s; the roster sweeps converge the same dir (`env_helpers.roster_config_dir`)."""
    config_dir = account_auth.api_key_config_dir(environment.pool_root_or_empty(), slug)
    if not config_dir:
        log.warning("launch_runner: no config dir can be derived for API-key account %r — this runner "
                    "launches in the session user's %s, without the managed plugins", slug,
                    default_runtime.HOME_DIR_SHOWN)
        return ""
    import accounts  # noqa: PLC0415 — lazy: `accounts` → `commands` → the daemon core
    err = session_user.ensure_dir_as_session_user(config_dir, mode=accounts.ACCOUNT_DIR_MODE)
    if err:
        log.warning("launch_runner: could not create %s for API-key account %r (%s) — this runner "
                    "launches in the session user's %s, without the managed plugins",
                    config_dir, slug, err, default_runtime.HOME_DIR_SHOWN)
        return ""
    return config_dir


def _record_runner_binding(task_id: object, runner_session_id: str, worktree: str, project: str,
                           account_dir: Optional[str] = None) -> None:
    """Persist the Task→runner binding (session id + worktree ref, e.g. `acme-2`, + the credential account
    dir). Bookkeeping: swallows every failure (logged) so a dispatch never dies on a brief CP outage.
    `account_dir` is passed through untranslated — the CP derives the slug."""
    try:
        import control_plane_api
        control_plane_api.set_runner_binding(
            task_id, session_id=runner_session_id, worktree_ref=_worktree_ref(project, worktree),
            account_dir=account_dir,
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails a launch
        print(f"fleet_dispatch: could not record the runner binding for task {task_id} ({exc}) — the run "
              f"proceeds but its Task row will not name its worktree/session", file=sys.stderr)


def api_key_helper_command(provider: str, api_key_account: str) -> str:
    """The `apiKeyHelper` command a helper-keyed runner on an API-key account takes its key from, or `""`.

    A key in the ENVIRONMENT meets the CLI's first-run "Detected a custom API key … use it?" prompt,
    defaulting to "No" — an unattended runner parks on it forever. The helper fetches the same credential
    per request, so the key never enters the pane's environment. `""` for OAuth accounts and for adapters
    declaring a key variable (`api_key_env`), which take the export instead. The one home of this rule."""
    if not api_key_account or agents.get_adapter(provider).api_key_env():
        return ""
    cred_name = f"{account_auth.NODE_CREDENTIAL_PREFIX}{api_key_account}"
    if host_platform.is_windows():
        # The default CLI on Windows runs `apiKeyHelper` through cmd.exe, which neither reads POSIX quoting nor
        # runs a `#!` script: the bundle's interpreter is named and each word double-quoted ("'C:/Program\'
        # is not recognized" on a real PC). Only a slug the node grammar admits reaches here.
        if not account_auth.slug_from_node_credential_name(cred_name):
            return ""
        from host_platform import windows  # noqa: PLC0415 — the bundle's paths
        return " ".join(f'"{word}"' for word in (windows.paths().python, account_auth.CREDENTIAL_HELPER, cred_name))
    return f"{fleet_launch.shq(account_auth.CREDENTIAL_HELPER)} {fleet_launch.shq(cred_name)}"


def with_api_key_settings(
    content: Optional[str], settings_flag: str, *, provider: str, key_helper: str, settings_path: str,
) -> tuple[Optional[str], str]:
    """`(settings JSON, --settings flag)` with the adapter's `api_key_settings(key_helper)` merged in, or both
    unchanged when there is no helper. The one merge the runner, the env runner and an API-key console tab
    share: the key rides `apiKeyHelper` in the SAME settings file as the hooks (`api_key_helper_command`)."""
    if not key_helper:
        return content, settings_flag
    rendered = json.loads(content) if content is not None else {}
    rendered.update(agents.get_adapter(provider).api_key_settings(key_helper))
    return (json.dumps(rendered, indent=2),
            settings_flag or f"--settings {fleet_launch.shq(settings_path)}")


def with_additional_dirs(
    content: Optional[str], settings_flag: str, *, provider: str, dirs: "tuple[str, ...]", settings_path: str,
) -> tuple[Optional[str], str]:
    """`with_api_key_settings`' twin for a multi-repo slot: the adapter's `additional_dirs_settings(dirs)` (the
    non-primary worktrees, `slot_repos.extra_dirs`) merged into the same settings file, or both unchanged
    when there are no dirs or the CLI has no such setting."""
    extra = agents.get_adapter(provider).additional_dirs_settings(tuple(dirs)) if dirs else {}
    if not extra:
        return content, settings_flag
    rendered = json.loads(content) if content is not None else {}
    for key, value in extra.items():
        rendered[key] = {**rendered.get(key, {}), **value} if isinstance(value, dict) else value
    return (json.dumps(rendered, indent=2),
            settings_flag or f"--settings {fleet_launch.shq(settings_path)}")


def api_key_preflight_lines(key_helper: str) -> list[str]:
    """`launch.sh` lines that run the `apiKeyHelper` command once BEFORE the CLI starts and stop the pane,
    loudly, if it serves nothing — otherwise a declined name surfaces only as an `auth_expired` break on
    the first turn. The key goes to /dev/null. `[]` when there is no helper; `key_helper` is pre-quoted."""
    if not key_helper:
        return []
    return [
        f"if ! {key_helper} >/dev/null; then",
        "  printf 'launch.sh: the credential broker served no API key - refusing to launch a runner that "
        "would start unauthenticated\\n' >&2",
        "  exit 1",
        "fi",
    ]


def with_bedrock_settings(
    content: Optional[str], settings_flag: str, *, bedrock: "bedrock_launch.BedrockPlan | None",
    settings_path: str,
) -> tuple[Optional[str], str]:
    """`with_api_key_settings`' Jaah-account twin: `bedrock_launch.settings` (`awsCredentialExport` naming
    this session's broker credential) merged into the same per-dispatch settings file as the hooks, or both
    unchanged when there is no plan. Shared by the runner and a Jaah console tab."""
    if bedrock is None:
        return content, settings_flag
    rendered = json.loads(content) if content is not None else {}
    rendered.update(bedrock_launch.settings(bedrock, fleet_launch.shq))
    return (json.dumps(rendered, indent=2),
            settings_flag or f"--settings {fleet_launch.shq(settings_path)}")


def build_auth_lines(
    task_id: object, account_dir: Optional[str], *, runner_session_id: str = "",
    worktree: str = "", report_dir: str = "", securestorage_dir: str = "",
    api_key_account: str = "", provider: str = agent_providers.DEFAULT_PROVIDER,
    git_author: "tuple[str, str] | None" = None,
    bedrock: "bedrock_launch.BedrockPlan | None" = None,
    git_ceiling: str = "",
    memory_env: "dict[str, str] | None" = None,
    advertise_nodelinks: bool = False,
    guard_env: "dict[str, str] | None" = None,
) -> list[str]:
    """The `export` lines every dispatched runner's launch.sh sources:
    OMC_SKIP_HOOKS/OMC_PYTHON_LSP + COCKPIT_AGENT_BASE/CONTROL_TASK_ID (names `compose_env_guard` never
    touches, so they survive the worktree `.env` force-inject), JAAH_RUN_STARTED_AT, plus
    CONTROL_RUNNER_SESSION_ID, JAAH_WORKTREE, JAAH_REPORT_DIR, the adapter's account env, the
    securestorage var when the token is in the store, JAAH_SLOT_PORTS (`_slot_ports_export`), and — only with
    `advertise_nodelinks` — JAAH_NODELINKS.

    `runner_session_id` is the exact value `_record_runner_binding` stamps, so a settle/break report
    names the session the CP's `end_attempt` fence checks — never re-derived from a transcript name.
    `api_key_account` is the OTHER kind of account (`apps/api/app/account_auth.py`): its SLUG, never
    its key; no securestorage export, and the key fetched into the adapter's own variable
    (`api_key_env`) — nothing when the adapter takes it from a helper instead (`api_key_helper_command`).
    Empty defaults keep other callers' lines byte-identical. `COCKPIT_AGENT_BASE` must come from
    `resolve_control_plane_api_base()`, never an inlined loopback that points at the wrong CP."""
    shq = fleet_launch.shq
    # NO CREDENTIAL IS EXPORTED HERE. Do not re-add one to fix a runner that cannot reach the
    # CP: the fix is a verb in `agent_vocabulary`, or a running daemon. COCKPIT_AGENT_BASE is not a
    # credential; the monorepo's `control_plane_client` needs it where no daemon is reachable.
    lines = [
        "export OMC_SKIP_HOOKS=keyword-detector",
        # OMC's Python LSP: takes `basedpyright-langserver` only on this exact value (else `ty`, which
        # has no npm package, is not installed). The package postinst's runner-CLI floor installs it — one
        # decision. A foreign, unpinned variable: if OMC renames it, Python LSP just stops starting (LSP
        # is never mandatory), so deliberately unguarded.
        "export OMC_PYTHON_LSP=basedpyright",
        # Prompt suggestions off for every runner (why, and why never for a console tab: at the constant).
        default_runtime.PROMPT_SUGGESTION_OFF_EXPORT,
        f"export COCKPIT_AGENT_BASE={shq(control_plane_api.resolve_control_plane_api_base())}",
    ]
    lines.append(f"export CONTROL_TASK_ID={shq(str(task_id))}")
    # Where this node installed the standard, so the Stop-hook gate and the pre-commit hook find the gate
    # runner (`run_gate.py`) off Linux and the control MCP server finds the daemon tree (`runner_env.LIB_ROOT_ENV`).
    # Omitted at the package path, every reader's default, so a packaged node's launch.sh stays byte-identical.
    if runner_env.lib_root_relocated():
        lines.append(f"export {runner_env.LIB_ROOT_ENV}={shq(host_platform.paths().lib_root)}")
    # The node-managed engine, where the CLI's default socket is not it (`runner_env.docker_env`).
    for name, value in runner_env.docker_env().items():
        lines.append(f"export {name}={shq(value)}")
    # The epoch second this run launched, evaluated when launch.sh runs: a CLI with no trace tool of its own
    # measures its run duration from it for the charter's run telemetry. A fixed literal; nothing interpolated.
    lines.append('export JAAH_RUN_STARTED_AT="$(date -u +%s)"')
    # Git's credential comes from the broker only and it commits as the launcher (`git_env`);
    # unconditional, so a stray node `credential.helper` cannot push as its owner. `git_author` is
    # omitted when the CP named nobody — git refuses an empty ident.
    # This runner-session push/fetch is NOT moved to the daemon's bare mirror (D-P1): that needs a new daemon
    # push verb with the access guard's semantics (follow-up F1), so the helper's config-scope refusal
    # (`git_credential_helper.check_config_scopes`) is its interim closure. Residuals still reaching it are code
    # the runner runs anyway (hooks, `core.hooksPath`, `core.fsmonitor`, `alias.*`, filter/diff drivers,
    # `core.sshCommand`) and a config-read TOCTOU window — git reads its config before its first request and the
    # helper only on the 401 after it, so a writer of that config can plant and remove a key in between (and an
    # unauthenticated first request still follows a plant); the cross-starter boundary is a
    # per-session MicroVM.
    for name, value in {**git_env.BROKER_ONLY, **git_env.author_env(git_author)}.items():
        lines.append(f"export {name}={shq(value)}")
    if runner_session_id:
        lines.append(f"export CONTROL_RUNNER_SESSION_ID={shq(runner_session_id)}")
    # The kill-safety hooks key on the LEASED WORKTREE ROOT, not a drifting `$PWD`
    # (`break_cause.worktree_root()`). Omitted, never empty: an empty export would pin lookups to `""`.
    if worktree:
        lines.append(f"export JAAH_WORKTREE={shq(worktree)}")
    # A FOLDER slot's git ceiling (`folder_git_env`): without it git discovery climbs out of the empty folder
    # and finds an ancestor repository (a home-directory dotfiles `.git`), so `git status`, the settle guard
    # and every hook would read the folder as that repo. Omitted for a worktree, so its lines are unchanged.
    for name, value in folder_git_env(git_ceiling).items():
        lines.append(f"export {name}={shq(value)}")
    # The account env var is the ADAPTER's answer; its contract omits an ACCOUNT-scoped var when no
    # account was leased (a defined-but-empty value reads as authoritative). An adapter may also declare
    # a RUN-scoped var that must survive the api-key branch — `agents/base.py`'s `env()` owns that.
    for name, value in agents.get_adapter(provider).env(
            config_dir=account_dir or "", worktree=worktree).items():
        lines.append(f"export {name}={shq(value)}")
    # A token in the store is not under its config dir: the securestorage var moves the credential store
    # independently (and keys its service name), so the token sits in the store while transcripts and
    # plugins stay per-node. The node resolves it (`shared_storage.securestorage_dir`).
    # OMITTED, NEVER EMPTY — stricter than JAAH_WORKTREE: in the shipped CLI a defined-but-empty value
    # resolves to the CLI's own home, a different (possibly someone else's) credential store
    # (`default_runtime.HOME_DIR_NAME`).
    if securestorage_dir:
        lines.append(f"export {shared_storage.SECURESTORAGE_ENV}={shq(securestorage_dir)}")
    # ── THE JAAH ACCOUNT: THE DEFAULT CLI ON THE PLATFORM BEDROCK ACCOUNT (K2) ──────────────────────────
    # Per runner, in this file, never on the tmux server. Names and models only: the credentials arrive per
    # request through `awsCredentialExport` (`with_bedrock_settings`), never as a variable (fence C2).
    if bedrock is not None:
        lines.extend(bedrock_launch.env_lines(bedrock, shq))
    # ── AN API-KEY ACCOUNT: THE KEY, FETCHED AT RUN TIME, NEVER WRITTEN DOWN ────────────────────────────
    # Only for an API-key account (`securestorage_dir` empty by construction; `account_dir`, when set, is
    # its token-less home from `api_key_account_dir`).
    # A command substitution, not the literal: `launch.sh` is on disk and readable by every same-uid
    # runner, whatever `META_EXEC_MODE` says — what lands is the NAME `account_api_key:<slug>`,
    # useless without the broker socket (`cred_broker` owns that ACL). The environment, not argv: argv is
    # world-readable via `/proc/<pid>/cmdline`.
    # ACCEPTED RESIDUAL, at its true scope: `/proc/<pid>/environ` is owner-only, but EVERY runner on this
    # node is uid `jaah`, so every other concurrent runner can read this key — no escalation needed.
    # Closing it is a fleet-topology change (one uid per runner), and the
    # licensing-risk analysis must be re-read against that scope.
    # The slug is `shq`'d: an operator-typed column inside a command substitution would otherwise execute.
    # The variable is the ADAPTER's (`api_key_env`); `""` means its key rides a helper, so nothing is exported.
    key_env = agents.get_adapter(provider).api_key_env() if api_key_account else ""
    if key_env:
        cred_name = f"{account_auth.NODE_CREDENTIAL_PREFIX}{api_key_account}"
        lines.append(
            f'{key_env}="$({shq(account_auth.CREDENTIAL_HELPER)} {shq(cred_name)})"'
        )
        lines.append(f"export {key_env}")
        # FAIL CLOSED, LOUDLY, HERE: `jaah-credential` prints nothing and exits non-zero on failure, so
        # the assignment lands EMPTY (no `set -e` catches it), and the CLI would die with an auth error far
        # from the cause. `exit 1` before the `exec` makes this the pane log's last line. The format
        # string is a fixed literal; only the `shq`'d name is interpolated.
        lines.append(f'if [ -z "${key_env}" ]; then')
        lines.append(
            "  printf 'launch.sh: the credential broker served no API key for %s - refusing to launch "
            f"a runner that would start unauthenticated\\n' {shq(cred_name)} >&2"
        )
        lines.append("  exit 1")
        lines.append("fi")
    # Where the runner renders its report — in the env, since the prompt is composed before this node
    # picks anything. Omitted, never empty.
    if report_dir:
        lines.append(f"export JAAH_REPORT_DIR={shq(report_dir)}")
    # ── THIS SLOT'S HOST PORTS, UNDER ONE NAME THE `.env` FORCE-INJECT CANNOT REACH ─────────────────────
    # Not in the prompt: `_start_here_block` composes before any slot is leased, so the prompt names this
    # VARIABLE and its value is supplied here — sparing the runner from deriving ports from the compose
    # file and the pool port grid. `_slot_ports_export` owns the shape.
    if worktree:
        lines.extend(_slot_ports_export(worktree, shq))
    # ── THE NODELINKS MOUNTED HERE, AT LAUNCH (`shared_storage.nodelinks_env_value`) ─────────────────────
    # Only what `ismount` confirms now; omitted, never empty, when there are none.
    nodelinks = shared_storage.nodelinks_env_value() if advertise_nodelinks else ""
    if nodelinks:
        lines.append(f"export {shared_storage.NODELINKS_ENV}={shq(nodelinks)}")
    # ── PROJECT MEMORY, ONLY FOR AN ON TASK (`project_memory_bundle.env_for`) ───────────────────────────
    # Last, and nothing at all when OFF, so an OFF launch.sh is byte-identical to one from before memory.
    for name, value in (memory_env or {}).items():
        lines.append(f"export {name}={shq(value)}")
    # ── THE ARCHITECTURAL GUARD, ONLY WITH ITS STANZA (`guard_bundle.env_for`) ────────────────────────────
    # After memory, and nothing at all without the stanza, so an unguarded launch.sh is byte-identical.
    for name, value in (guard_env or {}).items():
        lines.append(f"export {name}={shq(value)}")
    if guard_env and guard_bundle.redirects_hooks(guard_env):
        lines.append(guard_bundle.HOOKS_REDIRECT_SH)
    return lines


def folder_git_ceiling(worktree: str, has_repo: bool) -> str:
    """The `GIT_CEILING_DIRECTORIES` value a FOLDER slot's runner gets — the folder's parent,
    `<jaah_home>/repos/<pid>` — or `""` for a git worktree, which must keep finding its own repository."""
    if has_repo or not worktree:
        return ""
    return os.path.dirname(worktree.rstrip("/\\")) or ""


def folder_git_env(git_ceiling: str) -> dict[str, str]:
    """The env a folder slot's runner AND its tmux window carry (`{}` for a worktree): the one home of the
    variable's name, so the two cannot drift."""
    return {"GIT_CEILING_DIRECTORIES": git_ceiling} if git_ceiling else {}


def _slot_ports_export(worktree: str, shq: Callable[[str], str]) -> list[str]:
    """`[export JAAH_SLOT_PORTS=...]` for a worktree whose `.env` declares any, `[]` otherwise.

    One new name, deliberately NOT the port vars themselves: `FRONTEND_PORT`/`API_PORT` are exactly what
    `compose_env_guard` force-reads from `.env`, so exporting them would fight the guard. The pairs are
    DATA (`NAME=port`, comma-separated, `.env` order, keys verbatim — `wt_urls_json`'s role mapping is
    lossy). All reads go through `worktree_mechanics` (`env_port_var_names`, `wt_ports_json`), the one
    reader of a pool `.env`. From a secrets file only keys passing the `PORT`-token test with all-digit
    values survive, `shq`'d. Best effort, import included: never fails a committed dispatch."""
    try:
        from worktree_mechanics import ports as wm_ports  # noqa: PLC0415 — deferred: see the import-surface note above

        ports = wm_ports.wt_ports_json(
            worktree, wm_ports.env_port_var_names(worktree),
        )
    except Exception as exc:  # noqa: BLE001 — a port we could not read never fails a committed dispatch
        log.warning("build_auth_lines: could not read %s's ports (%s) — this runner is launched without "
                    "JAAH_SLOT_PORTS and has to read the compose file for them", worktree, exc)
        return []
    if not ports:
        return []
    value = ",".join(f"{name}={port}" for name, port in ports.items())
    return [f"export JAAH_SLOT_PORTS={shq(value)}"]


# ── THE LAUNCH SEAM FOR A PROCESS WITH NO MODEL OF OURS (an SDK agent under `jaah-acp-host`, below) ─────
#: The interpreter an agent bundle is unpacked under: this daemon's own, installed and not writable by the
#: session user.
PYTHON = sys.executable or host_platform.paths().python


def _launch_no_model(
    *,
    project: str,
    repo_path: str,
    worktree: str,
    task_id: object,
    ref_key: str,
    title: str,
    git_author: "tuple[str, str] | None",
    has_repo: bool,
    provider: str,
    exec_lines: "Callable[[str], tuple[str, ...]]",
    what: str,
    api_key_account: str = "",
    runtime: str = "",
    dispatch_id: str = "",
    generation: "int | None" = None,
) -> dict:
    """The pane a process with no model gets — an SDK agent under `jaah-acp-host`: `launch.sh`
    with the runner env and `exec_lines(runner_session_id)`, the task binding, a tmux window with its pane
    log, the `runner_registry` record under `provider` and `runtime` (leased `api_key_account`, a registry agent's
    bound key account, when set), and the `ready → active` claim. Returns the ack facts."""
    shq = fleet_launch.shq

    def _write(path: str, content: str, mode: int = META_FILE_MODE) -> None:
        err = session_user.write_file_as_session_user(path, content, mode=mode)
        if err:
            raise RuntimeError(f"launch_no_model: could not write {path}: {err}")

    meta = os.path.join(os.path.dirname(worktree), "_meta")
    err = session_user.ensure_dir_as_session_user(meta, mode=META_DIR_MODE)
    if err:
        raise RuntimeError(f"launch_no_model: could not create {meta}: {err}")
    pane_dir = env_helpers.log_dir("runner")
    err = session_user.ensure_dir_as_session_user(str(pane_dir))
    if err:
        raise RuntimeError(f"launch_no_model: could not create {pane_dir}: {err}")
    runner_session_id = str(uuid.uuid4())
    report_dir = ensure_report_dir(task_id, meta)
    git_ceiling = folder_git_ceiling(worktree, has_repo)
    auth_line = "\n".join(build_auth_lines(
        task_id, None, runner_session_id=runner_session_id, worktree=worktree, report_dir=report_dir,
        git_author=git_author, git_ceiling=git_ceiling, advertise_nodelinks=True,
    ))
    lines = tuple(exec_lines(runner_session_id))
    _record_runner_binding(task_id, runner_session_id, worktree, project, None)
    launch_path = os.path.join(meta, f"{ref_key}.launch.sh")
    _write(launch_path, fleet_launch.build_launch_sh(
        auth_line=auth_line, hub_root=str(HUB_ROOT), worktree=worktree, settings_flag="", sys_file="",
        task_file="", exec_lines=lines,
    ), mode=META_EXEC_MODE)
    win = fleet_launch.slot_window_name(
        project, _slot_number(worktree), fleet_launch.task_slot_title(task_id, title))
    pane_log = str(pane_dir / f"{(repo_path or project).replace('/', '_')}-{ref_key}.pane.log")
    window_env = folder_git_env(git_ceiling)
    pid = fleet_launch.vis_window(win, worktree, f"bash {shq(launch_path)}", pane_log,
                                  **({"extra_env": window_env} if window_env else {}))
    runner_registry.record(task_id=task_id, pane_pid=pid, worktree=worktree, session_id=runner_session_id,
                           report_dir=report_dir, provider=provider, api_key_account=api_key_account,
                           runtime=runtime, dispatch_id=dispatch_id, generation=generation)
    # The same `ready → active` claim `launch_runner` makes at launch.
    control_plane_api.claim_task_active(task_id)
    os_pid = host_platform.pane_os_pid(pid) if host_platform.is_windows() else pid
    log.info("launch_no_model: task %s runs %s in %s (pane pid %s)", task_id, what, worktree, os_pid)
    return {"session_id": runner_session_id, "pid": os_pid or "", "started_at": proc_started_at(os_pid)}


# ── SDK AGENTS ──────────────────────────────────────────────────────────────────────────────────────────
# An SDK agent (a manifest 2 bundle or an ACP registry install) runs under `jaah-acp-host`, the pane's process.
# `agent_process` stages it before the claim; this writes `launch.json` and opens the pane.

#: The `runner_registry` provider an SDK agent's pane is recorded under (its single home is there).
SDK_PROVIDER = runner_registry.SDK_PROVIDER


def launch_agent(
    *,
    project: str,
    repo_path: str,
    worktree: str,
    task_id: object,
    ref_key: str,
    staged: "agent_process.StagedAgent",
    git_author: "tuple[str, str] | None" = None,
    has_repo: bool = True,
    dispatch_id: str = "",
    generation: "int | None" = None,
) -> dict:
    """Spawn ONE SDK agent run into an already-decided worktree — `_launch_no_model`'s pane, running
    `jaah-acp-host` over the agent's `AcpLaunch` (`sdk_acp_exec_lines`)."""
    return _launch_no_model(
        project=project, repo_path=repo_path, worktree=worktree, task_id=task_id, ref_key=ref_key,
        title=staged.launch.display_name, git_author=git_author, has_repo=has_repo, provider=SDK_PROVIDER,
        runtime=acp_runtime.RUNTIME_ACP,
        exec_lines=lambda runner_session_id: sdk_acp_exec_lines(
            staged, task_id=task_id, runner_session_id=runner_session_id, worktree=worktree),
        what=f"SDK v2 agent {staged.launch.display_name} {staged.launch.version} under {acp_runtime.HOST_BINARY}",
        # The one account whose key the broker serves this pane's root (`cred_broker._resolve_for`).
        api_key_account=staged.launch.key_account,
        dispatch_id=dispatch_id, generation=generation,
    )


def sdk_acp_exec_lines(staged: "agent_process.StagedAgent", *, task_id: object, runner_session_id: str,
                       worktree: str) -> tuple[str, ...]:
    """An SDK agent's pane: the first prompt and `launch.json` in its run dir, then `jaah-acp-host`. A
    bundle that could not be staged, or refused agent variables, are reported as the attempt's `task.fail`
    instead (`agent_process.acp_launch_or_failure`)."""
    import agent_process  # noqa: PLC0415 — deferred: needed only on this launch

    acp, failure = agent_process.acp_launch_or_failure(staged)
    if acp is None:
        return agent_process.staging_failure_lines(failure, runner_session_id, staged.run_dir)
    prompt_path = os.path.join(staged.run_dir, agent_process.PROMPT_FILE)
    launch_json = os.path.join(staged.run_dir, acp_runtime.LAUNCH_JSON)
    document = acp_runtime.launch_document(
        acp, task_id=task_id, session_id=runner_session_id, provider=SDK_PROVIDER,
        auth_kind="", worktree=worktree, additional_dirs=(), charter_path="", prompt_path=prompt_path,
        runtime=agent_process.acp_runtime_extras(staged))
    acp_runtime.register_session(session_id=runner_session_id, task_id=task_id, provider=SDK_PROVIDER)
    for path, content in ((prompt_path, staged.launch.prompt), (launch_json, acp_runtime.launch_json_text(document))):
        err = session_user.write_file_as_session_user(path, content, mode=0o600)
        if err:
            raise RuntimeError(f"launch_agent: could not write {path}: {err}")
    return acp_runtime.exec_lines(launch_json)


# One lock per account home: launches run concurrently (`reconcile.execute.execute_intents`), and the account-layer
# converge, an adapter's `prepare`/`register_mcp`/`api_key_prepare` and the converge cooldown are each a
# read-modify-write of that home (an adapter's trust entry, `_account_layer_online_at`). Two launches
# on different accounts still run at once. `account_lock` adds an `flock` on the home itself, so another
# PROCESS writing it (a console tab's `ctl prepare-trust`) waits too rather than lose a trust or MCP entry.
_ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


def _account_lock(account_dir: object) -> threading.Lock:
    """The in-process half of `account_lock`: one `threading.Lock` per resolved home."""
    with _ACCOUNT_LOCKS_GUARD:
        return _ACCOUNT_LOCKS.setdefault(os.path.realpath(str(account_dir)) if account_dir else "",
                                         threading.Lock())


#: How long `account_lock` waits for another process's `flock` on an account home. Bounded: a runner can open its
#: own home, so an agent holding the lock must cost one launch's wait, never the reconcile pass.
ACCOUNT_FLOCK_WAIT_S = 10.0


def _flock_within(fd: int, wait_s: float) -> bool:
    """Take an exclusive `flock` on `fd` within `wait_s` (non-blocking tries); False when it stayed held."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            host_platform.lock(fd, blocking=False)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


@contextlib.contextmanager
def account_lock(account_dir: object):
    """Hold `account_dir` against every other writer: this process's threads (`_account_lock`), then other
    processes through an exclusive `flock` on the directory itself (a read-only fd suffices and no lock file is
    left in the home), waited for at most `ACCOUNT_FLOCK_WAIT_S`. A home that cannot be opened or locked in time
    (absent, a filesystem without `flock`, or a holder that will not let go) keeps the in-process half only,
    logged: preparation is never refused, nor stalled, for want of the lock."""
    with _account_lock(account_dir):
        fd = None
        if account_dir:
            try:
                fd = os.open(str(account_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                if not _flock_within(fd, ACCOUNT_FLOCK_WAIT_S):
                    log.warning("account lock: %s stayed locked by another process for %.0fs; preparing it "
                                "with the in-process lock only", account_dir, ACCOUNT_FLOCK_WAIT_S)
                    os.close(fd)
                    fd = None
            except OSError as exc:
                log.info("account lock: %s is locked in-process only (%s)", account_dir, exc)
                if fd is not None:
                    os.close(fd)
                fd = None
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)  # closing the fd releases the flock


@ce_caps.serialised
def launch_runner(
    *,
    project: str,
    repo_path: str,
    worktree: str,
    task_id: object,
    ref_key: str,
    system_prompt: str,
    task_prompt: str,
    model: str,
    title: str,
    account_dir: Optional[str],
    autoclose: object = None,
    securestorage_dir: str = "",
    api_key_account: str = "",
    provider: str = agent_providers.DEFAULT_PROVIDER,
    git_author: "tuple[str, str] | None" = None,
    bedrock: "bedrock_launch.BedrockPlan | None" = None,
    has_repo: bool = True,
    memory: "dict | None" = None,
    guard: "dict | None" = None,
    lane: bool = False,
    integration_branch: str = "",
    agent: "agent_process.StagedAgent | None" = None,
    extra_dirs: "tuple[str, ...]" = (),
    runtime: str = acp_runtime.RUNTIME_LEGACY,
    resume_ref: str = "",
    dispatch_id: str = "",
    generation: "int | None" = None,
) -> dict:
    """Spawn ONE runner into an already-decided worktree, and return the ack facts:
    `{"session_id", "pid", "started_at"}`.

    `agent`: an SDK agent run `agent_process.stage` prepared — the launch goes to `launch_agent`, and every
    agent-CLI argument is unread. `lane`: the merge lane's own run — its turn-end refusal joins the hook chain
    (`fleet_launch._lifecycle_chains`).

    `autoclose`: `None` = not decided here (the env `VISIBLE_AUTOCLOSE`, default on, decides); `False`
    is the CP's instruction for the `advisor` persona, whose runner parks on a live conversation and must
    not be reaped — teardown of a settled runner is the CP's `kill_runner` intent anyway.

    `securestorage_dir`: the account's token store when its token is there, passed through
    (`reconcile.enact_launch._launch_into_slot` resolved it, `shared_storage.securestorage_dir`). It reaches the session env AND every hook chain
    (the default CLI does not inherit it into hook subprocesses — `fleet_launch.build_settings`).

    `api_key_account`: an API-key account's slug, passed ONLY to `build_auth_lines`. Deliberately not
    into `write_settings`: no installed hook needs the account, and `autoclose-check.py`'s probe points
    the config-dir variable at OTHER OAuth accounts — a key there would spend an unleased account per Stop,
    and the settings file is a worse place for a secret than `launch.sh`.

    `provider`: which agent CLI; reaches only the adapter's env, hook rendering and exec line. Defaults to
    the catalogue default (a data default); an unregistered provider raises.

    `bedrock`: the Jaah-account plan (K2, `bedrock_launch.BedrockPlan`), or None. Its runner opens in this
    node's Jaah home (`bedrock_launch.config_dir`) on the plan's own model, takes its credentials per request
    from the broker (`with_bedrock_settings`), is recorded as that session's one entitled pane, and is
    tailed live for metering (`bedrock_usage_tail`).

    `memory`: the lease payload's project-memory key, present only for an ON task: the bundle is fetched and
    written beside `<ref_key>.sys` and three exports join `launch.sh` (`project_memory_bundle`). None changes
    nothing.

    `guard`: the lease payload's Architectural Guard stanza (`{"project_id"}`), present only where the guard is
    available for the org: the rule bundle is fetched and written into `_meta`, refreshed every 60 s, and the
    `JAAH_GUARD_*` exports join `launch.sh` (`guard_bundle`) — BEFORE the hook settings are built, which are told
    `guard=` explicitly. `integration_branch` (the slot's base) becomes `JAAH_GUARD_BASE`. None changes nothing.

    `runtime`: the dispatch fact (`acp_runtime.wire_runtime`). `acp` with an adapter that has an `acp_launch` (and
    no Jaah-account plan) starts `jaah-acp-host --launch <ref_key>.launch.json` as the pane root instead of the
    CLI: no per-launch hook settings, key export, consent watch, prompt paste or transcript note, since the host
    does those jobs over ACP (the account layer is still converged: an engine's plugins live there). Anything else
    is the legacy launch, byte for byte.

    `resume_ref`: the engine's own session id, set only for a console hand-off (`reconcile.enact_launch._launch_into_slot`,
    the lease payload's `handoff`): the new front reopens that conversation instead of starting the task. Under
    ACP it rides launch.json's `session_meta` (`NATIVE_REF_KEY`), which the host opens with `session/resume` or
    `session/load`; on the legacy path the pane runs the adapter's `resume_exec_line` (a CLI answering None
    raises — the CP offers a hand-off only for a provider whose catalogue `console.toggle` is set), with no task
    prompt pasted, and a `SESSION_DISCOVER` CLI's vendor session is pinned to `resume_ref` at once (no discovery:
    the session is already known). Empty changes nothing.

    `dispatch_id`/`generation`: the CP dispatch launching this runner (`lease_slot`'s fence), recorded with its pane
    in one `runner_registry.record` entry.

    `has_repo` False: `worktree` is an empty FOLDER slot. The launch is otherwise identical; the
    runner's env and its tmux window carry `GIT_CEILING_DIRECTORIES` (`folder_git_env`).

    Writes `_meta` (prompts, settings, `launch.sh`), seeds trust, opens the tmux window running
    `launch.sh` directly. The occupancy check runs LAST and only LOGS: the veto is upstream in
    `app/slot_state.py::lease`, and a liveness fact is freshest just before the spawn. It lives here, not in
    `vis_window`, because the interactive `ctl.py tmux-window` path legitimately opens busy worktrees."""
    # The community edition's caps, before anything is written: a refusal is the `capacity` verdict the control plane
    # answers by returning the task to `ready` (no attempt spent); an account past the ledger is a fault.
    if at_capacity := ce_caps.capacity_refusal(replacing=worktree):
        log.warning("launch_runner: task %s not launched — %s", task_id, ce_caps.capacity_text(at_capacity))
        return at_capacity
    if why := ce_caps.admit_account(config_dir=account_dir, api_key_account=api_key_account, action="launch"):
        raise RuntimeError(f"launch_runner: {why}")
    if agent is not None:
        return launch_agent(project=project, repo_path=repo_path, worktree=worktree, task_id=task_id,
                            ref_key=ref_key, staged=agent, git_author=git_author, has_repo=has_repo,
                            dispatch_id=dispatch_id, generation=generation)
    # The one adapter lookup, first: it RAISES for a provider with no adapter, which is the intended end
    # of the dispatch — and the next statement is vendor-specific.
    adapter = agents.get_adapter(provider)
    # An API-key account still gets a real config dir — plugins or its vendor's furniture, hooks, the control
    # MCP server, its own session files — just no token in it. Without one the CLI falls back to the session
    # user's own home (the CLI's default config home), which no sweep converges and which may hold ANOTHER
    # account's login: no control MCP tools, no question capture, no collected transcript.
    if api_key_account and not account_dir:
        account_dir = api_key_account_dir(api_key_account)
    if bedrock is not None:
        # The default CLI is the one Bedrock is wired for; the CP routes only it here (I2). Asked of
        # the registry, never a provider-name branch: the default adapter is the Bedrock-capable one.
        if adapter is not agents.get_adapter(agent_providers.DEFAULT_PROVIDER):
            raise RuntimeError(f"launch_runner: a Jaah-account dispatch names the {adapter.provider} CLI; only "
                               f"{agent_providers.DEFAULT_PROVIDER} runs on Bedrock")
        account_dir = account_dir or bedrock_launch.ensure_config_dir()
        if not account_dir:
            log.warning("launch_runner: no Jaah home could be derived or created on this node — task %s's "
                        "runner launches in the session user's %s, without the managed plugins",
                        task_id, default_runtime.HOME_DIR_SHOWN)
        # The plan's model, never the bundle's: on Bedrock a model is one of this tenant's profiles.
        model = bedrock.model_arg
    # Before anything else: the account's `settings.json` installs this runner's hooks and is read at
    # startup, so a converge after the spawn is one this session does not get. Asked of the adapter:
    # the OMC layer's marketplace checkouts break another CLI's MCP handshake, and a vendor answering
    # False furnishes its own home in `prepare()`.
    if adapter.account_layer() == "omc":
        with account_lock(account_dir):
            converge_account_layer(account_dir)
    else:
        log.info("launch_runner: provider %s does not use the managed (omc) account layer — leaving "
                 "%s to its own adapter's prepare()", provider, account_dir or "(no account)")
    shq = fleet_launch.shq
    # The whole `_meta` tree is written AS THE SESSION USER (the runner must read and `bash` it; daemon-
    # owned copies were a measured cleanup burden). A refused write aborts the launch loudly.
    def _write(path: str, content: str, mode: int = META_FILE_MODE) -> None:
        err = session_user.write_file_as_session_user(path, content, mode=mode)
        if err:
            raise RuntimeError(f"launch_runner: could not write {path}: {err}")

    meta = os.path.join(os.path.dirname(worktree), "_meta")
    # Explicit mode: the seam's default is the session umask (`0775`); see `META_DIR_MODE`.
    err = session_user.ensure_dir_as_session_user(meta, mode=META_DIR_MODE)
    if err:
        raise RuntimeError(f"launch_runner: could not create {meta}: {err}")
    sys_file = os.path.join(meta, f"{ref_key}.sys")
    task_file = os.path.join(meta, f"{ref_key}.task")
    _write(sys_file, fleet_launch.node_naming(system_prompt))
    _write(task_file, fleet_launch.node_naming(task_prompt))

    # Minted before build_auth_lines so it rides into the runner's env as CONTROL_RUNNER_SESSION_ID —
    # the id the binding stamps and the runner's hooks report, which the CP's end_attempt fence checks.
    runner_session_id = str(uuid.uuid4())
    # Created as the session user (the runner writes it); never "" — `cred_broker` confines uploads to it.
    report_dir = ensure_report_dir(task_id, meta)
    # The same helper on either runtime: under ACP the engine is told it through its `AcpLaunch`, never this env.
    key_helper = api_key_helper_command(provider, api_key_account or "")
    git_ceiling = folder_git_ceiling(worktree, has_repo)
    # Project memory (ON only): the bundle beside the prompts, refreshed every 60s; a miss is an empty bundle.
    memory_env = (
        project_memory_bundle.prepare_launch(memory, meta=meta, ref_key=ref_key, mode=META_FILE_MODE,
                                             task_id=task_id, worktree=worktree, session_id=runner_session_id)
        if memory else None
    )
    # The Architectural Guard (stanza only): the rule bundle beside the prompts, refreshed every 60s; BEFORE the
    # hook settings below, which are told `guard=` explicitly (the chains never read the daemon env).
    guard_env = (
        guard_bundle.prepare_launch(guard, meta=meta, ref_key=ref_key, mode=META_FILE_MODE, runtime=provider,
                                    base=integration_branch, worktree=worktree)
        if guard and guard_bundle is not None else None
    )
    # The ACP launch, when the dispatch fact and the adapter both allow one (`acp_runtime.acp_launch`); None is
    # the legacy path. After the key helper and the guard: the launch carries both, and a Jaah-account plan's
    # credential export and env (the default adapter's `settings`).
    acp = acp_runtime.acp_launch(adapter, runtime, agents.AcpLaunchContext(
        task_id=str(task_id), session_id=runner_session_id, worktree=worktree, config_dir=account_dir or "",
        project=project, model=model, api_key_account=api_key_account, additional_dirs=tuple(extra_dirs),
        key_helper=key_helper, guard=bool(guard_env), bedrock=bedrock))

    # Anything this CLI needs on disk before it is exec'd (git hooks, trust record, MCP registration) —
    # before the hook file and the spawn, since the CLI reads them at startup. Exactly one `prepare` per launch,
    # on either path, and a raise aborts the launch before the spawn: a refused workspace-trust write (the default
    # adapter's `prepare`) would park the runner on the trust dialog.
    if acp is None:
        with account_lock(account_dir):
            adapter.prepare(worktree=worktree, config_dir=account_dir or "", project=project)
    else:
        with account_lock(account_dir):
            acp_runtime.run_prepare(acp, provider=provider, task_id=task_id)

    auth_line = "\n".join([*build_auth_lines(
        task_id, account_dir, runner_session_id=runner_session_id, worktree=worktree,
        report_dir=report_dir, securestorage_dir=securestorage_dir,
        api_key_account="" if key_helper or acp is not None else api_key_account, provider=provider,
        git_author=git_author,
        bedrock=bedrock, git_ceiling=git_ceiling, memory_env=memory_env, advertise_nodelinks=True,
        guard_env=guard_env,
    ), *api_key_preflight_lines(key_helper),
        *(bedrock_launch.preflight_lines(bedrock, fleet_launch.shq) if bedrock is not None else ()),
        # Launch step 4 (ON only): the node helper, started in the pane so its calls are this task's.
        *project_memory_bundle.helper_launch_lines(memory_env, hub_root=str(HUB_ROOT), worktree=worktree)])

    # The control MCP registration, after the auth lines (`prepare` ran once, above). For a CLI that relocates
    # a whole home this puts the control MCP server where the runner looks.
    # Neither step here may abort a launch: a raise here leaves a leased slot with no runner.
    if acp is None:
        with account_lock(account_dir):
            try:
                adapter.register_mcp(config_dir=account_dir or "")
            except Exception:  # noqa: BLE001 — see the note above; a launch is never lost to registration
                log.warning("launch_runner: the %s adapter's MCP registration failed; the runner is launched "
                            "anyway and may be degraded", provider, exc_info=True)
            # Its own try, after the step above (it merges over the settings file `prepare` and it write): a
            # failure there must not skip the one setting without which a key runner parks on its sign-in screen.
            if api_key_account:
                try:
                    adapter.api_key_prepare(config_dir=account_dir or "")
                except Exception:  # noqa: BLE001 — same rule: the launch goes ahead
                    log.warning("launch_runner: the %s adapter could not select API-key auth; the runner is "
                                "launched anyway and may stop at its sign-in screen", provider, exc_info=True)

    launch_path = os.path.join(meta, f"{ref_key}.launch.sh")
    if acp is not None:
        # The host is the pane root, so `runner_registry.record` below binds it exactly as it binds a CLI.
        _record_runner_binding(task_id, runner_session_id, worktree, project,
                               account_dir if bedrock is None else None)
        if bedrock is not None:
            # Metering waits for the agent's own session id (`acp/runtime.py::_meter_jaah_account`); what the tail
            # needs besides it is kept daemon-side, never in launch.json. Best effort, as `register` is: a miss
            # leaves the session to AWS's invocation log.
            from acp import runtime as acp_hub  # noqa: PLC0415 — only a Jaah-account ACP launch needs it

            try:
                acp_hub.hub().store.set_jaah_account(
                    runner_session_id, task_id=str(task_id), provider=provider, meter={
                        "jaah_session_id": bedrock.session_id, "worktree": worktree,
                        "config_dir": account_dir or default_runtime.session_user_config_dir()})
            except Exception:  # noqa: BLE001 — bookkeeping never fails a launch
                log.warning("launch_runner: could not record Jaah session %s for live metering — its calls are "
                            "billed only by AWS's invocation log", bedrock.session_id, exc_info=True)
        launch_json = os.path.join(meta, f"{ref_key}.{acp_runtime.LAUNCH_JSON}")
        document = acp_runtime.launch_document(
            acp, task_id=task_id, session_id=runner_session_id, provider=provider,
            auth_kind=acp_runtime.auth_kind_of(api_key_account, bedrock=bedrock is not None),
            additional_dirs=tuple(extra_dirs),
            worktree=worktree, charter_path=sys_file, prompt_path=task_file)
        if resume_ref:
            # The host's own key beside the agent's `_meta`: it opens this conversation instead of a new one.
            document["session_meta"] = {**document["session_meta"], NATIVE_REF_KEY: resume_ref}
        _write(launch_json, acp_runtime.launch_json_text(document))
        acp_runtime.register_session(session_id=runner_session_id, task_id=task_id, provider=provider,
                                     key_sign_in=acp.authenticate is not None)
        launch_sh = fleet_launch.build_launch_sh(
            auth_line=auth_line, hub_root=str(HUB_ROOT), worktree=worktree, settings_flag="",
            sys_file=sys_file, task_file=task_file, provider=provider,
            exec_lines=acp_runtime.exec_lines(launch_json))
    else:
        settings_path = os.path.join(meta, f"{ref_key}.settings.json")
        # `project_name=project`: the CP's `projects.name`, not one re-derived from the path (which carries
        # the repo name). `mode=META_FILE_MODE` is decided here, the only layer that knows it is a `_meta` file.
        settings_kwargs = dict(
            extra="true", worktree=worktree, role="runner", hooks_dir=str(HOOKS_DIR), account_dir=account_dir,
            project_name=project, autoclose=autoclose, securestorage_dir=securestorage_dir, provider=provider,
            guard=bool(guard_env),
            # Spread: every other runner's chain is byte-for-byte what it was (`tests/test_agent_adapters.py` goldens).
            **({"lane": True} if lane else {}),
        )
        # Only a CLI with a setting for them takes this branch for `extra_dirs` alone: every other one keeps its
        # adapter's own `write_settings` (which may write more than this file).
        dirs_settings = adapter.additional_dirs_settings(tuple(extra_dirs)) if extra_dirs else {}
        if key_helper or bedrock is not None or dirs_settings:
            # The API key rides `apiKeyHelper`, a Jaah session's credentials `awsCredentialExport`, and a
            # multi-repo slot's other repositories `permissions.additionalDirectories`, in this same settings file
            # (`api_key_helper_command`, `with_bedrock_settings`, `with_additional_dirs`).
            content, settings_flag = with_additional_dirs(*with_bedrock_settings(*with_api_key_settings(
                *fleet_launch.build_settings(settings_path, **settings_kwargs),
                provider=provider, key_helper=key_helper, settings_path=settings_path),
                bedrock=bedrock, settings_path=settings_path),
                provider=provider, dirs=extra_dirs, settings_path=settings_path)
            err = session_user.write_file_as_session_user(settings_path, content, mode=META_FILE_MODE)
            if err:
                raise RuntimeError(f"write_settings: could not write {settings_path}: {err}")
        else:
            settings_flag = fleet_launch.write_settings(settings_path, mode=META_FILE_MODE, **settings_kwargs)

        # An API-key launch's own argv (`api_key_argv`) rides with the model flag: both are quoted CLI options.
        model_args = (("--model", model) if model else ()) + (adapter.api_key_argv() if api_key_account else ())
        # A hand-off reopens the engine's session in the TUI — refused BEFORE the binding below when this CLI's
        # terminal cannot, so a refused resume leaves no binding to a runner that never existed.
        resume_lines = None
        if resume_ref:
            resume_lines = adapter.resume_exec_line(sys_file=sys_file, settings_flag=settings_flag,
                                                    model_args=model_args, native_ref=resume_ref)
            if resume_lines is None:
                raise RuntimeError(f"launch_runner: the {adapter.provider} CLI's terminal cannot resume a "
                                   "session; no hand-off to it")

        # Bind the Task to this runner BEFORE the spawn (worktree, session, account — the dispatcher alone
        # holds all three), so the binding outlives a failed launch; a CP hiccup costs attribution, not a run.
        # A Jaah home is no account: the CP attributes the run by its `jaah_sessions` row, not a config dir.
        _record_runner_binding(task_id, runner_session_id, worktree, project,
                               account_dir if bedrock is None else None)
        if resume_lines is not None:
            # No `session_args`: the resumed session keeps its own id.
            launch_sh = fleet_launch.build_launch_sh(
                auth_line=auth_line, hub_root=str(HUB_ROOT), worktree=worktree, settings_flag=settings_flag,
                sys_file=sys_file, task_file=task_file, provider=provider, exec_lines=tuple(resume_lines))
        else:
            session_args = session_args_for(adapter, runner_session_id)
            launch_sh = fleet_launch.build_launch_sh(
                auth_line=auth_line, hub_root=str(HUB_ROOT), worktree=worktree, settings_flag=settings_flag,
                sys_file=sys_file, task_file=task_file, model_args=model_args, session_args=session_args,
                provider=provider,
            )
    _write(launch_path, launch_sh, mode=META_EXEC_MODE)

    # Composed by the ONE composer (`fleet_launch.slot_window_name`) over the pinned Title format
    # (`task_slot_title`), byte-identical to the CP's `worktrees.title`; tmux's format bounds the length.
    win = fleet_launch.slot_window_name(
        project, _slot_number(worktree), fleet_launch.task_slot_title(task_id, title))
    # `tmux pipe-pane` does NOT create its target dir and fails SILENTLY without it; no installer
    # creates the `$JAAH_HOME` tree on a cloud node, and the pane log is the crash-diagnosis artifact.
    pane_dir = env_helpers.log_dir("runner")
    err = session_user.ensure_dir_as_session_user(str(pane_dir))
    if err:
        raise RuntimeError(f"launch_runner: could not create {pane_dir}: {err}")
    pane_log = str(pane_dir / f"{(repo_path or project).replace('/', '_')}-{ref_key}.pane.log")

    # OBSERVATION ONLY, never refuses: a node-side refusal of a CP-assigned launch is a live-lock (CP
    # re-decides, node re-refuses). Logged loudly because two runners in one worktree silently lose work.
    refusal = fleet_launch.launch_refusal(worktree)
    if refusal:
        log.warning(
            "launch_runner: %s into %s looks occupied (%s) — launching anyway. The CP decided to dispatch "
            "here; a node-side refusal now is the live-lock the owner ended, not a safety net worth "
            "keeping. Two runners in one worktree do not error — they silently lose one runner's "
            "uncommitted work, so this should never happen if the CP's own occupancy check is doing its "
            "job.",
            ref_key, worktree, refusal,
        )
    # `spawn` collects tmux's own answers. Anything reaching this window later must use the WINDOW ID,
    # never the name: operator free text, and a `:` in it resolves to EMPTY WITH EXIT 0 (tmux 3.6).
    spawn: dict = {}
    # Taken BEFORE the spawn, as a float epoch: it is the mtime floor for `discover_session_id(since=…)`,
    # and a transcript written within this call must not fall below it.
    launched_at = time.time()
    # The folder slot's git ceiling rides the window too (`folder_git_env`), so a shell an operator opens in
    # the attached console reads the folder as no repository either. Passed only when there is one, so a
    # worktree's spawn call is unchanged.
    window_env = folder_git_env(git_ceiling)
    pid = fleet_launch.vis_window(win, worktree, f"bash {shq(launch_path)}", pane_log, out=spawn,
                                  **({"extra_env": window_env} if window_env else {}))
    # The one moment the pid→task mapping is knowable: every later answer is runner-forgeable or
    # unreadable by this uid. Best-effort; never unwinds a committed dispatch.
    runner_registry.record(task_id=task_id, pane_pid=pid, worktree=worktree,
                           session_id=runner_session_id, account_dir=account_dir or "",
                           # The only account this runner may ask a key for; the socket's name request
                           # is not a proof, this record is.
                           api_key_account=api_key_account or "",
                           report_dir=report_dir, provider=adapter.provider,
                           # The only Jaah session this runner may ask credentials for.
                           bedrock_session=bedrock.session_id if bedrock is not None else "",
                           # The pane is `jaah-acp-host`: the observation senses it by this pane (`reconcile`).
                           runtime=acp_runtime.RUNTIME_ACP if acp is not None else "",
                           # The dispatch fence, in the same entry: a re-served lease finds its own runner.
                           dispatch_id=dispatch_id, generation=generation)
    # `ready → active` the moment the runner exists, as `_launch_no_model` does: the runner's SessionStart
    # claim is a second, idempotent try, and a launched runner is never left looking unstarted.
    control_plane_api.claim_task_active(task_id)
    if bedrock is not None and acp is None:
        # Live metering starts with the pane: the tail reads this runner's transcript from here on. Under ACP the
        # hub registers it once the host names the agent's session (`set_jaah_account` above).
        bedrock_usage_tail.register(
            jaah_session_id=bedrock.session_id, pane_pid=pid, worktree=worktree,
            config_dir=account_dir or default_runtime.session_user_config_dir(),
            native_session_id=runner_session_id)
    # Where this session's transcript will be found, for a CLI that mints its own id (otherwise the run
    # shows in Audit > Sessions with no transcript). Every legacy provider, no branch. Best-effort. Not under
    # ACP: the host streams the session's events, so no vendor transcript is looked for.
    if acp is None:
        session_vendor.record(runner_session_id, provider=provider, config_dir=account_dir or "",
                              worktree=worktree, launched_at=launched_at,
                              usage_floor=bool(resume_ref))
        # A resume's vendor session is known, not discovered: pinned now (discovery after `launched_at` would
        # find nothing new, or name another runner's rollout).
        if resume_ref and adapter.session_binding() == agents.SESSION_DISCOVER:
            session_vendor.pin_vendor_session(runner_session_id, resume_ref)
    # Mark the agents cache dirty so the next tick re-probes this account; never probe inline (a Node
    # cold start on the dispatch path, and too early to see the new session). An API-key runner has no
    # home (`api_key_account_dir` failed): its session then registers in the session user's own CLI home.
    env_helpers.mark_agents_dirty(account_dir or default_runtime.session_user_config_dir())
    # The backstop for a consent seed that missed: until the first composer, accept any startup consent
    # dialog the CLI shows (`consent_watch`). A daemon thread, like the paste below; a no-op for a CLI
    # with none known. The stable window id only: a name carries the operator's title and may resolve
    # elsewhere.
    if acp is None and adapter.consent_dialogs() and spawn.get("window_id"):
        threading.Thread(
            target=_watch_consents,
            args=(spawn["window_id"], adapter.consent_dialogs(), worktree, task_id),
            name=f"consent-watch-{ref_key}", daemon=True,
        ).start()
    # A CLI that takes no prompt at launch gets its task pasted into the pane (a no-op otherwise). On a
    # daemon thread: the paste waits for the TUI to paint, and blocking the reconcile pass would stall
    # every other intent. Nothing recovers a lost race — the runner never has a turn, so autoclose is
    # never armed, and its SessionStart already claimed the task — so the thread kills the window
    # instead (a dead runner on an active task is recoverable). The stable window id, not `win`.
    # Never on a resume: the reopened conversation already holds the task, and a paste would restart it.
    if acp is None and not resume_ref and adapter.task_delivery() == agents.TASK_TMUX_PASTE:
        threading.Thread(
            target=_deliver_task_prompt,
            args=(spawn.get("window_id") or win, task_prompt, worktree, adapter, task_id,
                  account_dir or "", launched_at, runner_session_id),
            name=f"task-prompt-{ref_key}", daemon=True,
        ).start()
    # The ack names a KERNEL pid, as `reconcile`'s later report does: on Windows tmux's pane pid is an MSYS
    # pid, so it is mapped first (`host_platform.pane_os_pid`); posix maps to itself.
    os_pid = host_platform.pane_os_pid(pid) if host_platform.is_windows() else pid
    return {"session_id": runner_session_id, "pid": os_pid or "", "started_at": proc_started_at(os_pid)}


def session_args_for(adapter, runner_session_id: str) -> tuple[str, ...]:
    """The `--session-id` argv elements for this CLI, or none at all.

    Offered only to a `SESSION_ASSIGN` CLI; a `SESSION_DISCOVER` CLI given an unknown flag dies at argv.
    The runner still gets the id as `CONTROL_RUNNER_SESSION_ID`. A named function so the decision is
    testable rather than copied into a test."""
    if adapter.session_binding() == agents.SESSION_ASSIGN:
        return ("--session-id", runner_session_id)
    return ()


def _watch_consents(win: str, dialogs: tuple, worktree: str, task_id: object) -> None:
    """`consent_watch.watch_launch` on a launch thread (never raises)."""
    import consent_watch  # deferred: subprocess-heavy, needed only after a launch

    consent_watch.watch_launch(win, dialogs, expect_cwd=worktree, task_id=task_id)


def _deliver_task_prompt(win: str, task_prompt: str, worktree: str, adapter, task_id: object,
                         config_dir: str, launched_at: float, session_id: str = "") -> None:
    """Paste one runner's task prompt into its freshly launched pane, and END THE WINDOW if it did not
    land.

    The kill is the point: a promptless runner is never reclaimed (the CP sees a healthy `active`
    runner; autoclose needs a Stop that never comes). Killing it yields a dead runner on an active task,
    which the break/reclaim path handles. Through `kill_gates.guarded_window_kill`, the one choke point
    for an automated kill (stable id, identity proof). A failed kill is logged: a human has to look."""
    import answer_delivery  # deferred: subprocess-heavy sibling, needed only on this delivery
    import kill_gates

    try:
        delivered = answer_delivery.deliver_task_prompt(
            win, task_prompt, expect_cwd=worktree, tui=adapter.tui())
    except Exception as exc:  # noqa: BLE001 — a worker thread that raises takes the log with it
        log.warning("launch_runner: task %s prompt delivery raised (%s: %s) — this runner has nothing "
                    "to work on", task_id, type(exc).__name__, exc)
        delivered = False
    if delivered:
        _record_discovered_session(adapter, task_id, config_dir, worktree, launched_at, session_id)
        return
    log.warning("launch_runner: task %s prompt was NOT delivered into %s — this runner is up with "
                "nothing to work on and nothing reclaims it, so the window is being killed", task_id, win)
    try:
        killed = kill_gates.guarded_window_kill(
            win, f"task {task_id}: the runner's task prompt could not be delivered into its pane",
            expect_cwd=worktree)
    except Exception as exc:  # noqa: BLE001 — see above; this thread owns its own failures
        log.warning("launch_runner: task %s — killing the promptless runner in %s raised (%s: %s); the "
                    "slot is held and needs a human", task_id, win, type(exc).__name__, exc)
        return
    if not killed:
        log.warning("launch_runner: task %s — the promptless runner in %s could not be killed (gone, "
                    "unresolvable, or refused on identity); the slot may be held and needs a human",
                    task_id, win)


def _record_discovered_session(adapter, task_id: object, config_dir: str, worktree: str,
                               launched_at: float, session_id: str = "") -> None:
    """Read back the VENDOR's own session id for a `SESSION_DISCOVER` runner, once it has had a turn.

    Without it `adapter.transcript()` can never be handed a real id and Audit > Sessions has no
    transcript. Here because the id appears with the first turn; `launched_at` keeps the previous
    runner's rollout from being named. Pinned onto this node's dispatch note (`session_vendor`), used
    only for resume and transcript lookup — `CONTROL_RUNNER_SESSION_ID` stays the CP's binding. Only
    paste-delivered vendors reach this; argv-delivered ones pin on first resolution
    (`session_sweep.resolve_capture`)."""
    if adapter.session_binding() != agents.SESSION_DISCOVER:
        return
    try:
        vendor_session = adapter.discover_session_id(
            config_dir=config_dir, worktree=worktree, since=launched_at)
    except Exception as exc:  # noqa: BLE001 — diagnostic only; no dispatch may fail on a lookup
        log.warning("launch_runner: task %s — discovering the vendor session id raised (%s: %s)",
                    task_id, type(exc).__name__, exc)
        return
    if not vendor_session:
        log.info("launch_runner: task %s — no vendor session id discoverable yet under %s; its "
                 "transcript cannot be looked up until one appears", task_id, config_dir or "(none)")
        return
    # Pinned, not merely logged: discovery is a floor with no ceiling, so a later ask (after re-lease)
    # could name a newer runner's session. This is the earliest, safest answer; the pin is first-wins.
    if session_id:
        session_vendor.pin_vendor_session(session_id, vendor_session)
    # `%r` on both runner-controlled values: a newline would forge a journal record.
    # The lookup is guarded because it is only a diagnostic and `transcript()` may raise on a truncated
    # walk — unguarded, it would kill this thread after the pin already landed.
    try:
        where = str(adapter.transcript(config_dir=config_dir, worktree=worktree,
                                       session_id=vendor_session) or "not written yet")
    except Exception as exc:  # noqa: BLE001 — same rule as the discovery above: never cost the dispatch
        where = "not resolvable right now (%s)" % type(exc).__name__
    log.info("launch_runner: task %s — vendor session id %r (transcript: %r)", task_id, vendor_session,
             where)


# There are no on-demand dispatch verbs: an on-demand dispatch is a request for a CP decision, answered
# with a `lease_slot` intent this node's reconcile pass enacts.
