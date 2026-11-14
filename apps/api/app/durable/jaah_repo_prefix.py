"""The org prefix of Jaah-made repos, kept true as durable work: an org's repos renamed on GitHub to `<slug>-<name>`.

Every repo Jaah makes for an org is named with the org's prefix (`app/jaah_repos.py::repo_prefix`), and a git login
for one without it is refused, so the name must follow the slug. Two callers:

- `enqueue_rename` — an operator's slug rename (`app/routes/orgs.py::update_org`), after its commit: each of the
  org's served repos (`jaah_repos.SERVED_STATES`) still lacking the org's CURRENT prefix is renamed, a leading old
  prefix (`old_slugs`) dropped first. The enqueue follows the commit, so a crash between the two loses it; the next
  api start's sweep catches it up.
- `request_startup_sweep` — once per api start: (i) every tenant org whose slug predates the letters-and-digits
  grammar (`app/org_slug.py::ORG_SLUG_RE`) gets a conforming one (`conforming`, or digits appended when that is
  taken, empty, or still a tag of tailnet devices), audited as `org_slug_renamed` and its devices retagged
  (`app/durable/org_retag.py`) as an operator's rename would; then (ii) every org with an unprefixed served repo has
  it renamed, every slug the org ever had (its `org_slug_renamed` audit rows) taken as an old prefix to drop.
  A no-op once all is conforming; concurrent starts collapse into one sweep (`SWEEP_DEDUP_ID`).

Each repo is renamed and its row, web app, project repo URL and setup answer follow it (`jaah_repos.follow_rename`)
in one commit per repo, under the row's lock, so a retried attempt skips what it already did. The repo's current
name is read by its immutable id first: a rename GitHub took whose commit was lost is only recorded. The admin
credential is the Easy Mode GitHub one (`hosted_apps.config.github_setup`); none set up → nothing to rename.
A target name already taken on GitHub (or by another row) moves on to `-2` … `-9`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, NamedTuple, Optional
from uuid import UUID, uuid4

from dbos import DBOS
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.durable import runtime
from app.durable.runtime import enqueue_or_warn

log = logging.getLogger("jaah-repo-prefix")

QUEUE = runtime.declare_queue("jaah-repo-prefix", concurrency=2, polling_interval_sec=runtime.BACKGROUND_POLL_SEC)
#: One startup sweep in flight at a time, however many replicas start together.
SWEEP_DEDUP_ID = "jaah-repo-prefix-sweep"
#: The names a renamed repo tries: `<prefix><name>`, then `-2` … `-9` after it.
NAME_ATTEMPTS = 9
#: Attempts at a free conforming slug before an org is left for the next start.
SLUG_ATTEMPTS = 5


def rename_workflow_id(org_id: UUID, change_id: str) -> str:
    """The workflow ID of one rename's repo pass — keyed per change. Pure."""
    return f"jaah-repo-prefix:{org_id}:{change_id}"


@contextmanager
def _session() -> Iterator[Session]:
    """A session on the operator plane, detached from any caller's org: every pass here is keyed by the org id it
    was enqueued for, or is the startup sweep's fleet-wide census. Its own, not `vnodes.fleet_session`: this
    module ships in every edition (the community edition too), and the slug grammar is every edition's."""
    from app import db_async
    from app.tenancy import exclusion_log, seam

    db_async.hand_back()  # Under the caller's own scope, before the operator plane's.
    with exclusion_log.caller("jaah_repo_prefix:_session"), seam.detached(), seam.operator_scope():
        with db_async.side_session() as session:
            yield session


def _admin_token() -> Optional[str]:
    """The Easy Mode GitHub admin token, or None (logged) when Easy Mode's GitHub half is not set up — or the
    edition has no Easy Mode at all (no repo of Jaah's own to rename)."""
    from app import edition_features

    if not edition_features.enabled("easy_mode_hosting"):
        return None
    from app.hosted_apps import config

    try:
        return config.github_token(config.github_setup())
    except config.NotSetUp as exc:
        log.info("jaah-repo-prefix: no GitHub admin credential, nothing renamed (%s)", exc)
        return None


# ── The repo pass ────────────────────────────────────────────────────────────────────────────────────────────────


def _unprefixed(session, org_id: UUID, old_slugs: list[str]) -> list[int]:
    """The ids of the org's served rows whose name lacks the org's current prefix."""
    from app import jaah_repos
    from app.models.jaah_repos import JaahRepo

    slug = jaah_repos.org_slug_of(session, org_id)
    if not slug:
        return []
    rows = session.execute(select(JaahRepo.id, JaahRepo.name).where(
        JaahRepo.org_id == org_id, JaahRepo.state.in_(jaah_repos.SERVED_STATES),
    )).all()
    return [row_id for row_id, name in rows if jaah_repos.prefixed_rename(name, slug, old_slugs) is not None]


def _current_name(token: str, row) -> Optional[str]:
    """The repo's `owner/repo` on GitHub now: by its immutable id when recorded, else by the row's name."""
    from app import edition_features

    if not edition_features.enabled("easy_mode_hosting"):
        return None
    from app.hosted_apps import github

    found = github.repo_by_id(token, row.repo_id) if row.repo_id is not None else github.get_repo(token, row.name)
    return str(found["full_name"]) if found else None


def _rename_on_github(session, token: str, current: str, target: str) -> str:
    """Rename `current` to `target`'s repo name, or the first of its `-2` … `-9` free on GitHub and among the rows;
    the new `owner/repo`. `GitHubError` when every one is taken."""
    from app import edition_features, jaah_repos

    if not edition_features.enabled("easy_mode_hosting"):
        raise RuntimeError("renaming a Jaah-made repo needs Easy Mode hosting")
    from app.hosted_apps import github

    owner = current.partition("/")[0]
    base = target.partition("/")[2]
    if owner.lower() != jaah_repos.github_org().lower():
        # Moved out of (or mid-move off) the Jaah org: not Jaah's to rename.
        raise github.GitHubError(f"{current} is no longer in the GitHub organization {jaah_repos.github_org()}.")
    for n in range(1, NAME_ATTEMPTS + 1):
        name = base if n == 1 else f"{base}-{n}"
        if jaah_repos.name_taken(session, f"{owner}/{name}"):
            continue
        try:
            return str(github.rename_repo(token, current, name)["full_name"])
        except github.GitHubNameTaken:
            continue
    raise github.GitHubError(f"Every name for {current} under {base} is taken in the GitHub organization.")


def _rename_one(token: str, row_id: int, old_slugs: list[str]) -> bool:
    """Rename one row's repo and commit everything that follows it; whether it was renamed. Re-read under the row's
    lock, so a concurrent pass, or a retry of this one, skips a row already renamed."""
    from app import jaah_repos
    from app.models.jaah_repos import JaahRepo

    with _session() as session:
        row = session.scalar(select(JaahRepo).where(JaahRepo.id == row_id).with_for_update()
                             .execution_options(populate_existing=True))
        slug = jaah_repos.org_slug_of(session, row.org_id) if row is not None else ""
        target = jaah_repos.prefixed_rename(row.name, slug, old_slugs) if row is not None and slug else None
        if row is None or row.state not in jaah_repos.SERVED_STATES or target is None:
            session.rollback()
            return False
        current = _current_name(token, row)
        if current is None:
            session.rollback()
            log.warning("jaah-repo-prefix: %s (row %s) is not on GitHub; left as it is", row.name, row_id)
            return False
        if jaah_repos.prefixed_rename(current, slug, old_slugs) is None:
            new_full_name = current  # renamed by an attempt whose commit was lost
        else:
            new_full_name = _rename_on_github(session, token, current, target)
        old = row.name
        jaah_repos.follow_rename(session, row, new_full_name)
        session.commit()
    log.info("jaah-repo-prefix: %s -> %s", old, new_full_name)
    return True


def rename_org_repos_now(org_id: str, old_slugs: list[str]) -> int:
    """Rename each of the org's served repos lacking its current prefix; the number renamed. A repo GitHub refuses
    is logged and left for the next pass; an unreachable GitHub raises (the step retries)."""
    from app import edition_features

    if not edition_features.enabled("easy_mode_hosting"):
        return 0
    from app.hosted_apps import github

    with _session() as session:
        # Every slug the org ever had, not only the one this rename left: a pass an earlier rename never finished
        # still drops its prefix (`acme-shop` under `globex` becomes `globex-shop`, never `globex-acme-shop`).
        old_slugs = sorted(set(old_slugs) | set(_past_slugs(session, UUID(org_id))), key=len, reverse=True)
        pending = _unprefixed(session, UUID(org_id), old_slugs)
        session.rollback()
    if not pending:
        return 0
    token = _admin_token()
    if token is None:
        return 0
    renamed = 0
    for row_id in pending:
        try:
            renamed += _rename_one(token, row_id, old_slugs)
        except github.GitHubError as exc:
            log.warning("jaah-repo-prefix: org %s: row %s not renamed (%s); the next api start retries", org_id,
                        row_id, exc)
    return renamed


# About four hours of doubling from 30s: GitHub being unreachable is the only failure that raises.
@DBOS.step(retries_allowed=True, max_attempts=10, interval_seconds=30.0, backoff_rate=2.0)
def rename_repos(org_id: str, old_slugs: list[str]) -> int:
    """One attempt; an unreachable GitHub raises and is retried. The number of repos renamed."""
    return rename_org_repos_now(org_id, old_slugs)


@DBOS.workflow()
def rename_org_repos(org_id: str, old_slugs: list[str], change_id: str) -> None:
    """Imported by `app/durable/runtime.py` for its decorator side effect."""
    renamed = rename_repos(org_id, old_slugs)
    log.info("jaah-repo-prefix: org %s: %d repo(s) renamed", org_id, renamed)


def enqueue_rename(org_id: UUID, old_slugs: list[str]) -> bool:
    """Enqueue the repo pass of one committed slug rename. Never raises into the route."""
    change_id = uuid4().hex
    return enqueue_or_warn(
        QUEUE, rename_workflow_id(org_id, change_id), rename_org_repos, str(org_id), list(old_slugs), change_id,
        log=log, failure=f"jaah-repo-prefix: could not enqueue the repo rename of org {org_id}",
        consequence="its repos keep their old names until the next api start's sweep",
    )


# ── The startup sweep ────────────────────────────────────────────────────────────────────────────────────────────


def _past_slugs(session, org_id: UUID) -> list[str]:
    """Every slug the org was renamed from, by its `org_slug_renamed` audit rows."""
    from app.models.audit import ActivityEvent

    found = session.scalars(select(ActivityEvent.data["from"].astext).where(
        ActivityEvent.kind == "org_slug_renamed", ActivityEvent.data["org_id"].astext == str(org_id),
    ))
    return sorted({slug for slug in found if slug}, key=len, reverse=True)


def _candidate(session, old: str, first: bool) -> str:
    """`conforming(old)` on a first try while it is free; else it with two random digits (`unique_slug`)."""
    from app import org_slug
    from app.entity_shape import slug_taken
    from app.models.orgs import Org

    base = org_slug.conforming(old)
    if first and base and not slug_taken(session, Org, base):
        return base
    return org_slug.unique_slug(session, (base or "org")[: org_slug.ORG_SLUG_MAX - 3])


class Conformed(NamedTuple):
    """One org given a conforming slug: what its tailnet retag (`org_retag.enqueue_retag`) takes."""

    org_id: str
    old: str
    new: str
    device_ids: list[str]


def conform_slug(org_id: UUID, old: str) -> Optional[Conformed]:
    """Give the org a slug under `ORG_SLUG_RE` in place of `old`, as an operator's rename would
    (`routes/orgs.py::_rename_slug`): audited and its console row refreshed; its tailnet devices (read before the
    commit) travel in the answer for the workflow to retag — a step cannot start a workflow. None when the org
    changed meanwhile or no free slug was found (the next start tries again)."""
    from app import edition_features
    from app.entity_shape import SYSTEM_ACTOR_ID, slug_taken
    from app.events import emit_console_event
    from app.models.orgs import Org
    from app.user_activity import record_write

    for attempt in range(SLUG_ATTEMPTS):
        with _session() as session:
            new = _candidate(session, old, attempt == 0)
            session.rollback()
        # Read before the old slug is free, as a rename does; a slug devices still carry is never given.
        on_old: list[str] = []
        if edition_features.enabled("tailnet"):
            from app.durable import org_retag

            devices = org_retag.tailnet_devices(old, new)
            if devices.on_new:
                continue
            on_old = list(devices.on_old)
        with _session() as session:
            row = session.get(Org, org_id, with_for_update=True, populate_existing=True)
            if row is None or row.slug != old:
                session.rollback()
                return None
            if slug_taken(session, Org, new):
                session.rollback()
                continue
            row.slug = new
            row.updated_by_id = SYSTEM_ACTOR_ID
            record_write(
                session, None, "org_slug_renamed",
                f"Renamed the slug of org {row.name} from {old} to {new}: an org slug is letters and digits only",
                data={"org_id": str(row.id), "from": old, "to": new, "devices": len(on_old)}, org_id=row.id,
            )
            session.flush()
            emit_console_event(session, entity="org", id=row.id, action="upserted", commit=True)
        log.info("jaah-repo-prefix: org %s slug %s -> %s", org_id, old, new)
        return Conformed(str(org_id), old, new, on_old)
    log.warning("jaah-repo-prefix: org %s keeps slug %s: no free conforming slug found", org_id, old)
    return None


def stale_slugs_now() -> list[tuple[str, str]]:
    """Every tenant org whose slug is not under `ORG_SLUG_RE`, as `(org id, slug)`."""
    from app import org_slug
    from app.models.orgs import Org
    from app.tenancy.ids import is_tenant_org

    with _session() as session:
        stale = [(str(org_id), slug) for org_id, slug in session.execute(select(Org.id, Org.slug))
                 if is_tenant_org(org_id) and not org_slug.is_org_slug(slug)]
        session.rollback()
    return stale


def rename_all_now() -> int:
    """The repo pass for every org with an unprefixed served repo, each org's past slugs as old prefixes; the
    number of repos renamed."""
    from app import jaah_repos
    from app.models.jaah_repos import JaahRepo

    with _session() as session:
        org_ids = list(session.scalars(select(JaahRepo.org_id).where(
            JaahRepo.state.in_(jaah_repos.SERVED_STATES)).distinct()))
        owed = {org_id: _past_slugs(session, org_id) for org_id in org_ids}
        owed = {org_id: old for org_id, old in owed.items() if _unprefixed(session, org_id, old)}
        session.rollback()
    return sum(rename_org_repos_now(str(org_id), old) for org_id, old in owed.items())


@DBOS.step()
def stale_slugs() -> list[tuple[str, str]]:
    return stale_slugs_now()


@DBOS.step(retries_allowed=True, max_attempts=10, interval_seconds=30.0, backoff_rate=2.0)
def conform(org_id: str, slug: str) -> Optional[Conformed]:
    """One org's slug conformed; an unreadable tailnet raises and is retried. One step per org, so the workflow
    retags each org it conformed even when a later org's step fails."""
    return conform_slug(UUID(org_id), slug)


@DBOS.step(retries_allowed=True, max_attempts=10, interval_seconds=30.0, backoff_rate=2.0)
def rename_all() -> int:
    """One attempt at every org's unprefixed repos; an unreachable GitHub raises and is retried."""
    return rename_all_now()


@DBOS.workflow()
def sweep(change_id: str) -> None:
    """Imported by `app/durable/runtime.py` for its decorator side effect. Slugs first: the repo pass names repos
    after the slug each org has once it is done. `change_id` keys the run only."""
    from app import edition_features

    conformed = []
    for org_id, slug in stale_slugs():
        result = conform(org_id, slug)
        if result is None:
            continue
        done = Conformed(*result)
        conformed.append(done)
        # The community edition has no tailnet to retag.
        if edition_features.enabled("tailnet"):
            from app.durable import org_retag

            org_retag.enqueue_retag(UUID(done.org_id), done.old, done.new, done.device_ids)
    repos = rename_all()
    if conformed or repos:
        log.info("jaah-repo-prefix: sweep: %d slug(s) conformed, %d repo(s) renamed", len(conformed), repos)


def request_startup_sweep() -> bool:
    """Ask for one sweep from the api's startup; True when this call placed it. Never raises: a sweep already
    queued or running (another replica's start) refuses it, and any other failure waits for the next start."""
    from dbos._error import DBOSQueueDeduplicatedError

    change_id = uuid4().hex
    try:
        runtime.enqueue(QUEUE, f"jaah-repo-prefix-sweep:{change_id}", sweep, change_id,
                        enqueue_options={"deduplication_id": SWEEP_DEDUP_ID})
    except DBOSQueueDeduplicatedError:
        return False
    except Exception as exc:  # noqa: BLE001 — the api's start never fails on it
        log.warning("jaah-repo-prefix: could not enqueue the startup sweep (%s: %s) — stale org slugs and "
                    "unprefixed repos wait for the next start", type(exc).__name__, exc)
        return False
    return True
