"""The node half of the per-provider runtime switch (architecture C7, D-SW-1): which runtime a launch takes, and
the `launch.json` + exec line that start `jaah-acp-host` when it is ACP.

The control plane resolves the operator's `acp_runtime` switch at dispatch and sends `runtime: "acp" | "legacy"`
in the dispatch facts (`apps/api/app/acp_runtime.py`). The node obeys it, with two downgrades of its own: a
provider whose adapter has no `acp_launch` member, and, on a node off Linux, a provider whose engine adapter
(`acp_binary`) this node has not installed, run legacy whatever the fact says. Every bundle ships `jaah-acp-host`
(in its macOS and Windows bin directories), but the adapters are npm installs a Mac's `jaah-converge-clis` and
a Windows runner service's `claude_cli_windows.ensure` converge after it: an ACP pane launched before them died at
spawn. On Linux nothing is sensed here: a vnode's `jaah-runner-cli-vn@` converges them after the node is online, so
the control plane places no ACP run before this node attests the adapter (`routes/fleet.py::_acp_engine_refusal`).
Absent (an older control plane) means legacy. The runtime is fixed for the attempt; nothing here carries a session
across runtimes.

The host CLI is `jaah-acp-host --launch <launch.json>`; `launch.json` holds exactly `LAUNCH_JSON_KEYS`
(architecture §3.4b). `AcpLaunch.prepare` runs before the spawn and is never serialised.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import account_auth
import agent_providers
import agents
import fleet_launch
import host_platform
import runner_clis
from acp import launch

log = logging.getLogger("acp-runtime")

RUNTIME_ACP = "acp"
RUNTIME_LEGACY = "legacy"
#: The catalogue runtimes (`Capabilities.runtime`) an `acp` fact may move: switchable `acp`, and `acp_only` (an engine
#: with no terminal runtime, which the control plane always sends `acp`).
ACP_CATALOGUE_RUNTIMES: frozenset[str] = frozenset({RUNTIME_ACP, "acp_only"})
#: The dispatch-fact key, on the static `lease_slot` payload and the MicroVM `launch` payload alike.
RUNTIME_KEY = "runtime"

#: The host binary, resolved on the runner PATH `fleet_launch.build_launch_sh` exports.
HOST_BINARY = "jaah-acp-host"
#: The launch document's file name: beside `launch.sh` (`<ref_key>.launch.json` on a static node).
LAUNCH_JSON = "launch.json"

#: Exactly the keys of `launch.json`; argv, env, session_meta, mcp_servers and authenticate come from the adapter's
#: `AcpLaunch`. Single-homed in the
#: host's reader (`acp/launch.py`), which refuses any other set.
LAUNCH_JSON_KEYS: tuple[str, ...] = launch.LAUNCH_FIELDS


def wire_runtime(payload: dict) -> str:
    """The dispatch fact as sent: `acp` only when the control plane said exactly that, else `legacy`."""
    return RUNTIME_ACP if payload.get(RUNTIME_KEY) == RUNTIME_ACP else RUNTIME_LEGACY


#: The launch lane of a Jaah-account (Bedrock) run: no account pair, and its own switch on the control plane
#: (`apps/api/app/acp_runtime.py::LANE_JAAH_ACCOUNT`).
LANE_JAAH_ACCOUNT = "jaah_account"


def auth_kind_of(api_key_account: str, *, bedrock: bool = False) -> str:
    """The run's auth kind: the Jaah-account lane for a Bedrock run, else an API-key slug means `api_key`, none
    `oauth`."""
    if bedrock:
        return LANE_JAAH_ACCOUNT
    return account_auth.AUTH_KIND_API_KEY if api_key_account else account_auth.AUTH_KIND_OAUTH


def acp_launch(adapter, runtime: str, ctx: agents.AcpLaunchContext) -> Optional[agents.AcpLaunch]:
    """The adapter's `AcpLaunch` when this run is ACP, else None (the legacy path).

    None also when the adapter has no `acp_launch` member, and when the catalogue pins this run's auth kind to the
    legacy path (`Capabilities.runtime` `legacy_only`: a Claude subscription login, A5): either runs legacy
    regardless of the switch, so a stray `acp` fact never moves it. `acp_only` moves like `acp`. A Jaah-account run
    (`ctx.bedrock`) is no catalogue pair: the launch path already admitted its provider (`bedrock_launch.admits`)."""
    if runtime != RUNTIME_ACP:
        return None
    if not host_platform.is_linux() and not _engine_installed(adapter.provider):
        log.info("acp runtime: this node has no ACP adapter for %s — task %s runs on the legacy path",
                 adapter.provider, ctx.task_id)
        return None
    caps = agent_providers.capabilities_of(adapter.provider)
    kind = auth_kind_of(ctx.api_key_account, bedrock=ctx.bedrock is not None)
    if ctx.bedrock is None and caps is not None and dict(caps.runtime).get(kind) not in ACP_CATALOGUE_RUNTIMES:
        log.info("acp runtime: provider %s runs %s accounts on the legacy path only — task %s stays legacy",
                 adapter.provider, kind, ctx.task_id)
        return None
    build = getattr(adapter, "acp_launch", None)
    if build is None:
        log.info("acp runtime: provider %s has no ACP launch — task %s runs on the legacy path",
                 adapter.provider, ctx.task_id)
        return None
    return build(ctx)


def _engine_installed(provider: str) -> bool:
    """Whether the runner's PATH finds `provider`'s ACP engine adapter here (`runner_clis.sense_runner_clis`, the
    search the node attests from). Unknowable (None) counts as absent: the legacy path always has its CLI."""
    record = agent_providers.by_value(provider)
    sensed = runner_clis.sense_runner_clis()
    return bool(record is not None and record.acp_binary and sensed and record.acp_binary in sensed)


def run_prepare(launch: agents.AcpLaunch, *, provider: str, task_id: object) -> None:
    """`launch.prepare`, raising as the legacy path's `prepare` does: the caller aborts the launch before the
    host spawns, so a refused trust write never parks a runner on its dialog."""
    if launch.prepare is None:
        return
    try:
        launch.prepare()
    except Exception:
        log.warning("acp runtime: the %s ACP launch's preparation failed for task %s; the launch is aborted",
                    provider, task_id)
        raise


def launch_document(launch: agents.AcpLaunch, *, task_id: object, session_id: str, provider: str, auth_kind: str,
                    worktree: str, additional_dirs: tuple[str, ...], charter_path: str,
                    prompt_path: str, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """`launch.json`'s content: exactly `LAUNCH_JSON_KEYS`. `runtime`: per-run fields beside `kind` (an SDK run's
    `agent_process.acp_runtime_extras`), written in the host's object form; None writes the bare `"acp"`. The
    launch's `host_hooks` ride it as `lifecycle_hooks` (`acp/lifecycle.py`)."""
    if launch.host_hooks:
        runtime = {**(runtime or {}), "lifecycle_hooks": dict(launch.host_hooks)}
    return {
        "task_id": str(task_id),
        "session_id": session_id,
        "provider": provider,
        "auth_kind": auth_kind,
        "additional_dirs": list(additional_dirs),
        "worktree": worktree,
        "charter_path": charter_path,
        "prompt_path": prompt_path,
        "runtime": {"kind": RUNTIME_ACP, **runtime} if runtime else RUNTIME_ACP,
        "argv": list(launch.argv),
        "env": dict(launch.env),
        "session_meta": dict(launch.session_meta),
        "mcp_servers": [dict(server) for server in launch.mcp_servers],
        "authenticate": dict(launch.authenticate) if launch.authenticate else None,
    }


def register_session(*, session_id: str, task_id: object, provider: str, key_sign_in: bool = False) -> None:
    """Record `session_id` as an ACP run in this daemon's runtime hub, BEFORE its host starts: the hub serves
    `runtime.events_append` / `runtime.request_open` only for a session a launcher recorded here, so a legacy
    run (a VM guest speaks as its VM) cannot make itself an ACP one. Called beside every `launch_document`;
    `key_sign_in` when the launch names `authenticate` (`runtime.account_key` serves no other session).
    Raises, as `run_prepare` does: the caller aborts the launch before the host spawns, since a host whose
    session is unrecorded would have every event and question refused and run blind."""
    from acp import runtime as acp_hub  # noqa: PLC0415 — deferred: the hub is only needed by an ACP launch

    try:
        acp_hub.hub().register(session_id=session_id, task_id=task_id, provider=provider, key_sign_in=key_sign_in)
    except Exception:
        log.warning("acp runtime: could not record session %s of task %s as ACP; the launch is aborted",
                    session_id, task_id, exc_info=True)
        raise


def launch_json_text(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def exec_lines(launch_json_path: str) -> tuple[str, ...]:
    """`launch.sh`'s exec line: the host is the pane root (static) or the guest's foreground process (MicroVM)."""
    return (f"exec {HOST_BINARY} --launch {fleet_launch.shq(launch_json_path)}",)
