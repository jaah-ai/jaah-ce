"""A runner's task outcomes: settle, fail, PRs, questions, runner session, appended tasks and follow-ups."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, StrictBool, ValidationInfo, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import (
    agent_resolution,
    agent_session_lifecycle,
    carryover,
    edition_features,
    fleet_verdicts,
    merge_lane_state,
    node_credentials,
    node_identity,
    project_identity,
    runner_places,
    runner_terminations,
    slot_state,
    task_notify,
    task_prs,
)
from app.agent_sessions import AGENT_KIND_SDK_AGENT
from app.actors import task_actor_values
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import SessionLocal, get_session
from app.events import emit_console_event
from app.tenancy import seam
from app.routes.commands import _SLUG_RE, _resolve_command_args, dispatch_and_audit_sync
# The Reset soft-delete watermark's ONE predicate — imported, never re-spelled here.
from app.task_artifacts import visible_artifact_clause
from app.dispatch_compose.directives import FULL_AUTO_BLOCKER_HEADERS
from app.task_lookup import get_task_or_404
# Every PERSONA fact comes from the one registry record per persona (`app/persona_registry.py`).
from app.persona_registry import (
    DONE_CRITERION_OPERATOR,
    DONE_CRITERION_OUTCOME,
    PERSONA_DONE_CRITERION,
    settle_sources,
)
from app.task_manifest import ARTIFACT_KIND_LABELS, OUTCOME_SUMMARY_TARGET_CHARS
from app.task_settle import (
    NO_BUILD_OUTCOME_REASON,
    NO_REPO_OUTCOME_REASON,
    REFERENCE_PARAMS,
    TERMINAL_REFERENCES,
    is_folder_build,
    missing_advisory_kinds,
    missing_hard_kinds,
    terminals_of,
)
from app.url_validation import validated_http_url
from app.node_clone import account_slug_from_dir
from app.node_identity import node_id_or_none, resolve_node_id
from app.entity_shape import get_or_404
from app.models.audit import ActivityEvent
from app.models.credentials import Account
from app.models.projects import Project
from app.models.tasks import Task, TaskQuestion
# Task transitions go through the watermark-gated helpers; the state machine single-homes in routes/tasks/.
from app.routes.tasks.dispatch import _RUNNER_BINDING_CLEARED
from app.routes.tasks.policy import (
    DB_DIRECT_STAGE_PERSONAS,
    EPIC_BUILD_PERSONAS,
    PERSONA_ADHOC,
    PERSONA_EPIC,
    PERSONA_FINISH,
    PERSONA_PLAN,
    PERSONA_TRIAGE,
    MAX_TASK_TEXT_LEN,
    MERGED_PR_CRITERION_PERSONAS,
    NO_PR_CLOSE_REASONS,
)
from app.routes.tasks.epics import _epic_filed_count, append_tasks, _maybe_roll_up_parent
from app.persona_registry import SHAPE_FOLLOW_UP, SHAPE_TASKS
from app.routes.tasks.qa_render import TaskQuestionOut, _task_question_out
from app.routes.tasks.epic_finish import append_finish_follow_up, execute_finish_phase
from app.routes.tasks.awaiting import (
    await_response_on_dispatched_task, resume_past_pending_answer_delivery, supersede_pending_answer_delivery,
    system_fail_dispatched_task,
)
from app.routes.tasks.runner_settle import (
    settle_analysis_child_on_artifact,
    settle_coder_no_pr,
    settle_coder_on_merge,
)
# State vocabulary and guard sets single-home in `app/task_lifecycle.py`.
from app.task_lifecycle import (
    ACTIVE,
    AWAITING_RESPONSE,
    DONE,
    DRAFT,
    SYSTEM_FAILED,
    _COMPLIANCE_CASCADE_TERMINALS,
    _COMPLIANCE_MIRROR_SETTLED_STATES,
    _RUNNER_RUNNING_STATES,
)
from app.user_activity import record_task_note
from app.routes.agent import auth
from app.routes.agent.auth import (
    HOST_PRODUCER_CALLER,
    _check_token_for_task,
    _guard_task_creating_caller,
)
from app.routes.agent.task_reads import SystemTaskIn, create_system_task

if TYPE_CHECKING:
    from app.lane_forge_auth import CredentialSource

router = APIRouter(prefix="/api/agent", tags=["agent"])
log = logging.getLogger("agent-routes")


# Dispatch is request/reply (app/routes/commands.py), executed by apps/jaahd/commands.py.


# ── The compliance_fix GROUP Task and its per-cell mirrors ──────────────────────────────────────
# A group `compliance_fix` Task (`compliance_path` set, `asset_id IS NULL`) is what a runner is
# dispatched against; its per-cell mirror Tasks are the matrix's read surface. The (repo, path) slot lock
# is `uq_tasks_compliance_slot`. The runner settles the group through `/settle`/`/fail`, and
# `_settle_compliance_group` cascades the terminal onto the mirrors.


def _compliance_group_mirrors(
    group_task: Task, session: Session, *, only_open: bool = False
) -> list[Task]:
    """The per-cell status-mirror Tasks linked to a GROUP compliance_fix Task (by `compliance_group_id`;
    a mirror always carries a non-null `asset_id`). `only_open` restricts to non-terminal mirrors — the
    settle cascade's target set."""
    stmt = select(Task).where(
        Task.compliance_group_id == group_task.id,
        Task.asset_id.is_not(None),
    )
    if only_open:
        stmt = stmt.where(Task.state.not_in(_COMPLIANCE_MIRROR_SETTLED_STATES))
    return list(session.scalars(stmt).all())


def _compliance_group_repo(group_task: Task, session: Session) -> Optional[str]:
    """The repo NAME a GROUP compliance_fix Task covers (its Project's name — the matrix/sensitivity gate
    key), or None when the project row is gone."""
    project = session.get(Project, group_task.project_id)
    return project.name if project is not None else None


def _settle_compliance_group(
    group_task: Optional[Task],
    terminal: str,
    session: Session,
    *,
    pr_url: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Cascade a group `compliance_fix` Task's terminal settle to its per-cell mirror Tasks
    (`compliance_group_id`). Called after the group settles, from `/settle`, `/fail` and
    `app/task_pipeline_cp.py::block_task`; a no-op for non-group rows.

      * `done` + `pr_url`  → `settle_coder_on_merge(mirror, pr_url)` + stamp `merged_at`.
      * `done` + no pr_url → mirror `active → done` directly + `merged_at`.
      * `blocked`          → `await_response_on_dispatched_task(mirror, reason)`.
      * `failed`           → `system_fail_dispatched_task(mirror, reason)`.

    Each mirror settle guards on `state == active`, so re-runs are no-ops. A `done` group also enqueues
    one `sync_develop` for the repo's main clone.
    """
    if group_task is None or group_task.compliance_path is None:
        return
    if terminal not in _COMPLIANCE_CASCADE_TERMINALS:
        return
    merged_at = group_task.merged_at or datetime.now(timezone.utc)
    for mirror in _compliance_group_mirrors(group_task, session, only_open=True):
        if terminal == DONE and pr_url:
            if settle_coder_on_merge(mirror.id, pr_url, session):
                # Stamp the merge record the matrix reads from the group's merged_at, so mirror and group agree.
                session.execute(
                    update(Task).where(Task.id == mirror.id)
                    .values(merged_at=merged_at, **task_actor_values(session))
                )
                session.commit()
        elif terminal == DONE:
            # No-PR settle: `settle_coder_on_merge` needs a pr_url, so settle the mirror directly; `outcome_pr_url`
            # stays NULL.
            row = session.execute(
                update(Task)
                .where(Task.id == mirror.id, Task.state == ACTIVE)
                .values(
                    state=DONE,
                    **_RUNNER_BINDING_CLEARED,
                    merged_at=merged_at,
                    outcome_summary=func.coalesce(
                        Task.outcome_summary, "compliance deficiency resolved — no PR needed"
                    ),
                    **task_actor_values(session),
                )
                .returning(Task.id)
            ).first()
            if row is not None:
                emit_console_event(session, entity="task", id=row[0], action=DONE)
                session.commit()
        elif terminal == AWAITING_RESPONSE:
            await_response_on_dispatched_task(mirror.id, reason or "compliance fix blocked", session)
        elif terminal == SYSTEM_FAILED:
            # `final=True`: a mirror is never dispatched, so the retry branch would wrongly send it to `ready`.
            system_fail_dispatched_task(
                mirror.id, reason or "compliance fix failed", session, final=True
            )
    if terminal == DONE:
        repo = _compliance_group_repo(group_task, session)
        if repo:
            # Resync the fleet's local clone (number 0) after a fix PR lands. `_resolve_command_args` is required:
            # it injects `base` and `path`, and `worktree_ops.sync_develop` fails closed without `base`.
            # The CP's own node, by uuid (`node_identity.cp_node_id`: its `NODE_ID` may be a name).
            own = node_identity.cp_node_id(session)
            node_id = str(own) if own is not None else (resolve_node_id() or "local")
            dispatch_and_audit_sync(
                session, node_id, "sync_develop",
                _resolve_command_args(
                    "sync_develop", {"project_id": group_task.project_id, "number": 0}, session, node_id
                ),
                "system:compliance",
            )


# ── The unified settle: ONE terminal contract for every persona ────────────────
# `POST /tasks/{id}/complete` (alias `/settle`) is the one terminal door. Totality: every (persona x state x
# terminal type) reaches a settle or a refusal naming its cause, never a silent 2xx that writes nothing. The finish
# engine's only other caller is `sweep_finish_phases` (`app/task_pipeline_cp.py::finish`), in process.

# Settle calls an incomplete manifest costs before the CP accepts one: four refusals, the fifth accepted,
# so shipped work is never stranded; the accepted fifth raises a hidden alert
# (`_raise_manifest_incomplete_alert`). Counted CP-side per task (`tasks.settle_refusals`): a runner
# counting its own could be killed and restart the count.
_MANIFEST_REFUSAL_LIMIT = 5


# Dedup key prefix for that alert: one open row per task, refreshed (`create_system_task`'s `dedup_open`).
_MANIFEST_ALERT_KEY_PREFIX = "manifest-incomplete:"


#: The settle's `memory_wrong` bounds: a `mem-N` token's length and how many of each count.
_MEMORY_TOKEN_MAX = 32


_MEMORY_TOKENS_MAX = 20


class SettleIn(BaseModel):
    # The runner's settled result, stored verbatim in `tasks.outcome_summary` (`Text`) and in the note.
    #
    # `OUTCOME_SUMMARY_TARGET_CHARS` is a target published in `description`, never a validator (see
    # `task_manifest`); `max_length` is the cap. One cap for every branch (`agent_vocabulary.
    # _declared_arg_limits` keys by leaf name); 100_000 matches the daemon's `ToolArg` cap, tested in
    # the daemon's verb-call contract tests (a lower daemon cap would refuse a last call).
    summary: str = Field(
        min_length=1,
        max_length=100_000,
        description=(
            f"The settled result, as an operator reads it on the board: aim for under "
            f"{OUTCOME_SUMMARY_TARGET_CHARS} characters — what was done and how it ended, not the "
            f"evidence behind it, which belongs in the uploaded documents. A TARGET, not a cap: a "
            f"longer summary is stored in full and never refused."
        ),
    )
    # The claimed ending, a plain `str` so the refusal can name this persona's allowed set. Capped at 64
    # because it is echoed with `!r` into a relayed 422 and logs (`app/url_validation.py`'s reservation).
    terminal_type: str = Field(max_length=64)
    pr_url: Optional[str] = None
    # A `merged_pr` settle's other PRs, one per further repository the task changed (`app/task_prs.py`):
    # `pr_url` names the primary repository's PR when there is one. Accepted only beside `pr_url`; the cap bounds a
    # call, not a project (no repository cap exists, N4).
    pr_urls: Optional[list[Annotated[str, Field(max_length=2048)]]] = Field(
        default=None,
        max_length=200,
        description=(
            "merged_pr only: every other merged PR this task opened, one per further repository it changed "
            "(pr_url names the primary repository's PR when there is one). The settle is refused while any PR "
            "the task opened is not attested here as merged."
        ),
    )
    no_pr_reason: Optional[str] = Field(default=None, max_length=64)
    no_pr_evidence: Optional[str] = None
    # Gate results and the recon artifact ride the settle, each optional and persisted unconditionally
    # (before the note's content-dedup). Verdicts are `str`, not `Literal`: an unexpected token must not 422
    # the whole terminal settle. `max_length=32` bounds that leniency.
    clarity_score: Optional[float] = None
    spec_verdict: Optional[str] = Field(default=None, max_length=32)
    critic_verdict: Optional[str] = Field(default=None, max_length=32)
    recon_map: Optional[dict[str, Any]] = None
    # A merged PR's diff stats (`app/pr_stats.py`), stored on a `merged_pr` settle and ignored on any other. `Any`,
    # not a model: a payload of the wrong shape is stored as `not_captured`/`malformed`, never a 422 on the settle.
    pr_stats: Optional[Any] = Field(
        default=None,
        description=(
            "Merged-PR diff stats, on a merged_pr settle only: {status: captured|partial|not_captured, reason, "
            "additions, deletions, files_changed, excluded_additions, excluded_deletions, forge}."
        ),
    )
    # A merged PR's provenance (`app/pr_changes.py`): merge sha, commits, changed paths — the daemon's stamp, like
    # `pr_stats`, stored on a `merged_pr` settle and ignored on any other; `Any` for the same reason.
    pr_provenance: Optional[Any] = Field(
        default=None,
        description=(
            "Merged-PR provenance, on a merged_pr settle only: {status: captured|partial|not_captured, reason, "
            "forge, pr_number, merge_sha, commits: [{sha, author_name, author_login, message}], files: [path]}."
        ),
    )

    # Project-memory flags riding the settle (`app/routes/agent_memory.py::record_settle_votes`): `mem-N` units that
    # were wrong, counted only when the task's memory is on. Use is never self-reported: confirms come from captured
    # evidence. A `memory_used` from a node on an older jaahd is an unknown key, ignored like any other, so a
    # mixed-version settle never 422s. Clipped, never refused: a token past 32 characters is no `mem-N` and is
    # dropped, and only the first 20 count, so the terminal call can neither 422 nor slow on them (any non-list
    # counts as none).
    memory_wrong: Optional[list[str]] = Field(
        default=None, description="`mem-N` units that were wrong; ignored when memory is off.",
    )

    @field_validator("memory_wrong", mode="before")
    @classmethod
    def _clip_memory_tokens(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return None
        return [t for t in value if isinstance(t, str) and len(t) <= _MEMORY_TOKEN_MAX][:_MEMORY_TOKENS_MAX]

    # The runner's own count of its `memory_propose` calls. A forcing function, not a record: the daemon requires
    # it on a memory-ON settle so the question "did I learn anything worth keeping" is asked at the one point a
    # runner is sure to read the schema. The control plane already knows the true count (`memory_sources` rows
    # of this task on the `propose` feed), so a value is compared and logged, never stored, and never refuses
    # the settle (anything but a non-negative int counts as absent).
    memory_proposed: Optional[int] = Field(
        default=None, description="How many memory_propose calls this task made; ignored when memory is off.",
    )

    @field_validator("memory_proposed", mode="before")
    @classmethod
    def _memory_proposed_or_none(cls, value: Any) -> Any:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    # The shared scheme floor (`app/url_validation.py`) on the runner's own report, rendered as the "merged
    # PR ↗" link operators click.
    _check_pr_url = field_validator("pr_url")(validated_http_url)

    @field_validator("pr_urls")
    @classmethod
    def _check_pr_urls(cls, value: Optional[list[str]], info: ValidationInfo) -> Optional[list[str]]:
        return None if value is None else [validated_http_url(url, info) for url in value]


def _settle_reference_values(body: SettleIn) -> dict[str, Optional[str]]:
    """The `tasks` reference columns this terminal type carries, plus NULLs over the other reference
    terminal's, so the two are mutually exclusive on the row.

    Terminals with no reference (`analysis`, `coders`, `finish`, `outcome`) write none. Gate 4 refuses
    both references in one call, but a `merged_pr` then `no_pr` sequence would leave a stale PR url;
    declaring `no_pr` asserts nothing merged, so the url is cleared (`settle_coder_no_pr`,
    `_child_rollup_ready`).
    """
    carried = {"pr_url": body.pr_url, "no_pr_reason": body.no_pr_reason,
               "no_pr_evidence": body.no_pr_evidence}
    required = TERMINAL_REFERENCES.get(body.terminal_type, ())
    if not required:
        return {}
    return {
        f"outcome_{name}": (carried[name] if name in required else None)
        for name in REFERENCE_PARAMS
    }


def _plan_no_build_stamp(persona: str, body: SettleIn) -> dict[str, Optional[str]]:
    """What a plan's settle writes over `outcome_no_pr_reason`: `NO_BUILD_OUTCOME_REASON` on an `analysis`
    (nothing to build), NULL on a `coders` — the one column the epic rollup and the final-report publisher
    read to tell a settled no-build from an append still outstanding. CP-written: the runner
    carries no reference on either ending (`TERMINAL_REFERENCES`). Empty for every other persona."""
    if persona != PERSONA_PLAN:
        return {}
    return {
        "outcome_no_pr_reason": NO_BUILD_OUTCOME_REASON if body.terminal_type == "analysis" else None,
    }


def _settle_state_refusal(
    task_id: int, persona: str, sources: tuple[str, ...], state: Optional[str] = None
) -> str:
    """Step 5's refusal, rendered from the tuple and shared with the payload write that repeats its
    predicate, so state names cannot go stale. `state` is omitted by the racing caller.
    """
    observed = f", state={state!r}" if state is not None else ""
    return (
        f"task {task_id} (persona={persona!r}{observed}) cannot settle: state must be one of "
        f"({', '.join(sources)})"
    )


def _raise_manifest_incomplete_alert(
    task_id: int, project_id: int, persona: str, missing: list[str], session: Session
) -> None:
    """The hidden operator alert an accepted-but-incomplete settle raises, naming the missing rows.

    Calls `create_system_task` directly (`cron_alarms.raise_alarm` refuses non-cron keys). After the settle's
    commit, in its own transaction (it commits and takes an advisory lock). Best effort: the settle is
    already committed.
    """
    kinds = ", ".join(f"{kind} ({ARTIFACT_KIND_LABELS.get(kind, kind)})" for kind in missing)
    try:
        out = create_system_task(
            body=SystemTaskIn(
                persona=PERSONA_ADHOC,
                project=project_identity.label(session, project_id),
                project_id=project_id,
                title=f"Task {task_id} settled with an incomplete artifact manifest",
                body=(
                    f"Task {task_id} (persona `{persona}`) was refused "
                    f"{_MANIFEST_REFUSAL_LIMIT - 1} times for an incomplete artifact manifest and its "
                    f"{_MANIFEST_REFUSAL_LIMIT}th settle was ACCEPTED so the work would not be "
                    f"stranded.\n\nStill missing: {kinds}.\n\n"
                    f"Nothing is wrong with the task's result — this is a record that its runner never "
                    f"delivered what its persona owes, which is a charter or tooling problem to fix "
                    f"rather than a unit of work."
                ),
                # A notice, not work: `draft` is never dispatched.
                state=DRAFT,
                dedup_open=True,
                dedup_key=f"{_MANIFEST_ALERT_KEY_PREFIX}{task_id}",
            ),
            # The affirmative producer declaration the task-creation guard requires; no Task behind this call.
            x_agent_task_id=HOST_PRODUCER_CALLER,
            _=None,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by contract (docstring above)
        # Roll back first: some `create_system_task` branches re-raise without rolling back, and an aborted
        # transaction would 500 the settle delegation below. Only the alert's own transaction is discarded.
        session.rollback()
        log.warning(
            "task %s settled with an incomplete manifest (%s) but the alert could not be raised: %s",
            task_id, ", ".join(missing), exc,
        )
        return
    log.warning(
        "task %s settled with an incomplete manifest (%s) on its %sth call — alert Task %s is open",
        task_id, ", ".join(missing), _MANIFEST_REFUSAL_LIMIT, out.id,
    )


def _settle_outcome_criterion_task(task_id: int, session: Session, *, no_repo: bool = False) -> bool:
    """`active → done` for a persona whose done-criterion is its posted outcome; no shared helper exists.

    No anti-spoof floor: there is no merge record to forge. The `state == ACTIVE` guard is the idempotency
    guard. No write-once predicate on `outcome_pr_url`, which landed in its own statement above.

    `no_repo`: a build task settling `outcome` in a folder slot. It also stamps
    `NO_REPO_OUTCOME_REASON` over the PR references, which is what the epic rollup floor and Finish read.
    """
    now = datetime.now(timezone.utc)
    stamp = (
        {"outcome_no_pr_reason": NO_REPO_OUTCOME_REASON, "outcome_pr_url": None, "outcome_no_pr_evidence": None}
        if no_repo else {}
    )
    row = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.state == ACTIVE)
        .values(
            state=DONE, dispatch_attempts=0, github_synced_at=now, **_RUNNER_BINDING_CLEARED, **stamp,
            **task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if row is None:
        return False
    # A lane run's settle returns any row it still holds to the queue and re-files (L10).
    merge_lane_state.lane_task_left_active(session, row[0])
    emit_console_event(session, entity="task", id=row[0], action=DONE)
    session.commit()
    log.info(
        "task %s SETTLED active->done on its own settle call (outcome done-criterion; no merge to "
        "verify; github_synced_at=%s)", task_id, now,
    )
    _maybe_roll_up_parent(row[0], session)
    return True


def _delegate_settle(
    task_id: int, persona: str, body: SettleIn, session: Session,
    attestation: Optional[task_prs.Attestation] = None,
) -> bool:
    """Step 8's delegation to the persona's existing settle helper, so this route adds no second state machine.
    Returns whether the row moved.

    One keyword is load-bearing on the analysis helper: `sources` (its default excludes
    `awaiting_response`, which step 5 admits).

    A `merged_pr` terminal settles on the runner's own attestation: no CP decision rests on Git content, and
    the runner's charter makes it poll its PR until merged before calling.
    """
    terminal_type = body.terminal_type
    if persona in DB_DIRECT_STAGE_PERSONAS:
        return settle_analysis_child_on_artifact(
            task_id, session,
            # `settle_sources`, the function step 5 gated on, not `PERSONA_SETTLE_STATES`: one spelling of the door's
            # decision, so a persona gaining `merged_pr` cannot be admitted and then match nothing.
            sources=settle_sources(persona, terminal_type),
        )
    if persona == PERSONA_FINISH:
        # The finish engine computes the whole resolution report from the DB and flips the child `→ done`
        # idempotently; a None return means it found no finish child with an epic parent, which step 5a
        # already refused.
        return execute_finish_phase(task_id, session) is not None
    if persona in MERGED_PR_CRITERION_PERSONAS:
        if terminal_type == "outcome":
            # Only a folder build task is allowed `outcome` (step 3): its report is the deliverable.
            settled = _settle_outcome_criterion_task(task_id, session, no_repo=True)
            pr_url = None
        elif terminal_type == "no_pr":
            settled = settle_coder_no_pr(task_id, session)
            if settled and task_prs.close_lane_rows_on_no_pr(session, task_id, str(body.no_pr_reason)):
                session.commit()
            pr_url = None
        else:
            # `sources` for the analysis arm's reason: step 5 admits a wider set for `merged_pr` (the recovery arm),
            # so the default would store the payload and report `settled: false`.
            # `outcome_pr_url` and the one stats/provenance row name the primary repository's PR
            # (`task_prs.Attestation.outcome_url`). The daemon read stats and provenance off `pr_url`, so they
            # are stored only when that is the PR they describe.
            outcome_url = attestation.outcome_url if attestation is not None else (body.pr_url or "")
            describes_outcome = outcome_url == body.pr_url
            settled = settle_coder_on_merge(
                task_id, outcome_url, session,
                sources=settle_sources(persona, terminal_type),
                pr_stats=body.pr_stats if describes_outcome else None,
                pr_provenance=body.pr_provenance if describes_outcome else None,
                attestation=attestation,
            )
            pr_url = outcome_url
        if settled:
            # A compliance_fix group cascades to its mirrors, which have no settle call of their own.
            _settle_compliance_group(session.get(Task, task_id), DONE, session, pr_url=pr_url)
        return settled
    if PERSONA_DONE_CRITERION.get(persona) == DONE_CRITERION_OUTCOME:
        return _settle_outcome_criterion_task(task_id, session)
    # Unreachable (step 3 refuses personas with no terminal). False, not a raise, so a future persona's first
    # settle is a visible `settled: false`, not a 500.
    log.error("task %s (persona=%s) reached settle delegation with no helper", task_id, persona)
    return False


def _record_settle_memory_votes(session: Session, task: Task, wrong: list[str], summary: str) -> None:
    """`record_settle_votes` when the task's memory is on; nothing when it is off. Never raises."""
    if not edition_features.enabled("project_memory"):
        # The community edition has no project memory: always off.
        return
    from app.project_memory import memory_active

    try:
        if not memory_active(session, task):
            return
        from app.routes.agent_memory import record_settle_votes

        record_settle_votes(session, task, wrong, summary)
    except Exception:  # noqa: BLE001 — a vote must never change the settle
        session.rollback()
        log.warning("task %s: settle memory votes dropped", task.id, exc_info=True)


def _log_memory_proposed(session: Session, task: Task, claimed: Optional[int]) -> None:
    """Compare the runner's `memory_proposed` with the `propose`-feed units this task actually sourced; one
    info line either way, nothing when memory is off or the runner sent none. Never raises."""
    if not edition_features.enabled("project_memory"):
        # The community edition has no project memory: always off.
        return
    from app.project_memory import memory_active

    try:
        if claimed is None or not memory_active(session, task):
            return
        from app.project_memory.models import MemorySource

        # The source row's own feed: a proposal that deduped onto a unit another feed created is still this
        # task's proposal (`ingest.ingest_candidate`), which the unit's feed would not show.
        actual = session.execute(
            select(func.count()).select_from(MemorySource)
            .where(MemorySource.task_id == task.id, MemorySource.feed == "propose")
        ).scalar_one()
        log.info("task %s: memory_proposed claimed=%s recorded=%s", task.id, claimed, actual)
    except Exception:  # noqa: BLE001 — a count must never change the settle
        session.rollback()
        log.warning("task %s: memory_proposed check dropped", task.id, exc_info=True)


# `/settle` is the legacy alias of `/complete`, kept until the legacy-deletion task.
@router.post("/tasks/{task_id}/complete", status_code=200, response_model=None)
@router.post("/tasks/{task_id}/settle", status_code=200, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def settle_task(
    task_id: int,
    body: SettleIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The one settle. A runner declares what it delivered and which ending it claims; the CP settles it or
    refuses with a reason naming its cause.

      1. unknown task                                  → 404
      2. `epic`                                        → 409 (its state is its children's rollup)
      3. terminal type this persona may not declare    → 422, naming the set it may
      4. the terminal type's own reference shape       → 422
      5. a state this persona may not settle from      → 409, rendering the tuple
      5a. a `finish` with no epic parent / a parentless `plan` → 409, nothing written
      5b. a `plan` `coders` with zero appended children, or a `plan` `analysis` (nothing to build) with
          children appended                             → 409, nothing written
      6. a `triage` with an open question              → 409, nothing written
      7. an unsatisfied hard manifest row              → 422, naming every missing kind
      8. accepted: payload + note written, then the persona's own settle helper runs
      9. memory votes, when the task's memory is on: a flag per `memory_wrong` unit, a confirm per unit the task's
         captured evidence shows it used (never refuses; logged on failure)

    Accept-on-the-fifth applies to step 7 only; steps 2-6 are illegal requests and never touch the counter.
    A manifest refusal persists what the runner reported (summary, gates, references, note) before raising,
    since the session may be gone by retry time. Do not change this. Response: `{"stored": true, "settled":
    <bool>, "state": ...}`; `settled: false` on an accepted call means the helper could not move the row.
    """
    # ── 1. the task itself, AND THAT IT IS THIS CALLER'S ──────────────────────
    # Bound to the node holding the slot: `task_id` is from the path, and this call now owns the
    # `active → done` edge, so without it any node could settle another node's task. Reuses
    # `_task_or_404_for_node`, whose gaps fit: non-node arms are not narrowed, and a task holding no slot is
    # not refused (a runner finishing after its slot was freed).
    task = auth._task_or_404_for_node(session, caller, task_id)
    persona = task.persona
    project_id = task.project_id

    # ── 2. an epic has no runner and nothing to declare ───────────────────────
    if persona == PERSONA_EPIC:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} is an epic and cannot settle: an epic is a non-dispatched container "
                f"whose state is its children's rollup, so it has no runner and no terminal to declare"
            ),
        )

    # ── 3. may this persona declare this ending? ──────────────────────────────
    # Operator-criterion personas first: they have no ending by design (only the operator's Complete settles
    # them) and are absent from `PERSONA_TERMINALS`, so the refusal states the design rather than a missing
    # table row.
    if PERSONA_DONE_CRITERION.get(persona) == DONE_CRITERION_OPERATOR:
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id} (persona={persona!r}) has no terminal call: nothing this session sends "
                f"settles it, and only the operator's Complete ends it"
            ),
        )
    # A playbook stage row declares its deliverable's endings (`playbook_engine.settle_terms`), as its prompt
    # was told; its persona was chosen for that deliverable, so the two agree and its settle helper below fits.
    # A build task in a folder slot has no repository, so it declares `outcome` instead.
    folder_build = is_folder_build(session, task)
    allowed = terminals_of(session, task)
    if not allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id} (persona={persona!r}) declares no terminal type at all "
                f"(`app/persona_registry.py::PERSONA_TERMINALS`), so no settle of it can be validated"
            ),
        )
    if body.terminal_type not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id} (persona={persona!r}) may not settle with terminal type "
                f"{body.terminal_type!r} — allowed: {{{', '.join(allowed)}}}"
                + (" (it runs in a folder slot with no repository: upload your report and settle "
                   "`outcome`)" if folder_build else "")
            ),
        )

    # ── 4. the terminal type's own reference shape ────────────────────────────
    # The CP half of the published `oneOf`: what this ending requires and what it must not carry.
    required_refs = TERMINAL_REFERENCES[body.terminal_type]
    supplied = {name: getattr(body, name) for name in REFERENCE_PARAMS}
    missing_refs = [name for name in required_refs if not str(supplied[name] or "").strip()]
    if missing_refs:
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id}: terminal type {body.terminal_type!r} requires "
                f"{', '.join(missing_refs)} — nothing was recorded"
            ),
        )
    carried_refs = [
        name for name in REFERENCE_PARAMS
        if name not in required_refs and supplied[name] is not None
    ]
    if carried_refs:
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id}: terminal type {body.terminal_type!r} does not carry "
                f"{', '.join(carried_refs)} — nothing was recorded"
            ),
        )
    if body.terminal_type == "no_pr" and body.no_pr_reason not in NO_PR_CLOSE_REASONS:
        # The anti-spoof floor's reason half (`NO_PR_CLOSE_REASONS`, `routes/tasks/policy.py`): only a sanctioned
        # terminal unlocks an epic rollup without a merge.
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id}: unrecognized no_pr_reason {body.no_pr_reason!r} — must be one of "
                f"{list(NO_PR_CLOSE_REASONS)}"
            ),
        )

    # ── 4b. a `merged_pr` reference must name THIS project's repo ─────────────
    # This call settles on `pr_url`, and `validated_http_url` checks only shape, so a lookalike host would
    # render as "merged PR ↗". This proves the url points at this task's repo on its own forge host
    # (`RepoRef.pr_number`); it does not prove the PR merged (the runner's charter-mandated poll does;
    # trusting it is an accepted residual risk). A project whose repo names no forge is refused: there is no repo for a
    # PR to be under, so nothing could be checked.
    # A multi-repo task: `pr_url` and every `pr_urls` entry must each name a PR under one of the
    # project's repositories (`unknown_pr_repo`), and every PR the task opened must be attested merged
    # (`unmerged_prs`) — judged here, before anything is written, so a refusal records nothing.
    attestation: Optional[task_prs.Attestation] = None
    if body.terminal_type == "merged_pr":
        project_row = session.get(Project, task.project_id)
        if project_row is None:
            raise HTTPException(
                status_code=422,
                detail=f"task {task_id}: this task's project repo cannot be resolved — nothing was recorded",
            )
        try:
            attestation = task_prs.attest(session, task, project_row, body.pr_url or "", body.pr_urls or ())
        except task_prs.PrRefused as exc:
            raise HTTPException(status_code=422, detail=exc.detail) from None
    elif body.pr_urls is not None:
        raise HTTPException(
            status_code=422,
            detail=f"task {task_id}: terminal type {body.terminal_type!r} does not carry pr_urls — nothing was recorded",
        )

    # ── 5. a state this persona may settle from ───────────────────────────────
    # Rendered from the tuple, never hand-spelled. The set depends on the terminal: `merged_pr` also recovers
    # a row the CP failed out from under a still-delivering runner (`persona_registry.settle_sources`).
    sources = settle_sources(persona, body.terminal_type)
    # A row parked only on an answer still being delivered resumes first: a stuck delivery never refuses the
    # runner's own settle (`resume_past_pending_answer_delivery`).
    if (task.state not in sources and ACTIVE in sources
            and resume_past_pending_answer_delivery(task_id, session)):
        session.refresh(task)
    if task.state not in sources:
        raise HTTPException(
            status_code=409, detail=_settle_state_refusal(task_id, persona, sources, task.state)
        )

    # ── 5a. parent shape, carried over rather than re-derived ─────────────────
    # A `finish` with no epic parent would be a `settled: false` nothing ever moves.
    if persona == PERSONA_FINISH and (
        task.parent_task_id is None or session.get(Task, task.parent_task_id) is None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} is a finish child with no epic parent and cannot settle: the finish "
                f"phase's whole report is computed from that epic's children — nothing was recorded"
            ),
        )
    # A top-level `plan`: the child count below would tally every top-level `coder` portfolio-wide.
    if persona == PERSONA_PLAN and task.parent_task_id is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} is a plan child with no parent epic and cannot settle: a plan's "
                f"children are counted under its epic, and a parentless one has no epic to count under "
                f"— nothing was recorded"
            ),
        )

    # ── 5a'. a merge-lane base check settles on a verdict the control plane holds ──
    # Its settle summary is the agent's claim; a certificate or a red record is the evidence. A run that settled
    # "certified" 49 s after it was filed, with no gate run, cost the next one a full re-gate.
    if edition_features.enabled("merge_lane"):
        from app import merge_lane

        unproven = merge_lane.base_check_without_verdict(session, task)
        if unproven:
            raise HTTPException(status_code=409, detail=f"task {task_id}: {unproven} — nothing was recorded")

    # ── 5b. a plan settles on its artifact AND its appended children ──────────
    # An explicit pre-check: the helper's copy returns False, wedging the row `active` behind a 200. The two
    # plan endings are exclusive on the filed count: `coders` needs ≥1, `analysis` (nothing to
    # build) needs 0 — a plan that appended children settles them, never a no-build.
    if persona == PERSONA_PLAN:
        filed = _epic_filed_count(task.parent_task_id, session, task)
        if body.terminal_type == "coders" and filed < 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"task {task_id} (persona={persona!r}) cannot settle: a plan settle requires ≥1 appended "
                    f"coder (append the fan-out FIRST, then settle) — zero coders appended, "
                    f"nothing was recorded (nothing to build? settle `analysis` instead)"
                ),
            )
        if body.terminal_type == "analysis" and filed >= 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"task {task_id} (persona={persona!r}) cannot settle `analysis` (nothing to build): "
                    f"{filed} child(ren) were appended, so this plan settles `coders` — nothing was recorded"
                ),
            )

    # ── 6. a triage's settle-eligibility IS its answered Q&A trail ────────────
    # The invariant every settle door carries; visibility-filtered, since an invisible round cannot be cleared.
    if persona == PERSONA_TRIAGE:
        open_question = session.scalars(
            select(TaskQuestion.id).where(
                TaskQuestion.task_id == task_id,
                TaskQuestion.status == "open",
                visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
            )
        ).first()
        if open_question is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"task {task_id} is a triage task with open question(s) — resolve the Q&A trail "
                    f"before settling; nothing was recorded"
                ),
            )

    # ── the payload write, which BOTH the refusal and the accept path keep ────
    gate_fields = {
        name: value for name, value in (
            ("clarity_score", body.clarity_score),
            ("spec_verdict", body.spec_verdict),
            ("critic_verdict", body.critic_verdict),
            ("recon_map", body.recon_map),
        ) if value is not None
    }
    # The state predicate is repeated, making step 5 a guard: a Reset committing in between would otherwise
    # let the abandoned attempt's note surface as the current artifact (`app/task_artifacts.py`). Zero rows is
    # step 5's answer.
    settleable = session.execute(
        update(Task).where(Task.id == task_id, Task.state.in_(sources))
        .values(
            outcome_summary=body.summary,
            **_settle_reference_values(body),
            **_plan_no_build_stamp(persona, body),
            **gate_fields,
            **task_actor_values(session),
        )
        .returning(Task.id)
    ).first()
    if settleable is None:
        session.rollback()
        raise HTTPException(status_code=409, detail=_settle_state_refusal(task_id, persona, sources))
    # One note kind for every settle, and `extra["summary"]` is required: readers treat a row without it as an
    # audit line (`app/task_artifacts.py::is_hidden_note_kind`).
    missing = missing_hard_kinds(task, supplied, session)
    advisory = missing_advisory_kinds(task, supplied, session)
    # Content-deduped against the newest visible note of this kind: personas settling from several states and
    # manifest-refused retries reach here twice. The visibility filter sits inside the ordering, so a hidden
    # newest row is skipped rather than deduped against (`app/task_artifacts.py`).
    newest = session.scalars(
        select(ActivityEvent)
        .where(ActivityEvent.kind == "task_note:outcome")
        .where(ActivityEvent.data["id"].astext == str(task_id))
        .where(visible_artifact_clause(
            ActivityEvent.created_at, task.artifacts_hidden_before))
        .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .limit(1)
    ).first()
    if newest is None or (newest.data or {}).get("summary") != body.summary:
        record_task_note(
            session, task_id, "outcome", body.summary,
            extra={
                "summary": body.summary,
                "terminal_type": body.terminal_type,
                "pr_url": body.pr_url,
                "no_pr_reason": body.no_pr_reason,
                "no_pr_evidence": body.no_pr_evidence,
                "manifest_advisory_missing": advisory,
            },
        )

    # ── 7. the manifest ───────────────────────────────────────────────────────
    accepted_incomplete = False
    if missing:
        # The increment is the decision, in one statement: guarded below the waiver, a match means refused
        # (`attempt` is the new count) and no match means this call is accepted. Read-then-increment would race.
        attempt = session.execute(
            update(Task)
            .where(Task.id == task_id, Task.settle_refusals < _MANIFEST_REFUSAL_LIMIT - 1)
            .values(settle_refusals=Task.settle_refusals + 1, **task_actor_values(session))
            .returning(Task.settle_refusals)
        ).scalar_one_or_none()
        if attempt is not None:
            session.commit()
            raise HTTPException(
                status_code=422,
                detail=(
                    f"task {task_id} (persona={persona!r}) cannot settle: missing required artifact(s): "
                    f"{', '.join(missing)}. Upload each one and settle again — your summary and "
                    f"references ARE stored, so you need not re-derive them. Refusal {attempt} of "
                    f"{_MANIFEST_REFUSAL_LIMIT - 1}; the {_MANIFEST_REFUSAL_LIMIT}th call is accepted "
                    f"and raises an operator alert."
                ),
            )
        accepted_incomplete = True
    # Commit before the helper: its artifact test is a SQL `EXISTS` that cannot see pending rows.
    session.commit()
    if accepted_incomplete:
        _raise_manifest_incomplete_alert(task_id, project_id, persona, missing, session)

    # ── 8. delegate to the persona's own settle helper ────────────────────────
    # The helper re-reads the row from this session's identity map; the ORM `update()` above synchronized
    # the loaded object, so a plan's `no_build` stamp is visible to `settle_analysis_child_on_artifact`. A
    # Core-level write here would need an explicit `session.expire(task)` first.
    settled = _delegate_settle(task_id, persona, body, session, attestation)
    session.expire_all()
    fresh = session.get(Task, task_id)
    # ── 9. project-memory votes (R-E1) ─────────────────────────────────────────
    # After the helper has moved the row, so they never delay it; a failure is logged and dropped — it never
    # refuses or changes the settle.
    if fresh is not None:
        _record_settle_memory_votes(session, fresh, body.memory_wrong or [], body.summary or "")
        _log_memory_proposed(session, fresh, body.memory_proposed)
    # A settled task has left the live states: a carry-over ends with it (`app/carryover.py`).
    if fresh is not None and fresh.state not in carryover.LIVE_STATES and carryover.close(session, task_id=task_id):
        session.commit()
    return {
        "stored": True,
        "settled": settled,
        "state": fresh.state if fresh else None,
        "terminal_type": body.terminal_type,
        "manifest_advisory_missing": advisory,
        "manifest_accepted_incomplete": accepted_incomplete,
    }


# ── A PR the task opened, as `jaah-forge` saw it ──────────────────
class PrObservedIn(BaseModel):
    pr_url: str = Field(min_length=1, max_length=2048)
    pr_number: int = Field(ge=1, le=2_147_483_647)
    state: Literal["open", "merged", "closed"]
    # `jaah-forge pr create` (true) or `pr view` (false): only a PR the task opened gets a row.
    opened: bool = False
    # Stamped by the daemon from the caller's checkout (`cred_broker`), never the caller's own argument; null is
    # the primary repository.
    repo_id: Optional[int] = None

    _check_pr_url = field_validator("pr_url")(validated_http_url)


@router.post("/tasks/{task_id}/prs", status_code=200, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def record_task_pr(
    task_id: int,
    body: PrObservedIn,
    caller: node_credentials.AgentCaller = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """`pull_request.observed`: upsert the PR the node daemon saw `jaah-forge pr create`/`pr view` return for this
    task (`app/task_prs.py::observe`). Bound to the node holding the task, as the settle is. 404 when `repo_id`
    is not one of the task's project's repositories; 422 when the URL is not that PR of that repository."""
    task = auth._task_or_404_for_node(session, caller, task_id)
    project = get_or_404(session, Project, task.project_id, detail=f"task {task_id} has no project")
    try:
        row = task_prs.observe(
            session, task, project, repo_id=body.repo_id, pr_url=body.pr_url, pr_number=body.pr_number,
            state=body.state, opened=body.opened,
        )
    except task_prs.PrRefused as exc:
        session.rollback()
        raise HTTPException(status_code=404 if exc.code == "unknown_repo" else 422, detail=exc.detail) from None
    if row is None:
        # A viewed PR the task never opened: nothing to record.
        session.rollback()
        return {"recorded": False}
    out = {"recorded": True, "repo_path": row.repo_path, "pr_number": row.pr_number, "pr_url": row.pr_url, "state": row.state,
           "is_primary": row.is_primary}
    session.commit()
    return out


# ── DB-direct fail ─────────────────────────────────────────────────
# Persona-agnostic (the transition guards on `state == active`) and thin. The CP's escalation park reaches
# the same transition in process (`app/task_pipeline_cp.py::block_task`), which still asks
# `_refuse_a_persona_with_no_terminal_call` and cascades via `_settle_compliance_group`.
class FailTaskIn(BaseModel):
    reason: str = Field(min_length=1, max_length=100_000)
    # Which runner session is reporting. Optional; supplied, it fences the report to its attempt
    # (`system_fail_dispatched_task`): a broken runner is re-dispatched into the same worktree within ~10s
    # (TRACKER-2335 rule 6), and a stale report must not end the resumed attempt.
    session_id: str = Field(default="", max_length=200)
    # The merge lane's "this node cannot run me" (its gate fence will not start): honoured for that persona
    # only, it leaves the node out for the project's lane tasks (`app/launch_failures.py`).
    node_fault: bool = False


def _refuse_a_persona_with_no_terminal_call(task_id: int, session: Session, *, verb: str) -> None:
    """404 an unknown id; 422 a persona whose done-criterion is `operator`.

    The three endings ask one question in one place. Such a persona's session may not move its row:
      · the block (`app/task_pipeline_cp.py::block_task`) would park it and leak the slot;
      · `/fail` spends an attempt and can land `system_failed`;
      · `/settle` would record an unearned terminal.
    The prompt forbids all three, but this persona reads untrusted material, so the prompt must not be the
    only control. Keyed on the criterion, not a persona literal.
    """
    task = get_task_or_404(task_id, session)
    if PERSONA_DONE_CRITERION.get(task.persona) == DONE_CRITERION_OPERATOR:
        raise HTTPException(
            status_code=422,
            detail=(
                f"task {task_id} (persona={task.persona!r}) cannot {verb}: it has no gate to raise and no "
                f"ending to declare — say it in the console, where the operator is, and only their "
                f"Complete ends this row"
            ),
        )


# The headers that declare a human-only blocker: the Full Auto kinds, plus the generic `Blocker` an interview
# task's runner may use.
_BLOCKER_HEADERS: frozenset[str] = frozenset(h.casefold() for h in (*FULL_AUTO_BLOCKER_HEADERS, "Blocker"))


def _has_answered_blocker_round(session: Session, task_id: int) -> bool:
    """Does TASK_ID carry a visible (not Reset-hidden) answered round whose header declares a blocker?

    A runner that fails after such a round has the human's answer already, so its `/fail` is terminal
    rather than spending a retry that re-asks or re-fails the same blocker."""
    hidden_before = session.scalar(select(Task.artifacts_hidden_before).where(Task.id == task_id))
    headers = session.scalars(
        select(TaskQuestion.header).where(
            TaskQuestion.task_id == task_id,
            TaskQuestion.status == "answered",
            TaskQuestion.header.is_not(None),
            visible_artifact_clause(TaskQuestion.created_at, hidden_before),
        )
    ).all()
    return any(h.strip().casefold() in _BLOCKER_HEADERS for h in headers)


@router.post("/tasks/{task_id}/fail", status_code=200, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def fail_task(
    task_id: int,
    body: FailTaskIn,
    _: None = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """DB-direct runner failure: the runner cannot proceed and reports it DB-side.

    Narrowed on the runner side: a blocker a human could clear is an `AskUserQuestion` round, never this
    (`portfolio/fleet/runner-charter.md` §"When you cannot proceed"); hooks and CP paths post here too.

    Not unconditionally terminal (`end_attempt`): with attempts left the task returns to `ready`; on the
    last it lands `system_failed` and rolls up the epic — and at once after an answered blocker round
    (`_has_answered_blocker_round`). 404 unknown id; 409 not a dispatched `active` row.
    """
    _refuse_a_persona_with_no_terminal_call(task_id, session, verb="fail")
    cause, node = runner_terminations.CAUSE_REPORTED_FAILURE, None
    lane_run = session.scalar(select(Task.persona).where(Task.id == task_id)) == merge_lane_state.LANE_PERSONA
    reason = body.reason
    # Every attempt's reason rides its termination note, redacted and capped: a retry discards `reason`
    # (`end_attempt`), so the note is each attempt's only record. A lane run's is also what the Merge lane tab
    # says it failed of.
    detail = merge_lane_state.run_failure_line(body.reason)
    if lane_run:
        # Redacted where it is stored too: a lane run is readable by every `tasks:view` holder on its project
        # (`authz._lane_run_clause`), and its reason quotes gate and forge stderr.
        reason = merge_lane_state.redacted_reason(body.reason)
    if body.node_fault and lane_run:
        # The node the lane ran on, read before `end_attempt` kills the runner (the slot stays bound).
        hold = slot_state.task_hold(session, task_id)
        node_uuid = node_id_or_none(session, hold["node_id"]) if hold else None
        if node_uuid is not None:
            cause, node = runner_terminations.CAUSE_NODE_FAULT, str(node_uuid)
    # The forge refused the lane run's credential and no retry was accepted since: every re-dispatch would be
    # refused the same way, so the run ends here naming the credential (`lane_forge_auth`).
    refused: Optional[CredentialSource] = None
    if edition_features.enabled("merge_lane"):
        from app import lane_forge_auth

        refused = lane_forge_auth.unresolved_forge_rejection(session, task_id) if lane_run else None
        if refused is not None:
            reason = lane_forge_auth.blocker_reason(refused)
    # As in `settle_task`: an answer stuck in delivery never refuses the runner's own fail. Only a caller naming the
    # row's session: hooks and CP paths post here unfenced, and must not drop an answer a live runner awaits.
    if body.session_id:
        resume_past_pending_answer_delivery(task_id, session, session_id=body.session_id)
    state = system_fail_dispatched_task(
        task_id, reason, session, session_id=body.session_id,
        # A human already answered the blocker this failure reports; a retry only asks the same thing again.
        final=refused is not None or _has_answered_blocker_round(session, task_id),
        # A live runner said it could not finish; `end_attempt` discards `reason` on the retryable branch, so this
        # row is the only record.
        cause=cause, node=node, note_detail=detail,
    )
    if state is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task_id} is not a dispatched 'active' task"
                + (f" running session {body.session_id!r}" if body.session_id else "")
                + " — cannot fail"
            ),
        )
    # Cascade to compliance_fix group mirrors only on `system_failed`: the cascade is terminal, and a
    # retryable failure is still queued.
    if edition_features.enabled("merge_lane"):
        from app import lane_forge_auth

        if state == SYSTEM_FAILED and refused is not None:
            lane_forge_auth.alert_credential_blocker(session, task_id, refused)
    if state == SYSTEM_FAILED:
        _settle_compliance_group(
            session.get(Task, task_id), SYSTEM_FAILED, session, reason=body.reason
        )
    return {"state": state}


# ── task_questions: write ─────────────────────────────────────────────────────
# The only writer of `task_questions`, called by the runner's fail-open `AskUserQuestion` capture hook;
# the console reads rows via `GET /api/tasks/{task_id}/questions` (shape in tasks.py).
class TaskQuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=100_000)
    # Ordered offered choices (an object per choice may carry a `recommended_index` marker, or a plain
    # string list) — free-form JSONB. None for a free-form question with no discrete options.
    options: Optional[list[Any]] = None
    # The question's `multiSelect`. Default False keeps pre-0117 hooks working. Column:
    # `app/models/tasks.py::TaskQuestion.multi_select`. Strict because this is the trust boundary and Pydantic
    # coerces `"true"`/`1` to True (the node's question-capture hook enforces the same rule client-side).
    multi_select: StrictBool = False
    # The question's short `header`, distinguishing rounds of a batch. `None` keeps pre-0145 hooks working.
    # Column: `app/models/tasks.py::TaskQuestion.header` (unbounded `Text`). `max_length=200` is a sanity bound at
    # the trust boundary, not the tool's ~12-character convention.
    header: Optional[str] = Field(default=None, max_length=200)
    # The Architectural Guard rule a "Guard" stuck-limit round is about, read by the capture hook from the guard's
    # state file (S6). Column: `app/models/tasks.py::TaskQuestion.guard_context`; shape kept by
    # `app/governance/guard_escalation.py::clean_context`, which also drops it on any other header.
    guard_context: Optional[dict[str, Any]] = None


def _announce_question_round_in_task_scope(
    task_id: int, question_id: int, caller_scope: "UUID | object | None"
) -> None:
    """Announce one question round under the org of the task, on a session of this function's own.

    `bind_from`, not `operator_scope()`: this is per-task notification state. It re-derives rather than
    inheriting the request's bind (hence `seam.detached()`): the question belongs to the task's org, and
    `bind_from` would raise `TenancyScopeConflict` on fleet-wide arms inside a background task with no error
    surface. Its own `SessionLocal()`, since the request's is closed; it only resolves the org, as in
    `durable/task_notify_queue.py::deliver_task_notification`. Never raises (`announce_question_round`
    swallows its own failures); a deleted task is logged and returned.

    `caller_scope` is captured by the route, since the request's scope is gone here. It is needed because
    `detached()` plus `bind_from` disarms both cross-org guards, and `bind_from` is an existence oracle with
    no entitlement check: a node in org A could otherwise post into org B's Slack via an enumerable
    `task_id`. So the announce is refused (logged, not raised) when the caller is not `seam.OPERATOR` and
    both known orgs differ. Residual: on `cp_self`/`insecure` arms `create_task_question`'s bare
    `session.get` still writes the row; this guarantees only that the announce stays in the caller's tenant.
    """
    try:
        with SessionLocal() as session, seam.detached(), seam.bind_from(session, Task, task_id) as org:
            # The entitlement check the composition removed. `OPERATOR` is entitled to every org; anything else must
            # match. Refuses only when both sides are known (`app/automation_fire.py::_owner_or_reason`): `org is not None`
            # is unreachable now (`tasks.org_id` NOT NULL, 0192) but kept as the bind's own `None` arm;
            # `caller_scope is not None` is still live. The real
            # crossing has orgs on both sides, so these arms cost the property nothing; do not tighten them.
            if (
                caller_scope is not seam.OPERATOR
                and caller_scope is not None
                and org is not None
                and caller_scope != org
            ):
                log.warning(
                    "task-notify: refusing to announce question round %s on task %s — the caller is "
                    "scoped to %r and the task derives org %r, so announcing would carry this round "
                    "across the tenancy boundary; the round was recorded but not announced",
                    question_id, task_id, caller_scope, org,
                )
                return
            # Release the pooled connection before the up-to-25s Slack relay; the scope is a ContextVar region,
            # unaffected (precedent: `durable/task_notify_queue.py::deliver_task_notification`).
            session.rollback()
            task_notify.announce_question_round(task_id, question_id)
    except seam.TenancyRowMissing:
        log.warning(
            "task-notify: task %s no longer exists — question round %s was recorded but not announced",
            task_id, question_id,
        )


def record_question_round(
    session: Session, task: Task, *, question: str, options: Optional[list[Any]], multi_select: bool,
    header: Optional[str], guard_context: Optional[dict[str, Any]] = None,
) -> TaskQuestion:
    """Write one open `task_questions` round and park its task on it — the ONE writer both question routes
    call (`create_task_question` for a runner's capture hook, `agent_sdk.ask_question_batch` for an SDK
    agent). The caller commits and announces.

    409 when no runner is running (`_RUNNER_RUNNING_STATES`): a round is answerable only by delivery into the
    live session, and the CP decides a runner stopped before the node enacts the kill, so a dying pane can
    still post (`runner_terminations.drop_undelivered_questions` is the other half). Both states matter:
    later questions of a batch post from `awaiting_response`.

    422 for an SDK agent's question outside `FULL_AUTO_BLOCKER_HEADERS` on a Full Auto task: its
    process speaks the daemon directly, so the rule a runner's capture hook enforces client-side is enforced
    here, whichever verb it asked through."""
    if task.run_agent_id is not None and task.autonomous and header not in FULL_AUTO_BLOCKER_HEADERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"this task runs in Full Auto, so it may ask only about a blocker a human alone can clear, "
                f"with `header` set to one of {', '.join(FULL_AUTO_BLOCKER_HEADERS)} — got {header!r}; "
                f"make the decision yourself and record why"
            ),
        )
    if task.state not in _RUNNER_RUNNING_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task {task.id} is {task.state!r} — no runner is running against it, so a question "
                f"raised now could never be answered back into the session that asked it; nothing was "
                f"recorded"
            ),
        )
    # The task row lock orders this capture after an answer settle in flight (`_open_round_or_409` holds the
    # same lock), so the supersede below sees the delivery that settle minted.
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update())
    # An edition without compliance has no Architectural Guard, so no question is a guard escalation.
    context = None
    if edition_features.enabled("compliance"):
        from app.governance import guard_escalation

        context = guard_escalation.clean_context(guard_context) \
            if (header or "").casefold() == guard_escalation.HEADER.casefold() else None
        if context is not None:
            options = guard_escalation.worded_options(options, context.get("mode"))
    row = TaskQuestion(
        task_id=task.id, question=question, options=options,
        multi_select=multi_select, header=header, status="open", guard_context=context,
    )
    session.add(row)
    session.flush()
    # The runner is past any answer still in flight to it: retire that delivery, re-quote its rounds next time.
    supersede_pending_answer_delivery(task.id, session)
    # Park the task on its question so it is visible. `await_response_on_dispatched_task` writes the
    # `question_raised` note on a won transition; otherwise it is written here.
    if await_response_on_dispatched_task(task.id, question, session) is None:
        record_task_note(
            session,
            task.id,
            "question_raised",
            # The question only; the label already says "Question raised".
            question[:120],
            # `multi_select` and `header` ride the note, which re-dispatched runners and the Q&A trail read back.
            extra={
                "question": question, "options": options,
                "multi_select": multi_select, "header": header,
            },
        )
    return row


# `/questions` is the legacy alias of `/input-requests`, kept until the legacy-deletion task.
@router.post("/tasks/{task_id}/input-requests", status_code=201, response_model=TaskQuestionOut)
@router.post("/tasks/{task_id}/questions", status_code=201, response_model=TaskQuestionOut)
@infra(Tenancy.ORG_SCOPED)
def create_task_question(
    task_id: int,
    body: TaskQuestionIn,
    background: BackgroundTasks,
    _: None = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> TaskQuestionOut:
    """Insert an open `task_questions` row for a runner's blocking question — the multi-round triage writer.
    404 on a missing Task; the row is always `status='open'`. Each question is a new round; a human answers
    via `POST /api/tasks/{id}/input-requests/{question_id}/answer`, and the Task settles only once no round is
    `open`. The returned `round` is derived at read time (the post-insert count of visible questions, so a
    Reset's next attempt starts at Round 1).

    409 when no runner is running (`record_question_round`). Safe: the capture hook is fail-open.
    """
    task = get_task_or_404(task_id, session)
    row = record_question_round(
        session, task, question=body.question, options=body.options,
        multi_select=body.multi_select, header=body.header, guard_context=body.guard_context,
    )
    session.commit()
    session.refresh(row)
    # After the commit, so the workflow finds the row; never raises. Deferred: the queue module pulls `dbos`.
    from app.durable.question_plain_queue import enqueue_plain

    enqueue_plain(task_id, row.id)
    # Announce per round: the transition fires only for a batch's first round (guarded on `ACTIVE`), so it
    # would drop later rounds. Queued through `_announce_question_round_in_task_scope` (see its docstring).
    # The caller's scope is captured here, the only frame where `seam.current_scope()` still answers "who
    # asked"; the wrapper uses it as the entitlement conjunct.
    background.add_task(
        _announce_question_round_in_task_scope, task_id, row.id, seam.current_scope()
    )
    # Off the request: a Slack post can take 25s against a 30s CP-hop budget, and inline announcing could get
    # a batch killed mid-way. Queued after `commit()` so the announcer sees the row.

    # Read-time round: the post-insert count of visible questions (this row is the newest). Visible only, to
    # match the console's numbering (`app/routes/tasks/qa_render.py::_load_task_questions_out`).
    round_index = session.scalar(
        select(func.count()).select_from(TaskQuestion).where(
            TaskQuestion.task_id == task_id,
            visible_artifact_clause(TaskQuestion.created_at, task.artifacts_hidden_before),
        )
    ) or 1
    # `resolved_by=None` is a fact: the row was just inserted `open`.
    return _task_question_out(row, round_index, resolved_by=None)


# ── The runner binding: which session, in which worktree ──────────────────────
class RunnerSessionIn(BaseModel):
    """The live runner's identity on the Task row. The two column fields are optional and independent: only
    those present (`model_fields_set`) are written, so clearing `runner_session_id` does not blank
    `worktree_ref`. `account_dir` is read once for the activity note, never persisted.
    """

    # None clears it (the runner session ended / is no longer resumable).
    runner_session_id: Optional[str] = None
    # The pool worktree the runner is executing in, e.g. `acme-2`. None clears it.
    worktree_ref: Optional[str] = None
    # The credential dir the runner launched with (`CLAUDE_CONFIG_DIR`). Not a task column: which account ran
    # is a per-attempt fact, used only to derive the account slug for the `runner_session` note.
    account_dir: Optional[str] = None


def _runner_session_facts(session: Session, task_id: int, slug: str) -> dict:
    """What `set_task_runner_session`'s session row learns from the lease: node, account, cwd, and the
    `agent_kind` its account's provider ran (a non-Claude runner is not a `claude_code` session). An SDK agent
    run (the task's `run_agent_id` stamp) is `sdk_agent` with no account and an `agent_ref` naming the agent,
    its version and the commit it ran (DD16, AC-4)."""
    agent_ref = agent_resolution.agent_ref_for_task(session, task_id)
    # The runtime the dispatch chose, so a session that dies before its first event is not read as the
    # `legacy` default.
    runtime = fleet_verdicts.dispatch_runtime_for_task(session, task_id)
    if agent_ref is not None:
        return {
            "node_id": agent_session_lifecycle.node_for_task(session, task_id),
            "agent_kind": AGENT_KIND_SDK_AGENT,
            "agent_ref": agent_ref,
            "cwd": agent_session_lifecycle.task_slot_path(session, task_id),
            "dispatched_runtime": runtime,
        }
    account = session.execute(
        select(Account.id, Account.provider).where(Account.slug == slug)
    ).first() if slug else None
    return {
        "node_id": agent_session_lifecycle.node_for_task(session, task_id),
        "account_id": account.id if account else None,
        "agent_kind": agent_session_lifecycle.agent_kind_for_provider(account.provider if account else None),
        "cwd": agent_session_lifecycle.task_slot_path(session, task_id),
        "dispatched_runtime": runtime,
    }


def record_runner_start(
    session: Session, task: Task, runner_session_id: str, *, account_slug: str = "",
    worktree_ref: Optional[str] = None, node_id: Optional[UUID] = None, runtime: Optional[str] = None,
) -> None:
    """The bookkeeping a runner's START owes, beside the `runner_session_id` column itself: the activity
    log's `runner_session` note, the agentic session row (`agent_session_lifecycle.note_start`) and the Jaah
    account's session link. Does not commit.

    Two callers: the node's runner-session callback below (a static slot's runner, after it launched), and
    `microvm_placement.admit`, which minted the id itself and binds it right after the claim — a VM task
    has no slot, so it names the VM's `node_id` (`node_for_task` reads a slot)."""
    task_id = task.id
    record_task_note(
        session, task_id, "runner_session",
        # Empty on purpose: the label and `data.session_id` already carry it; the feed reads `extra`.
        "",
        # The node and slot it launched in (`app/runner_places.py`), so where it ran outlives the hold.
        extra={"session_id": runner_session_id, **({"account_slug": account_slug} if account_slug else {}),
               **runner_places.stamp(session, task_id)},
    )
    # The fleet half of "a row exists from the moment a session starts" (AC-2.1). The node comes from the
    # task's lease unless the caller names it (a NULL `node_id` row appears in no node's `/work`). Best effort.
    # Not for a script persona (the merge lane): no agent runs, so there is no agentic session and no
    # transcript for the transcript sweeps to chase.
    if not merge_lane_state.is_lane_persona(task.persona):
        def facts() -> dict:
            found = _runner_session_facts(session, task_id, account_slug)
            # A VM task holds no slot, so its caller names the node and the runtime it launched.
            return {**found, **({"node_id": node_id} if node_id is not None else {}),
                    **({"dispatched_runtime": runtime} if runtime is not None else {})}

        agent_session_lifecycle.note_start(
            session,
            agent_session_id=runner_session_id,
            project_id=task.project_id,
            task_id=task_id,
            worktree_ref=worktree_ref,
            # `AgentSession.holder` records what started the session; "fleet" is the one origin left, spelled as a
            # literal since `slot_holders.HOLDER_FLEET` is gone.
            holder="fleet",
            # Lookups go through `resolve` so they run inside the savepoint; a kwarg would evaluate outside and could
            # fail the dispatch.
            resolve=facts,
        )
    # A run on the Jaah account: the session row it was dispatched under names this runner from now on.
    if edition_features.enabled("jaah_ai_credit"):
        from app.jaah_routing import sessions as jaah_sessions

        jaah_sessions.link_agent_session(session, task_id, runner_session_id)


@router.post("/tasks/{task_id}/runner-session", status_code=204, response_model=None)
@infra(Tenancy.ORG_SCOPED)
def set_task_runner_session(
    task_id: int,
    body: RunnerSessionIn,
    _: None = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> None:
    """Bind a Task to the runner executing it — its `claude --resume` session id and pool worktree. 404 on a
    missing Task.

    A running task must name its worktree, or nothing maps a Task to its process (sweep settles look like
    runner completions; settled tasks' runners cannot be reaped). Written at launch (best effort) and
    cleared on the terminal settle, so non-NULL `worktree_ref` means a runner holds it now.
    """
    task = get_task_or_404(task_id, session)
    provided = {
        field: getattr(body, field)
        for field in ("runner_session_id", "worktree_ref")
        if field in body.model_fields_set
    }
    if provided:
        session.execute(
            update(Task).where(Task.id == task_id)
            .values(**provided, **task_actor_values(session))
        )
    # "Runner start" for the activity log; a null id is a clear and gets no row. The paired end is the
    # terminal transition row; duration is derived at read time.
    if body.runner_session_id:
        # The account slug only (emails are per-node data resolved at read time). `account_slug_from_dir` and
        # `_SLUG_RE` bound it; an unbounded basename would persist forever. Either refusal records nothing.
        slug = account_slug_from_dir(body.account_dir)
        if not _SLUG_RE.match(slug):
            slug = ""
        record_runner_start(
            session, task, body.runner_session_id, account_slug=slug,
            worktree_ref=provided.get("worktree_ref") or task.worktree_ref,
        )
    session.commit()


# Per-child metrics ride the session-transcript sweep's single read
# (`app/routes/agent_sessions.py::report_transcript`, folded by `app/task_metrics_ingest.py`); there is
# no separate metrics route or work-list.

# No transient dispatch state to reap: the `* -> ready` edges are one atomic `_start`
# (`app/routes/tasks/`), and occupancy is `free`/`task`/`user` on the one `worktrees` row.


# ── plan → coders append ──────────────────────────────────────
# An epic's `plan` child posts its breakdown here while still `active`, then emits stage-complete.
# Gh-free; rows and edges single-home in `append_tasks`, which accepts an `active` or `done`
# child.
class AppendTaskIn(BaseModel):
    # Titles are capped at ≤6 content words by the writer `append_tasks`, which normalizes rather than
    # rejects (a 422 would cost a re-decomposition); its callers inherit the guard.
    title: str
    body: str
    # 0-based indices into this `tasks` list; every coder also depends on the plan child (server-side).
    # Empty for a flat breakdown that dispatches in parallel.
    depends_on: list[int] = Field(default_factory=list)
    # 1-based phase ordinal and name, both nullable. Same-phase children run concurrently; each phase k+1
    # child auto-depends on every phase-k child (`append_tasks`, never the caller).
    phase_index: Optional[int] = Field(default=None, ge=1)
    phase_name: Optional[str] = None
    # Which build persona runs the child: `coder` (the default), `translate` for a child whose whole
    # work is localizing text, or `marketer` for marketing copy. Refused (422) outside `EPIC_BUILD_PERSONAS`.
    persona: Optional[str] = None

    @field_validator("persona")
    @classmethod
    def _persona_is_a_build_persona(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in EPIC_BUILD_PERSONAS:
            raise ValueError(f"persona must be one of {', '.join(EPIC_BUILD_PERSONAS)} (got {value!r})")
        return value


class AppendTasksIn(BaseModel):
    tasks: list[AppendTaskIn]


class AppendTasksResult(BaseModel):
    created: list[int]
    # Prior coder/finish children this call archived (`append_tasks`' atomic archive+replace); `[]` on the
    # first append. Kept, never deleted.
    archived: list[int]


@router.post(
    "/tasks/{task_id}/append-tasks", response_model=AppendTasksResult
)
@infra(Tenancy.ORG_SCOPED)
def append_tasks_route(
    task_id: int,
    body: AppendTasksIn,
    x_agent_task_id: str = Header(default=""),
    _: None = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> AppendTasksResult:
    """Append coder Tasks to an epic from its plan child's breakdown. The plan runner posts it as
    its work while `active`, then emits stage-complete. Rows and edges come from `append_tasks`
    (app/routes/tasks.py). 422 on an empty list; 404 on an unknown id; a non-epic-plan or undispatched
    target is a guarded no-op.

    Every call is an atomic replace: current coder and finish children are archived and the new breakdown
    created in one transaction, so retries converge and re-plans land. `active` children are left alone. A
    bad `depends_on` aborts the whole append. A self-declared Task caller needs a `SHAPE_TASKS` licence
    (the `plan` persona); `append_tasks` checks whose breakdown it is.
    """
    _guard_task_creating_caller(x_agent_task_id, session, shape=SHAPE_TASKS)
    if not body.tasks:
        raise HTTPException(
            status_code=422,
            detail="tasks must be non-empty — a plan append needs at least one coder",
        )
    plan_child = get_task_or_404(task_id, session)
    result = append_tasks(
        plan_child,
        [c.model_dump() for c in body.tasks],
        session,
    )
    return AppendTasksResult(**result)


# ── finish → follow-up coder (the finisher's LAST SHOT rule) ────
# The `finish` child files a gap it cannot close as a visible coder child of its own epic, next to the
# build that raised it. Rows, dedup and the dependency edge single-home in `append_finish_follow_up`.
class FinishFollowUpIn(BaseModel):
    # Capped at ≤6 content words by the writer, which normalizes (see `AppendTaskIn.title`).
    title: str = Field(min_length=1)
    # Stored verbatim. Capped because it reaches the runner launch as one argv element; past ~128KiB every
    # dispatch fails `E2BIG`.
    body: str = Field(min_length=1, max_length=MAX_TASK_TEXT_LEN)
    # Required: a re-dispatched finish runner re-finds the same gap, and without a stable key each run stacks
    # a child and the epic can never roll up. No coarse fallback (distinct gaps must not collapse). Bounded
    # and charset-restricted: a stored column and DB predicate.
    dedup_key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    # `dedup_key`s of follow-ups this epic's finish phase already filed that must be `done` first. A real edge,
    # so the scheduler holds the row — never a "run only after X" gate written into `body`. Applied on first
    # filing only: a repeat `dedup_key` writes nothing, edges included.
    depends_on: list[Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")]] = Field(
        default_factory=list, max_length=50,
    )


class FinishFollowUpOut(BaseModel):
    id: int
    # False when `dedup_key` matched a live follow-up, returned untouched (it may be leased); True on insert.
    created: bool
    epic_id: int


@router.post("/tasks/{task_id}/follow-up", response_model=FinishFollowUpOut)
@infra(Tenancy.ORG_SCOPED)
def file_finish_follow_up(
    task_id: int,
    body: FinishFollowUpIn,
    x_agent_task_id: str = Header(default=""),
    _: None = Depends(_check_token_for_task),
    session: Session = Depends(get_session),
) -> FinishFollowUpOut:
    """File a Finish-phase follow-up as a visible coder of `task_id`'s epic. `task_id` is the calling finish
    child's own id: authority is path-scoped, checked by the writer. 404 when it is not a live `finish` child
    of a live epic (archived, undispatched, abandoned, settled), since nothing would sequence the follow-up.

    Idempotent on `dedup_key` (`created=false`, nothing written). The `X-Agent-Task-Id` guard asks who is
    filing (`SHAPE_FOLLOW_UP`, the `finish` licence); the path asks whose finish phase.
    """
    _guard_task_creating_caller(x_agent_task_id, session, shape=SHAPE_FOLLOW_UP)
    finish = session.get(Task, task_id)
    result = (
        append_finish_follow_up(finish, body.model_dump(), session) if finish is not None else None
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"finish task {task_id} not found")
    return FinishFollowUpOut(**result)
