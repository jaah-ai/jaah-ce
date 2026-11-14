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
from typing import Callable

import clone_identity   # the delegated-op registry: a native git child must be admitted by the broker
import control_plane_api   # the slot-LABEL write (`POST /api/agent/slot-holder`: Title + `last_used`)
import env_helpers   # `mark_agents_dirty`: the agents cache's command-side invalidation
from jaah_env import env_get
import git_env   # `NO_REPO_CODE`: this seam pins repo-local code off on every git argv it runs
import kill_gates as kill_gates_mod   # single home for a socket-correct tmux window kill
import node_frame_auth   # single home for "is this CP->node frame authentic" (the cp_frame_proof check)
from reconcile import tick_wire, accounts_probe, context, orchestrate  # the sensing pass, reused by the `reconcile` verb
import session_user   # native git shell-outs run AS the session user (see _native_run)
from worktree_mechanics import core

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
    "release", "reconcile", "create",
})

# A little UNDER the CP's COMMAND_TIMEOUT (180s), so this daemon replies "failed" before the CP times out.
EXECUTE_TIMEOUT_SECONDS = 170.0

_BUS_SCHEMA_VERSION = 1

# Node-side occupancy policy — the NODE-side half of the double-guard (a node-bus command never
# passes the CP's HTTP guard). Occupancy is `worktrees.state`.
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
    """A mutation verb's precondition failed (e.g. an unresolvable roster).
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


def refused_reply(command_type: str, reason: str) -> dict:
    """A native handler's refusal, as the console renders it: NON-ZERO exit AND the operator-facing sentence.

    `_execute_native` maps `exit_code == 0` to `done`, so a refusal at 0 would ship as a green tick."""
    log.info("%s: refused — %s", command_type, reason)
    return {"exit_code": 1, "stdout": "", "stderr": reason, "reason": reason}


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
import restart_ops  # noqa: E402,F401 — node_restart (the console's hard restart, every edition)


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
        # Never admitted to the credential broker, as `worktree_mechanics.core._run_git`: no git on the session
        # user's tmux channel is served a login (`clone_identity`).
        return session_user.run_capped(
            list(argv), cwd=cwd, env=dict(env or {}), timeout=timeout, as_session_user=True,
            failure_rc=core.SWITCH_UNAVAILABLE_RC,
        )
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


class CommandBridge(context.ReconcileLockHolder):
    """Executes each request handed to `deliver()` IN-PROCESS via its `NATIVE_HANDLERS`-registered
    handler, replying with the JSON result.

    Owns NO transport: requests arrive by direct call from the WebSocket node-bus client.
    An unregistered command_type is answered with an in-process failure — no subprocess fallback."""

    def __init__(
        self,
        node_id: str,
        console_live_probe: Callable[[str, str], bool] | None = None,
        reconcile_lock: "asyncio.Lock | None" = None,
        account_probe_nudge: "Callable[[], None] | None" = None,
        agents_cache: "accounts_probe.AgentsProbeCache | None" = None,
    ) -> None:
        self._node_id = node_id
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
        # cancel/drain outstanding handlers. No count of concurrent commands is bounded here.
        self._inflight: set[asyncio.Task] = set()

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
        """Handle one command. A handler fault is caught so it neither sinks silently as an unretrieved
        task exception nor leaves the requester hanging to its client timeout."""
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
        if command_type.startswith("stack_"):
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
            "reconcile": self._reconcile,
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
        # The next lease's refresh (`slot_clone.prepare`) handles a stale checkout.

        # No `holder` in the result: the node does not decide occupancy. `ctl.py::_mutate` reads only
        # `status`/`reason`/`path`.
        return {"status": "done", "result": {"project": project, "number": number, "released": True}}

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
            observation = await orchestrate.reconcile_slot(slot, ctx)
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
        """`create` builds nothing: a repository slot is cloned at its lease (`lifecycle.claim_mechanics`) and a
        folder slot is made there too, so there is no declared-but-unbuilt tree to materialise ahead of one."""
        project = args.get("project")
        if not project:
            return {"status": "failed", "result": {"reason": "create needs project"}}
        return {"status": "failed", "result": {"reason": (
            f"create: nothing to build for {project!r} — a slot's tree is made at its first lease")}}

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

        FAIL-OPEN so a CP blip cannot block every operator command: this only re-checks a decision the CP
        already made."""
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
import worktree_ops as _worktree_ops  # noqa: E402,F401 — registers logs/open_finder/open_cursor/run/dispatch + worktree_erase
import fleet_windows as _fleet_windows  # noqa: E402,F401 — registers fleet_windows (a READ, never a MUTATION_COMMANDS member)
import bedrock_close as _bedrock_close  # noqa: E402,F401 — registers jaah.close_session (K10, end a Jaah session's panes)
import nodevault_commands as _nodevault_commands  # noqa: E402,F401 — registers the six nodevault.* verbs (§4.3)
# The cloud node's families (node_update, gha_runners_apply, nodelink_converge), discovered rather than imported:
# the community edition's tree leaves them out (`cloud_node.py`).
import cloud_modules as _cloud_modules  # noqa: E402
_cloud_node = _cloud_modules.load("cloud_node")
