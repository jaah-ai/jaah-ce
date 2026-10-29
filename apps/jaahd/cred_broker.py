"""Credential broker — the daemon's local answer to "what is my git login?", over a unix socket.

User `jaah` (every agent session on the node) must not read `/etc/jaah/node.env`, so the
daemon makes CP calls on local callers' behalf over a socket whose reachability is a filesystem question:
`/run/jaah/creds/sock` (0660 after bind) in `/run/jaah/creds/` (2750 jaahd:jaah, systemd-made).
One JSON line each way; two request shapes, both identity-scoped:

    -> {"credential": "github_token"}
    <- {"ok": true, "value": "..."}          or  {"ok": false, "error": "unavailable"}

    -> {"credential": "git_credential", "checkout": "<the caller's working directory>"[, "fresh": true]
        [, "for": {"protocol": "https", "host": "...", "path": "..."}]}
    <- {"ok": false, "error": "...", "destination_refused": true}    when "for" is not the login's repository
    <- {"ok": true, "value": {"scheme": "basic", "username": "...", "token": "...", "host": "...",
                              "root": "...", "forge": "...", "api_base": "...", "repo_path": "...",
                              "web_url": "..."}}

    -> {"call": "task.complete", "args": {"summary": "...", "terminal_type": "analysis"}}
    <- {"ok": true, "result": {...}}         or  {"ok": false, "error": "not_a_runner"}

Verbs: `agent_vocabulary`. `node_token` is NOT served: it would make every `jaah` process a
holder of the node's CP bearer credential; the `call` shape replaced it. Only `github_token` (the bare
token, for the `gh` wrapper), `git_credential` (the same login with its metadata, for git's credential
helper `jaah-git-credential`), the API-key account prefix and a Jaah session's Bedrock credentials
(`bedrock_session:<uuid>`, `_resolve_bedrock`) are served; any other name is `bad_request`, never
`unavailable` ("retry").
Identity is never the request's: uid/pid from SO_PEERCRED, the task from `runner_registry.resolve_peer`'s
/proc ancestry walk. A MicroVM guest is the one caller with no pid here: its connection arrives on its VM's
own control listener, which `jaah-microvmd` created and hands over tagged with the VM id (`VmCaller`,
`serve_vm_connection`, fed by `microvm_control`), and its task is `vms.json`'s row for that VM — nothing the
guest sends, no uid and no process ancestry is consulted for it. Git credential: a peer inside a runner gets its task's launcher's token
(`/tasks/{id}/git-token`); a peer in NO runner gets the system arm (`/git-token?project=`), the credential
of the human who declared that project's slots, with the project read from `clone_identity` (in-process,
never the socket). A project may connect several repositories: a runner's login is for the
repository of the checkout it works in (`_caller_repo_id`), judged by `slot_repos.checkout_repo_id` from where
that checkout sits among its OWN slot's (the slot is the primary, `_repos/<rid>/<N>` repository `<rid>`); a caller
cannot name a repository id, and the CP refuses one its task's project does not connect. A MicroVM guest's
repository is the one its reported checkout sits in under `/jaah/repos/<rid>` (`_vm_repo_id`), admitted only
among the repositories `vms.json` records the VM holding. The system arm requires positive proof via `clone_identity.is_delegated_op`;
residual: a deployment running daemon and runners under one uid.

Cache: keyed by RESOLVED IDENTITY (`task:<id>:<run>` / `system:<project>`), never by name — a name key would hand
the first runner's user's token to every other. A git identity caches its whole login once; each git name
is a projection of that entry, so `github_token` and `git_credential` share one CP trip. TTL (`_GIT_CREDENTIAL_TTL_S`) bounds how long a revoked
or expired token circulates; only successes are cached. Never log a value: uid/pid/name/outcome, cache
hit, and the resolved identity. Stdlib only (plus `control_plane_api`). The `file_body` confinement core
is `_read_confined_upload`.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextvars
import errno
import json
import logging
import mimetypes
import os
import posixpath
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

import account_auth
import bedrock_launch
import agent_vocabulary
import clone_identity
import control_plane_api
# The one home of every outward env var name and default; stdlib-only.
import environment
import git_credential_helper
import host_platform
import nodevault_crypto
import nodevault_identity
import nodevault_store
import pr_stats_capture
import runner_registry
# The one presigned-PUT client (`session_sweep.put_object`), reused rather than re-written.
import session_sweep
import cloud_modules
from file_io import json_object

log = logging.getLogger("jaahd.cred_broker")

# A MicroVM guest's bindings and fixed guest paths; None on a community-edition node, which runs no VM (so no
# `VmCaller` ever reaches the paths that read them).
microvm_registry = cloud_modules.load("microvm_registry")
microvm_seed = cloud_modules.load("microvm_seed")
# `microvm_registry.VmBinding`: a cloud-only module's type, so named here rather than imported.
VmBinding = Any

# The contract path (clients hardcode it). The name and fallback are `environment.credential_socket()`'s
# (shared with `agent_socket`); bound at import because the suite reads it by name, with `socket_path()`
# the call-time read.
SOCKET_PATH = Path(environment.credential_socket())


def socket_path(override: str | os.PathLike | None = None) -> Path:
    """The socket to bind or unlink, resolved AT CALL TIME — `override`, else the configured override in
    the environment, else `SOCKET_PATH`.

    Call-time so a fixture setting the env var after import is honoured (else a "hermetic" suite talks to
    the live daemon). `agent_socket._socket_path`'s order exactly; `SOCKET_PATH` last so a monkeypatched
    attribute still wins when nothing is set."""
    return Path(override or environment.credential_socket_explicit() or SOCKET_PATH)


# For `start()`/`stop()`, whose `socket_path` parameter shadows the function.
_resolve_socket_path = socket_path

# The git names the `credential` shape serves — each resolved against the same two identities. Two names,
# ONE secret: `git_credential` is `github_token`'s token plus the login metadata (scheme, username, host,
# root) git's credential helper needs to answer in the forge's form and to refuse a foreign host. Both are
# projections of one cached login (`_project`), so neither widens what a caller can reach.
_GITHUB_TOKEN = "github_token"
_GIT_CREDENTIAL = "git_credential"
# The pinned `GitTokenOut` wire (`apps/api/app/routes/agent/task_secrets.py`), and the only fields `git_credential`
# serves: an extra field the CP adds later is dropped here until it is argued for, never passed through.
# `write_problem` is argued for: the CP's sentence naming why no credential of the starter's can write to the
# repository, which `jaah-forge` appends to a refused write so the runner asks for exactly that.
_GIT_LOGIN_FIELDS = (
    "scheme", "username", "token", "host", "root", "forge", "api_base", "repo_path", "web_url", "write_problem")
# The one home of the name's prefix (`apps/api/app/account_auth.py` borrows it from the same file by path).
_ACCOUNT_API_KEY_PREFIX = account_auth.NODE_CREDENTIAL_PREFIX

# Which route a caller reaches is decided by `_resolve` from peer ancestry alone; `{task_id}` comes from
# `runner_registry`, never the request.
_TASK_GIT_TOKEN_PATH = "/api/agent/tasks/{task_id}/git-token"
# The API-key ACCOUNT route: the CP authorizes on the account row (enabled, `auth_kind='api_key'`, a
# credential pointer), so the slug selects, never authorizes.
_ACCOUNT_API_KEY_PATH = "/api/agent/accounts/{slug}/api-key"
_CLONE_GIT_TOKEN_PATH = "/api/agent/git-token"
# K3, a Jaah session's short-lived Bedrock credentials (`app/jaah_routing/credentials.py`). The CP
# authorizes on the session row: this node, the runner's task (`X-Agent-Task-Id`), open, above the floor.
_BEDROCK_CREDENTIALS_PATH = "/api/agent/bedrock-credentials"
_BEDROCK_PREFIX = bedrock_launch.CREDENTIAL_PREFIX

# The working directory the asking process reported (`"checkout"` on a credential line, `_handle`), for
# `_caller_repo_id` alone. Carried beside the call rather than through `_resolve`'s arguments, whose shape is
# every caller's; empty when the caller sent none (an older client: the primary, as before).
_CALLER_CHECKOUT: contextvars.ContextVar[str] = contextvars.ContextVar("cred_broker_caller_checkout", default="")
_MAX_CHECKOUT_CHARS = 4096
# `"fresh": true` on a git credential line: the caller's forge just answered 401 to the login this broker
# served, so the cached entry is skipped and replaced by a CP read. GitHub revokes a user token the moment
# the CP refreshes it, so without this every node keeps serving the dead token for up to
# `_GIT_CREDENTIAL_TTL_S`. Git identities only: an account key or a Bedrock session ignores it.
_CALLER_FRESH: contextvars.ContextVar[bool] = contextvars.ContextVar("cred_broker_caller_fresh", default=False)

# The system-arm cache-key namespace (`system:<project>`): a readable sentinel, never "". A PREFIX, not the
# whole key — the answer depends on the project, so a bare sentinel would cross-serve declarers' tokens.
_SYSTEM_IDENTITY = "system"

# identity key -> (monotonic deadline, value), keyed by RESOLVED IDENTITY (module docstring). The value is
# the login dict for a git identity, the key string for an account. Monotonic so
# an NTP step or suspend cannot stretch or mass-expire entries. Module level so a rebuilt server keeps it.
_CACHE: dict[str, tuple[float, "str | dict[str, str]"]] = {}

# Must stay UNDER the CP's refresh margin (`apps/api/app/git_access.py::_REFRESH_MARGIN`, 10 min): a
# served token is guaranteed only that long, and the reply carries no expiry. 5 minutes keeps half the
# margin as headroom and collapses a clone's per-request credential-helper calls into one CP trip. Not mirrored as a
# constant — the CP owns the margin; if it shrinks, shrink this.
_GIT_CREDENTIAL_TTL_S = 300.0
# How long a disabled or rotated API-key account keeps being served. Far below the git TTL: the key is
# fleet-shared, non-expiring, billable and hand-rotated, so an off switch must bite fast; still a cache so
# a dispatch burst is not a CP burst.
_ACCOUNT_KEY_TTL_S = 30.0
# A Jaah session's credentials are served until this long before their own `Expiration`, never past it, so
# Claude Code is never handed a set about to lapse mid-call (and a revoked tenant's next ask meets the CP).
_BEDROCK_EXPIRY_MARGIN_S = 300.0

# Same 30s as an ordinary verb. It exceeds the client's 5s socket budget on purpose: the client gives up
# first and git fails fast rather than parking at a prompt; only the CP's detail is lost.
_GIT_TOKEN_TIMEOUT_S = 30.0

# Doubles as the StreamReader limit, so an over-long line raises rather than buffers. Sized for the `call`
# shape: `task.complete` carries up to 100 000 characters, and 256 KiB matches `agent_socket`'s reply cap.
_MAX_REQUEST_BYTES = 262144

# A leak guard for a client that connects and says nothing; real clients write immediately.
_READ_TIMEOUT = 5.0

# The error vocabulary, deliberately COARSE and not the CP's words:
#   bad_request  — not a JSON object naming a credential/verb this broker serves. Client bug.
#   unavailable  — no answer from the CP (unreachable, timeout, empty or non-JSON). Retry.
#   rejected     — the CP answered NO (HTTP error status). Fixable, never worth retrying as sent.
# `rejected` covers the credential shape too (the CP's actionable 4xx for a missing or unusable login). A
# new `_ERR_*` would be a word a model is handed unexplained — `tests/test_mcp_server.py` checks each
# against the plugin instructions.
# `rejected` carries the CP's status and detail (owner decision). ACCEPTED RESIDUAL: any socket
# peer can read it, and a validation error echoes the request back; bounded and one-line, and no request
# here sends a secret, so none can be echoed. The git-token 4xx names a PERSON, which is what makes it
# fixable.
_ERR_BAD_REQUEST = "bad_request"
_ERR_UNAVAILABLE = "unavailable"
_ERR_REJECTED = "rejected"

# The only binding bound on a `rejected` detail (the upstream body keeps 8 KiB for parsing).
_MAX_REJECTED_DETAIL = 300
# Over the cap: told to come back, never queued — queueing turns a flood into a wedge.
_ERR_BUSY = "busy"

# A NodeVault credential this node cannot open (`_open_nodevault`). Each carries a fixed, locally authored
# `detail` on the wire — the runner must learn WHY, and nothing in it comes from a caller or the CP.
_ERR_NODEVAULT_KEY_MISSING = "nodevault_key_missing"
_ERR_NODEVAULT_OPEN_FAILED = "nodevault_open_failed"
_NODEVAULT_KEY_MISSING_DETAIL = "This node doesn't hold the NodeVault key yet"
_NODEVAULT_OPEN_FAILED_DETAIL = "This node could not open the NodeVault credential"

# The `malformed_call` detail cap: larger because it carries OUR instruction to a model, which must read
# all of it; only the field path is caller-controlled (capped at its raise site).
_MAX_REFUSAL_DETAIL = 600

# Mirror of `MAX_ATTACHMENT_SIZE` (`apps/api/app/routes/attachments.py`), enforced against the caller's own
# descriptor before any transfer; the org's own, lower limit is the control plane's to refuse at the ticket.
# 5 GiB is safe ONLY because `_upload_file_body` streams off the fd (`session_sweep.put_object`); buffered,
# x `_MAX_INFLIGHT`, it would take the root daemon down.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024

# The multipart fallback's ceiling (taken only on a bucketless CP's 503): that door READS THE WHOLE FILE
# into this process, so it keeps the pre-ticket number.
_MAX_BUFFERED_UPLOAD_BYTES = 25 * 1024 * 1024

# The two `daemon_only` steps of a `file_body` verb, by name, so URLs stay in the vocabulary table.
_ARTIFACT_TICKET_VERB = "artifact.upload_begin"
_ARTIFACT_CONFIRM_VERB = "artifact.upload_commit"

# The ONE ticket status that means "no object storage" (`routes/attachments.py::_storage_or_503`), and
# the whole condition for the multipart door. Not any-5xx: a restarting CP must not silently downgrade a
# large artifact to a buffered send.
_NO_STORAGE_STATUS = 503

# `getattr`: POSIX-only. It matters here: the fd is held across a blocking read, and jaahd forks
# constantly — an inherited fd is a confined file handed to a process that never passed confinement.
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# Windows has neither: a final-component symlink there needs a privilege the runner account does not hold,
# and the fd's own resolved path is re-checked below either way. `O_BINARY` exists only there.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)

# A dedicated executor, not `to_thread`'s shared default (8 workers on 4 cores, also serving credential
# fetches), so a socket flood cannot starve git's credential helper or the daemon's own loops. The longest verb is
# `artifact.upload` (400s).
_MAX_INFLIGHT = 8
_MAX_INFLIGHT_PER_PEER = 2
# Accepted connections, separately: each holds a 256 KiB reader for up to `_READ_TIMEOUT`, so an unbounded
# accept lets a dribbling peer grow memory and fds at will.
_MAX_CONNECTIONS = 64
_OPEN_CONNECTIONS = 0
_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_MAX_INFLIGHT, thread_name_prefix="cred-broker-call")
_INFLIGHT_BY_PEER: dict[str, int] = {}


def _peer_key(uid: int, pid: int) -> str:
    """The principal the in-flight cap is charged to — NOT the raw pid.

    `fork()` gives fresh pids, so a pid-keyed cap never binds. A runner's PANE pid is shared by its
    descendants and unchoosable; a non-runner is charged as its uid."""
    identity = runner_registry.resolve_peer(pid)
    return f"pane:{identity.pane_pid}" if identity else f"uid:{uid}"


def _acquire_slot(key: str) -> bool:
    """Charge one in-flight CP call to `key`, or refuse. On the event loop with no `await` between read and
    write, so no lock."""
    if _INFLIGHT_BY_PEER.get(key, 0) >= _MAX_INFLIGHT_PER_PEER:
        return False
    if sum(_INFLIGHT_BY_PEER.values()) >= _MAX_INFLIGHT:
        return False
    _INFLIGHT_BY_PEER[key] = _INFLIGHT_BY_PEER.get(key, 0) + 1
    return True


def _release_slot(key: str) -> None:
    """Release one; popping at zero keeps the dict bounded by concurrent in-flight."""
    remaining = _INFLIGHT_BY_PEER.get(key, 1) - 1
    if remaining > 0:
        _INFLIGHT_BY_PEER[key] = remaining
    else:
        _INFLIGHT_BY_PEER.pop(key, None)


# The WAIT LANE: an ACP host's long polls (`LocalVerb.wait`, `acp/runtime.py`) are parked on the loop as
# futures for up to `runtime.MAX_WAIT_S`, never on `_CALL_EXECUTOR` nor charged to `_INFLIGHT_BY_PEER` — a node
# of parked hosts would otherwise hold every slot an ordinary verb needs. Their own caps: per principal (a
# host holds two polls; one spare for a reconnect overlap), and node-wide, scaled to the live runners.
_MAX_WAIT_PER_PEER = 3
_WAIT_RUNNER_FLOOR = 8
_WAIT_BY_PEER: dict[str, int] = {}


# The node cap's runner count, re-read at most every `_WAIT_CAP_TTL_S` (it is on the loop, per wait).
_WAIT_CAP_TTL_S = 5.0
_WAIT_CAP_CACHE: dict[str, float] = {}


def _wait_node_cap() -> int:
    now = time.monotonic()
    cached = _WAIT_CAP_CACHE.get("cap")
    if cached is not None and now - _WAIT_CAP_CACHE.get("at", 0.0) < _WAIT_CAP_TTL_S:
        return int(cached)
    try:
        live = len(runner_registry.live_runners())
    except Exception as exc:  # noqa: BLE001 — an unreadable registry falls back to the floor
        log.debug("cred-broker: live runner count unreadable (%s) — the wait cap uses its floor", exc)
        live = 0
    if host_platform.is_linux() and microvm_registry is not None:
        try:  # a MicroVM's host speaks over its VM listener, not a registered pane: best effort
            live += len(microvm_registry.load())
        except Exception as exc:  # noqa: BLE001
            log.debug("cred-broker: VM binding count unreadable (%s)", exc)
    cap = 3 * max(_WAIT_RUNNER_FLOOR, live)
    _WAIT_CAP_CACHE.update(cap=cap, at=now)
    return cap


def _host_fence(identity: runner_registry.RunnerIdentity | None, pid: int, vm: VmCaller | None,
                verb: str, capability: object = "") -> runner_registry.RunnerIdentity | None:
    """`identity` when the caller may speak a `runtime.*` verb: a pid caller only when it IS its pane's root
    process — the ACP host (D-HOST-1) — and still the process the registry recorded (start ticks; Windows
    re-maps by MSYS identity in `resolve_peer` itself); a VM caller only when its request carries the
    capability the VM's host claimed (`microvm_host.claim_host_capability`), which the host holds in its own
    non-dumpable memory, never in env or argv. Any descendant of the pane, or any other guest process (the
    agent, a tool it runs), is refused, so a model cannot forge the host's events or answers.
    `runtime.host_claim` itself is fenced in `_host_claim`."""
    if identity is None:
        return identity
    if vm is not None:
        binding = _vm_binding(vm)
        if binding is None or not microvm_registry.holds_host_capability(binding, capability):
            raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_FORBIDDEN_ARG,
                                               f"{verb}: only the VM's ACP host (its capability) may call it")
        return identity
    if identity.pane_pid != pid or (not host_platform.is_windows()
                                    and runner_registry._start_ticks(pid) != identity.pane_start_ticks):
        raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_FORBIDDEN_ARG,
                                           f"{verb}: only the run's ACP host (its pane's root) may call it")
    return identity


def _wait_row(payload: dict) -> agent_vocabulary.LocalVerb | None:
    """The wait-lane row `payload` names, or None — a malformed call is left to the ordinary arm, which
    refuses it with the same reason."""
    try:
        row = agent_vocabulary.resolve_local(payload.get("call"), payload.get("args"))
    except agent_vocabulary.VerbRefused:
        return None
    return row if row is not None and row.wait else None


async def _serve_wait(row: agent_vocabulary.LocalVerb, args: object, uid: int, pid: int, vm: VmCaller | None,
                      peer_key: str, capability: object = "") -> tuple[str, dict]:
    """One wait verb, awaited on the loop → `(outcome, reply)`. Identity is the peer's (`_identity_of`); the
    hub fences the args to that identity's session. Never logs the result (operator words)."""
    global _OPEN_CONNECTIONS
    # The fence first: a caller that is not a host is refused before it is charged to any cap.
    try:
        identity = _host_fence(_identity_of(uid, pid, vm), pid, vm, row.name, capability)
    except agent_vocabulary.VerbRefused as refused:
        log.warning("cred-broker: uid=%s pid=%s refused: %s", uid, pid, refused.detail)
        return refused.reason, {"ok": False, "error": refused.reason}
    except Exception as exc:  # noqa: BLE001 — an unreadable registry is no answer
        log.warning("cred-broker: uid=%s pid=%s call %s failed: %s", uid, pid, row.name, type(exc).__name__)
        return _ERR_UNAVAILABLE, {"ok": False, "error": _ERR_UNAVAILABLE}
    if identity is None:
        log.warning("cred-broker: uid=%s pid=%s refused: %s from no live runner", uid, pid, row.name)
        return agent_vocabulary.ERR_NOT_A_RUNNER, {"ok": False, "error": agent_vocabulary.ERR_NOT_A_RUNNER}
    if _WAIT_BY_PEER.get(peer_key, 0) >= _MAX_WAIT_PER_PEER or sum(_WAIT_BY_PEER.values()) >= _wait_node_cap():
        log.warning("cred-broker: uid=%s pid=%s over the wait cap (key=%s total=%s)",
                    uid, pid, peer_key, sum(_WAIT_BY_PEER.values()))
        return _ERR_BUSY, {"ok": False, "error": _ERR_BUSY}
    _WAIT_BY_PEER[peer_key] = _WAIT_BY_PEER.get(peer_key, 0) + 1
    # A parked poll has finished reading, so it does not count against `_MAX_CONNECTIONS` (the guard against
    # dribbling readers) — else parked hosts would lock every ordinary caller out at accept.
    _OPEN_CONNECTIONS -= 1
    try:
        from acp import runtime  # noqa: PLC0415 — only an ACP host's call needs it

        hub = runtime.hub()
        wait = hub.control_wait if row.name == "runtime.control_wait" else hub.request_wait
        result = await wait(identity, args if isinstance(args, dict) else {})
    except agent_vocabulary.VerbRefused as refused:
        log.warning("cred-broker: uid=%s pid=%s refused: %s", uid, pid, refused.detail)
        return refused.reason, {"ok": False, "error": refused.reason}
    except Exception as exc:  # noqa: BLE001 — no answer to read is one outcome
        log.warning("cred-broker: uid=%s pid=%s call %s failed: %s", uid, pid, row.name, type(exc).__name__)
        return _ERR_UNAVAILABLE, {"ok": False, "error": _ERR_UNAVAILABLE}
    finally:
        _OPEN_CONNECTIONS += 1
        remaining = _WAIT_BY_PEER.get(peer_key, 1) - 1
        if remaining > 0:
            _WAIT_BY_PEER[peer_key] = remaining
        else:
            _WAIT_BY_PEER.pop(peer_key, None)
    return "ok", {"ok": True, "result": result}


def _peer_identity(writer: asyncio.StreamWriter) -> tuple[int, int]:
    """The peer's (uid, pid) as the KERNEL reports them on the accepted socket — `host_platform.peer_of`
    (`SO_PEERCRED` on Linux; unforgeable, stamped at connect()).

    (-1, -1) if unreadable: forwarded as an unknown principal for the CP to judge — refusing would gain
    nothing, since the socket's reachability already gates it."""
    sock = writer.get_extra_info("socket")
    try:
        peer = host_platform.peer_of(sock)
    except (AttributeError, OSError) as exc:
        log.warning("cred-broker: could not read SO_PEERCRED (%s) — forwarding an unknown principal", exc)
        return -1, -1
    if host_platform.is_windows():
        # A Windows peer has no uid (its principal is a SID), so `Peer.uid` is -1 for every caller. The ONE
        # uid-shaped fact the broker needs is "is this the daemon itself" (`_resolve`'s system arm), so the
        # daemon's SID maps to `_daemon_uid()` and every other principal stays -1 — which is never it.
        if _daemon_peer(peer.principal):
            return _daemon_uid(), peer.pid
        # The pipe's DACL admits only SYSTEM, the daemon and the runner account, so today any other principal
        # is the runner. Checked anyway, so a widened DACL never gets a non-runner's process walked to a pane.
        if not _runner_peer(peer.principal):
            log.warning("cred-broker: pipe peer %s is not the runner account — forwarding an unknown principal",
                        peer.principal or "-")
            return -1, -1
        return -1, peer.pid
    return peer.uid, peer.pid


# Windows has no uids: the daemon's own stand-in, distinct from -1 ("unknown"/"not the daemon"). Never a real
# posix uid.
_WINDOWS_DAEMON_UID = -2


def _daemon_uid() -> int:
    """The uid `_peer_identity` reports for THIS daemon: `os.getuid()` on posix (read per call — the suite
    rebinds it), `_WINDOWS_DAEMON_UID` on Windows, where `os.getuid` does not exist."""
    return _WINDOWS_DAEMON_UID if host_platform.is_windows() else os.getuid()


def _runner_peer(principal: str) -> bool:
    """Windows: is `principal` the runner account's SID (`windows.runner_sid()`)? FAIL CLOSED on an
    unreadable SID."""
    if not principal or principal == "-1":
        return False
    from host_platform import windows
    try:
        return principal == windows.runner_sid()
    except OSError as exc:
        log.warning("cred-broker: cannot resolve the runner account's SID (%s) — no peer is a runner", exc)
        return False


def _daemon_peer(principal: str) -> bool:
    """Is the peer whose kernel-reported principal is `principal` this daemon? Windows: its token SID equals
    ours (`windows.current_sid()`); posix: its uid equals ours. FAIL CLOSED: an unreadable own SID, an empty
    or unknown (`"-1"`) principal is never the daemon."""
    if not principal or principal == "-1":
        return False
    if host_platform.is_windows():
        from host_platform import windows
        try:
            return principal == windows.current_sid()
        except OSError as exc:
            log.warning("cred-broker: cannot read this daemon's own SID (%s) — no peer is the daemon", exc)
            return False
    try:
        return int(principal) == os.getuid()
    except ValueError:
        return False


@dataclass(frozen=True)
class VmCaller:
    """A connection that arrived on MicroVM `vm_id`'s control listener (the MicroVM design's D13). The tag is
    the supervisor's, attached to the accepted fd it passed over its 0600 socket — the listener IS the
    identity, so a guest can name only itself."""

    vm_id: str


def _vm_binding(vm: VmCaller) -> VmBinding | None:
    return microvm_registry.load().get(vm.vm_id)


def _vms_root() -> Path:
    import env_helpers  # noqa: PLC0415 — stdlib-only leaf; lazy so the broker's import set is unchanged

    return env_helpers.daemon_state_dir("vms")


def _vm_identity(vm: VmCaller, binding: VmBinding | None = None) -> runner_registry.RunnerIdentity | None:
    """The task VM `vm.vm_id` runs, from this daemon's own registry, as a runner identity: its clone and
    report dir on THIS host, its runner session, its account. None for a VM bound to no task (a console VM,
    or one this node does not know)."""
    binding = binding or _vm_binding(vm)
    if binding is None or binding.kind != "task" or not binding.task_id.isdigit():
        return None
    vdir = _vms_root() / vm.vm_id
    slug = binding.account_slug
    return runner_registry.RunnerIdentity(
        task_id=binding.task_id, pane_pid=0, pane_start_ticks=0, worktree=str(vdir / "worktree"),
        session_id=binding.runner_session_id,
        account_dir=account_auth.api_key_config_dir(environment.pool_root_or_empty(), slug) if slug else "",
        api_key_account=binding.api_key_account, report_dir=str(vdir / "report"),
        # An ACP launch binds a host capability (`microvm_session.launch`): its run is ACP, so a launch that signs
        # in with its key keeps that key from every guest process but the host (`_signs_in_on_acp`).
        runtime="acp" if binding.host_capability else "")


def vm_runner_identity(binding: VmBinding) -> runner_registry.RunnerIdentity | None:
    """`_vm_identity` for a node-side sensor speaking as task VM `binding`'s runner (`call_as_runner`): the same
    identity the VM's own control calls carry, never a second derivation of it."""
    return _vm_identity(VmCaller(binding.vm_id), binding)


def _caller_identity(uid: int, pid: int) -> runner_registry.RunnerIdentity | None:
    """THE ONE ANSWER to "which task is this caller?" for a pid caller: `runner_registry.resolve_peer`'s
    ancestry walk. A VM caller's is `_vm_identity`, never this."""
    return runner_registry.resolve_peer(pid)


def _identity_of(uid: int, pid: int, vm: VmCaller | None) -> runner_registry.RunnerIdentity | None:
    return _caller_identity(uid, pid) if vm is None else _vm_identity(vm)


def _guest_path(named: str, identity: runner_registry.RunnerIdentity) -> str:
    """A path a guest named, on THIS host: under `/jaah/report` it is re-rooted onto the VM's own report dir;
    anything else is left as it is (relative joins the report dir, any other absolute path — the worktree
    included — is then refused as outside it)."""
    guest = microvm_seed.FIXED_MOUNTS["report"]
    if named == guest or named.startswith(guest + "/"):
        return identity.report_dir + named[len(guest):]
    return named


def _fetch_git_login(
    task_id: str, project: str, uid: int, pid: int, project_id: int | None = None, repo_id: int | None = None,
) -> dict[str, str]:
    """Ask the control plane for the git login THIS caller may use. Returns the wire object restricted to
    `_GIT_LOGIN_FIELDS`, each a str, or `{}` when the CP answered with no token.

    `task_id` (from `resolve_peer`, one frame up) already selects the route; `project` is the system arm's
    whole subject, read from `clone_identity` by `_resolve` so the CP query and the cache key are one read.
    `repo_id` names a non-primary repository on either arm: the task arm's from the caller's checkout
    (`_caller_repo_id`), the system arm's from the clone declaration; absent, the primary (today's request).
    `X-Agent-Task-Id` is overwritten on the task arm (see `_perform_call`); the system arm is genuinely the
    daemon's own request and keeps `host`. A 4xx PROPAGATES so `_handle` returns `rejected` with the CP's
    human-facing detail, never `unavailable`. The username comes from the CP (`git_access.git_login_for`
    owns the per-forge table), so no node end hard-codes it. Synchronous; run on `_CALL_EXECUTOR`."""
    headers = control_plane_api._agent_headers()
    if task_id:
        path = _TASK_GIT_TOKEN_PATH.format(task_id=task_id)
        headers["X-Agent-Task-Id"] = agent_vocabulary.caller_task_header(task_id)
        if repo_id is not None:
            path += f"?repo_id={int(repo_id)}"
    else:
        path = _CLONE_GIT_TOKEN_PATH
        # Quoted: `projects.name` is operator-typed and may carry a space, `&` or `#`. An EMPTY project
        # still calls, with no parameter, so the CP refuses with text naming the cause (a git op outside
        # `clone_identity.cloning`) — and no other project is ever substituted.
        # `project_id` (the identity, when the declarer knew it) rides beside the name; an older CP reads
        # only the name.
        if project:
            path += "?project=" + urllib.parse.quote(project, safe="")
            if project_id is not None:
                path += f"&project_id={int(project_id)}"
            # A non-primary repository of that project, from the roster's `repos[].id` the declarer
            # named, never a caller's argument. Absent for the primary, so the primary's request is unchanged.
            if repo_id is not None:
                path += f"&repo_id={int(repo_id)}"
    log.info("cred-broker: uid=%s pid=%s git credential -> GET %s", uid, pid, path)
    req = urllib.request.Request(
        f"{control_plane_api.resolve_control_plane_api_base()}{path}", headers=headers, method="GET")
    raw = control_plane_api._agent_urlopen(req, timeout=_GIT_TOKEN_TIMEOUT_S)
    # An empty or non-JSON 200 is a CP fault: `json.loads` raises → `unavailable`; no `token` → {}.
    body = json.loads(raw) if raw else {}
    if not isinstance(body, dict) or not body.get("token"):
        return {}
    # `or ""`: a null field (an older CP's absent metadata) reads as empty, never the string "None".
    return {field: str(body.get(field) or "") for field in _GIT_LOGIN_FIELDS}


# --- `node_token` is not served -----------------------------------------------------------------------
#
# DO NOT RE-ADD IT: every `jaah` process would hold the whole of `/api/agent/*`. The `call` shape replaced
# it. The generic `GET /api/agent/credential?name=…` client went too, so `_fetch_git_login` is
# the only thing the `credential` shape reaches for git.


def _fetch_account_api_key(slug: str, uid: int, pid: int) -> str:
    """Ask the control plane for ONE API-key account's model key (Anthropic, OpenAI, ...). Returns it, or "" when the CP
    answered with none.

    Not a name lookup: the slug selects an account and the CP authorizes on the row, answering an
    undisclosing 404 otherwise, so no other credential is reachable. No `X-Agent-Task-Id` — the account,
    not a task, is the subject. A 4xx propagates (`rejected`). The value is a LIVE KEY: never logged or
    interpolated; the log line carries the slug. Synchronous; run on `_CALL_EXECUTOR`."""
    path = _ACCOUNT_API_KEY_PATH.format(slug=urllib.parse.quote(slug, safe=""))
    log.info("cred-broker: uid=%s pid=%s account api-key -> GET %s", uid, pid, path)
    req = urllib.request.Request(
        f"{control_plane_api.resolve_control_plane_api_base()}{path}",
        headers=control_plane_api._agent_headers(), method="GET")
    raw = control_plane_api._agent_urlopen(req, timeout=_GIT_TOKEN_TIMEOUT_S)
    body = json.loads(raw) if raw else {}
    api_key = body.get("api_key") if isinstance(body, dict) else None
    return str(api_key) if api_key else ""


def _signs_in_on_acp(peer: runner_registry.RunnerIdentity) -> bool:
    """Whether `peer`'s run is an ACP session whose launch signs in with its account key (launch.json
    `authenticate`, `acp_runtime.register_session(key_sign_in=True)`). Reads the hub's session row: file I/O."""
    if peer.runtime != "acp" or not peer.session_id:
        return False
    from acp import runtime  # noqa: PLC0415 — only an ACP runner's key read needs it

    record = runtime.hub().store.get(peer.session_id)
    return bool(record and record.get("key_sign_in"))


def account_api_key(slug: str) -> str:
    """`_fetch_account_api_key` for this daemon itself: an adapter's pre-launch step that hands an API-key
    account's key to its CLI on stdin (`AcpLaunch.prepare`). "" when the CP has none; raises what the fetch
    raises. The value is a LIVE KEY: never logged."""
    return _fetch_account_api_key(slug, _daemon_uid(), os.getpid())


def _identity_key(
    task_id: str, project: str, run: str = "", project_id: int | None = None, repo_id: int | None = None,
) -> str:
    """The cache key for a resolved caller: its own task PLUS ITS RUN, else the system sentinel PLUS THE
    PROJECT.

    `task:`-namespaced so non-collision with `system` is this function's property, not `runner_registry`'s
    digit validation. The task key carries `run` (the runner's session) because the answer
    depends on who STARTED that run: a Stop/Start by another person re-dispatches the same task under a new
    session, and a task-only key would serve it the previous starter's login until the TTL ran out (Task
    5302). The system key carries the project for the same reason — by id when declared, since names repeat
    across orgs and one node may clone two same-named projects. `system:` with an empty project is a real key
    whose answer (a CP refusal) is never cached. A non-primary `repo_id` is its own key, on either arm: two
    repositories of one project may sit on two forges, with two logins."""
    if not task_id:
        key = f"{_SYSTEM_IDENTITY}:{project_id}" if project_id is not None else f"{_SYSTEM_IDENTITY}:name:{project}"
    else:
        key = f"task:{task_id}:{run}" if run else f"task:{task_id}"
    return f"{key}:repo:{repo_id}" if repo_id is not None else key


def _vm_repo_id(vm: VmCaller, checkout: str) -> int | None:
    """The repository a guest's git or `jaah-forge` call works in: `<rid>` when the checkout it reported lies
    under `/jaah/repos/<rid>`, None (the primary, at `/jaah/worktree`) for anything else. The id is the guest's
    LAYOUT, admitted only among the repositories this node cloned into the VM (`VmBinding.repo_ids`, written by
    the daemon at provision); any other id — a directory the guest made itself, a repository the project no
    longer connects — is refused, never served as the primary."""
    named = posixpath.normpath(checkout) if checkout.startswith("/") else ""
    root = microvm_seed.GUEST_REPOS
    if not named.startswith(root + "/"):
        return None
    first = named[len(root) + 1:].split("/", 1)[0]
    if not (first.isascii() and first.isdigit()):
        return None
    binding = _vm_binding(vm)
    if binding is None or int(first) not in binding.repo_ids:
        raise agent_vocabulary.VerbRefused(
            _ERR_BAD_REQUEST, f"{root}/{first} is not one of VM {vm.vm_id}'s repositories, so it has no login here")
    return int(first)


def _caller_repo_id(peer: runner_registry.RunnerIdentity | None, checkout: str) -> int | None:
    """The repository a runner's git or `jaah-forge` call is working in: the checkout holding the directory it
    reported (`checkout`, the request's `_CALLER_CHECKOUT`), judged only among that runner's OWN slot's checkouts by where the checkout
    sits (`slot_repos.checkout_repo_id`). None — the primary, as before — for a caller that reported nothing,
    the slot itself, or a directory outside its slot. A repository id
    the caller sends is never read: the checkout on disk is the only source."""
    if peer is None or not checkout:
        return None
    import slot_repos  # noqa: PLC0415 — lazy: the slot layout is needed only on this arm

    return slot_repos.checkout_repo_id(peer.worktree, checkout)


def _account_identity_key(slug: str) -> str:
    """The cache key for ONE account's API key.

    A separate `account:` namespace: a collision with `_identity_key` would serve a git token for an API
    key or vice versa. Keyed by account, since every runner on it is entitled to the same key."""
    return f"account:{slug}"


def _prune_expired(now: float) -> None:
    """Drop entries past their deadline. Keyed by identity the cache grows per task ever run, so this is
    what keeps a weeks-long process from holding an unbounded set of credentials."""
    for key in [key for key, (deadline, _token) in _CACHE.items() if deadline <= now]:
        _CACHE.pop(key, None)


def _project(name: str, cached: "str | dict[str, str]") -> "str | dict[str, str]":
    """What `name` is served from one cache entry: `github_token` the login's bare token, `git_credential`
    the login dict, an account key its string. An empty entry projects to ""/{} (→ `unavailable`).

    `github_token`'s one caller is the `gh` wrapper, which sends it to GitHub, so a login for any OTHER forge
    projects to "" — a GitLab or Azure DevOps token must never reach `api.github.com` (the host check the git
    helper makes on `git_credential` has no counterpart inside `gh`)."""
    if name == _GITHUB_TOKEN:
        if not isinstance(cached, dict) or cached.get("forge") != "github":
            return ""
        return cached.get("token", "")
    if name == _GIT_CREDENTIAL:
        return cached if isinstance(cached, dict) else {}
    return cached


async def _resolve(name: str, uid: int, pid: int, vm: VmCaller | None = None
                   ) -> tuple["str | dict[str, str]", bool, str]:
    """(value, cache_hit, identity key) for a credential ask, the value `_project`ed for `name`. It is
    empty when the CP answered with none; a CP REFUSAL propagates, so `_handle` can say `rejected` rather
    than `unavailable`.

    Identity comes from `pid` (SO_PEERCRED); `name` selects only the projection, never whose credential. Only fresh successes are hits. No lock
    around the double-check: two concurrent first-asks by one identity cost a duplicate GET, never another
    identity's answer — a lock would serialize every ask behind one network wait."""
    if vm is not None:
        return await _resolve_vm(name, uid, vm)
    if name.startswith(_BEDROCK_PREFIX):
        return await _resolve_bedrock(name, uid, pid)
    peer = runner_registry.resolve_peer(pid)
    return await _resolve_for(name, uid, pid, peer)


async def _resolve_vm(name: str, uid: int, vm: VmCaller) -> tuple["str | dict[str, str]", bool, str]:
    """`_resolve` for a guest: its VM's task, or — for a console VM — its account's key alone. No system arm,
    no daemon arm, no Jaah session: a VM is none of those."""
    if name.startswith(_BEDROCK_PREFIX):
        raise agent_vocabulary.VerbRefused(
            _ERR_BAD_REQUEST, f"VM {vm.vm_id} runs on no Jaah session and may not read one's credentials")
    peer = _vm_identity(vm)
    console_slug = ""
    if peer is None:
        binding = _vm_binding(vm)
        asked = account_auth.slug_from_node_credential_name(name)
        if binding is not None and binding.kind == "console" and asked and asked == binding.api_key_account:
            console_slug = asked
        else:
            raise agent_vocabulary.VerbRefused(
                _ERR_BAD_REQUEST, f"VM {vm.vm_id} is bound to no task here, so it has no credential to serve")
    vm_repo_id = (_vm_repo_id(vm, _CALLER_CHECKOUT.get())
                  if peer is not None and not account_auth.slug_from_node_credential_name(name) else None)
    return await _resolve_for(name, uid, -1, peer, console_slug=console_slug, vm_repo_id=vm_repo_id)


async def _resolve_for(name: str, uid: int, pid: int, peer: runner_registry.RunnerIdentity | None, *,
                       console_slug: str | None = None, vm_repo_id: int | None = None
                       ) -> tuple["str | dict[str, str]", bool, str]:
    """`_resolve`'s body once the caller's identity is known (`console_slug=None`: not yet decided, so a
    pid caller's console-key and system-arm proofs are asked here; a VM caller's repository is `vm_repo_id`,
    `_resolve_vm`'s)."""
    # A console tab on an API-key account is no runner, but the CP named its account when it opened the tab
    # (`runner_registry.record_console_key`). That proof admits ONE name — that account's key — and nothing
    # else: every other name from such a caller still meets the refusal below.
    vm_caller = console_slug is not None
    console_slug = console_slug or ""
    if peer is None and not vm_caller:
        asked = account_auth.slug_from_node_credential_name(name)
        granted = runner_registry.console_key_account(pid) if asked else ""
        console_slug = asked if granted and granted == asked else ""
    # ONE ancestry walk, used for both admission and the declaration served: a second walk could find the op
    # gone (git exited, the requester reparented) and fall through to another thread's live declaration.
    is_daemon = uid == _daemon_uid()
    delegated = clone_identity.delegated_declaration(pid)
    if peer is None and not console_slug and not vm_caller and not is_daemon and delegated is None:
        # The system arm needs a POSITIVE proof, not an absent pane: a runner that double-forks loses its
        # ancestry (it cannot forge it) and would otherwise be promoted to the slot-declarer's credential.
        # The uid is the kernel's; runners are `jaah`, the daemon `jaahd` (Windows: the
        # daemon's service SID, folded to `_daemon_uid()` by `_peer_identity`). The daemon's own
        # delegated git ops pass via `clone_identity.is_delegated_op` — a registry of daemon-started ops,
        # empty when none is in flight (see `clone_identity`). Not covered: a deployment running daemon and
        # runners under ONE uid, which this fleet does not. `not_a_runner`, as the control tools say it: the
        # git helper prints only this word, and `bad_request` hid the cause on a real PC.
        raise agent_vocabulary.VerbRefused(
            agent_vocabulary.ERR_NOT_A_RUNNER,
            f"pid {pid} is not a runner and is not this daemon, so it has no git identity to serve",
        )
    task_id = peer.task_id if peer else ""
    # The second name this shape serves: an account key is authorized by the CP on the ACCOUNT row, with
    # its own route, cache namespace and fetch — but it still borrows the identity proof above (only a live
    # runner or this daemon), so no `jaah` process can walk the roster. The CP also refuses a caller whose
    # credential names no node.
    account_slug = account_auth.slug_from_node_credential_name(name)
    if not account_slug and name.startswith(_ACCOUNT_API_KEY_PREFIX):
        # Prefixed but unparseable is a REFUSAL, never a fall-through: `""` would take the git branch and
        # hand this caller's GitHub token back as `ANTHROPIC_API_KEY`. Unreachable today (`_SLUG_RE`), refused
        # anyway — one credential served for another is the class this socket must never have.
        raise agent_vocabulary.VerbRefused(
            _ERR_BAD_REQUEST,
            f"{name!r} is not a well-formed account credential name",
        )
    if account_slug:
        # The slug is the caller's choice; this comparison makes it a proof. Without it any runner could
        # walk every slug and collect every live, billable key. The authority is the registry entry
        # `fleet_dispatch.launch_runner` writes at launch. The daemon (no peer) and a pre-upgrade entry
        # (`api_key_account=""`) are refused — the fail-closed direction.
        leased = peer.api_key_account if peer else console_slug
        if not leased or leased != account_slug:
            raise agent_vocabulary.VerbRefused(
                _ERR_BAD_REQUEST,
                f"pid {pid} was not leased account {account_slug!r} and may not read its key",
            )
        if peer is not None and peer.provider == runner_registry.SDK_PROVIDER:
            # An SDK agent's key (a registry agent's bound account) is the ACP host's to send in `authenticate`:
            # the pane's root reads it, never the agent or a tool it runs. No VM runs one: a guest is refused.
            if vm_caller:
                raise agent_vocabulary.VerbRefused(
                    _ERR_BAD_REQUEST, "an SDK agent's account key is not served inside a MicroVM")
            _host_fence(peer, pid, None, "the account key")
        elif peer is not None and await asyncio.get_running_loop().run_in_executor(
                _CALL_EXECUTOR, _signs_in_on_acp, peer):
            # Its ACP host reads the key once with `runtime.account_key`; no process of the run reads it by name —
            # else the agent, or a tool it runs, would collect the key the sign-in keeps out of its reach.
            raise agent_vocabulary.VerbRefused(
                _ERR_BAD_REQUEST, f"pid {pid}'s run signs in through its ACP host; its account key is not served here")
    # Read ONCE and used for both key and fetch: `clone_identity` is live state, and two reads could ask
    # about one project and cache under another. Empty outside a daemon git op's credential helper; the CP refuses it.
    loop = asyncio.get_running_loop()
    if not task_id:
        # Every uid, the daemon's own included, is served the declaration its op was registered under (none
        # for a console or VM caller, or a daemon-uid process outside `clone_identity.run_own`), NEVER the live
        # one: another thread's `cloning` may hold it for a different project, and an undeclared op — or a
        # filter driver planted in this op's tree — would be handed that project's token.
        project, project_id, repo_id = delegated or ("", None, None)
    else:
        # Off the loop: it reads session-user files, and a stalled read must stall one request, not the daemon.
        project, project_id = "", None
        repo_id = (None if account_slug else vm_repo_id if vm_caller
                   else await loop.run_in_executor(_CALL_EXECUTOR, _caller_repo_id, peer, _CALLER_CHECKOUT.get()))
    identity = (
        _account_identity_key(account_slug) if account_slug
        # The run's session (from launch). A task ask naming no
        # run is never cached (below): nothing would tell a restart apart from the run before it.
        else _identity_key(task_id, project, peer.session_id if peer else "", project_id, repo_id))
    cacheable = bool(account_slug) or not task_id or bool(peer and peer.session_id)
    now = time.monotonic()
    cached = _CACHE.get(identity)
    if cached and cached[0] > now and (account_slug or not _CALLER_FRESH.get()):
        return _project(name, cached[1]), True, identity
    if account_slug:
        value = await loop.run_in_executor(_CALL_EXECUTOR, _fetch_account_api_key, account_slug, uid, pid)
    else:
        value = await loop.run_in_executor(
            _CALL_EXECUTOR, _fetch_git_login, task_id, project, uid, pid, project_id, repo_id)
    if value and cacheable:
        _prune_expired(now)
        # The account-key TTL is how long an operator's `enabled` off switch keeps being bypassed; a
        # non-expiring, billable, shared key gets a much shorter window than a self-expiring git token.
        _CACHE[identity] = (
            now + (_ACCOUNT_KEY_TTL_S if account_slug else _GIT_CREDENTIAL_TTL_S), value)
    return _project(name, value), False, identity


async def _resolve_bedrock(name: str, uid: int, pid: int) -> tuple[str, bool, str]:
    """`_resolve` for `bedrock_session:<uuid>`: the K3 answer as the JSON text Claude Code's
    `awsCredentialExport` reads, for the ONE caller entitled to it — a runner (or console tab) this node
    launched on that very session (`runner_registry`, written at launch). Everyone else, the daemon
    included, is refused: no other process on the node may mint a tenant's
    Bedrock session.

    Cached per session until `_BEDROCK_EXPIRY_MARGIN_S` before the credentials' own `Expiration`. The value
    is a SECRET: never logged, never interpolated — the log line carries the session."""
    try:
        session_id = str(uuid.UUID(name[len(_BEDROCK_PREFIX):]))
    except ValueError:
        raise agent_vocabulary.VerbRefused(
            _ERR_BAD_REQUEST, f"{name!r} is not a well-formed Jaah session credential name") from None
    peer = runner_registry.resolve_peer(pid)
    granted = peer.bedrock_session if peer else runner_registry.console_bedrock_session(pid)
    if not granted or granted != session_id:
        raise agent_vocabulary.VerbRefused(
            _ERR_BAD_REQUEST, f"pid {pid} was not launched on Jaah session {session_id} and may not read its "
                              f"credentials")
    identity = f"bedrock:{session_id}"
    now = time.monotonic()
    cached = _CACHE.get(identity)
    if cached and cached[0] > now:
        return str(cached[1]), True, identity
    value, lifetime = await asyncio.get_running_loop().run_in_executor(
        _CALL_EXECUTOR, _fetch_bedrock_credentials, session_id, peer.task_id if peer else "", uid, pid)
    if value and lifetime > _BEDROCK_EXPIRY_MARGIN_S:
        _prune_expired(now)
        _CACHE[identity] = (now + lifetime - _BEDROCK_EXPIRY_MARGIN_S, value)
    return value, False, identity


def forget_bedrock_session(session_id: str) -> None:
    """Drop one Jaah session's cached credentials (`bedrock_close`, K10): a closed session's next ask goes
    to the CP, which refuses it."""
    _CACHE.pop(f"bedrock:{session_id}", None)


def _fetch_bedrock_credentials(session_id: str, task_id: str, uid: int, pid: int) -> tuple[str, float]:
    """`(the K3 body as JSON text, seconds until its Expiration)`, or `("", 0)` for a 200 with no usable
    credentials. A runner's ask names its task (`X-Agent-Task-Id`, from the registry, never the request); a
    console tab's names none. A 403 (`balance_floor`, `unavailable`, `not_jaah`, `unknown_session`)
    propagates as `rejected`. Synchronous; run on `_CALL_EXECUTOR`."""
    query = urllib.parse.urlencode({"name": f"{_BEDROCK_PREFIX}{session_id}"})
    log.info("cred-broker: uid=%s pid=%s Jaah session %s -> GET %s", uid, pid, session_id,
             _BEDROCK_CREDENTIALS_PATH)
    headers = {**control_plane_api._agent_headers(), "X-Agent-Task-Id": task_id}
    req = urllib.request.Request(
        f"{control_plane_api.resolve_control_plane_api_base()}{_BEDROCK_CREDENTIALS_PATH}?{query}",
        headers=headers, method="GET")
    raw = control_plane_api._agent_urlopen(req, timeout=_GIT_TOKEN_TIMEOUT_S)
    body = json.loads(raw) if raw else {}
    creds = body.get("Credentials") if isinstance(body, dict) else None
    fields = ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")
    if not isinstance(creds, dict) or not all(isinstance(creds.get(f), str) and creds.get(f) for f in fields):
        return "", 0.0
    try:
        expires = datetime.fromisoformat(creds["Expiration"].replace("Z", "+00:00"))
        lifetime = (expires - datetime.now(timezone.utc)).total_seconds() if expires.tzinfo else 0.0
    except ValueError:
        lifetime = 0.0
    # Only the four fields Claude Code reads, re-serialized: nothing else the CP might add travels on.
    return json.dumps({"Credentials": {f: creds[f] for f in fields}}), lifetime


def _is_inside(root_real: str, candidate: str) -> bool:
    """Is `candidate` AT OR under `root_real`? Both must already be absolute and resolved.

    The root itself passes (refusing a directory is `S_ISREG`'s job). `commonpath`, NOT `startswith`:
    `task-12-evil` starts with `task-12`. ValueError (mixed abs/rel) is refused."""
    try:
        return os.path.commonpath([root_real, candidate]) == root_real
    except ValueError:
        return False


def _refuse_upload(reason: str, detail: str) -> agent_vocabulary.VerbRefused:
    """One constructor for every refusal below: `detail` (with the path) goes to the journal, `reason` to
    the wire — echoing a path would answer "does `/etc/jaah/node.env` exist?" to any `jaah` process."""
    return agent_vocabulary.VerbRefused(reason, detail)


def _fd_path(fd: int) -> str:
    """What the KERNEL says this descriptor actually opened — `host_platform.fd_path` (procfs on Linux, so a
    rename or symlink of the original name changes nothing). Raises `OSError` where the kernel cannot be
    asked; the caller denies on that."""
    return host_platform.fd_path(fd)


def _refuse_unless_runner_owned(fd: int, pid: int, candidate: str) -> None:
    """Windows' half of the upload's ownership rule: the file open on `fd` must be OWNED by the runner
    account `jaah`. Fails closed when either SID cannot be read."""
    from host_platform import windows  # noqa: PLC0415 — Windows-only seam names

    try:
        owner, runner = windows.file_owner_sid(fd), windows.runner_sid()
    except OSError as exc:
        raise _refuse_upload(agent_vocabulary.ERR_FORBIDDEN_ARG,
                             f"pid {pid}: cannot confirm who owns {candidate} ({exc})") from None
    if owner != runner:
        raise _refuse_upload(
            agent_vocabulary.ERR_FORBIDDEN_ARG,
            f"pid {pid}: {candidate} is owned by {owner}, not the runner account — a report must be a "
            f"file the caller itself wrote")


def _open_confined_upload(named: str, pid: int, uid: int, vm: VmCaller | None = None
                          ) -> tuple[str, int, int]:
    """The path, an OPEN DESCRIPTOR and the size of the ONE file this peer may upload, or `VerbRefused`.
    **The caller owns the descriptor and must `os.close` it.**

    THE SECURITY CORE of every `file_body` verb. Every check is on the descriptor, so what is
    sent is the inode judged here; re-opening by name would reopen the swap race. Sound because the inode
    must be one the caller OWNS (the root daemon reads nothing the caller could not); confinement to the
    runner's own staging dir is the second, weaker property, needing both the containment checks and the
    single-link rule. The root is the runner's recorded report dir (`fleet_dispatch.ensure_report_dir`: never
    in `git status`), and nothing else — no recorded dir refuses. A VM caller names a GUEST path (`_guest_path`), and its files are
    owned by the daemon uid its guest uid maps onto, which is the uid it is judged against."""
    identity = _identity_of(uid, pid, vm)
    if vm is not None:
        uid = _daemon_uid()
        if identity is not None:
            named = _guest_path(named, identity)
    root = identity.report_dir if identity else ""
    if not root:
        # Absent must deny: an empty root would confine to "/" for a root reader.
        raise _refuse_upload(agent_vocabulary.ERR_FORBIDDEN_ARG,
                             f"pid {pid}: no report directory recorded for this caller, so no path is "
                             f"confinable")
    root_real = os.path.realpath(root)
    # The parent is resolved and the final component is NOT, so `O_NOFOLLOW` still judges it; `..` and a
    # symlinked directory cannot fool containment. An absolute `named` is allowed and checked the same.
    joined = os.path.join(root_real, named)
    base = os.path.basename(joined)
    if base in ("", ".", ".."):
        raise _refuse_upload(_ERR_BAD_REQUEST, f"pid {pid}: {named!r} names no file")
    candidate = os.path.join(os.path.realpath(os.path.dirname(joined)), base)
    if not _is_inside(root_real, candidate):
        raise _refuse_upload(
            agent_vocabulary.ERR_FORBIDDEN_ARG,
            f"pid {pid}: {named!r} resolves to {candidate}, outside this runner's report directory {root_real}")

    # A name is not an inode: the peer owns this directory and can swap the name after the checks above.
    # From here every check is on the OPEN DESCRIPTOR. `O_NONBLOCK` is for fifos, which would block the
    # open and, on an 8-worker pool, wedge every local caller.
    try:
        fd = os.open(candidate, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC | _O_BINARY)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # `O_NOFOLLOW` fired: a policy refusal, whatever the link points at.
            raise _refuse_upload(agent_vocabulary.ERR_FORBIDDEN_ARG,
                                 f"pid {pid}: {candidate} is a symlink; a report must be a real file") \
                from None
        raise _refuse_upload(_ERR_BAD_REQUEST, f"pid {pid}: cannot open {candidate}: {exc}") from None
    try:
        st = os.fstat(fd)
        # `fstat`, never a path stat, which could describe a different inode.
        if not stat.S_ISREG(st.st_mode):
            raise _refuse_upload(agent_vocabulary.ERR_FORBIDDEN_ARG,
                                 f"pid {pid}: {candidate} is not a regular file (mode {st.st_mode:#o})")
        # The inode must belong to the caller (`uid` is SO_PEERCRED): closes a cross-owner hard link
        # (`ln /etc/jaah/node.env $WT/report.html`), which `O_NOFOLLOW` admits. `fs.protected_hardlinks`
        # is a kernel default this repo does not set, so it is asserted here.
        if host_platform.is_windows():
            # `st_uid` is 0 for every file there: the owner is the file's owner SID, and the only owner a
            # runner can be is the runner account.
            _refuse_unless_runner_owned(fd, pid, candidate)
        elif st.st_uid != uid:
            raise _refuse_upload(
                agent_vocabulary.ERR_FORBIDDEN_ARG,
                f"pid {pid}: {candidate} is owned by uid {st.st_uid}, not the calling uid {uid} — a "
                f"report must be a file the caller itself wrote")
        # Exactly one link: a same-uid hard link to a SIBLING runner's file would upload under this task.
        # Buys confinement and attribution, not anti-exfiltration (the caller could inline the bytes).
        if st.st_nlink != 1:
            raise _refuse_upload(
                agent_vocabulary.ERR_FORBIDDEN_ARG,
                f"pid {pid}: {candidate} has {st.st_nlink} links, so the name is not the inode's only "
                f"one — a report must be a file this runner wrote inside its own report directory")
        # Containment re-asserted from the fd: `O_NOFOLLOW` guards only the final component, not a parent.
        try:
            opened = _fd_path(fd)
        except OSError as exc:
            # Fail closed: an unanswerable containment question is a denial.
            raise _refuse_upload(agent_vocabulary.ERR_FORBIDDEN_ARG,
                                 f"pid {pid}: cannot confirm what fd {fd} opened ({exc})") from None
        if not _is_inside(root_real, opened):
            raise _refuse_upload(
                agent_vocabulary.ERR_FORBIDDEN_ARG,
                f"pid {pid}: {named!r} opened {opened}, outside this runner's report directory {root_real}")
        # Size from `fstat` — the inode about to be read; the CP's refusal would arrive only after the
        # whole transfer.
        if st.st_size > _MAX_UPLOAD_BYTES:
            raise _refuse_upload(
                agent_vocabulary.ERR_FORBIDDEN_ARG,
                f"pid {pid}: {opened} is {st.st_size} bytes, over the {_MAX_UPLOAD_BYTES} byte cap")
    except BaseException:
        # The fd is the caller's only on success; a leaked fd per refusal would let a peer exhaust the
        # daemon. `BaseException`: cancellation leaks just as permanently.
        os.close(fd)
        raise
    return candidate, fd, st.st_size


def _read_confined_upload(named: str, pid: int, uid: int, vm: VmCaller | None = None) -> tuple[str, bytes]:
    """The path and BYTES of the one file this peer may upload — `_open_confined_upload` plus the read.

    The buffered door, used only by the multipart fallback, so capped at
    `min(_MAX_UPLOAD_BYTES, _MAX_BUFFERED_UPLOAD_BYTES)` — the min, so lowering the CP mirror lowers this."""
    candidate, fd, size = _open_confined_upload(named, pid, uid, vm)
    cap = min(_MAX_UPLOAD_BYTES, _MAX_BUFFERED_UPLOAD_BYTES)
    try:
        if size > cap:
            raise _refuse_upload(
                agent_vocabulary.ERR_FORBIDDEN_ARG,
                f"pid {pid}: {candidate} is {size} bytes, over the {cap} byte cap this control plane's "
                f"buffered upload route may be handed — it has no object storage to stream to")
        with open(fd, "rb", closefd=False) as fh:
            blob = fh.read(cap + 1)
    finally:
        os.close(fd)
    if len(blob) > cap:
        # Grown since the `fstat`: bound on what was ACTUALLY read.
        raise _refuse_upload(agent_vocabulary.ERR_FORBIDDEN_ARG,
                             f"pid {pid}: {candidate} grew past the {cap} byte cap")
    return candidate, blob


def _safe_upload_name(filename: str) -> str:
    """The basename the control plane is told, whichever door the bytes take.

    Basename minus CR/LF/quote/backslash: a HEADER-INJECTION guard on the multipart door, where the name is
    interpolated raw into a header of a request signed with the node's credential. Identical on the ticket
    door so both declare the same name. The fallback `upload.bin` claims no format, because the CP derives
    a `file`'s media type from the name."""
    return "".join(c for c in os.path.basename(filename) if c not in '"\\\r\n') or "upload.bin"


def _multipart_body(
    filename: str, blob: bytes, fields: "dict[str, str] | None" = None
) -> tuple[bytes, str]:
    """Encode ONE file, plus any declared form `fields`, as `multipart/form-data`, returning
    (body, Content-Type). Hand-rolled: this daemon is stdlib-only.

    `fields` (`Verb.form_fields`) go FIRST, each its own part, so the CP reads them before the payload."""
    # 128 random bits: a boundary collision needs 2^128 guesses and the peer never sees it.
    boundary = secrets.token_hex(16)
    # Sanitised in ONE place, shared with the ticket door — see `_safe_upload_name` for both rules.
    safe = _safe_upload_name(filename)
    # Field names come from the verb table but reach a header line, so they are sanitised like the
    # filename. Values are part BODIES and cannot start a header.
    field_parts: list[bytes] = []
    for name, value in (fields or {}).items():
        safe_name = "".join(c for c in name if c not in '"\\\r\n')
        field_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    # Guessed from the name (markdown is not `text/html`), and sanitised: `guess_type` echoes part of a
    # `data:` URL input back, so its output is caller-derived header text.
    guessed = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    guessed = "".join(c for c in guessed if c not in '"\\\r\n') or "application/octet-stream"
    head = (
        f"--{boundary}\r\n"
        # `name="file"` is the contract: the CP route's `File(...)` parameter
        # (`apps/api/app/routes/attachments.py::agent_upload_artifact`); anything else is a 422.
        f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
        # Cosmetic: the route forces the stored `content_type` from the kind
        # (`app/task_manifest.py::KIND_MEDIA`).
        f"Content-Type: {guessed}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    # One allocation, not `head + blob + tail` (two more payload copies): with `_MAX_INFLIGHT` 8, this
    # process's peak memory is a correctness concern — its death takes the node's fleet with it.
    return b"".join((*field_parts, head, blob, tail)), f"multipart/form-data; boundary={boundary}"


def _send_to_cp(verb: agent_vocabulary.Verb, url: str, headers: dict, data: "bytes | None") -> object:
    """One request to the control plane, and its parsed answer. The single seam every verb leaves by, so
    every call gets the same credentialed opener, timeout and empty/unparseable-body handling."""
    req = urllib.request.Request(url, data=data, headers=headers, method=verb.method)
    raw = control_plane_api._agent_urlopen(req, timeout=verb.timeout_s)
    if not raw:
        # An empty body is a real success for several verbs (`/telemetry` is 204). None, not {}.
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # Non-JSON is a CP fault the peer cannot act on → `unavailable`.
        log.warning("cred-broker: %s returned a non-JSON body", verb.name)
        raise


def _daemon_cp_call(verb_name: str, task_id: str, body: dict, **path_params: object) -> object:
    """Make a `daemon_only` verb's call on the peer's behalf. NOT a door a peer can reach.

    The URL comes off the vocabulary table. `task_id` is the peer-derived one; every `path_params` value
    comes from an answer this daemon just read, never from a caller."""
    verb = agent_vocabulary.BY_NAME[verb_name]
    path = verb.path.format(task_id=task_id, **path_params)
    headers = control_plane_api._agent_headers()
    headers["X-Agent-Task-Id"] = agent_vocabulary.caller_task_header(task_id)
    log.info("cred-broker: call=%s task=%s -> %s %s", verb.name,
             agent_vocabulary.caller_task_header(task_id), verb.method, path)
    return _send_to_cp(
        verb, f"{control_plane_api.resolve_control_plane_api_base()}{path}", headers,
        json.dumps(body, default=control_plane_api._json_default).encode(),
    )


def _mint_upload_ticket(
    candidate: str, size: int, fields: dict, task_id: str
) -> "tuple[int, str] | None":
    """`(attachment_id, upload_url)` for one artifact, or None when this control plane has no bucket.

    The declared size lets the route refuse oversize, a bad `kind` or extension BEFORE minting anything;
    the CP replaces the claim at `/confirm` with what S3 received (`/confirm`, not `/complete` — see
    `confirm_attachment` in `apps/api/app/routes/attachments.py` for the authz predicate). None only on the
    503 (`_storage_or_503`, a shipped state); every other status propagates. But a 503 may be an ALB blip:
    above the buffered cap it RAISES (`unavailable`, retryable) rather than reach `_read_confined_upload`'s
    `forbidden_arg` ("fix your request") and destroy a large report over a transient."""
    body = dict(fields)
    body["filename"] = _safe_upload_name(candidate)
    body["size_bytes"] = size
    try:
        answer = _daemon_cp_call(_ARTIFACT_TICKET_VERB, task_id, body)
    except urllib.error.HTTPError as exc:
        if exc.code != _NO_STORAGE_STATUS:
            raise
        if size > _MAX_BUFFERED_UPLOAD_BYTES:
            # Re-raised → `unavailable`, so the runner retries (see the docstring).
            log.warning("cred-broker: task %s — the ticket route answered HTTP %s and this artifact is "
                        "%s bytes, over the %s byte buffered fallback; treating it as retryable rather "
                        "than downgrading", task_id, exc.code, size, _MAX_BUFFERED_UPLOAD_BYTES)
            raise
        log.info("cred-broker: task %s — the control plane has no object storage (HTTP %s), so the "
                 "artifact takes the buffered multipart route", task_id, exc.code)
        return None
    # Checked, not assumed: one value becomes a request path, the other a URL, so a malformed answer fails
    # here as `unavailable`. `bool` excluded (`True` is an int). `<= 0`: the CP's `object_key` refuses a
    # non-positive id and a sequence never issues 0.
    attachment_id = answer.get("attachment_id") if isinstance(answer, dict) else None
    upload_url = answer.get("upload_url") if isinstance(answer, dict) else None
    if isinstance(attachment_id, bool) or not isinstance(attachment_id, int) or attachment_id <= 0:
        raise RuntimeError(
            f"{_ARTIFACT_TICKET_VERB}: the control plane answered with attachment_id "
            f"{attachment_id!r}, which is not a row id")
    if not isinstance(upload_url, str) or not upload_url:
        # Only its absence is named: a presigned URL is a bearer token, and this reaches the journal.
        raise RuntimeError(f"{_ARTIFACT_TICKET_VERB}: the control plane answered with no upload url")
    return attachment_id, upload_url


def _upload_file_body(
    verb: agent_vocabulary.Verb, path: str, headers: dict, named: str, fields: dict,
    task_id: str, pid: int, uid: int, vm: VmCaller | None = None,
) -> object:
    """Carry ONE `file_body` verb's file to the control plane and return the row it stored.

    Ticket, then stream the confined DESCRIPTOR straight to S3, then confirm — the bytes never sit in this
    process. `session_sweep.put_object` is reused for its non-obvious correctness (no forced `Content-Type`,
    an explicit `Content-Length` since chunked is refused, no redirect following, never quoting the URL).
    The fallback (ticket 503 only) re-enters `_read_confined_upload` BY NAME, re-running the whole guard,
    after the first descriptor is closed."""
    candidate, fd, size = _open_confined_upload(named, pid, uid, vm)
    try:
        ticket = _mint_upload_ticket(candidate, size, fields, task_id)
        if ticket is not None:
            attachment_id, upload_url = ticket
            # The descriptor itself (the uploader dups it, so the proofs travel with it) and `size` from the
            # same `fstat` the ticket was signed against; `session_sweep._ExactlyNBytes` enforces it.
            try:
                session_sweep.put_object(upload_url, fd, size=size)
            except session_sweep.UploadFailed as exc:
                # `bad_request`, not `unavailable`: `UploadFailed` here means the runner changed its file's
                # size mid-send, which no retry fixes (and each retry leaves a pending row).
                raise _refuse_upload(
                    _ERR_BAD_REQUEST,
                    f"pid {pid}: {verb.name} could not stream {candidate}: {exc}") from None
            log.info("cred-broker: uid=%s pid=%s streamed %s bytes of %s to attachment %s of task %s",
                     uid, pid, size, verb.name, attachment_id, task_id)
            return _daemon_cp_call(
                _ARTIFACT_CONFIRM_VERB, task_id, {}, attachment_id=attachment_id)
    finally:
        os.close(fd)
    confined, blob = _read_confined_upload(named, pid, uid, vm)
    data, content_type = _multipart_body(confined, blob, fields)
    # Overwritten: `_agent_headers()` says `application/json`, which FastAPI's `File(...)` 422s.
    headers["Content-Type"] = content_type
    log.info("cred-broker: uid=%s pid=%s call=%s task=%s -> %s %s (buffered fallback)",
             uid, pid, verb.name, agent_vocabulary.caller_task_header(task_id), verb.method, path)
    return _send_to_cp(verb, f"{control_plane_api.resolve_control_plane_api_base()}{path}", headers, data)


# The verb a `Verb.attachment_paths` `{path}` entry is uploaded through, and the kind it lands as: the
# caller's own free-form deliverable, which then travels to the filed task as an `{artifact_id}`.
_ATTACHMENT_UPLOAD_VERB = "artifact.upload"
_ATTACHMENT_UPLOAD_KIND = "file"


def _upload_attachment_paths(entries: list, task_id: str, pid: int, uid: int, vm: VmCaller | None = None
                             ) -> list:
    """Turn each `{path}` entry into the `{artifact_id}` of the caller's own `file` artifact it uploads as,
    leaving `{artifact_id}` entries as they are (`agent_vocabulary._check_attachments` shape-checked both).

    Each upload is `artifact.upload`'s own walk — `_upload_file_body`, so `_read_confined_upload`'s whole
    guard judges the path exactly as it judges an `artifact_upload` call. Sequential, and a refusal stops the
    call before the CP sees it; files already uploaded stay as the caller's artifacts (nothing was filed,
    and a retry re-uploads rather than double-files, since the filing is what dedups)."""
    verb = agent_vocabulary.BY_NAME[_ATTACHMENT_UPLOAD_VERB]
    path = verb.path.replace("{task_id}", task_id)
    resolved = []
    for entry in entries:
        if "path" not in entry:
            resolved.append(entry)
            continue
        headers = control_plane_api._agent_headers()
        headers["X-Agent-Task-Id"] = agent_vocabulary.caller_task_header(task_id)
        stored = _upload_file_body(
            verb, path, headers, str(entry["path"]), {"kind": _ATTACHMENT_UPLOAD_KIND}, task_id, pid, uid, vm)
        artifact_id = stored.get("id") if isinstance(stored, dict) else None
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int):
            raise agent_vocabulary.VerbRefused(
                _ERR_UNAVAILABLE, f"pid {pid}: an attachment upload answered no artifact id ({stored!r})")
        resolved.append({"artifact_id": artifact_id})
    return resolved


def _host_claim(identity: runner_registry.RunnerIdentity | None, args: dict, pid: int,
                vm: VmCaller | None) -> dict:
    """`runtime.host_claim`: the caller's host capability. A pid caller passes the pid fence and needs none (""):
    on a static node the pid IS the proof. A VM caller naming its own session is minted the VM's capability once
    per launch (`microvm_host.claim_host_capability`); a second claim, or a VM no ACP launch bound, is refused."""
    if identity is None:
        raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_NOT_A_RUNNER,
                                           "runtime.host_claim: the caller is not a live runner")
    if args.get("session_id") != identity.session_id or not identity.session_id:
        raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_FORBIDDEN_ARG,
                                           "runtime.host_claim: a caller may name only its own session")
    if vm is None:
        _host_fence(identity, pid, None, "runtime.host_claim")
        return {"capability": ""}
    microvm_host = cloud_modules.load("microvm_host")  # a MicroVM node's module; only a VM's claim needs it

    capability = microvm_host.claim_host_capability(vm.vm_id)
    if not capability:
        # After the host's own claim, a second one is a guest process trying to take the host's place.
        log.warning("cred-broker: VM %s claimed a host capability it has none left of", vm.vm_id)
        raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_FORBIDDEN_ARG,
                                           f"runtime.host_claim: VM {vm.vm_id} has no capability left to claim")
    return {"capability": capability}


def _perform_call(verb_name: object, args: object, uid: int, pid: int, vm: VmCaller | None = None,
                  capability: object = "") -> object:
    """Run ONE vocabulary verb on the peer's behalf and return the control plane's parsed result.

    The task identity is DERIVED (`_caller_identity`), never accepted; policy is `agent_vocabulary`'s.
    `X-Agent-Task-Id` is overwritten: `_agent_headers()` declares the daemon's `host` sentinel, which on a
    peer's request would pass the CP's task-creating guard. Raises `VerbRefused`; a CP
    transport failure propagates (`unavailable`). Synchronous; run in a thread."""
    identity = _identity_of(uid, pid, vm)
    local = agent_vocabulary.resolve_local(verb_name, args)
    if local is not None:
        log.info("cred-broker: uid=%s pid=%s local call=%s", uid, pid, local.name)
        if local.name == "runtime.host_claim":
            return _host_claim(identity, args if isinstance(args, dict) else {}, pid, vm)
        if local.name.startswith("runtime."):
            identity = _host_fence(identity, pid, vm, local.name, capability)
        return _answer_local(local.name, args if isinstance(args, dict) else {}, identity)
    if verb_name in (_FORGE_AUTH_REJECTED, _FORGE_AUTH_ACCEPTED):
        return _forge_auth_report(verb_name, args, identity, uid, pid, vm)
    return _call_as(verb_name, args, identity, uid, pid, vm)


# The forge's verdict on a served login (`git_credential_helper.report_forge_auth`). git says `store` after
# every command the forge took, so `accepted` reaches the control plane only to close a refusal this daemon
# forwarded for the same task and host: `(task_id, host)` pairs, cleared by the forwarded `accepted`.
_FORGE_AUTH_REJECTED = "forge_auth.rejected"
_FORGE_AUTH_ACCEPTED = "forge_auth.accepted"
_UNRESOLVED_FORGE_REJECTIONS: set[tuple[str, str]] = set()


def _forge_auth_report(verb_name: str, args: object, identity: runner_registry.RunnerIdentity | None, uid: int,
                       pid: int, vm: VmCaller | None) -> object:
    """Forward a `forge_auth.*` report as `_call_as` does, dropping an `accepted` no refusal is open for."""
    host = str(args.get("host") or "").lower() if isinstance(args, dict) else ""
    key = (identity.task_id if identity else "", host)
    if verb_name == _FORGE_AUTH_ACCEPTED and key not in _UNRESOLVED_FORGE_REJECTIONS:
        return {"skipped": "no refusal open for this task and host"}
    result = _call_as(verb_name, args, identity, uid, pid, vm)
    if verb_name == _FORGE_AUTH_REJECTED:
        _UNRESOLVED_FORGE_REJECTIONS.add(key)
    else:
        _UNRESOLVED_FORGE_REJECTIONS.discard(key)
    return result


def _answer_local(name: str, args: dict | None = None,
                  identity: runner_registry.RunnerIdentity | None = None) -> dict:
    """A daemon-local verb's answer (`agent_vocabulary.LOCAL_VERBS`): public NodeVault facts from this
    daemon's own keystore — no CP call, and no key bytes, a fingerprint and key hashes only — the guard's
    forced bundle refetch (`guard_bundle.refresh_project`), or the ACP runtime's non-waiting TASK verbs,
    answered for `identity` (peer-derived) alone. The wait verbs never reach here: `_handle`'s wait lane."""
    if name in ("runtime.events_append", "runtime.request_open", "runtime.request_cancel"):
        from acp import runtime  # noqa: PLC0415 — only an ACP host's call needs it

        hub = runtime.hub()
        handler = {"runtime.events_append": hub.events_append, "runtime.request_open": hub.request_open,
                   "runtime.request_cancel": hub.request_cancel}[name]
        return handler(identity, args or {})
    if name == "runtime.account_key":
        from acp import runtime  # noqa: PLC0415 — only an ACP host's call needs it

        # The answer is a LIVE KEY: the call is logged by name alone (`_perform_call`), never its answer.
        return runtime.hub().account_key(identity, args or {}, fetch=account_api_key)
    if agent_vocabulary.LOCAL_VERBS[name].wait:
        raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_BAD_REQUEST, f"{name}: a wait verb off the wait lane")
    if name == "guard.refresh":
        guard_bundle = cloud_modules.load("guard_bundle")  # the `compliance` feature's; None where it is not
        if guard_bundle is None:
            raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_BAD_REQUEST,
                                               "guard.refresh: the Architectural Guard is not in this edition")
        project_id = guard_bundle.project_id_of(args or {})
        if project_id is None:
            raise agent_vocabulary.VerbRefused(agent_vocabulary.ERR_BAD_REQUEST,
                                               "guard.refresh needs a positive integer project_id")
        return guard_bundle.refresh_project(project_id)
    keystore = nodevault_store.Keystore()
    if name == "nodevault.fingerprint":
        return {"fingerprint": keystore.fingerprint(), "capable": nodevault_identity.node_capable()}
    if name == "nodevault.keys":
        return {"keys": keystore.keys()}
    raise AssertionError(f"{name}: a LOCAL_VERBS row with no answer here")


def call_as_runner(verb_name: str, args: dict, identity: runner_registry.RunnerIdentity) -> object:
    """Speak ONE vocabulary verb AS the runner `identity` names, from inside the daemon — for a node-side
    sensor reporting what that runner's own hook would have, on a vendor CLI that fires no such hook
    (`vendor_turn_errors`). Same policy, same identity stamps and same CP route as the runner's own call,
    so the CP cannot tell the two apart; `identity` comes from `runner_registry`, never from a caller.
    ONLY `_AS_RUNNER_VERBS`: the uid here is the daemon's own, which an upload's confinement must never
    judge a path with. Raises as `_perform_call` does. Synchronous; run in a thread."""
    if verb_name not in _AS_RUNNER_VERBS:
        raise agent_vocabulary.VerbRefused(
            agent_vocabulary.ERR_UNKNOWN_VERB, f"{verb_name}: not a verb the daemon speaks as a runner")
    return _call_as(verb_name, args, identity, _daemon_uid(), identity.pane_pid)


# What the daemon may say AS a runner: the report a CLI with no such hook could not make itself, and the
# read of the runner's own task the idle backstop checks `active` with before it files one (a read: no upload
# confinement to misjudge).
_AS_RUNNER_VERBS = frozenset({"session.error", "task.get"})


class _NodeVaultRefused(agent_vocabulary.VerbRefused):
    """A NodeVault fetch this node cannot complete. Unlike every other refusal its `detail` travels on the
    wire: it is one of the fixed `_NODEVAULT_*_DETAIL` sentences, never caller or CP text."""


# The verb whose `merged_pr` body the daemon stamps with `pr_stats` and `pr_provenance` (`pr_stats_capture`).
_SETTLE_VERB = "task.complete"


# The verb whose `repo_id` the daemon stamps off the caller's reported `checkout` (`_observed_repo_id`).
_PR_OBSERVED_VERB = "pull_request.observed"


def _observed_repo_id(identity: runner_registry.RunnerIdentity | None, checkout: object,
                      vm: VmCaller | None) -> int | None:
    """The repository a `pull_request.observed` PR belongs to: None (the project's primary) for the runner's slot
    itself or a directory inside it, `<rid>` for its own `_repos/<rid>/<N>` checkout. Anything else — no
    identity, no checkout, another slot's checkout or a directory outside the slot — is REFUSED, never
    defaulted to the primary: a PR recorded on the wrong repository is worse than one not recorded.

    A VM caller is stamped the primary: its checkout is a guest path the host's slot layout cannot judge
    (per-repo attribution inside a MicroVM is deferred)."""
    if vm is not None:
        return None
    named = checkout if isinstance(checkout, str) else ""
    if identity is not None and identity.worktree and named:
        slot = os.path.realpath(identity.worktree)
        here = os.path.realpath(named)
        if here == slot or here.startswith(slot + os.sep):
            return None
        import slot_repos  # noqa: PLC0415 — lazy, as in `_caller_repo_id`

        repo_id = slot_repos.checkout_repo_id(identity.worktree, named)
        if repo_id is not None:
            return repo_id
    raise agent_vocabulary.VerbRefused(
        _ERR_BAD_REQUEST, f"{_PR_OBSERVED_VERB}: {named[:256]!r} is not a checkout of this task's slot")


# The verb whose answer may carry sealed NodeVault fields for this daemon to open.
_NODEVAULT_FETCH_VERB = "credential.get"


_NV_HASH_CHARS = frozenset("0123456789abcdef")


def _nv_hash_of(entry: object) -> str:
    """The 64-hex key hash an entry's envelope `k` names (`nv:<hash>`), or "" for anything else."""
    envelope = entry.get("envelope") if isinstance(entry, dict) else None
    key_id = envelope.get("k") if isinstance(envelope, dict) else None
    if not isinstance(key_id, str) or not key_id.startswith(nodevault_crypto.NV_KEY_ID_PREFIX):
        return ""
    digest = key_id[len(nodevault_crypto.NV_KEY_ID_PREFIX):]
    return digest if len(digest) == 64 and set(digest) <= _NV_HASH_CHARS else ""


def _open_nodevault(result: object) -> object:
    """`credential.get`'s answer with its `nodevault` map OPENED: each `{field: {envelope, aad}}` is
    opened with the held key its envelope's `k` names and merged into `config` as plaintext, and the map is
    dropped — so the runner-uid process never sees an envelope, and a non-NodeVault answer passes as it is.

    Runs in the daemon, the only uid that can read the keystore. Old and new keys both open while
    a rotation runs: whichever key the envelope names is the one used. Never logs a value and caches no
    plaintext; the one log line is the payload's SHAPE (field count, key hash prefixes). Raises
    `_NodeVaultRefused` — `nodevault_key_missing` when this node does not hold the key (the CP has already
    started its delivery), `nodevault_open_failed` for an envelope that will not open."""
    if not isinstance(result, dict) or "nodevault" not in result:
        return result
    sealed = result.pop("nodevault")
    if not sealed:
        return result
    if not isinstance(sealed, dict) or not isinstance(result.get("config"), dict):
        raise _NodeVaultRefused(_ERR_NODEVAULT_OPEN_FAILED, _NODEVAULT_OPEN_FAILED_DETAIL)
    hashes = sorted({_nv_hash_of(entry) for entry in sealed.values()})
    # Hex only: `k` is CP text, and a crafted one must not put control characters into the journal.
    log.info("cred-broker: %s carries %d sealed NodeVault field(s) under key(s) %s (ciphertext only)",
             _NODEVAULT_FETCH_VERB, len(sealed), ",".join(h[:12] for h in hashes if h) or "-")
    opened: dict = {}
    try:
        keystore = nodevault_store.Keystore()
        for field, entry in sealed.items():
            key_hash = _nv_hash_of(entry)
            if not key_hash:
                raise ValueError("the envelope names no NodeVault key")
            nvk = keystore.key(key_hash)
            if nvk is None:
                log.warning("cred-broker: NodeVault key %s is not held on this node", key_hash[:12])
                raise _NodeVaultRefused(_ERR_NODEVAULT_KEY_MISSING, _NODEVAULT_KEY_MISSING_DETAIL)
            aad = base64.b64decode(entry["aad"], validate=True)
            opened[field] = nodevault_crypto.open_secret(entry["envelope"], aad=aad, nvk=nvk)
    except _NodeVaultRefused:
        raise
    except (nodevault_crypto.NodeVaultCryptoError, nodevault_store.KeystoreError, OSError,
            KeyError, TypeError, ValueError) as exc:
        # The exception's class only: a crypto error's text is ours, but nothing here is worth the risk.
        log.warning("cred-broker: a NodeVault field did not open: %s", type(exc).__name__)
        raise _NodeVaultRefused(_ERR_NODEVAULT_OPEN_FAILED, _NODEVAULT_OPEN_FAILED_DETAIL) from None
    result["config"] = {**result["config"], **opened}
    return result


def _call_as(
    verb_name: object, args: object, identity: runner_registry.RunnerIdentity | None, uid: int, pid: int,
    vm: VmCaller | None = None,
) -> object:
    """`_perform_call`'s body once the identity is settled — the one place a verb is resolved, stamped
    and sent."""
    task_id = identity.task_id if identity else ""
    verb, path, query, body = agent_vocabulary.resolve(verb_name, args, task_id)
    # Keys the daemon owns: `node_id` is THIS node; `account` is the account the runner was dispatched onto.
    for key in verb.identity_body_keys:
        if key == "node_id":
            body[key] = control_plane_api.this_node_id()
        elif key == "account":
            # Omitted, not refused, when no account was recorded (optional CP-side; a lost incident is worse
            # than an unattributed one). Either spelling: an API-key runner whose home could not be derived has no account dir, and
            # `auth_rejected` is its only automatic exclusion; the CP resolves both via
            # `account_auth.account_slug_from_ref`.
            if identity is not None and identity.account_dir:
                body[key] = identity.account_dir
            elif identity is not None and identity.api_key_account:
                body[key] = f"{account_auth.API_KEY_REF_PREFIX}{identity.api_key_account}"
        else:
            # Loud: `_DAEMON_OWNED_KEYS` refuses the key in `args`, so an unstamped one would go silently
            # absent. Declaring a key and stamping it are one change.
            raise AssertionError(f"{verb.name}: no stamp implemented for identity key {key!r}")
    if verb.name == _SETTLE_VERB and body.get("terminal_type") == "merged_pr":
        # Read off the forge with the task's OWN login, one budget for both keys; never raises and never
        # blocks the settle (AC9.2).
        body.update(pr_stats_capture.capture_all(
            lambda: _fetch_git_login(task_id, "", uid, pid), body.get("pr_url"), keys=verb.daemon_body_keys))
    elif verb.name == _PR_OBSERVED_VERB:
        body["repo_id"] = _observed_repo_id(identity, body.pop("checkout", None), vm)
    elif set(verb.daemon_body_keys) & set(body):  # pragma: no cover — `resolve` refused it in `args`
        raise AssertionError(f"{verb.name}: a daemon body key reached the wire unstamped")
    url = f"{control_plane_api.resolve_control_plane_api_base()}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = control_plane_api._agent_headers()
    headers["X-Agent-Task-Id"] = agent_vocabulary.caller_task_header(task_id)
    if verb.file_body:
        # `resolve` shape-checked the name only; everything else is `_upload_file_body`'s.
        named = str(body.pop(verb.file_body))
        # The declared form fields, popped strictly (`_check_file_body` required each).
        fields = {name: str(body.pop(name)) for name in verb.form_fields}
        # An upload has no JSON body, so a leftover is a daemon stamp silently lost — a table bug.
        if body:
            raise AssertionError(
                f"{verb.name}: an upload verb would drop stamped body keys {sorted(body)}")
        return _upload_file_body(verb, path, headers, named, fields, task_id, pid, uid, vm)
    if verb.attachment_paths and body.get(verb.attachment_paths):
        body[verb.attachment_paths] = _upload_attachment_paths(
            body[verb.attachment_paths], task_id, pid, uid, vm)
    if verb.takes_body:
        data = json.dumps(body, default=control_plane_api._json_default).encode()
    else:
        data = None
    log.info(
        "cred-broker: uid=%s pid=%s call=%s task=%s -> %s %s",
        uid, pid, verb.name, agent_vocabulary.caller_task_header(task_id), verb.method, path,
    )
    try:
        result = _send_to_cp(verb, url, headers, data)
    except urllib.error.HTTPError as exc:
        refused = pr_stats_capture.refused_keys(
            exc.code, control_plane_api.cp_error_body(exc), tuple(k for k in verb.daemon_body_keys if k in body))
        if not refused:
            raise
        # A CP that forbids a field: the settle matters, the stamp does not — once more without it.
        log.warning("cred-broker: %s refused %s (HTTP %s); settling without them", verb.name, sorted(refused),
                    exc.code)
        for key in refused:
            body.pop(key)
        result = _send_to_cp(verb, url, headers, json.dumps(body, default=control_plane_api._json_default).encode())
    if verb.name == _NODEVAULT_FETCH_VERB:
        result = _open_nodevault(result)
    return result


def _credential_name(line: bytes) -> str | None:
    """The requested credential name, or None if this line is not a well-formed request.

    SHAPE ONLY; whether the name is served is `_handle`'s call, so an unknown name gets a journal line.
    Every other key (`uid`/`pid`/`task_id`/`repo_id` included) is ignored: identity comes from ancestry, and
    `checkout` is read on its own (`_reported_checkout`)."""
    parsed = json_object(line)
    if parsed is None:
        return None
    name = parsed.get("credential")
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        return None
    return name.strip()


def _reported_checkout(line: bytes) -> str:
    """The `checkout` a credential line reports (the caller's working directory), or "". Where the caller
    works, not what it may have: `_caller_repo_id` admits only its own slot's checkouts."""
    value = (json_object(line) or {}).get("checkout")
    return value if isinstance(value, str) and 0 < len(value) <= _MAX_CHECKOUT_CHARS and "\0" not in value else ""


def _reported_destination(line: bytes) -> dict[str, str] | None:
    """Where git will send the login (`"for"` on a git credential line: its `protocol`/`host`/`path`), or None
    when the line names none — `jaah-forge`, which asks for the login to call the forge's API, not git."""
    parsed = json_object(line)
    if parsed is None or "for" not in parsed:
        return None
    value = parsed["for"]
    if not isinstance(value, dict):
        # Named but unreadable: checked as a destination of nowhere, so refused.
        value = {}
    return {key: value[key] if isinstance(value.get(key), str) else ""
            for key in git_credential_helper.DESTINATION_KEYS}


def _refuse_foreign_destination(destination: dict[str, str] | None, value: object) -> str | None:
    """Why the login `value` may not go where git said it will (`git_credential_helper.check_destination`:
    another host, or another repository on the same host), or None when it may. The helper refuses too;
    this is the refusal at the source, so no key a future git reads before the helper can redirect the
    login elsewhere."""
    if destination is None or not isinstance(value, dict):
        return None
    try:
        git_credential_helper.check_destination(destination, value)
    except git_credential_helper.Refused as refused:
        return " ".join(str(refused).split())
    return None


def _reported_fresh(line: bytes) -> bool:
    """Whether a credential line asks past the cache (`"fresh": true`, `_CALLER_FRESH`). Only a literal true."""
    return (json_object(line) or {}).get("fresh") is True


def _request_shape(line: bytes) -> tuple[str, object] | None:
    """`("credential", name)` or `("call", {"call": ..., "args": ..., "capability": ...})`, or None when the
    line is not a well-formed request of either shape.

    Both keys at once is refused (a client bug, not a typo to guess at). Shape only — never a second copy
    of either allowlist."""
    parsed = json_object(line)
    if parsed is None:
        return None
    has_credential = "credential" in parsed
    has_call = "call" in parsed
    if has_credential and has_call:
        return None
    if has_credential:
        name = _credential_name(line)
        return ("credential", name) if name is not None else None
    if has_call:
        verb = parsed.get("call")
        if not isinstance(verb, str) or not verb.strip() or len(verb) > 256:
            return None
        args = parsed.get("args", {})
        if args is not None and not isinstance(args, dict):
            return None
        # A guest ACP host's capability (`_host_fence`) rides beside the args, never in them.
        capability = parsed.get("capability", "")
        if not isinstance(capability, str) or len(capability) > 256:
            return None
        return ("call", {"call": verb.strip(), "args": args or {}, "capability": capability})
    return None


def _rejected_reason(exc: urllib.error.HTTPError) -> str:
    """The wire string for a CP refusal: its HTTP status, plus the CP's own reason compacted to one line.

    The detail is an owner decision (`_ERR_REJECTED`). Only `json.loads` decides what is `detail` — an
    unparseable (e.g. truncated) body gets the bare status, since a regex could be fed planted text.
    RELAYED: a string `detail` in full; else `msg` and `loc` per entry of a list `detail`. NEVER: `input`,
    `ctx`, `url`, other keys, other types, anything from an unparseable body (pinned by
    `test_exactly_msg_and_loc_cross_and_nothing_else`). All three relayed can carry caller-influenced text
    — accepted: it returns to the peer that sent it, bounded twice, and no verb sends a secret to the CP.
    Never raises."""
    status = getattr(exc, "code", 0) or 0
    body = control_plane_api.cp_error_body(exc)
    text = ""
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, str):
            text = detail
        elif isinstance(detail, list):
            rendered = [_validation_entry(entry) for entry in detail]
            text = "; ".join(part for part in rendered if part)
    text = _one_clean_line(text)[:_MAX_REJECTED_DETAIL].strip()
    if not text:
        # A status with no reason still says the CP answered and refused — don't resend as is.
        return f"{_ERR_REJECTED}: HTTP {status}"
    return f"{_ERR_REJECTED}: HTTP {status} — {text}"


def _one_clean_line(text: str) -> str:
    """Collapse whitespace AND drop non-printables: a NUL/C0 byte is valid UTF-8 and would otherwise ride
    into a journal line and a model-facing tool error."""
    return " ".join("".join(c if c.isprintable() or c == " " else " " for c in text).split())


def _validation_entry(entry: object) -> str:
    """One pydantic validation error rendered `msg (field.path)`, or "" when there is nothing to say.

    Reads ONLY `msg` and `loc`. `input` — the caller's own echoed value — is never touched."""
    if not isinstance(entry, dict):
        return ""
    message = str(entry.get("msg") or "").strip()
    if not message:
        return ""
    raw_loc = entry.get("loc")
    parts = [str(p) for p in raw_loc if p is not None] if isinstance(raw_loc, (list, tuple)) else []
    where = ".".join(parts)
    return f"{message} ({where})" if where else message


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, vm: VmCaller | None = None) -> None:
    """One request, one response, one connection. Every failure path answers `{"ok": false}` and closes;
    a client blocked on a never-completing `readline()` is worse than a refusal. `vm` is set only by
    `serve_vm_connection`: the caller is that VM's guest, and no kernel peer credential is read."""
    global _OPEN_CONNECTIONS
    if _OPEN_CONNECTIONS >= _MAX_CONNECTIONS:
        # Closed without a reply: composing one allocates for a peer already over its share; a closed
        # connection reads as unavailable, the honest retryable answer.
        log.warning("cred-broker: refusing a connection, %s already open", _OPEN_CONNECTIONS)
        writer.close()
        return
    _OPEN_CONNECTIONS += 1
    uid, pid = -1, -1
    name = ""
    outcome = _ERR_BAD_REQUEST
    cache_hit = False
    # Whose credential this ask resolved to (`task:<id>:<run>` / `system:<project>`), for the journal only — the
    # only record of which user's token a runner used. "-" where no answer came back; a failure is still
    # attributable via `_fetch_git_login`'s route log line just before.
    identity = "-"
    reply: dict = {"ok": False, "error": _ERR_BAD_REQUEST}
    try:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
        except (asyncio.TimeoutError, ValueError) as exc:
            # ValueError is `readline()`'s over-long line. Both are client bugs → `bad_request`.
            log.warning("cred-broker: a peer sent no usable request line (%s)", exc)
            line = b""
        # AFTER the read, never before: Windows identifies a pipe client by impersonating it, which the
        # kernel refuses until the server has read from the pipe (ERROR_CANNOT_IMPERSONATE). The kernel's
        # answer is the same either way on posix (`SO_PEERCRED` is stamped at connect).
        if vm is None:
            uid, pid = _peer_identity(writer)
            peer_key = _peer_key(uid, pid)
        else:
            # The guest acts as the daemon uid on the host (its uid 1000 maps there), which is what an
            # upload's ownership check must compare against; no pid exists for it.
            uid, pid = _daemon_uid(), -1
            peer_key = f"vm:{vm.vm_id}"
        # Which shape, decided once: an unparseable line is one `bad_request`, and both keys is refused.
        parsed = _request_shape(line) if line else None
        if parsed is None:
            reply = {"ok": False, "error": _ERR_BAD_REQUEST}
        else:
            kind, payload = parsed
            if kind == "credential":
                name = payload
                _CALLER_CHECKOUT.set(_reported_checkout(line))
                _CALLER_FRESH.set(_reported_fresh(line))
                if name not in (_GITHUB_TOKEN, _GIT_CREDENTIAL) and not name.startswith(
                        (_ACCOUNT_API_KEY_PREFIX, _BEDROCK_PREFIX)):
                    # Refused here, not forwarded — there is no generic credential endpoint behind this.
                    # `bad_request`: no retry can make the name exist. Prefix test only; the exact parse
                    # (`account_auth.slug_from_node_credential_name`) and its malformed-name refusal are in
                    # `_resolve`.
                    outcome = _ERR_BAD_REQUEST
                    reply = {"ok": False, "error": _ERR_BAD_REQUEST}
                    log.warning("cred-broker: uid=%s pid=%s asked for %r, which this broker does not "
                                "serve", uid, pid, name)
                # Same cap as the `call` arm: failures are not cached, so a refused identity's flood would
                # be a CP round trip each on the shared pool.
                elif not _acquire_slot(peer_key):
                    log.warning("cred-broker: uid=%s pid=%s over the in-flight cap on a credential ask",
                                uid, pid)
                    writer.write(json.dumps({"ok": False, "error": _ERR_BUSY}).encode() + b"\n")
                    await writer.drain()
                    return
                else:
                    try:
                        # `vm` only when there is one: a pid caller's call keeps its three-argument shape.
                        value, cache_hit, identity = await (
                            _resolve(name, uid, pid) if vm is None else _resolve(name, uid, pid, vm))
                    except urllib.error.HTTPError as refused:
                        # The CP refused: its operator-facing detail reaches the terminal via
                        # `jaah-credential`'s stderr. Still FAILS CLOSED (nothing on stdout, non-zero
                        # exit), so git errors out rather than parking at a prompt.
                        outcome = _ERR_REJECTED
                        reply = {"ok": False, "error": _rejected_reason(refused)}
                        log.warning("cred-broker: uid=%s pid=%s credential %s refused with HTTP %s",
                                    uid, pid, name, getattr(refused, "code", "?"))
                    except agent_vocabulary.VerbRefused as refused:
                        # Neither a live runner nor this daemon (`_resolve`'s uid check). Reason on the
                        # wire, detail to the journal only; not `unavailable` (no retry helps). Fails closed.
                        outcome = _ERR_BAD_REQUEST
                        reply = {"ok": False, "error": refused.reason}
                        log.warning("cred-broker: uid=%s pid=%s credential %s refused: %s",
                                    uid, pid, name, refused)
                    except Exception as exc:  # noqa: BLE001 — no answer to read is one outcome
                        outcome = _ERR_UNAVAILABLE
                        reply = {"ok": False, "error": _ERR_UNAVAILABLE}
                        log.warning("cred-broker: uid=%s pid=%s credential %s failed: %s",
                                    uid, pid, name, exc)
                    else:
                        foreign = (_refuse_foreign_destination(_reported_destination(line), value)
                                   if name == _GIT_CREDENTIAL and value else None)
                        if foreign:
                            outcome = _ERR_BAD_REQUEST
                            # `destination_refused`: the helper stays quiet, as for its own refusal.
                            reply = {"ok": False, "error": foreign, "destination_refused": True}
                            log.info("cred-broker: uid=%s pid=%s credential %s refused for its destination: %s",
                                        uid, pid, name, foreign)
                        elif value:
                            outcome = "ok"
                            reply = {"ok": True, "value": value}
                        else:
                            # A 200 with no token: nothing the caller can fix → same word as a timeout.
                            outcome = _ERR_UNAVAILABLE
                            reply = {"ok": False, "error": _ERR_UNAVAILABLE}
                    finally:
                        _release_slot(peer_key)
            else:
                name = str(payload.get("call"))
                wait_row = _wait_row(payload)
                if wait_row is not None:
                    outcome, reply = await _serve_wait(wait_row, payload.get("args"), uid, pid, vm, peer_key,
                                                       payload.get("capability", ""))
                else:
                    if not _acquire_slot(peer_key):
                        outcome = _ERR_BUSY
                        reply = {"ok": False, "error": _ERR_BUSY}
                        log.warning("cred-broker: uid=%s pid=%s over the in-flight cap (key=%s total=%s)",
                                    uid, pid, peer_key, sum(_INFLIGHT_BY_PEER.values()))
                        writer.write(json.dumps(reply).encode() + b"\n")
                        await writer.drain()
                        return
                    try:
                        result = await asyncio.get_running_loop().run_in_executor(
                            _CALL_EXECUTOR, _perform_call, payload.get("call"), payload.get("args"), uid, pid,
                            *(() if vm is None else (vm, payload.get("capability", ""))))
                    except agent_vocabulary.VerbRefused as refused:
                        # Reason on the wire, detail to the journal only (it can quote the request, and the
                        # reply is readable by all of group `jaah`). Exempt: `malformed_call`, whose caller
                        # is a MODEL that loops without the correction; safe because `agent_vocabulary`
                        # composes that detail itself, never from a caller's value.
                        outcome = refused.reason
                        if isinstance(refused, _NodeVaultRefused):
                            reply = {"ok": False, "error": refused.reason, "detail": refused.detail}
                        elif refused.reason == agent_vocabulary.ERR_MALFORMED_CALL:
                            detail = _one_clean_line(refused.detail)[:_MAX_REFUSAL_DETAIL].strip()
                            reply = {"ok": False, "error": f"{refused.reason}: {detail}"}
                        else:
                            reply = {"ok": False, "error": refused.reason}
                        log.warning("cred-broker: uid=%s pid=%s refused: %s", uid, pid, refused.detail)
                    except urllib.error.HTTPError as refused:
                        # The CP answered NO — not a transport failure. Before the blanket `except` below.
                        reason = _rejected_reason(refused)
                        # The journal's `outcome=` stays the bare enum word (aggregatable); the wire carries
                        # status and detail.
                        outcome = _ERR_REJECTED
                        reply = {"ok": False, "error": reason}
                        # No body echo: `_agent_urlopen` already logged it at ERROR.
                        log.warning("cred-broker: uid=%s pid=%s call %s refused with HTTP %s",
                                    uid, pid, name, getattr(refused, "code", "?"))
                    except Exception as exc:  # noqa: BLE001 — every remaining transport failure is one outcome
                        # No status at all: timeout, closed connection, DNS, non-JSON — no answer to read.
                        outcome = _ERR_UNAVAILABLE
                        reply = {"ok": False, "error": _ERR_UNAVAILABLE}
                        log.warning("cred-broker: uid=%s pid=%s call %s failed: %s", uid, pid, name, exc)
                    else:
                        outcome = "ok"
                        # `result` may be None (a 204), so the key is carried unconditionally.
                        reply = {"ok": True, "result": result}
                    finally:
                        # Released in EVERY arm: a leaked slot would permanently shrink this principal's budget.
                        _release_slot(peer_key)
        log.info(
            "cred-broker: uid=%s pid=%s%s name=%s identity=%s outcome=%s cache_hit=%s",
            uid, pid, f" vm={vm.vm_id}" if vm is not None else "", name or "-", identity, outcome, cache_hit,
        )
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
    except OSError as exc:
        # The client hung up mid-answer; nothing wrong on this side.
        log.debug("cred-broker: uid=%s pid=%s disconnected before the reply landed: %s", uid, pid, exc)
    finally:
        _OPEN_CONNECTIONS -= 1
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


# Open guest connections per VM: one guest must not hold the node-wide `_MAX_CONNECTIONS` and starve every
# other VM and host caller. Touched only on the loop thread.
_VM_OPEN: dict[str, int] = {}
_MAX_CONNECTIONS_PER_VM = 8


async def serve_vm_connection(vm_id: str, sock) -> None:
    """Serve ONE guest connection `jaah-microvmd` accepted on VM `vm_id`'s control listener (vsock 1024) and
    handed over (`microvm_control`). Identity is `vm_id`, the supervisor's tag — never the request."""
    if _VM_OPEN.get(vm_id, 0) >= _MAX_CONNECTIONS_PER_VM:
        log.warning("cred-broker: VM %s already holds %d connections — refusing another", vm_id,
                    _MAX_CONNECTIONS_PER_VM)
        sock.close()
        return
    _VM_OPEN[vm_id] = _VM_OPEN.get(vm_id, 0) + 1
    try:
        try:
            reader, writer = await asyncio.open_connection(sock=sock, limit=_MAX_REQUEST_BYTES)
        except OSError as exc:
            log.warning("cred-broker: VM %s connection could not be served: %s", vm_id, exc)
            sock.close()
            return
        await _handle(reader, writer, VmCaller(vm_id))
    finally:
        _VM_OPEN[vm_id] -= 1
        if not _VM_OPEN[vm_id]:
            _VM_OPEN.pop(vm_id, None)


async def start(socket_path: Path | str | None = None) -> asyncio.AbstractServer | None:
    """Bind the broker socket and start serving. Returns the server, or None when it could not bind.

    Not fatal (the daemon's other loops keep running) but an ERROR: the realistic cause is a missing
    `/run/jaah/creds`. It deliberately does NOT mkdir — the directory's mode (2750 jaahd:jaah) is the
    access control, and a Python mkdir would substitute the daemon's umask. A stale socket is unlinked
    (a crash under `KillMode=process` leaves one, and `bind()` would EADDRINUSE forever). The path is
    resolved ONCE so unlink, bind and chmod hit one inode."""
    path = _resolve_socket_path(socket_path)
    # Windows: the endpoint is a named pipe (`\\.\pipe\jaah-creds`), not a file — nothing stale to unlink
    # (the pipe dies with its last handle) and no mode to set: the pipe's DACL, applied by
    # `host_platform.windows.serve_socket` at creation, is the whole access control.
    pipe = host_platform.is_windows()
    try:
        if not pipe and path.is_socket():
            path.unlink()
        server = await host_platform.serve_socket(str(path) if pipe else path, _handle,
                                                  limit=_MAX_REQUEST_BYTES)
        # Not redundant with the directory mode: connect(2) checks WRITE on the inode, and `UMask=0002`
        # leaves it 0775. 0660 keeps the group as the boundary; the 2750 directory decides reachability.
        if not pipe:
            os.chmod(path, 0o660)
    except OSError as exc:
        log.error(
            "cred-broker: could not bind %s (%s) — credential requests will be refused by connect(2); "
            "check that jaahd.service's ExecStartPre created the directory", path, exc,
        )
        return None
    log.info("cred-broker: serving on %s", path)
    return server


async def stop(server: asyncio.AbstractServer, socket_path: Path | str | None = None) -> None:
    """Stop accepting, drain in-flight handlers, and remove the socket file.

    The unlink stops clients connecting to a dead daemon's inode. Needed: `start_unix_server` cleans up
    only from 3.13, and the node floor is 3.12 (the package's declared dependency); `missing_ok` for newer runtimes.
    Resolved once so the unlink and its log line name the same path."""
    path = _resolve_socket_path(socket_path)
    server.close()
    try:
        await server.wait_closed()
    except Exception as exc:  # noqa: BLE001 — shutdown must not raise into `_serve`'s finally chain
        log.debug("cred-broker: wait_closed raised during shutdown: %s", exc)
    if host_platform.is_windows():
        return  # a named pipe leaves no file behind (see `start`)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("cred-broker: could not unlink %s on shutdown: %s", path, exc)
