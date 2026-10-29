"""Console Task pipeline sweeps — epic fan-out and the crash-recovery reclaimers.

Driven in the control plane by the `task-pipeline` cron (`app/task_pipeline_job.py`). Every read
and write goes through `app/task_pipeline_cp.py` in process, aliased as `control_plane_api`.

Stdlib only at import (beyond `control_plane_api`, which defers its ORM imports): the quick-tier gate
that polices this must import it from a bare host `python3`.
"""
from __future__ import annotations

import functools
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from app import alert_stamps
from app import task_pipeline_cp as control_plane_api

# `app.task_lifecycle` is stdlib-only at import, so naming a state constant here costs nothing.
from app.task_lifecycle import AWAITING_RESPONSE, SYSTEM_FAILED

# `app.persona_registry` imports only `app.task_lifecycle` and `app.task_manifest`, so the persona sets
# below can be derived from it at no import cost and without a cycle.
from app.persona_registry import (
    DB_DIRECT_SETTLE_STATES,
    DONE_CRITERION_ARTIFACT,
    DONE_CRITERION_ARTIFACT_WITH_CODERS,
    DONE_CRITERION_MERGED_PR,
    DONE_CRITERION_OPERATOR,
    DONE_CRITERION_OUTCOME,
    PERSONA_DONE_CRITERION,
    PERSONA_RECORDS,
)

# Pinned: log filters and saved searches key on the logger name.
log = logging.getLogger("jaahd.tasks")

def _task_env_int(name: str, default: int) -> int:
    """Parse an int env var at call time (a test overrides it); the module constant snapshots it once."""
    return int(os.environ.get(name, str(default)))


# State-sync backstop cadence, 20 min: the webhook drives transitions near-instantly and this poll only
# covers a missed delivery. Owned here, not shared with the fix-job family's own constant.
TASK_SWEEP_INTERVAL = _task_env_int("AGENT_TASK_SWEEP_INTERVAL", 20 * 60)

# ── The persona sets this sweep filters on, derived from `app/persona_registry.py` ────────────────────
# Each set is a question asked of the registry records, read as membership only.

# The rows the artifact reclaimer sweeps: done-on-artifact and settled DB-direct. The first
# half makes `settle_if_artifact` a legitimate recovery, the second means the stage closes itself.
# `prototyper` fails the first; `finish` fails the second and self-heals in `sweep_finish_phases`.
_ANALYSIS_PERSONAS: tuple[str, ...] = tuple(
    p.name for p in PERSONA_RECORDS
    if p.done_criterion in (DONE_CRITERION_ARTIFACT, DONE_CRITERION_ARTIFACT_WITH_CODERS)
    and p.settle_states == DB_DIRECT_SETTLE_STATES
)

# The DB-direct delivery question: personas whose own settle stores the artifact and drives
# the row to `done`. Coincides with the set above today but kept separate: a persona could settle
# DB-direct on a non-artifact criterion.
_DB_DIRECT_STAGE_PERSONAS: tuple[str, ...] = tuple(
    p.name for p in PERSONA_RECORDS if p.settle_states == DB_DIRECT_SETTLE_STATES
)

# The build rows (done-criterion: merged PR), which `reclaim_stale_active_coders` requeues when their
# runner dies unfinished. A shipped build row settled itself on its `merged_pr` terminal before its
# lease dropped, so nothing is left to settle here.
_MERGED_PR_DONE_PERSONAS: tuple[str, ...] = tuple(
    name for name, criterion in PERSONA_DONE_CRITERION.items()
    if criterion == DONE_CRITERION_MERGED_PR
)


# The rows whose only recovery is a fresh runner: they ship no PR and no stage artifact.
#   `outcome`  — close themselves on their own settle POST, which a crash loses.
#   `operator` — the `advisor` chat: the operator's next message goes into the session, so once it dies
#                the row can never advance. A fresh session beats a permanently parked row holding a slot.
# `reclaim_stale_active_restartable_tasks` is their path.
_RESTART_ONLY_PERSONAS: tuple[str, ...] = tuple(
    name for name, criterion in PERSONA_DONE_CRITERION.items()
    if criterion in (DONE_CRITERION_OUTCOME, DONE_CRITERION_OPERATOR)
)

# The subset needing the strict liveness probe: a wrong verdict costs a chat its live conversation, so it
# is judged only on a live node's affirmative "no runner here". Mirrors
# `routes/tasks/policy.py::OPERATOR_CRITERION_PERSONAS`.
_OPERATOR_DONE_PERSONAS: tuple[str, ...] = tuple(
    name for name, criterion in PERSONA_DONE_CRITERION.items()
    if criterion == DONE_CRITERION_OPERATOR
)


# The merge lane's row: a restartable run (every run takes every queued row), so a silent node is given a
# short grace (`lane_runner_held`) instead of the general one. Without it, a lane run on a starved node sat 15
# minutes before the lane could run anywhere else.
_LANE_PERSONAS: tuple[str, ...] = ("merge_lane",)


def _post_reclaim(reclaim: Callable[[], dict], task_id: object, *, sweep: str, persona: object,
                  landed: str) -> None:
    """Run a reclaim and SAY WHAT CAME BACK — the shared tail of all three `active` reclaimers.

    `sweep` is each caller's own log prefix (saved searches key on it). A helper guard narrower than the
    sweep's selection is a wedge, not a no-op, so a refusal is a WARNING naming the persona; an error
    leaves the row for a later pass without stopping the sweep. A non-dict reply costs one row."""
    try:
        answer = reclaim()
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: leave the row active for a later pass
        log.warning("%s: reclaim failed for task %s — leaving it active (no reclaim): %s",
                    sweep, task_id, exc)
        return
    if isinstance(answer, dict) and answer.get("reclaimed"):
        log.info("%s: task %s (persona %r) %s", sweep, task_id, persona, landed)
        return
    # Two causes: a helper guard narrower than the sweep's selection (a permanent wedge), or the row
    # moved since the snapshot (an ordinary race, likeliest for `outcome` rows). This side cannot tell
    # which, so the line names both.
    log.warning(
        "%s: task %s (persona %r) REFUSED its reclaim — the reclaim answered %r. Either the row settled "
        "between this pass's snapshot and the reclaim (benign, and it will not repeat), or the helper's "
        "guard is narrower than this sweep's selection — in which case the task will sit `active` with "
        "its slot bound, refused identically every pass, until that is fixed or an operator acts",
        sweep, task_id, persona, answer)


# The terminal analytical phase persona. Finish is a dispatched read-only runner that settles with a
# `finish` terminal (the server computes the report). In neither persona set above; its only self-heal
# is `sweep_finish_phases`, the lease-gated backstop for a runner that died mid-settle.
PERSONA_FINISH = "finish"

# The every-ask-captured duty lives in the runner prompts, not a server-side parser: an index-diff over
# a free-text or image epic body is defeatable, so no `coverage_verdict` is folded or forwarded here.


# Metrics capture lives on the NODE (`apps/jaahd/session_sweep.py`): it reads a runner transcript off
# the disk of the host holding the worktree, so there is no control-plane equivalent.


def reclaim_stale_active_analysis_children(lease_held: Callable[[str], bool]) -> None:
    """Lease-based `active → ready` reclaimer for a STALE epic analysis child — its ONLY always-on
    recovery, since the done-idle reap excludes it.

    On an unheld lease (`app/slot_state.py`, the CP's sole authority): settle-first, since a captured
    stage-output artifact means done; only `settled=False` falls through to `reclaim_active`.

    Fails closed: an uncertain lease read counts as held, and a settle error leaves the child `active`.
    A `system_failed` child is settle-only; `awaiting_response` is never probed, since settling it would
    answer a human's question."""
    for task in control_plane_api.list_open_tasks():
        # No `parent_task_id is None` skip: a crashed top-level analysis row must be reclaimed too. The
        # persona filter admits exactly the analysis personas and leaves a legacy decomposed coder alone.
        persona = task.get("persona")
        if persona not in _ANALYSIS_PERSONAS:
            continue  # not an analysis row (e.g. a legacy decomposed coder) — leave it
        state = task.get("state")
        if state not in ("active", SYSTEM_FAILED):
            continue  # only a stuck in-flight child (`active`) or a spent one (`system_failed`) is relevant
        task_id = task.get("id")
        if task_id is None:
            continue
        # Spent analysis child: settle-only, never reclaim. Fail closed: a settle error leaves it spent.
        if state == SYSTEM_FAILED:
            try:
                settled = control_plane_api.settle_if_artifact(task_id).get("settled")
            except Exception as exc:  # noqa: BLE001 — leave child spent for next-sweep retry, never reclaim
                log.warning("epic-child-reclaimer: settle check failed for system_failed task %s — "
                            "leaving child system_failed for next-sweep retry (no reclaim): %s",
                            task_id, exc)
                continue
            if settled:
                log.info("epic-child-reclaimer: task %s spent child SETTLED "
                         "system_failed->done on captured settled-output artifact (done-on-artifact; no "
                         "re-dispatch)", task_id)
            continue
        # Task-keyed; an analysis row has no merge-verify, so this lease read is its only liveness touch.
        try:
            held = lease_held(str(task_id))
        except Exception as exc:  # noqa: BLE001 — fail CLOSED: never reclaim on an uncertain lease read
            log.warning("epic-child-reclaimer: lease check failed for task %s — treating as HELD "
                        "(no reclaim): %s", task_id, exc)
            continue
        if held:
            continue  # a live runner still holds this task's lease — leave the child `active`
        # Settle-first (done-on-artifact). Fail closed: reclaim needs the same API the settle failed to reach.
        try:
            settled = control_plane_api.settle_if_artifact(task_id).get("settled")
        except Exception as exc:  # noqa: BLE001 — leave child active for next-sweep retry, never reclaim
            log.warning("epic-child-reclaimer: settle check failed for task %s — leaving child "
                        "active for next-sweep retry (no reclaim): %s", task_id, exc)
            continue
        if settled:
            log.info("epic-child-reclaimer: task %s stale active child SETTLED active->done on "
                     "captured settled-output artifact (done-on-artifact; no re-dispatch)", task_id)
            continue
        # The response is read so a helper refusal is audible (see `_post_reclaim`).
        _post_reclaim(lambda: control_plane_api.reclaim_active(task_id), task_id,
                      sweep="epic-child-reclaimer", persona=persona,
                      landed="stale active child reclaimed active->ready (runner gone)")


def _gone_rows(state: str, personas: tuple[str, ...] | None, gone: Callable[[str, Any], bool], *,
               sweep: str, check: str, left: str) -> Iterator[tuple[dict, Any]]:
    """`(task, task_id)` for every open row in `state` (persona in `personas`; any when None) whose runner
    `gone(task_id, persona)` says is gone — the three task-keyed reclaimers' shared head. Fails CLOSED: a probe
    that raises is logged `"<sweep>: <check> for task <id> — <left>: <exc>"` (each caller's own words, which
    saved searches key on) and reclaims nothing; a runner still there, or not affirmably gone, leaves its row."""
    for task in control_plane_api.list_open_tasks():
        persona = task.get("persona")
        if personas is not None and persona not in personas:
            continue  # another path owns this criterion
        if task.get("state") != state:
            continue  # only a stuck in-flight row is reclaimable
        task_id = task.get("id")
        if task_id is None:
            continue
        try:
            away = gone(str(task_id), persona)
        except Exception as exc:  # noqa: BLE001 — fail CLOSED: never reclaim on an uncertain read
            log.warning("%s: %s for task %s — %s: %s", sweep, check, task_id, left, exc)
            continue
        if away:
            yield task, task_id


def reclaim_stale_active_coders(
    lease_held: Callable[[str], bool],
) -> None:
    """Lease-based `active → ready` reclaimer for a stale BUILD row — a `merged_pr`-persona task whose
    runner is gone with the work unfinished.

    Settles nothing: no control-plane decision rests on Git content, and the runner settles itself once
    its PR is merged (`terminal_type: merged_pr`), so a shipped row is `done` before its lease drops.

    A dead runner is always restarted, even with an open PR or a branch ahead of base: the resumed runner
    returns to the same worktree and branch to finish it. `lease_held` (`task_pipeline_job._runner_held`)
    answers liveness directly; an uncertain read counts as held and never reclaims.
    """
    # Criterion-driven (P2 A2): only a build row is this reclaimer's to requeue; no parent requirement, since
    # a parentless `coder` is a legitimate operator-created build task. Task-keyed: nothing here asks GitHub.
    for task, task_id in _gone_rows(
        "active", _MERGED_PR_DONE_PERSONAS, lambda task_id, _persona: not lease_held(task_id),
        sweep="coder-reclaimer", check="lease check failed", left="treating as HELD (no reclaim)",
    ):
        persona = task.get("persona")
        # A row that already reported a terminal is never requeued: `settle_task` commits the reference
        # before its manifest gate can refuse, leaving `active` with a delivered PR recorded, and requeueing
        # would redo merged work. WARNING, since the row needs a human.
        if task.get("outcome_pr_url") or task.get("outcome_no_pr_reason"):
            log.warning(
                "coder-reclaimer: task %s reported a terminal (pr_url=%s no_pr_reason=%s) but is still "
                "`active` — NOT reclaiming, because its work may already have shipped. Its settle was "
                "refused (an incomplete artifact manifest is the usual cause); it needs an operator",
                task_id, bool(task.get("outcome_pr_url")), task.get("outcome_no_pr_reason"),
            )
            continue
        # Restart the runner; an open PR is the unfinished delivery it returns to (`end_attempt` keeps the
        # slot bound). The response is read so a helper refusal is a WARNING on the first tick; the row is
        # left `active` for a later pass.
        _post_reclaim(lambda: control_plane_api.reclaim_active_coder(task_id), task_id,
                      sweep="coder-reclaimer", persona=persona,
                      landed="stale active child reclaimed active->ready (nothing shipped, runner gone)")


def reclaim_silent_active_tasks() -> None:
    """The task-level stall detector: reclaim every `active` row whose runner shows no sign of life — no
    session, node sighting or transcript write — for `runner_liveness.SILENCE_THRESHOLD`
    (`control_plane_api.list_silent_active_tasks`), recording a `stalled` break. Every persona; the liveness
    probes the reclaimers above use can answer UNKNOWN forever for such a row, which is what this backstops."""
    try:
        silent = control_plane_api.list_silent_active_tasks()
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: no reclaim this pass, the sweep goes on
        log.warning("stall-detector: work-list read failed — reclaiming nothing this pass: %s", exc)
        return
    for task_id in silent:
        _post_reclaim(functools.partial(control_plane_api.reclaim_stalled, task_id), task_id,
                      sweep="stall-detector", persona=None,
                      landed="silent active row reclaimed active->ready (stalled: no session, heartbeat or "
                             "transcript write)")


def reclaim_stale_active_restartable_tasks(
    runner_held: Callable[[str], bool],
    chat_runner_confirmed_dead: Callable[[str], bool],
    lane_runner_held: Callable[[str], bool] | None = None,
) -> None:
    """The `active` reclaimer for the rows whose only recovery is a FRESH RUNNER — the `outcome` and
    `operator` criteria (`_RESTART_ONLY_PERSONAS` above).

    No artifact or terminal check: these rows ship nothing, so still `active` means the work never closed.
    Known bounded cost: `settle_task` commits an outcome note before a manifest 422, so a runner that then
    died is re-dispatched to redo it; bounded by `fleet_dispatch_cap()`. A restarted chat is a fresh
    session with the original body; it spends a dispatch attempt, so repeated deaths end `system_failed`,
    releasing its slot.

    Two probes: `runner_held` reclaims on dead or on unknown after the silence grace (fine for `outcome`);
    a chat takes `chat_runner_confirmed_dead`, true only on a live node's affirmative "no runner here",
    since a quiet node may still be running it. The lane's row (`_LANE_PERSONAS`) takes `lane_runner_held`
    when given: the same probe with a shorter silence grace. All task-keyed; an uncertain read reclaims nothing."""
    def gone(task_id: str, persona: Any) -> bool:
        # A chat needs an affirmative death; everything else may act on a spent silence grace.
        if persona in _OPERATOR_DONE_PERSONAS:
            return chat_runner_confirmed_dead(task_id)
        held = lane_runner_held if lane_runner_held and persona in _LANE_PERSONAS else runner_held
        return not held(task_id)

    for task, task_id in _gone_rows(
        "active", _RESTART_ONLY_PERSONAS, gone,
        sweep="restart-reclaimer", check="liveness check failed", left="leaving it active (no reclaim)",
    ):
        _post_reclaim(lambda: control_plane_api.reclaim_active(task_id), task_id,
                      sweep="restart-reclaimer", persona=task.get("persona"),
                      landed="stale active row reclaimed active->ready (runner gone)")


def reclaim_stale_awaiting_response_tasks(runner_confirmed_dead: Callable[[str], bool]) -> None:
    """The `awaiting_response` reclaimer: a task parked on a RUNNER-RAISED question whose asker is gone.

    The operator's answer is delivered into the runner's session, so a dead session makes the row
    unanswerable, and nothing else watches this state.

    The state is not the discriminator: `await_response_on_wedged_dispatch` also parks rows with no
    runner, whose question only a human settles. That distinction is enforced inside the atomic
    `reclaim_awaiting_response_task` (this sweep cannot see question state), so a refusal is ordinary.
    No persona filter; no GH/branch probes (a parked row shipped nothing).

    Fails closed harder than its siblings: the probe (`task_pipeline_job._runner_confirmed_dead`) is true
    only on a live node's affirmative "no runner here", never on silence. An unjudgeable row stays parked."""
    for task, task_id in _gone_rows(
        AWAITING_RESPONSE, None, lambda task_id, _persona: runner_confirmed_dead(task_id),
        sweep="awaiting-response-reclaimer", check="liveness check failed", left="leaving it parked (no reclaim)",
    ):
        try:
            answer = control_plane_api.reclaim_awaiting_response(task_id)
        except Exception as exc:  # noqa: BLE001 — fail CLOSED: leave the row parked for a later pass
            log.warning("awaiting-response-reclaimer: reclaim failed for task %s — leaving it "
                        "parked: %s", task_id, exc)
            continue
        # A non-dict reply costs this row only; the reclaim contracts to return a dict.
        reclaimed = answer.get("reclaimed") if isinstance(answer, dict) else None
        if reclaimed:
            log.info("awaiting-response-reclaimer: task %s (persona %r) was parked on a question its "
                     "runner could no longer be asked about — reclaimed awaiting_response->ready for "
                     "re-dispatch into the same worktree", task_id, task.get("persona"))
        else:
            # DEBUG: a standing condition, not an event. Either the CP's own wedged-dispatch gate (refused
            # every pass, permanently) or the row moved; the operator already sees it in Needs-attention.
            # The reclaim that lands gets the line.
            log.debug("awaiting-response-reclaimer: task %s was not a runner-raised question round the "
                      "reclaim could take (no open round, or it moved) — nothing done", task_id)


def expire_timed_out_awaiting_response_tasks() -> None:
    """The `awaiting_response` EXPIRER: a task parked on a question nobody has read within the operator's
    bounds. Two stages, both decided per row by `task_pipeline_cp.expire_awaiting_response`: past the
    timeout the runner is stopped and the question stays answerable; past the release bound the task is
    failed and its worktree handed back.

    The case the reclaimer above leaves alone: the runner is alive inside `AskUserQuestion` and the human
    is not coming. No liveness probe, since liveness is not evidence here. Called for every parked row
    because the age test belongs inside the atomic write (`expire_timed_out_awaiting_response_task`); a
    refusal (too young, wedged-dispatch gate, `advisor` chat, moved) is ordinary and the helper logs expiries."""
    for task in control_plane_api.list_open_tasks():
        if task.get("state") != AWAITING_RESPONSE:
            continue
        task_id = task.get("id")
        if task_id is None:
            continue
        try:
            answer = control_plane_api.expire_awaiting_response(task_id)
        except Exception as exc:  # noqa: BLE001 — fail CLOSED: leave the row parked for a later pass
            log.warning("awaiting-response-expirer: expire failed for task %s — leaving it "
                        "parked: %s", task_id, exc)
            continue
        # A non-dict reply costs this row only; the expiry contracts to return a dict.
        expired = answer.get("expired") if isinstance(answer, dict) else None
        stopped = answer.get("stopped") if isinstance(answer, dict) else None
        if expired:
            log.info("awaiting-response-expirer: task %s (persona %r) was parked on a question nobody "
                     "read within the release bound — failed, and its worktree slot handed back",
                     task_id, task.get("persona"))
        elif stopped:
            log.info("awaiting-response-expirer: task %s (persona %r) was parked on a question nobody "
                     "read within the timeout — runner stopped, question still answerable",
                     task_id, task.get("persona"))


# Injected one-shot fanout-ceiling alert callback, or None for a no-op. A setter, since the alert sink is
# unreachable from this stdlib-only module; main.py injects it and the sweep stays zero-arg.
_notify_epic_blocked_fn: "Callable[[int, str, str], None] | None" = None


def set_epic_fanout_blocked_notifier(fn: "Callable[[int, str, str], None] | None") -> None:
    """Wire (or clear, with `None`) the injected fanout-ceiling alert callback. Called once by main.py's
    `--sweep-tasks` branch before the sweep runs; a test calls it directly to inject a mock."""
    global _notify_epic_blocked_fn
    _notify_epic_blocked_fn = fn


def fanout_blocked_title(eid: int) -> str:
    """The escalation row's title for an epic whose fan-out hit the ceiling. Scope-neutral: the row lands
    in the epic's project's org (`app/tenancy/copy.py`). Censused by `tests/test_tenant_system_rows.py`."""
    return f"epic task {eid} fan-out BLOCKED — child dispatch failing"


def stuck_epic_title(epic_id: int) -> str:
    """The escalation row's title for an epic stuck active with every child done — `fanout_blocked_title`'s
    twin."""
    return f"epic task {epic_id} STUCK — active, all children done, rollup blocked"


def _escalate_needs_attention_task(project: str, title: str, reason_body: str, dedup_key: str,
                                   *, announced: bool = False, store=None, about_task_id: int | None = None) -> None:
    """Create (dedup) a HIDDEN `adhoc` system Task and park it, so an epic-escalation alert surfaces in
    the Needs-attention view.

    The `dedup_key` is per source, so two sources keep separate rows. `announced=True` takes the row's
    needs-attention claim before parking it, so an operator already paged is not paged again;
    `announced=False` leaves the claim for the announcers. Best-effort: failures are swallowed."""
    try:
        result = control_plane_api.create_system_task(
            persona="adhoc", project=project, title=title, body=reason_body,
            state="active", dedup_open=True, dedup_key=dedup_key, about_task_id=about_task_id,
        )
        task_id = result.get("id")
        if isinstance(task_id, int):
            if announced:
                # Before `block_task`: the park is what announcers observe. A dedup hit on an already-claimed
                # row refuses the claim, correctly.
                (store or alert_stamps.default_store()).claim(needs_attention_stamp_key(task_id))
            control_plane_api.block_task(task_id, reason_body)
    except Exception as exc:  # noqa: BLE001 — best-effort: never break the sweep on an escalation failure
        log.warning("needs-attention escalation for %r failed: %s", title, exc)


def _alert_epic_fanout_blocked(eid: int, repo: str, reason: str, attempts: int) -> None:
    """One-shot alert for an epic the fanout ceiling just escalated: the hidden-Task escalation plus the
    injected notify callback. `attempts` is the enforcer's own count (`record_fanout_attempt_failed`), so
    the sentence states what was compared rather than re-reading the epic's org-tier knob here.
    Best-effort: exceptions are logged and swallowed."""
    announced = False
    if _notify_epic_blocked_fn is not None:
        try:
            _notify_epic_blocked_fn(eid, repo, reason)
            announced = True  # the operator has been told; the row below is the VIEW surface, not a page
        except Exception as exc:  # noqa: BLE001 — best-effort: a notify failure must never break the sweep
            log.warning("epic-fanout: epic %s blocked notify_epic_blocked callback failed: %s", eid, exc)
    _escalate_needs_attention_task(
        repo,
        fanout_blocked_title(eid),
        f"epic task {eid} in `{repo}` is `system_failed`: fan-out failed {attempts} sweeps in a "
        f"row (last error: {reason}). Held for human triage — resolve the underlying cause, then Start the "
        "epic to resume fan-out.",
        # Spelling pinned: changing this dedup key re-fires the alert for every escalated epic.
        dedup_key=f"epic-fanout-blocked-{eid}",
        announced=announced,
        about_task_id=eid,
    )


def _process_epic_children_fanout() -> None:
    """Eager fan-out for a STARTED epic's analysis children.

    A still-`draft` child is flipped `ready` (`ready_issueless`) and dispatches keyed on `task.id`; a crash
    re-drives at most that one child. All children are readied up front; `dispatchable_tasks`'s dependency
    gate sequences dispatch. A `readied=False` reply bumps `fanout_attempts`, and at the ceiling the
    endpoint escalates the parent epic. `sweep_opt_out` is honored here because this dispatches runners."""
    for epic in control_plane_api.list_epics_fanout():
        eid = epic.get("id")
        repo = epic.get("project")
        if eid is None or not repo:
            log.warning("epic-fanout: skipping malformed epic row %r", epic)
            continue
        for child in epic.get("children") or []:
            # Only a still-`draft` child is a target: one already readied is NOT re-driven.
            if child.get("state") != "draft":
                continue
            if child.get("sweep_opt_out"):
                log.info("epic-fanout: epic %s child %s opted out of the sweep (sweep_opt_out) — "
                         "not auto-readied", eid, child.get("id"))
                continue
            # Flip the child `ready`; it then dispatches keyed on `task.id`.
            ready_reply = control_plane_api.ready_issueless(child.get("id"))
            if ready_reply.get("readied"):
                log.info("epic-fanout: epic %s child %s readied (draft->ready, dispatches on task.id)",
                         eid, child.get("id"))
            else:
                # Never strand the child: the failure bumps `fanout_attempts` and escalates the PARENT
                # EPIC at the ceiling; below it, the next sweep re-drives it.
                log.warning("epic-fanout: epic %s child %s ready-issueless returned readied=False — "
                            "bumping fanout-attempt (never stranded)", eid, child.get("id"))
                fanout_reply = control_plane_api.fanout_attempt_failed(child.get("id"))
                if fanout_reply.get("just_failed"):
                    _alert_epic_fanout_blocked(
                        eid, repo, "ready-issueless failed", fanout_reply.get("attempts"),
                    )


def sweep_epic_children_fanout() -> None:
    """Eager fan-out sweep: ready each still-`draft` analysis child of a STARTED epic.

    Idempotent and crash-safe via `_process_epic_children_fanout`'s guarded draft→ready flip. The
    ceiling alert fires through the injected notifier, so the zero-arg contract stays intact."""
    _process_epic_children_fanout()


def sweep_epic_rollups() -> None:
    """Reconcile-scan self-heal for the missed-edge-trigger gap.

    The per-child rollup trigger fires from one request, so a crash can strand an epic `active` with every
    child `done`. This re-evaluates every active epic through the same guarded UPDATE each tick, and alerts
    once on the reply's `stuck` field."""
    reply = control_plane_api.reconcile_epic_rollups()
    rolled_up = reply.get("rolled_up") or []
    if rolled_up:
        log.info(
            "epic-rollup-reconcile: completed %d epic(s) %s that a missed edge trigger left active",
            len(rolled_up), rolled_up,
        )
    _process_stuck_epics(reply.get("stuck") or [])


# ── stuck-epic detector + alert ───────────────────────────────────────────────────────────────────
# 30 minutes: well above the 300s outcome-backfill cadence, so a normal backfill never flaps an alert.
STUCK_EPIC_THRESHOLD_MINS = 30

# Injected one-shot alert callback, mirroring the fanout-ceiling notifier: wired by main.py, and
# unwired is a silent no-op.
_notify_stuck_epic_fn: "Callable[[int, str, str, float], None] | None" = None


def set_stuck_epic_notifier(fn: "Callable[[int, str, str, float], None] | None") -> None:
    """Wire (or clear, with `None`) the injected stuck-epic alert callback. Called once by main.py's
    `--sweep-tasks` branch before the sweep runs; a test calls it directly to inject a mock."""
    global _notify_stuck_epic_fn
    _notify_stuck_epic_fn = fn


STUCK_EPIC_STAMP_FAMILY = "stuck-epic"


def stuck_epic_stamp_key(project: str, epic_id: int) -> str:
    """The durable one-shot-alert marker for a stuck epic — exactly one alert per stuck epic. Keyed on
    the EPIC ID alone, since it is unique on its own; the project rides in the alert, not the key."""
    return alert_stamps.key_for(STUCK_EPIC_STAMP_FAMILY, epic_id)


def _clear_rearmed_stuck_epic_stamps(reported_epic_ids: set[int], store=None) -> None:
    """Re-arm: drop the stamp of any previously-alerted epic no longer in THIS tick's `stuck` list, so a
    later recurrence alerts again instead of staying suppressed by a stale stamp."""
    (store or alert_stamps.default_store()).retain(
        STUCK_EPIC_STAMP_FAMILY,
        [stuck_epic_stamp_key("", epic_id) for epic_id in reported_epic_ids],
    )


def _alert_stuck_epic(epic_id: int, project: str, reason: str, stuck_seconds: float) -> None:
    """One-shot alert for an epic `active` with every current child `done` past the threshold, over
    `_alert_epic_fanout_blocked`'s two surfaces. `reason` is embedded verbatim. Best-effort."""
    minutes = stuck_seconds / 60
    announced = False
    if _notify_stuck_epic_fn is not None:
        try:
            _notify_stuck_epic_fn(epic_id, project, reason, stuck_seconds)
            announced = True  # the operator has been told; the row below is the VIEW surface, not a page
        except Exception as exc:  # noqa: BLE001 — best-effort: a notify failure must never break the sweep
            log.warning("stuck-epic: epic %s notify callback failed: %s", epic_id, exc)
    _escalate_needs_attention_task(
        project,
        stuck_epic_title(epic_id),
        f"epic task {epic_id} in `{project}` has been `active` with every current child `done` for "
        f"{minutes:.0f} min (threshold {STUCK_EPIC_THRESHOLD_MINS}m) and did not roll up: {reason}. "
        "The reconcile rollup scan re-drives this every tick but cannot fix a genuinely-missing "
        "outcome — resolve the underlying cause and it will roll up on the next reconcile pass.",
        dedup_key=f"epic-stuck-{epic_id}",
        announced=announced,
        about_task_id=epic_id,
    )


def _process_stuck_epics(stuck_rows: list[dict], store=None) -> None:
    """Apply the threshold to `reconcile_epic_rollups`'s `stuck` field and fire exactly one alert per
    stuck epic, idempotent across ticks via the durable stamp.

    Re-arm rule: every epic id THIS tick reports keeps its stamp, threshold crossed or not; anything
    stamped but absent has resolved. `store` is the injection seam (`app/alert_stamps.py`)."""
    stamps = store or alert_stamps.default_store()
    reported_ids: set[int] = set()
    for row in stuck_rows:
        epic_id = row.get("epic_id")
        project = row.get("project")
        if epic_id is None or not project:
            log.warning("stuck-epic: skipping malformed stuck row %r", row)
            continue
        reported_ids.add(epic_id)
        stuck_seconds = row.get("stuck_seconds") or 0
        if stuck_seconds < STUCK_EPIC_THRESHOLD_MINS * 60:
            continue
        stamp = stuck_epic_stamp_key(project, epic_id)
        if stamps.seen(stamp):
            continue  # already alerted for this stuck spell — AC1.1: exactly once, not per tick
        try:
            _alert_stuck_epic(epic_id, project, row.get("reason") or "unknown", stuck_seconds)
        except Exception as exc:  # noqa: BLE001 — one alert failure must never break the sweep
            log.warning("stuck-epic: epic %s alert failed: %s", epic_id, exc)
            continue
        stamps.mark(stamp)
    _clear_rearmed_stuck_epic_stamps(reported_ids, stamps)


# ── Needs-attention entry-notify ──────────────────────────────────────────────────────────────────
# Fire the alert once on a row's entry into the Needs-attention set, de-duped by a durable
# per-task watermark. The view is authoritative; this is additive.
_notify_needs_attention_fn: "Callable[[dict], None] | None" = None


def set_needs_attention_notifier(fn: "Callable[[dict], None] | None") -> None:
    """Wire (or clear, with `None`) the injected Needs-attention entry-notify callback. Unwired is a
    silent no-op, and the VIEW still surfaces the row."""
    global _notify_needs_attention_fn
    _notify_needs_attention_fn = fn


NEEDS_ATTENTION_STAMP_FAMILY = "needs-attention"

# A backstop, not the primary announcer: `app/task_notify.py` announces off the transition, and the
# shared claim arbitrates. This grace sits far above the notifier's latency and far below a human's.
NOTIFY_GRACE_SECONDS = 120.0


def needs_attention_stamp_key(task_id: int) -> str:
    """The per-task watermark for the entry-notify: one alert per ENTRY, not one per poll.

    Shared with `app/task_notify.py`, which claims the same key. Changing the family or key shape re-arms
    and re-announces every parked row (`alert_stamps.StampStore`)."""
    return alert_stamps.key_for(NEEDS_ATTENTION_STAMP_FAMILY, task_id)


def _clear_rearmed_needs_attention_stamps(
    present_ids: set[int], store=None, *, read_at: datetime | None = None
) -> None:
    """Re-arm: a task that has LEFT the set loses its watermark, so re-entering it alerts again.

    `read_at` is when `present_ids` was READ, and a stamp claimed after it survives: a task that parked
    mid-pass is absent from the snapshot yet already announced (`alert_stamps.StampStore.retain`)."""
    (store or alert_stamps.default_store()).retain(
        NEEDS_ATTENTION_STAMP_FAMILY,
        [needs_attention_stamp_key(task_id) for task_id in present_ids],
        stamped_before=read_at,
    )


def sweep_needs_attention_notify(store=None) -> None:
    """Fire the entry-notify exactly once per row's entry into the Needs-attention set — and only for an
    entry NOBODY ELSE announced.

    Claims each unstamped row's watermark, notifies the ones it won, and re-arms tasks that left the set.
    Unwired it still maintains watermarks, so wiring later does not replay the backlog. Two gates:

      * the grace (`NOTIFY_GRACE_SECONDS`): a younger row belongs to the transition notifier and is skipped
        without claiming. A row with no readable age is treated as old, so absence cannot silence it.
      * the claim: atomic; a notify that raises releases it so the next tick retries."""
    read_at = datetime.now(timezone.utc)
    rows = control_plane_api.list_needs_attention()
    present_ids: set[int] = set()
    stamps = store or alert_stamps.default_store()
    # One read for the whole sweep; `claim` below stays the per-row arbiter, so a stamp landing after this
    # read is still caught there.
    already = stamps.seen_many(
        needs_attention_stamp_key(row.get("id")) for row in rows if isinstance(row.get("id"), int)
    )
    for row in rows:
        task_id = row.get("id")
        if not isinstance(task_id, int):
            log.warning("needs-attention: skipping malformed row %r", row)
            continue
        present_ids.add(task_id)
        stamp = needs_attention_stamp_key(task_id)
        if stamp in already:
            continue  # already announced for this entry-spell — exactly once, not per tick
        age = row.get("age_seconds")
        if isinstance(age, (int, float)) and not isinstance(age, bool) and age < NOTIFY_GRACE_SECONDS:
            continue  # the transition notifier owns this entry — do NOT claim it out from under it
        if not stamps.claim(stamp):
            continue  # the transition notifier announced this entry while we were reading
        if _notify_needs_attention_fn is not None:
            try:
                _notify_needs_attention_fn(row)
            except Exception as exc:  # noqa: BLE001 — a notify failure must never break the sweep
                log.warning("needs-attention: entry-notify failed for task %s: %s", task_id, exc)
                stamps.release(stamp)  # hand the claim back so the next tick retries the notify
                continue
    _clear_rearmed_needs_attention_stamps(present_ids, stamps, read_at=read_at)


def sweep_finish_phases(lease_held: Callable[[str], bool]) -> None:
    """CRASH BACKSTOP for the terminal Finish phase — `dispatchable_tasks` is the PRIMARY
    driver, so this recovers ONLY a finish runner that crashed mid-settle.

    Settles a finish child only when stuck `active` with its task-keyed lease unheld; an erroring lease
    read counts as held. Per-child failures are skipped. The settle body is empty (the server computes
    the report)."""
    for finish in control_plane_api.list_finish_pending():
        finish_id = finish.get("id")
        if finish_id is None:
            log.warning("finish-sweep: skipping malformed finish row %r", finish)
            continue
        if finish.get("state") != "active":
            continue  # ready/draft → the dispatcher drives it; done → nothing to do
        try:
            held = lease_held(str(finish_id))
        except Exception as exc:  # noqa: BLE001 — fail CLOSED: never settle on an uncertain lease read
            log.warning("finish-sweep: lease check failed for finish %s — treating as HELD "
                        "(no backstop settle): %s", finish_id, exc)
            continue
        if held:
            continue  # a live runner still holds the task's lease — leave the finish child `active`
        try:
            reply = control_plane_api.finish(finish_id)
            log.info("finish-sweep: crashed finish %s settled by backstop (state=%s, settled=%s)",
                     finish_id, reply.get("state"), reply.get("settled"))
        except Exception as exc:  # noqa: BLE001 — one bad finish child must not abort the batch
            log.warning("finish-sweep: backstop settle failed for finish %s: %s", finish_id, exc)


