"""Blind review children of a coder task (architecture §4.6, C6a): `reviewer`, `security_reviewer` and `tester`.

A coder runner declares its change's review tier (`review_findings.declare_tier`). When the project runs Blind review
(`projects.blind_review`, offered only with the plan's intelligent routing) and the tier asks for it (A7: Major →
reviewer + tester; Security → reviewer + security_reviewer + tester; any other tier → none), `on_tier` files one
`ready` child per missing persona under the coder (`parent_task_id`, no dependency edge). Each child reviews the PR
the coder last reported to the merge lane (`tasks.review_target`), blind:

* its body is the coder's title and body plus a fixed role brief (`compose_body`) — never the PR body, the coder's
  report or settle summary, its commit messages, transcript or Q&A (R8);
* its prerequisite documents are the parent epic's current PRD and architecture (`epic_inputs`);
* its tree is the PR's merge-base with the diff applied uncommitted (`lease_blind_apply`, run by the node's
  `blind_checkout`), so it places only on a node advertising that launch kind (`node_can_run`);
* the peer reads refuse it the coder task and its artifacts (`hidden_peer_ids`).

Its settle (`review` terminal) writes its findings against the CODER task (`record_findings`). Advisory only
(decision D2): nothing reads a child's state or findings for the coder, the merge lane, the epic rollup or Finish.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import plan_terms, review_findings
from app.agent_router import decisions
from app.agent_router.personas import REVIEW_PERSONAS
from app.events import emit_console_event
from app.models.merge_lane import MergeLaneRow
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import Project
from app.models.review import ReviewFinding
from app.models.tasks import Task
from app.outcome_ledger import writer as outcome_writer
from app.task_branch import task_branch

log = logging.getLogger(__name__)

#: The `launch_kinds` entry a node reports when it can prepare a blind tree (`apps/jaahd/blind_checkout.py`).
BLIND_CHECKOUT = "blind_checkout"
#: Which children each declared tier asks for (A7). Any other tier asks for none.
#: `security` asks for every review persona on purpose: a persona added to `REVIEW_PERSONAS` joins that tier.
TIER_PERSONAS: dict[str, tuple[str, ...]] = {
    "major": ("reviewer", "tester"),
    "security": REVIEW_PERSONAS,
}
#: The children's dedup key prefix; the open-row unique index keys it per (project, persona).
DEDUP_PREFIX = "blind-review-"
_LABELS = {"reviewer": "Review", "security_reviewer": "Security review", "tester": "Test review"}

#: What a refused Blind review toggle says (the project form shows it beside the switch).
NOT_ON_PLAN = "Blind review needs intelligent routing, which your plan doesn't include."
ON_MICROVM = "Blind review isn't available on MicroVM nodes."
NO_CAPABLE_NODE = ("Blind review needs a node that can check out a pull request blind; update the Jaah package on "
                   "one of this project's nodes first.")

# ── The role brief ───────────────────────────────────────────────────────────────────────────────────

_FOCUS = {
    "reviewer": "Review the change for correctness, maintainability and fit to the spec below.",
    "security_reviewer": ("Review the change for security defects: authentication and authorization, secrets, "
                          "injection, data exposure, unsafe defaults."),
    "tester": ("Judge whether the change is adequately tested: run the tests that cover it, and name the behaviour "
               "the spec asks for that no test proves. Report findings only; write no tests."),
}

_BRIEF = """You are a blind reviewer of another agent's change. {focus}

Your worktree is the pull request's merge-base with the change applied as UNCOMMITTED edits.
- Inspect only that uncommitted diff (`git diff`, `git diff --stat`, and the files it touches).
- Do not inspect git history (`git log`, `git show`, `git blame`, the reflog) and do not `jaah-forge pr view` or
  otherwise read the authoring pull request: its description and commits are the author's own account, and this
  review must not see it.
- Never read an agent's transcripts or session state (its home configuration directory, `.omc/`), and do not use
  the project memory tools: they can carry the author's own account.
- Never edit, commit or push.

Settle with `task_complete`, `terminal_type: "review"`, and `findings`: one entry per defect you found (severity
blocker/major/minor/info, category {category}, file and line where it applies, a one-line title and the detail).
An empty list is a valid result. Your findings are advisory: they never block or delay the change."""

_CATEGORY = {"reviewer": "correctness", "security_reviewer": "security", "tester": "test"}


def compose_body(coder: Task, persona: str) -> str:
    """A child's whole body: the fixed role brief, then the coder task's title and body — the spec the change was
    built against. Nothing else of the coder's is read."""
    brief = _BRIEF.format(focus=_FOCUS[persona], category=_CATEGORY[persona])
    return (f"{brief}\n\n--- The change's spec (the coder task's title and body) ---\n"
            f"# {coder.title}\n\n{coder.body or ''}")


# ── Is Blind review on ───────────────────────────────────────────────────────────────────────────────


def _project_of(session: Session, task: Task) -> Optional[Project]:
    return session.scalars(
        select(Project).where(Project.id == task.project_id, Project.org_id == task.org_id)).first()


def _task_in_org(session: Session, task_id: Optional[int], org_id: Any) -> Optional[Task]:
    if task_id is None:
        return None
    return session.scalars(select(Task).where(Task.id == task_id, Task.org_id == org_id)).first()


def _plan_on(session: Session, project: Project) -> bool:
    org = session.scalars(select(Org).where(Org.id == project.org_id)).first()
    return org is not None and plan_terms.intelligent_routing(session, org)


def active(session: Session, project: Project) -> bool:
    """Blind review runs for `project`: its own switch, under the plan's intelligent routing (D6)."""
    return bool(project.blind_review) and _plan_on(session, project)


def node_can_run(node_row: Any) -> bool:
    """Whether a node (any row or object carrying `launch_kinds` and `microvm`) can prepare a blind tree: it reports
    `blind_checkout` and is not a MicroVM node."""
    kinds = getattr(node_row, "launch_kinds", None) or ()
    return BLIND_CHECKOUT in kinds and not bool(getattr(node_row, "microvm", False))


def capable_node_ids(session: Session, node_ids: frozenset, org_id: Any = None) -> frozenset:
    """The enabled ids among `node_ids` (of `org_id`, when given) that `node_can_run`."""
    if not node_ids:
        return frozenset()
    query = select(Node.id, Node.launch_kinds, Node.microvm).where(Node.id.in_(node_ids), Node.enabled.is_(True))
    if org_id is not None:
        query = query.where(Node.org_id == org_id)
    rows = session.execute(query).all()
    return frozenset(row.id for row in rows if node_can_run(row))


def toggle_refusal(*, plan_on: bool, on_microvm: bool, capable: bool) -> Optional[str]:
    """Why Blind review cannot be turned on, as the sentence the form shows; None when it can."""
    if not plan_on:
        return NOT_ON_PLAN
    if on_microvm:
        return ON_MICROVM
    if not capable:
        return NO_CAPABLE_NODE
    return None


# ── Spawning ─────────────────────────────────────────────────────────────────────────────────────────


def _reported_pr(session: Session, coder: Task) -> Optional[MergeLaneRow]:
    return session.scalars(
        select(MergeLaneRow).where(MergeLaneRow.task_id == coder.id)
        .order_by(MergeLaneRow.created_at.desc(), MergeLaneRow.id.desc()).limit(1)
    ).first()


def review_target(session: Session, coder: Task, project: Project) -> Optional[dict[str, Any]]:
    """What the children review: the PR the coder last reported to the merge lane, or None when it never reported
    a head commit there."""
    row = _reported_pr(session, coder)
    if row is None or not row.head_sha:
        return None
    return {
        "coder_task_id": coder.id, "pr_number": row.pr_number, "pr_url": row.pr_url,
        "base_branch": row.base_branch, "head_branch": task_branch(project.branch_prefix, coder.id),
        "head_sha": row.head_sha,
    }


def _children(session: Session, coder: Task) -> list[Task]:
    return list(session.scalars(
        select(Task).where(Task.parent_task_id == coder.id, Task.org_id == coder.org_id,
                           Task.persona.in_(REVIEW_PERSONAS)).order_by(Task.id)
    ))


def _spawn(session: Session, coder: Task, persona: str, target: dict[str, Any]) -> Optional[Task]:
    child = Task(
        project_id=coder.project_id, org_id=coder.org_id,
        title=f"{_LABELS[persona]}: {coder.title}"[:500], body=compose_body(coder, persona),
        # A hidden coder's children stay hidden with it: their body is its spec.
        persona=persona, state="ready", hidden=coder.hidden, language=coder.language, autonomous=True,
        parent_task_id=coder.id,
        dedup_key=f"{DEDUP_PREFIX}{coder.id}", review_target=target,
        created_by_id=coder.created_by_id, started_by_id=coder.started_by_id,
    )
    try:
        with session.begin_nested():
            session.add(child)
            session.flush()
    except IntegrityError:
        return None  # A concurrent declaration filed it.
    emit_console_event(session, entity="task", id=child.id, action="ready")
    log.info("blind review: filed %s task %s for coder task %s", persona, child.id, coder.id)
    return child


def spawn(session: Session, task: Task, tier: str) -> list[Task]:
    """A coder's declared tier: file each blind child the tier asks for that the coder does not have yet (an upgrade
    adds only the missing ones). Returns the new children."""
    wanted = TIER_PERSONAS.get(tier, ())
    if task.persona != "coder" or not wanted:
        return []
    project = _project_of(session, task)
    if project is None or not active(session, project):
        return []
    target = review_target(session, task, project)
    if target is None:
        log.info("blind review: coder task %s declared %s but reported no PR head; nothing filed", task.id, tier)
        return []
    have = {child.persona for child in _children(session, task)}
    return [child for persona in wanted if persona not in have
            for child in [_spawn(session, task, persona, target)] if child is not None]


@review_findings.on_tier_declared
def on_tier(session: Session, task: Task, tier: str) -> None:
    """The registered hook: `spawn`, its result dropped."""
    spawn(session, task, tier)


def status(session: Session, coder: Task) -> Optional[str]:
    """The coder's Routing panel line about Blind review, or None when it has nothing to say."""
    if coder.persona != "coder":
        return None
    children = _children(session, coder)
    if children:
        return "Blind review of this task's pull request (advisory: never blocks its merge):"
    if coder.review_tier not in TIER_PERSONAS:
        return None
    project = _project_of(session, coder)
    if project is None or not active(session, project):
        return None
    if review_target(session, coder, project) is None:
        return ("Blind review: nothing was filed, because this task never reported its pull request's head commit "
                "to the merge lane.")
    return None


def runner_of(session: Session, child: Task) -> Optional[str]:
    """Which runner reviews `child`, in words: what ran its latest dispatch, else its live decision's pick; None
    before the router decided."""
    from app.models.routing import FINAL, LIVE_STATUSES, RoutingDecision

    row = session.scalars(
        select(RoutingDecision).where(RoutingDecision.task_id == child.id,
                                      RoutingDecision.status.in_((*LIVE_STATUSES, FINAL)))
        .order_by(RoutingDecision.created_at.desc()).limit(1)
    ).first()
    if row is None:
        return None
    pick = row.actual if row.status == FINAL and row.actual else (
        row.router_pick if row.used == "router" else row.default_pick)
    return decisions.describe(session, pick) if pick else None


def children_out(session: Session, coder: Task) -> list[dict[str, Any]]:
    """The coder's review children for its Routing panel: id, role, state and runner."""
    return [{"task_id": c.id, "role": _LABELS[c.persona], "state": c.state, "runner": runner_of(session, c)}
            for c in _children(session, coder)]


# ── The child's dispatch ─────────────────────────────────────────────────────────────────────────────


def lease_blind_apply(task: Optional[Task]) -> Optional[dict[str, Any]]:
    """The `lease_slot` intent's `blind_apply` (`apps/jaahd/blind_checkout.py::BlindApply`) for a review child, else
    None."""
    target = getattr(task, "review_target", None) if task is not None else None
    if task is None or task.persona not in REVIEW_PERSONAS or not target:
        return None
    return {key: target.get(key) for key in ("head_branch", "head_sha", "base_branch", "pr_number")}


def epic_inputs(session: Session, child: Task) -> list[Task]:
    """The tasks whose current PRD and architecture a review child reads: the coder's epic's live (unarchived)
    `business_analysis` (or `app_wizard`, a new web app's PRD) and `architecture` children, newest first, so a replaced stage never wins. Empty for a coder
    with no epic."""
    coder = _task_in_org(session, child.parent_task_id, child.org_id)
    if coder is None or coder.parent_task_id is None:
        return []
    return list(session.scalars(
        select(Task).where(Task.parent_task_id == coder.parent_task_id, Task.org_id == child.org_id,
                           Task.archived_at.is_(None), Task.persona.in_(("business_analysis", "app_wizard", "architecture")))
        .order_by(Task.id.desc())
    ))


def hidden_peer_ids(session: Session, caller: Task) -> tuple[int, ...]:
    """The tasks a caller's peer reads must not see, for a review child: the reviewed coder task and the coder's epic
    (whose final report and metrics restate the coder's work and name its PR). A review child also never sees a
    merge-lane run (`hides_merge_lane`)."""
    if caller.persona not in REVIEW_PERSONAS or caller.parent_task_id is None:
        return ()
    coder = _task_in_org(session, caller.parent_task_id, caller.org_id)
    epic = coder.parent_task_id if coder is not None else None
    return (caller.parent_task_id,) + ((epic,) if epic is not None else ())


def hides_merge_lane(caller: Task) -> bool:
    """A review child never reads a merge-lane run: its settled output names the PRs it batched."""
    return caller.persona in REVIEW_PERSONAS


# ── Findings ─────────────────────────────────────────────────────────────────────────────────────────


def record_findings(session: Session, child: Task, raw: Any) -> list:
    """A review child's settle: its findings, written against the coder task it reviewed under its blind lane. The
    detecting configuration is what ran the child. Lenient (D2): a malformed entry is dropped."""
    coder = _task_in_org(session, child.parent_task_id, child.org_id)
    if coder is None or child.persona not in REVIEW_PERSONAS:
        return []
    parsed = review_findings.parse_findings(raw)
    # The coder's row locked first, so concurrent child settles cannot read the same count and overrun the cap.
    session.execute(select(Task.id).where(Task.id == coder.id).with_for_update())
    held = session.scalar(select(func.count()).select_from(ReviewFinding).where(ReviewFinding.task_id == coder.id))
    parsed = parsed[: max(0, review_findings.MAX_FINDINGS_PER_TASK - (held or 0))]
    if not parsed:
        return []
    target = child.review_target or {}
    ids = review_findings.record(
        session, coder, parsed, lane=f"blind_{child.persona}", detecting_task=child,
        detecting_config=decisions.actual_config(child),
        routing_decision_id=review_findings.authoring_decision_id(session, coder),
        head_sha=target.get("head_sha"),
    )
    # The coder's ledger row counts the security ones under this blind lane (metric #7's facts): attribution only.
    outcome_writer.add_findings(session, coder.id, review_findings.security_by_lane(session, ids))
    return ids
