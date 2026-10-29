"""ce_caps.py — the community edition's caps, enforced by the node on its own (independent of the control plane).

Two caps, both literals in `edition.py` and never configurable (PRD §7):
  * agent sessions — a launch (fleet runner or console agent) is refused while `CE_MAX_AGENTS` agent windows
    are live in this node's tmux session (pool worktrees are not capped: only live agents are);
  * accounts — the first `CE_MAX_ACCOUNTS` DISTINCT accounts the node is asked to mint, log in, probe or
    launch with are admitted into a ledger; any other is refused until the control plane's `account_delete`
    (the forget) drops one and frees its place.

Every entry point is a no-op unless `edition.IS_CE`, so a cloud node behaves exactly as before. A refused
launch answers `capacity_refusal`'s dict; `CAPACITY_MARKER` opens the text the slot-intent ack carries, which is
how the control plane tells a capacity refusal from a fault (`apps/api/app/node_capacity.py`).
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, ParamSpec, TypeVar

import edition
import env_helpers
import file_io
import fleet_launch
import host_platform
import session_user
from fleet_tmux_server import HOLDER_WINDOW

log = logging.getLogger("jaahd.ce_caps")
P = ParamSpec("P")
R = TypeVar("R")

# The lead-in of every capacity refusal's text; the control plane matches it anchored.
CAPACITY_MARKER = "capacity: "
# The ledger of admitted accounts, in the node's state root under `JAAH_HOME` — the volume (image) or data dir
# (native) that also holds the account dirs, so the two never drift apart. The native node runs one uid, and the
# container's daemon uid has no volume of its own to keep it across an upgrade, so a runner can edit it on both: a
# licence term, not a security boundary.
LEDGER_NAME = "local-accounts.json"
# Held across one agent spawn's count and launch (`serialised`).
SPAWN_LOCK_NAME = "local-spawn.lock"
# Windows that are a human's shell or a view, never an agent (`fleet_launch._live_pane_refusal` skips them too).
_NON_AGENT_SUFFIXES = ("|term", "|web")


# ── agent sessions ─────────────────────────────────────────────────────────────────────────────────────────

def live_agent_windows() -> int | None:
    """How many agent windows are live in this node's tmux session; None when tmux could not be asked."""
    env = fleet_launch.tmux_safe_env()
    try:
        session = fleet_launch.tmux_session()
        listing = subprocess.run(
            session_user.tmux_cmd(session_user.fleet_socket(env), "list-windows", "-t", f"={session}",
                                  "-F", "#{window_name}"),
            env=env, capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — an unanswered query is unknown, never "none live"
        log.warning("ce_caps: the tmux window query failed (%s)", exc)
        return None
    if listing.returncode != 0:
        stderr = (listing.stderr or "").lower()
        if "no server running" in stderr or "can't find session" in stderr or "error connecting" in stderr:
            return 0
        log.warning("ce_caps: the tmux window query failed (rc=%s): %s", listing.returncode, stderr.strip())
        return None
    return sum(1 for name in (listing.stdout or "").splitlines()
               if name and name != HOLDER_WINDOW and not name.endswith(_NON_AGENT_SUFFIXES))


def serialised(fn: Callable[P, R]) -> Callable[P, R]:
    """Run `fn` (an agent spawn: a fleet launch, a console create) under one node-wide lock on a community-edition node,
    so the count `capacity_refusal` reads inside it cannot be raced by a second spawn — two launches each seeing 9
    live would otherwise both start. A cloud node calls `fn` unlocked, exactly as before."""
    @functools.wraps(fn)
    def gate(*args: P.args, **kwargs: P.kwargs) -> R:
        if not edition.IS_CE:
            return fn(*args, **kwargs)
        path = env_helpers.state_dir(SPAWN_LOCK_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+") as lock:
            host_platform.lock(lock)
            return fn(*args, **kwargs)
    return gate


def capacity_refusal(live: int | None = None, *, replacing: str = "") -> dict | None:
    """The refusal of one more agent session — `{"ok": False, "refused": "capacity", "live", "cap"}` — or None.

    `replacing` names the worktree being launched into: a live runner already there is replaced, not added to, so
    it is not counted. None on a cloud node, and when the count is unknown: the control plane's own cap still holds
    the line, and a node that refused on a failed query would wedge every launch behind a tmux hiccup."""
    if not edition.IS_CE:
        return None
    live = live_agent_windows() if live is None else live
    if live is not None and replacing:
        import runner_registry  # noqa: PLC0415 — deferred: only a local launch asks

        live -= runner_registry.live_in_worktree(replacing) is not None
    if live is None or live < edition.CE_MAX_AGENTS:
        return None
    return {"ok": False, "refused": "capacity", "live": live, "cap": edition.CE_MAX_AGENTS}


def capacity_text(refusal: dict) -> str:
    """The refusal as one line, `CAPACITY_MARKER` first — the slot-intent ack's detail."""
    return (f"{CAPACITY_MARKER}{refusal['live']} of {refusal['cap']} agent sessions are live on this node — "
            f"the work waits for one to end")


# ── accounts ───────────────────────────────────────────────────────────────────────────────────────────────

def account_ref(*, config_dir: str | None = "", api_key_account: str | None = "") -> str:
    """The ledger key of one account: its slug. An OAuth account's config dir, an API-key account's dir and the
    token store's leaf are `<jaah_home>/auth/<slug>` and `<jaah_home>/auth/.shared/<slug>`, so keying every kind
    on the slug never counts one account twice; a config dir outside that tree is keyed on its resolved path."""
    if api_key_account:
        return api_key_account.strip()
    if not config_dir:
        return ""
    path = Path(config_dir.rstrip("/")).expanduser()
    if path.parent.name == "auth" or (path.parent.name == ".shared" and path.parent.parent.name == "auth"):
        return path.name
    return str(path.resolve())


def _ledger_path() -> Path:
    return env_helpers.state_dir(LEDGER_NAME)


@contextlib.contextmanager
def _locked_ledger():
    """The ledger's refs, read and written under one exclusive lock (two launches never both take the last place)."""
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+") as lock:
        host_platform.lock(lock)
        refs = file_io.read_json_or(path, {}, expect=dict).get("refs", [])
        refs = [r for r in refs if isinstance(r, str) and r]
        before = list(refs)
        yield refs
        if refs != before:
            env_helpers.atomic_write_json(path, {"refs": refs}, mode=0o600)


def admit_account(*, config_dir: str | None = "", api_key_account: str | None = "", action: str = "use") -> str:
    """"" when this account may be used (it is in the ledger, or a place is free and it now holds it); else why
    not. `action` names what was asked (mint, log in, probe, launch) in the refusal. Always "" on a cloud node."""
    if not edition.IS_CE:
        return ""
    ref = account_ref(config_dir=config_dir, api_key_account=api_key_account)
    if not ref:
        return ""
    with _locked_ledger() as refs:
        if ref in refs:
            return ""
        if len(refs) < edition.CE_MAX_ACCOUNTS:
            refs.append(ref)
            return ""
    return (f"{CAPACITY_MARKER}refusing to {action} with account {ref!r}: this node already uses "
            f"{edition.CE_MAX_ACCOUNTS} accounts, the most the community edition allows — remove one to free its place")


def forget_account(*, config_dir: str | None = "", api_key_account: str | None = "") -> None:
    """Drop the account from the ledger, freeing its place (the control plane's forget). No-op on a cloud node."""
    if not edition.IS_CE:
        return
    ref = account_ref(config_dir=config_dir, api_key_account=api_key_account)
    if not ref:
        return
    with _locked_ledger() as refs:
        if ref in refs:
            refs.remove(ref)
            log.info("ce_caps: forgot account %s — %d of %d places used", ref, len(refs),
                     edition.CE_MAX_ACCOUNTS)


def admitted_accounts() -> list[str]:
    """The admitted refs, oldest first (for a status read and the tests)."""
    with _locked_ledger() as refs:
        return list(refs)


# ── the provider CLIs' telemetry off-switches ──────────────────────────────────────────────────────────────

# Names the file the node's unit or image loaded (the image's `cli_env`); its pairs also ride every
# agent window, whose environment the tmux server would otherwise freeze to an allowlist.
CLI_ENV_FILE_ENV = "JAAH_CLI_ENV_FILE"


def cli_env() -> dict[str, str]:
    """The `KEY=VALUE` pairs of the CLI telemetry file, or {} (a cloud node, or no file named)."""
    if not edition.IS_CE:
        return {}
    path = os.environ.get(CLI_ENV_FILE_ENV, "")
    if not path:
        return {}
    pairs: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("ce_caps: cannot read the CLI env file %s: %s", path, exc)
        return {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.isidentifier():
            pairs[key] = value
    return pairs


def window_env() -> dict[str, str]:
    """What every agent window of a community-edition node carries beyond the fleet allowlist, or {} on a cloud node:
    the CLI off-switches, and the posture paths a runner's hooks resolve (the credential socket, the run root, the
    tmux directory), which a user-level node keeps out of the packaged `/run/jaah` defaults."""
    if not edition.IS_CE:
        return {}
    import environment  # noqa: PLC0415 — a stdlib leaf; deferred like the other posture reads here

    paths = {name: os.environ[name] for name in (environment.CRED_SOCKET_ENV, environment.RUN_ROOT_ENV,
                                                 environment.TMUX_TMPDIR_ENV) if os.environ.get(name)}
    return {**cli_env(), **paths}
