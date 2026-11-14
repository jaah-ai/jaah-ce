"""The `releases.state` vocabulary and its transition table — one home for both, like `app/task_lifecycle.py`.

A Release is a request to move one stage's branch head into the next stage's branch. Its state is the request's
lifecycle position only; the deploy run's facts stay in the `deployments` ledger, linked by `deployment_id`.

```
requested ──(approvals_required>0)──▶ awaiting_approval ──(N approvals)──▶ scheduled|queued
requested ──(scheduled_for in future)──▶ scheduled ──(due & not frozen)──▶ queued
requested ──▶ queued ──(executor claims)──▶ merging ──(merge_sha reported)──▶ deploying ──(ledger anchor)──▶ live
any non-terminal ──▶ canceled
merging|deploying ──▶ failed                failed ──(Retry)──▶ queued
merging ──(target not deploy-tracked)──▶ shipped ──(a deploy of it lands within 2 h)──▶ live
failed{deploy_failed|no_deploy} ──(a re-run of its deploy lands within 2 h)──▶ live
deploying ──(no anchor within 2 h)──▶ failed{no_deploy, open_run}
queued ──(unclaimed 5 min, no executor running)──▶ failed{node|not_started, retry}
```

`requested` is never stored: a request is routed (`route`) before its row is written. Stdlib-only at import.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

REQUESTED = "requested"
AWAITING_APPROVAL = "awaiting_approval"
SCHEDULED = "scheduled"
QUEUED = "queued"
MERGING = "merging"
DEPLOYING = "deploying"
LIVE = "live"
SHIPPED = "shipped"
FAILED = "failed"
CANCELED = "canceled"

#: Every value `releases.state` may hold (the CHECK renders from it). `requested` is the pre-insert routing point.
STATES: tuple[str, ...] = (AWAITING_APPROVAL, SCHEDULED, QUEUED, MERGING, DEPLOYING, LIVE, SHIPPED, FAILED, CANCELED)
TERMINAL: frozenset[str] = frozenset({LIVE, SHIPPED, FAILED, CANCELED})
#: The non-terminal states. Several releases into one stage may hold one at once (a step's concurrency, R6).
OPEN: tuple[str, ...] = tuple(s for s in STATES if s not in TERMINAL)
#: The states of a release under way on its target: at most one per (project, to_stage) (`uq_releases_running`).
RUNNING: tuple[str, ...] = (MERGING, DEPLOYING)
#: An open release that has not started merging: Newest wins may supersede it.
NOT_STARTED: tuple[str, ...] = (AWAITING_APPROVAL, SCHEDULED, QUEUED)

#: `release_stages.concurrency` (snapshot key `concurrency`): a new request supersedes the waiting ones, or queues.
CONCURRENCY_NEWEST = "newest"
CONCURRENCY_IN_ORDER = "in_order"
#: The states whose release is on its target stage: the waiting set difference reads them.
ARRIVED: frozenset[str] = frozenset({LIVE, SHIPPED})

KIND_PROMOTE = "promote"
KIND_HOTFIX = "hotfix"
KIND_BACKMERGE = "backmerge"
#: Some of a step's waiting changes, cherry-picked onto the target (a promote carries them all).
KIND_PICK = "pick"
#: Reverts a failed arrival on its stage (architecture §4.4; the executor's flow: child E).
KIND_ROLLBACK = "rollback"
KINDS: tuple[str, ...] = (KIND_PROMOTE, KIND_HOTFIX, KIND_BACKMERGE, KIND_PICK, KIND_ROLLBACK)

#: `failure.action`: the one thing a person can do about a failure.
FAILURE_ACTIONS: tuple[str, ...] = ("retry", "connect", "open_pr", "open_run")


@dataclass(frozen=True)
class Transition:
    name: str
    sources: frozenset[str]
    targets: frozenset[str]


def _t(name: str, sources: set[str] | frozenset[str], targets: set[str]) -> Transition:
    return Transition(name, frozenset(sources), frozenset(targets))


TRANSITIONS: tuple[Transition, ...] = (
    # A new request, routed by `route` before its row exists.
    _t("request", {REQUESTED}, {AWAITING_APPROVAL, SCHEDULED, QUEUED}),
    # The last required approval landed.
    _t("approve", {AWAITING_APPROVAL}, {SCHEDULED, QUEUED}),
    # A scheduled release came due outside every freeze (the tick's).
    _t("due", {SCHEDULED}, {QUEUED}),
    _t("claim", {QUEUED}, {MERGING}),
    _t("merged", {MERGING}, {DEPLOYING, SHIPPED}),
    # The ledger anchored it (`app/releases/anchoring.py`); a `shipped` one too, once a deploy of it is seen, and one
    # failed for want of a deploy when a re-run lands.
    _t("anchor", {DEPLOYING, SHIPPED, FAILED}, {LIVE}),
    # `queued`: the tick's node-offline failure; the rest the executor's or the ledger's.
    _t("fail", {QUEUED, MERGING, DEPLOYING}, {FAILED}),
    _t("retry", {FAILED}, {QUEUED}),
    _t("cancel", frozenset(OPEN), {CANCELED}),
)
TRANSITION_BY_NAME: Mapping[str, Transition] = MappingProxyType({t.name: t for t in TRANSITIONS})


class IllegalTransition(ValueError):
    """`name` does not move a release from `source` to `target`."""


def check(name: str, source: str, target: str) -> None:
    """Refuse a move the table does not hold: the single guard every writer calls."""
    transition = TRANSITION_BY_NAME.get(name)
    if transition is None or source not in transition.sources or target not in transition.targets:
        raise IllegalTransition(f"release transition {name!r} does not move {source!r} to {target!r}")


def route(*, approvals_required: int, approvals: int, scheduled: bool) -> str:
    """Where a request (or a fully approved one) goes next: approval first, then the schedule, else the queue."""
    if approvals < approvals_required:
        return AWAITING_APPROVAL
    return SCHEDULED if scheduled else QUEUED


def transitions_from(state: str) -> tuple[Transition, ...]:
    return tuple(t for t in TRANSITIONS if state in t.sources)
