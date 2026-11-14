"""A project's pipeline: an ordered list of stages, every one the same shape (name, branch, address). Nothing is
built in: the defaults are Ideas → Building → Dev → Production, and an owner adds, removes, renames and reorders any
of them (`app/releases/pipeline.py`).

A stage's `level` is derived from its branch on save (`level_for`), never set as an option:

- `none` — no branch: an item is here because a step moved it;
- `work` — a pattern with `*` (`fleet/task-*`): the task has started and builds on its own work branch;
- `branch` — a branch name: the change is merged into, or contained in, that branch.

The two project branch fields stay single-homed on the project: the first `branch`-level stage (the INTEGRATION
stage, today's Dev) stores `branch = NULL` and reads `projects.integration_branch`; the `work` stage stores NULL and
reads `projects.branch_prefix` + `*` (`branch_of`). So a stage is the integration stage exactly when it is
branch-level and stores no branch (`is_integration`), which the save keeps true.

Releases run between branch-level stages only: release code reads `branch_stages(...)`, whose first is the
integration stage (a release source, never a target), whose last is production (`production_stage`), and whose rest
are the only stages a release or an auto-deploy may target (`release_targets`). A stage's `auto_deploy`,
`approvals_required` and `deployers` are the settings of the step INTO it.

Absence is not a state: every project with a repo has its stages, written at create, on first repo connect, and
lazily by the first write that needs them (`materialise`, idempotent and race-safe). A read never writes (a
cookie-authenticated GET must not: SameSite=Lax rests on it, `auth.set_session_cookie`): until then it shows the
unsaved defaults (`for_read`), under the ids `materialise` will write, so a deploy naming a stage id the read showed
finds it.

Ideas and Building reach projects created before the pipeline at runtime, never in a migration (`upgrade`): the
previous release, still serving during a rolling deploy, reads position 0 as Dev. So new rows ahead of Dev are written
only once no older control-plane process is live (`old_version_gone`); until a project is upgraded, `for_read`
synthesises the two under the ids `upgrade` will write, and `pipeline_upgraded_at` records the upgrade so a stage
deleted afterwards is never re-created. Residual risk, accepted: a previous-release process still serving more than
`OLD_VERSION_WAIT` after this one started would read the upgraded rows wrongly.

Production's default branch is `main`; the release executor corrects a `master` repo on its first run.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional, Sequence, Union
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app import edition_features
from app.clock import utcnow
from app.models.projects import Project
from app.models.releases import LEVELS, ReleaseStage

log = logging.getLogger(__name__)

NONE, WORK, BRANCH = LEVELS
IDEAS = "Ideas"
BUILDING = "Building"
DEV = "Dev"
PRODUCTION = "Production"
PRODUCTION_BRANCH_DEFAULT = "main"
#: How long after this process started rows ahead of Dev are written even while an older peer still heartbeats.
OLD_VERSION_WAIT = timedelta(minutes=30)


# ── Levels and the release helpers ──────────────────────────────────────────────────────────────────

def level_for(branch: Optional[str]) -> str:
    """The level a typed branch implies: empty → `none`, a pattern with `*` → `work`, a name → `branch`."""
    text = (branch or "").strip()
    if not text:
        return NONE
    return WORK if "*" in text else BRANCH


def branch_stages(stages: Sequence[ReleaseStage]) -> list[ReleaseStage]:
    """The branch-level stages, in order: what releases run between."""
    return [s for s in stages if s.level == BRANCH]


def integration_stage(stages: Sequence[ReleaseStage]) -> Optional[ReleaseStage]:
    """The first branch-level stage: its branch is the project's integration branch (today's Dev)."""
    found = branch_stages(stages)
    return found[0] if found else None


def production_index(levels: Sequence[str]) -> Optional[int]:
    """Where production sits among stages of these levels: the last branch-level one, when at least two are; else
    None. The one home of the rule `production_stage` and the org guardrails (`app/releases/guardrails.py`) share."""
    found = [index for index, level in enumerate(levels) if level == BRANCH]
    return found[-1] if len(found) >= 2 else None


def production_stage(stages: Sequence[ReleaseStage]) -> Optional[ReleaseStage]:
    """The last branch-level stage after the integration stage (today's Production); None without one."""
    index = production_index([s.level for s in stages])
    return stages[index] if index is not None else None


def release_targets(stages: Sequence[ReleaseStage]) -> list[ReleaseStage]:
    """The branch-level stages after the integration stage: the only ones a release or an auto-deploy targets."""
    return branch_stages(stages)[1:]


def is_integration(stage: ReleaseStage) -> bool:
    """Whether `stage` is the integration stage: branch-level storing no branch (module docstring)."""
    return stage.level == BRANCH and stage.branch is None


def branch_of(project: Project, stage: ReleaseStage) -> str:
    """The branch the stage shows: none for a `none` stage, the project's prefix + `*` for the `work` stage, the
    project's integration branch for the integration stage, every other stage's its own."""
    if stage.level == NONE:
        return ""
    if stage.level == WORK:
        return f"{project.branch_prefix or ''}*"
    return stage.branch or project.integration_branch


# ── Reading and writing a project's stages ──────────────────────────────────────────────────────────

def stages_of(session: Session, project_id: int) -> list[ReleaseStage]:
    """The project's stored stages in order (none for a project without a repo)."""
    return list(session.scalars(
        select(ReleaseStage).where(ReleaseStage.project_id == project_id).order_by(ReleaseStage.position)
    ))


def live_urls(session: Session, project_ids: Sequence[int]) -> dict[int, list[tuple[str, str]]]:
    """Each project's stored stages that have an address, as `(name, url)` in pipeline order. Stored rows only: an
    unsaved default stage has no address, and this never touches `health.looked_at` (the pipeline read does). A hosted
    app's live stage shows the app's live address (`hosted_apps/service.py` `live_stage_urls`), so it follows Make main
    and a removed main domain with nothing rewritten."""
    out: dict[int, list[tuple[str, str]]] = {pid: [] for pid in project_ids}
    if not project_ids:
        return out
    rows = session.execute(
        select(ReleaseStage.project_id, ReleaseStage.name, ReleaseStage.url, ReleaseStage.url_source,
               ReleaseStage.deploy_environment)
        .where(ReleaseStage.project_id.in_(list(project_ids)), ReleaseStage.url.is_not(None), ReleaseStage.url != "")
        .order_by(ReleaseStage.project_id, ReleaseStage.position)
    ).all()
    live: dict[tuple[int, str], str] = {}
    if edition_features.enabled("easy_mode_hosting"):
        from app.hosted_apps import service as hosted

        live = hosted.live_stage_urls(session, [(pid, name, source, env) for pid, name, _, source, env in rows])
    for project_id, name, url, _, _ in rows:
        out[project_id].append((name, live.get((project_id, name), url)))
    return out


def has_repo(project: Project) -> bool:
    return bool((project.repo_url or "").strip())


#: Namespace of the default stages' ids (`_default_id`).
_DEFAULT_STAGE_NS = UUID("0b7d6c1e-5f43-4f0e-9a51-2a6f3c8e7d10")
#: The `_default_id` keys: Dev and Production keep the positions they were first written at, so their ids never
#: changed; Ideas and Building are named.
_IDEAS_KEY, _BUILDING_KEY, _DEV_KEY, _PRODUCTION_KEY = "ideas", "building", 0, 1


def _default_id(project: Project, key: Union[int, str]) -> UUID:
    """A UUIDv7 (the entity shape's id) that is a function of the project and the key: its 48-bit timestamp the
    project's `ingested_at` (its insert stamp), its random bits a uuid5 of "<project id>:<key>"."""
    millis = int(project.ingested_at.timestamp() * 1000) & ((1 << 48) - 1)
    value = (millis << 80) | (uuid5(_DEFAULT_STAGE_NS, f"{project.id}:{key}").int & ((1 << 80) - 1))
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version 7
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # RFC 9562 variant
    return UUID(int=value)


def _row(project: Project, key: Union[int, str], position: int, name: str, level: str, branch: Optional[str],
         *, auto: bool) -> ReleaseStage:
    """One unsaved default row, every column set (no server default has run)."""
    return ReleaseStage(
        id=_default_id(project, key), org_id=project.org_id, project_id=project.id, position=position, name=name,
        description="", branch=branch, level=level, deploy_environment=None, url=None, url_source=None,
        auto_deploy=auto, approvals_required=0, deployers=None,
    )


def _ahead_of_dev(project: Project, names: Sequence[str] = ()) -> list[ReleaseStage]:
    """Ideas and Building as unsaved rows at positions 0 and 1: Start by hand, no approvals, the default who. A
    name a stored stage already holds gets a number (names are unique per project)."""
    taken = {n.strip().lower() for n in names}

    def free(name: str) -> str:
        candidate, n = name, 2
        while candidate.lower() in taken:
            candidate, n = f"{name} {n}", n + 1
        taken.add(candidate.lower())
        return candidate

    return [
        _row(project, _IDEAS_KEY, 0, free(IDEAS), NONE, None, auto=False),
        _row(project, _BUILDING_KEY, 1, free(BUILDING), WORK, None, auto=False),
    ]


def _branch_defaults(project: Project, first: int, *, merge_by_itself: bool) -> list[ReleaseStage]:
    """Dev and Production as unsaved rows from position `first`. `merge_by_itself`: Dev's columns hold the Merge
    step as today (by itself); False only for the two-row layout a previous release still reads."""
    return [
        _row(project, _DEV_KEY, first, DEV, BRANCH, None, auto=merge_by_itself),
        _row(project, _PRODUCTION_KEY, first + 1, PRODUCTION, BRANCH, PRODUCTION_BRANCH_DEFAULT, auto=False),
    ]


def _defaults(project: Project) -> list[ReleaseStage]:
    """The four default stages as unsaved rows, ids derived from the project so a read's and the write's are the
    same."""
    assert project.id is not None and project.ingested_at is not None, "flush the project before its stages"
    return [*_ahead_of_dev(project), *_branch_defaults(project, 2, merge_by_itself=True)]


def for_read(session: Session, project: Project) -> list[ReleaseStage]:
    """The project's stages without writing: its stored ones (with Ideas and Building synthesised ahead of them until
    the project is upgraded), else the unsaved defaults `materialise` would write."""
    return composed(project, stages_of(session, project.id))


def composed(project: Project, stages: list[ReleaseStage]) -> list[ReleaseStage]:
    """`for_read` on stages already read: the stored ones, Ideas and Building synthesised ahead of them until the
    project is upgraded, else the unsaved defaults. Never reads or writes."""
    if not has_repo(project):
        return stages
    if not stages:
        return _defaults(project)
    if project.pipeline_upgraded_at is None and all(s.level == BRANCH for s in stages):
        return [*_ahead_of_dev(project, [s.name for s in stages]), *stages]
    return stages


def materialise(session: Session, project: Project) -> list[ReleaseStage]:
    """The project's stored stages, writing the defaults first when it has a repo and none yet: all four once no
    older process is live (`old_version_gone`), else Dev and Production only (`upgrade` adds the rest later).
    Flushes; never commits. A concurrent writer's rows win (the savepoint's unique key refuses ours)."""
    stages = stages_of(session, project.id)
    if stages or not has_repo(project):
        return stages
    full = old_version_gone(session)
    rows = _defaults(project) if full else _branch_defaults(project, 0, merge_by_itself=False)
    try:
        with session.begin_nested():
            session.add_all(rows)
            session.flush()
    except IntegrityError:
        return stages_of(session, project.id)
    if full and project.pipeline_upgraded_at is None:
        project.pipeline_upgraded_at = utcnow()
        session.flush()
    written = stages_of(session, project.id)
    # Their gates and targets too, from the same defaults every reader shows (`app/pipeline_gates/defaults.py`).
    from app.pipeline_gates import defaults as gate_defaults  # noqa: PLC0415 — it imports this module

    gate_defaults.ensure(session, project, written, written)
    return written


def production(project: Project, session: Optional[Session] = None) -> Optional[ReleaseStage]:
    """The project's production stage (`production_stage`), its stages materialised when missing; None for a
    project without a repo."""
    session = session or object_session(project)
    if session is None:
        return None
    return production_stage(materialise(session, project))


def production_environment(project: Project, session: Optional[Session] = None) -> Optional[str]:
    """The forge environment the ledger counts as production: Production's `deploy_environment`, the single home."""
    stage = production(project, session)
    return (stage.deploy_environment or None) if stage is not None else None


# ── The runtime upgrade ─────────────────────────────────────────────────────────────────────────────

def old_version_gone(session: Session) -> bool:
    """Whether rows ahead of Dev may be written: no live control-plane peer started before this process (the
    previous release's, during a rolling deploy), or `OLD_VERSION_WAIT` has passed since this one started. There is
    no version registry: a peer's start time is the proxy (module docstring)."""
    from app import cp_peers
    from app.models.control_plane import CpPeer
    from app.version import PROCESS_STARTED_AT

    if utcnow() - PROCESS_STARTED_AT >= OLD_VERSION_WAIT:
        return True
    older = session.scalar(select(CpPeer.id).where(
        CpPeer.id != cp_peers.self_id(), CpPeer.started_at < PROCESS_STARTED_AT,
        CpPeer.heartbeat_at >= func.now() - timedelta(seconds=cp_peers.PEER_STALE_SECONDS),
    ).limit(1))
    return older is None


def shift(session: Session, stages: Sequence[ReleaseStage], positions: Sequence[int]) -> None:
    """Move each stage to its new position in two phases, so `uq_release_stages_project_position` never sees two
    rows on one position mid-write. Flushes."""
    if not stages:
        return
    top = max(max(s.position for s in stages), max(positions, default=0)) + 1
    for offset, stage in enumerate(stages):
        stage.position = top + offset
    session.flush()
    for stage, position in zip(stages, positions, strict=True):
        stage.position = position
    session.flush()


def upgrade(session: Session, project: Project) -> bool:
    """Write Ideas and Building ahead of a project's stored stages, once (`pipeline_upgraded_at`), and set the
    integration stage's columns to the Merge step as it has always run: by itself, no approvals, the default who (a
    value stored there before never had an effect; a reset one is logged). Whether it upgraded. Locks the project
    row, so a concurrent save or tick waits. Flushes; never commits."""
    locked = session.scalar(select(Project).where(Project.id == project.id).with_for_update()
                            .execution_options(populate_existing=True))
    if locked is None or locked.pipeline_upgraded_at is not None:
        return False
    stages = stages_of(session, project.id)
    if not stages:
        return False  # `materialise` writes all four.
    if all(s.level == BRANCH for s in stages):
        shift(session, stages, [s.position + 2 for s in stages])
        session.add_all(_ahead_of_dev(project, [s.name for s in stages]))
        dev = integration_stage(stages)
        if dev is not None:
            if dev.approvals_required or dev.deployers is not None:
                log.info("pipeline upgrade: project %s reset %s's approvals %s and who %s to the Merge defaults",
                         project.id, dev.name, dev.approvals_required, dev.deployers)
            dev.auto_deploy, dev.approvals_required, dev.deployers = True, 0, None
    locked.pipeline_upgraded_at = utcnow()
    session.flush()
    from app.pipeline_gates import defaults as gate_defaults  # noqa: PLC0415 — it imports this module

    upgraded = stages_of(session, project.id)
    gate_defaults.ensure(session, locked, upgraded, upgraded)
    return True


#: Projects one release-tick pass upgrades at most.
UPGRADE_BATCH = 100


def upgrade_pending() -> list[int]:
    """The release tick's first step: upgrade up to `UPGRADE_BATCH` projects not yet upgraded, each in its own
    org-bound session and commit, once `old_version_gone`. The ids upgraded."""
    from app.db import BridgedSession
    from app.models.releases import ReleaseStage as Stage
    from app.tenancy import seam
    from app.trigger_intake import fleet_session

    with fleet_session() as session:
        if not old_version_gone(session):
            return []
        ids = list(session.scalars(
            select(Project.id).where(
                Project.pipeline_upgraded_at.is_(None),
                select(Stage.id).where(Stage.project_id == Project.id).exists(),
            ).order_by(func.random()).limit(UPGRADE_BATCH)  # a project failing every pass blocks none behind it
        ))
    done: list[int] = []
    for project_id in ids:
        try:
            with BridgedSession() as session, seam.detached(), seam.bind_from(session, Project, project_id):
                project = session.scalar(select(Project).where(Project.id == project_id))
                if project is not None and upgrade(session, project):
                    done.append(project_id)
                session.commit()
        except seam.TenancyRowMissing:
            continue
        except Exception:  # noqa: BLE001 — one project's fault must not stop the others
            log.exception("pipeline upgrade: project %s failed", project_id)
    return done
