"""Every pull request a task opened, in any of its project's repositories: the single
home of `task_prs`' rules and of both its writers.

- **Observed** (`observe`): the node daemon's `task.pr_observed`, posted by `jaah-forge pr create`/`pr view` once
  the forge answered. The repository is the one the caller's checkout belongs to — the daemon judges it from the
  checkout's place in the runner's own slot, never from an argument — and the PR URL must name a pull request
  under that repository, so a row can only ever record a PR of the task's own project.
- **Attested** (`attest` → `record_attested`): the `merged_pr` settle. Its PRs are `{pr_url} ∪ pr_urls`, each of
  which must be under one of the project's repositories (else `unknown_pr_repo`). It is refused (`unmerged_prs`)
  while a recorded PR is still open and unattested, or while a repository the task opened a PR in has none merged
  — a PR closed and replaced by another in the same repository is fine, a repository whose only PR was closed
  unmerged is not (AS5/D10). On success every attested PR is recorded `merged`.

A PR is identified by (repository, number), never by its URL's spelling, so `www.github.com` and `github.com`
name one PR and `acme/api#12` and `acme/web#12` are two. No forge is ever asked anything: the observed
state is what `jaah-forge` read, and `merged` at the settle is the runner's own attestation.

`task_pr_stats`/`task_pr_changes` keep their keys and record ONE PR per task — the primary repository's attested PR,
else the runner's `pr_url` (`Attestation.outcome_url`). Per-PR stats for the other repositories need a key change
in a later release (deferred).

Nothing here commits: every write lands in the caller's transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import forges, merge_lane_state
from app.models.merge_lane import MergeLaneRow
from app.models.projects import Project, ProjectRepo
from app.models.task_extras import TASK_PR_STATES, TaskPr
from app.models.tasks import Task
from app.project_repos import repos_of

OPEN, MERGED, CLOSED = TASK_PR_STATES

#: The settle's refusal codes (`routes/agent/task_terminal.py::settle_task` step 4b); each leads its 422 detail.
UNKNOWN_PR_REPO = "unknown_pr_repo"
UNMERGED_PRS = "unmerged_prs"

#: The most PRs one task records: a task changes a few repositories, and every task list reads them all.
MAX_PRS_PER_TASK = 50


class PrRefused(ValueError):
    """A write this module will not make. `code` names the rule; `detail` is the whole sentence for the caller."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Repo:
    """One repository a task's PR can be under: a `project_repos` row (`id`), or — for a project whose row the
    outgoing release has not yet adopted — the projected primary itself (`id` None)."""

    id: Optional[int]
    ref: forges.RepoRef
    primary: bool

    @property
    def key(self) -> tuple[str, str]:
        return self.ref.forge, self.ref.path.lower()


@dataclass(frozen=True)
class AttestedPr:
    repo: Repo
    number: int
    url: str


@dataclass(frozen=True)
class Attestation:
    """An accepted settle's PRs. `outcome_url` is what `tasks.outcome_pr_url` and the one stats/provenance row
    name: the primary repository's attested PR, else the runner's `pr_url`."""

    prs: tuple[AttestedPr, ...]
    outcome_url: str


def project_repos(session: Session, project: Project) -> list[Repo]:
    """The repositories a PR of this project's tasks can be under, primary first; a row whose URL no longer
    parses is skipped (nothing could be checked against it)."""
    rows: list[ProjectRepo] = repos_of(session, project.id)
    out: list[Repo] = []
    for row in rows:
        ref = forges.repo_ref_or_none(row)  # type: ignore[arg-type] — a row parses exactly as a project does
        if ref is not None:
            out.append(Repo(id=row.id, ref=ref, primary=row.is_primary))
    if not rows:
        ref = forges.repo_ref_or_none(project)
        if ref is not None:
            out.append(Repo(id=None, ref=ref, primary=True))
    return sorted(out, key=lambda repo: not repo.primary)


def _match(repos: Iterable[Repo], url: str) -> Optional[tuple[Repo, int]]:
    for repo in repos:
        number = repo.ref.pr_number(url)
        if number is not None:
            return repo, number
    return None


def _row_key(row: TaskPr) -> tuple[str, str]:
    return row.forge, row.repo_path.lower()


def _label(forge_path: str, number: int) -> str:
    return f"{forge_path} #{number}"


def observe(
    session: Session, task: Task, project: Project, *, repo_id: Optional[int], pr_url: str, pr_number: int,
    state: str, opened: bool,
) -> Optional[TaskPr]:
    """Record the PR the daemon saw `jaah-forge` create (`opened`) or view. Only a PR the task OPENED gets a row: a
    view updates a row that exists and records nothing else (None), so looking at someone else's PR never makes
    it this task's. `repo_id` None is the primary. Raises `PrRefused` (`unknown_repo` when `repo_id` is not one of
    this project's repositories, `pr_not_in_repo` when the URL is not pull request `pr_number` under it,
    `task_done` once the task is done, `too_many_prs` past `MAX_PRS_PER_TASK`). `merged` is final: a later read
    never reopens it."""
    if task.state == "done":
        raise PrRefused("task_done", f"task {task.id} is done, so its PRs are final — nothing was recorded")
    if state not in TASK_PR_STATES:
        raise PrRefused("bad_state", f"state {state!r} is not one of {', '.join(TASK_PR_STATES)}")
    repos = project_repos(session, project)
    repo = next((r for r in repos if (r.id == repo_id if repo_id is not None else r.primary)), None)
    if repo is None:
        raise PrRefused(
            "unknown_repo",
            f"repository {repo_id} is not connected to this task's project, so no PR of it is recorded",
        )
    number = repo.ref.pr_number(pr_url)
    if number is None or number != pr_number:
        raise PrRefused(
            "pr_not_in_repo",
            f"{pr_url!r} is not pull request #{pr_number} of {repo.ref.web_url()}, the repository this call "
            f"came from — nothing was recorded",
        )
    existing = _find(session, task.id, repo, number)
    now = datetime.now(timezone.utc)
    if existing is None:
        if not opened:
            return None
        count = session.scalar(select(func.count()).select_from(TaskPr).where(TaskPr.task_id == task.id)) or 0
        if count >= MAX_PRS_PER_TASK:
            raise PrRefused(
                "too_many_prs", f"task {task.id} already records {MAX_PRS_PER_TASK} PRs — nothing was recorded")
        # A concurrent first observation of the same URL lands once (the loser re-reads the winner's row).
        session.execute(
            insert(TaskPr)
            .values(
                org_id=task.org_id, task_id=task.id, project_repo_id=repo.id, repo_path=repo.ref.path,
                forge=repo.ref.forge, pr_number=number, pr_url=pr_url.strip(), is_primary=repo.primary,
                state=state, merged_at=now if state == MERGED else None,
            )
            .on_conflict_do_nothing(constraint="uq_task_prs_task_url")
        )
        return _find(session, task.id, repo, number)
    if existing.state != MERGED:
        _move(session, existing, state, now)
    return existing


def _move(session: Session, row: TaskPr, state: str, now: datetime) -> None:
    """One row to `state`; reaching `merged` stamps `merged_at` once. A Core update (this table's `state` is a PR's,
    never a task's: a repository check reads ORM `.state =` writes as `Task.state`)."""
    values: dict[str, object] = {"state": state, "updated_at": now}
    if state == MERGED:
        values["merged_at"] = func.coalesce(TaskPr.merged_at, now)
    session.execute(update(TaskPr).where(TaskPr.id == row.id).values(**values))
    session.refresh(row)


def _find(session: Session, task_id: int, repo: Repo, number: int) -> Optional[TaskPr]:
    return session.scalars(
        select(TaskPr).where(
            TaskPr.task_id == task_id, TaskPr.forge == repo.ref.forge,
            func.lower(TaskPr.repo_path) == repo.ref.path.lower(), TaskPr.pr_number == number,
        )
    ).first()


def attest(session: Session, task: Task, project: Project, pr_url: str, pr_urls: Iterable[str]) -> Attestation:
    """Judge a `merged_pr` settle's PRs WITHOUT writing: an `Attestation`, or `PrRefused` with `UNKNOWN_PR_REPO` /
    `UNMERGED_PRS` (or `no_repo` when the project has no repository a PR could be checked against)."""
    repos = project_repos(session, project)
    if not repos:
        raise PrRefused(
            "no_repo",
            f"task {task.id}: this task's project repo cannot be resolved, so pr_url cannot be checked — nothing "
            f"was recorded",
        )
    attested: list[AttestedPr] = []
    seen: set[tuple[tuple[str, str], int]] = set()
    for raw in [pr_url, *pr_urls]:
        url = (raw or "").strip()
        if not url:
            continue
        found = _match(repos, url)
        if found is None:
            where = ", ".join(repo.ref.web_url() for repo in repos)
            raise PrRefused(
                UNKNOWN_PR_REPO,
                f"{UNKNOWN_PR_REPO}: task {task.id}: {url!r} does not name a pull request under {where}, which "
                f"{'is' if len(repos) == 1 else 'are'} this task's project repo{'' if len(repos) == 1 else 's'} — "
                f"nothing was recorded",
            )
        repo, number = found
        if (repo.key, number) not in seen:
            seen.add((repo.key, number))
            attested.append(AttestedPr(repo=repo, number=number, url=url))

    # A PR of a repository since disconnected cannot be attested (no repository to check it against), so it does
    # not hold the task back either.
    connected = {repo.key for repo in repos}
    recorded = [
        r for r in session.scalars(select(TaskPr).where(TaskPr.task_id == task.id).order_by(TaskPr.id))
        if _row_key(r) in connected
    ]
    attested_keys = {(pr.repo.key, pr.number) for pr in attested}
    # A repository is delivered when a PR of it is attested now, or one was already seen merged.
    delivered_repos = {pr.repo.key for pr in attested} | {_row_key(r) for r in recorded if r.state == MERGED}
    unmerged = [
        r for r in recorded
        if (r.state == OPEN and (_row_key(r), r.pr_number) not in attested_keys)
        or (r.state in (OPEN, CLOSED) and _row_key(r) not in delivered_repos)
    ]
    if unmerged:
        named = "; ".join(f"{_label(r.repo_path, r.pr_number)} ({r.state}) {r.pr_url}" for r in unmerged)
        raise PrRefused(
            UNMERGED_PRS,
            f"{UNMERGED_PRS}: task {task.id} opened PR(s) that this settle does not attest as merged: {named}. "
            f"A task is done only once every PR it opened has merged: watch each to `state: merged` and name "
            f"every merged PR in `pr_url` + `pr_urls` (a repository whose only PR was closed unmerged needs a "
            f"merged replacement) — nothing was recorded",
        )
    primary = next((pr.url for pr in attested if pr.repo.primary), None) or next(
        (r.pr_url for r in recorded if r.is_primary and r.state == MERGED), None)
    return Attestation(prs=tuple(attested), outcome_url=primary or attested[0].url)


def record_attested(session: Session, task: Task, attestation: Attestation) -> None:
    """Mark every attested PR `merged` (inserting those never observed), in the settle's transaction."""
    now = datetime.now(timezone.utc)
    for pr in attestation.prs:
        existing = _find(session, task.id, pr.repo, pr.number)
        if existing is not None:
            _move(session, existing, MERGED, now)
            continue
        session.execute(
            insert(TaskPr)
            .values(
                org_id=task.org_id, task_id=task.id, project_repo_id=pr.repo.id, repo_path=pr.repo.ref.path,
                forge=pr.repo.ref.forge, pr_number=pr.number, pr_url=pr.url, is_primary=pr.repo.primary,
                state=MERGED, merged_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_task_prs_task_url", set_={"state": MERGED, "merged_at": now, "updated_at": now},
            )
        )
    session.flush()


def prs_by_task(session: Session, task_ids: Iterable[int]) -> dict[int, list[TaskPr]]:
    """Every listed task's PRs, primary first then in the order they were opened, in one select."""
    ids = set(task_ids)
    out: dict[int, list[TaskPr]] = {task_id: [] for task_id in ids}
    if not ids:
        return out
    rows = session.scalars(
        select(TaskPr).where(TaskPr.task_id.in_(ids))
        .order_by(TaskPr.task_id, TaskPr.is_primary.desc(), TaskPr.opened_at, TaskPr.id)
    )
    for row in rows:
        out[row.task_id].append(row)
    return out


def close_lane_rows_on_no_pr(session: Session, task_id: int, reason: str) -> int:
    """A runner settled `no_pr`, so no PR of this task will merge: its open or failed merge-lane rows
    (`queued`/`failed`) go to `closed`, and the lane neither batches nor lists them. A `testing` row is its batch's
    to end (closed under it, the run's next event on it is refused); the runner closes the PR, so the lane's
    pinned merge fails and reports it. The CP never calls a forge. Returns the count; does not commit."""
    open_states = (merge_lane_state.QUEUED, merge_lane_state.FAILED)
    result = session.execute(
        update(MergeLaneRow)
        .where(MergeLaneRow.task_id == task_id, MergeLaneRow.state.in_(open_states))
        # A dict, not `state=`: this is a lane row's state, never a task's (see `_move`).
        .values({
            "state": merge_lane_state.CLOSED,
            "note": f"Closed: the task settled without a PR ({reason})",
            "updated_at": func.now(),
        })
        .execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount or 0)
