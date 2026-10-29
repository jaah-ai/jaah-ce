"""Command executor bridge — fed by the WebSocket node bus.

Executes each request handed to `deliver()` IN-PROCESS via its `NATIVE_HANDLERS`-registered handler.
The registry is append-only and self-registered via `@native_command(...)`, so a family lands by ADDING
a handler-module import here, never by editing the `_on_message` routing ladder.

Payload (CP -> node, request/reply): `{"v":1, "cp_frame_proof": str, "command_type": str, "args": dict}`.
Reply (node -> CP): `{"v":1, "status": "done"|"failed"|"rejected", "result": {...}|None}`.

`cp_frame_proof` (`node_frame_auth`) gates execution fail-closed — the node-side half of the
double-guard (the CP rejects before enqueue AND the node re-checks before execute).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

import clone_identity   # the delegated-op registry: a native git child must be admitted by the broker
import control_plane_api   # the slot-LABEL write (`POST /api/agent/slot-holder`: Title + `last_used`)
import env_helpers   # `mark_agents_dirty`: the agents cache's command-side invalidation
from jaah_env import env_get
import git_env   # `NO_REPO_CODE`: this seam pins repo-local code off on every git argv it runs
import kill_gates as kill_gates_mod   # single home for a socket-correct tmux window kill
import node_frame_auth   # single home for "is this CP->node frame authentic" (the cp_frame_proof check)
import node_settings   # the CP's node tunables (`node_max_concurrent_commands`)
from reconcile import tick_wire, accounts_probe, context, orchestrate  # the sensing pass, reused by the `reconcile` verb
import session_user   # native git shell-outs run AS the session user (see _native_run)
from worktree_mechanics import core, lifecycle   # in-process git-only retire/create mechanics

log = logging.getLogger("jaahd.commands")

COMMANDS_ENABLED = env_get("JAAH_COMMANDS_ENABLED", "1") != "0"

# A test PIN, not the value: `None` (production) means "read `os.environ['NODE_TOKEN']` at validation
# time". Never resolve it at import — a long-running daemon then validates every request against
# whatever the value was at start, so any rotation rejects EVERY runner request until a restart. The
# pin exists because pinning via `os.environ` leaks `NODE_TOKEN` into every subprocess a test spawns.
NODE_TOKEN: str | None = None

# Worktree-pool state-mutation verbs served natively. Account
# selection is NOT here — the CP picks the account and hands it over on the `lease_slot` intent.
MUTATION_COMMANDS = frozenset({
    "release", "retire", "reconcile", "set_title", "create",
})

# A little UNDER the CP's COMMAND_TIMEOUT (180s), so this daemon replies "failed" before the CP times out.
EXECUTE_TIMEOUT_SECONDS = 170.0

_BUS_SCHEMA_VERSION = 1

# Node-side occupancy policy — the NODE-side half of the double-guard (a node-bus command never
# passes the CP's HTTP guard). Occupancy is `worktrees.state`;
# slot 0 is structurally unleasable, so `is_main` off the roster is what `_retire` guards on.
# RELEASE IS DELIBERATELY UNGUARDED (owner decision: `worktrees:edit` carries no occupancy limit).

# Command types the native execute path re-runs an occupancy check for. The CP's `delete_one_slot`
# (`app/routes/pool_slots.py`) commits the row delete BEFORE dispatching `worktree_erase`, and
# `_guard_check` reads the roster live — so the CP's own path passes by construction, and only a message
# naming a slot the CP still declares held is refused.
# The NodeVault verbs (`nodevault_commands`, architecture §4.3) are members too, as §3.1 pins: they carry
# no project/number, so the occupancy re-check passes them. (`cp_frame_proof` gates every frame, member or not.)
GUARDED_COMMAND_TYPES = frozenset({
    "worktree_erase",
    "nodevault.install_key", "nodevault.retire_key", "nodevault.held_keys",
    "nodevault.wrap_for_node", "nodevault.rekey", "nodevault.verify",
})

# Single-homed in `reconcile/tick_wire.py`; re-exported, never redefined.
OCCUPIED_SLOT_STATES = tick_wire.OCCUPIED_SLOT_STATES

# Account commands that change `$JAAH_HOME/auth/<slug>` and so need a post-exec FULL reconcile sweep
# (`account_converge` touches only local config). Both completion verbs: `*_await_login` ends the flow
# exactly as `*_submit_code` does.
_ACCOUNT_RECONCILE_COMMANDS = frozenset({
    "account_add", "account_add_submit_code", "account_add_await_login",
    "account_reauth", "account_reauth_submit_code", "account_reauth_await_login",
    "account_delete", "account_shared_delete",
})


def _expected_token() -> str:
    """The token to validate against, resolved AT CALL TIME — the pin if set, else `os.environ` read
    fresh, so a rotation takes effect without restarting the daemon."""
    return NODE_TOKEN if NODE_TOKEN is not None else env_helpers.environ_node_token()


def _reply(status: str, result: dict | None = None) -> bytes:
    return json.dumps({"v": _BUS_SCHEMA_VERSION, "status": status, "result": result}).encode()


class _MutationRejected(Exception):
    """A mutation verb's precondition failed (e.g. `retire` on the project's main clone).
    `_handle_mutation` catches it and replies `failed` — loudly, never silently."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _no_sensed_record_reason(project: str, number: str) -> str:
    """Why a slot mutation refused: the slot has no SENSED record to mutate.

    The CP says this with `mutated == 0` on `POST /api/agent/slot-holder`. Slot EXISTENCE has one author,
    the sensor; a label stamp minting a row would mint an unusable `path`-less one."""
    return (
        f"{project} #{number} has no sensed worktree record — refusing to create a slot record from a "
        "mutation (the sensor owns a slot's existence)"
    )


def _no_sensed_record(verb: str, project: str, number: str) -> dict:
    """`_no_sensed_record_reason` as a loud verb reply."""
    return {"status": "failed", "result": {"reason": f"{verb}: {_no_sensed_record_reason(project, number)}"}}


# ── Native handler registry (strangler membrane) ───────────────────────────────────────────────────
# `command_type` → in-process handler for a ported command family. APPEND-ONLY and self-registered:
# a family lands by adding an import here, never by editing the `_on_message` routing ladder. A
# handler is a SYNCHRONOUS `(command_type, args, run) -> {"exit_code", "stdout", "stderr"}` callable
# that shells out via the injected `run` closure; the bridge runs it OFF the event loop, so its
# blocking subprocess calls never stall the message loop.
NativeHandler = Callable[[str, dict, Callable[..., dict]], dict]
NATIVE_HANDLERS: dict[str, NativeHandler] = {}

# Command types handled by a DEDICATED `_on_message` branch before the registry `else`; a native
# registration of one would be dead code, so `native_command` rejects it.
_RESERVED_COMMAND_TYPES = MUTATION_COMMANDS


def native_command(*command_types: str) -> Callable[[NativeHandler], NativeHandler]:
    """Register `fn` as the native in-process handler for each `command_type`.

    APPEND-ONLY: a DIFFERENT function for an already-claimed command_type is a hard error, because two
    modules silently claiming one verb is a routing bug rather than last-writer-wins; re-registering
    the SAME function is a no-op. A `_RESERVED_COMMAND_TYPES` member is rejected as dead-code."""
    def register(fn: NativeHandler) -> NativeHandler:
        for command_type in command_types:
            if command_type in _RESERVED_COMMAND_TYPES:
                raise RuntimeError(
                    f"cannot register a native handler for reserved command_type {command_type!r}: "
                    "it is served by a dedicated _on_message branch before the strangler registry, so "
                    "the registration would be dead-code (re-home its dedicated branch instead)"
                )
            existing = NATIVE_HANDLERS.get(command_type)
            if existing is not None and existing is not fn:
                raise RuntimeError(
                    f"native handler for {command_type!r} already registered "
                    f"({existing.__name__}); the registry is append-only"
                )
            NATIVE_HANDLERS[command_type] = fn
        return fn
    return register


# ── Ported native handler families (self-register via @native_command on import) ─────────────────────
# Imported purely for the registration side effect, and AFTER `native_command` /
# `NATIVE_HANDLERS` / `_RESERVED_COMMAND_TYPES` are defined: each family module does `from commands
# import native_command`, so an earlier import hits a half-initialized module.
import accounts as accounts_mod  # noqa: E402,F401 — account_* family


def _native_run(*argv, cwd=None, timeout: float | None = None, env=None) -> dict:
    """Bounded, output-capped subprocess shell-out for a NATIVE ported handler. Output capture is
    `session_user.capped_result`'s (the CP execute path's caps), so a ported family's reply is byte-stable.

    TIMEOUT GUARDRAIL, enforced rather than advisory: a native handler runs inside
    `asyncio.to_thread` and CANNOT be forcibly killed once its shell-out is running, so an explicit
    `timeout` above `EXECUTE_TIMEOUT_SECONDS` is clamped down to it. A handler that legitimately
    needs longer MUST run its op DETACHED, never synchronously via this closure."""
    if timeout is None:
        timeout = EXECUTE_TIMEOUT_SECONDS
    elif timeout > EXECUTE_TIMEOUT_SECONDS:
        log.warning(
            "native handler passed subprocess timeout=%ss over the %ss bus budget for %r — clamping "
            "(a longer op must run DETACHED, never synchronously via _native_run)",
            timeout, EXECUTE_TIMEOUT_SECONDS, argv[0] if argv else "?",
        )
        timeout = EXECUTE_TIMEOUT_SECONDS
    # Repo-local code (hooks, fsmonitor, templates, askpass) pinned off on BOTH branches below: the seam owns
    # it, so no caller has to remember (`git_env.NO_REPO_CODE`). A caller's own copy is harmless.
    if argv and argv[0] == "git":
        argv = ("git", *git_env.NO_REPO_CODE, *argv[1:])
    # git runs AS THE SESSION USER where the uid split applies (daemon-owned files make git distrust the
    # tree). Fail-closed when the switch is unavailable; the delegated env is the caller's `env` ONLY,
    # never this process's (`NODE_TOKEN`) — a promise scoped to this branch: non-git argv inherits this
    # environment as the daemon uid. Not widened to `bash`, whose handlers pop DESKTOP windows here.
    if argv and argv[0] == "git" and session_user.switch_needed():
        if not session_user.can_run_as_session_user():
            return {
                "exit_code": core.SWITCH_UNAVAILABLE_RC, "stdout": "",
                "stderr": "session-user switch required but unavailable — refusing to run git as the "
                          "daemon uid; check jaah-runner@<node name>.service",
            }
        # Registered as this daemon's own op, as `worktree_mechanics.core._run_git` does: delegation sheds
        # the daemon's uid, and the broker would refuse the credential helper (`clone_identity`).
        try:
            with clone_identity.delegated_op(session_user.session_uid()) as registration:
                return session_user.run_capped(
                    list(argv), cwd=cwd, env=dict(env or {}), timeout=timeout, as_session_user=True,
                    on_child_pid=registration.claim, failure_rc=core.SWITCH_UNAVAILABLE_RC,
                )
        except Exception as exc:  # noqa: BLE001 — the uid lookup or registration failed before the run
            return {
                "exit_code": core.SWITCH_UNAVAILABLE_RC, "stdout": "",
                "stderr": f"session-user delegation failed: {exc}",
            }
    if not (argv and argv[0] == "git"):
        return session_user.run_capped(list(argv), timeout=timeout, cwd=cwd, env=env)
    try:
        # In-process git is registered under this thread's declaration (`clone_identity.run_own`), or the
        # broker serves its credential helper no project. Its `env` is laid OVER this process's, as the
        # delegated branch's is over the seam's defaults: a git pin (`git_env.NO_USER_CONFIG`) is not a
        # whole environment.
        result = clone_identity.run_own(list(argv), text=True, timeout=timeout, cwd=cwd,
                                        env=None if env is None else {**os.environ, **env})
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": -1, "stdout": "", "stderr": f"timeout: {exc}"}
    except Exception as exc:  # noqa: BLE001 — `run_capped`'s arms, for the one runner it cannot take
        return {"exit_code": -1, "stdout": "", "stderr": str(exc)}
    return session_user.capped_result(result)


# Enough to name the cause in the console's hover title; the full text stays in `result.stderr`.
_FAILURE_REASON_CHARS = 400


def _native_failure_reason(result: dict) -> dict:
    """`{"reason": …}` naming why a native handler failed — its stderr, else its stdout, else nothing.

    Empty dict when there is no output: `reason: ""` would override the caller's own default."""
    for stream in ("stderr", "stdout"):
        text = str(result.get(stream) or "").strip()
        if text:
            return {"reason": text[-_FAILURE_REASON_CHARS:]}
    return {}


class CommandBridge:
    """Executes each request handed to `deliver()` IN-PROCESS via its `NATIVE_HANDLERS`-registered
    handler, replying with the JSON result.

    Owns NO transport: requests arrive by direct call from the WebSocket node-bus client.
    An unregistered command_type is answered with an in-process failure — no subprocess fallback."""

    def __init__(
        self,
        node_id: str,
        hub_root: Path,
        console_live_probe: Callable[[str, str], bool] | None = None,
        reconcile_lock: "asyncio.Lock | None" = None,
        account_probe_nudge: "Callable[[], None] | None" = None,
        agents_cache: "accounts_probe.AgentsProbeCache | None" = None,
    ) -> None:
        self._node_id = node_id
        self._hub_root = hub_root
        # Forwarded into every ReconcileContext this bridge builds so the DONE-close gate's
        # console-liveness check works on this path too, not just the idle-tick loop's.
        self._console_live_probe = console_live_probe
        # Serializes this bridge's `reconcile` verb against the idle-tick publisher's projection (and
        # any overlapping `reconcile` request) so the two never write the same keys concurrently.
        # SHARED with the publisher when `main._serve` injects one; a standalone bridge makes its own.
        self._reconcile_lock = reconcile_lock
        # Invalidates the publisher's account-probe interval so the NEXT tick re-probes, instead of the
        # console waiting out a full account-probe interval to notice a login that just succeeded (see
        # `_fire_post_reconcile`). None on a standalone/test bridge, which skips the nudge.
        self._account_probe_nudge = account_probe_nudge
        # The cross-tick `claude agents` cache, SHARED with the idle-tick publisher, so every reconcile
        # this bridge runs reuses its snapshot instead of forking per account (8 x ~0.4s on prod nodes).
        # None on a standalone/test bridge, which then probes per pass.
        self._agents_cache = agents_cache
        # The daemon's OWN post-exec reconciles (`_fire_post_reconcile`), held so the loop does not GC a
        # running task mid-pass. Separate from `_inflight`, which is the per-message handlers: a reply
        # has already gone out when one of these starts, and `stop()` drains both.
        self._background: set[asyncio.Task] = set()
        # Per-message dispatch bookkeeping: the read loop calls `deliver` inline, so a tracked task per
        # command is spawned rather than awaiting the handler there. `_inflight` lets `stop()`
        # cancel/drain outstanding handlers; `_dispatch_sem` (`_dispatch_bound`) bounds concurrent handling.
        self._inflight: set[asyncio.Task] = set()
        self._dispatch_sem: asyncio.Semaphore | None = None
        self._dispatch_limit = 0

    def _serialization_lock(self) -> "asyncio.Lock":
        """The shared reconcile-serialization lock, created lazily on first use so it binds to the
        running event loop. `main._serve` injects one shared with the idle-tick publisher."""
        if self._reconcile_lock is None:
            self._reconcile_lock = context.new_reconcile_lock()
        return self._reconcile_lock

    def _dispatch_bound(self) -> asyncio.Semaphore:
        """The upper bound on commands this bridge handles CONCURRENTLY, so a flood of slow provisioning
        subprocesses cannot exhaust host resources; excess queues here, never on the message loop.

        `node_max_concurrent_commands` (`node_settings`), read per command: a changed value builds a fresh
        semaphore, and commands already holding or queued on the old one finish there, so for one drain
        the two bounds overlap."""
        limit = node_settings.get("node_max_concurrent_commands")
        if self._dispatch_sem is None or limit != self._dispatch_limit:
            self._dispatch_sem = asyncio.Semaphore(limit)
            self._dispatch_limit = limit
        return self._dispatch_sem

    async def start(self) -> None:
        """Bind the concurrency bound inside the running loop, not `__init__`. Optional: each command binds
        it too."""
        self._dispatch_bound()

    async def stop(self) -> None:
        # Cancel + drain any command handlers still in flight so a teardown never leaks a running task
        # (or a half-done mutation) past the bridge's lifetime.
        pending = list(self._inflight) + list(self._background)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def deliver(self, msg) -> None:
        """Hand one request in and return immediately — the bridge's only entry point.

        MUST NOT await the handler: the WebSocket read loop calls this inline, so awaiting would let one
        slow command head-of-line-block every other command on this node and starve the reconcile loop.
        Concurrency is safe — `reconcile` self-serializes via `_serialization_lock`. `msg` need only
        expose `.data` and an awaitable `.respond(bytes)`, which is what keeps this ladder
        transport-blind.
        """
        task = asyncio.create_task(self._serve_one(msg))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _serve_one(self, msg) -> None:
        """Handle one command under the concurrency bound. The semaphore is acquired HERE, inside the
        task rather than in `deliver`, so an exhausted bound queues the excess without blocking the
        message loop. A handler fault is caught so it neither sinks silently as an unretrieved task
        exception nor leaves the requester hanging to its client timeout."""
        # Resolved here, not in `deliver`: a WS frame can arrive before `start()` on a NATS-less daemon.
        async with self._dispatch_bound():
            try:
                await self._on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reply "failed" so the requester never hangs
                log.exception("command dispatch crashed: %s", exc)
                try:
                    await msg.respond(_reply("failed", {"reason": f"internal error: {exc}"}))
                except Exception:
                    pass

    async def _on_message(self, msg) -> None:
        try:
            payload = json.loads(msg.data or b"{}")
        except ValueError:
            await msg.respond(_reply("failed", {"reason": "malformed request"}))
            return
        if not isinstance(payload, dict):
            await msg.respond(_reply("failed", {"reason": "malformed request"}))
            return
        if not node_frame_auth.frame_auth_ok(payload, expected_token=_expected_token):
            # Two booleans, never the values: "sent nothing" vs "nothing configured" vs "disagree".
            log.warning(
                "command request rejected: invalid/missing cp_frame_proof (proof_provided=%s, token_configured=%s)",
                bool(payload.get("cp_frame_proof")),
                bool(_expected_token()),
            )
            await msg.respond(_reply("rejected", {"reason": "invalid token"}))
            return

        command_type = payload.get("command_type")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        if not isinstance(command_type, str) or not command_type:
            await msg.respond(_reply("failed", {"reason": "missing command_type"}))
            return
        args = await self._project_from_id(args)

        if command_type in MUTATION_COMMANDS:
            # Worktree-pool state mutation, served natively — not via the generic execute path.
            out = await self._handle_mutation(command_type, args)
        else:
            # An unregistered command_type is a caller error, answered in-process, never a subprocess.
            handler = NATIVE_HANDLERS.get(command_type)
            if handler is None:
                out = {"status": "failed", "result": {"reason": f"unknown command_type: {command_type}"}}
            else:
                out = await self._execute_native(handler, command_type, args)
            self._fire_post_reconcile(command_type, args, out)   # in-process, non-blocking
        await msg.respond(json.dumps({"v": _BUS_SCHEMA_VERSION, **out}).encode())

    def _fire_post_reconcile(self, command_type: str, args: dict, out: dict) -> None:
        """Schedule the daemon's OWN follow-up reconcile after a completed command, on this loop, and
        return without waiting for it. Decoupled from the reply because it is idempotent and the ambient
        projection tick covers the same ground a few seconds later — this only brings the console's
        refresh forward.

        In-process (a node never commands itself through the CP). A pass already holding the lock answers
        "already in progress"; failures are logged, not raised. Outside a running loop it logs and skips."""
        if out.get("status") != "done":
            return
        if command_type.startswith("stack_") or command_type in ("sync_develop", "rename_tmux"):
            proj = args.get("project")
            num = args.get("number")
            if proj is None or num is None:
                return
            reconcile_args: dict = {"project": proj, "number": num}
        elif command_type in _ACCOUNT_RECONCILE_COMMANDS:
            # Nudge the probe so the Add-Account wizard's success check (a `nodes` array on
            # `/api/resources`) does not wait out the account-probe interval. The roster is invalidated
            # directly: the nudge may be None, and the process-wide cache would otherwise read a
            # just-added account as "unknown account config_dir".
            accounts_probe.invalidate_account_roster()
            if self._account_probe_nudge is not None:
                try:
                    self._account_probe_nudge()
                except Exception as exc:  # noqa: BLE001 — a failed nudge must not fail the command
                    log.warning("account-probe nudge failed after %s: %s", command_type, exc)
            reconcile_args = {}   # full sweep (no project/number)
        else:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("post-exec reconcile after %s skipped: no running event loop", command_type)
            return
        task = loop.create_task(self._post_exec_reconcile(command_type, reconcile_args))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _post_exec_reconcile(self, command_type: str, args: dict) -> None:
        """The body of one `_fire_post_reconcile` task: run it, and LOG (never raise) whatever it
        answers — no caller is waiting, so an exception here would only surface as an unretrieved task
        error at shutdown."""
        try:
            out = await self._reconcile(args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a background follow-up, nothing upstream to fail
            log.warning("post-exec reconcile after %s crashed: %s", command_type, exc)
            return
        if out.get("status") != "done":
            log.warning(
                "post-exec reconcile after %s did not complete: %s",
                command_type, (out.get("result") or {}).get("reason", out),
            )

    # ── Worktree-pool state mutation ────────────────────────────────────────────────────────────────
    async def _project_from_id(self, args: dict) -> dict:
        """`args` with `project` set to the roster's name for `args['project_id']` (C5), once, before any
        handler: every handler below matches the pool roster on the name, which the node's one org keeps
        unique, so the id the CP addresses by and the name the roster is keyed by agree here.

        No `project_id` (an older CP), an unavailable roster, or an id the roster does not name leaves the
        CP's `project` as sent — the handlers then refuse exactly as they would for an unknown name."""
        project_id = args.get("project_id")
        if not isinstance(project_id, int) or isinstance(project_id, bool):
            return args
        tick = await tick_wire.fetch_pool_tick(self._node_id)
        if tick.ok:
            for name, proj in tick.projects.items():
                if proj.project_id == project_id:
                    return {**args, "project": name}
        return args

    async def _sensed_record(self, project, number) -> dict | None:
        """One slot's SENSED record from the Postgres SSOT, filtered node-side.

        `None` for absent AND unreachable — fine for aiming a best-effort kill ("no target" = skip). NOT the
        live-session guard: `_worktree_live_session` must tell those two apart."""
        try:
            return await asyncio.to_thread(
                control_plane_api.agent_api_worktree, str(project), number, self._node_id or "",
            )
        except Exception as exc:  # noqa: BLE001 — a best-effort kill-target read, never a hard failure
            log.warning("sensed-record read failed for %s #%s: %s", project, number, exc)
            return None

    async def _roster_pool(
        self, project: str,
    ) -> "tuple[worktree_mechanics_mod.PoolConfig, dict[int, dict[str, int]], tuple[int, int] | None]":
        """This project's pool config, the CP's decided port band per slot, AND the operator's tier-2 auto
        range — from ONE roster read, the one configuration source. Raises rather than return a
        partial config.

        The band and auto range ride the same tick: a copy on `args`, or a daemon default, would
        give one decision two answers. `None` auto range = keep the documented default. A slot the roster
        does not name has no band, which `apply_port_band` refuses. NOT cached: a stale roster would
        let `create` pick a slot the CP no longer declares."""
        tick = await tick_wire.fetch_pool_tick(self._node_id)
        if not tick.ok:
            raise RuntimeError(
                "the CP pool-tick roster is unavailable, and it is the only pool-config source (R4) — "
                "refusing to act on unverified pool configuration"
            )
        roster_project = tick.projects.get(project)
        if roster_project is None:
            raise RuntimeError(
                f"project {project!r} is not in this node's CP pool roster "
                f"(roster has: {', '.join(sorted(tick.projects)) or 'nothing'})"
            )
        bands: dict[int, dict[str, int]] = {}
        for slot in tick.slots:
            if slot.project != project:
                continue
            try:
                bands[int(slot.number)] = dict(slot.ports)
            except (TypeError, ValueError):
                # Never key a band under a guessed number.
                continue
        return roster_project.pool_config(), bands, tick.auto_ports

    async def _roster_pool_config(self, project: str) -> "worktree_mechanics_mod.PoolConfig":
        """`_roster_pool`'s config half, for the verbs that touch no ports (retire/release)."""
        config, _bands, _auto_ports = await self._roster_pool(project)
        return config

    async def _worktree_live_session(self, project, number) -> str | None:
        """Real-time live-session guard for a slot's worktree — the backstop against a reap killing a
        LIVE runner. A FRESH `claude agents` probe, never the per-pass cache.

        FAILS CLOSED: a probe crash, a blind probe (`agents_ok == False`), or an unanswerable path
        lookup returns the truthy `"unknown"` sentinel, so the caller refuses rather than risk killing
        a runner it cannot prove is idle. `None` (safe) ONLY when the record carries no worktree path
        or the probe POSITIVELY confirms no live session.

        Reads the whole-pool client because `agent_api_worktree` returns `None` for both "absent" (safe)
        and "unreachable" (refuse); `agent_api_pool_worktrees` keeps that split. Do not "simplify" it."""
        # `None` = could not ask. Fail closed.
        records = await asyncio.to_thread(
            control_plane_api.agent_api_pool_worktrees, self._node_id or ""
        )
        if records is None:
            log.warning(
                "live-session guard could not read the pool roster for %s #%s — treating as LIVE",
                project, number,
            )
            return "unknown"
        wanted = str(number)
        wt = ""
        for record in records:
            if record.get("project") == str(project) and str(record.get("number")) == wanted:
                wt = record.get("path") or ""
                break
        if not wt:
            # The API answered and there is no path on record: nothing on disk a reap could kill.
            return None
        ctx = context.ReconcileContext(console_live_probe=self._console_live_probe)
        try:
            agents_ok, agents_json, cwds = await ctx.agents()
        except Exception as exc:  # noqa: BLE001 — a probe fault MUST fail closed (treat as live), never open
            log.warning("live-session guard probe crashed for %s #%s: %s — treating as LIVE", project, number, exc)
            return "unknown"
        if not agents_ok:
            # Blind/partial probe — fail closed.
            return "unknown"
        if context.live_session_in(wt, cwds):
            return context.wt_live_session_id(wt, agents_json) or "live"
        return None

    async def _handle_mutation(self, command_type: str, args: dict) -> dict:
        handlers = {
            "release": self._release,
            "retire": self._retire,
            "reconcile": self._reconcile,
            "set_title": self._set_title,
            "create": self._create,
        }
        handler = handlers[command_type]   # in MUTATION_COMMANDS by the _on_message guard
        try:
            return await handler(args)
        except _MutationRejected as exc:
            return {"status": "failed", "result": {"reason": exc.reason}}
        except Exception as exc:  # noqa: BLE001 — a mutation must fail LOUDLY (D1), never wedge the bridge
            log.warning("mutation %s failed: %s", command_type, exc)
            return {"status": "failed", "result": {"reason": f"{command_type} failed: {exc}"}}

    async def _stamp_last_used(self, project: str, number: object, project_id: object = None) -> None:
        """Advance this slot's `worktrees.last_used` to now — BEST EFFORT, never a verb's verdict.

        `POST /api/agent/slot-holder` is that column's only writer (see `control_plane_api.set_slot_holder`),
        so without this call `age_last_used_secs` never advances and the CP's idle reads are inert.

        It cannot fail a verb: the hold is the CP's own, so failing a release that released (and skipping
        its tmux teardown) over a timestamp would be worse than a stale `last_used`. `mutated == 0` (the
        sensor never saw this slot) is logged with the same weight."""
        try:
            response = await asyncio.to_thread(
                control_plane_api.set_slot_holder,
                self._node_id or "", str(project), number, last_used=env_helpers.now_iso(),
                project_id=project_id,
            )
        except Exception as exc:  # noqa: BLE001 — a cosmetic stamp must never decide a mutation's outcome
            log.warning(
                "could not stamp last_used for %s #%s (%s: %s) — the mutation itself stands; the column "
                "keeps its previous value and `age_last_used_secs` lags by one cycle",
                project, number, type(exc).__name__, exc,
            )
            return
        if isinstance(response, dict) and response.get("mutated") == 0:
            log.warning(
                "last_used stamp for %s #%s touched no row — %s",
                project, number, _no_sensed_record_reason(str(project), str(number)),
            )

    async def _roster_slot(self, project: str, number: object):
        """This slot's roster entry, or None when the roster does not carry it.

        The node's only source for the CP's view of a slot. An unavailable roster RAISES: a blind read must
        never report `free` to an occupancy guard."""
        tick = await tick_wire.fetch_pool_tick(self._node_id)
        if not tick.ok:
            raise _MutationRejected(
                f"cannot resolve the CP's view of {project} #{number}: the pool-tick roster is "
                "UNAVAILABLE, and it is the only source for a slot's state — refusing to act on an "
                "unverified slot"
            )
        for slot in tick.slots:
            if slot.project == str(project) and slot.number == str(number):
                return slot
        return None

    async def _set_title(self, args: dict) -> dict:
        """Set a slot's STICKY title. The title is user INTENT, not observed state, so it is written
        DIRECTLY and never captured by reconcile from a live window. An empty/omitted title is a
        deliberate "reset to default", stored as `null`.

        No roster read: `mutated == 0` on the write itself says the slot does not exist."""
        project = args.get("project")
        number = args.get("number")
        if not project or number is None:
            return {"status": "failed", "result": {"reason": "set_title needs project + number"}}
        title = args.get("title") or ""   # empty ⇒ clear the override to null

        try:
            response = await asyncio.to_thread(
                control_plane_api.set_slot_holder,
                self._node_id or "", str(project), number, title=title,
                project_id=args.get("project_id"),
            )
        except Exception as exc:  # noqa: BLE001 — a title nobody recorded must be reported, never assumed
            return {"status": "failed", "result": {"reason": (
                f"the control-plane API refused/could not record title for {project} #{number} "
                f"({type(exc).__name__}: {exc}) — nothing was changed"
            )}}
        # No row touched: a FAILED verb, or the next `rename_tmux` renames a live window to a title no row
        # carries.
        if isinstance(response, dict) and response.get("mutated") == 0:
            return _no_sensed_record("set_title", str(project), str(number))
        # A dropped title is a FAILED verb (safety): the CP answers 200 with `title_rejected`, but the
        # names it refuses are the ones `console.py` resolves windows BY — a swallowed 200 lets the next
        # `rename_tmux` + Console open spawn a SECOND agent into an occupied worktree.
        rejected = (response or {}).get("title_rejected") if isinstance(response, dict) else None
        if rejected:
            return {"status": "failed", "result": {"reason": (
                f"the control plane refused the title for {project} #{number}: {rejected} — nothing was "
                f"changed, and the live tmux window was NOT renamed"
            )}}
        return {"status": "done", "result": {"project": project, "number": number, "title": title or None}}

    async def _release(self, args: dict) -> dict:
        """Tear down the slot's live session, best-effort killing the tmux window by the slot's persisted
        `tmux_target` — never a bare kill. NO git reset/clean, NO detach, NO parking: the next lease resets.

        This verb does NOT free the hold: `worktrees.state` is the CP's (`slot_state.release`/`reap_stale`),
        and a `release_slot` intent means "the CP already freed this, catch your session up". What is left
        is the node-only half: kill the window, stamp `last_used`. `session_id` is accepted but fences
        nothing — the CP's `slot_state` guard upstream is the fence."""
        project = args.get("project")
        number = args.get("number")
        if not project or number is None:
            return {"status": "failed", "result": {"reason": "release needs project + number"}}
        force = bool(args.get("force", False))

        current = await self._sensed_record(project, number)
        # No occupancy guard (module policy). The live-session probe protects WORK; `--force` is its escape
        # hatch. `--merged` does NOT bypass it (a git fact, not liveness). Fail-closed on an unknown probe.
        if not force:
            live_sid = await self._worktree_live_session(project, number)
            if live_sid:
                return {"status": "rejected", "result": {"reason": (
                    f"{project} #{number} has a LIVE session ({live_sid}) in its worktree — release would "
                    "kill it; pass --force to deliberately reap it once you've confirmed it's not needed"
                )}}
        target = (current or {}).get("tmux_target")

        # Stamp before the kill (not a gate). Late-hook protection is the probe above plus
        # `expect_cwd` below. Accepted residual: a re-lease between here and the kill could renumber the
        # stale index onto a new runner's window in the SAME worktree, which `expect_cwd` cannot tell apart.
        await self._stamp_last_used(project, number, args.get("project_id"))

        slot_path = str((current or {}).get("path") or "")
        if target and not slot_path:
            # No path: the kill cannot be identity-checked (empty `expect_cwd` = unverified). Skip it.
            log.warning(
                "release: tore down %s #%s but did NOT kill window %s — the record carries no worktree "
                "path to verify the kill against, and an unverified index kill is the 2026-08-14 cascade",
                project, number, target,
            )
        elif target:
            # Best-effort. `expect_cwd` pins the kill to THIS worktree: a persisted `session:index` goes
            # stale under renumbering and once killed neighbouring consoles.
            await self._kill_window(str(target), expect_cwd=slot_path)
            # The account's session died with the window; the agents cache must not serve it as live.
            env_helpers.mark_agents_dirty((current or {}).get("account"))

        # The tree stays on its branch. Detaching here was a deadlock: a slot belongs to its task, the
        # task returns to THIS slot, and `fleet_decide` refuses a slot whose tree reports another branch.
        # The next lease's `reset_to_base` handles a stale checkout.

        # No `holder` in the result: the node does not decide occupancy. `ctl.py::_mutate` reads only
        # `status`/`reason`/`path`.
        return {"status": "done", "result": {"project": project, "number": number, "released": True}}

    async def _retire(self, args: dict) -> dict:
        """Retire a RESOLVED fleet slot: `worktree_mechanics.lifecycle.retire_mechanics` refuses a
        non-`fleet/(issue|task)-N` branch and detaches the tree to a fresh `origin/<base>`, leaving the
        branch ref recoverable. Live-session guarded exactly like `_release`, with NO `--force` escape
        hatch, and the main clone refused outright.

        Like `_release`, it does not end the hold. The order below keeps the destructive step behind every
        refusal; a mechanics failure leaves the tree untouched and reports `failed`."""
        project = args.get("project")
        number = args.get("number")
        if not project or number is None:
            return {"status": "failed", "result": {"reason": "retire needs project + number"}}

        # Refuse the main clone categorically, before the mechanics: slot 0's tree is the project's main
        # clone directory. `is_main` (the roster's attribute), not `number == 0`; an unavailable roster
        # RAISES. A slot the roster lacks is not refused here — the probe and `ResetRefused` cover it.
        try:
            slot = await self._roster_slot(project, number)
            if slot is not None and slot.is_main:
                raise _MutationRejected(
                    f"{project} #{number} is the project's MAIN CLONE — retire detaches the slot's tree "
                    f"to a fresh origin/<base>, and doing that to the main clone is never what a retire "
                    f"means"
                )
        except _MutationRejected as exc:
            return {"status": "failed", "result": {"reason": exc.reason}}
        # The SAME real-time probe `_release` uses, fail-closed on an unknown result: a retire
        # must never reap a live runner's tree.
        live_sid = await self._worktree_live_session(project, number)
        if live_sid:
            return {"status": "rejected", "result": {"reason": (
                f"{project} #{number} has a LIVE session ({live_sid}) in its worktree — retire would kill "
                "it; a resolved-slot retire is only ever for a slot whose issue is already CLOSED and idle"
            )}}

        # A `ResetRefused` must leave the slot intact.
        try:
            config = await self._roster_pool_config(str(project))
        except Exception as exc:  # noqa: BLE001 — an unresolvable roster config is a loud retire failure
            return {"status": "failed",
                    "result": {"reason": f"retire: could not resolve pool config for {project!r}: {exc}"}}

        try:
            await asyncio.to_thread(lifecycle.retire_mechanics, config, number)
        except core.ResetRefused as exc:
            return {"status": "failed", "result": {"reason": str(exc)}}
        except Exception as exc:  # noqa: BLE001 — any other mechanics fault is a loud per-task failure
            return {"status": "failed", "result": {"reason": f"retire-mechanics failed: {exc}"}}

        # The hold is the CP's; only the best-effort `last_used` stamp remains.
        await self._stamp_last_used(project, number, args.get("project_id"))

        return {"status": "done", "result": {
            "project": project, "number": number, "retired": True,
        }}

    async def _reconcile(self, args: dict) -> dict:
        """Serialize against the idle-tick projection (and any overlapping `reconcile`), then reconcile.

        A reconcile already in flight RETURNS "already in progress" promptly rather than waiting, which
        could hang a bus request past the CP's command timeout — and the in-flight pass already covers
        the caller's intent. Reported as `status: done` so the caller treats it as success, not a daemon
        rejection.

        Callers: the CP's bus `reconcile` verb and the daemon's own in-process triggers. No runner reaches
        it (a runner is refused every pool verb)."""
        lock = self._serialization_lock()
        if lock.locked():
            return {"status": "done", "result": {"skipped": "reconcile already in progress"}}
        async with lock:
            return await self._reconcile_locked(args)

    async def _reconcile_locked(self, args: dict) -> dict:
        """OBSERVE slots and publish. `project`+`number` observes one slot; no args runs a full sweep.

        Sensor-only. An unreachable CP is a LOUD failure here, unlike the idle tick's silent
        freeze, because something asked for THIS pass."""
        project = args.get("project")
        number = args.get("number")

        tick = await tick_wire.fetch_pool_tick(self._node_id)
        if not tick.ok:
            return {"status": "failed", "result": {"reason": (
                "the CP pool-tick roster is unavailable — it is the authoritative slot set (R4), so "
                "there is nothing to reconcile against"
            )}}

        # No lease wiring: the CP is the sole lease authority.
        ctx = context.ReconcileContext(
            console_live_probe=self._console_live_probe,
            node_id=self._node_id,
            # Shared with the idle-tick publisher — see `__init__`.
            agents_cache=self._agents_cache,
        )
        # READS THE CACHE, NEVER REFRESHES IT: `refresh_agents_cache` drains `take_agents_dirty()`, eating
        # the mark `fleet_dispatch.launch_runner` leaves for a just-spawned runner — the account would read
        # session-free until the hourly scan, and `observe_slot`'s orphan reap would SIGKILL the runner's
        # reparented processes. Refreshing is the projection tick's job alone. Cost: this pass may decide
        # on a snapshot up to ~1h old for an account neither leased nor dirty; the reap's own exclusions
        # bound that, and the input is not self-inflicted.

        if project and number is not None:
            slot = next(
                (s for s in tick.slots if s.project == str(project) and s.number == str(number)), None
            )
            if slot is None:
                return {"status": "failed", "result": {"reason": (
                    f"slot {project} #{number} is not in this node's CP pool roster"
                )}}
            # A fresh ctx has an empty `fetched` memo; without the age every call forks `git fetch`.
            project_cfg = tick.projects.get(slot.project)
            observation = await orchestrate.reconcile_slot(
                slot, ctx,
                fetch_stamp_age_secs=project_cfg.fetch_stamp_age_secs if project_cfg else None,
            )
            if observation is None:
                return {"status": "failed", "result": {
                    "reason": f"worktree {slot.project} #{slot.number} does not exist on disk"}}
            return {"status": "done", "result": {
                "project": slot.project, "number": slot.number, "observation": observation}}

        # Full sweep: one agents probe feeds slots and runner enumeration; ONE observation POST carries
        # `runners_complete`, so the CP advances `nodes.runners_updated_at` from evidence.
        await ctx.agents()
        await orchestrate.reconcile_pass(node_id=self._node_id, ctx=ctx, tick=tick)
        return {"status": "done", "result": {"posted": True}}

    async def _create(self, args: dict) -> dict:
        """Materialize a DECLARED-but-unbuilt worktree slot — `worktree_mechanics.lifecycle.create_mechanics` does
        the git work ONLY and the daemon writes none of the record it returns (never the user-facing
        `create` verb, which forwards HERE — recursion).

        Busy slots from the roster are passed as `exclude`. `number` names a declared row, not an
        allocation. Bands are a per-slot map because with no number the slot is chosen inside
        `create_mechanics`."""
        project = args.get("project")
        if not project:
            return {"status": "failed", "result": {"reason": "create needs project"}}

        exclude = await self._held_slots(str(project))

        # The roster's declared rows bound `create_mechanics`' scan.
        try:
            config, bands, auto_ports = await self._roster_pool(str(project))
        except Exception as exc:  # noqa: BLE001 — an unresolvable roster config is a loud create failure
            return {"status": "failed",
                    "result": {"reason": f"create: could not resolve pool config for {project!r}: {exc}"}}

        def _mechanics() -> dict:
            return lifecycle.create_mechanics(
                config, number=args.get("number"), exclude=exclude, node_id=self._node_id,
                bands=bands,
                # The operator's tier-2 range, off the same tick as the bands.
                auto_ports=auto_ports,
            )

        try:
            record = await asyncio.to_thread(_mechanics)
        except core.ResetRefused as exc:
            # Pool-full or git failure — loud.
            return {"status": "failed", "result": {"reason": str(exc)}}
        except Exception as exc:  # noqa: BLE001 — any other mechanics fault is a loud per-task failure
            return {"status": "failed", "result": {"reason": f"create-mechanics failed: {exc}"}}

        number = record.pop("number", None)
        if number is None:
            return {"status": "failed", "result": {"reason": "create-mechanics record missing 'number'"}}
        number = str(number)

        # No write: slot EXISTENCE has one author, the sensor; a create-minted `path`-less row once wedged
        # a slot number permanently.
        return {"status": "done", "result": {
            "project": project, "number": number, "path": record.get("path"),
        }}

    async def _held_slots(self, project: str) -> list[str]:
        """Slot numbers for `project` the CP says something is IN — the set `create_mechanics` is told to
        skip. `OCCUPIED_SLOT_STATES`, a positive test, not `!= "free"`: an undescribed slot's `""` must
        not read as busy. An unfetchable roster RAISES — 'nothing busy' from a blind read double-leases."""
        tick = await tick_wire.fetch_pool_tick(self._node_id)
        if not tick.ok:
            raise RuntimeError(
                f"held-slot scan for {project!r}: the CP pool-tick roster is UNAVAILABLE, and it is the "
                "only source for a slot's occupancy — refusing to report 'nothing busy' from a blind "
                "read, which would double-lease a live slot"
            )
        return [
            slot.number for slot in tick.slots
            if slot.project == project and slot.state in OCCUPIED_SLOT_STATES
        ]

    async def _kill_window(self, target: str, expect_cwd: str = "") -> None:
        """Best-effort teardown of a released slot's tmux window; never fails a release. `expect_cwd`
        refuses a window not living in that worktree.

        Via `kill_gates.kill_session`, not raw `tmux kill-window`: after the uid switch a window may be on
        either of two servers, where a `session:index` names different windows; it also passes the
        tmux-safe env. Off the event loop."""
        await asyncio.to_thread(
            kill_gates_mod.kill_session, target, expect_cwd=expect_cwd, reason="release teardown",
        )

    # ── Native in-process execute path (strangler) ────────────────────────────────────────────────
    async def _execute_native(self, handler: NativeHandler, command_type: str, args: dict) -> dict:
        """Run a `NATIVE_HANDLERS`-registered command family IN-PROCESS, applying the standard wrapper
        (test short-circuit → occupancy guard → shell-out → exit_code → status) in a worker thread, so blocking subprocess calls never stall the message loop.

        Returns the reply only and pushes nothing to the CP: a family wanting an IMMEDIATE refresh must
        arrange it via `_fire_post_reconcile`."""
        if args.get("_test"):
            return {"status": "done", "result": {"skipped": "test command"}}
        guard_reason = await self._guard_check(command_type, args)
        if guard_reason:
            log.info("command rejected by guard: %s", guard_reason)
            return {"status": "rejected", "result": {"reason": guard_reason}}
        result = await asyncio.to_thread(handler, command_type, args, _native_run)
        if result.get("exit_code", -1) == 0:
            return {"status": "done", "result": result}
        # The console needs `result.reason`; a handler's own `reason` still wins.
        return {"status": "failed", "result": {**_native_failure_reason(result), **result}}

    async def _guard_check(self, command_type: str, args: dict) -> str | None:
        """Node-side occupancy check for the native execute path: a refusal reason, or None.

        Reads `worktrees.state`; see `GUARDED_COMMAND_TYPES` for why the CP's own Delete passes.

        FAIL-OPEN so a CP blip cannot block every operator command — unlike `_retire`/`_held_slots`, which
        fail CLOSED because guessing wrong there destroys a live runner's work; this only re-checks a
        decision the CP already made."""
        if command_type not in GUARDED_COMMAND_TYPES:
            return None
        project = args.get("project")
        number = args.get("number")
        if project is None or number is None:
            return None
        try:
            slot = await self._roster_slot(project, number)
        except Exception as exc:  # noqa: BLE001 — fail-open: an unreadable roster must not block operators
            log.warning("guard-check roster read failed for %s #%s: %s — failing open", project, number, exc)
            return None
        state = (slot.state if slot else "") or ""
        if state in OCCUPIED_SLOT_STATES:
            return (
                f"{project} #{number} is {state!r}, not free — the control plane holds this slot, and "
                f"{command_type} would act on a tree it is using (occupancy guard)"
            )
        return None

# ── Ported native handler families (strangler) ──────────────────────────────────────────────────────
# Side-effect imports (registration), LAST so `from commands import native_command` resolves against a
# fully-defined module. A family lands by ADDING a line here.
import stack_ops as _stack_ops  # noqa: E402,F401 — registers stack_up/down/restart/rebuild
import worktree_ops as _worktree_ops  # noqa: E402,F401 — registers rename_tmux/logs/sync_develop/open_finder/open_cursor/run/dispatch/check + worktree_erase
import fleet_windows as _fleet_windows  # noqa: E402,F401 — registers fleet_windows (a READ, never a MUTATION_COMMANDS member)
import bedrock_close as _bedrock_close  # noqa: E402,F401 — registers jaah.close_session (K10, end a Jaah session's panes)
import nodevault_commands as _nodevault_commands  # noqa: E402,F401 — registers the six nodevault.* verbs (§4.3)
# The cloud node's families (node_update, gha_runners_apply, nodelink_converge), discovered rather than imported:
# the community edition's tree leaves them out (`cloud_node.py`).
import cloud_modules as _cloud_modules  # noqa: E402
_cloud_node = _cloud_modules.load("cloud_node")
