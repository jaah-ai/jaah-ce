"""The release API's read shapes (the pinned web ↔ API seam): the project tab, a Release, the org overview row and
its status chip. Plain dicts the routes return as JSON; ids of stages are uuid strings, of releases integers.

`StatusChip` = up_to_date | awaiting_approval | scheduled | queued | deploying | live | deployed | failed | frozen, or
null when nothing was ever deployed and changes wait — `deployed` is a `shipped` release (merged where no deploy
events are connected). What waits is never a status: the row's `waiting_count`/`extra_commits` draw beside it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jira_api, merge_lane_state
from app.actors import actor_names
from app.authz.principal import Principal
from app.entity_shape import iso_or_none, optional_uuid
from app.models.automations import TaskTrigger
from app.models.hosted_apps import HostedApp
from app.models.merge_lane import MergeLaneRow
from app.models.production_qa import Deployment
from app.models.projects import Project
from app.models.releases import Release, ReleaseApproval, ReleaseFreeze, ReleaseFreezeStage, ReleaseJob, ReleaseStage
from app.releases import access, executor, lifecycle, override as override_mod, service, snapshot, \
    stages as stages_mod, waiting as waiting_mod
from app.pipeline.rules import StepRules
from app.releases.waiting import Change
from app.triggers import SOURCE_AZURE_DEVOPS, SOURCE_BITBUCKET, SOURCE_GITHUB, SOURCE_GITLAB

_DEPLOY_SOURCE_KINDS = (SOURCE_GITHUB, SOURCE_GITLAB, SOURCE_AZURE_DEVOPS, SOURCE_BITBUCKET)
#: The newest changes a list carries (a waiting step, a release): before a project's first survey every change
#: ever merged is waiting, and no screen lists thousands. The org row's `waiting_count` is the full count.
CHANGES_SHOWN = 500


def _cancellation_out(release: Release, cancellation: dict[str, Any], names: dict[UUID, str]) -> dict[str, Any]:
    """Why a canceled release ended, with only what its kind carries: `{kind}`, a person's `by` and `comment`, the
    newer release that superseded it (`by_release_id`)."""
    out: dict[str, Any] = {"kind": cancellation.get("kind")}
    if cancellation.get("by_id"):
        out["by"] = _person(names, _cancelled_by(release))
    if "comment" in cancellation:
        out["comment"] = cancellation.get("comment")
    if cancellation.get("by_release_id") is not None:
        out["by_release_id"] = cancellation["by_release_id"]
    return out


def _cancelled_by(release: Release) -> Optional[UUID]:
    """Who canceled `release` (`cancellation.by_id`), when a person did."""
    raw = (release.cancellation or {}).get("by_id")
    try:
        return UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _person(names: dict[UUID, str], user_id: Optional[UUID]) -> Optional[dict[str, Any]]:
    if user_id is None:
        return None
    return {"id": str(user_id), "name": names.get(user_id, "")}


def _uuid(override: Optional[dict[str, Any]]) -> Optional[UUID]:
    by = (override or {}).get("by_id")
    try:
        return UUID(str(by)) if by else None
    except ValueError:
        return None


def _override_out(override: Optional[dict[str, Any]], names: dict[UUID, str]) -> Optional[dict[str, Any]]:
    """`{by, at, reason, freeze_name}` of a release run through a freeze, else None."""
    if not override:
        return None
    return {"by": _person(names, _uuid(override)), "at": override.get("at"), "reason": override.get("reason"),
            "freeze_name": override.get("freeze_name")}


def _untested_out(release: Release, names: dict[UUID, str]) -> Optional[dict[str, Any]]:
    """`{by, at, reason}` of a release a person chose to ship untested, else None."""
    untested = override_mod.untested_of(release)
    if not untested:
        return None
    return {"by": _person(names, _uuid(untested)), "at": untested.get("at"), "reason": untested.get("reason")}


def change_out(change: Change, names: dict[UUID, str], roots: dict[int, int]) -> dict[str, Any]:
    """`root_task_id`: the top-level task the board lists for the change (an epic for its child's merge)."""
    task_id = change.task_id
    return {
        "change_id": str(change.change_id), "title": change.title, "author": _person(names, change.author_id),
        "merged_at": iso_or_none(change.merged_at), "task_id": task_id, "pr_url": change.pr_url,
        "root_task_id": roots.get(task_id, task_id) if task_id is not None else None,
    }


class Reader:
    """One request's view over one project: the stages, the merged changes and the names, each read once."""

    def __init__(self, session: Session, project: Project, principal: Optional[Principal]):
        self.session = session
        self.project = project
        # The `can_*` flags answer as the write will run (`access.as_writer`).
        self.principal = access.as_writer(principal, project.org_id)
        # Read only: a GET builds a Reader, and a cookie-authenticated GET never writes.
        # The branch-level stages: the steps between them are releases (`stages.branch_stages`).
        self.stages = stages_mod.branch_stages(stages_mod.for_read(session, project))
        self.by_id = {s.id: s for s in self.stages}
        self.changes = waiting_mod.merged_changes(session, project) if self.stages else []
        self._names: dict[UUID, str] = {}
        self._asks: Optional[dict[Any, Any]] = None

    def asks_ticket(self, stage: ReleaseStage) -> Optional[str]:
        """The ticket system the step INTO `stage` asks a key of when pressed (D8), or None."""
        if self._asks is None:
            from app.pipeline_gates import tickets  # noqa: PLC0415 — pipeline_gates imports this package

            self._asks = tickets.ask_gates(self.session, self.project.id)
        gate = self._asks.get(stage.id)
        return str((gate.config or {}).get("system")) if gate is not None else None

    def ticket_project(self, stage: ReleaseStage) -> Optional[str]:
        """The Jira project key (OAM in OAM-123) of the login the step INTO `stage` asks a ticket through, for the
        field's example; None when it asks none, on another system, or the viewer can't use that login."""
        if self.asks_ticket(stage) != "jira" or self.principal is None:
            return None
        from app.pipeline_gates import tickets  # noqa: PLC0415 — pipeline_gates imports this package

        gate = (self._asks or {})[stage.id]
        try:
            credential = tickets.resolve(self.session, self.principal, self.project.id, gate.credential, "jira")
        except tickets.TicketError:
            return None
        key = jira_api.project_key_of(credential.config)
        return key.upper() if key else None

    def names(self, ids: Iterable[Optional[UUID]]) -> dict[UUID, str]:
        wanted = {i for i in ids if i is not None and i not in self._names}
        if wanted:
            self._names.update(actor_names(self.session, wanted))
        return self._names

    def roots(self, changes: Iterable[Change]) -> dict[int, int]:
        """`{task id: its top-level task's id}` for `changes`: the row the board lists each change as."""
        from app.pipeline.membership import roots_of  # noqa: PLC0415 — app.pipeline imports app.releases

        return roots_of(self.session, {c.task_id for c in changes if c.task_id is not None})

    def waiting(self) -> dict[UUID, list[Change]]:
        return waiting_mod.waiting(self.session, self.project, self.stages, self.changes)

    def releases_out(self, releases: list[Release]) -> list[dict[str, Any]]:
        ids = [r.id for r in releases]
        approvals: dict[int, list[ReleaseApproval]] = {i: [] for i in ids}
        if ids:
            for row in self.session.scalars(
                select(ReleaseApproval).where(ReleaseApproval.release_id.in_(ids)).order_by(ReleaseApproval.id)
            ):
                approvals[row.release_id].append(row)
        deployments = {
            d.id: d for d in self.session.scalars(
                select(Deployment).where(Deployment.id.in_([r.deployment_id for r in releases if r.deployment_id]))
            )
        } if any(r.deployment_id for r in releases) else {}
        carried = waiting_mod.carried(self.session, self.project, ids, self.changes)
        names = self.names(
            [r.requested_by_id for r in releases]
            + [a.user_id for rows in approvals.values() for a in rows]
            + [c.author_id for rows in carried.values() for c in rows]
            + [_cancelled_by(r) for r in releases]
            + [_uuid(r.freeze_override) for r in releases]
            + [_uuid(override_mod.untested_of(r)) for r in releases]
        )
        roots = self.roots([c for rows in carried.values() for c in rows[:CHANGES_SHOWN]])
        from app.pipeline_gates import results as gate_results  # noqa: PLC0415 — pipeline_gates imports this package

        gates = gate_results.failed_gates(self.session, self.project.id, "release",
                                          [str(r.id) for r in releases if r.state == lifecycle.FAILED])
        return [{**self._release_out(r, approvals[r.id], deployments.get(r.deployment_id), carried[r.id], names,
                                     roots),
                 "failed_gate": gates.get(str(r.id)), "ticket": (r.inputs or {}).get("ticket")}
                for r in releases]

    def release_out(self, release: Release) -> dict[str, Any]:
        return self.releases_out([release])[0]

    def _release_out(self, release: Release, approvals: list[ReleaseApproval], deployment: Optional[Deployment],
                     changes: list[Change], names: dict[UUID, str], roots: dict[int, int]) -> dict[str, Any]:
        # The target as the release was requested: its approvals and who (`snapshot.rule`).
        stage = snapshot.rule(release, self.by_id.get(release.to_stage_id) if release.to_stage_id else None)
        open_ = release.state in lifecycle.OPEN
        may_approve = bool(stage is not None and release.state == lifecycle.AWAITING_APPROVAL
                           and access.may_approve(self.principal, self.project.id, stage, release))
        cancellation = release.cancellation or None
        return {
            "id": release.id,
            "kind": release.kind,
            "from_stage_id": str(release.from_stage_id) if release.from_stage_id else None,
            "to_stage_id": str(release.to_stage_id) if release.to_stage_id else None,
            "state": release.state,
            "requested_by": _person(names, release.requested_by_id),
            "requested_at": iso_or_none(release.requested_at),
            "scheduled_for": iso_or_none(release.scheduled_for),
            "approvals": [{"user": _person(names, a.user_id), "at": iso_or_none(a.approved_at), "comment": a.comment}
                          for a in approvals],
            "approvals_required": stage.approvals_required if stage is not None else 0,
            "can_approve": may_approve and not any(
                str(a.user_id) == str(getattr(self.principal, "user_id", None)) for a in approvals),
            "can_reject": may_approve,
            # Shown wherever an approver acts (R3.4), in full.
            "instructions": StepRules.of(stage).instructions if stage is not None else "",
            # Why it was canceled (timed out, rejected by whom and what they said, superseded by which release);
            # None when it was not.
            "cancellation": None if cancellation is None else _cancellation_out(release, cancellation, names),
            "can_cancel": bool(
                stage is not None and open_ and access.may_cancel(self.principal, self.project.id, stage, release)
            ),
            "pr_url": release.pr_url,
            "merge_sha": release.merge_sha,
            "head_sha": release.head_sha,
            # The executor reports the pinned commit count; until it does, none is known.
            "commits": None,
            "deployment": None if deployment is None else {
                "id": deployment.id, "run_url": deployment.run_url, "status": deployment.status,
                "finished_at": iso_or_none(deployment.finished_at),
            },
            "failure": release.failure,
            # The release's history line for a freeze override (R7.5): who, when, which freeze, why.
            "freeze_override": _override_out(release.freeze_override, names),
            # Shipped without the full gate on a person's reason (`override.untested_of`): `{by, at, reason}`.
            "untested": _untested_out(release, names),
            "changes": [change_out(c, names, roots) for c in changes[:CHANGES_SHOWN]],
        }

    # ── The project tab ──────────────────────────────────────────────────────────────────────────────────

    def open_releases(self) -> list[Release]:
        return service.open_releases(self.session, self.project.id)

    def current(self, stage: ReleaseStage) -> Optional[Release]:
        """The latest release that arrived on `stage`."""
        return self.session.scalar(
            select(Release).where(
                Release.project_id == self.project.id, Release.to_stage_id == stage.id,
                Release.state.in_(tuple(lifecycle.ARRIVED)),
            ).order_by(Release.id.desc()).limit(1)
        )

    def last_release(self, stage: ReleaseStage) -> Optional[Release]:
        return self.session.scalar(
            select(Release).where(Release.project_id == self.project.id, Release.to_stage_id == stage.id)
            .order_by(Release.id.desc()).limit(1)
        )

    def extra_commits(self) -> dict[str, Optional[int]]:
        """`{from_stage_id: n|null}` from the latest finished survey (`payload.extra_commits`, keyed by from-stage
        id); null until the first survey lands; 0 while a promote of that step is open, and 0 once one arrived after
        that survey began (its count predates the promote, which shipped those commits; the survey the tick files once a
        release ends re-counts). A promote files no back-merge (only a hotfix does, `runs._file_backmerge`): the
        target's merge commit is one the next promote's merge already holds."""
        job = self.session.scalar(
            select(ReleaseJob).where(
                ReleaseJob.project_id == self.project.id, ReleaseJob.mode == "survey", ReleaseJob.done_at.is_not(None),
            ).order_by(ReleaseJob.done_at.desc()).limit(1)
        )
        counts = ((job.payload or {}).get("extra_commits") or {}) if job is not None else {}
        # An open promote merges the whole source branch: its extra commits are in flight, not waiting.
        promoting = {r.to_stage_id for r in self.open_releases() if r.kind == lifecycle.KIND_PROMOTE}
        if job is not None:
            began = job.claimed_at or job.requested_at
            promoting |= set(self.session.scalars(select(Release.to_stage_id).where(
                Release.project_id == self.project.id, Release.kind == lifecycle.KIND_PROMOTE,
                Release.state.in_(tuple(lifecycle.ARRIVED)), Release.updated_at > began,
            )))
        out: dict[str, Optional[int]] = {}
        for index, stage in enumerate(self.stages[:-1]):
            value = counts.get(str(stage.id)) if job is not None else None
            if value is not None and self.stages[index + 1].id in promoting:
                value = 0
            out[str(stage.id)] = int(value) if isinstance(value, int) else (0 if job is not None else None)
        return out

    def deploy_events(self) -> str:
        return deploy_events(self.session, self.project)

    def may_deploy_any(self) -> bool:
        return any(access.may_deploy(self.principal, self.project.id, s) for s in self.stages[1:])

    def stage_out(self, stage: ReleaseStage, open_by_target: dict[UUID, Release]) -> dict[str, Any]:
        index = self.stages.index(stage)
        nxt = self.stages[index + 1] if index + 1 < len(self.stages) else None
        next_action: Optional[str] = None
        if nxt is not None:
            pending = open_by_target.get(nxt.id)
            if pending is None and access.may_deploy(self.principal, self.project.id, nxt):
                next_action = "deploy"
            elif (
                pending is not None and pending.state == lifecycle.AWAITING_APPROVAL
                and access.may_approve(self.principal, self.project.id, snapshot.rule(pending, nxt) or nxt, pending)
            ):
                next_action = "approve"
        current = self.current(stage) if not stages_mod.is_integration(stage) else None
        hit = service.freeze_on(self.session, self.project, stage)
        deployers = None
        if stage.deployers is not None:
            users = [user for u in access.deployer_users(stage) if (user := optional_uuid(u))]
            names = self.names(users)
            deployers = {
                "roles": access.deployer_roles(stage),
                "users": [{"id": str(u), "name": names.get(u, "")} for u in users],
            }
        return {
            "id": str(stage.id), "position": index, "name": stage.name,
            "branch": stages_mod.branch_of(self.project, stage), "url": stage.url, "url_source": stage.url_source,
            "deploy_environment": stage.deploy_environment, "approvals_required": stage.approvals_required,
            "deployers": deployers, "auto_deploy": stage.auto_deploy,
            "current": self.release_out(current) if current is not None else None,
            "next_action": next_action, "frozen_until": iso_or_none(hit.until if hit is not None else None),
            # The ticket system the step into this stage asks a key of when pressed (D8), else null.
            "asks_ticket": self.asks_ticket(stage),
            # Its Jira project key, for the ticket field's example (OAM-123), else null.
            "ticket_project": self.ticket_project(stage),
            # Freeze now: frozen with no end until a person switches it off.
            "frozen_now": bool(hit is not None and hit.freeze_now),
            # No stage is locked any more: a save applies from the next release (`app/releases/snapshot.py`).
            "locked_by": None,
        }

    def tab(self) -> dict[str, Any]:
        open_releases = self.open_releases()
        open_by_target = {r.to_stage_id: r for r in open_releases if r.to_stage_id is not None}
        pending = {k: v[:CHANGES_SHOWN] for k, v in self.waiting().items()}
        names = self.names([c.author_id for rows in pending.values() for c in rows])
        roots = self.roots([c for rows in pending.values() for c in rows])
        freezes = list(self.session.scalars(
            select(ReleaseFreeze).where(ReleaseFreeze.project_id == self.project.id).order_by(ReleaseFreeze.starts_at)
        ))
        return {
            "stages": [self.stage_out(s, open_by_target) for s in self.stages],
            "freezes": freezes_out(self.session, freezes),
            "waiting": {str(k): [change_out(c, names, roots) for c in v] for k, v in pending.items()},
            "extra_commits": self.extra_commits(),
            "open": self.releases_out(open_releases),
            "can_deploy": self.may_deploy_any(),
            "executor": executor.executor_state(self.session, self.project),
            "deploy_events": self.deploy_events(),
            "promote_pin": self.promote_pin(pending),
        }

    def promote_pin(self, pending: dict[UUID, list[Change]]) -> Optional[dict[str, Any]]:
        """What the next promote out of the integration branch ships with the merge lane on: the commit the lane
        certified furthest along the branch (`merge_lane_state.certified_pin`, the release executor's pin), keyed by
        that stage; `sha` None when nothing is certified (the release then gates the branch's head itself). None with
        the lane off or no stage after the integration branch's. `newer_change_ids`: the stage's waiting changes the
        pinned commit does not contain, so a whole promote leaves them waiting (`after_pin`). `stall`: why the pin lags the
        branch (`merge_lane_state.pin_stall`), None when nothing is behind it."""
        source = next((s for s in self.stages[:-1]
                       if s.level != stages_mod.WORK and stages_mod.branch_of(self.project, s) == self.project.integration_branch),
                      None)
        if source is None or not merge_lane_state.lane_state(self.session, self.project).active:
            return None
        pin = merge_lane_state.certified_pin(self.session, self.project)
        return {
            "stage_id": str(source.id),
            "sha": pin.sha if pin else None,
            "certified_at": pin.certified_at.isoformat() if pin else None,
            "behind": pin.behind if pin else None,
            "newer_change_ids": self.after_pin(pending.get(source.id, []), pin) if pin else [],
            "stall": merge_lane_state.pin_stall(self.session, self.project, pin),
        }

    def after_pin(self, changes: list[Change], pin: merge_lane_state.CertifiedPin) -> list[str]:
        """The ids of `changes` the pinned commit lacks. The CP asks no git, so place on the branch is time, as
        `certified_pin` counts `behind`: a lane merge after the pin was the head (`newer_shas`), or a merge the lane
        never saw that landed after it."""
        laned = set(self.session.scalars(select(MergeLaneRow.merge_sha).where(
            MergeLaneRow.project_id == self.project.id, MergeLaneRow.state == merge_lane_state.MERGED,
            MergeLaneRow.base_branch == self.project.integration_branch,
            MergeLaneRow.merge_sha.in_({c.merge_sha for c in changes}),
        ))) if changes else set()
        return [str(c.change_id) for c in changes if c.merge_sha != pin.sha and (
            c.merge_sha in pin.newer_shas
            or (c.merge_sha not in laned and c.merged_at is not None and c.merged_at > pin.head_at))]

    # ── The org overview row ─────────────────────────────────────────────────────────────────────────────

    def overview(self) -> dict[str, Any]:
        project_ref = {"id": self.project.id, "name": self.project.name}
        if len(self.stages) < 2:
            return {"project": project_ref, "production": None, "waiting_count": 0, "extra_commits": None,
                    "can_deploy": False, "from_stage_id": None}
        production = self.stages[-1]
        source = self.stages[-2]
        pending = self.waiting().get(source.id, [])
        last = self.last_release(production)
        open_ = next((r for r in reversed(self.open_releases()) if r.to_stage_id == production.id), None)
        extra = self.extra_commits().get(str(source.id))
        frozen = service.freeze_on(self.session, self.project, production) is not None
        return {
            "project": project_ref,
            "production": {
                "stage_id": str(production.id), "url": production.url,
                "last_release": self.release_out(last) if last is not None else None,
                "status": status_chip(open_, last, bool(pending) or bool(extra), frozen),
            },
            "waiting_count": len(pending),
            "extra_commits": extra,
            "can_deploy": access.may_deploy(self.principal, self.project.id, production),
            "from_stage_id": str(source.id),
        }


def deploy_events(session: Session, project: Project) -> str:
    """`easy_mode` for an Easy Mode app, `connected` when a record-only deploy trigger or a deploy workflow
    feeds the ledger, else `not_connected`."""
    hosted = session.scalar(
        select(HostedApp.id).where(HostedApp.project_id == project.id, HostedApp.deleted_at.is_(None)).limit(1)
    )
    if hosted is not None:
        return "easy_mode"
    if project.deploy_workflows:
        return "connected"
    from app.production_qa import recorder

    for trigger in session.scalars(select(TaskTrigger).where(
        TaskTrigger.project_id == project.id, TaskTrigger.source_kind.in_(_DEPLOY_SOURCE_KINDS),
    )):
        if recorder.records_only(trigger):
            return "connected"
    return "not_connected"


def status_chip(open_: Optional[Release], last: Optional[Release], waiting: bool, frozen: bool) -> Optional[str]:
    """Production's release state for the org list; the waiting count draws beside it. Null: never deployed, and
    changes wait (the count alone)."""
    if open_ is not None:
        if open_.state in (lifecycle.SCHEDULED, lifecycle.QUEUED) and frozen:
            return "frozen"
        if open_.state in (lifecycle.MERGING, lifecycle.DEPLOYING):
            return "deploying"
        return open_.state
    if last is not None and last.state == lifecycle.FAILED:
        return "failed"
    if waiting and frozen:
        return "frozen"
    if last is not None and last.state == lifecycle.LIVE:
        return "live"
    if last is not None and last.state == lifecycle.SHIPPED:
        return "deployed"
    return None if waiting else "up_to_date"


def freeze_out(freeze: ReleaseFreeze, stage_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """A freeze on the wire (`PUT …/release/freezes` reads it back): `repeat` with `weekly` kept for an old client;
    `stage_ids` its stages when `scope` is `stages`."""
    repeat = freeze.repeat if freeze.repeat not in (None, "never") else ("weekly" if freeze.weekly else "never")
    return {
        "id": str(freeze.id), "name": freeze.name, "starts_at": iso_or_none(freeze.starts_at), "ends_at": iso_or_none(freeze.ends_at),
        "weekly": repeat == "weekly", "timezone": freeze.timezone, "repeat": repeat,
        "scope": freeze.scope or "after_integration", "stage_ids": stage_ids or [],
        "let_pressed": bool(freeze.let_pressed), "let_queued": freeze.let_queued is not False,
    }


def freezes_out(session: Session, freezes: list[ReleaseFreeze]) -> list[dict[str, Any]]:
    """Every freeze on the wire, its stages read in one query."""
    ids = [f.id for f in freezes if f.scope == "stages"]
    stages: dict[Any, list[str]] = {}
    if ids:
        for freeze_id, stage_id in session.execute(select(ReleaseFreezeStage.freeze_id, ReleaseFreezeStage.stage_id)
                                                   .where(ReleaseFreezeStage.freeze_id.in_(ids))):
            stages.setdefault(freeze_id, []).append(str(stage_id))
    return [freeze_out(f, sorted(stages.get(f.id, []))) for f in freezes]
