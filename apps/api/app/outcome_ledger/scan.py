"""The outcome scan (Architecture §8): the CP half of the hidden `outcome_scan` runner.

The CP never calls a forge. Facts it cannot see — reverts, fix-forward PRs, per-head CI, a lane PR's
commit list — are read by a regular runner: `file_due` (from the ledger tick, every 15 minutes) files one hidden
`outcome_scan` task for each due project; its runner runs `tools/outcome_scan.py`, which asks `plan` what to watch
and reports raw facts through `ingest`, page by page; `detectors.py` judges them into proposed links, stored by
`writer.propose_link`. The model judges nothing.

* **Due** — `last filed + scan_interval(project) ≤ now`, and the project has a landed row inside its survival
  window (plus a day's grace) or, on a non-lane project, a runner head with no CI verdict yet. An idle project
  files nothing.
* **One open per project** — an open scan blocks the next; one open longer than the interval raises the stale-scan
  alarm (one per project, `cron_alarms`), resolved once it ends.
* **Git person** — the project's "Run jobs as" (`git_access.project_forge_person`); none → nothing filed, an alarm, and
  `status(...).git_person = false` for the console.
* **CI security scans** — a failing security check (`security.py`) on an origin's merge sha is one `ci_scan`
  finding against the origin task (`review_findings`), counted on the origin's row; a re-scan adds nothing.
* **Cursor** — the integration-branch head and time the last accepted report covered; moved only by the `last`
  page, so a scan that dies mid-report re-reads from the old cursor next time (the detectors are idempotent).

Wire contract: `apps/api/tests/fixtures/outcome_scan_wire.json`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import cron_alarms, git_access, review_findings
from app.clock import parse_utc, utcnow as _now
from app.events import emit_console_event
from app.models.outcomes import TaskOutcome, TaskOutcomeHead
from app.models.projects import Project
from app.models.review import ReviewFinding
from app.models.task_extras import TaskPr, TaskPrChanges
from app.models.tasks import Task
from app.outcome_ledger import detectors, security, settings, states, writer

log = logging.getLogger("outcome-ledger")

SCAN_PERSONA = "outcome_scan"
#: Task states a scan no longer holds the project in.
_ENDED = ("done", "system_failed")
#: Per-call caps of one report page (the toolbox pages under them; a page over them is cut, never refused).
CAPS = {"commits": 500, "prs": 200, "checks": 500, "pr_commits": 50}
#: How long past its survival window a landed row is still watched (a revert on the window's last day).
_GRACE = timedelta(days=1)
#: A non-lane head whose CI never settled is `unknown` after this.
_HEAD_CI_HORIZON = timedelta(hours=48)
_ORIGINS_MAX = 500
_HEADS_MAX = 500
_TASKS_MAX = 500
_SHA = re.compile(r"\A[0-9a-fA-F]{7,64}\Z")
_PR_NUMBER = re.compile(r"/(?:pull|pulls|merge_requests|pullrequest|pullrequests|pullRequest)/(\d+)(?:[/?#].*)?\Z")

NO_GIT_PERSON = "no_git_person"
NO_REPOSITORY = "no_repository"
ALREADY_OPEN = "open"
REASONS = {
    NO_GIT_PERSON: "No one to run it as: set \"Run jobs as\" on the project or the org (the org has no Owner/Admin).",
    NO_REPOSITORY: "The project has no repository.",
    ALREADY_OPEN: "A scan is already running.",
}


def dedup_key(project_id: int) -> str:
    return f"outcome-scan:{project_id}"


def stale_alarm_key(project_id: int) -> str:
    return f"{cron_alarms.OUTCOME_SCAN_KEY_PREFIX}stale:{project_id}"


def person_alarm_key(project_id: int) -> str:
    return f"{cron_alarms.OUTCOME_SCAN_KEY_PREFIX}no-git-person:{project_id}"


def pr_number_of(url: Optional[str]) -> Optional[int]:
    """The PR number a forge PR URL ends in (GitHub `/pull/N`, GitLab `/merge_requests/N`, Bitbucket
    `/pull-requests/N`, Azure DevOps `/pullrequest/N`), or None."""
    if not url:
        return None
    match = _PR_NUMBER.search(url) or re.search(r"/pull-requests/(\d+)(?:[/?#].*)?\Z", url)
    return int(match.group(1)) if match else None


# ── Filing ────────────────────────────────────────────────────────────────────────────────────────────


def open_scan(session: Session, project_id: int) -> Optional[Task]:
    return session.scalars(
        select(Task).where(Task.project_id == project_id, Task.persona == SCAN_PERSONA, Task.state.not_in(_ENDED))
        .order_by(Task.id.desc()).limit(1)
    ).first()


def _lane_active(session: Session, project: Project) -> bool:
    from app.merge_lane_state import lane_state

    return lane_state(session, project).active


def _watched(session: Session, project: Project, now: datetime) -> bool:
    """Whether the project has anything to scan: a landed row in its window, or a non-lane head with no verdict."""
    landed = session.scalar(select(func.count()).select_from(TaskOutcome).where(
        TaskOutcome.project_id == project.id, TaskOutcome.excluded.is_(False),
        TaskOutcome.outcome_state.in_(states.LANDED_STATES), TaskOutcome.survival_ends_at > now - _GRACE,
    ))
    if landed:
        return True
    return bool(_pending_heads(session, project, now, limit=1, shadow_only=_lane_active(session, project)))


def is_due(session: Session, project: Project, now: datetime) -> bool:
    last = project.outcome_scan_last_filed_at
    if last is not None and last + timedelta(minutes=settings.scan_interval(session, project).minutes) > now:
        return False
    return _watched(session, project, now)


def file_scan(session: Session, project: Project, *, now: Optional[datetime] = None) -> tuple[Optional[Task], str]:
    """File the project's hidden `outcome_scan` task now (the tick's due filing, or an Owner/Admin's Scan now).
    Returns `(task, "")` when filed, else `(the open scan or None, reason)`. Does not commit."""
    now = now or _now()
    existing = open_scan(session, project.id)
    if existing is not None:
        return existing, ALREADY_OPEN
    if not project.repo_url:
        return None, NO_REPOSITORY
    person = git_access.project_forge_person(session, project)
    if person is None:
        cron_alarms.raise_alarm(
            session, dedup_key=person_alarm_key(project.id),
            title=f"Outcome scan: no \"Run jobs as\" person for {project.name}",
            body=(f"Project {project.name} (id {project.id}) has merged work to judge, but nobody can run its "
                  "jobs: \"Run jobs as\" is unset (or names someone who left) on the project and its org, and the "
                  "org has no live Owner or Admin to fall back to. An org Admin sets it in Settings (org default) or "
                  "on the project. Reverts and fix-forward PRs are not detected until then (outcome rows still "
                  "survive on their window)."),
        )
        return None, NO_GIT_PERSON
    task = Task(
        project_id=project.id, org_id=project.org_id, title=f"Outcome scan: {project.name}",
        body=("Outcome scan: read the integration branch's new commits, the PRs merged since the last scan and the CI "
              "results of watched heads, and report them. Filed by the control plane "
              "(`app/outcome_ledger/scan.py::file_scan`)."),
        dedup_key=dedup_key(project.id), persona=SCAN_PERSONA, state="ready", hidden=True, parent_task_id=None,
        created_by_id=person, started_by_id=person,
    )
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        # A concurrent filing holds the key: its scan serves just as well.
        return open_scan(session, project.id), ALREADY_OPEN
    project.outcome_scan_last_filed_at = now
    resolve_stale(session, project.id)  # A new scan files only once the last one ended.
    emit_console_event(session, entity="task", id=task.id, action="ready")
    log.info("outcome-ledger: filed outcome scan task %s for project %s", task.id, project.id)
    return task, ""


def _stale(session: Session, project: Project, task: Task, now: datetime) -> bool:
    started = task.created_at or now
    return started + timedelta(minutes=settings.scan_interval(session, project).minutes) < now


def file_due(session: Session) -> int:
    """The tick's part (f): file a scan for each due project, and raise or resolve each project's stale-scan
    alarm. Each project in its own savepoint; commits. Returns how many scans were filed."""
    now = _now()
    candidates = set(session.scalars(select(TaskOutcome.project_id).where(
        TaskOutcome.project_id.is_not(None), TaskOutcome.excluded.is_(False),
        or_(
            TaskOutcome.outcome_state.in_(states.LANDED_STATES) & (TaskOutcome.survival_ends_at > now - _GRACE),
            TaskOutcome.id.in_(select(TaskOutcomeHead.outcome_id).where(
                TaskOutcomeHead.verdict == "pending", TaskOutcomeHead.verdict_source.is_(None),
                TaskOutcomeHead.pushed_at > now - _HEAD_CI_HORIZON,
            )),
        ),
    ).distinct()))
    open_projects = set(session.scalars(select(Task.project_id).where(
        Task.persona == SCAN_PERSONA, Task.state.not_in(_ENDED))))
    _resolve_ended(session, open_projects)
    filed = 0
    for project_id in sorted(p for p in candidates | open_projects if p is not None):
        try:
            with session.begin_nested():
                project = session.get(Project, project_id)
                if project is None:
                    continue
                running = open_scan(session, project_id)
                if running is not None:
                    if _stale(session, project, running, now):
                        cron_alarms.raise_alarm(
                            session, dedup_key=stale_alarm_key(project_id),
                            title=f"Outcome scan stale: {project.name}",
                            body=(f"Outcome scan task {running.id} of project {project.name} (id {project_id}) has been "
                                  "open longer than the project's scan interval; no further scan is filed while it is "
                                  "open. Open the task to see why it has not settled."),
                        )
                    continue
                if is_due(session, project, now):
                    task, reason = file_scan(session, project, now=now)
                    filed += task is not None and not reason
        except Exception:  # noqa: BLE001 — one project never stops the pass
            log.exception("outcome-ledger: could not file the outcome scan for project %s", project_id)
    session.commit()
    return filed


def _resolve_ended(session: Session, open_projects: set[Optional[int]]) -> None:
    """Resolve each open stale-scan alarm whose project no longer has an open scan (it ended, due again or not)."""
    prefix = stale_alarm_key(0)[:-1]
    keys = session.scalars(select(Task.dedup_key).where(
        Task.dedup_key.like(f"{prefix}%"), Task.state.not_in(_ENDED))).all()
    for key in keys:
        tail = (key or "")[len(prefix):]
        if tail.isdecimal() and int(tail) not in open_projects:
            cron_alarms.resolve_alarm(session, dedup_key=key, summary="The outcome scan ended.")


def resolve_stale(session: Session, project_id: int) -> None:
    """A scan settled: its project's stale and no-person alarms, if any, are resolved."""
    cron_alarms.resolve_alarm(session, dedup_key=stale_alarm_key(project_id), summary="The outcome scan settled.")
    cron_alarms.resolve_alarm(session, dedup_key=person_alarm_key(project_id), summary="An outcome scan ran.")


# ── The watch list (`outcome_scan.plan`) ──────────────────────────────────────────────────────────────


def _origin_rows(session: Session, project: Project, now: datetime) -> list[TaskOutcome]:
    return list(session.scalars(
        select(TaskOutcome).where(
            TaskOutcome.project_id == project.id, TaskOutcome.excluded.is_(False),
            TaskOutcome.outcome_state.in_(states.LANDED_STATES), TaskOutcome.merged_at.is_not(None),
            TaskOutcome.survival_ends_at > now - _GRACE,
        ).order_by(TaskOutcome.merged_at.desc()).limit(_ORIGINS_MAX)
    ))


def _changes(session: Session, row: TaskOutcome, pr_number: Optional[int]) -> Optional[TaskPrChanges]:
    if row.task_id is None or pr_number is None:
        return None
    return session.get(TaskPrChanges, (row.task_id, pr_number))


def _origins(session: Session, project: Project, now: datetime) -> list[tuple[TaskOutcome, detectors.Origin]]:
    out = []
    for row in _origin_rows(session, project, now):
        number = pr_number_of(row.pr_url)
        changes = _changes(session, row, number)
        commit_shas = tuple(
            str(c.get("sha")).lower() for c in (changes.commits if changes is not None else [])
            if isinstance(c, dict) and isinstance(c.get("sha"), str)
        )
        files = tuple(str(f) for f in (changes.files if changes is not None else []) if isinstance(f, str))
        own = frozenset(session.scalars(select(TaskPr.pr_number).where(TaskPr.task_id == row.task_id))) \
            if row.task_id is not None else frozenset()
        merge_sha = writer.merge_sha_of(session, row)  # A row merged before `on_merged` read the provenance.
        out.append((row, detectors.Origin(
            outcome_id=row.id, pr_number=number, pr_url=row.pr_url, merge_sha=merge_sha, merged_at=row.merged_at,
            window_ends_at=row.survival_ends_at, title=row.task_title or "", commit_shas=commit_shas, files=files,
            own_prs=own,
        )))
    return out


def _pending_heads(session: Session, project: Project, now: datetime, *, limit: int = _HEADS_MAX,
                   shadow_only: bool = False) -> list[Any]:
    """Runner heads of the project with no CI verdict yet, pushed in the last 48 hours, with their row's PR URL.
    `shadow_only` (a lane project, whose lane judges every other head): only benchmark shadows' heads, which the lane
    never sees — the forge's CI is their gate (`outcome_ledger/benchmark.py`)."""
    query = (
        select(TaskOutcomeHead, TaskOutcome.pr_url).join(TaskOutcome, TaskOutcome.id == TaskOutcomeHead.outcome_id)
        .where(
            TaskOutcome.project_id == project.id, TaskOutcomeHead.verdict == "pending",
            TaskOutcomeHead.verdict_source.is_(None), TaskOutcomeHead.pushed_at > now - _HEAD_CI_HORIZON,
            TaskOutcomeHead.source == "runner_push",
        )
    )
    if shadow_only:
        query = query.where(TaskOutcome.shadow.is_(True))
    return list(session.execute(query.order_by(TaskOutcomeHead.pushed_at.desc()).limit(limit)).all())


def _need_commits(session: Session, row: TaskOutcome) -> bool:
    """A lane-merged origin whose PR commit list is not known yet (the runner settled before the merge, so no
    provenance was captured) and whose runner pushes were reported — what `set_commit_facts` needs."""
    if row.enqueued_at is None or row.nonrunner_commits is not None:
        return False
    return bool(session.scalar(select(func.count()).select_from(TaskOutcomeHead).where(
        TaskOutcomeHead.outcome_id == row.id, TaskOutcomeHead.source == "runner_push",
        func.cardinality(TaskOutcomeHead.pushed_shas) > 0,
    )))


def _since_at(project: Project, origins: list[tuple[TaskOutcome, detectors.Origin]]) -> Optional[datetime]:
    """Where the merged-PR read starts: the cursor, else the oldest watched merge. A fix of an origin merged after
    the cursor is merged after it too."""
    if project.outcome_scan_cursor_at is not None:
        return project.outcome_scan_cursor_at
    merged = [row.merged_at for row, _ in origins if row.merged_at is not None]
    return min(merged) if merged else None


def plan(session: Session, task: Task) -> dict[str, Any]:
    """`outcome_scan.plan`: the project's watch list (wire: `verbs."outcome_scan.plan".response`)."""
    from app import forges

    project = session.get(Project, task.project_id)
    if project is None:
        raise LookupError("the scan's project is gone")
    now = _now()
    origins = _origins(session, project, now)
    ref = forges.repo_ref_or_none(project)
    heads = [
        {"head_sha": head.head_sha, "pr_number": pr_number_of(head.pr_url or pr_url)}
        for head, pr_url in _pending_heads(session, project, now, shadow_only=_lane_active(session, project))
    ]
    since_at = _since_at(project, origins)
    return {
        "project_id": project.id,
        "forge": ref.forge if ref is not None else project.forge,
        "integration_branch": project.integration_branch,
        "since_sha": project.outcome_scan_cursor_sha,
        "since_at": since_at.isoformat() if since_at is not None else None,
        "origins": [
            {"outcome_id": str(o.outcome_id), "pr_number": o.pr_number, "pr_url": o.pr_url, "merge_sha": o.merge_sha,
             "commit_shas": list(o.commit_shas), "files": list(o.files), "title": o.title}
            for _, o in origins
        ],
        "heads": heads,
        "need_commits": sorted({o.pr_number for row, o in origins if o.pr_number is not None
                                and _need_commits(session, row)}),
        # The merge shas whose checks the toolbox reads for CI security scans: recent merges only (a merge sha's
        # CI settles within hours), so a long survival window costs no forge call per scan.
        "check_shas": [o.merge_sha for _, o in origins
                       if o.merge_sha and o.merged_at is not None and o.merged_at > now - _HEAD_CI_HORIZON],
        "caps": dict(CAPS),
    }


# ── The report (`outcome_scan.report`) ────────────────────────────────────────────────────────────────


class ReportInvalid(ValueError):
    """The report's shape is wrong in a way no page could mean (not a list, a bad page number)."""


#: The longest string any reported field keeps (the toolbox sends bodies cut at 4000 characters).
_FIELD_CHARS = 4000
_LIST_ITEMS = 500


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_FIELD_CHARS]
    if isinstance(value, list):
        return [_clip(v) for v in value[:_LIST_ITEMS]]
    return value


def _dicts(body: dict, key: str) -> list[dict]:
    value = body.get(key) or []
    if not isinstance(value, list):
        raise ReportInvalid(f"{key} must be a list")
    return [{k: _clip(v) for k, v in item.items()} for item in value if isinstance(item, dict)][: CAPS[key]]


def _later_tasks(session: Session, project: Project, since: datetime) -> list[dict]:
    """The project's visible tasks filed after `since` — the evidence `detectors.task_links` reads — with their
    parent, so each origin can drop its own task chain (`_chain_free`). Hidden system tasks never count."""
    rows = session.execute(
        select(Task.id, Task.title, Task.body, Task.created_at, Task.parent_task_id).where(
            Task.project_id == project.id, Task.created_at > since, Task.hidden.is_(False),
        ).order_by(Task.id.desc()).limit(_TASKS_MAX)
    ).all()
    return [{"id": r.id, "title": r.title, "body": (r.body or "")[:_FIELD_CHARS], "created_at": r.created_at,
             "parent_task_id": r.parent_task_id} for r in rows]


def _chain_free(tasks: list[dict], origin_task: Optional[Task]) -> list[dict]:
    """`tasks` without the origin's own chain: the origin itself, its children, and its epic siblings (a follow-up
    of the same epic citing the PR is the plan, not a defect)."""
    if origin_task is None:
        return tasks
    family = {origin_task.id, origin_task.parent_task_id} - {None}
    return [t for t in tasks if t["id"] not in family and t["parent_task_id"] not in family]


def _head_verdicts(session: Session, project: Project, checks: list[dict], now: datetime) -> int:
    """Per-head forge CI (A4) on a non-lane project — and a benchmark shadow's heads on any project — each pending
    runner head the page carries checks for gets its combined verdict once it is no longer pending."""
    by_sha: dict[str, list[str]] = {}
    for check in checks:
        sha = str(check.get("sha") or "").lower()
        if _SHA.match(sha):
            by_sha.setdefault(sha, []).append(str(check.get("state") or ""))
    written = 0
    for head, _ in _pending_heads(session, project, now, shadow_only=_lane_active(session, project)):
        states_ = by_sha.get(head.head_sha.lower())
        if states_ is None or head.task_id is None:
            continue
        verdict = detectors.combined_ci(states_)
        if verdict != "pending":
            writer.set_head_verdict(session, head.task_id, head.head_sha, verdict, "forge_ci")
            written += 1
    return written


def _ci_findings(session: Session, project: Project, origins: list[tuple[TaskOutcome, detectors.Origin]],
                 checks: list[dict]) -> int:
    """The failing security checks on the origins' merge shas, each recorded once as a `ci_scan` finding against its
    origin task and counted on the origin row. Returns how many were new."""
    by_sha = {o.merge_sha.lower(): (row, o.merge_sha) for row, o in origins if o.merge_sha and row.task_id is not None}
    if not by_sha:
        return 0
    active = security.patterns(project)
    found = 0
    for check in checks:
        row, merge_sha = by_sha.get(str(check.get("sha") or "").lower(), (None, None))
        finding = security.ci_finding(check, active) if row is not None else None
        if row is None or finding is None or merge_sha is None:
            continue
        task = session.scalars(
            select(Task).where(Task.id == row.task_id, Task.org_id == project.org_id)
        ).first()
        if task is None or _ci_recorded(session, task.id, merge_sha, finding.title):
            continue
        try:
            # The finding and its count land together: a finding with no count would be skipped by every later
            # scan (`_ci_recorded`) and never counted.
            with session.begin_nested():
                ids = review_findings.record(session, task, [finding], lane=security.CI_LANE,
                                             detecting_config={"kind": security.CI_LANE}, head_sha=merge_sha)
                if writer.add_findings(session, task.id, {security.CI_LANE: len(ids)}, outcome_id=row.id) is None:
                    raise LookupError("the origin row was not counted")
        except LookupError:
            log.warning("outcome scan: CI finding %r on %s not recorded", finding.title, merge_sha)
            continue
        found += 1
    return found


def _ci_recorded(session: Session, task_id: int, sha: str, title: str) -> bool:
    """Whether this check is already a finding on `sha`, or `sha` holds its cap of CI findings."""
    titles = list(session.scalars(select(ReviewFinding.title).where(
        ReviewFinding.task_id == task_id, ReviewFinding.detecting_lane == security.CI_LANE,
        ReviewFinding.head_sha == sha,
    )))
    return title in titles or len(titles) >= security.MAX_CI_FINDINGS_PER_SHA


def _expire_heads(session: Session, project: Project, now: datetime) -> None:
    """A non-lane head whose CI never settled inside the horizon is `unknown`."""
    stale = session.scalars(
        select(TaskOutcomeHead).join(TaskOutcome, TaskOutcome.id == TaskOutcomeHead.outcome_id).where(
            TaskOutcome.project_id == project.id, TaskOutcomeHead.verdict == "pending",
            TaskOutcomeHead.verdict_source.is_(None), TaskOutcomeHead.pushed_at <= now - _HEAD_CI_HORIZON,
            TaskOutcomeHead.source == "runner_push",
        ).limit(_HEADS_MAX)
    ).all()
    for head in stale:
        if head.task_id is not None:
            writer.set_head_verdict(session, head.task_id, head.head_sha, "unknown", "forge_ci")


def ingest(session: Session, task: Task, body: dict[str, Any]) -> dict[str, Any]:
    """`outcome_scan.report`: judge one page and store its links, head verdicts and PR commit facts; the `last`
    page moves the cursor. Lenient like every runner-fed fact: malformed items are dropped, never refused. Does
    not commit. Response: wire `verbs."outcome_scan.report".response`."""
    project = session.get(Project, task.project_id)
    if project is None:
        raise LookupError("the scan's project is gone")
    page = body.get("page", 1)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ReportInvalid("page must be a positive integer")
    last = body.get("last") is True
    commits, prs = _dicts(body, "commits"), _dicts(body, "prs")
    checks, pr_commits = _dicts(body, "checks"), _dicts(body, "pr_commits")
    now = _now()
    origins = _origins(session, project, now)
    rows = {str(row.id): row for row, _ in origins}
    forge = plan_forge(project)
    merge_times = [o.merged_at for _, o in origins if o.merged_at is not None]
    later = _later_tasks(session, project, min(merge_times)) if last and merge_times else []
    links = 0
    for row, origin in origins:
        tasks = _chain_free(later, session.get(Task, row.task_id) if row.task_id is not None else None)
        for proposal in detectors.detect([origin], commits=commits, prs=prs, tasks=tasks, forge=forge):
            link = writer.propose_link(
                session, rows[str(proposal.origin_outcome_id)], kind=proposal.kind,
                evidence_kind=proposal.evidence_kind, evidence_ref=proposal.evidence_ref,
                evidence_title=proposal.evidence_title, confidence=proposal.confidence,
                defect_state=proposal.defect_state,
            )
            links += link is not None
    _head_verdicts(session, project, checks, now)
    findings = _ci_findings(session, project, origins, checks)
    by_number = {o.pr_number: row for row, o in origins if o.pr_number is not None}
    for item in pr_commits:
        number, shas = item.get("pr_number"), item.get("shas")
        merged = by_number.get(number) if isinstance(number, int) else None
        if merged is not None and merged.task_id is not None and isinstance(shas, list):
            writer.set_commit_facts(session, merged.task_id, pr_number=number, pr_commits=shas)
    cursor_sha = None
    if last:
        sha = body.get("cursor_sha")
        if isinstance(sha, str) and _SHA.match(sha):
            cursor_sha = sha.lower()
            project.outcome_scan_cursor_sha = cursor_sha
            project.outcome_scan_cursor_at = parse_utc(body.get("cursor_at")) or now
        _expire_heads(session, project, now)
    session.flush()
    return {"accepted": True, "page": page, "links": links, "findings": findings, "cursor_sha": cursor_sha}


def plan_forge(project: Project) -> str:
    from app import forges

    ref = forges.repo_ref_or_none(project)
    return ref.forge if ref is not None else (project.forge or "github")


# ── The console's read model ──────────────────────────────────────────────────────────────────────────


def status(session: Session, project: Project) -> dict[str, Any]:
    """What project settings (and the Command center's Performance tab) show about the project's scan."""
    interval = settings.scan_interval(session, project)
    running = open_scan(session, project.id)
    now = _now()
    return {
        "last_filed_at": project.outcome_scan_last_filed_at.isoformat() if project.outcome_scan_last_filed_at else None,
        "cursor_sha": project.outcome_scan_cursor_sha,
        "cursor_at": project.outcome_scan_cursor_at.isoformat() if project.outcome_scan_cursor_at else None,
        "interval_minutes": interval.minutes,
        "interval_source": interval.source,
        "git_person": git_access.project_forge_person(session, project) is not None,
        "open_task_id": running.id if running is not None else None,
        "stale": bool(running is not None and _stale(session, project, running, now)),
    }
