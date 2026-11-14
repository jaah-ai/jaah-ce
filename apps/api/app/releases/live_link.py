"""The task page's "Live" link (PRD R6.2, R9): the address a task's change is served at, and how far it got.

EVERY PROJECT WITH RELEASE STAGES (`app/releases/stages.py`): a task whose change merged (`task_outcomes.merged_at`)
carries the furthest of —
- `live` — a `live` release to Production carries its merge commit (the ledger saw the deploy);
- `deployed` — a `shipped` release to Production carries it, or a survey found it on Production's branch;
- `deploying` — an open release to Production carrying it is `merging` or `deploying`;
- `on_dev` — otherwise, while Dev has an address —
each at its stage's address (`release_stages.url`); absent when that stage has none. Release rows only: no forge read.

AN EASY MODE PROJECT (a hosted app, cloud only) keeps its own reading, at the app's live address (`names.live_address`:
its main custom domain when it has one): an epic carries it while the app serves (`live`) or is set up
(`provisioning`); a merged task is `live` once the merge commit is in the app's last successful
deploy, `deploying` while not yet. GitHub is read at most once per app per `DEPLOY_TTL_S` (the last good deploy run)
and once per deploy run (that run's history), in process: a board of many rows costs no GitHub call per row, and
nothing is stored.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features
from app.models.hosted_apps import HostedApp
from app.models.outcomes import TaskOutcome
from app.models.releases import Release, ReleaseChange, ReleaseContained
from app.models.tasks import Task
from app.releases import lifecycle, stages as stages_mod

log = logging.getLogger(__name__)

LIVE = "live"
DEPLOYED = "deployed"
DEPLOYING = "deploying"
ON_DEV = "on_dev"
PROVISIONING = "provisioning"
#: App states whose address answers with the app (a paused one answers with its paused page).
_SERVING = ("live", "paused")
_EPIC = "epic"

#: How long the last good deploy of an app is trusted before GitHub is asked again.
DEPLOY_TTL_S = 60.0
#: app id → (expires at, the last good deploy `{id, sha, at}` or None when none yet, or `_UNREADABLE`).
_deploys: dict[UUID, tuple[float, Any]] = {}
#: app id → (deploy run id, the commits that run deployed, whether they are its whole history, or None when the read
#: failed; expires at): one entry per app, replaced when a newer run deploys. A failed read is retried after the TTL.
_histories: dict[UUID, tuple[int, Optional[tuple[frozenset[str], bool]], float]] = {}
_UNREADABLE = object()


@dataclass(frozen=True)
class LiveLink:
    url: str
    state: str


def clear_cache() -> None:
    _deploys.clear()
    _histories.clear()


def _good_deploy(app: HostedApp) -> Any:
    if not edition_features.enabled("easy_mode_hosting"):
        return _UNREADABLE
    from app.hosted_apps import config as hosting_setup, github

    now = time.monotonic()
    cached = _deploys.get(app.id)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        run: Any = github.last_good_deploy(hosting_setup.github_token(hosting_setup.setup()), app.repo_full_name)
    except Exception as exc:  # A decoration: no failure of it may fail the task read.
        log.info("hosted app %s: last good deploy not read: %s", app.id, exc)
        run = _UNREADABLE
    _deploys[app.id] = (now + DEPLOY_TTL_S, run)
    return run


def _deployed(app: HostedApp, run: dict[str, Any]) -> Optional[tuple[frozenset[str], bool]]:
    """The commits run `run` deployed (one page back from its head) and whether that page is the whole history; None
    when GitHub could not be read (asked again after `DEPLOY_TTL_S`)."""
    if not edition_features.enabled("easy_mode_hosting"):
        return None
    from app.hosted_apps import config as hosting_setup, github

    now = time.monotonic()
    cached = _histories.get(app.id)
    if cached is not None and cached[0] == run["id"] and (cached[1] is not None or cached[2] > now):
        return cached[1]
    found: Optional[tuple[frozenset[str], bool]]
    try:
        shas = github.history(hosting_setup.github_token(hosting_setup.setup()), app.repo_full_name, run["sha"])
        found = (frozenset(shas), len(shas) < github.HISTORY_PAGE)
    except Exception as exc:  # A decoration: no failure of it may fail the task read.
        log.info("hosted app %s: deploy %s history not read: %s", app.id, run["id"], exc)
        found = None
    _histories[app.id] = (run["id"], found, now + DEPLOY_TTL_S)
    return found


def _merged_state(app: HostedApp, sha: Optional[str], merged_at: datetime) -> Optional[str]:
    run = _good_deploy(app)
    if run is _UNREADABLE:
        return None
    if run is None:
        return DEPLOYING
    deployed = _deployed(app, run)
    if deployed is None:
        return None
    shas, whole = deployed
    if sha and sha in shas:
        return LIVE
    # No merge commit on record, or one older than the page read: a merge before the run took `main` is in it. A
    # heuristic: `merged_at` is when the control plane recorded the merge, so a lagging record can read `deploying`.
    if (not sha or not whole) and run["at"] is not None and merged_at <= run["at"]:
        return LIVE
    return DEPLOYING


def _merges(session: Session, task_ids: list[int]) -> dict[int, tuple[Optional[str], datetime]]:
    """Each task's newest merged dispatch: its merge commit (when recorded) and when it merged."""
    from app.outcome_ledger.writer import merge_sha_of

    if not task_ids:
        return {}
    rows = session.scalars(
        select(TaskOutcome)
        .where(TaskOutcome.task_id.in_(task_ids), TaskOutcome.merged_at.is_not(None))
        .order_by(TaskOutcome.task_id, TaskOutcome.dispatch_seq.desc())
    ).all()
    out: dict[int, tuple[Optional[str], datetime]] = {}
    for row in rows:
        if row.task_id is not None and row.task_id not in out and row.merged_at is not None:
            out[row.task_id] = (merge_sha_of(session, row), row.merged_at)
    return out


def live_links(session: Session, tasks: Iterable[Task]) -> dict[int, LiveLink]:
    """The live link of each task that carries one, by task id."""
    tasks = list(tasks)
    project_ids = {t.project_id for t in tasks}
    if not project_ids:
        return {}
    by_project: dict[int, HostedApp] = {}
    if edition_features.enabled("easy_mode_hosting"):
        apps = session.scalars(
            select(HostedApp).where(HostedApp.project_id.in_(project_ids), HostedApp.deleted_at.is_(None))
        ).all()
        by_project = {a.project_id: a for a in apps}
    hosted = [t for t in tasks if (a := by_project.get(t.project_id)) is not None and a.org_id == t.org_id]
    out = _hosted_links(session, hosted, by_project)
    staged = [t for t in tasks if t.project_id not in by_project and t.persona != _EPIC]
    out.update(_release_links(session, staged))
    return out


def _release_links(session: Session, tasks: list[Task]) -> dict[int, LiveLink]:
    """The release reading (module docstring) for tasks of projects with stages."""
    merges = _merges(session, [t.id for t in tasks])
    tasks = [t for t in tasks if merges.get(t.id) and merges[t.id][0]]
    if not tasks:
        return {}
    out: dict[int, LiveLink] = {}
    for project_id in {t.project_id for t in tasks}:
        stages = stages_mod.branch_stages(stages_mod.stages_of(session, project_id))
        if len(stages) < 2:
            continue
        dev, production = stages[0], stages[-1]
        mine = [t for t in tasks if t.project_id == project_id]
        shas = {merges[t.id][0] for t in mine}
        carried = session.execute(
            select(ReleaseChange.merge_sha, Release.state)
            .join(Release, Release.id == ReleaseChange.release_id)
            .where(Release.project_id == project_id, Release.to_stage_id == production.id,
                   Release.state.in_((lifecycle.LIVE, lifecycle.SHIPPED, lifecycle.MERGING, lifecycle.DEPLOYING)),
                   ReleaseChange.merge_sha.in_(shas))
        ).all()
        contained = set(session.scalars(select(ReleaseContained.merge_sha).where(
            ReleaseContained.project_id == project_id, ReleaseContained.stage_id == production.id,
            ReleaseContained.merge_sha.in_(shas))))
        states: dict[str, set[str]] = {}
        for sha, state in carried:
            states.setdefault(sha, set()).add(state)
        for task in mine:
            sha = merges[task.id][0] or ""
            held = states.get(sha, set())
            if lifecycle.LIVE in held:
                state, url = LIVE, production.url
            elif lifecycle.SHIPPED in held or sha in contained:
                state, url = DEPLOYED, production.url
            elif held & {lifecycle.MERGING, lifecycle.DEPLOYING}:
                state, url = DEPLOYING, production.url or dev.url
            else:
                state, url = ON_DEV, dev.url
            if url:
                out[task.id] = LiveLink(url=url, state=state)
    return out


def _hosted_links(session: Session, hosted: list[Task], by_project: dict[int, HostedApp]) -> dict[int, LiveLink]:
    """The Easy Mode reading (module docstring)."""
    if not edition_features.enabled("easy_mode_hosting"):
        return {}
    if not hosted:
        return {}
    from app.hosted_apps import config as hosting_setup, domains, service
    from app.hosted_apps.names import live_address
    merges = _merges(session, [t.id for t in hosted if t.persona != _EPIC])
    mains = domains.main_names_by_project(session, sorted({t.project_id for t in hosted}))
    urls: dict[int, Optional[str]] = {}
    out: dict[int, LiveLink] = {}
    for task in hosted:
        app = by_project[task.project_id]
        if task.project_id not in urls:
            try:
                names = service.names_of(app)
            except hosting_setup.NotSetUp:
                names = None
            urls[task.project_id] = live_address(names, mains.get(task.project_id)) if names is not None else None
        url = urls[task.project_id]
        if url is None:
            continue
        if task.persona == _EPIC:
            state = PROVISIONING if app.state == "provisioning" else LIVE if app.state in _SERVING else None
        else:
            merge = merges.get(task.id)
            state = _merged_state(app, *merge) if merge is not None and app.state in _SERVING else None
        if state is not None:
            out[task.id] = LiveLink(url=url, state=state)
    return out
