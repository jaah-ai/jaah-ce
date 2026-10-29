"""agent_config_dirs.py — the credential-account config-dir roster every cross-account liveness/idle
probe unions, shared with `hooks/autoclose-check.py` (loaded by path) so no caller re-derives its own,
narrower copy — a re-derivation scoped to `$CLAUDE_CONFIG_DIR` alone caused the 2026-08-18 false reap.

Stdlib only at import; `shared_storage` and `environment` are stdlib-only leaves, so a hook loading this
file by path still resolves them by bare name.

Narrower than `reconcile.accounts_probe._agent_config_dirs` (the liveness SSOT, which publishes the wider
`probed_config_dirs` roster with no filter): these callers are one-shot kill-safety gates that cannot
publish a coverage set, so `dir_present` keeps an unreadable account IN the roster instead. A probed set
that can shrink silently is the bug this module closes.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import environment     # stdlib-only leaf: the ONE home of every outward surface's env var name
import shared_storage  # stdlib-only leaf: the ONE home of "which dir holds this account's token"

log = logging.getLogger("jaahd.agent_config_dirs")


def dir_present(path: str) -> bool:
    """Is PATH a directory — with UNREADABLE answered True rather than False.

    `os.path.isdir` swallows EACCES into False, silently dropping an account dir this process may not
    stat out of the probe set. Only absence licenses skipping a probe."""
    try:
        os.stat(path)          # EACCES here means an ancestor we may not traverse, never "absent"
    except PermissionError:
        return True
    except OSError:
        return False
    return os.path.isdir(path)


def agent_config_dirs(*, read_cp: bool = True) -> list[str]:
    """Config dirs a cross-account liveness/idle probe must union: the local default (`CLAUDE_CONFIG_DIR`,
    always included) PLUS every credential-pool account's config_dir (`control_plane_api.agent_api_accounts`).
    The default is an UNCONDITIONAL floor: a kill-safety gate must never probe zero dirs, so an unreachable
    API degrades to probing the default. Present-or-unreadable dirs only; NOT deduped — callers dedupe.

    `read_cp=False` skips the CP read and returns the floor: a caller holding no node token (user `jaah`)
    would only earn a 401, which the prod token-failure alarm counts."""
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")]
    if not read_cp:
        return dirs
    try:
        from control_plane_api import agent_api_accounts
        for rec in agent_api_accounts(include_disabled=True):
            cd = rec.get("config_dir") if isinstance(rec, dict) else None
            if cd and dir_present(cd):   # present-or-unreadable; see `dir_present` for why not isdir
                dirs.append(cd)
    except Exception as exc:  # unreachable API / import edge — keep the default floor, never crash the gate
        log.warning("agent_config_dirs: DB account read failed, probing default config dir only: %s", exc)
    return dirs


def slot_of(worktree: str) -> tuple[str, str] | None:
    """`(project, number)` for a `<project>-N` / `<project>/N` pool worktree, else None (a main clone or
    an ad-hoc dir). Same two shapes as `hooks/user-session-end.py`'s `slot_of`, but deliberately a second
    copy: the hook must avoid daemon-space imports, and this one is stricter than the hook's tests absorb.

    SCOPED TO `$JAAH_HOME` when set: a scratch checkout like `~/work/2` would otherwise read as slot
    `(work, 2)`, a permanent silent decline in a kill-safety gate. `JAAH_HOME` unset falls back to the
    unscoped match — a missing env var must never make a real pool slot invisible."""
    # `strip("/")`, not a truthiness test: `abspath("")` is the CWD, so "/" would resolve to a REAL slot.
    path = os.path.abspath(worktree.rstrip("/")) if worktree.strip("/") else ""
    if not path:
        return None
    jaah_home = environment.pool_root_raw()
    if jaah_home:
        home = os.path.abspath(jaah_home)
        if not (path == home or path.startswith(home + os.sep)):
            return None
    base = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    if parent and base and base.isdigit():
        return (parent, base)
    if "-" not in base:
        return None
    project, number = base.rsplit("-", 1)
    if not project or not number or not number.isdigit():
        return None
    return (project, number)


# Which of three states `<config_dir>/.credentials.json` is in. `agents/claude.py::ClaudeAdapter
# .credential_state` delegates here.
CREDS_TOKEN = "token"            # parsed, and it carries an accessToken-shaped field
CREDS_NONE = "none"              # absent, unparseable, or parsed with no token — no credential here
CREDS_UNREADABLE = "unreadable"  # it is THERE and this process may not read it


def creds_state(config_dir: str) -> str:
    """Which of the three states above `<config_dir>/.credentials.json` is in. NEVER reads/returns/logs
    the token VALUE — only presence + shape.

    `CREDS_UNREADABLE` means not necessarily idle: `claude agents --json` returning `[]` may be an auth
    failure. `CREDS_NONE` cannot say whether the account is disabled or an enabled one that lost its
    credential — `control_plane_api.account_enabled` is the discriminator. Cannot catch a credential
    revoked mid-session (the file still parses).

    Looked for at `shared_storage.token_dir`: a SHARED account's token is on the EFS store, not under
    `config_dir`, so the blind spelling would read a healthy account as `CREDS_NONE`.

    Read by `agents.base.credential_file_present` (`O_NOFOLLOW|O_NONBLOCK`, regular files only): the
    account dir is runner-group-writable, and `reconcile.enact_launch._launch_into_slot` reaches this from
    the event loop, so a planted FIFO or symlink is `CREDS_NONE`, never a hang. Imported here, not at the
    top: this module stays stdlib-only at import for the hook that loads it by path."""
    from agents.base import credential_file_present, credential_state
    from agents.claude_login import MAX_CREDENTIAL_BYTES, has_oauth_token

    cred = str(Path(shared_storage.token_dir(str(config_dir))).expanduser() / ".credentials.json")
    return credential_state(cred, lambda: credential_file_present(
        cred, max_bytes=MAX_CREDENTIAL_BYTES, validate=has_oauth_token),
        runner_shape="oauth", max_bytes=MAX_CREDENTIAL_BYTES)
