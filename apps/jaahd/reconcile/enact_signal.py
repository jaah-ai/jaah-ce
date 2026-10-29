"""Executors aimed at a live runner or a slot's end: kill, deliver, runtime control, erase, destroy."""
from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path

import cloud_modules
import host_platform
from env_helpers import mark_agents_dirty
from reconcile import tick_wire, context, enact_reset
from reconcile.vocab import OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_REFUSED_PRECONDITION, OUTCOME_REFUSED_STALE
from reconcile.tick_wire import RosterSlot, SLOT_TASK, SLOT_USER, SlotIntent
from reconcile.context import ReconcileContext

log = logging.getLogger(__name__)


# Projection-addressed verbs. Their gates are fail-closed safety code; each returns `(outcome, reason,
# extras)` and `execute_intents` acks.

# Module-level, not per-context: these are evidence that only means something across passes.

# intent_id -> refusal reasons already logged; a refused intent stays pending, so this stops per-tick
# re-logging.
_KILL_RUNNER_REFUSALS_LOGGED: dict[object, set[str]] = {}

_DELIVER_ANSWER_REFUSALS_LOGGED: dict[object, set[str]] = {}


def _log_refusal_once(logged: dict[object, set[str]], intent_id: object, why: str) -> bool:
    """Has this (intent, reason) been logged this run? Records it, False the first time. Only the log line
    is de-duplicated, never the ack."""
    reasons = logged.setdefault(intent_id, set())
    if why in reasons:
        return True
    reasons.add(why)
    return False


def _retire_intent_state(intent_id: object) -> None:
    """Drop every cross-pass entry for an intent that has just been retired (else a long-lived daemon leaks).

    Call only on a retiring outcome (`done` for these verbs, `control_plane_api.SLOT_INTENT_OUTCOMES`); a
    non-retiring one must keep its entry so it stays logged-once."""
    _KILL_RUNNER_REFUSALS_LOGGED.pop(intent_id, None)
    _DELIVER_ANSWER_REFUSALS_LOGGED.pop(intent_id, None)


def _refuse_once(logged: dict, kind: str, intent_id: object, what: str, why: str) -> tuple[str, str, dict]:
    """Refuse one intent: log once per (intent, reason) per run in `logged`, answer `refused_precondition`.

    Not a retirement: the CP re-serves it and the node re-evaluates — every guard relies on that, so a slot
    mid-observation recovers on its own."""
    if not _log_refusal_once(logged, intent_id, why):
        log.warning("%s: %s refused — %s. Logged once per intent per daemon run.", kind, what, why)
    return OUTCOME_REFUSED_PRECONDITION, why, {}


# A settle's `kill_runner` (or a `release_slot`) on an ACP runner: how long `_close_acp_before_kill` lets the host cancel the turn and
# flush before the sweep. Short: the pass waits on it, and the turn's result is what it is for — an engine answers
# a cancel at once; the host's own session/close and engine stop may be cut by the sweep, whose SIGTERM it flushes on.
KILL_CLOSE_WAIT_S = 6.0
KILL_CLOSE_POLL_S = 0.5


def _refuse_kill_runner(intent_id: object, what: str, why: str) -> tuple[str, str, dict]:
    """Refuse one kill intent (`_refuse_once`)."""
    return _refuse_once(_KILL_RUNNER_REFUSALS_LOGGED, "kill-intent", intent_id, what, why)


async def _enact_kill_runner(ctx: "ReconcileContext", slot: RosterSlot | None, intent: SlotIntent,
                             obs: dict | None) -> tuple[str, str, dict]:
    """Enact one `kill_runner` intent — clear the tree the CP named (`_clear_tree`), docker exempt. The hold
    stays where the CP put it: the task restarts in the same worktree.

    Addressed by `(project, number)` against this pass's projection (`_matched_record`), not `obs`: `path`,
    `state` and `task_id` are the CP's record. The node does not adjudicate the instruction; what remains
    are missing or wrong addresses. The task-binding fence is proof of address: a pending kill matches by
    slot alone, and the slot may have been re-leased. It is unconditional (a taskless intent is refused,
    not a licence), and the state fence requires `state == 'task'` specifically — a `user`-owned tree is
    never a `kill_runner`'s to sweep."""
    project, number = intent.project, intent.number
    intent_id = intent.id   # the ack's correlation key, and the log de-dup key below
    task_id = intent.payload.get("task_id")
    task = str(task_id) if task_id is not None else ""
    what = f"kill_runner on {project} #{number}" + (f" (task {task})" if task else "")

    records = await ctx.worktree_records()
    if records is None:
        # No projection, no path to sweep.
        return _refuse_kill_runner(
            intent_id, what,
            "the pool projection is unreachable — nothing to sweep without a resolved worktree path",
        )

    matched = context._matched_record(records, project, number)
    if matched is None:
        return _refuse_kill_runner(
            intent_id, what, "slot absent from this pass's projection — unobserved, not empty",
        )
    # Mark the account dirty so the next tick re-probes rather than serving a swept session as live; before
    # the sweep (it only schedules an observation).
    mark_agents_dirty(matched.get("account"))

    record_path = str(matched.get("path") or "")
    record_state = str(matched.get("state") or "")
    record_task_id = matched.get("task_id")

    # No task names no address; the state fence alone would pass a `user`-owned slot too.
    if not task:
        return _refuse_kill_runner(
            intent_id, what,
            "the intent names no task — a missing address, not a licence to sweep whatever holds the slot",
        )
    # A kill intent outlives its condition: if the slot is bound to another task, retire it untouched (a
    # refusal would leave it armed).
    if str(record_task_id or "") != task:
        log.warning(
            "kill-intent: %s is STALE — the slot is now bound to %s, not this task, so the binding "
            "this intent was minted for is gone. Retiring it untouched; sweeping here would hit "
            "whatever legitimately holds the slot now.",
            what,
            f"task {record_task_id}" if record_task_id is not None else "no task",
        )
        _retire_intent_state(intent_id)
        return OUTCOME_DONE, "nothing swept: the slot is no longer bound to this task", {}
    # Only a `task`-held slot: the record is sensed, so a `free` or `user` slot can still show a live pane (an
    # operator's or human's console).
    if record_state == SLOT_USER:
        return _refuse_kill_runner(
            intent_id, what,
            "the slot is user-owned, not task-owned — a kill_runner intent may never sweep a human's "
            "own console session",
        )
    if record_state != SLOT_TASK:
        return _refuse_kill_runner(
            intent_id, what,
            f"the slot's state is {record_state or 'unknown'!r}, so nothing holds it — refusing to sweep "
            "a tree nothing here claims",
        )
    if not record_path:
        # `_clear_tree` needs a real cwd and `expect_cwd`; the projection serves `path` NOT NULL, so this is
        # malformed data.
        return _refuse_kill_runner(
            intent_id, what, "the matched record carries no worktree path to sweep",
        )
    # The launch fence: the same task relaunched into its own held slot (a Stop then Run, a retry) passes every
    # check above, so an intent minted for the predecessor would kill the successor. Retire it untouched.
    intent_generation = intent.payload.get("generation")
    if type(intent_generation) is int and await asyncio.to_thread(
        _kill_generation_is_stale, record_path, intent_generation, matched.get("generation"),
    ):
        log.warning(
            "kill-intent: %s is STALE — minted for launch generation %s, and a newer launch holds the slot "
            "now. Retiring it untouched; sweeping here would kill the successor.",
            what, intent_generation,
        )
        _retire_intent_state(intent_id)
        return OUTCOME_DONE, "nothing swept: stale generation — a newer launch holds the slot", {}

    await _close_acp_before_kill(record_path, task, what)
    await enact_reset._clear_tree(ctx, record_path, what)
    if slot is not None:
        await enact_reset._stamp_slot_last_used(ctx, slot)
    _retire_intent_state(intent_id)
    return OUTCOME_DONE, "", {}


def _kill_generation_is_stale(worktree: str, intent_generation: int, projection_generation: object) -> bool:
    """Was this kill minted for a launch the slot no longer runs? Not stale when some live pane registered in
    the tree carries the intent's generation (its target is still there, whatever else is). Otherwise stale when
    a registered pane carries a newer generation, or the CP's projection already names one — the second arm
    covers the spawn-before-register window (`fleet_dispatch.py`'s launch), when the successor has no entry
    yet. A live pane or a projection with no generation never makes it stale: the task fence alone decides."""
    import runner_registry  # noqa: PLC0415 — deferred, as `_close_acp_before_kill`

    panes = runner_registry.live_all_in_worktree(worktree)
    if any(ident.generation is None for ident in panes):
        # A pane registered before the fence names no launch: the task fence alone decides, or it is never killed.
        return False
    pane_generations = {ident.generation for ident in panes if ident.generation is not None}
    if intent_generation in pane_generations:
        return False
    if any(g > intent_generation for g in pane_generations):
        return True
    return type(projection_generation) is int and projection_generation > intent_generation


async def _close_acp_before_kill(worktree: str, task: str | None, what: str) -> None:
    """Before a `kill_runner` or a `release_slot` sweeps an ACP runner's tree: queue a `close` on its host (one
    opened for `task`; any task when None) and wait up to
    `KILL_CLOSE_WAIT_S` for that runner to end. The sweep SIGTERMs every process in the tree one by one, the
    engine included, so a runner that settled mid-turn never got its turn's result — the only place an engine
    reports the turn's tokens (measured engines answer a cancelled prompt with them: README). The close lets the host
    cancel the turn, emit that result and flush; a host still up at the deadline is swept as before, and its
    signal handler ships what it already took (`acp/host.py::Host.serve`). Never raises and never refuses: a
    legacy runner, another task's session or an unreachable hub fall straight through to the sweep."""
    import runner_registry  # noqa: PLC0415 — deferred, as `enact_launch._old_front_pane`

    try:
        ident = await asyncio.to_thread(runner_registry.live_in_worktree, worktree)
        session_id = str(getattr(ident, "session_id", "") or "")
        if not session_id:
            return
        hub = context._acp_hub()
        if not await asyncio.to_thread(hub.is_acp, session_id):
            return
        if await asyncio.to_thread(_acp_task_mismatch, hub, session_id, task):
            return
        queued = await asyncio.to_thread(hub.enqueue_control, session_id, {
            "id": f"kill-close-{session_id}", "type": "close", "payload": {}})
    except Exception as exc:  # noqa: BLE001 — the sweep below still ends the runner
        log.warning("%s: no graceful ACP close before the sweep (%s: %s)", what, type(exc).__name__, exc)
        return
    if not queued:
        return
    # Counted polls, not a clock (as `enact_launch._close_for_handoff`).
    for _ in range(int(KILL_CLOSE_WAIT_S / KILL_CLOSE_POLL_S)):
        await asyncio.sleep(KILL_CLOSE_POLL_S)
        current = await asyncio.to_thread(runner_registry.live_in_worktree, worktree)
        if current is None or str(getattr(current, "session_id", "") or "") != session_id:
            log.info("%s: ACP session %s closed before the sweep", what, session_id)
            return
    log.info("%s: ACP session %s still up after %.0fs; sweeping", what, session_id, KILL_CLOSE_WAIT_S)


def _refuse_deliver_answer(intent_id: object, what: str, why: str,
                           kind: str = "deliver-answer") -> tuple[str, str, dict]:
    """Refuse one pane-write intent (`deliver_answer`/`deliver_message`, named by `kind` in the log)."""
    return _refuse_once(_DELIVER_ANSWER_REFUSALS_LOGGED, kind, intent_id, what, why)


async def _fenced_pane(ctx: "ReconcileContext", intent: SlotIntent, what: str,
                       kind: str) -> tuple[tuple[str, str] | None, tuple[str, str, dict] | None]:
    """The session fence every pane-write verb shares: `((tmux_target, worktree_path), None)` for the pane of
    the session the payload names, or `(None, refusal)` — the ack to return instead of writing anything.

    The payload's session id is the fence, not the slot: a slot outlives its session, and a mismatch would
    inject one task's text as a trusted prompt into an unrelated runner. Mismatched and unresolved sessions
    are refused, fail-closed; the worktree path is what the delivery then proves the pane against (cwd)."""
    intent_id = intent.id
    expected_session = str(intent.payload.get("session_id") or "")
    if not expected_session:
        return None, _refuse_deliver_answer(
            intent_id, what, "the payload names no session — an unfenced write could land in a "
                             "different runner's pane", kind,
        )

    records = await ctx.worktree_records()
    if records is None:
        # No projection: the session cannot be verified, and a guess types the text into the wrong runner.
        return None, _refuse_deliver_answer(
            intent_id, what,
            "the pool projection is unreachable — it cannot identify the session this write belongs "
            "to (fail-closed)", kind,
        )

    matched = context._matched_record(records, intent.project, intent.number)
    if matched is None:
        return None, _refuse_deliver_answer(
            intent_id, what, "slot absent from this pass's projection — unobserved, not empty", kind,
        )

    observed_session = str(matched.get("session_id") or "")
    if not observed_session:
        return None, _refuse_deliver_answer(
            intent_id, what, "the slot resolved no session id — unknown rather than gone", kind,
        )
    if observed_session != expected_session:
        # Permanent: that session is gone. `refused_stale` does not retire;
        # `fleet_verdicts.purge_unexecutable_intents` disposes of it later (an answer stays durable on
        # `task_questions` for the next dispatch). Logged once.
        if not _log_refusal_once(_DELIVER_ANSWER_REFUSALS_LOGGED, intent_id, "stale session"):
            log.info(
                "%s: %s is stale — the session it belongs to is gone. Logged once per "
                "intent per daemon run.", kind, what,
            )
        return None, (OUTCOME_REFUSED_STALE, (
            f"the slot now runs session {observed_session[:8]}, not the {expected_session[:8]} this "
            f"write belongs to"
        ), {})

    tmux_target = str(matched.get("tmux_target") or "")
    record_path = str(matched.get("path") or "")
    if not tmux_target:
        return None, _refuse_deliver_answer(
            intent_id, what, "the slot carries no tmux_target — no pane resolved, which is unknown "
                             "rather than gone", kind,
        )
    if not record_path:
        # The delivery refuses without the expected worktree; say so here rather than as a delivery failure.
        return None, _refuse_deliver_answer(
            intent_id, what, "the slot record carries no worktree path to verify the pane against", kind,
        )
    return (tmux_target, record_path), None


def _bound_vm(intent: SlotIntent) -> str:
    """The MicroVM the payload's task + session live in, or "" — a pane-write's VM branch. The CP's ids are
    the fence, exactly as for a pane (`microvm_registry.bound_to`); a host that runs no MicroVMs (Windows) has none."""
    microvm_mark = cloud_modules.load("microvm_mark")
    if microvm_mark is None or not microvm_mark.host_supported():
        return ""
    microvm_registry = cloud_modules.load("microvm_registry")  # the MicroVM machinery, off the Windows import graph
    if microvm_registry is None:
        return ""

    binding = microvm_registry.find(task_id=intent.payload.get("task_id"),
                                    session_id=intent.payload.get("session_id"))
    return binding.vm_id if binding is not None else ""


def _windows_vm_intent(fields: dict) -> dict | None:
    """A VM-bound delivery on a marked Windows node, enacted by the WSL2 backend (`microvm_bridge` `vm_intent`):
    `{"ok": bool}`, `{"no_vm": True}` when no VM there is bound to the payload's task + session (the pane path
    then answers), `{"ok": False}` when the backend could not be asked. None off a marked Windows node."""
    if not host_platform.is_windows():
        return None
    bridge = cloud_modules.load("microvm_bridge")
    client = bridge.windows_client() if bridge is not None else None
    if client is None or not bridge.windows_marked():
        return None
    reply = client.vm_intent(fields)
    if reply is None:
        return {"ok": False}
    if reply.get("op") == "error":
        return {"no_vm": True} if reply.get("code") == "no_vm" else {"ok": False}
    return {"ok": reply.get("ok") is True}


def _vm_delivery_outcome(intent_id: object, what: str, delivered: object, payload: str) -> tuple[str, str, dict]:
    """The ack for a VM delivery — `deliver-answer`'s rule: `done` only on a submitted write."""
    if delivered is not True:
        log.warning("deliver-%s: %s did not confirm into its VM (result=%s) — retrying next pass",
                    payload, what, delivered)
        return OUTCOME_FAILED, f"the {payload} was not submitted into the VM console (result={delivered})", {}
    _retire_intent_state(intent_id)
    return OUTCOME_DONE, "", {}


def _runner_tui(worktree: str):
    """The screen of the CLI running in `worktree`, for `answer_delivery.deliver_answer`: its adapter's
    `tui()`, or None (the default CLI's own constants) for a default-provider runner or one recorded before
    the launch registry named its provider. An unregistered provider is None too — the default path is the one
    that refuses a multi-line paste into a pane it cannot prove is a composer."""
    import agents  # noqa: PLC0415 — deferred, as the module's other provider reads
    import runner_registry  # noqa: PLC0415

    ident = runner_registry.live_in_worktree(worktree)
    provider = ident.provider if ident is not None else ""
    if not provider or provider not in agents.registered_providers():
        return None
    return agents.get_adapter(provider).tui()


async def _stage_answer_attachments(intent: SlotIntent, task_id: object) -> None:
    """Materialise an answer's attachments into the task's own workdir before the answer lands: it names
    these files by path, and the runner may read one the moment it does. Best effort, as at launch."""
    attachments = intent.payload.get("attachments")
    attachments_dir = str(intent.payload.get("attachments_dir") or "")
    # Only this task's own workdir — the one its dispatch prompt named — is ever written here.
    if (isinstance(attachments, list) and attachments and task_id is not None
            and Path(attachments_dir).name == f"task-{task_id}"):
        import fleet_dispatch  # noqa: PLC0415 — deferred: daemon-space sibling, needed only here
        await asyncio.to_thread(
            fleet_dispatch.materialize_attachments, attachments, attachments_dir, wipe=False,
        )


def _acp_task_mismatch(hub, session_id: str, task_id: object) -> bool:
    """Does the payload name a task other than the one the ACP session was opened for? The session id is the
    fence; the task id, when the payload carries one, must agree with it."""
    if task_id is None:
        return False
    record = hub.store.get(session_id) or {}
    return str(record.get("task_id", "")) != str(task_id)


def _deliver_acp_answer(hub, intent: SlotIntent, session_id: str, question: str, answer: str,
                        what: str, *, bare: bool = False) -> tuple[str, str, dict]:
    """`deliver_answer` for an ACP session: resolve its open user-input request (the named one, else the
    oldest), or queue the composed answer as the host's next prompt. Synchronous; run in a thread.

    `bare` (every question ignored): the open request is DECLINED with no answer — nothing about it is
    sent — and only with none open is `answer` (the wake word) queued, unframed."""
    import answer_delivery  # noqa: PLC0415

    intent_id = intent.id
    if _acp_task_mismatch(hub, session_id, intent.payload.get("task_id")):
        return _refuse_deliver_answer(intent_id, what, "the ACP session belongs to another task")
    request_id = intent.payload.get("request_id")
    request_id = request_id if isinstance(request_id, str) and request_id else None
    if bare:
        resolved = hub.resolve_request(session_id, request_id=None, decision="decline", answers=None,
                                       intent_id=intent_id)
    else:
        # The answer fills the request's question with this text (else its first unanswered one); a request is
        # decided once every question is answered. `intent_id` makes a re-sent intent change nothing.
        resolved = hub.resolve_request(session_id, request_id=request_id, decision="accept",
                                       answers={"answer": answer}, question=question, intent_id=intent_id)
    if not resolved:
        queued = hub.enqueue_control(session_id, {
            "id": f"answer-{intent_id}", "type": "answer",
            "payload": {"text": answer if bare else answer_delivery.compose_answer_message(question, answer)},
        })
        if not queued:
            return OUTCOME_FAILED, "the answer could not be queued for the ACP session's host", {}
    _retire_intent_state(intent_id)
    return OUTCOME_DONE, "", {}


async def _enact_runtime_control(ctx: "ReconcileContext", slot: RosterSlot | None, intent: SlotIntent,
                                 obs: dict | None) -> tuple[str, str, dict]:
    """Enact one `runtime_control` intent: queue `payload.intent` on the named ACP session's control lane.
    The session id is the whole address (no pane, no slot); a session that is not an ACP one here, or an
    intent of the wrong shape, is refused. Never log the intent's payload: a steer is an operator's words."""
    from acp import runtime as acp_runtime  # noqa: PLC0415

    intent_id = intent.id
    what = f"runtime_control on {intent.project} #{intent.number}"
    session_id = str(intent.payload.get("session_id") or "")
    control = intent.payload.get("intent")
    if not session_id:
        return _refuse_deliver_answer(intent_id, what, "the payload names no session", "runtime-control")
    hub = context._acp_hub()
    if not await asyncio.to_thread(hub.is_acp, session_id):
        return _refuse_deliver_answer(intent_id, what, "the session is not an ACP session on this node",
                                      "runtime-control")
    if (not isinstance(control, dict) or control.get("type") not in acp_runtime.INTENT_TYPES
            or not isinstance(control.get("id"), str) or not control.get("id")
            or not isinstance(control.get("payload", {}), dict)):
        return _refuse_deliver_answer(intent_id, what, "the payload's intent is not {id, type, payload}",
                                      "runtime-control")
    if await asyncio.to_thread(_acp_task_mismatch, hub, session_id, intent.payload.get("task_id")):
        return _refuse_deliver_answer(intent_id, what, "the ACP session belongs to another task", "runtime-control")
    if not await asyncio.to_thread(hub.enqueue_control, session_id, control):
        return _refuse_deliver_answer(intent_id, what, "the session's control queue refused the intent",
                                      "runtime-control")
    _retire_intent_state(intent_id)
    return OUTCOME_DONE, "", {}


async def _enact_deliver_answer(ctx: "ReconcileContext", slot: RosterSlot | None, intent: SlotIntent,
                                obs: dict | None) -> tuple[str, str, dict]:
    """Enact one `deliver_answer` intent: write the answer into the named session's pane.

    The payload's session id is the fence, not the slot: a slot outlives its session, and a mismatch would
    inject one task's answer as a trusted prompt into an unrelated runner. Mismatched and unresolved sessions
    are refused. No task state is written here: the CP flips `awaiting_response -> active` on the `done` ack.

    Never log the payload: `question`/`answer` are an operator's own words (up to `MAX_TASK_TEXT_LEN`)."""
    import answer_delivery  # noqa: PLC0415 — deferred: subprocess-heavy sibling, needed only on this verb

    project, number = intent.project, intent.number
    intent_id = intent.id
    answer = str(intent.payload.get("answer") or "")
    question = str(intent.payload.get("question") or "")
    task_id = intent.payload.get("task_id")
    what = (
        f"deliver_answer on {project} #{number}"
        + (f" (task {task_id})" if task_id is not None else "")
    )

    if not answer.strip():
        # A CP-side bug (the route 422s a blank answer); never write an empty prompt.
        return _refuse_deliver_answer(intent_id, what, "the payload carries no answer text")
    session_id = str(intent.payload.get("session_id") or "")
    # `bare`: the CP's whole message (the ignored-round wake word), sent without the `Answer: ` frame.
    bare = intent.payload.get("bare") is True
    hub = None
    if session_id:
        # Built only here, and never fatal: a hub that cannot start leaves the legacy delivery below intact.
        try:
            hub = context._acp_hub()
            if not await asyncio.to_thread(hub.is_acp, session_id):
                hub = None
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile: %s — the ACP runtime hub is unavailable (%s: %s); delivering as a pane "
                        "session", what, type(exc).__name__, exc)
            hub = None
    if hub is not None:
        # An ACP session has no composer to paste into: the answer decides its open request, else it is
        # queued as the host's next prompt. The session/task fence runs before any file is staged.
        if await asyncio.to_thread(_acp_task_mismatch, hub, session_id, task_id):
            return _refuse_deliver_answer(intent_id, what, "the ACP session belongs to another task")
        await _stage_answer_attachments(intent, task_id)
        return await asyncio.to_thread(
            functools.partial(_deliver_acp_answer, hub, intent, session_id, question, answer, what, bare=bare))
    forwarded = await asyncio.to_thread(_windows_vm_intent, {
        "kind": "deliver_answer", "task_id": task_id, "session_id": intent.payload.get("session_id"),
        "question": question, "answer": answer, **({"bare": True} if bare else {})})
    if forwarded is not None and not forwarded.get("no_vm"):
        return _vm_delivery_outcome(intent_id, what, forwarded["ok"] or None, "answer")
    vm_id = await asyncio.to_thread(_bound_vm, intent)
    message = answer if bare else answer_delivery.compose_answer_message(question, answer)
    if vm_id:
        # A MicroVM session has no pane and no slot record: its console is the target (D19).
        delivered = await asyncio.to_thread(
            answer_delivery.deliver_vm_answer, vm_id, message,
            task_id=task_id, session_id=intent.payload.get("session_id"),
        )
        return _vm_delivery_outcome(intent_id, what, delivered, "answer")
    pane, refusal = await _fenced_pane(ctx, intent, what, "deliver-answer")
    if refusal is not None:
        return refusal
    tmux_target, record_path = pane

    await _stage_answer_attachments(intent, task_id)

    delivered = await asyncio.to_thread(
        functools.partial(
            answer_delivery.deliver_answer, tmux_target, message, expect_cwd=record_path,
            tui=_runner_tui(record_path),
        )
    )
    if delivered is not True:
        # `failed` retries next pass; `done` would mark the task `active` under a runner that never got the
        # answer.
        log.warning("deliver-answer: %s did not confirm (result=%s) — retrying next pass", what, delivered)
        return OUTCOME_FAILED, f"the answer was not submitted into the pane (result={delivered})", {}
    _retire_intent_state(intent_id)
    return OUTCOME_DONE, "", {}


async def _enact_deliver_message(ctx: "ReconcileContext", slot: RosterSlot | None, intent: SlotIntent,
                                 obs: dict | None) -> tuple[str, str, dict]:
    """Enact one `deliver_message` intent: type the payload's `text` into the named session's pane.

    The same session fence and cwd proof as `_enact_deliver_answer` (`_fenced_pane`), but NOT bound to
    `awaiting_response` and never preceded by `Escape`: the recipient is a WORKING runner (e.g. mid
    `jaah-forge pr view` poll), where `Esc` would abort its turn; the default CLI queues a message typed during
    a running tool. No task state is written, here or on the CP's `done` ack.

    Never log the payload: it may carry test output from the runner's own repo."""
    import answer_delivery  # noqa: PLC0415 — deferred: subprocess-heavy sibling, needed only on this verb

    text = str(intent.payload.get("text") or "")
    task_id = intent.payload.get("task_id")
    what = (
        f"deliver_message on {intent.project} #{intent.number}"
        + (f" (task {task_id})" if task_id is not None else "")
    )
    if not text.strip():
        return _refuse_deliver_answer(intent.id, what, "the payload carries no message text",
                                      "deliver-message")
    forwarded = await asyncio.to_thread(_windows_vm_intent, {
        "kind": "deliver_message", "task_id": task_id, "session_id": intent.payload.get("session_id"),
        "text": text})
    if forwarded is not None and not forwarded.get("no_vm"):
        return _vm_delivery_outcome(intent.id, what, forwarded["ok"] or None, "message")
    vm_id = await asyncio.to_thread(_bound_vm, intent)
    if vm_id:
        delivered = await asyncio.to_thread(
            answer_delivery.deliver_vm_message, vm_id, text,
            task_id=task_id, session_id=intent.payload.get("session_id"),
        )
        return _vm_delivery_outcome(intent.id, what, delivered, "message")
    pane, refusal = await _fenced_pane(ctx, intent, what, "deliver-message")
    if refusal is not None:
        return refusal
    tmux_target, record_path = pane
    delivered = await asyncio.to_thread(
        functools.partial(
            answer_delivery.deliver_message, tmux_target, text, expect_cwd=record_path,
            tui=_runner_tui(record_path),
        )
    )
    if delivered is not True:
        log.warning("deliver-message: %s did not confirm (result=%s) — retrying next pass", what, delivered)
        return OUTCOME_FAILED, f"the message was not submitted into the pane (result={delivered})", {}
    _retire_intent_state(intent.id)
    return OUTCOME_DONE, "", {}


async def _enact_erase_tree(ctx: "ReconcileContext", slot: RosterSlot | None, intent: SlotIntent,
                            obs: dict | None) -> tuple[str, str, dict]:
    """Enact `erase_tree`: the console Delete's own node erase (`worktree_ops.worktree_erase`, with its
    containment checks), on a tree the roster no longer names. `done` once the directory is
    gone (an absent one included, so a retry converges); anything else is `failed` and re-served. Imported
    inside the function: `worktree_ops` → `commands` → `reconcile` is a cycle."""
    import commands  # noqa: PLC0415 — deferred: see the docstring's import-cycle note
    import worktree_ops  # noqa: PLC0415

    args = {key: value for key, value in intent.payload.items() if key not in ("worktree_id", "label")}
    result = await asyncio.to_thread(worktree_ops.worktree_erase, "worktree_erase", args, commands._native_run)
    if result.get("exit_code", -1) == 0:
        return OUTCOME_DONE, "", {}
    return OUTCOME_FAILED, str(result.get("stderr") or "the erase did not finish")[:2000], {}


async def _enact_destroy_slot(ctx: "ReconcileContext", slot: RosterSlot, intent: SlotIntent,
                              obs: dict | None) -> tuple[str, str, dict]:
    """Enact `destroy_slot`: kill → unpushed scan → stack down with volumes → erase, the
    mechanics in `slot_destroy`. The ack's `report` is the contract (`app/slot_destroy.py` reads it):
    `{erased, refused, killed, unpushed, detail}`.

    `done` carries every verdict the CP acts on — erased, or refused (a retry would refuse again, so the
    intent retires and the row keeps `destroy_error`). A partial failure (the down or the erase did not
    finish) is `failed`: the intent stays pending and is re-served, every step tolerating a tree a previous
    attempt — or a `reset_to_base`/`release_slot` racing it — already took apart. No projection fence: the
    row is retiring, so nothing can be leased into it, and the CP's order stands whatever holds it."""
    import slot_destroy  # noqa: PLC0415 — deferred, as the module's other verb-only siblings

    task_id = intent.payload.get("task_id")
    report: dict = {"erased": False, "refused": False, "killed": [], "unpushed": [], "detail": ""}

    def _done(outcome: str, detail: str) -> tuple[str, str, dict]:
        report["detail"] = detail
        return outcome, detail, {"report": report}

    if slot.is_main:
        report["refused"] = True
        return _done(OUTCOME_DONE, "slot 0 is the project's clone, never a destroyable slot")
    kind_refusal = tick_wire.slot_kind_mismatch(slot, intent)
    if kind_refusal:
        report["refused"] = True
        return _done(OUTCOME_DONE, kind_refusal)
    if not slot.has_repo:
        return await _destroy_folder_slot(ctx, slot, intent, obs, report, _done)
    compose_project = slot_destroy.compose_project_for(intent.payload, slot.project_id, slot.number)
    if not compose_project:
        return OUTCOME_FAILED, (
            f"payload names no compose_project p{slot.project_id}-{slot.number} (got "
            f"{intent.payload.get('compose_project')!r}) — refusing to take down a stack this slot does not own"
        ), {}

    what = f"destroy_slot {slot.project} #{slot.number}" + (f" (task {task_id})" if task_id else "")
    if obs is not None:
        swept, targets = await enact_reset._clear_tree_detail(ctx, slot.path, what)
        report["killed"] = [f"pid {r.get('pid')}: {r.get('cmd') or '?'}"[:200] for r in swept] + [
            f"tmux {t}" for t in targets]
    task_branch = f"{slot.branch_prefix}fleet/task-{task_id}" if task_id else ""
    report["unpushed"] = await asyncio.to_thread(
        slot_destroy.scan_unpushed, slot.path if obs is not None else slot.clone,
        head=obs is not None, task_branch=task_branch,
    )
    down_ok, down_detail = await asyncio.to_thread(
        slot_destroy.compose_down, compose_project, slot.path if obs is not None else slot.clone,
    )
    if not down_ok:
        return _done(OUTCOME_FAILED, down_detail)
    erase = await asyncio.to_thread(slot_destroy.erase_tree, slot.project, int(slot.number), slot.clone)
    report["erased"], report["refused"] = erase["erased"], erase["refused"]
    detail = "; ".join(part for part in (erase["detail"], down_detail) if part)
    if erase["erased"] or erase["refused"]:
        return _done(OUTCOME_DONE, detail)
    return _done(OUTCOME_FAILED, detail)


async def _destroy_folder_slot(ctx: "ReconcileContext", slot: RosterSlot, intent: SlotIntent,
                               obs: dict | None, report: dict, done) -> tuple[str, str, dict]:
    """`destroy_slot` for a FOLDER slot: kill what runs in it, then `slot_destroy.erase_folder`.

    No unpushed scan and no compose down: there is no git history to lose and no stack
    this slot owns. Never routed through `worktree_erase`: run from a folder, git would climb into an
    ancestor repository; the folder erase has its own containment checks."""
    import slot_destroy  # noqa: PLC0415 — deferred, as `_enact_destroy_slot`'s

    task_id = intent.payload.get("task_id")
    what = f"destroy_slot {slot.project} #{slot.number} (folder)" + (f" (task {task_id})" if task_id else "")
    if obs is not None:
        swept, targets = await enact_reset._clear_tree_detail(ctx, slot.path, what)
        report["killed"] = [f"pid {r.get('pid')}: {r.get('cmd') or '?'}"[:200] for r in swept] + [
            f"tmux {t}" for t in targets]
    erase = await asyncio.to_thread(
        slot_destroy.erase_folder, slot.project, int(slot.number), slot.clone, slot.project_id)
    report["erased"], report["refused"] = erase["erased"], erase["refused"]
    if erase["erased"] or erase["refused"]:
        return done(OUTCOME_DONE, erase["detail"])
    return done(OUTCOME_FAILED, erase["detail"])
